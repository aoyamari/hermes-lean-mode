"""Cost receipt generation — estimate + state.db actual + comparison line."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .config import LeanConfig, PriceEntry
from .watchdog import RunState

logger = logging.getLogger("lean-mode.receipt")


def format_receipt(
    cfg: LeanConfig,
    run: RunState,
    outcome: Dict[str, Any],
    parent_model: Optional[str] = None,
    brief_was_used: bool = False,
    is_escalated: bool = False,
    escalation_cheap_cost: float = 0.0,
    escalation_trigger: Optional[str] = None,
    toolsets_inherited: bool = False,
) -> str:
    """Build the receipt string appended to results.

    Returns a multi-line string.
    """
    lines = []
    model_name = run.response_model or run.model or "unknown"
    cheap_model = model_name

    # Extract turns & tool_calls from SubagentResult.tool_execution_summary
    subagent_result = outcome.get("_subagent_result")
    tes = {}
    if subagent_result is not None and hasattr(subagent_result, "tool_execution_summary"):
        tes = subagent_result.tool_execution_summary or {}
    turns_str = str(tes.get("turns", "n/a")) if isinstance(tes, dict) else "n/a"
    tool_calls_str = str(tes.get("tool_calls", "n/a")) if isinstance(tes, dict) else "n/a"

    if is_escalated:
        # Escalated receipt
        cheap_cost = escalation_cheap_cost
        total_cost = outcome.get("spent", 0.0)  # escalation attempt cost
        grand_total = cheap_cost + total_cost

        lines.append(
            f"⚡ Lean receipt — ESCALATED ({escalation_trigger or 'unknown'}) "
            f"· cheap {cheap_model} ${cheap_cost:.4f} (wasted) "
            f"+ full {model_name} ${total_cost:.4f} = total ${grand_total:.4f}"
        )
        lines.append(f"saved: −${cheap_cost:.4f} vs running on {model_name} directly")
    else:
        tokens_in = run.tokens_input
        tokens_out = run.tokens_output
        cache_read = run.tokens_cache_read
        cache_write = run.tokens_cache_write
        has_cache = "cache_read_tokens" in run._seen_buckets or "cache_write_tokens" in run._seen_buckets
        tokens_cache = f"{cache_read}+{cache_write}" if has_cache else "n/a"
        api_calls = run.api_calls
        duration_s = outcome.get("duration", 0.0)

        spent = outcome.get("spent", 0.0)
        budget = run.budget_usd

        lines.append(
            f"⚡ Lean receipt — model {model_name} · {api_calls} calls · "
            f"{turns_str} turns · {tool_calls_str} tool calls · "
            f"{duration_s:.0f}s"
        )
        lines.append(
            f"tokens in/out/cache: {tokens_in} / {tokens_out} / {tokens_cache} · "
            f"est. cost ${spent:.4f}"
        )

        # Comparison line
        saved_str = _comparison_line(cfg, run, parent_model)
        if saved_str:
            lines.append(saved_str)

        # Toolsets inheritance note
        if toolsets_inherited:
            lines.append("toolsets: inherited from parent (narrowing rejected) · skills: ON")

        # Budget line
        if budget and budget > 0:
            actual_spent = outcome.get("budget_spent", spent)
            budget_limit = outcome.get("budget_limit", budget)
            lines.append(f"budget ${budget_limit} · spent ${actual_spent:.4f}")

    # Footer
    lines.append(f"run id {run.correlation_id} · audit: plugin-data/lean-mode/runs.jsonl")

    if brief_was_used and not is_escalated:
        lines.append("excludes the main agent's brief-writing turn")

    return "\n".join(lines)


def _comparison_line(cfg: LeanConfig, run: RunState, parent_model: Optional[str]) -> str:
    """Compute vs-non-Lean comparison.

    Price keys are matched by exact model ID first, then by suffix
    (e.g. 'glm-5.3-flash' matches 'surplus/glm-5.3-flash').
    """
    if not parent_model:
        return ""

    cheap_model_key = run.response_model or run.model or ""
    cheap_price = _lookup_price(cfg.prices, cheap_model_key)
    parent_price = _lookup_price(cfg.prices, parent_model)
    if not cheap_price or not parent_price:
        return ""

    # Re-price same tokens at parent model rates
    parent_cost = (
        run.tokens_input * (parent_price.input / 1_000_000)
        + run.tokens_output * (parent_price.output / 1_000_000)
        + run.tokens_cache_read * (parent_price.cache_read / 1_000_000)
        + run.tokens_cache_write * (parent_price.cache_write / 1_000_000)
        + run.tokens_reasoning * (parent_price.output / 1_000_000)
    )

    cheap_cost = run.spent_usd(cfg.prices, cheap_model_key)
    if parent_cost <= 0:
        return ""

    saved = parent_cost - cheap_cost
    saved_pct = (saved / parent_cost) * 100 if parent_cost > 0 else 0.0

    return f"vs non-Lean (same work on {parent_model}): ${parent_cost:.4f} (estimate) — saved ~{saved_pct:.0f}%"


def _lookup_price(prices: Dict[str, PriceEntry], model_key: str) -> Optional[PriceEntry]:
    """Look up a price entry by exact key, then by suffix match."""
    if not model_key:
        return None
    # Exact match first
    if model_key in prices:
        return prices[model_key]
    # Suffix match: e.g. 'glm-5.3-flash' matches 'surplus/glm-5.3-flash'
    for key in prices:
        if key.endswith("/" + model_key) or model_key.endswith(key):
            return prices[key]
    return None


def format_stats_output(
    stats: Dict[str, Any],
) -> str:
    """Format /quick stats output (≤12 lines for gateway)."""
    lines = []
    lines.append(f"📊 Lean Stats — {stats.get('period', 'last 7 days')}")

    total = stats.get("total_runs", 0)
    succeeded = stats.get("succeeded", 0)
    escalated = stats.get("escalated", 0)
    cancelled_budget = stats.get("cancelled_budget", 0)
    cancelled_watchdog = stats.get("cancelled_watchdog", 0)

    lines.append(f"  {total} runs · {succeeded} ok · {escalated} escalated · "
                 f"{cancelled_budget} budget-stop · {cancelled_watchdog} timeout")

    total_spent = stats.get("total_spent", 0.0)
    total_saved = stats.get("total_saved", 0.0)
    label = "estimate" if stats.get("is_estimate", True) else "actual"
    lines.append(f"  spent: ${total_spent:.4f} ({label})")

    if total_saved >= 0:
        lines.append(f"  net saved: ${total_saved:.4f} ({label})")
    else:
        lines.append(f"  net LOSS: ${abs(total_saved):.4f} ({label}) — escalated runs affected savings")

    unknown = stats.get("unknown_cost_runs", 0)
    if unknown:
        lines.append(f"  {unknown} runs with unknown cost (not counted in totals)")

    top_preset = stats.get("top_preset")
    if top_preset:
        lines.append(f"  top preset: @{top_preset}")

    return "\n".join(lines)


def format_config_output(cfg: "LeanConfig") -> str:  # noqa: F821
    """Format /quick config output — show effective configuration."""
    lines = []
    lines.append("⚙️ Lean Config")
    lines.append(f"  model: {cfg.model or '(use delegation default)'}")
    lines.append(f"  parent_model: {cfg.parent_model or '(auto-resolved)'}")
    lines.append(f"  provider: {cfg.provider or '(use default)'}")
    lines.append(f"  toolsets: {cfg.toolsets}")
    lines.append(f"  escalate: {cfg.escalate}")
    lines.append(f"  escalate_on: {cfg.escalate_on}")
    lines.append(f"  timeout_seconds: {cfg.timeout_seconds}")
    lines.append(f"  gateway_max_timeout_seconds: {cfg.gateway_max_timeout_seconds}")
    lines.append(f"  poll_interval_seconds: {cfg.poll_interval_seconds}")
    lines.append(f"  budget_usd: {cfg.budget_usd or '(unset)'}")
    lines.append(f"  min_escalation_seconds: {cfg.min_escalation_seconds}")
    lines.append(f"  audit_retention_lines: {cfg.audit_retention_lines}")
    lines.append("")

    # Prices
    if cfg.prices:
        lines.append("  prices:")
        for model_id, price in cfg.prices.items():
            lines.append(f"    {model_id}: in={price.input}/M out={price.output}/M "
                         f"cr={price.cache_read}/M cw={price.cache_write}/M")
    else:
        lines.append("  prices: (none)")

    # Presets
    if cfg.presets:
        lines.append("  presets:")
        for name, preset in cfg.presets.items():
            parts = []
            if preset.toolsets:
                parts.append(f"toolsets={preset.toolsets}")
            if preset.model:
                parts.append(f"model={preset.model}")
            if preset.timeout_seconds:
                parts.append(f"timeout={preset.timeout_seconds}s")
            if preset.budget_usd:
                parts.append(f"budget=${preset.budget_usd}")
            if preset.context_hint:
                parts.append(f"hint=\"{preset.context_hint[:50]}{'...' if len(preset.context_hint) > 50 else ''}\"")
            lines.append(f"    @{name}: {', '.join(parts)}")
    else:
        lines.append("  presets: (none configured)")

    return "\n".join(lines)
