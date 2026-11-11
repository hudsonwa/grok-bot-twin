"""register() registers exactly what plugin.yaml declares, and is pure (no files, no threads)."""
from __future__ import annotations

import threading

import yaml

from twin_testlib import ROOT, home, plugin, timer  # noqa: F401


class Recorder:
    profile_name = "default"
    plugin_id = "grok-bot-twin"

    def __init__(self):
        self.tools, self.hooks, self.cli, self.commands, self.unload = [], [], [], [], []

    def register_tool(self, name, toolset, schema, handler, **kw):
        assert schema["name"] == name and callable(handler)
        self.tools.append(name)

    def register_hook(self, name, cb, **kw):
        self.hooks.append((name, cb))

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli.append((name, setup_fn, handler_fn))

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands.append((name, handler))

    def on_unload(self, cb):
        self.unload.append(cb)

    def __getattr__(self, name):  # ctx.state / ctx.llm must not be touched in register()
        raise AttributeError(name)


def manifest():
    return yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))


def test_manifest_basics():
    m = manifest()
    assert m["name"] == "grok-bot-twin"
    assert m["version"] == "0.3.0" and isinstance(m["version"], str)
    assert m["license"] == "MIT"
    schema = m["config_schema"]
    assert schema["sync_interval_seconds"]["default"] == 60 and schema["sync_interval_seconds"]["min"] == 5
    for key in ("bots", "botport_path", "digest_to_grok", "digest_titles"):
        assert key in schema
    assert schema["digest_to_grok"]["default"] is False and schema["digest_titles"]["default"] is False
    assert "Disclosure" in m["description"] and "not affiliated with xAI" in m["description"]


def test_register_matches_manifest(home):
    m = manifest()
    ctx = Recorder()
    before_threads = {t.ident for t in threading.enumerate()}
    plugin.register(ctx)
    assert sorted(ctx.tools) == sorted(m["provides_tools"])
    assert sorted(n for n, _ in ctx.hooks) == sorted(m["provides_hooks"])
    assert [c[0] for c in ctx.cli] == ["twin"]
    assert [c[0] for c in ctx.commands] == ["twin"]
    assert len(ctx.unload) == 1
    # pure: nothing written, no thread started
    assert list(home.rglob("*")) == []
    assert {t.ident for t in threading.enumerate()} == before_threads
    assert not timer.running()


def test_hooks_accept_kwargs(home):
    ctx = Recorder()
    plugin.register(ctx)
    hooks = dict(ctx.hooks)
    # extra, unknown keywords must not break any hook
    assert hooks["pre_llm_call"](session_id="s1", user_message="hi", future_field=1) is None  # no twin yet
    assert hooks["on_session_start"](session_id="s1", platform="cli", extra=True) is None
    assert hooks["on_session_end"](session_id="s1", completed=True, extra=True) is None
    timer._reset_for_tests()
