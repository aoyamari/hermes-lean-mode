# SPIKE — Launch Path & API Verification (v2)

**Date:** 2026-11-09  
**Commit tested:** `110afdd60`  
**Plugin:** `lean-mode` (v0.1.0)

---

## 1. `launch()` from slash-command handler

**Verdict: 🚫 FAILS — cannot call `launch()` from slash-command handler.**

`SubagentLifecycleService(parent_agent_resolver)` at `plugins_context.py:197` binds `get_active_subagent_parent`, which reads `_ACTIVE_PARENT_AGENT` ContextVar. This var is set ONLY inside `turn_facade.py:144`.

Both slash-command paths run **outside** the agent turn:

| Path | File | bind_subagent_parent? |
|------|------|-----------------------|
| TUI Gateway `slash.exec` | `tui_gateway/methods_tools.py:1066` → `_run_plugin_command()` | ❌ None *(comment l.502: "nothing upstream binds it")* |
| CLI `_run_plugin_slash_command` | `cli.py:1284` → `invoke_plugin_command()` | ❌ None |

**→ Default path: `inject_message()`. `launch()` only from `quick_run` tool (called by model during agent turn).**

---

## 2. `inject_message()` availability

**Verdict: ✅ Available**

`PluginContext.inject_message(content, role="user", *, session_key, origin) -> bool`
- `plugins_context.py:395`
- Returns `True` when host accepted the message

**⚠️ Unverified (needs live test):** Whether the parent model reliably calls `quick_run` with a filled `context` after receiving the injected message.

---

## 3. Registered toolset names

**Verdict: 55 registered toolsets**

`bot_room, browser, catalog, clarify, code_execution, coding, computer_use, connections, context_engine, cronjob, debugging, delegation, desktop_ui, discord, discord_admin, feishu_doc, feishu_drive, file, hermes-acp, hermes-api-server, hermes-bluebubbles, hermes-cli, hermes-cron, hermes-dingtalk, hermes-discord, hermes-email, hermes-feishu, hermes-gateway, hermes-matrix, hermes-mattermost, hermes-qqbot, hermes-signal, hermes-slack, hermes-sms, hermes-telegram, hermes-webhook, hermes-wecom, hermes-wecom-callback, hermes-weixin, hermes-whatsapp, image_gen, kanban, memory, onboarding, project, safe, search, session_search, skills, terminal, todo, tts, video, video_gen, vision, web, x_search`

Default allow-list for Lean: `[terminal, web, file]` — validated at startup via `toolsets.validate_toolset()`.

---

## 4. Exact API shapes

### `SubagentLaunchRequest` (`subagent_lifecycle.py:49-61`)

```python
@dataclasses.dataclass(frozen=True)
class SubagentLaunchRequest:
    goal: str                                          # required
    context: Optional[str] = None
    role: str = "leaf"
    model: Optional[str] = None
    provider: Optional[str] = None
    allowed_toolsets: Optional[tuple[str, ...]] = None # MUST be tuple, NOT list
    blocked_tools: tuple[str, ...] = ()                # ❌ REJECTED if non-empty
    working_directory: Optional[str] = None             # ❌ REJECTED
    parent_session_id: Optional[str] = None
    correlation_id: Optional[str] = None
    metadata: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    timeout_seconds: Optional[float] = None             # ❌ REJECTED
```

**Rejected fields** (`_REQUEST_REJECTIONS` at l.220-233):
- `timeout_seconds` → "Per-launch timeout is not supported"
- `working_directory` → "Not supported"  
- `blocked_tools` (non-empty) → "Use allowed_toolsets"

### `SubagentHandle` (l.65-90)

`contract_version, subagent_id, parent_session_id, correlation_id, provider, model, role, depth, capability`

### `SubagentTerminalState` from `wait()` (l.97-104)

```python
handle, state (SubagentState), completed (bool), timed_out (bool, default False), diagnostic
```

**`wait()`** returns `SubagentTerminalState` — attributes, not dict keys.

### `SubagentResult` from `result()` (l.116-134)

```python
handle, terminal_state (SubagentState), ready, summary, structured_payload,
started_at, completed_at, error_classification, error_message,
usage_metadata (dict), tool_execution_summary (dict), result_hash
```

**`result()`** returns `SubagentResult` — attributes, not dict keys. `usage_metadata` contains `api_calls` only (no tokens).

### `SubagentCancelResult` from `cancel()` (l.106-114)

```python
accepted, already_terminal, unknown_handle, unsupported, state
```

**`cancel()` signature:** `cancel(self, handle, *, reason: str)` — `reason` is KEYWORD-ONLY.

### `SubagentState` values

`PENDING, STARTING, RUNNING, SUCCEEDED, FAILED, INTERRUPTED, CANCEL_REQUESTED, CANCELLED, UNKNOWN`

Terminal states: `SUCCEEDED, FAILED, INTERRUPTED, CANCELLED`

---

## 5. `post_api_request` — fires per API call during child execution

**Verdict: ✅ Fires per API request while child is running.**

Payload (`hooks.py:139-151`):
```python
session_id, model, provider, base_url, api_mode, api_call_count, api_duration,
started_at, ended_at, first_chunk_at, finish_reason, message_count, response_model,
usage: {input_tokens, output_tokens}, context_length, assistant_content_chars,
assistant_tool_call_count, moa_references
```

**⚠️ No `tokens_cache_read`/`tokens_cache_write` in standard usage object.** These are provider-specific. Receipt must handle missing fields gracefully.

---

## 6. Tool registration API — CRITICAL differences

### `register_tool()` requires `toolset` parameter

```python
def register_tool(self, name: str, toolset: str, schema: dict, handler: Callable, ...):
```

Our code omits `toolset`. **Fix needed:** pass `toolset="lean"`.

### Handler signature

Called as: `handler(args_dict, **context_kwargs)` (`tools/registry.py:986-988`)

Where:
- `args_dict` = tool call arguments (e.g. `{"task": "...", "context": "..."}`)
- `context_kwargs` = optional fields like `session_id`, `task_id`

Our handler uses `**kw` which doesn't accept the positional `args_dict`. **Fix:** `handler=lambda args, **kw: ...`.

### `_kwargs_accepted_by` (l.1112)

Filters `context_kwargs` to only what handler's signature declares. If handler takes `**kw`, all pass through.

---

## 7. Code changes required vs current implementation

| # | Issue | File(s) | Fix |
|---|-------|---------|-----|
| 1 | `register_tool` missing `toolset` param | `__init__.py` | Add `toolset="lean"` as second positional arg |
| 2 | Handler uses `**kw` instead of `(args, **kw)` | `__init__.py` | `lambda args, **kw: _handle_quick_run(ctx, cfg, audit, **args)` |
| 3 | `cancel()` call missing keyword-only `reason` | `__init__.py`, `watchdog.py` | `cancel(handle, reason="...")` |
| 4 | `SubagentLaunchRequest` includes rejected fields | `launch.py` | Remove `timeout_seconds`, `blocked_tools`, `working_directory`; tuple not list for `allowed_toolsets` |
| 5 | `wait()` result accessed as dict | `watchdog.py` | Use `.timed_out`, `.completed`, `.state.name` |
| 6 | `result()` accessed as dict | `watchdog.py` | Use `.summary`, `.terminal_state.name`, `.usage_metadata` |
| 7 | `.state` vs `.terminal_state` | `watchdog.py` | `SubagentResult.terminal_state` = state; `SubagentTerminalState.state` = SubagentState |
| 8 | Heavy imports at module level > 10s load | `__init__.py` | Move `yaml`, `Path`, imports inside handlers |
| 9 | `--raw` from slash command impossible | `command.py`, `__init__.py` | `--raw` only works from `quick_run` tool; document |
