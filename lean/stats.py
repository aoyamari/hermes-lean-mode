"""Statistics aggregation for `/quick stats`."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .audit import AuditLog

logger = logging.getLogger("lean-mode.stats")


def compute_stats(audit: AuditLog, days: int = 7) -> Dict[str, Any]:
    """Aggregate stats from audit log. Returns dict with summary fields.

    ``cost_usd_estimate`` at the top level = total (cheap + escalation).
    Escalated runs have status ``escalated_<cheap_status>``; they count
    as escalated but NOT as succeeded. ``total_saved`` is negative when
    escalation wasted cheap-model spend.
    """
    entries = audit.read_recent(days=days)

    total = len(entries)
    if total == 0:
        return {
            "period": f"last {days} days",
            "total_runs": 0,
            "succeeded": 0,
            "escalated": 0,
            "cancelled_budget": 0,
            "cancelled_watchdog": 0,
            "total_spent": 0.0,
            "total_saved": 0.0,
            "is_estimate": True,
            "unknown_cost_runs": 0,
            "top_preset": None,
        }

    succeeded = 0
    escalated = 0
    cancelled_budget = 0
    cancelled_watchdog = 0
    total_spent = 0.0
    total_saved = 0.0
    has_actual = True
    unknown_cost_runs = 0
    preset_counts: Dict[str, int] = {}

    for entry in entries:
        status = entry.get("status", "")
        escalated_info = entry.get("escalated")

        # escalated_* entries are escalated, not succeeded
        if status == "succeeded":
            succeeded += 1
        if escalated_info:
            escalated += 1
        if status == "cancelled_by_budget":
            cancelled_budget += 1
        if status == "cancelled_by_watchdog":
            cancelled_watchdog += 1

        # Total spent already includes cheap + escalation for escalated runs
        cost = entry.get("cost_usd_actual") or entry.get("cost_usd_estimate")
        if cost is None or cost == 0:
            unknown_cost_runs += 1
        else:
            total_spent += cost

        # Savings: escalated runs wasted the cheap-model spend
        if escalated_info:
            cheap_cost = escalated_info.get("cheap_cost_usd", 0)
            total_saved -= cheap_cost
        else:
            # Non-escalated: estimate saved vs parent
            # We don't have parent cost in audit entry directly
            pass

        preset = entry.get("preset")
        if preset:
            preset_counts[preset] = preset_counts.get(preset, 0) + 1

        if "cost_usd_actual" not in entry:
            has_actual = False

    top_preset = max(preset_counts, key=preset_counts.get) if preset_counts else None

    return {
        "period": f"last {days} days",
        "total_runs": total,
        "succeeded": succeeded,
        "escalated": escalated,
        "cancelled_budget": cancelled_budget,
        "cancelled_watchdog": cancelled_watchdog,
        "total_spent": total_spent,
        "total_saved": total_saved,
        "is_estimate": not has_actual,
        "unknown_cost_runs": unknown_cost_runs,
        "top_preset": top_preset,
    }
