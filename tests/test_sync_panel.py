"""The "What syncs" panel: seven rows per twin, a direction each, honest about what
this version cannot do yet, and the way a backend part plugs in. Invented bots only, a throwaway HERMES_HOME, the
fake botport helper."""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import fleet_testlib
from twin_testlib import ROOT, config, home, service, svc  # noqa: F401
from twin_testlib import cli  # noqa: E402
from hermes_plugins.grok_bot_twin.twin import sync_panel as sp  # noqa: E402  (the copy the plugin itself uses)
from hermes_plugins.grok_bot_twin.twin.engine import EngineError  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_botport.py"
fleet_testlib._SHAPES.update(json.loads((ROOT / "tests" / "desktop" / "syncs_shapes.json").read_text("utf-8"))["shapes"])
KEYS = ["name", "description", "instructions", "skills", "routines", "memory_facts", "session_briefs"]


@pytest.fixture(autouse=True)
def _fresh_providers():
    sp._reset_for_tests()
    yield
    sp._reset_for_tests()


@pytest.fixture
def bp(home, tmp_path):  # noqa: F811
    """A service with the fake botport helper, already checked: one real twin (Acme Support)."""
    if not FAKE.is_file():
        pytest.skip("fake_botport.py not present")
    bindir = tmp_path / "bin"
    bindir.mkdir(mode=0o755)
    exe = bindir / "botport"
    exe.write_text(f"#!{sys.executable} -I\n" + FAKE.read_text(encoding="utf-8"), encoding="utf-8")
    exe.chmod(0o755)
    os.chmod(tmp_path, 0o700)
    config.set_override({"botport_path": str(exe)})
    service.reset_service()
    s = service.get_service()
    s.trust_botport(True)
    s.invalidate()
    s.status(fresh=True)
    return s


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _twin(view):
    assert len(view["twins"]) >= 1
    return view["twins"][0]


def _item(twin, key):
    return next(i for i in twin["items"] if i["key"] == key)


def test_no_twins_is_an_empty_panel(svc):  # noqa: F811
    v = sp.view(svc)
    assert fleet_testlib.shape_problems(v, "SyncPanel") == []
    assert v["twins"] == [] and v["mirror"]["state"] == "unknown"


def test_the_sample_twin_has_no_panel(svc):  # noqa: F811
    run(["demo"])
    assert sp.view(svc)["twins"] == []


def test_the_rows_say_what_this_version_does(bp):
    v = sp.view(bp)
    assert fleet_testlib.shape_problems(v, "SyncPanel") == []
    t = _twin(v)
    assert t["name"] == "Acme Support" and t["mirror"] == "not_set_up"
    assert [i["key"] for i in t["items"]] == KEYS
    assert [i["label"] for i in t["items"]][-1] == "Session briefs"
    for it in t["items"]:
        assert [c["direction"] for c in it["choices"]] == list(sp.DIRECTIONS)
        assert it["sentence"] and all(c["sentence"] for c in it["choices"])
        assert all(("why" in c) == (c["state"] != "ok") for c in it["choices"])
        assert it["private"] is (it["key"] in ("memory_facts", "session_briefs"))
    # name: follows Grok; the way back needs the Mirror
    n = _item(t, "name")
    assert n["direction"] == "grok_to_hermes" and n["state"] == "needs_mirror"
    assert {c["direction"]: c["state"] for c in n["choices"]} == {
        "grok_to_hermes": "ok", "hermes_to_grok": "needs_mirror", "both": "needs_mirror", "off": "not_yet"}
    assert n["last_synced_at"] and n["last_direction"] == "grok_to_hermes"  # when the twin was made
    # instructions: nothing imported yet, so never synced, and it says how they come over today
    ins = _item(t, "instructions")
    assert ins["last_synced_at"] is None and "import the setup export" in ins["note"]
    assert "each time you import the setup export" in ins["sentence"]
    # skills without skill sync: off, and every way of syncing them needs the Mirror
    sk = _item(t, "skills")
    assert sk["direction"] == "off" and sk["state"] == "needs_mirror"
    # routines and the two private rows: off, not in this version yet
    for key in ("routines", "memory_facts", "session_briefs"):
        it = _item(t, key)
        assert it["direction"] == "off" and it["state"] == "not_yet" and it["last_synced_at"] is None
    assert "none go back to Grok" in _item(t, "session_briefs")["choices"][0]["sentence"]


def test_a_direction_that_cannot_work_is_refused_and_nothing_is_saved(bp):
    pid = _twin(sp.view(bp))["id"]
    with pytest.raises(EngineError) as e:
        sp.choose(bp, pid, "memory_facts", "both")
    assert e.value.code == "not_available" and e.value.message == sp.WHY["not_yet"]
    with pytest.raises(EngineError) as e:
        sp.choose(bp, pid, "name", "hermes_to_grok")
    assert e.value.code == "not_available" and "Mirror" in e.value.message
    assert sp.wanted(pid, "memory_facts") is None and not (bp.central / sp.FILE).exists()
    for bad in (("nope", "off"), ("name", "sideways"), (None, "off"), ("name", 3)):
        with pytest.raises(EngineError) as e:
            sp.choose(bp, pid, *bad)
        assert e.value.code == "bad_input"
    with pytest.raises(EngineError) as e:
        sp.choose(bp, "not-a-twin", "name", "off")
    assert e.value.code == "no_twin"
    # the direction already in effect: nothing changed, nothing to refuse
    same = sp.choose(bp, pid, "memory_facts", "off")
    assert same["said"]["sentence"] == "Nothing changed: Memory facts for Acme Support was already set to Off." and fleet_testlib.shape_problems(same, "SyncPanel") == []


def test_a_backend_part_plugs_in(bp):
    pid = _twin(sp.view(bp))["id"]
    seen = []
    sp.provide("memory_facts", directions=lambda c: {"grok_to_hermes": "ok", "hermes_to_grok": "ok", "both": "ok",
                                                     "off": "ok"},
               on_change=lambda c, d: seen.append((c.pair_id, d)), note=lambda c: "Two facts wait for you.")
    out = sp.choose(bp, pid, "memory_facts", "both")
    assert seen == [(pid, "both")] and sp.wanted(pid, "memory_facts") == "both"
    it = _item(_twin(out), "memory_facts")
    assert it["direction"] == "both" and it["state"] == "ok" and it["note"] == "Two facts wait for you."
    assert "Memory facts for Acme Support: Both ways." in out["said"]["sentence"]
    saved = json.loads((bp.central / sp.FILE).read_text("utf-8"))
    assert saved["twins"][pid]["memory_facts"]["direction"] == "both"
    # a part says it moved something
    sp.mark_synced(pid, "memory_facts", "hermes_to_grok", "2000-01-01T09:00:00Z")
    it = _item(_twin(sp.view(bp)), "memory_facts")
    assert it["last_synced_at"] == "2000-01-01T09:00:00Z" and it["last_direction"] == "hermes_to_grok"
    with pytest.raises(ValueError):
        sp.mark_synced(pid, "memory_facts", "both")
    # a part that refuses: its words, and nothing saved
    def refuse(c, d):
        raise EngineError("held", "A conflict waits: settle it first.")
    sp.provide("memory_facts", directions=lambda c: {d: "ok" for d in sp.DIRECTIONS}, on_change=refuse)
    with pytest.raises(EngineError) as e:
        sp.choose(bp, pid, "memory_facts", "off")
    assert e.value.message == "A conflict waits: settle it first." and sp.wanted(pid, "memory_facts") == "both"
    # a part that goes away: its saved choice is no longer shown as in effect
    sp._reset_for_tests()
    assert _item(_twin(sp.view(bp)), "memory_facts")["direction"] == "off"
    with pytest.raises(ValueError):
        sp.provide("passwords", directions=lambda c: {})
    # the twin goes: so do its choices
    bp.remove(pid)
    assert pid not in json.loads((bp.central / sp.FILE).read_text("utf-8"))["twins"]


def test_with_the_mirror_ready_nothing_says_it_needs_the_mirror(bp):
    sp.set_mirror_probe(lambda pid: "ready")
    v = sp.view(bp)
    t = _twin(v)
    assert v["mirror"]["state"] == "ready" and t["mirror"] == "ready"
    assert all(c["state"] != "needs_mirror" for it in t["items"] for c in it["choices"])
    sp.set_mirror_probe(lambda pid: "no_permission")
    assert sp.view(bp)["mirror"]["state"] == "no_permission"
    sp.set_mirror_probe(lambda pid: 1 / 0)  # a probe that breaks never breaks the panel
    assert sp.view(bp)["twins"][0]["mirror"] == "not_set_up"


def test_skill_sync_connected_means_skills_go_both_ways(bp, monkeypatch):
    real = bp.status

    def status(*a, **k):
        st = real(*a, **k)
        for p in st.get("pairs", []):
            p["mirror_bot"] = {"connected": True}
        return st
    monkeypatch.setattr(bp, "status", status)
    t = _twin(sp.view(bp))
    sk = _item(t, "skills")
    assert t["mirror"] == "ready" and sk["direction"] == "both" and "Send to Grok" in sk["note"]


def test_the_terminal(bp):
    rc, out, err = run(["syncs"])
    assert rc == 0 and err == "", err
    lines = out.splitlines()
    assert lines[0] == "What syncs for Acme Support"
    for label in ("Name", "Description", "Instructions", "Skills", "Routines", "Memory facts", "Session briefs"):
        assert any(line.startswith(f"  {label} ") for line in lines), label
    assert "Grok → Hermes" in out and "Not in this version yet" in out and "Needs the Mirror" in out
    assert "not synced yet" in out and "nothing private moves without your choice" in out
    assert "  Routines        Off" in out
    name_off = _item(_twin(sp.view(bp)), "name")["choices"][3]["sentence"]
    assert name_off == "The name stays as it is on each side."
    rc, out, err = run(["syncs", "--json"])
    assert rc == 0 and fleet_testlib.shape_problems(json.loads(out), "SyncPanel") == []
    rc, out, err = run(["syncs", "memory", "from-grok"])
    assert rc == 1 and "Not in this version yet." in err
    rc, out, err = run(["syncs", "Acme Support", "briefs", "off"])
    assert rc == 0 and "Nothing changed" in out
    rc, out, err = run(["syncs", "Acme Support", "passwords", "off"])
    assert rc == 2 and "is not an item" in err
    rc, out, err = run(["syncs", "Nobody Here"])
    assert rc == 2 and "No twin is called" in err
    rc, out, err = run(["syncs", "a", "b", "c", "d"])
    assert rc == 2
    assert "syncs [TWIN]" in cli.HELP_TEXT


def test_the_local_routes(bp, monkeypatch):
    pytest.importorskip("fastapi")
    import importlib.util
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from twin_testlib import timer
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_syncs", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    app = FastAPI()
    P = "/api/plugins/grok-bot-twin"
    app.include_router(api.router, prefix=P)
    c = TestClient(app)
    r = c.get(P + "/sync/panel")
    assert r.status_code == 200 and fleet_testlib.shape_problems(r.json(), "SyncPanel") == []
    pid = r.json()["twins"][0]["id"]
    r = c.post(P + "/sync/panel", json={"twin": pid, "item": "routines", "direction": "both"})
    assert r.status_code == 200 and r.json()["error"]["code"] == "not_available"
    r = c.post(P + "/sync/panel", json={"twin": pid, "item": "routines", "direction": "off"})
    assert r.status_code == 200 and fleet_testlib.shape_problems(r.json(), "SyncPanel") == []
    assert "Nothing changed" in r.json()["said"]["sentence"]
    for body in ({"twin": pid, "item": "name"}, {"twin": pid, "item": "Name!", "direction": "off"},
                 {"twin": "../x", "item": "name", "direction": "off"}, {"twin": 5, "item": "name", "direction": "off"}):
        assert c.post(P + "/sync/panel", json=body).status_code == 400


def test_stop_twinning_deletes_the_twins_session_briefs(bp):
    from hermes_plugins.grok_bot_twin.twin import briefs as br
    pid = _twin(sp.view(bp))["id"]
    conv = br.Conversation("s1", [br.Turn("user", "We decided on soup."), br.Turn("assistant", "Agreed, soup.")])
    br._save(pid, "out", br.compact(conv, pid, "Acme Support"))
    br.set_switches(pid, to_grok=True, from_grok=True)
    assert len(br.stored(pid, "out")) == 1
    bp.remove(pid)
    assert br.stored(pid, "out") == [] and br._read_state()["twins"][pid]["to_grok"] is False
