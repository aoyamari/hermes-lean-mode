"""Lean Mode — cheap-model subagent with budget cap, auto-escalation, and cost receipt.

Plugin for Hermes Agent. Registers:
- `/quick` command (inject path — see SPIKE.md)
- `/quick stats`: cumulative savings report
- `quick_run` tool: engine for the cheap subagent
- `post_api_request` hook: track token usage for receipt
- `subagent_start` / `subagent_stop` hooks: map child sessions
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("lean-mode")

# In-process state: correlation_id -> RunState, child_session_id -> correlation_id
_ACTIVE_RUNS: Dict[str, Any] = {}
_CHILD_MAP: Dict[str, str] = {}
# Pending session map: child_subagent_id -> child_session_id, set by subagent_start hook
# before launch() returns the handle. Popped after launch() returns.
_PENDING: Dict[str, str] = {}
_PENDING_CAP = 1024


def register(ctx):
    """Register hooks, commands, and tools for Lean Mode. Must be fast (<1s)."""
    cfg = _load_config(ctx)

    # ── Audit log (lazy — no plugin_data_dir call during register()) ──
    audit = _LazyAudit(ctx, cfg.audit_retention_lines)

    # ── Validate toolsets against parent (best-effort) ──────────────────
    _warn_unknown_toolsets(cfg)

    # ── Hooks ─────────────────────────────────────────────────────────
    ctx.register_hook(
        hook_name="post_api_request",
        callback=lambda **kw: _on_post_api_request(ctx, cfg, **kw),
    )
    ctx.register_hook(
        hook_name="subagent_start",
        callback=lambda **kw: _on_subagent_start(ctx, cfg, **kw),
    )
    ctx.register_hook(
        hook_name="subagent_stop",
        callback=lambda **kw: _on_subagent_stop(ctx, cfg, **kw),
    )

    # ── Tool ───────────────────────────────────────────────────────────
    ctx.register_tool(
        name="quick_run",
        toolset="lean",
        schema={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task for the cheap worker"},
                "context": {
                    "type": "string",
                    "description": "Brief from the main agent. Put everything the worker needs here — "
                                   "the worker cannot see this conversation. Keep under ~1,500 words.",
                },
                "preset": {"type": "string", "description": "Optional @preset name"},
                "model": {"type": "string", "description": "Override cheap model ID"},
                "toolsets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Override toolsets",
                },
                "timeout_seconds": {"type": "integer", "description": "Override timeout"},
                "budget_usd": {"type": "number", "description": "Override budget cap"},
                "escalate": {"type": "boolean", "description": "Enable auto-escalation (default true)"},
            },
            "required": ["task"],
        },
        # Tool handler receives (args_dict, **context_kwargs)
        handler=lambda args, **kw: _handle_quick_run(ctx, cfg, audit, **args),
    )

    # ── Slash command ─────────────────────────────────────────────────
    ctx.register_command(
        name="quick",
        handler=lambda raw: _handle_quick_command(ctx, cfg, audit, raw),
        description="Cheap-model task with budget cap, auto-escalation and cost receipt",
        args_hint="[@preset] [--budget N] [--no-escalate] [--full] <task> | stats [--days N] | config",
    )

    logger.info("Lean Mode v0.2.0 registered")


# ── Lazy audit ─────────────────────────────────────────────────────

class _LazyAudit:
    """Audit log that initialises only on first use.

    Forwards ALL attribute access to an underlying AuditLog instance,
    created lazily. This way any method on AuditLog (append, read_all,
    read_recent, ...) works transparently without manual proxying.
    """

    def __init__(self, ctx, retention_lines: int):
        self._ctx = ctx
        self._retention_lines = retention_lines
        self._audit = None

    def _ensure(self):
        if self._audit is None:
            from .lean.audit import AuditLog
            from plugins.plugin_storage import plugin_data_dir
            data_dir = plugin_data_dir("lean-mode")
            self._audit = AuditLog(data_dir, retention_lines=self._retention_lines)
        return self._audit

    def __getattr__(self, name):
        """Forward all attribute access to the underlying AuditLog."""
        # Avoid infinite recursion for internal attributes
        if name.startswith('_') or name in ('_ensure',):
            raise AttributeError(name)
        return getattr(self._ensure(), name)


# ── Config loading ──────────────────────────────────────────────────

def _load_config(ctx) -> Any:
    """Load lean config from Hermes config using the official API.

    Uses load_config_readonly() from hermes_cli.config when available (inside
    the gateway process). Falls back to reading config.yaml via get_hermes_home()
    when the import is not available (e.g., in plugin tests). This respects
    HERMES_HOME, profile switching, and config caching.
    """
    from .lean.config import LeanConfig
    try:
        from hermes_cli.config import load_config_readonly
        full_config = load_config_readonly() or {}
        lean_raw = full_config.get("lean", {})
        return LeanConfig.from_config(lean_raw)
    except ImportError:
        # Outside gateway process (plugin tests, validation) — read file directly
        pass
    except Exception as exc:
        logger.debug("Config load fallback: %s", exc)

    try:
        from hermes_constants import get_hermes_home
        import yaml
        from pathlib import Path
        config_path = get_hermes_home() / "config.yaml"
        if config_path.exists():
            raw = yaml.safe_load(config_path.read_text())
            lean_raw = (raw or {}).get("lean", {})
            return LeanConfig.from_config(lean_raw)
    except Exception as exc:
        logger.debug("Config file fallback: %s", exc)

    return LeanConfig()


def _warn_unknown_toolsets(cfg) -> None:
    """Warn about unknown toolsets in config (best-effort, import-heavy)."""
    try:
        from .lean.launch import validate_toolsets_against_parent
        from toolsets import get_toolset_names
        parent_toolsets = get_toolset_names()
        if parent_toolsets:
            err = validate_toolsets_against_parent(cfg.toolsets, parent_toolsets)
            if err:
                logger.warning(err)
    except Exception:
        pass


# ── Hook handlers ───────────────────────────────────────────────────

def _on_post_api_request(ctx, cfg: Any, **kw):
    """Track token usage per API call for active lean runs.

    Looks up session_id in _CHILD_MAP (already bound) first, then
    _PENDING (launch hasn't returned handle yet) as fallback.
    """
    session_id = kw.get("session_id", "")
    usage = kw.get("usage", {})
    if not usage or not session_id:
        return None

    # Already-bound runs (launch() returned handle)
    correlation_id = _CHILD_MAP.get(session_id)

    # Pending runs (subagent_start fired, launch() hasn't returned yet)
    if not correlation_id:
        for subagent_id, sid in list(_PENDING.items()):
            if sid == session_id:
                # Map now so future post_api_request calls hit _CHILD_MAP
                for corr_id, run in _ACTIVE_RUNS.items():
                    if run.child_session_id == session_id:
                        correlation_id = corr_id
                        _CHILD_MAP[session_id] = corr_id
                        break
                break

    if not correlation_id:
        return None

    run = _ACTIVE_RUNS.get(correlation_id)
    if not run:
        return None

    run.add_api_call(
        usage=usage,
        model=kw.get("model", ""),
        response_model=kw.get("response_model", ""),
    )
    run.provider = kw.get("provider", run.provider or "")
    run.duration_seconds = kw.get("api_duration", 0)
    return None


def _on_subagent_start(ctx, cfg: Any, **kw):
    """Store child_subagent_id -> child_session_id mapping.

    Fires synchronously inside launch() via:
      launch() → _build_child_preserving_parent_tools() → invoke_hook("subagent_start")

    Stores ALL subagent starts. Lean runs pop their entry after
    launch() returns via handle.subagent_id. Non-lean entries are
    evicted naturally via _PENDING_CAP (1024).
    """
    child_session_id = kw.get("child_session_id")
    child_subagent_id = kw.get("child_subagent_id")

    if not child_session_id or not child_subagent_id:
        return None

    if len(_PENDING) >= _PENDING_CAP:
        _PENDING.pop(next(iter(_PENDING)), None)

    _PENDING[child_subagent_id] = child_session_id
    return None


def _on_subagent_stop(ctx, cfg: Any, **kw):
    """Capture tool_call_history + clean up mapping when subagent stops.

    Stores tool_call_history and derived counts on the active RunState
    so the receipt can render turns/tool_calls even when SubagentResult's
    tool_execution_summary is absent.
    """
    child_session_id = kw.get("child_session_id")
    if child_session_id and child_session_id in _CHILD_MAP:
        correlation_id = _CHILD_MAP.get(child_session_id)
        if correlation_id:
            run = _ACTIVE_RUNS.get(correlation_id)
            if run:
                tch = kw.get("tool_call_history")
                if isinstance(tch, list):
                    run.tool_call_history = list(tch)
                # Derive turns: estimated from number of unique tool-call bursts.
                # Each distinct tool_call_index prefix signals a new assistant turn.
                # Fallback: distinct tool_name groups → turns estimate.
                turns_raw = kw.get("turns_count")
                if turns_raw is not None:
                    run.turns_count = int(turns_raw)
        _CHILD_MAP.pop(child_session_id, None)
    return None


# ── quick_run tool handler ──────────────────────────────────────────

def _handle_quick_run(ctx, cfg: Any, audit: Any, **kw) -> str:
    """Handle the quick_run tool call from the model."""
    task = kw.get("task", "")
    brief = kw.get("context", "")
    preset_name = kw.get("preset")
    model_override = kw.get("model")
    toolsets_override = kw.get("toolsets")
    timeout_override = kw.get("timeout_seconds")
    budget_override = kw.get("budget_usd")
    escalate = kw.get("escalate", True) if kw.get("escalate") is not None else True

    if not task:
        return "Error: task is required"

    return _run_lean(
        ctx, cfg, audit, task,
        brief=brief,
        preset=preset_name,
        model_override=model_override,
        toolsets_override=toolsets_override,
        timeout_override=timeout_override,
        budget_override=budget_override,
        escalate_override=escalate,
        inject_path=False,  # tool call uses direct launch
    )


# ── /quick command handler ──────────────────────────────────────────

def _handle_quick_command(ctx, cfg: Any, audit: Any, raw_args: str) -> str:
    """Handle the /quick slash command. Default path: inject_message."""
    from .lean.command import parse_quick
    from .lean.presets import preset_names
    from .lean.stats import compute_stats
    from .lean.receipt import format_stats_output

    cmd = parse_quick(raw_args)

    if cmd.subcommand == "stats":
        stats = compute_stats(audit, days=cmd.days or 7)
        return format_stats_output(stats)

    if cmd.subcommand == "config":
        from .lean.receipt import format_config_output
        return format_config_output(cfg)

    if not cmd.task:
        names = preset_names(cfg)
        presets_hint = "@" + ", @".join(names) if names else "(none configured)"
        return ("Usage: /quick [@preset] [--budget N] [--no-escalate] [--full] <task>\n"
                "       /quick stats [--days N]\n"
                "       /quick config\n"
                f"Flags: --model M, --tools a,b,c, --timeout S, --budget N, --no-escalate, --full\n"
                f"Presets: {presets_hint}")

    return _run_lean(
        ctx, cfg, audit, cmd.task,
        preset=cmd.preset,
        model_override=cmd.model,
        toolsets_override=cmd.toolsets,
        timeout_override=cmd.timeout_seconds,
        budget_override=cmd.budget_usd,
        no_escalate=cmd.no_escalate,
        full=cmd.full,
        inject_path=True,  # slash command -> inject_message (SPIKE finding: launch fails from slash)
    )


# ── Core execution ──────────────────────────────────────────────────

def _run_lean(
    ctx, cfg, audit, task: str,
    brief: str = "",
    preset: Optional[str] = None,
    model_override: Optional[str] = None,
    toolsets_override: Optional[List[str]] = None,
    timeout_override: Optional[int] = None,
    budget_override: Optional[float] = None,
    no_escalate: bool = False,
    full: bool = False,
    escalate_override: Optional[bool] = None,
    inject_path: bool = True,
) -> str:
    """Core lean execution: inject path or direct launch.

    inject_path=True (from slash command): injects a message asking the parent
    to call quick_run. This is the default because launch() FAILS from slash
    command handlers (see SPIKE.md).

    inject_path=False (from quick_run tool): launches subagent directly.
    """

    # ── Inject path (default for slash commands — launch() is unavailable) ─
    if inject_path:
        return _run_inject_path(ctx, cfg, audit, task, preset, model_override,
                                 toolsets_override, timeout_override, budget_override,
                                 no_escalate, full, escalate_override)

    # ── Direct launch path (for quick_run tool call) ────────────────────
    return _run_direct_launch(ctx, cfg, audit, task, brief, preset,
                               model_override, toolsets_override,
                               timeout_override, budget_override,
                               no_escalate, full, escalate_override)


def _run_inject_path(ctx, cfg, audit, task, preset, model_override,
                      toolsets_override, timeout_override, budget_override,
                      no_escalate, full, escalate_override) -> str:
    """Run via inject_message. Import-heavy modules loaded lazily."""
    from .lean.launch import _do_run_inject_path as rip
    return rip(ctx, cfg, audit, task, preset, model_override,
               toolsets_override, timeout_override, budget_override,
               no_escalate, full, escalate_override)


def _run_direct_launch(ctx, cfg, audit, task, brief, preset,
                        model_override, toolsets_override,
                        timeout_override, budget_override,
                        no_escalate, full, escalate_override) -> str:
    """Execute directly by launching a subagent. Full import path."""
    # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    from .lean.launch import (
        build_child_context, build_launch_request,
        _exclude_excluded,
    )
    from .lean.presets import resolve_preset
    from .lean.watchdog import RunState, Watchdog
    from .lean.receipt import format_receipt
    from .lean.audit import build_audit_entry
    from .lean.escalate import run_escalation
    from toolsets import get_toolset_names, validate_toolset

    # ── Resolve preset ────────────��───────────────────────────────────
    preset_context_hint = ""
    if preset:
        try:
            hint, ptools, pmodel, ptimeout, pbudget = resolve_preset(cfg, preset,
                cmd_toolsets=toolsets_override,
                cmd_model=model_override,
                cmd_timeout=timeout_override,
                cmd_budget=budget_override,
            )
            preset_context_hint = hint
            if pmodel and not model_override:
                model_override = pmodel
            if ptools and not toolsets_override:
                toolsets_override = list(ptools)
            if ptimeout and not timeout_override:
                timeout_override = ptimeout
            if pbudget and not budget_override:
                budget_override = pbudget
        except KeyError as e:
            return str(e)

    # ── Resolve config ───────────────────────────────────────────────
    effective_timeout = timeout_override or cfg.timeout_seconds
    effective_budget = budget_override if budget_override is not None else cfg.budget_usd
    effective_escalate = no_escalate if no_escalate else (escalate_override if escalate_override is not None else cfg.escalate)

    # ── Resolve model and tools ──────────────────────────────────────
    if full:
        effective_model = None
        effective_tools = None
    else:
        effective_model = model_override or cfg.model or None
        effective_tools = toolsets_override or cfg.toolsets
        effective_tools = _exclude_excluded(effective_tools)

    # ── Validate budget needs price entry ────────────────────────────
    cheap_key = effective_model or "default"
    if effective_budget and effective_budget > 0 and cheap_key not in cfg.prices:
        return (f"Error: --budget ${effective_budget} requires lean.prices.{cheap_key} in config. "
                f"Add the price entry or run without --budget.")

    # ── Build child context ──────────────────────────────────────────
    brief_text = brief
    if preset_context_hint:
        if brief_text:
            brief_text += "\n\n" + preset_context_hint
        else:
            brief_text = preset_context_hint

    child_context = build_child_context(cfg, brief_text)

    # ── Validate toolsets ────────────────────────────────────────────
    if effective_tools:
        unknown = [t for t in effective_tools if not validate_toolset(t)]
        if unknown:
            return f"Error: Unknown toolsets: {', '.join(unknown)}"

    # ── Get parent session ───────────────────────────────────────────
    parent_session_id = None
    try:
        parent_session_id = ctx.session_id()
    except Exception:
        pass

    # ── Build launch request ─────────────────────────────────────────
    launch_req = build_launch_request(
        cfg, task, child_context, parent_session_id,
        model=effective_model,
        toolsets=effective_tools,
    )

    # ── Create run state ──────────────────────────────────────────────
    now = time.monotonic()
    correlation_id = launch_req.get("correlation_id", f"lean-{int(now)}")
    run = RunState(correlation_id=correlation_id, started_at=now,
                   budget_usd=effective_budget if effective_budget and effective_budget > 0 else None)
    _ACTIVE_RUNS[correlation_id] = run

    # ── Launch subagent ───────────────────────────────────────────────
    toolsets_inherited = False
    try:
        handle = ctx.subagent_lifecycle.launch(launch_req)
    except Exception as exc:
        error_msg = str(exc)
        # If toolsets aren't available in parent, retry with None (inherit parent)
        if "broaden parent permissions" in error_msg.lower():
            logger.info(
                "lean run %s: toolsets %s not in parent, retrying with inherit",
                correlation_id, launch_req.get("allowed_toolsets"),
            )
            launch_req["allowed_toolsets"] = None
            toolsets_inherited = True
            try:
                handle = ctx.subagent_lifecycle.launch(launch_req)
            except Exception as exc2:
                _ACTIVE_RUNS.pop(correlation_id, None)
                return (f"[Lean Error] quick_run gagal: {exc2}\n"
                        f"Context: parent tidak punya toolsets {cfg.toolsets}.\n"
                        f"Coba: /quick --full")
        else:
            _ACTIVE_RUNS.pop(correlation_id, None)
            if effective_escalate and "launch_failed" in cfg.escalate_on:
                result = _try_escalate(ctx, cfg, run, audit, task, brief_text,
                                       parent_session_id, effective_timeout, effective_budget,
                                       None, None, {"status": "launch_failed", "spent": 0.0, "error": error_msg})
                if result:
                    return result
            return f"[Lean Error] quick_run gagal: {error_msg}. Coba /quick --full."

    # ── Map child session via _PENDING ─────────────────────────────────
    # subagent_start hook fired inside launch() and stored
    # child_subagent_id -> child_session_id in _PENDING.
    # Pop it now using handle.subagent_id (= child._subagent_id).
    child_session_id = _PENDING.pop(handle.subagent_id, None)
    if child_session_id:
        run.child_session_id = child_session_id
        _CHILD_MAP[child_session_id] = correlation_id
        logger.debug("mapped run %s -> session %s", correlation_id, child_session_id)
    else:
        logger.warning(
            "lean run %s: subagent_start hook did not fire for subagent %s. "
            "Token tracking unavailable — tokens=unknown.",
            correlation_id, handle.subagent_id,
        )

    # ── Watchdog ──────────────────────────────────────────────────────
    started_at = now
    timeout_clamped = False
    try:
        surface = ctx.session_surface()
        if surface in ("telegram", "discord", "whatsapp", "gateway"):
            if effective_timeout > cfg.gateway_max_timeout_seconds:
                effective_timeout = cfg.gateway_max_timeout_seconds
                timeout_clamped = True
    except Exception:
        pass
    watchdog = Watchdog(
        cfg=cfg,
        run=run,
        cancel_fn=lambda **canc_kw: ctx.subagent_lifecycle.cancel(handle, **canc_kw),
        wait_fn=lambda **wait_kw: ctx.subagent_lifecycle.wait(handle, **wait_kw),
        result_fn=lambda: ctx.subagent_lifecycle.result(handle),
        timeout_seconds=effective_timeout,
    )
    outcome = watchdog.monitor()
    outcome["duration"] = time.monotonic() - started_at
    outcome["ended_at"] = time.time()

    status = outcome.get("status", "empty_result")

    # ── Escalation check ──────────────────────────────────────────────
    is_escalated = False
    escalation_run_state: Optional[RunState] = None
    if effective_escalate and not full and not no_escalate:
        if status in ("cancelled_by_budget", "cancelled_by_watchdog"):
            pass  # Never escalate on cap/timeout
        elif status in cfg.escalate_on:
            esc_result = run_escalation(
                cfg, ctx, outcome, run, parent_session_id, task, brief_text,
                started_at + effective_timeout, effective_budget,
                parent_toolsets=get_toolset_names(),
                parent_model=cfg.parent_model,
                parent_provider=cfg.provider,
                pending_map=_PENDING,
                child_map=_CHILD_MAP,
                active_runs=_ACTIVE_RUNS,
            )
            if esc_result:
                escalation_run_state = esc_result.get("escalation_run_state")
                outcome = esc_result
                status = outcome.get("status", "failed")
                is_escalated = True

    # ── Build receipt ─────────────────────────────────────────────────
    escalation_model = outcome.get("escalation_model", "")
    receipt = format_receipt(
        cfg, run, outcome,
        parent_model=cfg.parent_model,
        brief_was_used=bool(brief_text),
        is_escalated=outcome.get("is_escalation", False),
        escalation_cheap_cost=outcome.get("escalation_cheap_cost", 0.0),
        escalation_trigger=outcome.get("escalation_trigger"),
        escalation_model=escalation_model,
        toolsets_inherited=toolsets_inherited,
        timeout_clamped=timeout_clamped,
    )

    # ── Result text ───────────────────────────────────────────────────
    result_text = outcome.get("result_text", "")
    if outcome.get("error"):
        if result_text:
            result_text += f"\n---\nError: {outcome['error']}"
        else:
            result_text = f"Error: {outcome['error']}"

    full_output = result_text
    if full_output:
        full_output += "\n\n---\n" + receipt
    else:
        full_output = receipt

    # ── Audit ─────────────────────────────────────────────────────────
    try:
        cheap_outcome = None
        if is_escalated:
            cheap_outcome = {"status": outcome.get("escalation_trigger", ""), "spent": outcome.get("escalation_cheap_cost", 0)}

        entry = build_audit_entry(
            correlation_id=correlation_id,
            status=status,
            outcome=outcome,
            run_state=run,
            cheap_outcome=cheap_outcome,
            parent_model=None,
            budget_usd=effective_budget,
            preset=preset,
            result_hash=outcome.get("result_hash"),
            toolsets_inherited=toolsets_inherited,
        )
        audit.append(entry)
    except Exception as exc:
        logger.warning("Audit log error: %s", exc)

    # ── Cleanup ───────────────────────────────────────────────────────
    _ACTIVE_RUNS.pop(correlation_id, None)

    return full_output


def _try_escalate(ctx, cfg, run, audit, task, brief, parent_session_id,
                   timeout, budget, parent_tools, parent_model, cheap_outcome) -> Optional[str]:
    """Try escalation after a launch failure (before a subagent was created)."""
    from .lean.escalate import run_escalation
    from .lean.receipt import format_receipt
    from toolsets import get_toolset_names

    deadline = time.monotonic() + timeout
    esc_result = run_escalation(
        cfg, ctx, cheap_outcome, run, parent_session_id, task, brief,
        deadline, budget, get_toolset_names(), parent_model, cfg.provider,
    )
    if esc_result:
        receipt = format_receipt(
            cfg, run, esc_result,
            is_escalated=True,
            escalation_cheap_cost=0.0,
            escalation_trigger="launch_failed",
        )
        result_text = esc_result.get("result_text", "")
        if result_text:
            result_text += "\n\n---\n" + receipt
        else:
            result_text = receipt
        return result_text
    return None
