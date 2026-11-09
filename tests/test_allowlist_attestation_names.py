"""strict allowlist of botport output, mandatory attestation, checked gh/hermes paths,
"Ready to switch" jobs by stable id, pair key rotation, and the model-visible names setting."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from twin_testlib import ROOT, central, config, dumps, home, service, timer, tools  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import importer
from hermes_plugins.grok_bot_twin.twin import engine as E
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

FAKE = Path(__file__).resolve().parent / "fake_botport.py"
MARKER = "EXTRA-MARKER-7f3a"
P = "/api/plugins/grok-bot-twin"


def _write_exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    path.write_text(f"#!{sys.executable} -I\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def rig(home, tmp_path):
    exe = _write_exe(tmp_path / "bin" / "botport", FAKE.read_text(encoding="utf-8"))
    os.chmod(tmp_path, 0o700)

    class R:
        def settings(self, **extra):
            config.set_override({"botport_path": str(exe), **extra})
            service.reset_service()
            self.svc = service.get_service()
            return self.svc

        def mode(self, **kw):
            d = central(home) / "botport-state"
            d.mkdir(parents=True, exist_ok=True)
            (d / "fake_mode.json").write_text(json.dumps(kw))

    r = R()
    r.exe = exe
    r.settings()
    return r


def _client():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_allowlist", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = fastapi.FastAPI()
    app.include_router(api.router, prefix=P)
    return TestClient(app)


# ---- nothing botport sends beyond the known fields reaches REST, tools, events or status-cache.json -------

def test_unknown_botport_fields_never_leave_the_service(rig, home, tmp_path, monkeypatch):
    rig.svc.trust_botport(True)
    rig.mode(extras=True)
    svc = rig.settings()
    assert MARKER in dumps(svc.botport().status(None)) and MARKER in dumps(svc.botport().parity("acme-helper"))
    folder = tmp_path / "pair-folder"
    folder.mkdir()
    out = [svc.status(fresh=True), svc.grok_list(), svc.preview(["b-acme-0001"]), svc.create(["b-acme-0001"]),
           svc.sync(None), svc.sync("acme-helper"), svc.conflicts("acme-helper"),
           svc.resolve("acme-helper", "skill:greet-visitors", "hermes"), svc.restore_points("acme-helper"),
           svc.restore("acme-helper", "persona.name@8ef22c6d10ba"), svc.recent("acme-helper", 10),
           svc.parity("acme-helper"), svc.approve("acme-helper", "routine:Morning summary"),
           svc.mirror("acme-helper", str(folder)), svc.mirror("acme-helper", None, False, True),
           svc.mirror("acme-helper", None, True), svc.send_to_grok("acme-helper"), svc.checklist("acme-helper"),
           svc.remove("acme-helper", True), svc.doctor(), svc.engine_info(), svc.trust_info(),
           svc.digest_preview("acme-helper"), svc.events()]
    texts = [dumps(o) for o in out]
    texts += [tools.twin_status({}), tools.twin_recent({})]
    config.set_override({"botport_path": str(rig.exe), "share_names_with_model": True})
    texts += [tools.twin_status({}), tools.twin_recent({}), tools.twin_status({"pair": "acme-helper"})]
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    c = _client()
    for path in ("/status", "/grok/bots", "/pairs/acme-helper/conflicts", "/pairs/acme-helper/restore-points",
                 "/pairs/acme-helper/recent", "/pairs/acme-helper/parity", "/pairs/acme-helper/checklist",
                 "/doctor", "/engine/trust", "/events"):
        r = c.get(P + path)
        assert r.status_code == 200, path
        texts.append(r.text)
    for path, body in (("/sync", {}), ("/twins/preview", {"grok_ids": ["b-acme-0001"]}),
                       ("/pairs/acme-helper/resolve", {"conflict_id": "skill:greet-visitors", "keep": "grok"}),
                       ("/pairs/acme-helper/approve", {"item": "connector:example-files"}),
                       ("/pairs/acme-helper/restore", {"point_id": "persona.name@8ef22c6d10ba"}),
                       ("/pairs/acme-helper/mirror", {"rotate_key": True}),
                       ("/pairs/acme-helper/remove", {"dry_run": True})):
        r = c.post(P + path, json=body)
        assert r.status_code == 200, (path, r.text)
        texts.append(r.text)
    texts.append((central(home) / "status-cache.json").read_text())
    texts.append((central(home) / "events.jsonl").read_text())
    texts.append((central(home) / "pairs.json").read_text())
    for t in texts:
        assert MARKER not in t and "zz_extra" not in t and "fake_key" not in t, t[:400]
    # the known fields still arrive
    st = svc.status(fresh=True)
    real = [p for p in st["pairs"] if p["pair_id"] == "acme-helper"][0]
    assert real["grok"]["id"] == "b-acme-0001" and real["state"] == "in_step"
    par = svc.parity("acme-helper")
    assert par["passed"] == 1 and par["total"] == 2
    assert set(par["checks"][0]) == {"id", "name", "pass", "detail"}
    assert svc.resolve("acme-helper", "skill:greet-visitors", "grok")["applied"] == [
        {"item": "skill:greet-visitors", "keep": "grok"}]


def test_normalize_pair_is_an_allowlist():
    from twin_testlib import model
    raw = {"pair_id": "acme-helper", "state": "in_step", "grok": {"id": "b-1", "name": "Acme", "token": "x"},
           "hermes": {"profile": "acme-helper", "home": "/secret/path"}, "extra": {"fake_key": "sk-x"},
           "coverage": [{"item": "skills", "g2h": "syncs", "h2g": "bogus", "x": 1}, {"item": "evil"}],
           "parity": {"passed": 1, "total": 2, "detail": "x"}, "error": {"code": "c", "message": "m", "trace": "t"},
           "last_sync_at": "not a time"}
    p = model.normalize_pair(raw)
    assert "extra" not in p and "token" not in p["grok"] and "home" not in p["hermes"]
    assert p["coverage"] == [{"item": "skills", "g2h": "syncs", "h2g": "by_hand"}]
    assert p["parity"] == {"passed": 1, "total": 2} and p["error"] == {"code": "c", "message": "m"}
    assert p["last_sync_at"] is None
    assert set(p) == {"pair_id", "demo", "grok", "hermes", "pending", "conflicts", "mirror_bot", "coverage",
                      "by_hand", "restore_points", "secrets_withheld_total", "last_sync_at", "last_change_at",
                      "waiting", "secrets_withheld", "secrets_held", "parity", "error", "state", "state_reason"}


# ---- attestation mandatory unless the loud, recorded opt-out ------------------------------------------------

def _fake_gh(tmp_path: Path, rc: int) -> None:
    _write_exe(tmp_path / "ghbin" / "gh", f"import sys\nsys.stderr.write('verification: fake\\n'); sys.exit({rc})\n")


def test_trust_needs_attestation_or_explicit_opt_out(rig, home, tmp_path, monkeypatch):
    _fake_gh(tmp_path, 1)
    monkeypatch.setenv("PATH", f"{tmp_path / 'ghbin'}:/usr/bin:/bin")
    svc = rig.svc
    with pytest.raises(EngineError) as ei:
        svc.trust_botport()  # default: attestation required
    assert ei.value.code == "attestation_failed" and "--allow-unattested" in ei.value.hint
    assert not (central(home) / "botport-trust.json").exists()
    res = svc.trust_botport(True)
    assert res["trusted"] and res["allow_unattested"] and res["warning"] == E.UNATTESTED_TEXT
    rec = json.loads((central(home) / "botport-trust.json").read_text())
    assert rec["allow_unattested"] is True
    svc.invalidate()
    assert svc.status(fresh=True)["engine"]["trust_warning"] == "botport trusted WITHOUT attestation (local build)"
    att = [c for c in svc.doctor()["checks"] if c["id"] == "botport_attestation"][0]
    assert att["ok"] is False and "WITHOUT attestation (local build)" in att["message"]
    # a record that is neither attested nor an explicit opt-out never runs
    rec["allow_unattested"] = False
    (central(home) / "botport-trust.json").write_text(json.dumps(rec))
    with pytest.raises(EngineError) as ei:
        svc.botport().run(["version"])
    assert ei.value.code == "untrusted_binary"
    # attested: no warning, the opt-out is cleared
    _fake_gh(tmp_path, 0)
    res = svc.trust_botport()
    assert res["attested"] and not res["allow_unattested"] and "warning" not in res
    svc.invalidate()
    assert "trust_warning" not in svc.status(fresh=True)["engine"]


def test_rest_trust_route_flag(rig, home, tmp_path, monkeypatch):
    _fake_gh(tmp_path, 1)
    monkeypatch.setenv("PATH", f"{tmp_path / 'ghbin'}:/usr/bin:/bin")
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    c = _client()
    r = c.post(P + "/engine/trust", json={})
    assert r.json()["error"]["code"] == "attestation_failed"
    assert c.post(P + "/engine/trust", json={"allow_unattested": "yes"}).status_code == 400
    r = c.post(P + "/engine/trust", json={"allow_unattested": True}).json()
    assert r["trusted"] and r["allow_unattested"] is True


def test_cli_setup_flag(rig, tmp_path, monkeypatch):
    from twin_testlib import cli
    _fake_gh(tmp_path, 1)
    monkeypatch.setenv("PATH", f"{tmp_path / 'ghbin'}:/usr/bin:/bin")
    assert cli.main(["setup"]) == 1
    assert cli.main(["setup", "--allow-unattested"]) == 0
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["setup", "--require-attestation"])


# ---- gh and hermes resolved to real paths and checked like botport ------------------------------------------

def test_tools_resolved_and_checked(home, tmp_path, monkeypatch):
    good = _write_exe(tmp_path / "okbin" / "gh", "print('hi')\n")
    link_dir = tmp_path / "linkbin"
    link_dir.mkdir()
    (link_dir / "gh").symlink_to(good)
    path, problem = E.resolve_tool("gh", str(link_dir))
    assert path == os.path.realpath(good) and problem is None  # the link is resolved to the real file
    bad = _write_exe(tmp_path / "badbin" / "gh", "print('hi')\n")
    bad.chmod(0o777)
    path, problem = E.resolve_tool("gh", str(tmp_path / "badbin"))
    assert path == os.path.realpath(bad) and "writable by other users" in problem
    wdir = tmp_path / "wbin"
    _write_exe(wdir / "hermes", "print('hi')\n")
    wdir.chmod(0o777)
    _, problem = E.resolve_tool("hermes", str(wdir))
    assert "folder above hermes is writable" in problem
    assert E.resolve_tool("gh", str(tmp_path / "nothing"))[0] is None


def test_unsafe_gh_is_refused_and_doctor_shows_paths(rig, home, tmp_path, monkeypatch):
    _fake_gh(tmp_path, 0)
    (tmp_path / "ghbin" / "gh").chmod(0o777)
    monkeypatch.setenv("PATH", f"{tmp_path / 'ghbin'}:/usr/bin:/bin")
    with pytest.raises(EngineError) as ei:
        rig.svc.trust_botport()
    assert "refusing to run gh" in ei.value.message and "writable by other users" in ei.value.message
    checks = {c["id"]: c for c in rig.svc.doctor()["checks"]}
    assert checks["gh_path"]["ok"] is False and "refusing to run gh" in checks["gh_path"]["message"]
    assert "hermes_path" in checks and checks["hermes_path"]["message"].startswith(("hermes", "refusing"))
    (tmp_path / "ghbin" / "gh").chmod(0o755)
    checks = {c["id"]: c for c in rig.svc.doctor()["checks"]}
    assert checks["gh_path"]["ok"] is True and "checked: ok" in checks["gh_path"]["message"]


def test_unsafe_hermes_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(importer, "hermes_check",
                        lambda: {"argv0": "/x/hermes", "path": "/x/hermes", "via": "path",
                                 "problem": "hermes is writable by other users"})
    with pytest.raises(EngineError) as ei:
        importer.hermes_argv("acme-helper")
    assert ei.value.code == "untrusted_binary"
    ok, err = importer.resume_cron("job1", "acme-helper")
    assert ok is False and "refusing to run hermes" in err


# ---- "Ready to switch" by stable job id, never by a "<bot name>: " prefix -----------------------------------

def _jobs(home: Path, jobs: list[dict]) -> Path:
    d = home / "profiles" / "acme-helper" / "cron"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "jobs.json"
    p.write_text(json.dumps({"jobs": jobs}))
    return p


def _job(jid, name, paused=True, reason=None):
    return {"id": jid, "name": name, "enabled": not paused, "state": "paused" if paused else "scheduled",
            "paused_reason": reason, "schedule": {"kind": "cron", "expr": "0 8 * * *", "display": "0 8 * * *"},
            "schedule_display": "0 8 * * *"}


def test_ready_to_switch_uses_stable_job_ids(rig, home):
    rig.svc.trust_botport(True)
    svc = rig.settings()
    jobs = [_job("a1b2c3d4e5f6", "Acme Helper: Morning summary",
                 reason="Imported by botport from the Grok bot; schedule is in UTC, resume once the Grok routine is off"),
            _job("0a0b0c0d0e0f", "Renamed by the owner",
                 reason="Imported from Grok Bot: resume once the Grok Bot routine is off"),
            _job("deadbeef0001", "Acme Helper: lookalike", reason="Created paused; awaiting operator approval."),
            _job("deadbeef0002", "Acme Helper: running", paused=False)]
    path = _jobs(home, jobs)
    svc.create(["b-acme-0001"])
    reg = json.loads((central(home) / "pairs.json").read_text())["pairs"]
    assert reg[0]["job_ids"] == ["a1b2c3d4e5f6", "0a0b0c0d0e0f"]
    ids = [j["job_id"] for j in svc.checklist("acme-helper")["switch"]["hermes_jobs_paused"]]
    assert ids == ["a1b2c3d4e5f6", "0a0b0c0d0e0f"]  # the name-prefix lookalike is not the twin's
    # the owner edits the pause reason: the recorded id still finds it; the bot's rename changes nothing
    jobs[0]["paused_reason"] = "paused by me"
    path.write_text(json.dumps({"jobs": jobs}))
    rig.mode(name="Acme Helper Renamed")
    ids = [j["job_id"] for j in svc.checklist("acme-helper")["switch"]["hermes_jobs_paused"]]
    assert ids == ["a1b2c3d4e5f6", "0a0b0c0d0e0f"]
    res = svc.switch("acme-helper", ["deadbeef0001"])
    assert res["resumed"] == [] and res["failed"][0]["error"] == "not a paused job of this twin"


def test_paused_jobs_for_ignores_names():
    assert importer.paused_jobs_for(None) == []


def test_demo_switch_uses_job_ids(home):
    svc = service.get_service()
    svc.demo_action("start")
    jobs = svc.checklist("acme-helper")["switch"]["hermes_jobs_paused"]
    assert jobs and all(j["job_id"].startswith("demo-") for j in jobs)
    res = svc.switch("acme-helper", [jobs[0]["job_id"]])
    assert res["resumed"] == [jobs[0]["job_id"]]


# ---- pair key rotation (botport twin mirror --rotate-key) -------------------------------------------------------

def test_rotate_key(rig, home):
    from twin_testlib import cli
    rig.svc.trust_botport(True)
    svc = rig.settings()
    svc.status(fresh=True)
    res = svc.mirror("acme-helper", None, False, True)
    assert res["key_rotated"] is True and "new pair key" in res["note"]
    calls = (central(home) / "botport-state" / "fake_calls.jsonl").read_text().splitlines()
    assert "--rotate-key" in json.loads(calls[-1])["argv"]
    with pytest.raises(service.BadInput):
        svc.mirror("acme-helper", None, True, True)
    assert cli.main(["mirror", "acme-helper", "--rotate-key"]) == 0
