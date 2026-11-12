"""Regression tests for hardening: engine output is never trusted as a path, an option or terminal text."""
from __future__ import annotations

import json
import os
import sys

import pytest

from twin_testlib import central, fake_key, home, service, store, svc  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import importer
from hermes_plugins.grok_bot_twin.twin.engine import EngineError


class _StubEngine:
    """Stands in for a (misbehaving) botport: returns whatever the test puts in."""

    def __init__(self, **data):
        self.data = data

    def conflicts(self, pid):
        return self.data["conflicts"]

    def restore_points(self, pid):
        return self.data["points"]


def _route(svc, monkeypatch, engine, pair=None):
    monkeypatch.setattr(svc, "require_pair", lambda pid: pid)
    monkeypatch.setattr(svc, "engine_for", lambda pid: engine)
    if pair is not None:
        monkeypatch.setattr(svc, "pair_status", lambda pid: pair)


# ---- store: reads never follow a link ----------------------------------------------------------------

def test_read_json_and_jsonl_refuse_symlinks(home, tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps({"stolen": True}) + "\n")
    d = central(home)
    store.ensure_dir(d)
    (d / "events.jsonl").symlink_to(outside)
    (d / "pairs.json").symlink_to(outside)
    assert store.read_jsonl(d / "events.jsonl") == []
    assert store.read_json(d / "pairs.json", "dflt") == "dflt"
    # a FIFO (or anything that is not a regular file) is refused without blocking
    os.mkfifo(d / "fifo.json")
    assert store.read_json(d / "fifo.json", None) is None
    # a regular file still reads
    (d / "ok.json").write_text('{"a": 1}')
    assert store.read_json(d / "ok.json") == {"a": 1}


# ---- profile names from the engine are never a path or an option ---------------------------------------

@pytest.mark.parametrize("bad", ["../../etc", "--help", "-x", "a/b", ".."])
def test_profile_home_refuses_non_ids(home, bad):
    with pytest.raises(ValueError):
        store.profile_home(bad)
    assert store.profile_home("default") == store.hermes_root()
    assert store.profile_home("acme-support") == store.hermes_root() / "profiles" / "acme-support"


def test_checklist_ignores_traversal_profile(svc, home, tmp_path, monkeypatch):
    # a cron/jobs.json outside the profiles folder that a "../" profile name would reach
    evil = home / "x" / "evil"  # where <root>/profiles/../evil would land
    (evil / "cron").mkdir(parents=True)
    (evil / "cron" / "jobs.json").write_text(json.dumps(
        {"jobs": [{"id": "job1", "name": "Acme Support: x", "enabled": False, "schedule": "0 9 * * *"}]}))
    pair = {"pair_id": "acme-support", "hermes": {"profile": "../evil"}, "grok": {"name": "Acme Support"},
            "by_hand": []}
    _route(svc, monkeypatch, None, pair)
    monkeypatch.setattr(store, "hermes_root", lambda: home / "x")
    assert svc.checklist("acme-support")["switch"]["hermes_jobs_paused"] == []
    with pytest.raises(EngineError):
        svc.switch("acme-support", ["job1"])


def test_a_hermes_cli_folder_where_hermes_was_started_is_never_run(tmp_path, monkeypatch):
    """Inside a Hermes process the plugin starts Hermes' own command line with this Python. `python -m` looks in
    the working folder first, and that is whatever folder Hermes was started in: a project someone else wrote,
    say. The child is started with -P and is told the folder of the Hermes that is running, so it runs that one."""
    import subprocess

    real, planted = tmp_path / "real-hermes", tmp_path / "some-project"
    for root, body in ((real, "import sys\nprint('the real one', sys.argv[1:])\n"),
                       (planted, "import pathlib\npathlib.Path('planted-ran.txt').write_text('x')\nprint('planted')\n")):
        (root / "hermes_cli").mkdir(parents=True)
        (root / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
        (root / "hermes_cli" / "main.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(real))  # this process "is" that Hermes: its hermes_cli comes from there
    monkeypatch.delitem(sys.modules, "hermes_cli", raising=False)
    monkeypatch.setattr(importer, "_folders_safe", lambda path, what: None)  # (pytest's folders are not the point)
    monkeypatch.setattr(importer, "check_path_safety", lambda path, what="": None)
    assert importer.hermes_code_root() == (str(real), None)
    argv = importer.hermes_argv("acme-support")
    assert argv == [sys.executable, "-P", "-m", "hermes_cli.main", "-p", "acme-support"]
    env = importer._env()
    assert env["PYTHONPATH"] == str(real)
    done = subprocess.run(argv + ["cron", "resume", "j1"], cwd=planted, env=env, capture_output=True, text=True,
                          timeout=60)
    assert done.stdout.strip() == "the real one ['-p', 'acme-support', 'cron', 'resume', 'j1']", done.stderr[-300:]
    assert not (planted / "planted-ran.txt").exists()
    # the flaw this guards against, shown once: without -P the folder's own package wins
    old = subprocess.run([sys.executable, "-m", "hermes_cli.main"], cwd=planted, env=env, capture_output=True,
                         text=True, timeout=60)
    assert old.stdout.strip() == "planted"
    # a folder of Hermes' code that somebody else could change is refused like any other program the plugin runs
    monkeypatch.setattr(importer, "_folders_safe",
                        lambda path, what: "a folder above Hermes' code is writable by other users"
                        if "hermes_cli" in path else None)
    assert importer.hermes_check()["problem"] == "a folder above Hermes' code is writable by other users"
    with pytest.raises(EngineError) as ei:
        importer.hermes_argv("acme-support")
    assert ei.value.code == "untrusted_binary"


def test_hermes_argv_and_cron_resume_refuse_option_like_values(monkeypatch):
    calls = []
    monkeypatch.setattr(importer, "run_capped", lambda argv, *a, **k: calls.append(argv) or (0, b"", b"", False))
    for bad in ("--help", "-x", "../p"):
        with pytest.raises(EngineError):
            importer.hermes_argv(bad)
        assert importer.resume_cron("job1", bad)[0] is False
    assert importer.resume_cron("--all", "acme-support")[0] is False
    assert importer.resume_cron("-h", "default")[0] is False
    assert calls == []
    ok, _ = importer.resume_cron("job1", "acme-support")
    assert ok and calls[-1][-3:] == ["cron", "resume", "job1"] and calls[-1][-5:-3] == ["-p", "acme-support"]


# ---- cron resume: Hermes' approval step, when offered, is the only path --------------------------------------

class _Ctx:
    def __init__(self, profile, reply=None, boom=False):
        self.profile_name = profile
        self.reply, self.boom, self.calls = reply, boom, []

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        if self.boom:
            raise RuntimeError("denied by the owner")
        return self.reply


@pytest.mark.parametrize("ctx", [
    _Ctx("acme-support", json.dumps({"success": False, "error": "The owner said no"})),
    _Ctx("acme-support", json.dumps({"error": "approval denied"})),
    _Ctx("acme-support", boom=True),
    _Ctx("acme-support", "not json"),
    _Ctx("other-profile", json.dumps({"success": True})),
])
def test_cron_resume_stops_when_approval_step_refuses(monkeypatch, ctx):
    cli_calls = []
    monkeypatch.setattr(importer, "run_capped", lambda argv, *a, **k: cli_calls.append(argv) or (0, b"", b"", False))
    ok, err = importer.resume_cron("job1", "acme-support", ctx)
    assert ok is False and err
    assert cli_calls == []  # never falls through to `hermes cron resume`
    if ctx.profile_name == "other-profile":
        assert ctx.calls == [] and "acme-support" in err


def test_cron_resume_uses_approval_step_when_offered(monkeypatch):
    cli_calls = []
    monkeypatch.setattr(importer, "run_capped", lambda argv, *a, **k: cli_calls.append(argv) or (0, b"", b"", False))
    ctx = _Ctx("acme-support", json.dumps({"success": True}))
    assert importer.resume_cron("job1", "acme-support", ctx) == (True, "")
    assert ctx.calls == [("cronjob_manage", {"action": "resume", "job_id": "job1"})] and cli_calls == []


@pytest.mark.parametrize("ctx", [None, type("NoDispatch", (), {"profile_name": "acme-support"})()])
def test_cron_resume_cli_only_without_approval_api(monkeypatch, ctx):
    cli_calls = []
    monkeypatch.setattr(importer, "run_capped", lambda argv, *a, **k: cli_calls.append(argv) or (0, b"", b"", False))
    assert importer.resume_cron("job1", "acme-support", ctx) == (True, "")
    assert cli_calls and cli_calls[-1][-3:] == ["cron", "resume", "job1"]


def test_switch_stops_at_the_first_refusal(svc, monkeypatch):
    pair = {"pair_id": "acme-support", "hermes": {"profile": "acme-support"}, "grok": {"name": "Acme Support"},
            "by_hand": []}
    _route(svc, monkeypatch, None, pair)
    monkeypatch.setattr(svc, "checklist", lambda pid: {"switch": {"hermes_jobs_paused": [
        {"job_id": "job1"}, {"job_id": "job2"}]}})
    tried = []
    monkeypatch.setattr(importer, "resume_cron",
                        lambda jid, prof, ctx: tried.append(jid) or (False, "resume was not approved"))
    res = svc.switch("acme-support", ["job1", "job2"])
    assert tried == ["job1"] and res["resumed"] == []
    assert [f["job_id"] for f in res["failed"]] == ["job1", "job2"] and "not tried" in res["failed"][1]["error"]


# ---- conflict copies, diffs and restore points are masked and terminal-safe -------------------------

def test_conflict_text_is_masked_and_escape_free(svc, monkeypatch):
    key = fake_key()
    raw = {"conflicts": [{
        "conflict_id": "skill:greet-visitors", "kind": "skill", "name": "greet-visitors", "since": "not a time",
        "grok": {"digest": "abc", "text": f"line one\nkey {key}\n\x1b]0;pwned\x07\x9b31m\u202eevil\n"},
        "hermes": {"digest": "def", "text": "line one\n"},
        "diff": f"--- grok\n+++ hermes\n-key {key}\n\x1b[2J\r+line one\n"}]}
    _route(svc, monkeypatch, _StubEngine(conflicts=raw))
    c = svc.conflicts("acme-support")["conflicts"][0]
    blob = json.dumps(c, ensure_ascii=False)
    assert key not in blob and "not shown: looks like a password or key" in c["grok"]["text"]
    for ch in ("\x1b", "\x07", "\x9b", "\u202e", "\r"):
        assert ch not in c["grok"]["text"] and ch not in c["diff"]
    assert "\n" in c["diff"] and c["diff"].startswith("--- grok")
    assert c["since"] is None


def test_restore_points_are_allowlisted_and_cleaned(svc, monkeypatch):
    pts = {"points": [
        {"id": "persona.name@8ef22c6d10ba", "item": "persona.name", "kind": "name", "name": "name",
         "at": "2000-01-01T09:00:00Z", "reason": "kept\x1b[31m copy " + fake_key(), "scope": "item",
         "extra": {"huge": "x" * 5000}},
        {"id": "../../etc/passwd"}, "not a dict"]}
    _route(svc, monkeypatch, _StubEngine(points=pts))
    out = svc.restore_points("acme-support")["points"]
    assert [p["id"] for p in out] == ["persona.name@8ef22c6d10ba"]
    p = out[0]
    assert "extra" not in p and "\x1b" not in p["reason"] and fake_key() not in p["reason"]
    assert p["at"] == "2000-01-01T09:00:00Z" and p["scope"] == "item"


def test_engine_profile_name_is_cleaned_for_display():
    from hermes_plugins.grok_bot_twin.twin.model import normalize_pair
    p = normalize_pair({"pair_id": "acme-support", "hermes": {"profile": "acme\x1b[2J\u202e"}})
    assert "\x1b" not in p["hermes"]["profile"] and "\u202e" not in p["hermes"]["profile"]


def test_slash_messaging_is_read_only(monkeypatch):
    from hermes_plugins.grok_bot_twin.twin import slash

    calls = []
    monkeypatch.setattr(slash, "_sync", lambda pair: calls.append(pair) or "synced")
    out = slash.handle("sync", platform="telegram")
    assert "only shows status" in out and calls == []
    assert slash.handle("demo remove", platform="discord").startswith("From a messaging chat")
    assert slash.handle("sync", platform="local") == "synced"
    # a caller that names no platform, or an unknown one, is not taken for this computer (it fails closed)
    calls.clear()
    for platform in (None, "", "something-new"):
        assert slash.handle("sync", platform=platform).startswith("From a messaging chat") and calls == []
