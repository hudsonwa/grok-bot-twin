"""The demo world end to end through TwinService (no botport, no Grok)."""
from __future__ import annotations

import pytest

from twin_testlib import central, home, service, store, svc  # noqa: F401
from hermes_plugins.grok_bot_twin.twin.engine import EngineError


def _pair(s, pid="acme-helper"):
    return next(p for p in s.status(fresh=True)["pairs"] if p["pair_id"] == pid)


def _events(s, etype):
    return [e for e in s.events()["events"] if e["type"] == etype]


def test_demo_start_in_step(svc):
    res = svc.demo_action("start")
    assert res["ok"] and res["pairs"][0]["pair_id"] == "acme-helper"
    p = _pair(svc)
    assert p["state"] == "in_step" and p["demo"] is True
    assert p["grok"]["name"] == "Acme Helper" and p["hermes"]["profile"] == "default"
    cov = {c["item"]: c for c in p["coverage"]}
    assert cov["name"]["g2h"] == "syncs" and cov["instructions"]["g2h"] == "by_hand"
    # the demo twin started from a bot export and has a (pretend) Mirror bot connected
    assert cov["routines"]["g2h"] == "by_hand" and cov["skills"]["h2g"] == "syncs_via_mirror"
    assert p["mirror_bot"] == {"connected": True}
    assert svc.demo._twin("acme-helper")["instructions"].startswith("You are Acme Helper.")
    assert svc.status()["mode"] == "demo"
    assert svc.demo_action("start")["did"] == "The demo is already running"


def test_grok_edit_check_sync_synced_event(svc):
    svc.demo_action("start")
    assert svc.check_changed() == []
    svc.demo_action("grok-edit")
    assert _pair(svc)["state"] == "changes_waiting"
    assert svc.check_changed() == ["acme-helper"]
    assert _events(svc, "changed")
    assert svc.tick() == 1  # timer cycle: check, then sync of the changed pair
    synced = _events(svc, "synced")
    assert synced and synced[-1]["pair_id"] == "acme-helper"
    assert synced[-1]["payload"]["message"] == "Acme Helper: description updated from Grok"
    assert _pair(svc)["state"] == "in_step"
    assert svc.check_changed() == []


def test_sync_now_shape(svc):
    svc.demo_action("start")
    svc.demo_action("grok-edit")
    res = svc.sync("acme-helper")
    r = res["pairs"][0]
    assert set(r) >= {"pair_id", "applied", "waiting", "conflicts", "secrets_withheld", "to_grok", "restore_point",
                      "state", "wall_ms"}
    assert r["applied"] == [{"item": "persona.description", "kind": "description", "name": "description",
                             "direction": "g2h"}]


def test_conflict_and_resolve(svc):
    svc.demo_action("start")
    svc.demo_action("conflict")
    r = svc.sync()["pairs"][0]
    assert r["conflicts"] == ["skill:greet-visitors"]
    assert _pair(svc)["state"] == "needs_you"
    assert _events(svc, "conflict")
    c = svc.conflicts("acme-helper")["conflicts"][0]
    assert c["conflict_id"] == "skill:greet-visitors"
    assert c["diff"].startswith("--- grok") and "cheerful" in c["hermes"]["text"] and "warm" in c["grok"]["text"]
    points_before = svc.restore_points("acme-helper")["points"]
    out = svc.resolve("acme-helper", c["conflict_id"], "hermes")
    assert out["applied"] == [{"item": "skill:greet-visitors", "keep": "hermes"}]
    assert out["restore_point"] is None  # Grok's copy is not Hermes' to keep: no restore point is claimed
    assert svc.restore_points("acme-helper")["points"] == points_before
    assert svc.conflicts("acme-helper")["conflicts"] == []
    g = svc.demo._grok("b-acme-0001")
    assert "warm" in g["skills"]["greet-visitors"]["text"]  # the Grok side is not touched
    p = _pair(svc)
    assert p["state"] == "changes_waiting" and p["pending"]["to_grok"] == 1
    # with the Mirror bot connected the skill travels through it, not the by-hand list; without, by hand
    assert not any(b["side"] == "grok" for b in p["by_hand"])
    svc.mirror("acme-helper", None, True)
    assert any(b["side"] == "grok" and b["item"] == "skill:greet-visitors" for b in _pair(svc)["by_hand"])
    with pytest.raises(service.BadInput):
        svc.resolve("acme-helper", "skill:x", "both")


def test_keep_grok_then_restore_puts_hermes_copy_back(svc):
    svc.demo_action("start")
    svc.demo_action("conflict")
    svc.sync()
    out = svc.resolve("acme-helper", "skill:greet-visitors", "grok")
    rp = out["restore_point"]
    assert rp and rp.startswith("skill:greet-visitors@") and len(rp.split("@")[1]) == 12
    assert "warm" in svc.demo._twin("acme-helper")["skills"]["greet-visitors"]["text"]
    assert _pair(svc)["state"] == "in_step"
    pts = svc.restore_points("acme-helper")["points"]
    assert pts[0]["id"] == rp and pts[0]["item"] == "skill:greet-visitors" and pts[0]["name"] == "greet-visitors"
    res = svc.restore("acme-helper", rp)
    assert res["restored"] == ["skill:greet-visitors"]
    assert "cheerful" in svc.demo._twin("acme-helper")["skills"]["greet-visitors"]["text"]  # visible change
    assert res["restore_point"] and res["restore_point"].startswith("skill:greet-visitors@")  # undo point
    p = _pair(svc)  # honest: Hermes now differs from Grok, waiting for the owner's OK to go to Grok
    assert p["state"] == "needs_you" and p["waiting"][0]["item"] == "skill:greet-visitors"
    assert svc.check_changed() == []  # the restore is not undone by the next tick
    again = svc.restore("acme-helper", rp)
    assert again["restored"] == [] and "already has that copy" in again["note"]
    with pytest.raises(service.BadInput):
        svc.restore("acme-helper", "../../etc")
    with pytest.raises(EngineError):
        svc.restore("acme-helper", "skill:greet-visitors@000000000000")


def test_restore_after_sync_keeps_secret_state(svc):
    svc.demo_action("start")
    before = svc.demo._twin("acme-helper")["description"]
    svc.demo_action("grok-edit")
    svc.sync()
    assert svc.demo._twin("acme-helper")["description"] != before
    pts = svc.restore_points("acme-helper")["points"]
    assert pts[0]["item"] == "persona.description" and pts[0]["scope"] == "item"
    svc.demo_action("secret")
    svc.sync()
    assert _pair(svc)["state"] == "needs_you" and _pair(svc)["secrets_held"] == 1
    res = svc.restore("acme-helper", pts[0]["id"])
    assert res["restored"] == ["persona.description"]
    assert svc.demo._twin("acme-helper")["description"] == before
    p = _pair(svc)
    assert p["secrets_held"] == 1 and p["state"] == "needs_you"  # not silently cleared


def test_hermes_edit_approve_send(svc):
    svc.demo_action("start")
    svc.demo_action("hermes-edit")
    p = _pair(svc)
    assert p["state"] == "needs_you" and p["waiting"][0]["item"] == "skill:fun-fact"
    out = svc.approve("acme-helper", "skill:fun-fact")
    assert out["approved"] == [{"item": "skill:fun-fact", "to": "remote"}]
    p = _pair(svc)
    assert p["state"] == "changes_waiting" and p["pending"]["to_grok"] == 1
    assert not any(b["side"] == "grok" for b in p["by_hand"])  # the (pretend) Mirror bot carries it
    sent = svc.send_to_grok("acme-helper")
    assert sent["skills"] == 1 and sent["digest_entries"] == 0
    assert _pair(svc)["state"] == "in_step"
    with pytest.raises(EngineError):
        svc.approve("acme-helper", "skill:fun-fact")  # nothing waiting any more


def test_mirror_and_remove(svc, tmp_path):
    svc.demo_action("start")
    acts = len(svc.demo._twin("acme-helper")["activity"])
    on = svc.mirror("acme-helper")  # the demo needs no pair folder; already connected: nothing changes
    assert len(svc.demo._twin("acme-helper")["activity"]) == acts
    assert on["connected"] is True and [s["id"] for s in on["steps"]] == ["mirror_bot", "share_folder", "add_pair",
                                                                          "test"]
    p = _pair(svc)
    assert p["mirror_bot"] == {"connected": True}
    assert {c["item"]: c for c in p["coverage"]}["skills"]["h2g"] == "syncs_via_mirror"
    assert svc.mirror("acme-helper", None, True)["connected"] is False
    with pytest.raises(service.BadInput):
        svc.mirror("acme-helper", "relative/dir", True)
    dry = svc.remove("acme-helper", True)
    assert dry["profile_kept"] is True and dry["removed"]
    assert "acme-helper" in svc.pair_ids()
    svc.remove("acme-helper", False)
    assert "acme-helper" not in svc.pair_ids()
    with pytest.raises(service.PairNotFound):
        svc.conflicts("acme-helper")


def test_pair_folder_checks(svc, home, tmp_path):
    with pytest.raises(service.BadInput):
        svc._check_pair_folder("relative")
    with pytest.raises(service.BadInput):
        svc._check_pair_folder(str(tmp_path / "missing"))
    inside = home / "x"
    inside.mkdir()
    with pytest.raises(service.BadInput):
        svc._check_pair_folder(str(inside))
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    with pytest.raises(service.BadInput):
        svc._check_pair_folder(str(link))
    ok = tmp_path / "pair-folder"
    ok.mkdir()
    assert svc._check_pair_folder(str(ok)) == str(ok.resolve())


def test_add_twin_preview_then_create(svc):
    svc.demo_action("start")
    bots = svc.grok_list()["bots"]
    assert {b["name"] for b in bots} == {"Acme Helper", "Recipe Planner"}
    recipe = next(b for b in bots if b["name"] == "Recipe Planner")
    assert recipe["twinned_pair_id"] is None
    prev = svc.preview([recipe["grok_id"]])
    items = prev["pairs"][0]["items"]
    assert any(i["kind"] == "skill" for i in items) and prev["pairs"][0]["schedules_paused"] == 1
    assert "recipe-planner" not in svc.pair_ids()  # preview writes no twin
    made = svc.create([recipe["grok_id"]])
    assert [p["pair_id"] for p in made["pairs"]] == ["recipe-planner"]
    with pytest.raises(service.BadInput):
        svc.preview([])
    with pytest.raises(service.BadInput):
        svc.preview(["../x"])


def test_digest_off_by_default(svc):
    svc.demo_action("start")
    assert _pair(svc)["digest"] == {"enabled": False, "titles": False, "allowed": False}
    with pytest.raises(service.BadInput):
        svc.set_digest("acme-helper", True)
    prev = svc.digest_preview("acme-helper")
    assert prev["enabled"] is False
    assert all(set(e) == {"at", "side", "kind"} for e in prev["entries"])  # counts + times only


def test_digest_opt_in(svc):
    from twin_testlib import config
    config.set_override({"botport_path": "/nonexistent-botport", "digest_to_grok": True, "digest_titles": True})
    svc.demo_action("start")
    out = svc.set_digest("acme-helper", True, False)
    assert out["enabled"] is True and out["titles"] is False
    assert all("title" not in e for e in out["preview"]["entries"])
    out = svc.set_digest("acme-helper", True, True)
    assert all("title" in e for e in out["preview"]["entries"])
    assert svc.send_to_grok("acme-helper")["digest_entries"] > 0


def test_demo_remove(svc, home):
    svc.demo_action("start")
    assert (central(home) / "demo").is_dir()
    assert svc.demo_action("remove")["did"] == "Demo removed"
    assert not (central(home) / "demo").exists()
    assert svc.status(fresh=True)["pairs"] == []
    with pytest.raises(EngineError):
        svc.demo_action("grok-edit")
    with pytest.raises(service.BadInput):
        svc.demo_action("explode")


def test_unknown_pair_and_bad_ids(svc):
    with pytest.raises(service.PairNotFound):
        svc.recent("nobody")
    for bad in ("../x", "", "a/b", "x" * 65, None, 3):
        with pytest.raises(service.BadInput):
            svc.require_pair(bad)


def test_grok_list_in_demo_survives_untrusted_botport(svc, monkeypatch):
    from hermes_plugins.grok_bot_twin.twin.engine import EngineError

    class Untrusted:
        def grok_list(self, globs):
            raise EngineError("untrusted_binary", "botport has not been checked and trusted yet")

    svc.demo_action("start")
    monkeypatch.setattr(svc, "botport", lambda: Untrusted())
    # no `bots` filter: real bots are not even asked for while the demo runs
    names = {b["name"] for b in svc.grok_list()["bots"]}
    assert "Acme Helper" in names and "Recipe Planner" in names
    from hermes_plugins.grok_bot_twin.twin import config
    monkeypatch.setattr(config, "get", lambda k, d=None: ["*"] if k == "bots" else d)
    res = svc.grok_list()
    assert "Recipe Planner" in {b["name"] for b in res["bots"]}
    assert res["warnings"][0]["code"] == "untrusted_binary"


def test_fixture_activity_relative_to_demo_start(svc, home):
    import json as _json
    from hermes_plugins.grok_bot_twin.twin.model import ago, now_dt, parse_iso
    svc.demo_action("start")
    entries = svc.recent("acme-helper", 10)["entries"]
    fixture = [e for e in entries if e["summary"] != "Demo started"]
    assert len(fixture) == 3
    for e in fixture:
        age = (now_dt() - parse_iso(e["at"])).total_seconds()
        assert 0 < age < 3600 and ago(e["at"]).endswith("min ago")
    assert entries[0]["summary"] == "Demo started"  # newest first; fixtures sit before the start
    stored = _json.loads((central(home) / "demo" / "hermes" / "acme-helper.json").read_text())
    assert all(e["at"].startswith("2000-") for e in stored["activity"] if "offset_s" in e)


def test_final_demo_names(svc):
    svc.demo_action("start")
    t = svc.demo._twin("acme-helper")
    assert sorted(t["skills"]) == ["fun-fact", "greet-visitors"]
    assert t["skills"]["fun-fact"]["text"] == "# Fun fact\nShare one short, friendly fun fact when asked."
    g = svc.demo._grok("b-acme-0001")
    assert g["routines"] == ["Morning summary", "Water reminder"] and g["connectors"] == ["example-files"]
    assert {b["name"] for b in svc.grok_list()["bots"]} == {"Acme Helper", "Recipe Planner"}


def test_doctor_marks_botport_optional_without_real_twins(svc):
    svc.demo_action("start")
    checks = svc.doctor()["checks"]
    failing = [c for c in checks if not c["ok"]]
    assert all(c.get("optional") for c in failing if c["id"] != "data_dir")


def test_cache_only_demo_twin_keeps_instructions_by_hand(svc):
    svc.demo_action("start")
    prev = svc.preview(["b-recipe-0002"])["pairs"][0]
    acts = {it["item"]: it["action"] for it in prev["items"]}
    assert acts["persona.name"] == "create" and acts["persona.instructions"] == "by_hand"
    assert acts["skill:pantry-check"] == "via_mirror" and acts["routine:Sunday meal plan"] == "by_hand"
    svc.create(["b-recipe-0002"])
    t = svc.demo._twin("recipe-planner")
    assert t.get("instructions") is None and t["skills"]  # instructions are never copied, skills via Mirror
    p = _pair(svc, "recipe-planner")
    assert p["mirror_bot"] == {"connected": True} and p["state"] == "in_step"
    assert any(b["item"] == "persona.instructions" and b["side"] == "hermes" for b in p["by_hand"])
    assert svc.sync("recipe-planner")["pairs"][0]["applied"] == []  # a later sync does not copy them either
    checks = {c["id"]: c for c in svc.parity("recipe-planner")["checks"]}
    assert checks["persona.instructions"]["pass"] is False
