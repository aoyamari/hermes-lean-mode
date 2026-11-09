"""Audit log — JSONL append + rotation."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import LeanConfig, PriceEntry

logger = logging.getLogger("lean-mode.audit")


class AuditLog:
    """Thread-safe audit log for /quick runs."""

    def __init__(self, data_dir: Path, retention_lines: int = 1000, max_bytes: int = 10 * 1024 * 1024):
        self.path = data_dir / "runs.jsonl"
        self.retention_lines = retention_lines
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: Dict[str, Any]) -> None:
        """Append one run entry to the audit log."""
        try:
            line = json.dumps(entry, default=str, ensure_ascii=False) + "\n"
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)

            # Check rotation
            if self.path.stat().st_size > self.max_bytes:
                self._rotate()
        except OSError as exc:
            logger.warning("Audit log append failed: %s", exc)

    def read_all(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Read all entries from the audit log. Returns list of dicts."""
        entries = []
        if not self.path.exists():
            return entries
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("Skipping unparseable audit line")
            if limit and len(entries) > limit:
                entries = entries[-limit:]
        except OSError as exc:
            logger.warning("Audit log read failed: %s", exc)
        return entries

    def read_recent(self, days: int = 7, now_ts: Optional[float] = None) -> List[Dict[str, Any]]:
        """Read entries within last N days."""
        import time
        cutoff = (now_ts or time.time()) - (days * 86400)
        entries = []
        for entry in self.read_all():
            ts = entry.get("ts", 0)
            if isinstance(ts, (int, float)) and ts >= cutoff:
                entries.append(entry)
            elif isinstance(ts, str):
                try:
                    et = float(ts)
                    if et >= cutoff:
                        entries.append(entry)
                except (ValueError, TypeError):
                    pass
        return entries

    def _rotate(self) -> None:
        """Rotate log: keep newest N lines."""
        try:
            entries = self.read_all()
            if len(entries) <= self.retention_lines:
                return
            kept = entries[-self.retention_lines:]
            with open(self.path, "w", encoding="utf-8") as f:
                for entry in kept:
                    f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("Audit log rotation failed: %s", exc)


def _reprice(tokens: Dict[str, int], model_key: str, prices: Dict[str, PriceEntry]) -> float:
    """Reprice token bucket at a given model's rates. Returns 0 if model not found."""
    price = prices.get(model_key)
    if not price:
        return 0.0
    total = 0.0
    total += tokens.get("input", 0) * (price.input / 1_000_000)
    total += tokens.get("output", 0) * (price.output / 1_000_000)
    total += tokens.get("cache_read", 0) * (price.cache_read / 1_000_000)
    total += tokens.get("cache_write", 0) * (price.cache_write / 1_000_000)
    return total


def build_audit_entry(
    correlation_id: str,
    status: str,
    outcome: Dict[str, Any],
    run_state: "RunState",  # noqa: F821
    cheap_outcome: Optional[Dict[str, Any]] = None,
    parent_model: Optional[str] = None,
    prices: Optional[Dict[str, PriceEntry]] = None,
    budget_usd: Optional[float] = None,
    preset: Optional[str] = None,
    result_hash: Optional[str] = None,
    toolsets_inherited: bool = False,
) -> Dict[str, Any]:
    """Build a JSON-serializable audit entry.

    For escalated runs, top-level ``status`` is prefixed with ``escated_``
    (e.g. ``escalated_succeeded``, ``escalated_failed``) and
    ``cost_usd_estimate`` = cheap + escalation cost. The original cheap
    attempt's status is recorded in ``escalated.status``.

    When ``prices`` and ``parent_model`` are both given, computes
    ``vs_non_lean_usd`` = repricing the tokens at the parent's rates.
    """
    now = outcome.get("ended_at", 0) or 0
    is_esc = outcome.get("is_escalation", False) or (cheap_outcome is not None)

    # Merge escalation tokens into top-level aggregates
    esc_input = outcome.get("escalation_tokens_input", 0) or 0
    esc_output = outcome.get("escalation_tokens_output", 0) or 0
    esc_cr = outcome.get("escalation_cache_read", 0) or 0
    esc_cw = outcome.get("escalation_cache_write", 0) or 0

    # Total cost = cheap (if any) + escalation
    cheap_cost = (cheap_outcome or {}).get("spent", 0.0) or 0.0
    esc_cost = outcome.get("spent", 0.0) or 0.0
    total_cost = cheap_cost + esc_cost if is_esc else esc_cost

    # Status reflects final outcome; for escalated runs, prefix
    display_status = f"escalated_{status}" if is_esc else status

    merged_tokens = {
        "input": (run_state.tokens_input or 0) + esc_input,
        "output": (run_state.tokens_output or 0) + esc_output,
        "cache_read": (run_state.tokens_cache_read or 0) + esc_cr,
        "cache_write": (run_state.tokens_cache_write or 0) + esc_cw,
    }

    # Compute vs_non_lean = repricing at parent rates
    vs_non_lean_usd = 0.0
    if prices and parent_model:
        vs_non_lean_usd = _reprice(merged_tokens, parent_model, prices)

    entry = {
        "ts": now,
        "correlation_id": correlation_id,
        "status": display_status,
        "model": run_state.response_model or run_state.model or "",
        "provider": run_state.provider or "",
        "toolsets": list(run_state.__dict__.get("_toolsets", [])),
        "duration_ms": int(outcome.get("duration", 0) * 1000),
        "tokens": merged_tokens,
        "cost_usd_estimate": total_cost,
        "vs_non_lean_usd": vs_non_lean_usd,
        "child_session_id": run_state.child_session_id or "",
        "result_hash": result_hash or "",
    }

    if toolsets_inherited:
        entry["toolsets_inherited"] = True

    # Per-call breakdown (only when available)
    api_detail = getattr(run_state, "api_calls_detail", None)
    if api_detail:
        entry["api_calls_detail"] = api_detail

    if outcome.get("budget_spent"):
        entry["cost_usd_actual"] = outcome.get("budget_spent")
    if budget_usd:
        entry["budget_usd"] = budget_usd
    if preset:
        entry["preset"] = preset

    # Escalation info
    if outcome.get("is_escalation") or (cheap_outcome and cheap_outcome.get("status") in
                                        ("failed", "empty_result", "max_iterations", "low_confidence", "launch_failed")):
        esc = {
            "trigger": outcome.get("escalation_trigger", cheap_outcome.get("status", "")),
            "model": outcome.get("escalation_model", ""),
            "status": status,  # escalation attempt's outcome (succeeded/failed/etc.)
            "cost_usd_estimate": esc_cost,
        }
        if cheap_outcome:
            esc["cheap_cost_usd"] = cheap_cost
        # Include escalation run's per-call detail
        esc_detail = outcome.get("escalation_api_calls_detail")
        if esc_detail:
            esc["api_calls_detail"] = esc_detail
        # Include escalation token aggregates
        for key in ("escalation_tokens_input", "escalation_tokens_output",
                     "escalation_cache_read", "escalation_cache_write",
                     "escalation_reasoning", "escalation_api_calls"):
            val = outcome.get(key)
            if val is not None:
                esc[key.replace("escalation_", "")] = val
        entry["escalated"] = esc

    return entry
