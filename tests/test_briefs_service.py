"""Session briefs through the service, the "What syncs" panel hook, the CLI and the page's routes (the routes are
skipped where FastAPI is not installed). Invented bots, year-2000 dates."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import types

import pytest

from twin_testlib import ROOT, central, config, home, service, timer  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import briefs as br
from test_briefs import PLAN_CHAT, Recorder, add_chat, make_db, FAKE_KEY

T0 = 946717200.0
PID = "acme-scout"


@pytest.fixture
def setup(home, tmp_path, monkeypatch):  # noqa: F811
    config.set_override({"botport_path": str(tmp_path / "no-botport-here")})
    data = central(home)
    data.mkdir(parents=True, exist_ok=True)
    (data / "pairs.json").write_text(json.dumps({"pairs": [{
        "pair_id": PID, "grok": {"id": "b-1", "name": "Test Scout", "description": "Finds trails."},
        "hermes": {"profile": "test-scout"}, "created_at": "2000-01-01T09:00:00Z"}]}), encoding="utf-8")
    bot = home / "profiles" / "test-scout"
    con = make_db(bot)
    add_chat(con, "s1", PLAN_CHAT + [("user", f"Also my key is {FAKE_KEY}, we decided to keep it.")], start=T0)
    con.close()
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    monkeypatch.setattr(br, "_panel", lambda: None)  # this module's own switches; the panel tests set their own
    rec = Recorder()
    br.set_transport(rec)
    service.reset_service()
    yield service.get_service(), rec, bot
    br.set_transport(None)


def _since_before_the_chat(monkeypatch):
    monkeypatch.setattr(br.time, "time", lambda: T0 - 10)


def test_service_off_by_default_then_on(setup, monkeypatch):
    svc, rec, _ = setup
    info = svc.briefs_info()
    assert [t["id"] for t in info["twins"]] == [PID] and info["twins"][0]["direction"] == "off"
    assert svc.briefs_now()["checked"] == 0 and rec.sent == []
    with monkeypatch.context() as m:
        _since_before_the_chat(m)
        svc.briefs_set(PID, "hermes_to_grok")
    res = svc.briefs_now(PID)
    assert res["results"][0]["made"] == 1 and res["results"][0]["sent"] == 1
    assert FAKE_KEY not in rec.sent[0][2] and "Test Scout (this twin)" in rec.sent[0][2]
    info = svc.briefs_info(PID)["twins"][0]
    assert info["direction"] == "hermes_to_grok" and info["count"]["out"] == 1 and info["last_out"]
    with pytest.raises(br.BriefError):
        svc.briefs_set(PID, "sideways")
    with pytest.raises(br.BriefError):
        svc.briefs_info("no-such-twin")


def test_tick_runs_only_when_something_is_on_and_not_too_often(setup, monkeypatch):
    svc, rec, _ = setup
    calls = []
    monkeypatch.setattr(type(svc), "briefs_now", lambda self, pid=None: calls.append(pid) or {})
    svc.briefs_tick(now=1000.0)
    assert calls == []  # everything off: nothing read
    br.set_direction(PID, "both")
    svc._briefs_last_run = 0.0
    svc.briefs_tick(now=1000.0)
    svc.briefs_tick(now=1100.0)  # within RUN_EVERY_S
    assert calls == [None]


def test_forget_through_the_service(setup, monkeypatch):
    svc, rec, _ = setup
    with monkeypatch.context() as m:
        _since_before_the_chat(m)
        svc.briefs_set(PID, "both")
    svc.briefs_now(PID)
    out = svc.briefs_forget(PID)
    assert out["forgot"]["deleted"] == 1 and out["twins"][0]["direction"] == "off"


def test_panel_is_the_switch_when_the_build_has_it(setup, monkeypatch):
    """A stand-in for the panel's sync_panel: wanted() is the owner's choice; provide() gets this row."""
    svc, rec, _ = setup
    chosen, provided, marked = {}, {}, []
    fake = types.ModuleType(br.__name__.rsplit(".", 1)[0] + ".sync_panel")
    fake.wanted = lambda pid, key: chosen.get((pid, key))
    fake.provide = lambda key, **kw: provided.update({key: kw})
    fake.mark_synced = lambda pid, key, way: marked.append((pid, key, way))

    def choose(s, pid, key, direction):
        dirs = provided[key]["directions"](types.SimpleNamespace(pair_id=pid))
        if dirs[direction] != "ok":
            raise br.EngineError("not_available", "Needs the Mirror")
        provided[key]["on_change"](types.SimpleNamespace(pair_id=pid), direction)
        chosen[(pid, key)] = direction

    fake.choose = choose
    monkeypatch.setattr(br, "_panel", lambda: fake)
    assert br.register_panel() and "session_briefs" in provided
    assert provided["session_briefs"]["default"](None) == "off"
    ctx = types.SimpleNamespace(pair_id=PID)
    assert provided["session_briefs"]["directions"](ctx) == {"grok_to_hermes": "ok", "hermes_to_grok": "ok",
                                                              "both": "ok", "off": "ok"}
    br.set_transport(None)
    assert provided["session_briefs"]["directions"](ctx)["both"] == "needs_mirror"
    with pytest.raises(br.EngineError):
        svc.briefs_set(PID, "both")
    br.set_transport(rec)
    with monkeypatch.context() as m:
        _since_before_the_chat(m)
        br.set_direction(PID, "both")       # this module's own file says on ...
        assert br.effective(PID) == (False, False)  # ... but the panel says nothing: off
        svc.briefs_set(PID, "hermes_to_grok")
    assert chosen[(PID, "session_briefs")] == "hermes_to_grok" and br.effective(PID) == (True, False)
    svc.briefs_now(PID)
    assert marked == [(PID, "session_briefs", "hermes_to_grok")]
    assert provided["session_briefs"]["last_synced"](ctx)[1] == "hermes_to_grok"
    svc.briefs_forget(PID)  # Forget turns the panel's switch off too
    assert chosen[(PID, "session_briefs")] == "off" and br.effective(PID) == (False, False)


def test_turned_on_in_the_panel_never_briefs_the_past(setup, monkeypatch):
    svc, rec, _ = setup
    fake = types.ModuleType(br.__name__.rsplit(".", 1)[0] + ".sync_panel")
    fake.wanted = lambda pid, key: "both"
    monkeypatch.setattr(br, "_panel", lambda: fake)
    assert svc.briefs_now(PID)["results"][0]["made"] == 0  # the chat ended in 2000, before "now"


def test_mem_layers_transport_adapter(setup):
    sent, inbound = [], [{"id": "g1", "title": "t", "markdown": br.HEADER + "\n# Soup\n\n## Decided\n- Soup.\n"},
                         {"id": "g2", "markdown": 7}]

    class T:
        def write_brief(self, brief):
            sent.append(brief)
            return True

        def read_briefs(self):
            return inbound

    layers = types.SimpleNamespace(mirror_ready=lambda: True, transport=lambda profile: T() if profile else None)
    t = br.LayersTransport(layers, lambda pid: "test-scout" if pid == PID else None)
    assert t.state(PID) == "ok" and t.state("other") == "needs_mirror"
    t.send(PID, "b-0123456789abcdef", br.HEADER + "\n# Title here\n")
    assert sent[0]["id"] == "b-0123456789abcdef" and sent[0]["title"] == "Title here"
    assert t.fetch(PID) == [("g1", inbound[0]["markdown"])]
    layers.mirror_ready = lambda: False
    with pytest.raises(br.TransportUnavailable):
        t.send(PID, "b-0123456789abcdef", "x")
    assert t.fetch(PID) == []


def test_cli_briefs(setup, monkeypatch, capsys):
    from hermes_plugins.grok_bot_twin.twin import cli
    svc, rec, _ = setup
    parser = __import__("argparse").ArgumentParser()
    cli.setup(parser)
    a = parser.parse_args(["briefs"])
    assert cli.cmd_briefs(a) == 0
    out = capsys.readouterr().out
    assert "Session briefs for Test Scout: off" in out
    with monkeypatch.context() as m:
        _since_before_the_chat(m)
        assert cli.cmd_briefs(parser.parse_args(["briefs", "--way", "to-grok", "--now"])) == 0
    out = capsys.readouterr().out
    assert "Hermes → Grok" in out and "Made 1 brief of finished Hermes chats. Handed 1 brief to the Grok bot." in out
    assert cli.cmd_briefs(parser.parse_args(["briefs", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["twins"][0]["direction"] == "hermes_to_grok"


# ---- the page's routes -------------------------------------------------------------------------------------------
def test_rest_routes(setup, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_briefs_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    P = "/api/plugins/grok-bot-twin"
    app.include_router(api.router, prefix=P)
    c = TestClient(app)
    assert c.get(P + "/briefs").json()["twins"][0]["direction"] == "off"
    assert c.post(P + "/briefs", json={"twin": PID, "direction": "up"}).status_code == 400
    assert c.post(P + "/briefs", json=["x"]).status_code == 400
    with monkeypatch.context() as m:
        _since_before_the_chat(m)
        r = c.post(P + "/briefs", json={"twin": PID, "direction": "both"}).json()
    assert r["twins"][0]["direction"] == "both"
    r = c.post(P + "/briefs/now", json={"twin": PID}).json()
    assert r["results"][0]["sent"] == 1 and FAKE_KEY not in json.dumps(r)
    assert "error" in c.post(P + "/briefs", json={"twin": "nope", "direction": "off"}).json()
    r = c.post(P + "/briefs/forget", json={"twin": PID}).json()
    assert r["forgot"]["deleted"] == 1


# ---- regressions ------------------------------------------------------------------------------
def _parser():
    from hermes_plugins.grok_bot_twin.twin import cli
    parser = __import__("argparse").ArgumentParser()
    cli.setup(parser)
    return cli, parser


def test_cli_a_request_from_another_computer_cannot_change_briefs(setup, monkeypatch):
    from hermes_plugins.grok_bot_twin.twin import tasks
    cli, parser = _parser()
    svc, rec, _ = setup
    br.set_direction(PID, "both")
    monkeypatch.setattr(tasks, "asked_turn", lambda *a, **k: True)
    for argv in (["briefs", "--way", "off"], ["briefs", "--forget", "--yes"], ["briefs", "--now"], ["briefs"]):
        with pytest.raises(br.EngineError) as e:
            cli.cmd_briefs(parser.parse_args(argv))
        assert e.value.message == "A request from another computer cannot read or change session briefs."
    assert br.direction_of(PID) == "both"


def test_cli_plain_sentences_and_forget_asks_first(setup, monkeypatch, capsys):
    cli, parser = _parser()
    svc, rec, _ = setup
    assert cli.cmd_briefs(parser.parse_args(["briefs", "--now"])) == 0
    out = capsys.readouterr().out
    assert "Briefs are off for this twin, so nothing was made." in out
    assert "Session briefs are off for this twin." in out and "Turn on: hermes twin briefs" in out
    with monkeypatch.context() as m:
        _since_before_the_chat(m)
        svc.briefs_set(PID, "hermes_to_grok")
    svc.briefs_now(PID)
    capsys.readouterr()
    assert cli.cmd_briefs(parser.parse_args(["briefs", "--forget"])) == 1
    out = capsys.readouterr().out
    assert ("This deletes the 1 brief kept for Test Scout and turns their briefs off. It cannot be undone. "
            "Run again with --yes.") in out
    assert len(br.stored(PID, "out")) == 1 and br.direction_of(PID) == "hermes_to_grok"
    assert cli.cmd_briefs(parser.parse_args(["briefs", "--forget", "--yes"])) == 0
    assert "Deleted 1 brief kept for this twin." in capsys.readouterr().out
    assert br.stored(PID, "out") == [] and br.direction_of(PID) == "off"


def test_one_twins_trouble_does_not_stop_the_others(setup, monkeypatch):
    svc, rec, _ = setup
    two = [{"pair_id": "acme-broken", "name": "Broken", "bot": "b-2", "profile": None},
           {"pair_id": PID, "name": "Test Scout", "bot": "b-1", "profile": "test-scout"}]
    monkeypatch.setattr(type(svc), "_briefs_twins", lambda self: two)
    br.set_direction("acme-broken", "both")
    with monkeypatch.context() as m:
        _since_before_the_chat(m)
        br.set_direction(PID, "hermes_to_grok")
    real = br.run

    def run(twin, *a, **k):
        if twin == "acme-broken":
            raise RuntimeError("invented failure")
        return real(twin, *a, **k)
    monkeypatch.setattr(br, "run", run)
    res = svc.briefs_now()
    assert [r.get("failed", False) for r in res["results"]] == [True, False]
    assert res["results"][1]["sent"] == 1


def _fake_panel(monkeypatch, chosen, refuse=False):
    fake = types.ModuleType(br.__name__.rsplit(".", 1)[0] + ".sync_panel")
    fake.wanted = lambda pid, key: chosen.get((pid, key))
    fake.provide = lambda key, **kw: None
    fake.mark_synced = lambda pid, key, way: None

    def choose(s, pid, key, direction):
        if refuse:
            raise br.EngineError("held", "The panel file is busy.")
        br.set_direction(pid, direction)
        chosen[(pid, key)] = direction
    fake.choose = choose
    monkeypatch.setattr(br, "_panel", lambda: fake)
    return fake


def test_a_saved_panel_choice_counts_only_while_the_row_can_have_it(setup, monkeypatch, capsys):
    svc, rec, _ = setup
    chosen = {(PID, "session_briefs"): "both"}
    _fake_panel(monkeypatch, chosen)
    assert br.effective(PID) == (True, True)  # the transport says ok
    br.set_transport(None)                    # the default: the Mirror is not set up
    assert br.effective(PID) == (False, False)
    assert svc.briefs_now(PID)["results"] == []
    t = svc.briefs_info(PID)["twins"][0]
    assert t["direction"] == "off" and t["can_turn_on"] is False
    cli, parser = _parser()
    assert cli.cmd_briefs(parser.parse_args(["briefs"])) == 0
    out = capsys.readouterr().out
    assert "Briefs can be turned on once the Mirror is set up." in out and "Turn on:" not in out
    svc.briefs_forget(PID)  # a choice the row cannot have now still goes off: it would come back with the Mirror
    assert chosen[(PID, "session_briefs")] == "off"


def test_forget_says_when_the_panel_switch_could_not_go_off(setup, monkeypatch):
    svc, rec, _ = setup
    with monkeypatch.context() as m:
        _since_before_the_chat(m)
        br.set_direction(PID, "hermes_to_grok")
        br.run(PID, "Test Scout", setup[2], now=T0 + 10 * 3600)
    assert len(br.stored(PID, "out")) == 1
    _fake_panel(monkeypatch, {(PID, "session_briefs"): "hermes_to_grok"}, refuse=True)
    out = svc.briefs_forget(PID)["forgot"]
    assert out["deleted"] == 1 and out["switch_off"] is False
    assert out["said"] == ("The briefs were deleted, but their switch in What syncs could not be turned off: "
                           "The panel file is busy. Turn Session briefs off on the Twins page.")


def test_panel_texts(setup):
    ctx = types.SimpleNamespace(pair_id=PID)
    assert br._panel_sentence(ctx, "grok_to_hermes") == ("Briefs of the Grok bot's chats are checked and kept on "
                                                         "this computer. Read them with hermes twin briefs --show.")
    assert br.PANEL_NOTE["needs_mirror"] == "Needs the Mirror. Until it is set up, briefs stay off."
