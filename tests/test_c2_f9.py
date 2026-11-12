"""F9: a routine runs in ONE place. "Move routines to Hermes" (service.switch) and every other resume go through
importer.resume_cron, which refuses a routine Hermes to Grok has moved to run in Grok (h2g.json's runner), or is
moving, with a sentence that says how to move it back. Hermes to Grok's own move back still switches Hermes on."""
from __future__ import annotations

import json

import pytest

from twin_testlib import home, store, svc  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import h2g, importer

PID = "test-scout"
BOT = "b-test-scout-1"
DIGEST = {"key": "morning-digest", "name": "Morning digest", "schedule": "0 9 * * *",
          "prompt": "Say good morning to the team.", "enabled": False}
WATER = {"key": "water-reminder", "name": "Water reminder", "schedule": "0 15 * * *",
         "prompt": "Remind the team to drink water.", "enabled": False}


def _job(jid: str, r: dict, on: bool) -> dict:
    return {"id": jid, "name": r["name"], "prompt": r["prompt"],
            "schedule": {"kind": "cron", "expr": r["schedule"], "display": r["schedule"]},
            "enabled": on, "state": "scheduled" if on else "paused"}


@pytest.fixture(autouse=True)
def _no_panel_choice(monkeypatch):
    try:
        from hermes_plugins.grok_bot_twin.twin import sync_panel
    except ImportError:
        return
    monkeypatch.setattr(sync_panel, "wanted", lambda pid, item: "both")


@pytest.fixture
def twin(svc, home, monkeypatch):  # noqa: F811
    """A real twin with two routines (job-1 on in Hermes, job-2 paused), a pretend Grok bot holding both, and a
    pretend `hermes cron` that records each call and flips the job in jobs.json."""
    store.atomic_write_json(svc.central / "pairs.json", {"pairs": [
        {"pair_id": PID, "grok": {"id": BOT, "name": "Test Scout"}, "hermes": {"profile": PID},
         "job_ids": ["job-1", "job-2"]}]})
    jobs = home / "profiles" / PID / "cron" / "jobs.json"
    jobs.parent.mkdir(parents=True)
    jobs.write_text(json.dumps({"jobs": [_job("job-1", DIGEST, True), _job("job-2", WATER, False)]}))
    w = h2g.FakeGrokWriter()
    w.put_bot(BOT, name="Test Scout", description="Scouts invented things.", instructions="Be brief.",
              routines=[DIGEST, WATER])
    svc.h2g_test_writer = w
    calls: list = []

    def cron(action, job_id, profile, ctx=None):
        calls.append((action, job_id))
        data = json.loads(jobs.read_text())
        for j in data["jobs"]:
            if j["id"] == job_id:
                j["enabled"], j["state"] = action == "resume", "scheduled" if action == "resume" else "paused"
        jobs.write_text(json.dumps(data))
        return True, ""
    monkeypatch.setattr(importer, "_cron_action", cron)
    # the switch's own gates (botport's pair state) are not what this test is about: the twin is ready to switch
    monkeypatch.setattr(svc, "pair_status", lambda pid: {"hermes": {"profile": PID}})
    monkeypatch.setattr(svc, "_blocked_by", lambda pid, st: None)
    monkeypatch.setattr(svc, "checklist", lambda pid: {"switch": {"hermes_jobs_paused": importer.paused_jobs_for(
        home / "profiles" / PID, ["job-1", "job-2"])}})
    svc.to_grok_allow(PID, ["routines"])
    return svc, w, calls


def test_F9_move_routines_to_hermes_never_resumes_a_routine_that_runs_in_grok(twin):
    svc, w, calls = twin
    # Hermes to Grok moves the digest to Grok: Hermes is paused FIRST, then Grok switches it on (the reverse way)
    assert svc.to_grok_runner(PID, "morning-digest", "grok")["runner"] == "grok"
    assert calls == [("pause", "job-1")] and w.read(BOT)["routines"][0]["enabled"] is True
    calls.clear()

    res = svc.switch(PID, ["job-1"])  # "Move routines to Hermes" for the routine that now runs in Grok
    assert ("resume", "job-1") not in calls and res["resumed"] == []
    assert res["failed"] == [{"job_id": "job-1", "error": h2g.IN_GROK.format(name="Morning digest",
                                                                             key="morning-digest")}]
    assert "runs in Grok" in res["failed"][0]["error"] and "runner morning-digest hermes" in res["failed"][0]["error"]

    assert svc.switch(PID, ["job-2"]) == {"resumed": ["job-2"], "failed": []}  # not moved to Grok: resumes
    assert calls == [("resume", "job-2")]

    # mid-move (a `_move:` mark: the Mirror has not answered yet) also refuses
    importer.pause_cron("job-2", PID)
    with store.update(lambda: store.read_json(svc.central / h2g.STATE_FILE),
                      lambda d: store.atomic_write_json(svc.central / h2g.STATE_FILE, d)) as d:
        d["runner"][PID]["_move:water-reminder"] = {"to": "grok", "change": "c-0000000000000000"}
    calls.clear()
    res = svc.switch(PID, ["job-2"])
    assert calls == [] and res["resumed"] == [] and "being moved" in res["failed"][0]["error"]
    assert importer.resume_cron("job-2", PID)[0] is False  # the one choke point, whoever calls it

    # Hermes to Grok's own "run in Hermes" still switches the digest back on in Hermes (and pauses Grok first)
    r = svc.to_grok_runner(PID, "morning-digest", "hermes")
    assert r["runner"] == "hermes" and calls == [("resume", "job-1")]
    assert w.read(BOT)["routines"][0]["enabled"] is False
