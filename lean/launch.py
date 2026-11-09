"""Request building and validation for subagent launches."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from .config import LeanConfig, LEAN_CONTEXT

logger = logging.getLogger("lean-mode.launch")

# Tools always excluded from cheap runs (no grandchildren, no memory, no skills)
_ALWAYS_EXCLUDED = {"delegate_task", "memory", "clarify", "send_message", "cronjob", "skills"}

# Sentinel: pass to build_launch_request(toolsets=INHERIT) to inherit parent's toolsets
INHERIT = object()


def build_child_context(cfg: LeanConfig, brief: str = "") -> str:
    """Build the context string for the child subagent.

    Concatenates LEAN_CONTEXT with operator's brief. Truncates brief at max_brief_chars.
    Returns the full context string and whether brief was truncated.
    """
    ctx = LEAN_CONTEXT
    if brief:
        if len(brief) > cfg.max_brief_chars:
            brief = brief[:cfg.max_brief_chars] + "\n\n[Brief truncated at max_brief_chars]"
        ctx += "\n\n## Brief from the main agent\n" + brief
    return ctx


def validate_toolsets_against_parent(cfg_toolsets: List[str], parent_toolsets: List[str]) -> Optional[str]:
    """Validate configured toolsets against parent's toolsets. Returns error or None."""
    unknown = [t for t in cfg_toolsets if t not in parent_toolsets and t not in _ALWAYS_EXCLUDED]
    if unknown:
        return f"Unknown toolsets in lean config: {', '.join(unknown)}. Valid: {', '.join(sorted(parent_toolsets))}"
    return None


def build_launch_request(
    cfg: LeanConfig,
    task: str,
    context: str,
    parent_session_id: Optional[str],
    model: Optional[str] = None,
    provider: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    parent_session_cwd: str = "",
) -> Dict[str, Any]:
    """Build a SubagentLaunchRequest-compatible dict.

    Returns dict ready for ctx.subagent_lifecycle.launch().
    Note: timeout_seconds cannot be passed (rejected by API). Use watchdog instead.
    """
    effective_model = model or cfg.model or None  # None = use delegation default
    effective_provider = provider or cfg.provider or None

    resolved_tools = None
    if toolsets is INHERIT:
        resolved_tools = None  # inherit parent — allowed_toolsets stays None
    elif toolsets is not None:
        resolved_tools = toolsets
    else:
        resolved_tools = cfg.toolsets

    # Exclude always-blocked tools
    if resolved_tools is not None:
        resolved_tools = [t for t in resolved_tools if t not in _ALWAYS_EXCLUDED]

    correlation_id = f"lean-{uuid.uuid4().hex[:12]}"

    req = {
        "goal": task,
        "context": context,
        "role": "leaf",
        "allowed_toolsets": tuple(resolved_tools) if resolved_tools else None,
        "correlation_id": correlation_id,
        "parent_session_id": parent_session_id,
    }
    if effective_model:
        req["model"] = effective_model
    if effective_provider:
        req["provider"] = effective_provider

    return req


def _exclude_excluded(tools: List[str]) -> List[str]:
    """Filter out always-excluded tools from a list."""
    return [t for t in tools if t not in _ALWAYS_EXCLUDED]
