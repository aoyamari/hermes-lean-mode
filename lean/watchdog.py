"""Watchdog — wall-clock and dollar-budget watchdog for subagent runs."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from .config import LeanConfig, PriceEntry

logger = logging.getLogger("lean-mode.watchdog")


class RunState:
    """In-process tracked state for one lean run."""

    def __init__(self, correlation_id: str, started_at: float, budget_usd: Optional[float] = None):
        self.correlation_id = correlation_id
        self.child_session_id: Optional[str] = None
        self.subagent_id: Optional[str] = None  # handle.subagent_id (child._subagent_id)
        self.started_at = started_at
        self.budget_usd = budget_usd
        self.tokens_input = 0
        self.tokens_output = 0
        self.tokens_cache_read = 0
        self.tokens_cache_write = 0
        self.tokens_reasoning = 0
        self.api_calls = 0
        self.tokens_source: Optional[str] = None  # "hook" or "SubagentResult.usage_metadata"
        self.duration_seconds = 0.0
        self.model: Optional[str] = None
        self.provider: Optional[str] = None
        self.response_model: Optional[str] = None
        self.is_escalation = False
        self.escalation_trigger: Optional[str] = None
        self._seen_buckets: set = set()  # token keys present in post_api_request payloads
        # Per-call breakdown — appended by add_api_call()
        self.api_calls_detail: List[Dict[str, Any]] = []
        # From subagent_stop hook — tool_call_history + derived counts
        self.tool_call_history: List[Dict[str, Any]] = []
        self.turns_count: Optional[int] = None

    def add_api_call(self, usage: Dict[str, Any], model: str = "", response_model: str = ""):
        """Add tokens from a post_api_request hook."""
        self.api_calls += 1
        self.tokens_input += usage.get("input_tokens", 0) or 0
        self.tokens_output += usage.get("output_tokens", 0) or 0
        # Track which token buckets the provider actually returned
        for key in usage:
            if key.endswith("_tokens"):
                self._seen_buckets.add(key)
        self.tokens_cache_read += usage.get("cache_read_tokens", 0) or 0
        self.tokens_cache_write += usage.get("cache_write_tokens", 0) or 0
        self.tokens_reasoning += usage.get("reasoning_tokens", 0) or 0
        if model and not self.model:
            self.model = model
        if response_model:
            self.response_model = response_model
        # Per-call detail
        self.api_calls_detail.append({
            "input_tokens": usage.get("input_tokens", 0) or 0,
            "output_tokens": usage.get("output_tokens", 0) or 0,
            "cache_read_tokens": usage.get("cache_read_tokens", 0) or 0,
            "cache_write_tokens": usage.get("cache_write_tokens", 0) or 0,
            "reasoning_tokens": usage.get("reasoning_tokens", 0) or 0,
            "model": model or "",
            "response_model": response_model or "",
        })

    def total_tokens(self) -> int:
        return self.tokens_input + self.tokens_output + self.tokens_cache_read + self.tokens_cache_write

    def spent_usd(self, prices: Dict[str, PriceEntry], model_key: Optional[str] = None) -> float:
        """Compute estimated spend using price table."""
        key = model_key or self.response_model or self.model or ""
        price = prices.get(key)
        if not price:
            # Try parent model name
            for pk in prices:
                if pk in key or key in pk:
                    price = prices[pk]
                    break
        if not price:
            return 0.0  # Cannot compute without price entry

        total = 0.0
        total += self.tokens_input * (price.input / 1_000_000)
        total += self.tokens_output * (price.output / 1_000_000)
        total += self.tokens_cache_read * (price.cache_read / 1_000_000)
        total += self.tokens_cache_write * (price.cache_write / 1_000_000)
        # NOTE: reasoning_tokens are already INCLUDED in output_tokens
        # (completion_tokens = total output including reasoning).
        # Do NOT add them separately — that double-counts.
        return total


class Watchdog:
    """Monitors a run's time and budget. Returns outcome when done."""

    def __init__(
        self,
        cfg: LeanConfig,
        run: RunState,
        cancel_fn: Callable,
        wait_fn: Callable,
        result_fn: Callable,
        timeout_seconds: Optional[int] = None,
    ):
        self.cfg = cfg
        self.run = run
        self._cancel = cancel_fn
        self._wait = wait_fn
        self._result = result_fn
        self._timeout = timeout_seconds or cfg.timeout_seconds

    def monitor(self) -> Dict[str, Any]:
        """Run the watchdog loop.

        Returns a dict with outcome fields:
            status, result_text, spent, budget_status, error
        """
        deadline = self.run.started_at + self._timeout
        poll = self.cfg.poll_interval_seconds
        budget = self.run.budget_usd

        while True:
            remaining = deadline - time.monotonic()

            # Wall-clock check
            if remaining <= 0:
                return self._do_cancel("cancelled_by_watchdog", "lean watchdog: wall-clock budget exhausted")

            # Budget check
            if budget and budget > 0:
                spent = self.run.spent_usd(self.cfg.prices)
                if spent >= budget:
                    overshoot = spent - budget
                    overshoot_note = " (overshoot: in-flight call)" if overshoot > 0 else ""
                    outcome = self._do_cancel(
                        "cancelled_by_budget",
                        f"lean budget: ${spent:.4f} >= ${budget}{overshoot_note}",
                    )
                    outcome["budget_spent"] = spent
                    outcome["budget_limit"] = budget
                    return outcome

            # Wait for child progress
            wait_time = min(poll, remaining)
            try:
                outcome = self._wait(timeout_seconds=wait_time)
                if not outcome.timed_out:
                    # Child finished
                    result = self._result()
                    return self._process_result(result)
            except Exception as exc:
                logger.warning("Watchdog wait error: %s", exc)
                return {
                    "status": "failed",
                    "error": f"watchdog error: {exc}",
                    "result_text": "",
                    "spent": self.run.spent_usd(self.cfg.prices),
                }

        return self._process_result(self._result())

    def _do_cancel(self, status: str, reason: str) -> Dict[str, Any]:
        """Cancel the child and return outcome."""
        try:
            self._cancel(reason=reason)
            # Grace period
            self._wait(timeout_seconds=30)
            result = self._result()
        except Exception as exc:
            logger.warning("Cancel error: %s", exc)
            return {
                "status": status,
                "error": str(exc),
                "result_text": "",
                "spent": self.run.spent_usd(self.cfg.prices),
            }

        result_text = result.summary or ""
        error = result.error_message or ""
        if result.terminal_state.name in ("FAILED", "CANCELLED", "INTERRUPTED"):
            status_from_state = result.terminal_state.name.lower()
            if status == "cancelled_by_watchdog":
                pass  # keep watchdog status
            elif status == "cancelled_by_budget":
                pass
            else:
                status = status_from_state

        return {
            "status": status,
            "reason": reason,
            "result_text": result_text,
            "spent": self.run.spent_usd(self.cfg.prices),
            "error": error,
            "terminal_state": result.terminal_state.name,
            "_subagent_result": result,
        }

    def _process_result(self, result) -> Dict[str, Any]:
        """Process a completed SubagentResult."""
        state = result.terminal_state.name if hasattr(result, "terminal_state") else "UNKNOWN"
        summary = result.summary or ""
        error = result.error_message or ""
        error_cls = result.error_classification or ""
        spent = self.run.spent_usd(self.cfg.prices)

        base = {"_subagent_result": result}

        if state == "SUCCEEDED":
            # Check for LEAN_CONFIDENCE marker in summary
            has_low_confidence = summary.rstrip().endswith("LEAN_CONFIDENCE: low") if summary else False
            return {**base,
                "status": "low_confidence" if has_low_confidence else "succeeded",
                "result_text": _strip_confidence(summary),
                "spent": spent,
                "error": "",
                "terminal_state": state,
            }
        elif state == "FAILED":
            return {**base,
                "status": "failed",
                "result_text": summary,
                "spent": spent,
                "error": error or error_cls or "child failed",
                "terminal_state": state,
            }
        elif state == "CANCELLED":
            return {**base,
                "status": "failed",
                "result_text": summary,
                "spent": spent,
                "error": error or "cancelled",
                "terminal_state": state,
            }
        elif state == "INTERRUPTED":
            return {**base,
                "status": "max_iterations",
                "result_text": summary,
                "spent": spent,
                "error": error or "max iterations exceeded",
                "terminal_state": state,
            }
        else:
            return {**base,
                "status": "empty_result",
                "result_text": summary or "",
                "spent": spent,
                "error": error or f"unexpected state: {state}",
                "terminal_state": state,
            }


def _strip_confidence(text: str) -> str:
    """Remove the LEAN_CONFIDENCE marker line from result text."""
    if not text:
        return text
    lines = text.rstrip().split("\n")
    if lines and lines[-1].strip().startswith("LEAN_CONFIDENCE:"):
        return "\n".join(lines[:-1]).strip()
    return text
