"""Tests for watchdog and budget."""

import time
from unittest.mock import MagicMock
from lean.config import LeanConfig, PriceEntry
from lean.watchdog import RunState, Watchdog


def test_watchdog_normal_completion():
    """Child finishes normally → no cancel."""
    cfg = LeanConfig()
    cfg.poll_interval_seconds = 0.1
    run = RunState("test", time.monotonic())

    mock_handle = MagicMock()
    wait_result = MagicMock()
    wait_result.timed_out = False
    mock_wait = MagicMock(return_value=wait_result)

    result = MagicMock()
    result.terminal_state.name = "SUCCEEDED"
    result.summary = "Task completed.\nLEAN_CONFIDENCE: ok"
    result.error_message = ""
    result.error_classification = ""
    mock_result = MagicMock(return_value=result)

    cancel = MagicMock()

    wd = Watchdog(cfg, run, cancel, mock_wait, mock_result)
    outcome = wd.monitor()

    assert outcome["status"] == "succeeded"
    cancel.assert_not_called()


def test_watchdog_times_out():
    """Child that never finishes → cancel called once, status cancelled_by_watchdog."""
    cfg = LeanConfig()
    cfg.timeout_seconds = 2
    cfg.poll_interval_seconds = 1
    run = RunState("test", time.monotonic())

    cancel = MagicMock()
    wait_result = MagicMock()
    wait_result.timed_out = True
    mock_wait = MagicMock(return_value=wait_result)

    result = MagicMock()
    result.terminal_state.name = "CANCELLED"
    result.summary = "Partial"
    result.error_message = ""
    result.error_classification = ""
    mock_result = MagicMock(return_value=result)

    wd = Watchdog(cfg, run, cancel, mock_wait, mock_result)
    outcome = wd.monitor()

    assert outcome["status"] == "cancelled_by_watchdog"
    cancel.assert_called_once()


def test_budget_cap():
    """Budget cap stops the run."""
    cfg = LeanConfig()
    cfg.timeout_seconds = 60
    cfg.poll_interval_seconds = 0.1
    cfg.prices["test-model"] = PriceEntry(input=10.0, output=50.0)  # expensive to trigger budget fast
    run = RunState("test", time.monotonic(), budget_usd=0.001)
    run.tokens_input = 100
    run.tokens_output = 50
    run.response_model = "test-model"

    cancel = MagicMock()
    wait_result = MagicMock()
    wait_result.timed_out = True
    mock_wait = MagicMock(return_value=wait_result)

    result = MagicMock()
    result.terminal_state.name = "CANCELLED"
    result.summary = ""
    result.error_message = ""
    result.error_classification = ""
    mock_result = MagicMock(return_value=result)

    wd = Watchdog(cfg, run, cancel, mock_wait, mock_result)
    outcome = wd.monitor()

    assert outcome["status"] == "cancelled_by_budget"
    assert outcome["budget_spent"] > 0


def test_budget_no_price_refuses():
    """Budget with no price entry → watchdog cannot price it, times out."""
    cfg = LeanConfig()
    cfg.timeout_seconds = 1  # short timeout so test finishes
    cfg.poll_interval_seconds = 0.2
    run = RunState("test", time.monotonic(), budget_usd=0.02)
    run.tokens_input = 50
    run.response_model = "no-price-model"

    cancel = MagicMock()
    wait_result = MagicMock()
    wait_result.timed_out = True
    mock_wait = MagicMock(return_value=wait_result)

    result = MagicMock()
    result.terminal_state.name = "CANCELLED"
    result.summary = ""
    result.error_message = ""
    result.error_classification = ""
    mock_result = MagicMock(return_value=result)

    wd = Watchdog(cfg, run, cancel, mock_wait, mock_result)
    # With no price entry, spent_usd returns 0, so budget won't trigger
    # The check happens in __init__.py before launch
    outcome = wd.monitor()
    assert outcome["status"] != "cancelled_by_budget"


def test_run_state_spent():
    """RunState.spent_usd computes correctly."""
    cfg = LeanConfig()
    cfg.prices["test"] = PriceEntry(input=1.0, output=5.0, cache_read=0.5, cache_write=1.0)
    run = RunState("test", 1000.0)
    run.tokens_input = 1000
    run.tokens_output = 500
    run.tokens_cache_read = 200
    run.response_model = "test"

    spent = run.spent_usd(cfg.prices)
    expected = (1000 * 1e-6) + (500 * 5e-6) + (200 * 0.5e-6)
    assert abs(spent - expected) < 0.0001
