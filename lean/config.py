"""Lean Mode configuration — reads from config.yaml `lean:` section and validates."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("lean-mode")

_HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))


def _find_hermes_default_model() -> str:
    """Read the main model ID from Hermes config.yaml.

    Looks for the first custom_providers entry's `model:` field, which
    is the default model for the main agent. Returns '' if not found.
    Uses json/yaml if available, otherwise a simple line-by-line parser.
    """
    config_path = os.path.join(_HERMES_HOME, "config.yaml")
    try:
        with open(config_path) as f:
            text = f.read()
    except Exception:
        return ""

    # Try yaml first
    try:
        import yaml
        data = yaml.safe_load(text)
        providers = data.get("custom_providers", [])
        for p in providers:
            model = p.get("model", "")
            if model:
                return model
        return ""
    except ImportError:
        pass

    # Fallback: line-by-line parser for YAML without nesting comprehension
    import re
    in_providers = False
    bracket_depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "custom_providers:":
            in_providers = True
            continue
        if in_providers:
            bracket_depth += stripped.count("[") - stripped.count("]")
            bracket_depth += stripped.count("{") - stripped.count("}")
            if bracket_depth < 0:
                bracket_depth = 0
            if stripped.startswith("- "):  # new list entry
                # Check for model: directly inside list item
                if "model:" in stripped:
                    m = re.search(r'model:\s*["\']?([^\s"\'#}]+)', stripped)
                    if m:
                        return m.group(1)
            elif stripped.startswith("model:"):
                # Indented model: key inside a list item
                m = re.search(r'model:\s*["\']?([^\s"\'#}]+)', stripped)
                if m:
                    return m.group(1)
            elif bracket_depth == 0 and not line.startswith(" "):
                # Back to top-level (line starts at column 0, no indent)
                in_providers = False
    return ""


@dataclass
class PriceEntry:
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


@dataclass
class PresetEntry:
    toolsets: Optional[List[str]] = None
    model: Optional[str] = None
    timeout_seconds: Optional[int] = None
    budget_usd: Optional[float] = None
    context_hint: Optional[str] = None


@dataclass
class LeanConfig:
    model: str = ""
    parent_model: str = ""  # model ID of main agent, for receipt comparison line
    provider: str = ""
    toolsets: List[str] = field(default_factory=lambda: ["web", "search", "file"])
    timeout_seconds: int = 300
    gateway_max_timeout_seconds: int = 120
    poll_interval_seconds: float = 3.0
    budget_usd: Optional[float] = None
    escalate: bool = True
    escalate_on: List[str] = field(default_factory=lambda: [
        "failed", "empty_result", "max_iterations", "low_confidence", "launch_failed"
    ])
    min_escalation_seconds: int = 60
    max_brief_chars: int = 6000
    max_result_chars: int = 8000
    prices: Dict[str, PriceEntry] = field(default_factory=dict)
    presets: Dict[str, PresetEntry] = field(default_factory=dict)
    audit_retention_lines: int = 1000

    @classmethod
    def from_config(cls, raw: Optional[Dict[str, Any]]) -> "LeanConfig":
        if not raw:
            return cls()
        cfg = cls()
        cfg.model = raw.get("model", "")
        if cfg.model == "combo":
            logger.warning(
                "lean.model set to 'combo' — this is the MAIN combo model, "
                "not a cheap model. You probably want surplus/glm-5.3-flash."
            )
        cfg.parent_model = raw.get("parent_model", "")
        if not cfg.parent_model:
            cfg.parent_model = _find_hermes_default_model()
            if cfg.parent_model:
                logger.info("auto-resolved parent_model to %s from Hermes config", cfg.parent_model)
        cfg.provider = raw.get("provider", "")
        if isinstance(raw.get("toolsets"), list):
            cfg.toolsets = raw["toolsets"]
        cfg.timeout_seconds = raw.get("timeout_seconds", 300)
        cfg.gateway_max_timeout_seconds = raw.get("gateway_max_timeout_seconds", 120)
        cfg.poll_interval_seconds = raw.get("poll_interval_seconds", 3.0)
        cfg.budget_usd = raw.get("budget_usd") or None
        cfg.escalate = raw.get("escalate", True)
        if isinstance(raw.get("escalate_on"), list):
            cfg.escalate_on = raw["escalate_on"]
        cfg.min_escalation_seconds = raw.get("min_escalation_seconds", 60)
        cfg.max_brief_chars = raw.get("max_brief_chars", 6000)
        cfg.max_result_chars = raw.get("max_result_chars", 8000)
        cfg.audit_retention_lines = raw.get("audit_retention_lines", 1000)
        prices_raw = raw.get("prices", {})
        if isinstance(prices_raw, dict):
            for model_id, entry in prices_raw.items():
                if isinstance(entry, dict):
                    cfg.prices[model_id] = PriceEntry(
                        input=entry.get("input", 0.0),
                        output=entry.get("output", 0.0),
                        cache_read=entry.get("cache_read", 0.0),
                        cache_write=entry.get("cache_write", 0.0),
                    )
        presets_raw = raw.get("presets", {})
        if isinstance(presets_raw, dict):
            for name, entry in presets_raw.items():
                if isinstance(entry, dict):
                    cfg.presets[name] = PresetEntry(
                        toolsets=entry.get("toolsets"),
                        model=entry.get("model"),
                        timeout_seconds=entry.get("timeout_seconds"),
                        budget_usd=entry.get("budget_usd"),
                        context_hint=entry.get("context_hint"),
                    )
        return cfg


LEAN_CONTEXT = """You are a LEAN worker — a cheap, cost-optimized subagent. Rules:
1. Be concise and direct. Do not over-explain.
2. Use only the tools available to you. Do not try to use skills or delegate.
3. If you cannot complete the task reliably — the task is too hard, data is unavailable,
   or you are uncertain about the result — end your FINAL answer with the exact line:
   LEAN_CONFIDENCE: low
   Otherwise end with:
   LEAN_CONFIDENCE: ok
4. The LEAN_CONFIDENCE line MUST be the very last line of your output, on its own line.
   Never include it mid-response.
5. You have no knowledge of the conversation history beyond what is provided in the brief below.
"""
