"""Auto-escalation — detect failures and re-run on parent model."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import LeanConfig
from .launch import build_child_context, build_launch_request
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
    parent_toolsets: Optional[List[str]] = None,
    parent_model: Optional[str] = None,
    parent_provider: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Launch escalation attempt with parent model + toolsets.

    Returns escalated outcome dict with 'is_escalation' flag, or None if escalation
    was skipped.
    """
    now = time.monotonic()
    remaining_time = remaining_deadline - now

    if remaining_time < cfg.min_escalation_seconds:
        return None  # not enough time

    if remaining_budget is not None and remaining_budget <= 0:
        return None  # no budget left

    # Build escalation launch request with parent model + full toolsets
    esc_context = build_child_context(cfg, brief)
    esc_model = parent_model or None
    esc_provider = parent_provider or None
    esc_tools = list(parent_toolsets) if parent_toolsets else None

    esc_req = build_launch_request(
        cfg, task, esc_context, parent_session_id,
        model=esc_model, provider=esc_provider,
        toolsets=esc_tools,
    )
    esc_correlation = f"{cheap_run.correlation_id}-esc"
    esc_req["correlation_id"] = esc_correlation

    # Create escalation run state
    esc_run = RunState(correlation_id=esc_correlation, started_at=now,
                       budget_usd=remaining_budget)
    esc_run.is_escalation = True
    esc_run.escalation_trigger = cheap_outcome.get("status", "unknown")

    try:
        handle = ctx.subagent_lifecycle.launch(esc_req)
        if cheap_run.child_session_id:
            esc_run.child_session_id = cheap_run.child_session_id + "-esc"

        # Map escalation child session
        _map_child_session(esc_run, handle)

        # Watchdog for escalation (shorter remaining time)
        deadline = min(remaining_deadline, now + remaining_time)
        watchdog = Watchdog(
            cfg=cfg,
            run=esc_run,
            cancel_fn=lambda **kw: ctx.subagent_lifecycle.cancel(handle, **kw),
            wait_fn=lambda **kw: ctx.subagent_lifecycle.wait(handle, **kw),
            result_fn=lambda: ctx.subagent_lifecycle.result(handle),
        )
        outcome = watchdog.monitor()
        outcome["is_escalation"] = True
        outcome["escalation_trigger"] = cheap_outcome.get("status", "unknown")
        outcome["escalation_cheap_cost"] = cheap_outcome.get("spent", 0.0)
        outcome["escalation_model"] = esc_model
        return outcome

    except Exception as exc:
        logger.warning("Escalation launch failed: %s", exc)
        return None


def _map_child_session(esc_run: RunState, handle: Any):
    """Try to get child_session_id from handle."""
    try:
        if hasattr(handle, "subagent_id") and handle.subagent_id:
            esc_run.child_session_id = handle.subagent_id
    except Exception:
        pass
