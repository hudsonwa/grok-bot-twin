"""C2 F8: "Let Hermes do the Grok part" (computer use clicking in the Grok app) is removed, not guarded.

Its pre_tool_call guard failed open (after its clock ran out, or with no flow file, a chat given clicking rights fell
back to plain approvals). With the flow gone the plugin has no say over computer_use at all, so there is nothing to
fail open; the plain path (paste the request in Grok, import the answer files) stays.
"""
from __future__ import annotations

import json

from twin_testlib import ROOT, home, plugin, service  # noqa: F401
from test_ux_flows import _client, _pair, fake  # noqa: F401
from test_setup_download_and_twin_facts import SOUL, profile, twin  # noqa: F401
from test_batch import P, Hermes, bot, export


class Recorder:
    profile_name = "default"
    plugin_id = "grok-bot-twin"

    def __init__(self):
        self.hooks, self.skills = [], []

    def register_hook(self, name, cb, **kw):
        self.hooks.append((name, cb))

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append(name)

    def __getattr__(self, name):
        if name.startswith("register_") or name == "on_unload":
            return lambda *a, **k: None
        raise AttributeError(name)


def test_F8_register_has_no_pre_tool_call_hook_and_one_skill(home):
    ctx = Recorder()
    plugin.register(ctx)
    names = [n for n, _ in ctx.hooks]
    assert "pre_tool_call" not in names
    assert sorted(names) == ["on_session_end", "on_session_start", "pre_llm_call"]
    assert ctx.skills == ["set-up-grok-bot-twin"]
    assert not (ROOT / "skills" / "finish-twins-in-grok").exists()
    assert not (ROOT / "twin" / "grok_part.py").exists()
    assert not hasattr(plugin, "_on_pre_tool_call") and not hasattr(plugin, "GROK_PART_SKILL")


def test_F8_a_computer_use_call_is_not_intercepted(home):
    """Even with a leftover 0.3.3 flow file on disk (expired or not), no hook of this plugin sees computer_use."""
    flow = home / "plugin-data" / "grok-bot-twin" / "grok-part.json"
    flow.parent.mkdir(parents=True, exist_ok=True)
    flow.write_text(json.dumps({"pair_ids": ["test-scout"], "names": ["Test Scout"], "request_sha256": "0" * 64,
                                "until": 0, "flow": "f8"}))
    ctx = Recorder()
    plugin.register(ctx)
    for name, cb in ctx.hooks:
        assert name != "pre_tool_call", name
    assert flow.exists()  # nothing reads it any more; uninstall empties the whole data folder


def test_F8_no_grok_part_route(monkeypatch):
    c = _client(monkeypatch)
    paths = [getattr(r, "path", "") for r in c.app.routes]
    assert paths and not [p for p in paths if "grok-part" in p or "grok_part" in p or "computer-use" in p]
    assert c.post(f"{P}/batch/grok-part", json={"stop": True}).status_code in (404, 405)


def test_F8_the_plain_import_path_still_works(twin, home, monkeypatch):
    Hermes(home, monkeypatch)
    c = _client(monkeypatch)
    pid = "acme-support"
    info = c.get(f"{P}/batch?pair_ids={pid}").json()
    assert "computer_use" not in info and info["request"]
    files = [{"name": "acme.json", "text": export(bot("Acme Support"))}]
    got = c.post(f"{P}/batch/import", json={"files": files, "pair_ids": [pid]}).json()["results"]
    assert got[0]["state"] == "done" and got[0]["instructions"] is True
