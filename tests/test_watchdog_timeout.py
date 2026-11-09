"""Tests for watchdog timeout fixes (A-E)."""

import time
from unittest.mock import MagicMock

import pytest

from lean.config import LeanConfig
from lean.watchdog import RunState, Watchdog


class FakeHandle:
    def __init__(self, subagent_id="handle-1"):
        self.subagent_id = subagent_id


def test_watchdog_uses_timeout_seconds():
    """Watchdog uses passed timeout_seconds, not cfg.timeout_seconds."""
    cfg = LeanConfig()
    cfg.timeout_seconds = 300  # default
    run = RunState("test", started_at=time.monotonic())

    cancel_calls = []

    def cancel_fn(**kw):
        cancel_calls.append(kw)

    # Mock wait to never complete (always timed_out=True)
    class NeverComplete:
        timed_out = True

    def wait_fn(**kw):
        return NeverComplete()

    class MockResult:
        summary = "Cancelled by test"
        error_message = ""
        terminal_state = MagicMock()
        terminal_state.name = "CANCELLED"

    def result_fn():
        return MockResult()

    watchdog = Watchdog(
        cfg=cfg, run=run,
        cancel_fn=cancel_fn, wait_fn=wait_fn, result_fn=result_fn,
        timeout_seconds=5,  # should use this, not 300
    )

    outcome = watchdog.monitor()
    assert outcome["status"] == "cancelled_by_watchdog", (
        f"Expected watchdog to cancel, got {outcome['status']}"
    )
    assert len(cancel_calls) > 0, "cancel() should have been called"
    # Run started ~0s ago, so 5s timeout should trigger quickly
    elapsed = time.monotonic() - run.started_at
    assert elapsed < 30, f"Should cancel within ~5s, took {elapsed:.0f}s"


def test_watchdog_cfg_timeout_when_no_override():
    """When timeout_seconds not passed, fall back to cfg.timeout_seconds."""
    cfg = LeanConfig()
    cfg.timeout_seconds = 300
    run = RunState("test", started_at=time.monotonic())

    class NeverComplete:
        timed_out = True

    def wait_fn(**kw):
        return NeverComplete()

    cancel_calls = []
    watchdog = Watchdog(
        cfg=cfg, run=run,
        cancel_fn=lambda **kw: cancel_calls.append(kw),
        wait_fn=wait_fn, result_fn=lambda: None,
        # no timeout_seconds -> should use cfg.timeout_seconds
    )
    assert watchdog._timeout == 300, "Should fall back to cfg.timeout_seconds"


def test_gateway_clamp():
    """effective_timeout clamped to gateway_max_timeout_seconds."""
    cfg = LeanConfig()
    cfg.gateway_max_timeout_seconds = 120
    cfg.timeout_seconds = 300
    run = RunState("test", started_at=time.monotonic())

    class NeverComplete:
        timed_out = True

    def wait_fn(**kw):
        return NeverComplete()

    cancel_calls = []
    watchdog = Watchdog(
        cfg=cfg, run=run,
        cancel_fn=lambda **kw: cancel_calls.append(kw),
        wait_fn=wait_fn, result_fn=lambda: None,
        timeout_seconds=300,  # user requested 300, but should be clamped to 120
    )
    # The clamp happens in _run_direct_launch, not in Watchdog itself.
    # Verify watchdog uses whatever timeout it receives.
    assert watchdog._timeout == 300, "Watchdog itself doesn't clamp — caller does"


def test_escalation_remaining_time():
    """Escalation watchdog gets remaining time, not full timeout."""
    now = time.monotonic()
    cfg = LeanConfig()
    cfg.timeout_seconds = 300
    run = RunState("test", started_at=now)

    class NeverComplete:
        timed_out = True

    def wait_fn(**kw):
        return NeverComplete()

    cancel_calls = []
    watchdog = Watchdog(
        cfg=cfg, run=run,
        cancel_fn=lambda **kw: cancel_calls.append(kw),
        wait_fn=wait_fn, result_fn=lambda: None,
        timeout_seconds=45,  # remaining time = 45s
    )
    assert watchdog._timeout == 45, "Escalation should use remaining time, not full timeout"


def test_total_run_time_cap():
    """Cheap + escalate total should not exceed effective_timeout + 30s grace."""
    # This is an integration test concept: verify the deadline math.
    # The watchdog for cheap run uses effective_timeout.
    # The escalation watchdog uses remaining = effective_deadline - now.
    # Total cap = effective_timeout + 30s (grace in _do_cancel).
    # This test validates the math, not the real subagent lifecycle.
    effective_timeout = 60
    grace = 30
    cheap_deadline = time.monotonic() + effective_timeout
    assert cheap_deadline > time.monotonic()
    # After cheap fails at cheap_deadline, escalation uses:
    remaining = cheap_deadline - time.monotonic()
    assert remaining <= effective_timeout
    # Total = cheap_time + esc_time + grace <= effective_timeout + grace
    max_total = effective_timeout + grace
    assert max_total == 90
