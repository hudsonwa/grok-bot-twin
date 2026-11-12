"""C2 finding F5: a model could start remote turns on another computer, and the local-only check failed open.

Fixed by removal (0.3.4): the model tool `twin_ask_machine` is gone (the owner keeps `/twin ask` and `hermes twin
ask`), the approval mode "linked" (approve a step from the computer that asked) is gone, and every "is this on
this computer?" check fails closed: a missing, empty or unknown platform, or Hermes' own per-chat values that
cannot be read, count as NOT local. Invented names only.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
import yaml

from test_register import Recorder
from test_security_twins import _linked
from twin_testlib import home, plugin  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import config, slash, tasks, tasks_api, tasks_hub
from hermes_plugins.grok_bot_twin.twin.connect import BadInput
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

ROOT = Path(__file__).resolve().parents[1]
REFUSED = "From a messaging chat"


def _hermes_says(monkeypatch, values: dict) -> None:
    """Stand in for Hermes' own per-chat values (gateway.session_context)."""
    ctx = types.ModuleType("gateway.session_context")
    ctx.get_session_env = lambda name, default="": values.get(name, default)
    ctx.session_is_messaging_surface = lambda: False
    pkg = types.ModuleType("gateway")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "gateway", pkg)
    monkeypatch.setitem(sys.modules, "gateway.session_context", ctx)


def test_F5_register_records_no_ask_machine_tool(home):  # noqa: F811
    ctx = Recorder()
    plugin.register(ctx)
    assert "twin_ask_machine" not in ctx.tools and ctx.tools  # (the other tools are still there)
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert "twin_ask_machine" not in manifest["provides_tools"]
    for name in ("twin_ask_machine", "TOOL_NAME", "TOOL_SCHEMA", "context_block"):
        assert not hasattr(tasks_api, name), name


def test_F5_local_check_refuses_empty_unknown_platform_and_missing_gateway(home, tmp_path, monkeypatch):  # noqa: F811
    # `/twin`: only a platform Hermes names as one of its local surfaces counts as this computer
    for platform in (None, "", "  ", "telegram", "api_server", "something-new"):
        assert slash._local(platform) is False, platform
        for words in ("check", "demo", "ask mini: say hello", "requests"):
            assert slash.handle(words, platform=platform).startswith(REFUSED), (words, platform)
    assert slash.handle("ask mini: say hello").startswith(REFUSED)  # (no platform given at all)
    for platform in ("cli", "tui", "desktop", "local", " CLI "):
        assert slash._local(platform) is True, platform
    # in a chat (the transcript search tool): Hermes' own module is missing -> refused, never local
    monkeypatch.setitem(sys.modules, "gateway", None)
    monkeypatch.setitem(sys.modules, "gateway.session_context", None)
    assert config.chat_not_local() is True
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "")  # (the env var is no longer a way around it)
    assert config.chat_not_local() is True
    monkeypatch.delenv("HERMES_SESSION_PLATFORM")
    # ... and with it: a chat that names no origin, or one that is not local, is refused
    for values, remote in (({}, True), ({"HERMES_SESSION_PLATFORM": "", "HERMES_SESSION_SOURCE": ""}, True),
                           ({"HERMES_SESSION_SOURCE": "something-new"}, True),
                           ({"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_SOURCE": "desktop"}, True),
                           ({"HERMES_SESSION_SOURCE": "desktop"}, False), ({"HERMES_SESSION_SOURCE": "cli"}, False)):
        _hermes_says(monkeypatch, values)
        assert config.chat_not_local() is remote, values
    # `hermes twin ask`: the owner's own terminal only; a command any chat started is refused before anything
    _linked(home, tmp_path)
    ns = argparse.Namespace(machine="mini", text=["say", "hello"], twin=None, check=None, wait=None,
                            as_profile=None, json=True)
    for name, value in (("HERMES_SESSION_ID", "an-ordinary-chat-0005"), ("HERMES_SESSION_PLATFORM", "telegram"),
                        ("HERMES_SESSION_SOURCE", "desktop")):
        with monkeypatch.context() as mp:
            mp.setenv(name, value)
            with pytest.raises(tasks.TaskError) as exc:
                tasks_api.cmd_ask(ns)
            assert exc.value.code == "local_only", name


def test_F5_linked_mode_cannot_be_set_and_a_stored_linked_reads_as_the_default(home, tmp_path, monkeypatch):  # noqa: F811
    svc = _linked(home, tmp_path)
    assert tasks.MODES == ("here", "safe")
    with pytest.raises(ValueError):
        tasks.set_mode("linked")
    with pytest.raises(BadInput):
        svc.tasks_mode("linked")
    ns = argparse.Namespace(what="mode", id="linked", step=None, json=True)
    with pytest.raises(EngineError) as exc:
        tasks_api.cmd_requests(ns)
    assert exc.value.code == "usage"
    # a "linked" kept by an older version: the owner answers on the asked computer, as by default
    data = home / "plugin-data" / "grok-bot-twin"
    data.mkdir(parents=True, exist_ok=True)
    (data / "tasks.json").write_text(json.dumps({"on": True, "since": "2000-01-01T09:00:00Z", "mode": "linked"}),
                                     encoding="utf-8")
    assert tasks.mode() == "here"
    view = svc.tasks(refresh=False)
    assert view["mode"]["value"] == "here" and [c["value"] for c in view["mode"]["choices"]] == ["here", "safe"]
    assert "asking" not in view and "warning" not in view["mode"]
    tasks.link_dropped()
    assert tasks.mode() == "here" and tasks.accepting() is False
    # nothing on the wire answers a step from the computer that asked any more
    assert tasks_hub.ROUTES == ("/v1/task/ask", "/v1/task/wait", "/v1/task/poll", "/v1/task/update")
    for name in ("answer", "answered"):
        assert not hasattr(tasks_hub.Hub, name), name
    for name in ("decide_remote", "set_asking", "asking", "log_answered", "MODE_WARNING"):
        assert not hasattr(tasks, name), name
    # the page's route refuses it too
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_f5_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/p")
    c = TestClient(app)
    assert c.post("/p/tasks/mode", json={"mode": "linked"}).status_code == 400
    assert c.post("/p/tasks/k-00000000000000c7/answer", json={"choice": "once", "q": "0a0b0c0d"}).status_code \
        in (404, 405)
    assert tasks.mode() == "here"


def test_F5_a_command_a_chat_started_neither_answers_a_step_nor_changes_the_mode(home, tmp_path, monkeypatch):  # noqa: F811
    """The terminal answer and mode commands refuse whenever the command carries a chat's marks (the same check as
    `hermes twin ask`), not only when Hermes' own chat numbers are loaded; the Twins page (own_page) still works."""
    svc = _linked(home, tmp_path)
    monkeypatch.setenv(config.SURFACE_VARS[0], "cli")
    with pytest.raises(tasks.TaskError) as exc:
        svc.tasks_mode("safe")
    assert exc.value.code == "answer_owner_only" and tasks.mode() == "here"
    with pytest.raises(tasks.TaskError) as exc:
        svc.tasks_approve("t-0000000000000000", "deny")
    assert exc.value.code == "answer_owner_only"
    assert svc.tasks_mode("safe", own_page=True)["mode"]["value"] == "safe"
    monkeypatch.delenv(config.SURFACE_VARS[0])
    assert svc.tasks_mode("here")["mode"]["value"] == "here"


def test_F5_an_instruction_line_too_long_to_check_is_held_not_passed():
    from hermes_plugins.grok_bot_twin.twin import xs_config
    long_line = "Answer politely. " * 300  # over 4,000 characters on one line
    assert xs_config.clean_persona("Be brief.\n" + long_line).split("\n")[1] != long_line
