"""The flows a new owner walks through, at the service and REST level: first run, connecting the helper,
the sample, real bots next to the sample, leaving (one twin, the sample, everything) and the undo paths.

Invented bots only (Acme Support, Recipe Planner). Every test runs in a throwaway HERMES_HOME; the helper is the
fake botport from tests/fake_botport.py or a file that is never executed.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from twin_testlib import ROOT, central, config, dumps, home, model, service, timer  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import engine as E
from hermes_plugins.grok_bot_twin.twin import importer
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

FAKE = Path(__file__).resolve().parent / "fake_botport.py"
P = "/api/plugins/grok-bot-twin"
# A file that looks like a program (Mach-O 64-bit header bytes) and is never run by any test.
PROGRAM = b"\xcf\xfa\xed\xfe" + b"\x07\x00\x00\x01" + b"\x00" * 64 + b"invented helper bytes"


def _write_exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    path.write_text(f"#!{sys.executable} -I\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _svc(**settings):
    config.set_override(settings)
    service.reset_service()
    return service.get_service()


def _client(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_ux", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    app = fastapi.FastAPI()
    app.include_router(api.router, prefix=P)
    return TestClient(app)


@pytest.fixture
def fake(home, tmp_path):
    """A fake botport named in the settings (not checked yet) and the service that uses it."""
    exe = _write_exe(tmp_path / "bin" / "botport", FAKE.read_text(encoding="utf-8"))
    os.chmod(tmp_path, 0o700)

    class R:
        pass

    r = R()
    r.exe = exe
    r.svc = _svc(botport_path=str(exe))
    r.calls = lambda: [json.loads(line)["argv"] for line in
                       (central(home) / "botport-state" / "fake_calls.jsonl").read_text().splitlines()] \
        if (central(home) / "botport-state" / "fake_calls.jsonl").exists() else []
    return r


def _pair(svc, pid="acme-support"):
    return next(p for p in svc.status(fresh=True)["pairs"] if p["pair_id"] == pid)


# ---- first run: three separate facts, never a false "Grok app not found" ---------------------------------------

def test_first_run_no_helper(home, tmp_path, monkeypatch):
    grok = tmp_path / "Grok Bot"
    grok.mkdir()
    svc = _svc(grok_app_dir=str(grok))  # the Grok app is installed; the helper is not
    st = svc.status(fresh=True)
    assert st["mode"] == "setup" and st["pairs"] == []
    assert st["setup"] == {"ready": False, "grok_app_found": True, "helper_found": False, "helper_checked": False,
                           "problem": None, "cause": None}  # a computer that never was set up has no "problem"
    bots = svc.grok_list()  # not an error, and not "the Grok app was not found"
    assert bots["ready"] is False and bots["app_found"] is True and bots["helper_found"] is False
    assert bots["bots"] == [] and bots["sample_bots"] == []
    full = svc.setup_state()
    assert full["grok_app"] == {"found": True, "download_url": None}
    assert full["helper"] == {"found": False, "version": None, "source": None}
    assert full["check"] == {"done": False, "problem": None, "gh_found": False, "unattested": False, "method": None}
    assert full["problem"] is None and full["cause"] is None and full["ready"] is False
    assert full["download_url"] == "https://github.com/hudsonwa/botport/releases/tag/" + E.EXPECTED_BOTPORT_TAG
    assert full["release_tag"] == E.EXPECTED_BOTPORT_TAG
    assert full["gh_help"] == {"url": "https://cli.github.com", "commands": ["brew install gh", "gh auth login"]}
    text = json.dumps([st, bots, full])
    for word in ("PATH", "attestation", "<path>", "SHA", "allow-unattested"):
        assert word not in text, word
    # nothing was written for an install that holds no twins
    assert sorted(p.name for p in central(home).iterdir()) == []
    c = _client(monkeypatch)
    assert c.get(P + "/setup").json() == full and c.get(P + "/grok/bots").json() == bots
    # the Grok app missing is its own fact
    assert _svc(grok_app_dir=str(tmp_path / "nowhere")).setup_state()["grok_app"]["found"] is False


def test_helper_found_but_not_checked(fake):
    svc = fake.svc
    s = svc.setup_state()
    assert s["helper"] == {"found": True, "version": None, "source": "settings"}
    assert s["check"]["done"] is False and s["check"]["problem"] is None and s["ready"] is False
    bots = svc.grok_list()
    assert bots["ready"] is False and bots["helper_found"] is True and bots["helper_checked"] is False
    assert bots["bots"] == []
    assert fake.calls() == []  # an unchecked helper is never run, not even to list bots
    with pytest.raises(EngineError) as ei:
        svc.preview(["b-acme-0001"])
    assert ei.value.code == "untrusted_binary" and "has not been verified yet" in ei.value.message
    assert "Verify botport" in ei.value.hint and "allow-unattested" not in ei.value.hint


def test_check_without_githubs_tool_says_what_to_do(fake, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no gh here (and the usual extra folders are off in tests)
    with pytest.raises(EngineError) as ei:
        fake.svc.trust_botport()
    assert ei.value.code == "gh_missing"
    assert ei.value.message == "This check needs GitHub's free command-line tool, signed in once."
    assert "gh auth login" in ei.value.hint and "allow-unattested" not in ei.value.hint
    assert fake.calls() == [] and not (central(Path(os.environ["HERMES_HOME"])) / "botport-trust.json").exists()
    r = _client(monkeypatch).post(P + "/engine/trust", json={})
    # the page's plain ask never names a developer's tool: one sentence and one next act
    assert r.status_code == 200 and r.json()["error"]["code"] == "not_pinned"
    assert fake.svc.setup_state()["check"] == {"done": False, "problem": None, "gh_found": False, "unattested": False,
                                               "method": None}


def test_gh_is_found_in_the_usual_folders_and_a_miss_is_not_remembered(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    brew = tmp_path / "brewbin"
    brew.mkdir(mode=0o755)
    monkeypatch.setattr(E, "EXTRA_BIN_DIRS", (str(brew),))
    assert fake.svc.setup_state()["check"]["gh_found"] is False
    _write_exe(brew / "gh", "import sys\nsys.exit(0)\n")  # the owner installs it while the sheet is open
    assert fake.svc.setup_state()["check"]["gh_found"] is True
    done = fake.svc.trust_botport()  # the check runs with it and passes
    assert done["trusted"] is True and done["attested"] is True
    assert done["setup"]["ready"] is True and done["setup"]["helper"]["version"] == "0.1.2"
    assert fake.svc.status(fresh=True)["setup"]["helper_checked"] is True


def test_a_local_build_accepted_without_the_check_is_not_called_genuine(fake):
    svc = fake.svc
    svc.trust_botport(True)  # the explicit local-build opt-out (terminal only)
    st = svc.setup_state()
    assert st["ready"] is True and st["check"]["done"] is True and st["check"]["unattested"] is True
    assert svc.status(fresh=True)["engine"]["allow_unattested"] is True


def test_helper_changed_after_the_check_is_noticed_cheaply(fake):
    fake.svc.trust_botport(True)
    assert fake.svc.setup_state()["check"]["done"] is True
    calls = len(fake.calls())
    fake.exe.write_text(fake.exe.read_text() + "\n# changed\n")
    s = fake.svc.setup_state()
    assert s["check"]["done"] is False and s["ready"] is False
    assert s["check"]["problem"] == "botport changed since it was verified. Press Verify botport."
    assert len(fake.calls()) == calls  # noticed from a stat(), the helper did not run


# ---- "Choose the downloaded file…" --------------------------------------------------------------------------

def _archive(members: list[tuple[str, bytes]], links: tuple = ()) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            info.mtime = 946684800  # 2000-01-01
            tar.addfile(info, io.BytesIO(data))
        for name, target in links:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return buf.getvalue()


def test_choose_the_downloaded_program(home, tmp_path, monkeypatch):
    svc = _svc()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    src = downloads / "botport"
    src.write_bytes(PROGRAM)  # as a browser leaves it: not even marked as a program
    quarantined = False
    if sys.platform == "darwin":
        quarantined = subprocess.run(["/usr/bin/xattr", "-w", "com.apple.quarantine", "0083;00000000;Browser;",
                                      str(src)], capture_output=True).returncode == 0
    out = svc.choose_helper(str(src))
    dest = central(home) / "helper" / "botport"
    assert dest.read_bytes() == PROGRAM and src.read_bytes() == PROGRAM
    assert dest.stat().st_mode & 0o777 == 0o700 and dest.parent.stat().st_mode & 0o777 == 0o700
    assert out["helper"] == {"found": True, "version": None, "source": "chosen"}
    assert out["check"]["done"] is False and out["ready"] is False  # found; the genuineness check comes next
    if quarantined:  # a byte copy carries no quarantine flag, so macOS will not block Hermes' own copy
        attrs = subprocess.run(["/usr/bin/xattr", str(dest)], capture_output=True, text=True).stdout
        assert "com.apple.quarantine" not in attrs
        assert "com.apple.quarantine" in subprocess.run(["/usr/bin/xattr", str(src)], capture_output=True,
                                                        text=True).stdout
    assert svc.status(fresh=True)["setup"]["helper_found"] is True
    # through REST too, and the answer is the /setup payload
    r = _client(monkeypatch).post(P + "/engine/path", json={"path": str(src)})
    assert r.status_code == 200 and r.json()["helper"]["source"] == "chosen"


def test_choose_the_downloaded_archive(home, tmp_path):
    svc = _svc()
    t = E.release_target()
    arc = tmp_path / f"botport-{t}.tar.gz"
    arc.write_bytes(_archive([(f"botport-{t}/README.md", b"read me"), (f"botport-{t}/botport", PROGRAM)]))
    out = svc.choose_helper(str(arc))
    assert out["helper"]["found"] is True and out["helper"]["source"] == "chosen"
    assert (central(home) / "helper" / "botport").read_bytes() == PROGRAM
    # only that one member was read: nothing else from the archive is on disk
    assert sorted(p.name for p in (central(home) / "helper").iterdir()) == ["botport"]


def test_choose_refuses_what_is_not_the_helper(home, tmp_path):
    svc = _svc()
    dest = central(home) / "helper" / "botport"
    notes = tmp_path / "notes.txt"
    notes.write_text("just some text")
    link = tmp_path / "botport-link"
    real = tmp_path / "botport"
    real.write_bytes(PROGRAM)
    link.symlink_to(real)
    outside = tmp_path / "outside-target"
    outside.write_bytes(PROGRAM)
    bad_archives = {
        "traversal": _archive([("../botport", PROGRAM), ("pkg/../../botport", PROGRAM)]),
        "absolute": _archive([("/botport", PROGRAM)]),
        "link-member": _archive([], links=(("pkg/botport", str(outside)),)),
        "no-helper": _archive([("pkg/README.md", b"read me")]),
        "not-a-program": _archive([("pkg/botport", b"#!/bin/sh\necho hello\n")]),
        "broken": b"\x1f\x8b" + b"not really gzip",
    }
    cases = [str(notes), str(link), str(tmp_path), str(tmp_path / "missing"), "relative/botport", "", None, 7]
    for name, blob in bad_archives.items():
        f = tmp_path / f"{name}.tar.gz"
        f.write_bytes(blob)
        cases.append(str(f))
    for case in cases:
        with pytest.raises(EngineError) as ei:
            svc.choose_helper(case)
        # "that is not botport" has its own code, so the page can say it in the row; either way plain words
        assert ei.value.code in ("bad_input", "not_botport") and "_" not in ei.value.message, case
        assert not dest.exists(), case
    assert not (tmp_path.parent / "botport").exists() and not (central(home).parent / "botport").exists()
    assert svc.setup_state()["helper"]["found"] is False
    # a helper location named in the settings wins, and is never silently replaced
    svc = _svc(botport_path=str(real))
    with pytest.raises(service.BadInput) as ei:
        svc.choose_helper(str(real))
    assert "settings" in ei.value.message and not dest.exists()


def test_helper_lookup_order(home, tmp_path, monkeypatch):
    chosen = central(home) / "helper" / "botport"
    on_path = _write_exe(tmp_path / "pathbin" / "botport", "pass\n")
    extra = _write_exe(tmp_path / "localbin" / "botport", "pass\n")
    named = _write_exe(tmp_path / "named" / "botport", "pass\n")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert E.find_botport("", chosen) is None
    monkeypatch.setattr(E, "HELPER_EXTRA_DIRS", (str(extra.parent),))
    assert E.find_botport("", chosen) == os.path.realpath(extra)       # a Desktop app's short PATH is not the end
    monkeypatch.setenv("PATH", f"{on_path.parent}:/usr/bin:/bin")
    assert E.find_botport("", chosen) == os.path.realpath(on_path)     # PATH before the usual folders
    _svc().choose_helper(str(_program(tmp_path)))
    assert E.find_botport("", chosen) == os.path.realpath(chosen)      # the copy the owner picked before PATH
    assert E.find_botport(str(named), chosen) == os.path.realpath(named)  # the settings before everything
    assert E.find_botport(str(tmp_path / "nope"), chosen) is None      # and nothing else is tried then


def _program(tmp_path: Path) -> Path:
    p = tmp_path / "picked" / "botport"
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(PROGRAM)
    return p


# ---- the sample: always restartable, never mixed with real bots, never on a real profile ---------------------

def test_sample_starts_again_after_its_last_twin_was_removed(home):
    svc = _svc()
    svc.demo_action("start")
    out = svc.remove("acme-support", False)
    assert out["sample"] is True and out["profile_kept"] is False
    # removing the last sample twin ends the sample completely: no half-alive world, no dead end
    assert not (central(home) / "demo").exists() and svc.status(fresh=True)["sample"] == {"on": False, "hide_real": False, "instructions_example": None}
    assert svc.status(fresh=True)["pairs"] == [] and svc.status()["mode"] == "setup"
    again = svc.demo_action("start")
    assert again["created"] is True and again["did"] == "Sample twin ready"
    assert [p["pair_id"] for p in again["pairs"]] == ["acme-support"] and again["sample"]["on"] is True
    assert svc.demo_action("remove")["did"] == "Sample removed. Nothing of yours was changed"
    assert svc.demo_action("remove")["did"] == "No sample to remove"


def test_sample_world_without_its_first_twin_is_rebuilt(home):
    svc = _svc()
    svc.demo_action("start")
    svc.create(["b-recipe-0002"])
    svc.remove("acme-support", False)  # not the last twin: the world stays, without Acme Support
    assert svc.pair_ids() == ["recipe-planner"] and (central(home) / "demo").is_dir()
    with pytest.raises(EngineError):
        svc.demo_action("grok-edit")  # the try-it buttons need the Acme Support twin
    out = svc.demo_action("start")
    assert out["created"] is True and [p["pair_id"] for p in out["pairs"]] == ["acme-support"]
    assert svc.demo_action("grok-edit")["did"] == "Changed the sample bot's description in Grok"


def test_sample_actions_say_what_they_did_in_plain_words(home):
    svc = _svc()
    with pytest.raises(EngineError) as ei:
        svc.demo_action("conflict")
    assert ei.value.code == "demo_missing" and ei.value.message == "Start the sample first."
    assert "See a sample" in ei.value.hint
    svc.demo_action("start")
    assert svc.demo_action("conflict")["did"] == "Changed the skill greet-visitors in both places"
    assert svc.demo_action("secret")["did"] == "Pasted a fake password into the sample bot's description in Grok"
    assert svc.demo_action("hermes-edit")["did"] == "Changed the skill fun-fact in Hermes"
    svc.sync()
    p = _pair(svc)
    # every open matter is named, never one of several
    assert p["state"] == "needs_you" and p["state_reason"] == (
        "1 item changed in both places; 1 thing that looks like a password or key was not copied; "
        "1 change ready to send to Grok")
    blocked = svc.checklist("acme-support")["blocked_by"]
    assert blocked == 'First choose which version of the skill "greet-visitors" to keep.'


def test_sample_never_uses_the_current_profile_unless_named(home):
    svc = _svc()
    svc.demo_action("start")
    p = _pair(svc)
    assert p["hermes"]["profile"] == model.SAMPLE_PROFILE == "grok-bot-twin-sample" and p["sample"] is True
    assert svc.profile() == "default" and svc.twinned_pairs("default") == []
    svc.demo_action("remove")
    svc = _svc(demo_profile="default")  # the owner's explicit choice
    svc.demo_action("start")
    assert _pair(svc)["hermes"]["profile"] == "default" and len(svc.twinned_pairs("default")) == 1
    svc.demo_action("remove")
    svc.demo_action("start", "work")  # hermes twin demo --hermes-profile work
    assert _pair(svc)["hermes"]["profile"] == "work"


def test_real_bots_are_listed_while_the_sample_is_on(fake, monkeypatch):
    svc = fake.svc
    svc.trust_botport(True)
    svc.demo_action("start")
    res = svc.grok_list()
    assert res["ready"] is True and res["app_found"] is True and res["real_hidden"] is False
    # the owner's real bots under `bots`; the invented ones apart, marked as the sample
    assert [(b["name"], b["sample"]) for b in res["bots"]] == [("Acme Support", False)]
    assert {b["name"] for b in res["sample_bots"]} == {"Acme Support", "Recipe Planner"}
    assert all(b["sample"] and b["demo"] for b in res["sample_bots"])
    # hidden only when the owner says so, and the answer says that it is
    out = svc.demo_action("hide-real")
    assert out["sample"]["on"] is True and out["sample"]["hide_real"] is True
    assert out["did"] == "Your real bots are hidden while the sample is on"
    calls = len(fake.calls())
    res = svc.grok_list()
    assert res["bots"] == [] and res["real_hidden"] is True and len(res["sample_bots"]) == 2
    assert len(fake.calls()) == calls  # hidden means not even read
    assert svc.status(fresh=True)["sample"]["hide_real"] is True
    svc.demo_action("show-real")
    assert [b["name"] for b in svc.grok_list()["bots"]] == ["Acme Support"]
    c = _client(monkeypatch)
    assert c.post(P + "/demo", json={"action": "hide-real"}).json()["sample"]["hide_real"] is True
    assert c.get(P + "/grok/bots").json()["real_hidden"] is True
    # ending the sample ends the choice with it
    svc.demo_action("remove")
    assert svc.grok_list()["real_hidden"] is False and svc.grok_list()["sample_bots"] == []
    with pytest.raises(EngineError):
        svc.demo_action("hide-real")


# ---- adding a twin: three plain lines, a free name by itself, one twin per bot -------------------------------

def test_preview_lines():
    entry = {"items": [{"kind": "name", "action": "create"}, {"kind": "description", "action": "create"},
                       {"kind": "instructions", "action": "by_hand"}, {"kind": "routine", "action": "by_hand"},
                       {"kind": "skill", "action": "via_mirror"}, {"kind": "skill", "action": "via_mirror"}]}
    assert model.preview_lines(entry) == [
        "Copies now: name and description.",
        "Later, with the page's help: instructions.",
        'Optional: 1 routine, from the twin\'s "…" menu.',
        "2 skills: optional, set up later."]
    entry = {"items": [{"kind": "name", "action": "create"}, {"kind": "description", "action": "held_back",
                                                              "item": "persona.description"},
                       {"kind": "routine", "action": "by_hand"}, {"kind": "routine", "action": "by_hand"},
                       {"kind": "connector", "action": "by_hand"}]}
    assert model.preview_lines(entry) == [
        "Copies now: name.",
        "Later, with the page's help: instructions, 1 connection.",
        'Optional: 2 routines, from the twin\'s "…" menu.',
        "1 thing that looks like a password or key will not be copied."]
    # the reasons moved out of the summary, next to the items they belong to
    assert model.preview_note("routine", "by_hand") == "arrives switched off, so nothing runs twice"
    assert model.preview_note("connector", "by_hand") == "logins are never copied"
    assert model.preview_note("routine", "create") is None and model.preview_note("skill", "by_hand") is None
    # honest scope: instructions are never claimed to copy unless the preview says so
    assert model.preview_lines({}) == ["Copies now: nothing yet.", "Later, with the page's help: instructions."]
    cache_only = {"items": [{"kind": "name", "action": "create"}, {"kind": "description", "action": "create"}]}
    assert model.preview_lines(cache_only)[1] == "Later, with the page's help: instructions."
    from_export = {"items": [{"kind": "name", "action": "create"}, {"kind": "instructions", "action": "create"}]}
    assert model.preview_lines(from_export) == ["Copies now: name and instructions.", "Nothing is left for later."]
    assert model.preview_lines(None) == ["Copies now: nothing yet.", "Later, with the page's help: instructions."]


def test_preview_and_create_through_rest(home, monkeypatch):
    _svc()
    c = _client(monkeypatch)
    c.post(P + "/demo", json={"action": "start"})
    pv = c.post(P + "/twins/preview", json={"grok_ids": ["b-recipe-0002"]}).json()
    pair = pv["pairs"][0]
    assert pair["lines"] == [
        "Copies now: name and description.",
        "Later, with the page's help: instructions.",
        'Optional: 1 routine, from the twin\'s "…" menu.',
        "2 skills: optional, set up later."]
    notes = {i["item"]: i.get("note") for i in pair["items"]}
    assert notes["routine:Sunday meal plan"] == "arrives switched off, so nothing runs twice"
    assert notes["persona.name"] is None
    assert pair["sample"] is True and pair["hermes"]["profile"] == "grok-bot-twin-sample-2"
    made = c.post(P + "/twins/create", json={"grok_ids": ["b-recipe-0002"]}).json()
    assert [p["pair_id"] for p in made["pairs"]] == ["recipe-planner"]
    # one bot, one twin: REST refuses a second twin of the same bot (the page blocked it; the API does too)
    again = c.post(P + "/twins/preview", json={"grok_ids": ["b-recipe-0002"]}).json()
    assert again["pairs"] == [] and again["skipped"][0]["reason"] == "already_twinned"
    assert again["skipped"][0]["name"] == "Recipe Planner"  # the page can say which bot was left out
    r = c.post(P + "/twins/create", json={"grok_ids": ["b-recipe-0002"]})
    assert r.status_code == 200 and r.json()["error"]["code"] == "already_twinned"
    assert r.json()["error"]["message"] == "That bot already has a twin."
    assert [p["pair_id"] for p in c.get(P + "/status").json()["pairs"]] == ["acme-support", "recipe-planner"]


def test_taken_name_is_fixed_by_itself_for_a_real_bot(home, monkeypatch):
    svc = _svc()
    calls = []

    class Helper:
        def create(self, gid, profile, dry_run):
            calls.append((gid, profile, dry_run))
            return {"pairs": [{"pair_id": "recipe-planner", "grok": {"id": gid, "name": "Recipe Planner"},
                               "hermes": {"profile": profile or "recipe-planner"}, "items": []}], "skipped": []}

    svc._remember([{"pair_id": "old-planner", "grok": {"id": "b-old-0009", "name": "Old Planner"},
                    "hermes": {"profile": "recipe-planner"}}])
    monkeypatch.setattr(svc, "botport", lambda: Helper())
    prev = svc.preview(["b-recipe-0002"])
    assert prev["skipped"] == [] and prev["pairs"][0]["hermes"]["profile"] == "recipe-planner-2"
    assert calls[-1] == ("b-recipe-0002", "recipe-planner-2", True)
    # a name the owner typed is not second-guessed: taken means refused
    prev = svc.preview(["b-recipe-0002"], {"b-recipe-0002": "recipe-planner"})
    assert prev["pairs"] == [] and prev["skipped"][0]["reason"] == "profile_already_paired"


# ---- "Ready to send to Grok": send it, or keep it in Hermes only (and change your mind) ------------------------

def test_keep_in_hermes_only_and_undo(home, monkeypatch):
    svc = _svc()
    svc.demo_action("start")
    svc.demo_action("hermes-edit")
    p = _pair(svc)
    assert p["waiting"] == [{"item": "skill:fun-fact", "reason": "needs_approval", "to": "grok",
                             "detail": "You changed the skill fun-fact in Hermes."}]
    assert p["state"] == "needs_you" and p["state_reason"] == "1 change ready to send to Grok" and p["skipped"] == []
    grok_before = svc.demo._grok("b-acme-0001")["skills"]["fun-fact"]["text"]
    assert svc.skip("acme-support", "skill:fun-fact") == {"pair_id": "acme-support", "skipped": ["skill:fun-fact"]}
    p = _pair(svc)
    assert p["waiting"] == [] and p["pending"]["waiting_approval"] == 0 and p["skipped"] == ["skill:fun-fact"]
    assert p["state"] == "in_step" and p["state_reason"] == "Up to date"
    r = svc.sync("acme-support")["pairs"][0]  # a sync does not ask again either
    assert r["waiting"] == [] and r["state"] == "in_step"
    # nothing was sent and nothing was lost: Grok keeps its version, Hermes keeps the changed one
    assert svc.demo._grok("b-acme-0001")["skills"]["fun-fact"]["text"] == grok_before
    assert "sign-off" in svc.demo._twin("acme-support")["skills"]["fun-fact"]["text"]
    assert svc.skip("acme-support", "skill:fun-fact", True)["skipped"] == []
    p = _pair(svc)
    assert [w["item"] for w in p["waiting"]] == ["skill:fun-fact"] and p["state"] == "needs_you"
    c = _client(monkeypatch)
    r = c.post(P + "/pairs/acme-support/skip", json={"item": "skill:fun-fact"}).json()
    assert r["skipped"] == ["skill:fun-fact"]
    assert c.post(P + "/pairs/acme-support/skip", json={"item": "skill:fun-fact", "undo": True}).json()["skipped"] == []
    with pytest.raises(service.BadInput):
        svc.skip("acme-support", "../x")
    with pytest.raises(service.PairNotFound):
        svc.skip("nobody", "skill:fun-fact")
    svc.skip("acme-support", "skill:fun-fact")
    svc.remove("acme-support", False)  # the note goes with the twin
    assert not (central(home) / "skipped.json").exists() or json.loads(
        (central(home) / "skipped.json").read_text()) == {}


# ---- "Move to Hermes" leaves a trace and can be undone ----------------------------------------------------------

def test_move_to_hermes_and_undo_in_the_sample(home):
    svc = _svc()
    svc.demo_action("start")
    assert _pair(svc)["switched"] is None
    jobs = [j["job_id"] for j in svc.checklist("acme-support")["switch"]["hermes_jobs_paused"]]
    assert len(jobs) == 2 and svc.checklist("acme-support")["blocked_by"] is None
    assert svc.switch("acme-support", jobs) == {"resumed": jobs, "failed": []}
    sw = _pair(svc)["switched"]
    assert sw["routines_on"] == 2 and model.parse_iso(sw["at"]) is not None
    ck = svc.checklist("acme-support")["switch"]
    assert ck["hermes_jobs_paused"] == [] and [j["job_id"] for j in ck["hermes_jobs_resumed"]] == jobs
    assert svc.switch("acme-support", undo=True) == {"paused": jobs, "failed": []}
    assert _pair(svc)["switched"] is None
    assert [j["job_id"] for j in svc.checklist("acme-support")["switch"]["hermes_jobs_paused"]] == jobs
    assert svc.switch("acme-support", undo=True) == {"paused": [], "failed": []}  # nothing left to undo
    with pytest.raises(service.BadInput):
        svc.switch("acme-support", "all")
    with pytest.raises(service.BadInput):
        svc.switch("acme-support", jobs, "yes")


def _job(jid, paused=True, reason=None):
    return {"id": jid, "name": "Acme Support: Morning summary", "enabled": not paused,
            "state": "paused" if paused else "scheduled", "paused_reason": reason,
            "schedule": {"kind": "cron", "expr": "0 8 * * *", "display": "0 8 * * *"}}


def test_move_to_hermes_and_undo_for_a_real_twin(fake, home, monkeypatch):
    svc = fake.svc
    svc.trust_botport(True)
    jobs_file = home / "profiles" / "acme-support" / "cron" / "jobs.json"
    jobs_file.parent.mkdir(parents=True)
    mine = _job("a1b2c3d4e5f6", reason="Imported by botport from the Grok bot; resume once the Grok routine is off")
    other = _job("deadbeef0002", paused=False)  # the owner's own running job: never touched
    jobs_file.write_text(json.dumps({"jobs": [mine, other]}))
    svc.create(["b-acme-0001"])
    done = []

    def fake_cron(action):
        def run(job_id, profile, ctx=None):
            done.append((action, job_id, profile))
            data = json.loads(jobs_file.read_text())
            for j in data["jobs"]:
                if j["id"] == job_id:
                    j["enabled"], j["state"] = action == "resume", "scheduled" if action == "resume" else "paused"
            jobs_file.write_text(json.dumps(data))
            return True, ""
        return run

    monkeypatch.setattr(importer, "resume_cron", fake_cron("resume"))
    monkeypatch.setattr(importer, "pause_cron", fake_cron("pause"))
    assert svc.switch("acme-support", ["a1b2c3d4e5f6"])["resumed"] == ["a1b2c3d4e5f6"]
    assert svc.switch("acme-support", ["deadbeef0002"])["failed"][0]["error"] == "not a switched-off routine of this twin"
    p = _pair(svc)
    assert p["switched"]["routines_on"] == 1 and p["sample"] is False
    assert [j["job_id"] for j in svc.checklist("acme-support")["switch"]["hermes_jobs_resumed"]] == ["a1b2c3d4e5f6"]
    assert svc.status(fresh=True) and _pair(svc)["switched"] is not None  # kept across status refreshes
    out = svc.switch("acme-support", undo=True)
    assert out == {"paused": ["a1b2c3d4e5f6"], "failed": []}
    assert done == [("resume", "a1b2c3d4e5f6", "acme-support"), ("pause", "a1b2c3d4e5f6", "acme-support")]
    assert _pair(svc)["switched"] is None
    assert [j["job_id"] for j in svc.checklist("acme-support")["switch"]["hermes_jobs_paused"]] == ["a1b2c3d4e5f6"]
    assert json.loads(jobs_file.read_text())["jobs"][1] == other


def test_pause_cron_has_the_same_rules_as_resume(home):
    assert importer.pause_cron("-h", "default")[0] is False
    assert importer.pause_cron("a1b2c3d4e5f6", "../x")[0] is False

    class Ctx:
        profile_name = "default"
        seen = []

        def dispatch_tool(self, name, args):
            self.seen.append((name, args))
            return json.dumps({"success": True})

    ctx = Ctx()
    assert importer.pause_cron("a1b2c3d4e5f6", "default", ctx) == (True, "")
    assert ctx.seen == [("cronjob_manage", {"action": "pause", "job_id": "a1b2c3d4e5f6"})]
    ok, why = importer.pause_cron("a1b2c3d4e5f6", "acme-support", ctx)  # another profile: its approval runs there
    assert ok is False and "acme-support" in why and len(ctx.seen) == 1


# ---- compare with Grok ---------------------------------------------------------------------------------------

def test_compare_with_grok(fake, home):
    svc = fake.svc
    svc.demo_action("start")
    sample = svc.parity("acme-support")
    # a new twin: everything matches but the instructions, which the owner still has to bring over
    left = [c for c in sample["checks"] if not c["pass"]]
    assert [(c["id"], c["action"]) for c in left] == [("persona.instructions", "set_instructions")]
    svc.set_instructions("acme-support", "You are Acme Support.\nBe friendly and brief.")
    sample = svc.parity("acme-support")
    assert sample["passed"] == sample["total"] and not any("model" in c["id"] for c in sample["checks"])
    by_id = {c["id"]: c for c in sample["checks"]}
    assert by_id["persona.instructions"]["detail"] == "you keep these yourself; Hermes cannot compare them with Grok"
    assert all(c["detail"] == "the same in Grok and Hermes" for c in sample["checks"]
               if c["id"] in ("persona.name", "persona.description"))
    svc.demo_action("remove")
    svc.trust_botport(True)
    svc.status(fresh=True)
    real = svc.parity("acme-support")
    model_check = next(c for c in real["checks"] if c["id"] == "model")
    assert model_check["name"] == "No model chosen for this twin yet" and model_check["action"] == "choose_model"
    assert model_check["pass"] is False
    assert real["checks"][0]["id"] == "persona.instructions" and real["checks"][0]["action"] == "set_instructions"
    assert "action" not in real["checks"][1]


# ---- skill sync: files to show, folders in plain words ----------------------------------------------------------

def test_skill_sync_reveals_only_real_files_of_this_twin(fake, home, tmp_path):
    svc = fake.svc
    svc.trust_botport(True)
    svc.status(fresh=True)
    folder = tmp_path / "shared"
    folder.mkdir()
    out = svc.mirror("acme-support", str(folder))
    assert out["connected"] is True and out["reveal"] == {"key_file": None, "guide_file": None}  # not there yet
    pairs = central(home) / "botport-state" / "pairs"
    (pairs / "acme-support.brief").mkdir(parents=True)
    (pairs / "acme-support.key").write_text("0" * 64)
    (pairs / "acme-support.brief" / "AGENT-MIRROR.md").write_text("# guide")
    out = svc.mirror("acme-support", str(folder))
    assert out["reveal"] == {"key_file": str(pairs / "acme-support.key"),
                             "guide_file": str(pairs / "acme-support.brief" / "AGENT-MIRROR.md")}
    assert "0" * 64 not in json.dumps(out)  # the path, never the key
    assert svc._reveal("acme-support", {"pair_key": "state:../../../etc/hosts"})["key_file"] == str(pairs / "acme-support.key")
    assert svc._reveal("other", {"pair_key": "state:/etc/hosts", "briefs": ["state:../x/AGENT-MIRROR.md"]}) == {
        "key_file": None, "guide_file": None}
    (pairs / "linked.key").symlink_to(pairs / "acme-support.key")
    assert svc._reveal("linked", {})["key_file"] is None
    for bad, words in ((str(tmp_path / "missing"), "Choose a folder that exists."),
                       ("relative", "Choose a folder that exists."),
                       (str(home), "Choose a folder outside Hermes' own folders.")):
        with pytest.raises(service.BadInput) as ei:
            svc.mirror("acme-support", bad)
        assert ei.value.message == words
    link = tmp_path / "shortcut"
    link.symlink_to(folder)
    with pytest.raises(service.BadInput) as ei:
        svc.mirror("acme-support", str(link))
    assert ei.value.message == "Choose the real folder, not a shortcut to it."
    assert svc.mirror("acme-support", None, True)["reveal"] == {"key_file": None, "guide_file": None}


# ---- leaving: one twin, everything ---------------------------------------------------------------------------

def test_stop_twinning_says_what_stays_and_what_goes(fake, home, monkeypatch):
    svc = fake.svc
    svc.trust_botport(True)
    svc.create(["b-acme-0001"])
    plan = svc.remove("acme-support", True)
    assert plan["sample"] is False and plan["profile_kept"] is True and plan["dry_run"] is True
    assert plan["stays"] == ["Your bot in Grok, unchanged.", "Its Hermes bot and all its chats."]
    # only what exists is listed, and nothing about the twin's small internal file (it still goes: see `removed`)
    assert plan["goes"] == ["Following Grok.", "The earlier versions kept for undo."]
    assert plan["again"] == "You can twin it again later; it picks up this Hermes bot again."
    assert plan["skill_sync"] is False
    assert plan["removed"] == ["state:pairs/acme-support.toml", "state:pairs/acme-support.key"]  # behind "Show files"
    assert "acme-support" in svc.pair_ids()  # a preview changes nothing
    r = _client(monkeypatch).post(P + "/pairs/acme-support/remove", json={"dry_run": True}).json()
    assert r["stays"] == plan["stays"] and r["goes"] == plan["goes"]
    svc.demo_action("start")
    sample = svc.remove("demo-acme-support", True)
    assert sample["sample"] is True and sample["profile_kept"] is False and sample["again"] == ""
    assert sample["skill_sync"] is False
    assert sample["stays"] == ["Everything of yours. The sample holds invented data only."]


def test_remove_everything(fake, home, monkeypatch):
    svc = fake.svc
    svc.trust_botport(True)
    svc.create(["b-acme-0001"])
    svc.demo_action("start")
    pairs = central(home) / "botport-state" / "pairs"
    pairs.mkdir(parents=True, exist_ok=True)
    (pairs / "acme-support.key").write_text("0" * 64)  # the twin's key file
    (central(home) / "helper").mkdir()
    (central(home) / "helper" / "botport").write_bytes(PROGRAM)
    profile = home / "profiles" / "acme-support"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "SOUL.md").write_text("the Hermes copy")
    (home / "sessions.db").write_text("chats")
    before = sorted(p.name for p in central(home).iterdir())
    plan = svc.uninstall(True)
    assert plan["dry_run"] is True and plan["twins"] == 1 and plan["sample"] is True and plan["skill_sync"] == []
    assert plan["stays"] == ["Your Grok bots.", "Your Hermes bots and their chats."]
    assert plan["goes"] == ["1 twin (Acme Support).", "Earlier versions of 1 twin.", "The sample.",
                            "Hermes' copy of botport (your download stays in Downloads)."]
    assert plan["next"] == "Switch the plugin off in Settings → Capabilities → Plugins." and plan["routines_on"] == 0
    assert "state:pairs/acme-support.key" in plan["removed"] and "data:botport-trust.json" in plan["removed"]
    assert "0" * 64 not in json.dumps(plan)
    assert sorted(p.name for p in central(home).iterdir()) == before and "acme-support" in svc.pair_ids()
    assert _client(monkeypatch).post(P + "/uninstall", json={"dry_run": True}).json()["twins"] == 1
    removed_by_helper = []
    bp = svc.botport()
    real_remove = bp.remove
    monkeypatch.setattr(bp, "remove", lambda pid, dry=False: (removed_by_helper.append((pid, dry)),
                                                              real_remove(pid, dry))[1])
    done = svc.uninstall(False)
    assert done["dry_run"] is False and done["twins"] == 1 and "note" not in done
    assert removed_by_helper == [("acme-support", False)]  # each real twin is unlinked through the helper first
    # the plugin's data folder is empty: links, key files, earlier versions, sample, helper copy, check record
    assert sorted(p.name for p in central(home).iterdir()) == []
    # the owner's own things are untouched
    assert (profile / "SOUL.md").read_text() == "the Hermes copy" and (home / "sessions.db").read_text() == "chats"
    st = svc.status(fresh=True)
    assert st["pairs"] == [] and st["sample"]["on"] is False and st["setup"]["helper_checked"] is False
    assert sorted(p.name for p in central(home).iterdir()) == []  # looking at the page afterwards leaves nothing
    assert svc.uninstall(True)["twins"] == 0 and svc.uninstall(False)["removed"] == []


def test_remove_everything_without_a_usable_helper(home, tmp_path):
    svc = _svc()
    svc._remember([{"pair_id": "acme-support", "grok": {"id": "b-acme-0001", "name": "Acme Support"},
                    "hermes": {"profile": "acme-support"}}])
    pairs = central(home) / "botport-state" / "pairs"
    pairs.mkdir(parents=True)
    (pairs / "acme-support.key").write_text("0" * 64)
    out = svc.uninstall(False)
    assert out["twins"] == 1 and "note" in out and "state:pairs/acme-support.key" in out["removed"]
    assert sorted(p.name for p in central(home).iterdir()) == []  # the key file is gone all the same


def test_remove_everything_mentions_the_helper_bot_when_skill_sync_was_on(home, monkeypatch):
    svc = _svc()
    svc._remember([{"pair_id": "acme-support", "grok": {"id": "b-acme-0001", "name": "Acme Support"},
                    "hermes": {"profile": "acme-support"}}])
    real = svc.status

    def status(fresh=False, allow_engine=True):
        st = real(fresh, allow_engine)
        for p in st["pairs"]:
            p["mirror_bot"] = {"connected": True}
        return st

    monkeypatch.setattr(svc, "status", status)
    out = svc.uninstall(True)
    assert out["skill_sync"] == ["Acme Support"]
    assert out["goes_note"] == "In Grok, delete the sync bot's routine yourself; Hermes cannot reach it."


# ---- doctor: the plugin's own lines are shown as written -------------------------------------------------------

def test_doctor_fix_text_has_nothing_to_mask_and_no_path(home, tmp_path):
    # the advice names the one thing to do; it carries no file path (so nothing can be masked as "[secret]", and
    # no folder of this computer travels in what the page receives). `hermes twin setup` prints the exact check.
    odd = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"
    exe = _write_exe(tmp_path / odd / "botport", FAKE.read_text(encoding="utf-8"))
    os.chmod(tmp_path, 0o700)
    svc = _svc(botport_path=str(exe))
    svc.trust_botport(True)
    att = next(c for c in svc.doctor()["checks"] if c["id"] == "botport_attestation")
    assert att["ok"] is False and odd not in att["fix"] and str(tmp_path) not in dumps(svc.doctor())
    assert "[secret]" not in att["fix"] and "[path]" not in att["fix"] and "<path" not in att["fix"]
    assert att["fix"] == "Press Verify botport on the Twins page, or run `hermes twin setup`."
    assert "allow-unattested" not in att["fix"]
    # what the helper itself reports is still cleaned as untrusted text
    own = next(c for c in svc.doctor()["checks"] if c["id"] == "state_dir")
    assert own["message"] == "state folder is fine"


def test_hints_have_real_values_and_no_unsafe_advice():
    for text in (E.INSTALL_HINT, E.TRUST_HINT):
        assert "<" not in text and "allow-unattested" not in text and "PATH" not in text
        assert "attestation" not in text
    steps = E.terminal_steps()
    # download, then hand the archive to the plugin as it is: no line unpacks it or copies a program anywhere
    assert len(steps) == 2 and steps[-1] == f"hermes twin setup --file botport-{E.release_target()}.tar.gz"
    assert not any("tar " in s or " cp " in s or "&&" in s or "mkdir" in s for s in steps)
    assert all("<" not in s and "$" not in s for s in steps) and E.EXPECTED_BOTPORT_TAG in steps[0]
    assert E.release_target() in steps[0] and E.release_target() in steps[1]
    assert E.terminal_steps("v9.9.9; echo x")[0] == steps[0]  # a tag that is not a tag never reaches a command
    assert E.release_url() == f"https://github.com/hudsonwa/botport/releases/tag/{E.EXPECTED_BOTPORT_TAG}"
    assert E.release_url("not a tag") == E.release_url()
