"""Preset resolution for @research, @code, etc."""

from __future__ import annotations

from typing import List, Optional, Tuple

from .config import LeanConfig


def resolve_preset(
    cfg: LeanConfig,
    preset_name: str,
    cmd_toolsets: Optional[List[str]] = None,
    cmd_model: Optional[str] = None,
    cmd_timeout: Optional[int] = None,
    cmd_budget: Optional[float] = None,
) -> Tuple[str, Optional[List[str]], Optional[str], Optional[int], Optional[float]]:
    """Resolve preset into (context_hint, toolsets, model, timeout, budget).

    Explicit command flags override preset values. Returns tuple of resolved values.
    Raises KeyError if preset_name is unknown.
    """
    if preset_name not in cfg.presets:
        valid = ", ".join(sorted(cfg.presets.keys()))
        raise KeyError(f"Unknown preset '@{preset_name}'. Valid presets: {valid}")

    p = cfg.presets[preset_name]
    toolsets = cmd_toolsets or p.toolsets
    model = cmd_model or p.model
    timeout = cmd_timeout or p.timeout_seconds
    budget = cmd_budget or p.budget_usd
    return (p.context_hint or "", toolsets, model, timeout, budget)


def preset_names(cfg: LeanConfig) -> List[str]:
    """Return sorted list of configured preset names."""
    return sorted(cfg.presets.keys())
