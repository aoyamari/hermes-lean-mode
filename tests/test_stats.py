"""Tests for stats aggregation."""

import json
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
            {"ts": now - 3600, "status": "succeeded", "cost_usd_estimate": 0.001, "correlation_id": "a"},
            {"ts": now - 7200, "status": "succeeded", "cost_usd_estimate": 0.002, "correlation_id": "b"},
            {"ts": now - 10800, "status": "succeeded", "cost_usd_estimate": 0.003, "correlation_id": "c",
             "escalated": {"trigger": "low_confidence", "model": "parent", "status": "succeeded", "cheap_cost_usd": 0.001}},
            {"ts": now - 14400, "status": "cancelled_by_budget", "cost_usd_estimate": 0.0005, "correlation_id": "d"},
        ]
        audit = _make_audit(Path(tmp), entries)
        stats = compute_stats(audit, days=7)

        assert stats["total_runs"] == 4
        assert stats["succeeded"] == 3
        assert stats["escalated"] == 1
        assert stats["cancelled_budget"] == 1


def test_stats_unknown_cost():
    """Runs without cost data are counted but excluded from $."""
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        entries = [
            {"ts": now - 3600, "status": "succeeded", "correlation_id": "a"},  # no cost fields
            {"ts": now - 7200, "status": "succeeded", "cost_usd_estimate": 0.005, "correlation_id": "b"},
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
            {"ts": now - 3600, "status": "succeeded", "cost_usd_estimate": 0.001, "preset": "research", "correlation_id": "a"},
            {"ts": now - 7200, "status": "succeeded", "cost_usd_estimate": 0.002, "preset": "research", "correlation_id": "b"},
            {"ts": now - 10800, "status": "succeeded", "cost_usd_estimate": 0.003, "preset": "code", "correlation_id": "c"},
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
        {"ts": 1000.0, "status": "succeeded", "cost_usd_estimate": 0.002},
        {"ts": 1100.0, "status": "succeeded", "cost_usd_estimate": 0.003},
        {"ts": 0, "status": "low_confidence", "cost_usd_estimate": 0.0, "escalated": {"status": "succeeded"}},
    ]
    result = compute_stats(audit)
    assert result["skipped_count"] == 2, f"Expected 2 skipped, got {result['skipped_count']}"
    assert result["total_runs"] == 2, f"Expected 2 valid runs, got {result['total_runs']}"
    assert result["succeeded"] == 2
