"""Tests for command parsing."""

from lean.command import parse_quick


def test_simple_task():
    cmd = parse_quick("summarize the latest sessions")
    assert cmd.task == "summarize the latest sessions"
    assert cmd.subcommand is None
    assert cmd.preset is None


def test_preset():
    cmd = parse_quick("@research find latest AI papers")
    assert cmd.preset == "research"
    assert cmd.task == "find latest AI papers"


def test_stats():
    cmd = parse_quick("stats")
    assert cmd.subcommand == "stats"
    assert cmd.days is None


def test_stats_with_days():
    cmd = parse_quick("stats --days 30")
    assert cmd.subcommand == "stats"
    assert cmd.days == 30


def test_budget_flag():
    cmd = parse_quick("--budget 0.02 research new models")
    assert cmd.budget_usd == 0.02
    assert cmd.task == "research new models"


def test_no_escalate():
    cmd = parse_quick("--no-escalate run this task")
    assert cmd.no_escalate is True
    assert cmd.task == "run this task"


def test_full():
    cmd = parse_quick("--full deploy the app")
    assert cmd.full is True
    assert cmd.task == "deploy the app"


def test_model():
    cmd = parse_quick("--model claude-sonnet-4-5 test task")
    assert cmd.model == "claude-sonnet-4-5"
    assert cmd.task == "test task"


def test_tools_flag():
    cmd = parse_quick("--tools terminal,read test")
    assert cmd.toolsets == ["terminal", "read"]
    assert cmd.task == "test"


def test_timeout_flag():
    cmd = parse_quick("--timeout 60 long task")
    assert cmd.timeout_seconds == 60
    assert cmd.task == "long task"


def test_preset_with_flags():
    cmd = parse_quick("@code --budget 0.05 --no-escalate fix the bug")
    assert cmd.preset == "code"
    assert cmd.budget_usd == 0.05
    assert cmd.no_escalate is True
    assert cmd.task == "fix the bug"


def test_empty():
    cmd = parse_quick("")
    assert cmd.task == ""


def test_apostrophe_in_task_does_not_crash():
    cmd = parse_quick("@research what's today's gold price")
    assert cmd.preset == "research"
    assert cmd.task == "what's today's gold price"
