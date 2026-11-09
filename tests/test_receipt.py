"""Tests for receipt generation."""

import pytest
from lean.config import LeanConfig, PriceEntry
from lean.receipt import format_receipt, format_stats_output
from lean.watchdog import RunState
from lean.stats import compute_stats


def test_receipt_format():
    """Token buckets × price table → exact $."""
    cfg = LeanConfig()
    cfg.prices["deepseek-r1"] = PriceEntry(input=0.091, output=0.182, cache_read=0.045, cache_write=0.091)

    run = RunState("test-correlation", 1000.0)
    run.tokens_input = 1000
    run.tokens_output = 500
    run.tokens_cache_read = 200
    run.api_calls = 3
    run.response_model = "deepseek-r1"

    outcome = {
        "status": "succeeded",
        "result_text": "Task completed",
        "spent": 0.000191,
        "duration": 12.5,
        "turns": 2,
        "tool_calls": 5,
    }

    receipt = format_receipt(cfg, run, outcome)
    assert "⚡ Lean receipt" in receipt
    assert "deepseek-r1" in receipt
    assert "3 calls" in receipt
    assert "est. cost $0.0002" in receipt


def test_receipt_missing_buckets():
    """Missing buckets → unknown cost."""
    cfg = LeanConfig()
    run = RunState("test-correlation", 1000.0)
    run.api_calls = 1
    run.response_model = "unknown-model"

    outcome = {
        "status": "succeeded",
        "result_text": "Done",
        "spent": 0.0,
        "duration": 5.0,
        "turns": 1,
        "tool_calls": 0,
    }

    receipt = format_receipt(cfg, run, outcome)
    assert "est. cost $0.0000" in receipt


def test_receipt_budget_line():
    """Budget line appears when budget is set."""
    cfg = LeanConfig()
    cfg.prices["test-model"] = PriceEntry(input=0.10, output=0.50)
    run = RunState("test", 1000.0, budget_usd=0.02)
    run.tokens_input = 100
    run.tokens_output = 50
    run.api_calls = 1
    run.response_model = "test-model"

    outcome = {
        "status": "cancelled_by_budget",
        "result_text": "Partial result",
        "spent": 0.021,
        "duration": 3.0,
        "budget_spent": 0.021,
        "budget_limit": 0.02,
        "turns": 1,
        "tool_calls": 0,
    }

    receipt = format_receipt(cfg, run, outcome)
    assert "budget $0.02" in receipt
    assert "spent $0.0210" in receipt


def test_receipt_escalated():
    """Escalated receipt shows negative savings."""
    cfg = LeanConfig()
    cfg.prices["cheap-model"] = PriceEntry(input=0.091, output=0.182)
    cfg.prices["full-model"] = PriceEntry(input=1.0, output=5.0)
    run = RunState("test-esc", 1000.0)
    run.response_model = "full-model"
    run.tokens_input = 500
    run.tokens_output = 200
    run.api_calls = 2

    outcome = {
        "status": "succeeded",
        "result_text": "Escalated result",
        "spent": 0.03,
        "duration": 30.0,
        "is_escalation": True,
        "escalation_trigger": "low_confidence",
        "escalation_cheap_cost": 0.001,
        "turns": 2,
        "tool_calls": 3,
    }

    receipt = format_receipt(cfg, run, outcome, is_escalated=True,
                              escalation_cheap_cost=0.001, escalation_trigger="low_confidence")
    assert "ESCALATED" in receipt
    assert "low_confidence" in receipt
    assert "wasted" in receipt


def test_receipt_comparison():
    """vs non-Lean comparison when parent price exists."""
    cfg = LeanConfig()
    cfg.prices["cheap"] = PriceEntry(input=0.091, output=0.182)
    cfg.prices["parent"] = PriceEntry(input=1.0, output=5.0)
    run = RunState("test", 1000.0)
    run.tokens_input = 1000
    run.tokens_output = 500
    run.response_model = "cheap"

    outcome = {
        "status": "succeeded", "result_text": "Done",
        "spent": 0.000182, "duration": 5.0, "turns": 1, "tool_calls": 0,
    }

    receipt = format_receipt(cfg, run, outcome, parent_model="parent")
    assert "vs non-Lean" in receipt
    assert "parent" in receipt


def test_receipt_no_comparison():
    """No comparison when parent price missing."""
    cfg = LeanConfig()
    cfg.prices["cheap"] = PriceEntry(input=0.091, output=0.182)
    run = RunState("test", 1000.0)
    run.response_model = "cheap"

    outcome = {
        "status": "succeeded", "result_text": "Done",
        "spent": 0.0, "duration": 5.0, "turns": 1, "tool_calls": 0,
    }

    receipt = format_receipt(cfg, run, outcome, parent_model="parent")
    assert "vs non-Lean" not in receipt


def test_stats_output():
    """Stats formatting."""
    stats = {
        "period": "last 7 days",
        "total_runs": 37,
        "succeeded": 30,
        "escalated": 3,
        "cancelled_budget": 2,
        "cancelled_watchdog": 2,
        "total_spent": 4.21,
        "total_saved": 3.80,
        "is_estimate": True,
        "unknown_cost_runs": 1,
        "top_preset": "research",
    }
    output = format_stats_output(stats)
    assert "37 runs" in output
    assert "30 ok" in output
    assert "3 escalated" in output
    assert "$4.21" in output
    assert "@research" in output
    assert "unknown" in output
