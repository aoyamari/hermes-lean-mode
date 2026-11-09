"""Cost receipt generation — estimate + state.db actual + comparison line."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .config import LeanConfig, PriceEntry
from .watchdog import RunState

logger = logging.getLogger("lean-mode.receipt")


def _format_cost(amount: float) -> str:
    """Format cost with 2 significant figures — keeps tiny values readable."""
    if amount <= 0:
        return "0.00"
    if amount >= 1:
        return f"{amount:.2f}"
    # For sub-dollar amounts: find first two non-zero digits
    import math
    if amount >= 0.01:
        return f"{amount:.4f}"
    # Very small: compute sig figs manually
    # Number of leading zeros after decimal point
    if amount == 0:
        return "0.00"
    leading_zeros = abs(math.floor(math.log10(abs(amount))))
    digits = leading_zeros + 2  # 2 sig figs
    return f"{amount:.{digits}f}"


def format_receipt(
    cfg: LeanConfig,
    run: RunState,
    outcome: Dict[str, Any],
    parent_model: Optional[str] = None,
    brief_was_used: bool = False,
    is_escalated: bool = False,
    escalation_cheap_cost: float = 0.0,
    escalation_trigger: Optional[str] = None,
    escalation_model: Optional[str] = None,
    toolsets_inherited: bool = False,
    timeout_clamped: bool = False,
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

    # Fallback: use run.tool_call_history when SubagentResult was empty
    if turns_str == "n/a" and tool_calls_str == "n/a" and hasattr(run, "tool_call_history"):
        if run.tool_call_history:
            tool_calls_str = str(len(run.tool_call_history))
        if run.turns_count is not None:
            turns_str = str(run.turns_count)

    if is_escalated:
        # Escalated receipt — use actual escalation model name
        cheap_model_name = cheap_model
        full_model_name = escalation_model or model_name
        cheap_cost = escalation_cheap_cost
        total_cost = outcome.get("spent", 0.0)  # escalation attempt cost
        grand_total = cheap_cost + total_cost

        lines.append(
            f"⚡ Lean receipt — ESCALATED ({escalation_trigger or 'unknown'}) "
            f"· cheap {cheap_model_name} ${_format_cost(cheap_cost)} (wasted) "
            f"+ full {full_model_name} ${_format_cost(total_cost)} = total ${_format_cost(grand_total)}"
        )
        lines.append(f"saved: −${_format_cost(cheap_cost)} vs running on {full_model_name} directly")
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
            f"est. cost ${_format_cost(spent)}"
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
            lines.append(f"budget ${budget_limit} · spent ${_format_cost(actual_spent)}")

    # Footer
    lines.append(f"run id {run.correlation_id} · audit: plugin-data/lean-mode/runs.jsonl")

    if brief_was_used and not is_escalated:
        lines.append("excludes the main agent's brief-writing turn")

    if timeout_clamped:
        lines.append(f"⚠ timeout clamped to {cfg.gateway_max_timeout_seconds}s (gateway limit)")

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
        # NOTE: reasoning_tokens are already in output_tokens — no double-count
    )

    cheap_cost = run.spent_usd(cfg.prices, cheap_model_key)
    if parent_cost <= 0:
        return ""

    saved = parent_cost - cheap_cost
    saved_pct = (saved / parent_cost) * 100 if parent_cost > 0 else 0.0

    return f"vs non-Lean (same work on {parent_model}): ${_format_cost(parent_cost)} (estimate) — saved ~{saved_pct:.0f}%"


def _lookup_price(prices: Dict[str, PriceEntry], model_key: str) -> Optional[PriceEntry]:
    """Look up a price entry by exact key, then by unique suffix match.

    Exact match first. Suffix match (e.g. 'glm-5.3-flash' matching
    'surplus/glm-5.3-flash') only if exactly ONE price entry matches.
    Returns None if 0 or >1 suffix matches (ambiguous).
    """
    if not model_key:
        return None
    # Exact match first
    if model_key in prices:
        return prices[model_key]
    # Suffix match: e.g. 'glm-5.3-flash' matches 'surplus/glm-5.3-flash'
    matches = []
    for key in prices:
        if key.endswith("/" + model_key) or model_key.endswith(key):
            matches.append(key)
    if len(matches) == 1:
        return prices[matches[0]]
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
    skipped = stats.get("skipped_count", 0)
    lines.append(f"  spent: ${_format_cost(total_spent)} ({label})")

    if total_saved >= 0:
        lines.append(f"  net saved: ${_format_cost(total_saved)} ({label})")
    else:
        lines.append(f"  net LOSS: ${_format_cost(abs(total_saved))} ({label}) — escalated runs affected savings")

    unknown = stats.get("unknown_cost_runs", 0)
    if unknown:
        lines.append(f"  {unknown} runs with unknown cost (not counted in totals)")

    if skipped:
        lines.append(f"  ⚠ {skipped} entries skipped (bad or missing timestamp)")

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

    # Warn if parent platform uses bundles (hermes- prefix)
    parent_ts = cfg.toolsets
    bundles = [t for t in parent_ts if t.startswith("hermes-")]
    if bundles:
        lines.append(
            f"  ⚠ parent uses bundle(s): {', '.join(bundles)} — "
            f"narrowing unavailable. Set platform_toolsets.<platform> "
            f"to granular toolsets (web, terminal, file, ...) instead."
        )

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
