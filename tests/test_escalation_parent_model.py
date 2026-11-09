"""Test escalation uses cfg.parent_model (not cheap model) + parent toolsets."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_plugin_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_plugin_root))

from lean.config import LeanConfig
from lean.escalate import should_escalate
from lean.watchdog import RunState


class FakeHandle:
    def __init__(self, subagent_id="esc-handle-1"):
        self.subagent_id = subagent_id


@pytest.fixture
def ctx():
    ctx = MagicMock()
    lifecycle = MagicMock()
    lifecycle.launch.return_value = FakeHandle()
    # wait returns a terminal state — not timed out
    state = MagicMock()
    state.timed_out = False
    lifecycle.wait.return_value = state
    # result returns a subagent result
    res = MagicMock()
    res.summary = "escalated result"
    res.usage_metadata = None
    res.ready = True
    lifecycle.result.return_value = res
    ctx.subagent_lifecycle = lifecycle
    return ctx


@patch("lean.escalate.Watchdog")
def test_escalation_uses_parent_model_and_toolsets(mock_watchdog_cls, ctx):
    """Escalation must use cfg.parent_model AND parent toolsets,
    not the lean cheap model / lean.toolsets."""
    cfg = LeanConfig(
        model="surplus/glm-5.3-flash",
        parent_model="combo",
        toolsets=["web", "search", "file"],
    )

    cheap_run = RunState("lean-test-1", started_at=time.time(), budget_usd=0.01)
    cheap_outcome = {"status": "failed", "spent": 0.001}

    # Make watchdog return quickly
    mock_watchdog = MagicMock()
    mock_watchdog.monitor.return_value = {
        "status": "completed",
        "spent": 0.02,
        "duration": 1.0,
    }
    mock_watchdog_cls.return_value = mock_watchdog

    from lean.escalate import run_escalation
    result = run_escalation(
        cfg=cfg,
        ctx=ctx,
        cheap_outcome=cheap_outcome,
        cheap_run=cheap_run,
        parent_session_id="parent-sess-1",
        task="research AI agents",
        brief="Do the research",
        remaining_deadline=time.time() + 300,
        remaining_budget=0.05,
        parent_model=cfg.parent_model,
        parent_provider="",
    )

    # Verify escalation request used parent model, NOT cheap model
    launch_call = ctx.subagent_lifecycle.launch.call_args
    assert launch_call is not None, "launch() should have been called"
    esc_req = launch_call[0][0]

    assert esc_req.get("model") == "combo", (
        f"Escalation should use combo (parent_model), got {esc_req.get('model')}"
    )
    assert esc_req.get("model") != "surplus/glm-5.3-flash", (
        "Escalation must NOT use the cheap model"
    )

    # Verify escalation uses PARENT toolsets (allowed_toolsets=None = inherit)
    assert esc_req.get("allowed_toolsets") is None, (
        f"Escalation should inherit parent toolsets (None), got {esc_req.get('allowed_toolsets')}"
    )

    assert result is not None
    assert result.get("is_escalation") is True


def test_parent_model_auto_resolved():
    """When lean.parent_model is empty, auto-resolve from Hermes config."""
    cfg = LeanConfig.from_config({"model": "surplus/glm-5.3-flash"})
    assert cfg.parent_model == "combo", (
        f"Expected auto-resolved parent_model='combo', got '{cfg.parent_model}'"
    )


@patch("lean.escalate.Watchdog")
def test_escalation_empty_parent_model_defaults_to_hermes_model(mock_watchdog_cls, ctx):
    """When lean.parent_model is empty, escalation defaults to auto-resolved value."""
    cfg = LeanConfig(
        model="surplus/glm-5.3-flash",
        parent_model="",  # will be auto-resolved in from_config
    )
    # Trigger auto-resolve
    cfg = LeanConfig.from_config({"model": "surplus/glm-5.3-flash"})
    assert cfg.parent_model == "combo", "Should auto-resolve to combo"

    cheap_run = RunState("lean-test-3", started_at=time.time(), budget_usd=0.01)
    cheap_outcome = {"status": "failed", "spent": 0.001}

    mock_watchdog = MagicMock()
    mock_watchdog.monitor.return_value = {"status": "completed", "spent": 0.02}
    mock_watchdog_cls.return_value = mock_watchdog

    from lean.escalate import run_escalation
    result = run_escalation(
        cfg=cfg,
        ctx=ctx,
        cheap_outcome=cheap_outcome,
        cheap_run=cheap_run,
        parent_session_id="parent-sess-1",
        task="test",
        brief="brief",
        remaining_deadline=time.time() + 300,
        remaining_budget=0.05,
        parent_model=cfg.parent_model,
        parent_provider="",
    )

    esc_req = ctx.subagent_lifecycle.launch.call_args[0][0]
    assert esc_req.get("model") == "combo"
    assert esc_req.get("allowed_toolsets") is None, "Escalation should inherit parent toolsets"


def test_escalation_skipped_when_not_enough_time(ctx):
    """Must not escalate if remaining time < min_escalation_seconds."""
    cfg = LeanConfig(min_escalation_seconds=300)
    cheap_run = RunState("lean-test-2", started_at=time.time(), budget_usd=0.01)
    cheap_outcome = {"status": "failed", "spent": 0.001}

    from lean.escalate import run_escalation
    result = run_escalation(
        cfg=cfg,
        ctx=ctx,
        cheap_outcome=cheap_outcome,
        cheap_run=cheap_run,
        parent_session_id="parent-sess-1",
        task="test",
        brief="brief",
        remaining_deadline=time.monotonic() + 1,  # only 1s remaining (monotonic)
        remaining_budget=0.05,
        parent_model="combo",
        parent_provider="",
    )
    assert result is None, "Should skip escalation when not enough time"
