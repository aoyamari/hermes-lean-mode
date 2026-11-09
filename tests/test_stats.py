"""Tests for stats aggregation."""

import tempfile
import time
from pathlib import Path
from lean.audit import AuditLog
from lean.stats import compute_stats


def _make_audit(tmp: Path, entries: list[dict]) -> AuditLog:
    log = AuditLog(tmp, retention_lines=100)
    for entry in entries:
        log.append(entry)
    return log


def test_stats_empty():
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(Path(tmp))
        stats = compute_stats(audit, days=7)
        assert stats["total_runs"] == 0


def test_stats_mixed():
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        entries = [
            {"ts": now - 3600, "status": "succeeded", "cost_usd_estimate": 0.001, 
             "vs_non_lean_usd": 0.003, "correlation_id": "a"},
            {"ts": now - 7200, "status": "succeeded", "cost_usd_estimate": 0.002, 
             "vs_non_lean_usd": 0.006, "correlation_id": "b"},
            {"ts": now - 10800, "status": "succeeded", "cost_usd_estimate": 0.003,
             "vs_non_lean_usd": 0.009, "correlation_id": "c",
             "escalated": {"trigger": "low_confidence", "model": "parent", "status": "succeeded", "cheap_cost_usd": 0.001}},
            {"ts": now - 14400, "status": "cancelled_by_budget", "cost_usd_estimate": 0.0005,
             "vs_non_lean_usd": 0.002, "correlation_id": "d"},
        ]
        audit = _make_audit(Path(tmp), entries)
        stats = compute_stats(audit, days=7)

        assert stats["total_runs"] == 4
        assert stats["succeeded"] == 3
        assert stats["escalated"] == 1
        assert stats["cancelled_budget"] == 1
        
        # net_saved = (vs_non_lean - actual) for all runs with vs_non_lean
        # a: 0.003 - 0.001 = 0.002
        # b: 0.006 - 0.002 = 0.004
        # c: 0.009 - 0.003 = 0.006 (escalated, waste already in actual)
        # d: 0.002 - 0.0005 = 0.0015
        # total: 0.002 + 0.004 + 0.006 + 0.0015 = 0.0135
        expected_net = 0.0135
        assert abs(stats["net_saved"] - expected_net) < 0.0001, (
            f"Expected net_saved={expected_net}, got {stats['net_saved']}"
        )
        assert stats["savings_counted_runs"] == 4
        
        # Escalated waste = cheap_cost from the escalated entry
        assert abs(stats["escalated_wasted"] - 0.001) < 0.0001


def test_stats_unknown_cost():
    """Runs without cost data are counted but excluded from $."""
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        entries = [
            {"ts": now - 3600, "status": "succeeded", "correlation_id": "a"},  # no cost fields
            {"ts": now - 7200, "status": "succeeded", "cost_usd_estimate": 0.005, 
             "vs_non_lean_usd": 0.015, "correlation_id": "b"},
        ]
        audit = _make_audit(Path(tmp), entries)
        stats = compute_stats(audit, days=7)

        assert stats["total_runs"] == 2
        assert stats["unknown_cost_runs"] == 1
        assert stats["total_spent"] == 0.005


def test_stats_preset_tracking():
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        entries = [
            {"ts": now - 3600, "status": "succeeded", "cost_usd_estimate": 0.001,
             "vs_non_lean_usd": 0.003, "preset": "research", "correlation_id": "a"},
            {"ts": now - 7200, "status": "succeeded", "cost_usd_estimate": 0.002,
             "vs_non_lean_usd": 0.006, "preset": "research", "correlation_id": "b"},
            {"ts": now - 10800, "status": "succeeded", "cost_usd_estimate": 0.003,
             "vs_non_lean_usd": 0.009, "preset": "code", "correlation_id": "c"},
        ]
        audit = _make_audit(Path(tmp), entries)
        stats = compute_stats(audit, days=7)
        assert stats["top_preset"] == "research"
    assert stats["skipped_count"] == 0


def test_stats_skipped_bad_entries():
    """Entries with ts=0 are reported as skipped, not silently dropped."""
    audit = AuditLog.__new__(AuditLog)
    audit.read_all = lambda: [
        {"ts": 0, "status": "succeeded", "cost_usd_estimate": 0.001},
        {"ts": 1000.0, "status": "succeeded", "cost_usd_estimate": 0.002, "vs_non_lean_usd": 0.006},
        {"ts": 1100.0, "status": "succeeded", "cost_usd_estimate": 0.003, "vs_non_lean_usd": 0.009},
        {"ts": 0, "status": "low_confidence", "cost_usd_estimate": 0.0, "escalated": {"status": "succeeded"}},
    ]
    result = compute_stats(audit)
    assert result["skipped_count"] == 2, f"Expected 2 skipped, got {result['skipped_count']}"
    assert result["total_runs"] == 2, f"Expected 2 valid runs, got {result['total_runs']}"
    assert result["succeeded"] == 2


def test_stats_legacy_entries():
    """Legacy entries without vs_non_lean_usd get it recomputed from tokens + prices."""
    from lean.config import PriceEntry
    prices = {
        "combo": PriceEntry(input=1.0, output=5.0, cache_read=0.5, cache_write=1.0),
    }
    parent_model = "combo"

    audit = AuditLog.__new__(AuditLog)
    now_ts = 2000.0
    audit.read_all = lambda: [
        # Entry without vs_non_lean_usd: should be recomputed
        {"ts": now_ts, "status": "succeeded", "cost_usd_estimate": 0.001,
         "tokens": {"input": 100, "output": 50, "cache_read": 20, "cache_write": 0}},
        # New entry with vs_non_lean_usd
        {"ts": now_ts + 100, "status": "succeeded", "cost_usd_estimate": 0.002,
         "vs_non_lean_usd": 0.006, "tokens": {"input": 200, "output": 100}},
        # Escalated legacy entry (no vs_non_lean_usd)
        {"ts": now_ts + 200, "status": "escalated_low_confidence", "cost_usd_estimate": 0.003,
         "tokens": {"input": 100, "output": 50},
         "escalated": {"tokens_input": 200, "tokens_output": 100, "cache_read": 50, "cheap_cost_usd": 0.0005}},
        # Entry where parent_model price lookup fails
        {"ts": now_ts + 300, "status": "succeeded", "cost_usd_estimate": 0.004,
         "tokens": {"input": 50, "output": 25}},
    ]
    result = compute_stats(audit, prices=prices, parent_model=parent_model)

    # Entry 1: tokens=(100 in, 50 out, 20 cr) @ combo=1/5/0.5 = 100*1e-6 + 50*5e-6 + 20*0.5e-6 = 0.00036
    # Entry 2: vs_non_lean_usd=0.006 (pre-computed)
    # Entry 3: cheap=(100 in, 50 out) + esc=(200 in, 100 out, 50 cr) = (300 in, 150 out, 50 cr)
    #          @ combo = 300*1e-6 + 150*5e-6 + 50*0.5e-6 = 0.001075
    # Entry 4: tokens=(50 in, 25 out) no escalation -> also recomputed
    #          @ combo = 50*1e-6 + 25*5e-6 = 0.000175
    expected_vnl = 0.00036 + 0.006 + 0.001075 + 0.000175  # = 0.00761
    assert abs(result["vs_non_lean_total"] - expected_vnl) < 0.00001, (
        f"Expected {expected_vnl}, got {result['vs_non_lean_total']}"
    )
    # Savings counted = 4 (all entries have cost + parent_model price)
    assert result["savings_counted_runs"] == 4
    assert result["savings_unknown_runs"] == 0

    # Net saved = vs_non_lean_total - total_spent
    # total_spent = 0.001 + 0.002 + 0.003 + 0.004 = 0.01
    expected_net = expected_vnl - 0.01
    assert abs(result["net_saved"] - expected_net) < 0.00001
