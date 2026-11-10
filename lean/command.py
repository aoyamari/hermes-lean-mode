"""Command parsing for `/quick` — flags, @presets, subcommands."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class QuickCommand:
    """Parsed `/quick` command."""
    subcommand: Optional[str] = None         # "stats" or None
    preset: Optional[str] = None             # @research, @code
    task: str = ""
    model: Optional[str] = None
    toolsets: Optional[List[str]] = None
    timeout_seconds: Optional[int] = None
    budget_usd: Optional[float] = None
    no_escalate: bool = False
    full: bool = False
    days: Optional[int] = None               # for stats


def parse_quick(raw_args: str) -> QuickCommand:
    """Parse `/quick [@preset] [flags] <task>` or `/quick stats [--days N]`."""
    cmd = QuickCommand()

    if not raw_args or not raw_args.strip():
        cmd.task = ""
        return cmd

    # Apostrophes in free text ("today's") are not quotes: shlex would fail on
    # one and silently drop a pair. Only honour double quotes.
    try:
        lex = shlex.shlex(raw_args, posix=True)
        lex.whitespace_split = True
        lex.quotes = '"'
        parts = list(lex)
    except ValueError:
        parts = raw_args.split()
    idx = 0

    # Check for stats subcommand
    if parts[idx] == "stats":
        cmd.subcommand = "stats"
        idx += 1
        while idx < len(parts):
            if parts[idx] == "--days" and idx + 1 < len(parts):
                try:
                    cmd.days = int(parts[idx + 1])
                    idx += 2
                except (ValueError, IndexError):
                    idx += 1
            else:
                idx += 1
        return cmd

    # Check for config subcommand
    if parts[idx] == "config":
        cmd.subcommand = "config"
        return cmd

    # Parse @preset
    if parts[idx].startswith("@"):
        cmd.preset = parts[idx][1:]
        idx += 1

    # Parse flags
    while idx < len(parts) and parts[idx].startswith("--"):
        flag = parts[idx]
        val = parts[idx + 1] if idx + 1 < len(parts) else None

        if flag == "--model" and val:
            cmd.model = val
            idx += 2
        elif flag == "--tools" and val:
            cmd.toolsets = [t.strip() for t in val.split(",")]
            idx += 2
        elif flag == "--timeout" and val:
            try:
                cmd.timeout_seconds = int(val)
                idx += 2
            except ValueError:
                idx += 1
        elif flag == "--budget" and val:
            try:
                cmd.budget_usd = float(val)
                idx += 2
            except ValueError:
                idx += 1
        elif flag == "--no-escalate":
            cmd.no_escalate = True
            idx += 1
        elif flag == "--full":
            cmd.full = True
            idx += 1
        else:
            idx += 1

    # --full and --model are mutually exclusive
    if cmd.full and cmd.model:
        cmd.full = False  # --model wins over --full for model selection
    if cmd.full:
        cmd.no_escalate = True

    # Remaining = task
    cmd.task = " ".join(parts[idx:]) if idx < len(parts) else ""
    return cmd
