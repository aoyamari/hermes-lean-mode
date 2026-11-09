"""Auto-escalation — detect failures and re-run on parent model."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

from .config import LeanConfig
from .launch import build_child_context, build_launch_request, INHERIT as _INHERIT
from .watchdog import RunState, Watchdog

logger = logging.getLogger("lean-mode.escalate")


def should_escalate(
    cfg: LeanConfig,
    status: str,
    skipped_reason: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Check if escalation should trigger based on cheap attempt outcome.

    Returns (should_escalate, reason_or_skip_reason).
    """
    if not cfg.escalate:
        return False, "escalation_disabled"

    if skipped_reason:
        return False, skipped_reason

    triggers = cfg.escalate_on
    if status in triggers:
        return True, None  # escalate

    return False, None  # no trigger matched


def run_escalation(
    cfg: LeanConfig,
    ctx: Any,
    cheap_outcome: Dict[str, Any],
    cheap_run: RunState,
    parent_session_id: Optional[str],
    task: str,
    brief: str,
    remaining_deadline: float,
    remaining_budget: Optional[float],
    parent_model: Optional[str] = None,
    parent_provider: Optional[str] = None,
    pending_map: Optional[Dict[str, str]] = None,
    child_map: Optional[Dict[str, str]] = None,
    active_runs: Optional[Dict[str, RunState]] = None,
) -> Optional[Dict[str, Any]]:
    """Launch escalation attempt with parent model + full toolsets.

    Escalation passes allowed_toolsets=None (inherit parent) so the
    escalation child has the SAME capability as the parent, regardless
    of lean.toolsets restriction.

    When pending_map and child_map are provided (the _PENDING / _CHILD_MAP
    dicts from the caller), the escalation child's post_api_request tokens
    are tracked — without them, escalation API calls are invisible.
    """
    now = time.monotonic()
    remaining_time = remaining_deadline - now

    if remaining_time < cfg.min_escalation_seconds:
        return None  # not enough time

    if remaining_budget is not None and remaining_budget <= 0:
        return None  # no budget left

    # Build escalation launch request with parent model + parent toolsets
    # allowed_toolsets=None = inherit parent's full toolsets
    esc_context = build_child_context(cfg, brief)
    esc_model = parent_model or None
    esc_provider = parent_provider or None

    esc_req = build_launch_request(
        cfg, task, esc_context, parent_session_id,
        model=esc_model, provider=esc_provider,
        toolsets=_INHERIT,  # inherit parent's toolsets
    )
    esc_correlation = f"{cheap_run.correlation_id}-esc"
    esc_req["correlation_id"] = esc_correlation

    # Create escalation run state
    esc_run = RunState(correlation_id=esc_correlation, started_at=now,
                       budget_usd=remaining_budget)
    esc_run.is_escalation = True
    esc_run.escalation_trigger = cheap_outcome.get("status", "unknown")
    esc_run.model = esc_model or ""

    # Register esc_run so post_api_request can find it
    if active_runs is not None:
        active_runs[esc_correlation] = esc_run

    try:
        handle = ctx.subagent_lifecycle.launch(esc_req)

        # Map escalation child session — pop from _PENDING, set _CHILD_MAP
        esc_child_session_id = None
        if pending_map is not None and child_map is not None:
            esc_child_session_id = pending_map.pop(handle.subagent_id, None)

        if esc_child_session_id:
            esc_run.child_session_id = esc_child_session_id
            if child_map is not None:
                child_map[esc_child_session_id] = esc_correlation
            logger.debug("mapped escalation %s -> session %s", esc_correlation, esc_child_session_id)
        else:
            # Fallback: subagent_id as child_session_id (won't get token tracking)
            if hasattr(handle, "subagent_id") and handle.subagent_id:
                esc_run.child_session_id = handle.subagent_id

        # Remaining wall-clock time for escalation
        esc_remaining = remaining_deadline - time.monotonic()
        watchdog = Watchdog(
            cfg=cfg,
            run=esc_run,
            cancel_fn=lambda **kw: ctx.subagent_lifecycle.cancel(handle, **kw),
            wait_fn=lambda **kw: ctx.subagent_lifecycle.wait(handle, **kw),
            result_fn=lambda: ctx.subagent_lifecycle.result(handle),
            timeout_seconds=max(1, int(esc_remaining)),
        )
        outcome = watchdog.monitor()
        outcome["is_escalation"] = True
        outcome["escalation_trigger"] = cheap_outcome.get("status", "unknown")
        outcome["escalation_cheap_cost"] = cheap_outcome.get("spent", 0.0)
        outcome["escalation_model"] = esc_model or esc_run.response_model or ""
        outcome["escalation_duration"] = time.monotonic() - now
        outcome["escalation_ended_at"] = time.time()
        outcome["ended_at"] = time.time()  # for audit ts

        # Merge escalation token aggregates into outcome for receipt
        outcome["escalation_tokens_input"] = esc_run.tokens_input
        outcome["escalation_tokens_output"] = esc_run.tokens_output
        outcome["escalation_cache_read"] = esc_run.tokens_cache_read
        outcome["escalation_cache_write"] = esc_run.tokens_cache_write
        outcome["escalation_reasoning"] = esc_run.tokens_reasoning
        outcome["escalation_api_calls"] = esc_run.api_calls
        outcome["escalation_api_calls_detail"] = esc_run.api_calls_detail
        outcome["escalation_run_state"] = esc_run  # for audit

        return outcome

    except Exception as exc:
        logger.warning("Escalation launch failed: %s", exc)
        return None
