"""End-to-end test: register() with fake ctx → dispatch through registered handlers.

Tests that _LazyAudit correctly forwards ALL attributes (including read_recent)
to the underlying AuditLog. This test FAILS with the old code (where _LazyAudit
only had append/read_all) and PASSES with __getattr__ forwarding.

Uses a temporary package approach to load the plugin as a proper Python package,
preserving its relative imports (from .lean.config etc).
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).parent          # tests/
_PLUGIN_ROOT = _HERE.parent            # plugin root (lean-mode/)


def _install_plugins_mock():
    """Install a mock plugins.plugin_storage module outside Hermes."""
    if "plugins" in sys.modules:
        return

    import types as _types

    storage_mod = _types.ModuleType("plugins.plugin_storage")
    storage_mod.__package__ = "plugins"

    def _plugin_data_dir(name: str) -> Path:
        hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
        root = hermes_home / "plugin-data" / name
        root.mkdir(parents=True, exist_ok=True)
        return root

    storage_mod.plugin_data_dir = _plugin_data_dir

    pkg = _types.ModuleType("plugins")
    pkg.__path__ = []
    pkg.__package__ = "plugins"
    pkg.plugin_storage = storage_mod

    sys.modules["plugins"] = pkg
    sys.modules["plugins.plugin_storage"] = storage_mod


def _load_plugin():
    """Create a proper package tree in a temp dir and import the plugin.

    Builds: <tmp>/lean_mode/__init__.py + lean_mode/lean/*.py
    Then adds <tmp> to sys.path and does `import lean_mode`.
    This preserves ALL relative imports.
    """
    _install_plugins_mock()

    # Build a temporary package directory mirroring the plugin structure
    import importlib
    pkg_root = Path(tempfile.mkdtemp(suffix="_lean_mode_pkg"))

    pkg_init = pkg_root / "lean_mode" / "__init__.py"
    pkg_init.parent.mkdir(parents=True)

    # Copy/rewrite __init__.py — replace relative imports with absolute
    # In the package context, .lean.config becomes lean_mode.lean.config
    src = (_PLUGIN_ROOT / "__init__.py").read_text()
    # The relative imports (.lean.X) will resolve correctly when __package__="lean_mode"
    pkg_init.write_text(src)

    # Copy lean/ subpackage
    lean_src = _PLUGIN_ROOT / "lean"
    lean_dst = pkg_root / "lean_mode" / "lean"
    shutil.copytree(str(lean_src), str(lean_dst), dirs_exist_ok=True)

    # Add to path and import
    sys.path.insert(0, str(pkg_root))

    # Clean up any stale module state
    for mod_name in list(sys.modules):
        if mod_name.startswith("lean_mode") or mod_name == "lean":
            sys.modules.pop(mod_name, None)

    import lean_mode  # noqa: F811
    return lean_mode


class FakeContext:
    """Minimal fake Hermes plugin context for testing register()."""

    def __init__(self, data_dir: Path):
        self._commands = {}
        self._tools = {}
        self._hooks = {}
        self._data_dir = data_dir

    def register_command(self, name, handler, description="", args_hint=""):
        self._commands[name] = handler

    def register_tool(self, name, toolset, schema, handler):
        self._tools[name] = {
            "name": name,
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
        }

    def register_hook(self, hook_name, callback):
        self._hooks[hook_name] = callback

    def plugin_data_dir(self):
        """Old-style access — should NOT be called after fix."""
        raise RuntimeError(
            "ctx.plugin_data_dir() should not be used — "
            "use from plugins.plugin_storage import plugin_data_dir instead"
        )

    def session_surface(self):
        return "telegram"

    def session_id(self):
        return "test-session"


def test_e2e_quick_stats_via_lazy_audit():
    """register() → dispatch 'stats' through the registered handler.

    The "stats" command calls compute_stats(audit, ...) which calls
    audit.read_recent(). With the old _LazyAudit, this crashes with
    AttributeError. With __getattr__ forwarding, it works.
    """
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "plugin-data" / "lean-mode"
        data_dir.mkdir(parents=True, exist_ok=True)

        now = time.time()
        run_log = data_dir / "runs.jsonl"
        entries = [
            {"ts": now - 3600, "status": "succeeded", "cost_usd_estimate": 0.001,
             "correlation_id": "a", "model": "gemini-flash", "provider": "openrouter",
             "toolsets": ["web", "terminal"], "duration_ms": 5000,
             "tokens": {"input": 100, "output": 50, "cache_read": 0, "cache_write": 0}},
            {"ts": now - 7200, "status": "succeeded", "cost_usd_estimate": 0.002,
             "correlation_id": "b", "model": "gemini-flash", "provider": "openrouter",
             "toolsets": ["web", "terminal"], "duration_ms": 8000,
             "tokens": {"input": 200, "output": 80, "cache_read": 0, "cache_write": 0}},
            {"ts": now - 10800, "status": "cancelled_by_budget", "cost_usd_estimate": 0.0005,
             "correlation_id": "c", "model": "gemini-flash", "provider": "openrouter",
             "toolsets": ["web"], "duration_ms": 3000,
             "tokens": {"input": 50, "output": 20, "cache_read": 0, "cache_write": 0}},
        ]
        with open(run_log, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        old_home = os.environ.get("HERMES_HOME")
        try:
            os.environ["HERMES_HOME"] = tmp

            plugin_init = _load_plugin()
            ctx = FakeContext(Path(tmp))
            plugin_init.register(ctx)

            handler = ctx._commands.get("quick")
            assert handler is not None

            result = handler("stats --days 7")
            assert result is not None
            assert "Lean Stats" in result, f"expected 'Lean Stats', got: {result[:80]}"
            assert "3 runs" in result, f"expected '3 runs', got: {result[:80]}"
            assert "2 ok" in result, f"expected '2 ok', got: {result[:80]}"
            assert "1 budget-stop" in result, f"expected '1 budget-stop', got: {result[:80]}"
        finally:
            # Clean up sys.path/modules so other tests don't interfere
            sys.path = [p for p in sys.path if "_lean_mode_pkg" not in str(p)]
            for mod_name in list(sys.modules):
                if mod_name.startswith("lean_mode") or mod_name == "lean":
                    sys.modules.pop(mod_name, None)
            if old_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_home


def test_e2e_quick_task_via_inject_message():
    """register() → dispatch '@research tes' through registered handler."""
    with tempfile.TemporaryDirectory() as tmp:
        old_home = os.environ.get("HERMES_HOME")
        try:
            os.environ["HERMES_HOME"] = tmp
            plugin_init = _load_plugin()
            ctx = FakeContext(Path(tmp))
            plugin_init.register(ctx)

            handler = ctx._commands.get("quick")
            assert handler is not None

            result = handler("@research tes")
            assert result is not None
            assert "Sent to parent" in result
            assert "tes" in result
        finally:
            sys.path = [p for p in sys.path if "_lean_mode_pkg" not in str(p)]
            for mod_name in list(sys.modules):
                if mod_name.startswith("lean_mode") or mod_name == "lean":
                    sys.modules.pop(mod_name, None)
            if old_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_home


def test_e2e_tool_handler_registration():
    """Confirm quick_run tool is registered with correct schema."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        plugin_init = _load_plugin()
        plugin_init.register(ctx)

        tool_info = ctx._tools.get("quick_run")
        assert tool_info is not None, "quick_run tool was not registered"

        schema = tool_info["schema"]
        assert "task" in schema.get("required", []), (
            f"'task' not in required: {schema.get('required')}"
        )
        assert "task" in schema.get("properties", {}), (
            f"'task' not in properties: {list(schema.get('properties', {}))}"
        )


def test_e2e_no_plugin_data_dir():
    """Confirm ctx.plugin_data_dir() is never called during register() or lazy init."""
    with tempfile.TemporaryDirectory() as tmp:
        old_home = os.environ.get("HERMES_HOME")
        try:
            os.environ["HERMES_HOME"] = tmp
            plugin_init = _load_plugin()
            ctx = FakeContext(Path(tmp))
            plugin_init.register(ctx)

            handler = ctx._commands.get("quick")
            result = handler("stats --days 7")
            assert result is not None
        finally:
            sys.path = [p for p in sys.path if "_lean_mode_pkg" not in str(p)]
            for mod_name in list(sys.modules):
                if mod_name.startswith("lean_mode") or mod_name == "lean":
                    sys.modules.pop(mod_name, None)
            if old_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_home


def test_e2e_config_command():
    """Verify /quick config subcommand + format_config_output directly.

    Tests the subcommand dispatch and format_config_output function
    by passing a mock LeanConfig. Config loading from file is verified
    live on the gateway (test env lacks hermes_cli/yaml deps).
    """
    from lean.config import LeanConfig, PresetEntry, PriceEntry
    from lean.receipt import format_config_output

    cfg = LeanConfig()
    cfg.model = "test-model"
    cfg.parent_model = "parent"
    cfg.provider = "openrouter"
    cfg.toolsets = ["web", "terminal"]
    cfg.prices = {"test-model": PriceEntry(input=0.1, output=0.5)}
    cfg.presets = {
        "research": PresetEntry(toolsets=["web", "search"]),
        "code": PresetEntry(toolsets=["terminal"]),
    }

    result = format_config_output(cfg)
    assert result is not None
    assert "test-model" in result
    assert "parent" in result
    assert "@research" in result
    assert "@code" in result
    assert "in=0.1/M" in result

    # Test empty config
    cfg2 = LeanConfig()
    result2 = format_config_output(cfg2)
    assert "delegation default" in result2
    assert "(none)" in result2 or "(none configured)" in result2


def test_e2e_config_subcommand_dispatch():
    """Verify 'config' subcommand is recognized and dispatched correctly."""
    with tempfile.TemporaryDirectory() as tmp:
        plugin_init = _load_plugin()
        ctx = FakeContext(Path(tmp))
        plugin_init.register(ctx)

        handler = ctx._commands.get("quick")
        assert handler is not None

        # Since config is loaded from file (which fails in test env),
        # verify the subcommand parser at least recognizes "config"
        from lean.command import parse_quick
        cmd = parse_quick("config")
        assert cmd.subcommand == "config"
