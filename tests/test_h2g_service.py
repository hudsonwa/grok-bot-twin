"""Hermes -> Grok through the service, the terminal and the sample (h2g_service.py). The core rules are in test_h2g.py."""
from __future__ import annotations

import json

import pytest

from twin_testlib import cli, home, service, store, svc  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import h2g, h2g_service, importer
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

PID = "acme-helper"
BOT = "b-acme-helper-1"


def _soul(name: str, description: str, persona: str) -> str:
    return "".join(f"<!-- botport:begin {k} -->\n{v}\n<!-- botport:end {k} -->\n"
                   for k, v in (("persona-name", name), ("persona-description", description), ("persona", persona)))


def _job(enabled: bool) -> dict:
    return {"id": "job-1", "name": "Morning digest", "prompt": "Say good morning to the team.",
            "schedule": {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"},
            "enabled": enabled, "state": "scheduled" if enabled else "paused"}


@pytest.fixture(autouse=True)
def _no_panel_choice(monkeypatch):
    """These tests drive the allowlist alone; with the What syncs panel in the build the owner's choice there would
    hold name/description at Grok -> Hermes (its default), so the owner is taken to have chosen Both ways. The panel gate itself is tested in tests/test_h2g.py."""
    try:
        from hermes_plugins.grok_bot_twin.twin import sync_panel
    except ImportError:
        return
    monkeypatch.setattr(sync_panel, "wanted", lambda pid, item: "both")


@pytest.fixture
def real_beta_off(svc, home):  # noqa: F811
    """A real twin with the Hermes to Grok beta left as shipped: off."""
    store.atomic_write_json(svc.central / "pairs.json", {"pairs": [
        {"pair_id": PID, "grok": {"id": BOT, "name": "Acme Helper"}, "hermes": {"profile": PID}, "job_ids": []}]})
    (home / "profiles" / PID).mkdir(parents=True)
    (home / "profiles" / PID / "SOUL.md").write_text(_soul("Acme Helper", "Helps with Acme widgets.", "Be brief."))
    w = h2g.FakeGrokWriter()
    w.put_bot(BOT, name="Acme Helper", description="Helps with Acme widgets.", instructions="Be brief.")
    svc.h2g_test_writer = w
    return svc


@pytest.fixture
def real(svc, home):  # noqa: F811
    """One real twin (record, SOUL.md, one routine paused in Hermes) and a pretend Grok bot that matches it."""
    store.atomic_write_json(svc.central / "pairs.json", {"pairs": [
        {"pair_id": PID, "grok": {"id": BOT, "name": "Acme Helper"}, "hermes": {"profile": PID},
         "job_ids": ["job-1"]}]})
    prof = home / "profiles" / PID
    (prof / "cron").mkdir(parents=True)
    (prof / "SOUL.md").write_text(_soul("Acme Helper", "Helps with Acme widgets.", "Be friendly.\nBe brief."))
    (prof / "cron" / "jobs.json").write_text(json.dumps({"jobs": [_job(False)]}))
    store.atomic_write_json(svc.central / h2g_service.BETA_FILE, {"on": True})  # the owner turned the beta on
    w = h2g.FakeGrokWriter()
    w.put_bot(BOT, name="Acme Helper", description="Helps with Acme widgets.", instructions="Be friendly.\nBe brief.",
              routines=[{"key": "morning-digest", "name": "Morning digest", "schedule": "0 9 * * *",
                         "prompt": "Say good morning to the team.", "enabled": False}])
    return svc, prof, w


def test_real_twin_without_the_mirror_says_so_and_changes_nothing(real):
    svc, prof, _ = real
    info = svc.to_grok_info(PID)
    assert info["allowed"] == [] and info["writer"] == "none" and info["permission"] == "not_set_up"
    svc.to_grok_allow(PID, ["name"])
    (prof / "SOUL.md").write_text(_soul("Acme Helper Pro", "Helps with Acme widgets.", "Be friendly.\nBe brief."))
    p = svc.to_grok_preview(PID)
    assert p["blocked"] == "The Mirror is not set up for this bot yet."
    with pytest.raises(EngineError) as e:
        svc.to_grok_apply(PID, p["change_id"])
    assert e.value.code == "no_permission"


def test_hermes_side_is_read_from_the_twins_own_files(real):
    svc, prof, _ = real
    side = svc._h2g_hermes(PID)
    assert side["name"] == "Acme Helper" and side["instructions"] == "Be friendly.\nBe brief."
    assert side["routines"] == [{"name": "Morning digest", "schedule": "0 9 * * *",
                                 "prompt": "Say good morning to the team.", "enabled": False}]
    # the owner's pasted block wins over botport's persona section
    (prof / "SOUL.md").write_text(_soul("Acme Helper", "x", "Old.") +
                                  "<!-- grok-bot-twin:begin instructions -->\nPasted.\n<!-- grok-bot-twin:end instructions -->\n")
    assert svc._h2g_hermes(PID)["instructions"] == "Pasted."
    # a job that is not the twin's (not recorded, not an importer's) is never read
    (prof / "cron" / "jobs.json").write_text(json.dumps({"jobs": [dict(_job(True), id="other")]}))
    assert svc._h2g_hermes(PID)["routines"] == []


def test_preview_apply_undo_with_a_writer(real):
    svc, prof, w = real
    svc.h2g_test_writer = w
    svc.to_grok_allow(PID, "all")
    assert svc.to_grok_preview(PID)["nothing"] is True
    (prof / "SOUL.md").write_text(_soul("Acme Helper Pro", "Helps with Acme widgets.", "Be friendly.\nBe short."))
    p = svc.to_grok_preview(PID)
    assert [c["item"] for c in p["changes"]] == ["name", "instructions"]
    assert "+Be short." in p["changes"][1]["diff"]
    r = svc.to_grok_apply(PID, p["change_id"])
    assert r["state"] == "done" and w.read(BOT)["name"] == "Acme Helper Pro"
    with pytest.raises(EngineError) as e:  # the same preview again is stale: it was applied
        svc.to_grok_apply(PID, "c-0000000000000000")
    assert e.value.code == "stale"
    u = svc.to_grok_undo(PID, r["change_id"])
    assert u["state"] == "done" and w.read(BOT)["name"] == "Acme Helper"
    assert w.read(BOT)["instructions"] == "Be friendly.\nBe brief."


def test_routine_handover_pauses_hermes_first(real, monkeypatch):
    svc, prof, w = real
    svc.h2g_test_writer = w
    jobs = prof / "cron" / "jobs.json"
    calls = []

    def act(on: bool):
        def fn(job_id, profile, ctx=None):
            calls.append(("resume" if on else "pause", job_id, profile))
            jobs.write_text(json.dumps({"jobs": [_job(on)]}))
            return True, ""
        return fn

    monkeypatch.setattr(importer, "pause_cron", act(False))
    monkeypatch.setattr(importer, "resume_cron", act(True))
    jobs.write_text(json.dumps({"jobs": [_job(True)]}))  # it runs in Hermes now
    svc.to_grok_allow(PID, ["routines"])
    r = svc.to_grok_runner(PID, "Morning digest", "grok")
    assert r["runner"] == "grok" and calls == [("pause", "job-1", PID)]
    assert w.read(BOT)["routines"][0]["enabled"] is True
    r = svc.to_grok_runner(PID, "morning-digest", "hermes")
    assert r["runner"] == "hermes" and calls[-1] == ("resume", "job-1", PID)
    assert w.read(BOT)["routines"][0]["enabled"] is False


def test_routine_handover_refused_when_hermes_does_not_pause(real, monkeypatch):
    svc, prof, w = real
    svc.h2g_test_writer = w
    (prof / "cron" / "jobs.json").write_text(json.dumps({"jobs": [_job(True)]}))
    monkeypatch.setattr(importer, "pause_cron", lambda *a, **k: (True, ""))  # says yes, but the job stays on
    svc.to_grok_allow(PID, ["routines"])
    with pytest.raises(EngineError) as e:
        svc.to_grok_runner(PID, "morning-digest", "grok")
    assert e.value.code == "hermes_not_paused" and w.read(BOT)["routines"][0]["enabled"] is False


def test_sample_twin_round_trip_and_allow_never_pauses_a_grok_routine(svc):  # noqa: F811
    svc.demo_action("start")
    pid = "acme-support"
    info = svc.to_grok_allow(pid, ["name", "description", "routines"])
    assert info["writer"] == "fake" and info["runner"] == {"morning-summary": "grok", "water-reminder": "grok"}
    assert svc.to_grok_preview(pid)["nothing"] is True
    t = svc.demo._twin(pid)
    t["description"] = "Answers questions about Acme widgets and gadgets."
    svc.demo._write_twin(pid, t)
    p = svc.to_grok_preview(pid)
    assert [c["item"] for c in p["changes"]] == ["description"]
    assert svc.to_grok_apply(pid, p["change_id"])["state"] == "done"
    fake = h2g.FakeGrokWriter(svc.central / h2g.FAKE_FILE)
    assert fake.read("b-acme-0001")["description"].endswith("and gadgets.")
    assert all(r["enabled"] for r in fake.read("b-acme-0001")["routines"])


def test_empty_hermes_instructions_never_clear_grok(svc):  # noqa: F811
    svc.demo_action("start")  # the sample's instructions are not brought over yet: Hermes has none
    svc.to_grok_allow("acme-support", ["instructions"])
    assert svc.to_grok_preview("acme-support")["nothing"] is True


def test_cli_to_grok(svc, capsys):  # noqa: F811
    parser = cli.build_parser()
    a = parser.parse_args(["to-grok", "Acme Support", "apply", "c-0123456789abcdef"])
    assert a.twin_command == "to-grok" and a.args == ["Acme Support", "apply", "c-0123456789abcdef"]
    svc.demo_action("start")
    assert cli.main(["to-grok"]) == 0
    assert "may not change this bot" in capsys.readouterr().out
    assert cli.main(["to-grok", "allow", "name", "description"]) == 0
    assert "may change in Grok: name, description." in capsys.readouterr().out
    t = svc.demo._twin("acme-support")
    t["name"] = "Acme Support Desk"
    svc.demo._write_twin("acme-support", t)
    assert cli.main(["to-grok", "Acme Support"]) == 0
    out = capsys.readouterr().out
    assert 'Name: "Acme Support" → "Acme Support Desk"' in out and "hermes twin to-grok apply c-" in out
    cid = out.split("hermes twin to-grok apply ")[1].split()[0]
    assert cli.main(["to-grok", "apply", cid]) == 0
    assert "Made in Grok." in capsys.readouterr().out
    assert cli.main(["to-grok", "undo", "c-zz"]) == 2  # not a change id: a usage answer
    assert cli.main(["to-grok", "runner", "x"]) == 2


def test_service_has_the_mixin():
    assert issubclass(service.TwinService, h2g_service.H2GMixin)


def test_real_twin_uses_the_mirror_once_skill_sync_has_a_shared_folder(real, tmp_path):
    import os
    from twin import skill_sync
    svc, prof, _ = real
    shared = tmp_path / "shared-folder"
    os.mkdir(shared, 0o700)
    st = skill_sync.load_state(svc.central)
    st["root"] = str(shared)
    skill_sync.save_state(svc.central, st)
    w = svc._h2g_writer(PID)
    assert type(w).__name__ == "MirrorFolderWriter" and w.kind == "mirror"
    assert svc.to_grok_info(PID)["permission"] == "not_set_up"  # the Mirror never ran


# ---- the beta switch: off by default for the whole plugin ------------------------------------------------------------
def test_beta_is_off_by_default_and_refuses_every_change(real_beta_off):
    svc = real_beta_off
    assert svc.to_grok_beta_on() is False
    info = svc.to_grok_info(PID)
    assert info["beta"] is False and "Beta (off)" in info["beta_note"] and "hermes twin to-grok beta on" in info["beta_note"]
    for call in (lambda: svc.to_grok_allow(PID, "all"), lambda: svc.to_grok_apply(PID, "c-0123456789abcdef"),
                 lambda: svc.to_grok_undo(PID, "c-0123456789abcdef"), lambda: svc.to_grok_resolve(PID, "name", "hermes"),
                 lambda: svc.to_grok_runner(PID, "morning-digest", "grok"), lambda: svc.to_grok_preview(PID)):
        with pytest.raises(EngineError) as e:
            call()
        assert e.value.code == "beta_off" and "beta and is off" in e.value.message
    assert not (svc.central / h2g.STATE_FILE).exists()  # nothing written, nothing for the Mirror
    assert not list(svc.central.rglob("to-grok"))
    assert svc.h2g_directions(PID, "name") == {"hermes_to_grok": "not_yet", "both": "not_yet"}


def test_timer_sends_nothing_while_the_beta_is_off(real_beta_off, monkeypatch):
    svc = real_beta_off
    store.atomic_write_json(svc.central / h2g.STATE_FILE, {"v": h2g.V})
    called = []
    monkeypatch.setattr(type(svc), "to_grok_poll", lambda self: called.append(1) or [])
    svc.to_grok_tick()
    assert called == []
    store.atomic_write_json(svc.central / h2g_service.BETA_FILE, {"on": True})
    svc.to_grok_tick()
    assert called == [1]


def test_beta_on_and_off_only_from_the_owners_terminal(real_beta_off, monkeypatch, capsys):
    from hermes_plugins.grok_bot_twin.twin import tasks
    svc = real_beta_off
    monkeypatch.setattr(tasks, "asked_turn", lambda *a, **k: True)  # a request from another computer
    assert cli.main(["to-grok", "beta", "on"]) != 0
    assert svc.to_grok_beta_on() is False
    monkeypatch.setattr(tasks, "asked_turn", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "_chat_numbers", lambda *a, **k: ["chat-1"])  # a command a chat started
    assert cli.main(["to-grok", "beta", "on"]) != 0
    assert svc.to_grok_beta_on() is False
    monkeypatch.setattr(tasks, "_chat_numbers", lambda *a, **k: [])
    assert cli.main(["to-grok", "beta", "on"]) == 0
    assert "is on" in capsys.readouterr().out and svc.to_grok_beta_on() is True
    assert cli.main(["to-grok", "beta", "maybe"]) == 2
    assert cli.main(["to-grok", "beta", "off"]) == 0 and svc.to_grok_beta_on() is False


def test_changes_refused_inside_a_request_or_a_chat_even_from_the_page(real, monkeypatch):
    from hermes_plugins.grok_bot_twin.twin import tasks
    svc, _, w = real
    svc.h2g_test_writer = w
    monkeypatch.setattr(tasks, "asked_turn", lambda *a, **k: True)
    for own in (False, True):
        for call in (lambda: svc.to_grok_allow(PID, "all", own), lambda: svc.to_grok_apply(PID, "c-0123456789abcdef", own),
                     lambda: svc.to_grok_undo(PID, "c-0123456789abcdef", own),
                     lambda: svc.to_grok_resolve(PID, "name", "hermes", own),
                     lambda: svc.to_grok_runner(PID, "morning-digest", "grok", own), lambda: svc.to_grok_give_up(PID, own)):
            with pytest.raises(EngineError) as e:
                call()
            assert e.value.code == "refused"
    monkeypatch.setattr(tasks, "asked_turn", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "_chat_numbers", lambda *a, **k: ["chat-1"])
    with pytest.raises(EngineError):
        svc.to_grok_allow(PID, "all")  # a command a chat started
    assert svc.to_grok_allow(PID, "all", True)["allowed"]  # the owner's own page
