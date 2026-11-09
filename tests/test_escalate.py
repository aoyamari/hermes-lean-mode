"""Tests for escalation logic."""

from lean.config import LeanConfig
from lean.escalate import should_escalate
from lean.watchdog import _strip_confidence


def test_should_escalate():
    """Each trigger in escalate_on escalates."""
    cfg = LeanConfig()
    cfg.escalate = True
    cfg.escalate_on = ["failed", "empty_result", "max_iterations", "low_confidence", "launch_failed"]

    for trigger in cfg.escalate_on:
        result, reason = should_escalate(cfg, trigger)
        assert result is True, f"Should escalate on {trigger}"
        assert reason is None

    # Not a trigger
    result, reason = should_escalate(cfg, "succeeded")
    assert result is False


def test_should_not_escalate_budget():
    """cancelled_by_budget never escalates."""
    cfg = LeanConfig()
    result, reason = should_escalate(cfg, "cancelled_by_budget")
    assert result is False


def test_should_not_escalate_watchdog():
    """cancelled_by_watchdog never escalates."""
    cfg = LeanConfig()
    result, reason = should_escalate(cfg, "cancelled_by_watchdog")
    assert result is False


def test_should_not_escalate_disabled():
    """Escalation disabled → no escalate."""
    cfg = LeanConfig()
    cfg.escalate = False
    result, reason = should_escalate(cfg, "failed")
    assert result is False
    assert reason == "escalation_disabled"


def test_strip_confidence():
    """LEAN_CONFIDENCE marker stripped from output."""
    text = "Task completed.\nAll done.\nLEAN_CONFIDENCE: low"
    stripped = _strip_confidence(text)
    assert "LEAN_CONFIDENCE" not in stripped
    assert stripped == "Task completed.\nAll done."

    text2 = "Normal output\nLEAN_CONFIDENCE: ok"
    stripped2 = _strip_confidence(text2)
    assert stripped2 == "Normal output"

    # No marker
    text3 = "Just normal text"
    stripped3 = _strip_confidence(text3)
    assert stripped3 == text3
