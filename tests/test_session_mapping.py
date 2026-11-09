"""Test the subagent session mapping order: hook fires inside launch()

Real call chain:
  launch() → _build_child_preserving_parent_tools() → invoke_hook("subagent_start")
  → our _on_subagent_start stores _PENDING[child_subagent_id] = child_session_id
  → returns from hook → returns from build → creates handle → returns handle
  → our code pops _PENDING[handle.subagent_id] → sets run.child_session_id + _CHILD_MAP
"""

import importlib.util
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Load __init__.py as a module (it's the plugin's main module)
_plugin_root = Path(__file__).resolve().parent.parent
_init_path = _plugin_root / "__init__.py"
spec = importlib.util.spec_from_file_location("lean_mode_main", _init_path)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

# Also load RunState from watchdog for creating real instances
sys.path.insert(0, str(_plugin_root))
from lean.watchdog import RunState


class FakeHandle:
    """Mimics SubagentHandle returned by ctx.subagent_lifecycle.launch()."""
    def __init__(self, subagent_id: str):
        self.subagent_id = subagent_id


class MockLifecycle:
    """Mimics ctx.subagent_lifecycle: launch() calls subagent_start hook
    synchronously BEFORE returning the handle.

    Uses a counter so each mock launch produces a unique session ID
    even with identical goals.
    """
    _counter = 0

    def __init__(self, subagent_start_hook):
        self._hook = subagent_start_hook

    def launch(self, request):
        type(self)._counter += 1
        n = type(self)._counter
        # Unique session_id per call
        child_session_id = f"sess-{request['goal'][:8]}-{n}"
        child_subagent_id = f"sub-{request['correlation_id']}"
        # Step 1: Fire subagent_start hook synchronously (as real Hermes does)
        self._hook(
            child_session_id=child_session_id,
            child_subagent_id=child_subagent_id,
        )
        # Step 2: Return handle
        return FakeHandle(subagent_id=f"sub-{request['correlation_id']}")


def _reset():
    """Clear module-level state before each test."""
    plugin._PENDING.clear()
    plugin._CHILD_MAP.clear()
    plugin._ACTIVE_RUNS.clear()
    MockLifecycle._counter = 0


def _make_run(correlation_id: str):
    """Create a RunState with default values."""
    run = RunState(correlation_id=correlation_id, started_at=time.time(), budget_usd=0.01)
    return run


# ── Tests ─────────────────────────────────────────────────────────────

def test_hook_fires_before_handle_returns():
    """subagent_start hook stores _PENDING before launch() returns handle."""
    _reset()

    cfg = MagicMock()
    ctx = MagicMock()

    launch_req = {
        "goal": "summarize today",
        "context": "Brief text here",
        "correlation_id": "lean-test-abc123",
        "role": "leaf",
        "allowed_toolsets": ("terminal", "read"),
        "parent_session_id": "parent-1",
    }

    lifecycle = MockLifecycle(
        subagent_start_hook=lambda **kw: plugin._on_subagent_start(ctx, cfg, **kw)
    )
    ctx.subagent_lifecycle = lifecycle

    # Create run state BEFORE launch (as _run_direct_launch does)
    run = _make_run("lean-test-abc123")
    plugin._ACTIVE_RUNS["lean-test-abc123"] = run

    # Launch — hook fires inside, before handle is returned
    handle = lifecycle.launch(launch_req)

    # Verify: _PENDING was populated by the hook
    assert "sub-lean-test-abc123" in plugin._PENDING, (
        "Hook should have stored child_subagent_id in _PENDING"
    )
    expected_sid = "sess-summariz-1"
    assert plugin._PENDING["sub-lean-test-abc123"] == expected_sid, (
        "_PENDING value should be child_session_id"
    )

    # Now mimic what _run_direct_launch does after launch():
    child_session_id = plugin._PENDING.pop(handle.subagent_id, None)
    assert child_session_id == expected_sid, "Should pop correct session_id"

    run.child_session_id = child_session_id
    plugin._CHILD_MAP[child_session_id] = run.correlation_id

    # Verify: _CHILD_MAP now links session_id -> correlation_id
    assert plugin._CHILD_MAP.get(expected_sid) == "lean-test-abc123"
    assert run.child_session_id == expected_sid

    # Simulate a post_api_request call
    plugin._on_post_api_request(ctx, cfg,
        session_id=expected_sid,
        usage={"input_tokens": 100, "output_tokens": 50},
        model="claude-haiku-4-5",
        response_model="claude-haiku-4-5",
    )
    assert run.api_calls == 1, "post_api_request should have been tracked"
    assert run.tokens_input == 100

    # Verify per-call detail was appended
    assert len(run.api_calls_detail) == 1
    assert run.api_calls_detail[0]["input_tokens"] == 100
    assert run.api_calls_detail[0]["model"] == "claude-haiku-4-5"

    # Simulate subagent_stop with tool_call_history
    plugin._on_subagent_stop(ctx, cfg,
        child_session_id=expected_sid,
        tool_call_history=[
            {"tool_name": "web_search", "tool_input": {}, "status": "ok"},
            {"tool_name": "write_file", "tool_input": {}, "status": "ok"},
        ],
        turns_count=2,
    )
    assert len(run.tool_call_history) == 2
    assert run.turns_count == 2

    _reset()


def test_two_parallel_runs_same_goal():
    """Two runs with identical goal string get distinct session mappings."""
    _reset()

    cfg = MagicMock()
    ctx = MagicMock()

    req1 = {
        "goal": "research AI agents",
        "context": "",
        "correlation_id": "lean-run-aaa",
        "role": "leaf",
        "allowed_toolsets": ("terminal", "web_search"),
        "parent_session_id": "parent-1",
    }
    req2 = {
        "goal": "research AI agents",
        "context": "",
        "correlation_id": "lean-run-bbb",
        "role": "leaf",
        "allowed_toolsets": ("terminal", "web_search"),
        "parent_session_id": "parent-1",
    }

    lifecycle = MockLifecycle(
        subagent_start_hook=lambda **kw: plugin._on_subagent_start(ctx, cfg, **kw)
    )
    ctx.subagent_lifecycle = lifecycle

    # Run 1
    run1 = _make_run("lean-run-aaa")
    plugin._ACTIVE_RUNS["lean-run-aaa"] = run1
    handle1 = lifecycle.launch(req1)
    sid1 = plugin._PENDING.pop(handle1.subagent_id, None)
    run1.child_session_id = sid1
    plugin._CHILD_MAP[sid1] = run1.correlation_id

    # Run 2
    run2 = _make_run("lean-run-bbb")
    plugin._ACTIVE_RUNS["lean-run-bbb"] = run2
    handle2 = lifecycle.launch(req2)
    sid2 = plugin._PENDING.pop(handle2.subagent_id, None)
    run2.child_session_id = sid2
    plugin._CHILD_MAP[sid2] = run2.correlation_id

    # Verify distinct session IDs (even with same goal)
    assert sid1 != sid2, "Two runs must have different session IDs"
    assert handle1.subagent_id != handle2.subagent_id

    # Simulate interleaved post_api_request calls
    plugin._on_post_api_request(ctx, cfg,
        session_id=sid1,
        usage={"input_tokens": 200, "output_tokens": 100},
        model="haiku",
    )
    plugin._on_post_api_request(ctx, cfg,
        session_id=sid2,
        usage={"input_tokens": 50, "output_tokens": 25},
        model="haiku",
    )
    # Another call for run 1
    plugin._on_post_api_request(ctx, cfg,
        session_id=sid1,
        usage={"input_tokens": 100, "output_tokens": 50},
        model="haiku",
    )

    # Run 1: 2 calls, 300 input, 150 output
    assert run1.api_calls == 2
    assert run1.tokens_input == 300
    assert run1.tokens_output == 150

    # Run 2: 1 call, 50 input, 25 output
    assert run2.api_calls == 1
    assert run2.tokens_input == 50
    assert run2.tokens_output == 25

    _reset()


def test_missing_pending_no_tracking():
    """When subagent_start hook didn't fire, tokens=unknown.
    post_api_request for unknown sessions is silently ignored."""
    _reset()

    cfg = MagicMock()
    ctx = MagicMock()

    # Hook does nothing
    lifecycle = MockLifecycle(subagent_start_hook=lambda **kw: None)
    ctx.subagent_lifecycle = lifecycle

    launch_req = {
        "goal": "test task",
        "context": "",
        "correlation_id": "lean-missing",
        "role": "leaf",
        "allowed_toolsets": ("terminal",),
        "parent_session_id": "parent-1",
    }

    run = _make_run("lean-missing")
    plugin._ACTIVE_RUNS["lean-missing"] = run
    handle = lifecycle.launch(launch_req)

    # Pop will fail — hook didn't store
    child_session_id = plugin._PENDING.pop(handle.subagent_id, None)
    assert child_session_id is None, "Should be None when hook didn't fire"

    # post_api_request for unknown session is ignored
    plugin._on_post_api_request(ctx, cfg,
        session_id="some-unknown-session",
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    assert run.api_calls == 0, "Unknown session should not be tracked"

    _reset()
