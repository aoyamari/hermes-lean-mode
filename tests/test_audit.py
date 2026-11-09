"""Tests for audit log."""

import json
import tempfile
from pathlib import Path
from lean.audit import AuditLog, build_audit_entry
from lean.watchdog import RunState


def test_audit_append_and_read():
    """One line per run, readable back."""
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(Path(tmp), retention_lines=100)
        run = RunState("test-correlation", 1000.0)
        run.tokens_input = 100
        run.tokens_output = 50
        run.response_model = "test-model"

        outcome = {
            "status": "succeeded",
            "spent": 0.001,
            "duration": 5.0,
            "ended_at": 1005.0,
        }

        entry = build_audit_entry(
            correlation_id="test-correlation",
            status="succeeded",
            outcome=outcome,
            run_state=run,
        )
        audit.append(entry)

        entries = audit.read_all()
        assert len(entries) == 1
        assert entries[0]["status"] == "succeeded"
        assert entries[0]["correlation_id"] == "test-correlation"


def test_audit_rotation():
    """Rotation keeps newest N lines."""
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(Path(tmp), retention_lines=3, max_bytes=1)  # tiny max to force rotation
        run = RunState("test", 1000.0)
        run.tokens_input = 10
        run.response_model = "m"

        for i in range(10):
            outcome = {"status": "succeeded", "spent": 0.0, "duration": 1.0, "ended_at": 1000.0 + i}
            entry = build_audit_entry(
                correlation_id=f"test-{i}", status="succeeded", outcome=outcome, run_state=run,
            )
            audit.append(entry)

        entries = audit.read_all()
        assert len(entries) <= 3  # rotated to retention_lines + some

        # Entries should be the newest ones
        assert entries[-1]["correlation_id"] == "test-9"  # newest


def test_audit_no_secrets():
    """Goal is truncated, no secrets in log."""
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(Path(tmp))
        run = RunState("test", 1000.0)
        run.tokens_input = 10
        run.response_model = "m"

        outcome = {"status": "succeeded", "spent": 0.0, "duration": 1.0, "ended_at": 1000.0}
        entry = build_audit_entry(
            correlation_id="test", status="succeeded", outcome=outcome, run_state=run,
        )
        # No full goal or result in entry
        assert "goal" not in entry
        assert "result" not in entry
        assert "result_hash" in entry


def test_audit_escalated_entry():
    """Escalated runs have escalation info in audit."""
    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditLog(Path(tmp))
        run = RunState("test-esc", 1000.0)
        run.tokens_input = 100
        run.response_model = "full-model"

        outcome = {
            "status": "succeeded", "spent": 0.03, "duration": 30.0, "ended_at": 1030.0,
            "is_escalation": True, "escalation_trigger": "low_confidence",
            "escalation_cheap_cost": 0.001, "escalation_model": "full-model",
        }

        cheap_outcome = {"status": "low_confidence", "spent": 0.001}
        entry = build_audit_entry(
            correlation_id="test-esc", status="succeeded", outcome=outcome,
            run_state=run, cheap_outcome=cheap_outcome,
        )
        audit.append(entry)

        entries = audit.read_all()
        assert len(entries) == 1
        assert "escalated" in entries[0]
        assert entries[0]["escalated"]["trigger"] == "low_confidence"
        assert entries[0]["escalated"]["cheap_cost_usd"] == 0.001
