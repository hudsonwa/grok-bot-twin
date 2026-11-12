"""What the Twins page needs from the backend, topic by topic: a pinned download and its one cause when it is
missing, a twin that is started and then finished, skills copied by hand, moving routines, leaving, both sides.

Invented bots only (Acme Support, Recipe Planner). Every test has its own HERMES_HOME, HOME (so "Downloads" is a
folder of the test's own), Grok app folder and pinned-release table. "botport" is tests/fake_botport.py or a file
that is never executed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from twin_testlib import central, config, fake_key, home, service  # noqa: F401
from test_ux_flows import FAKE, P, PROGRAM, _archive, _client, _job, _pair, _svc, _write_exe, fake  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import connect, hermes_side as hs, importer, pins
from hermes_plugins.grok_bot_twin.twin import engine as E
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

TAG = E.EXPECTED_BOTPORT_TAG
VERSION = TAG[1:]
SOUL = ("<!-- botport:begin persona-description -->\nAnswers questions about Acme widgets.\n"
        "<!-- botport:end persona-description -->\n<!-- botport:begin persona-name -->\nAcme Support\n"
        "<!-- botport:end persona-name -->\n")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pin(programs: dict | None = None, archives: dict | None = None, tag: str = TAG) -> None:
    pins.set_table({"releases": {tag: {"programs": programs or {}, "archives": archives or {}}}})


def runnable() -> bytes:
    """The fake botport as one runnable file (a script; the tests that use it widen what counts as a program)."""
    return f"#!{sys.executable} -I\n{FAKE.read_text(encoding='utf-8')}".encode()


def calls(home: Path) -> list:
    f = central(home) / "botport-state" / "fake_calls.jsonl"
    return [json.loads(line)["argv"] for line in f.read_text().splitlines()] if f.exists() else []


def mode(home: Path, **fields) -> None:
    d = central(home) / "botport-state"
    d.mkdir(parents=True, exist_ok=True)
    (d / "fake_mode.json").write_text(json.dumps(fields), encoding="utf-8")


def profile(home: Path, name: str = "acme-support") -> Path:
    d = home / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    if not (d / "SOUL.md").exists():
        (d / "SOUL.md").write_text(SOUL, encoding="utf-8")
    return d


def err(fn, *a, **k) -> EngineError:
    with pytest.raises(EngineError) as ei:
        fn(*a, **k)
    return ei.value


@pytest.fixture
def twin(fake, home):
    """One real twin (through the fake botport, accepted as a local build) with its Hermes folder."""
    fake.svc.trust_botport(True)
    profile(home)
    fake.svc.create(["b-acme-0001"])
    return fake.svc


# =====================================================================================================================
# botport is verified without GitHub's tool
# =====================================================================================================================

def test_a_pinned_download_connects_without_githubs_tool(home, tmp_path, monkeypatch):
    monkeypatch.setattr(connect, "_PROGRAM_MAGIC", (*connect._PROGRAM_MAGIC, b"#!"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no gh anywhere
    prog = runnable()
    target = pins.platform_target()
    name = pins.asset_name(target)
    archive = _archive([(f"botport-{target}/botport", prog), (f"botport-{target}/README.md", b"read me")])
    pin({target: sha(prog)}, {name: sha(archive)})
    svc = _svc()
    data = central(home)
    # one flipped byte: not the genuine file. Nothing is kept and nothing ran.
    bad = bytearray(archive)
    bad[len(bad) // 2] ^= 0x01
    (tmp_path / "damaged.tar.gz").write_bytes(bytes(bad))
    e = err(svc.choose_helper, str(tmp_path / "damaged.tar.gz"))
    assert e.code in ("not_genuine", "not_botport")  # (a flipped byte may also break the archive itself)
    tampered = _archive([(f"botport-{target}/botport", prog + b"\n# one more line")])
    (tmp_path / "tampered.tar.gz").write_bytes(tampered)
    e = err(svc.choose_helper, str(tmp_path / "tampered.tar.gz"))
    assert e.code == "not_genuine" and e.message == f"That file is not the genuine botport {VERSION}."
    assert e.hint == "Download it again with the Download botport button, then choose the new file."
    # not botport at all
    (tmp_path / "notes.txt").write_text("shopping list")
    e = err(svc.choose_helper, str(tmp_path / "notes.txt"))
    assert (e.code, e.message) == ("not_botport", "That is not botport.")
    assert e.hint == "Choose the file you downloaded with the Download botport button."
    assert sorted(p.name for p in data.iterdir()) == [] and calls(home) == []
    # the genuine archive: ready at once, verified by the plugin's own table
    (tmp_path / name).write_bytes(archive)
    res = svc.choose_helper(str(tmp_path / name))
    assert res["ready"] is True and res["helper"] == {"found": True, "version": "0.1.2", "source": "chosen"}
    assert res["check"] == {"done": True, "problem": None, "gh_found": False, "unattested": False, "method": "pinned"}
    assert res["download"]["found"] is None and res["problem"] is None and res["cause"] is None
    rec = json.loads((data / "botport-trust.json").read_text())
    assert rec["pinned"] == {"tag": TAG} and rec["allow_unattested"] is False and rec["release_tag"] == TAG
    assert rec["sha256"] == sha(prog) and (data / "helper" / "botport").read_bytes() == prog
    assert [c[0] for c in calls(home)] == ["version"]  # the first and only run came after the match
    assert svc.grok_list()["bots"][0]["name"] == "Acme Support"  # and it is usable
    view = svc.trust_info()
    assert view["trusted"] and view["method"] == "pinned" and view["attested"] and not view["allow_unattested"]
    att = next(c for c in svc.doctor()["checks"] if c["id"] == "botport_attestation")
    assert att["ok"] is True and TAG in att["message"]
    # a record cannot claim "pinned" by itself: with the digest gone from the table, botport is not run any more
    pin({target: "0" * 64})
    assert svc.setup_state()["check"]["done"] is False
    assert err(svc.botport().run, ["version"]).code == "untrusted_binary"


def test_the_bare_program_and_a_repacked_archive_are_judged_by_the_program(home, tmp_path, monkeypatch):
    monkeypatch.setattr(connect, "_PROGRAM_MAGIC", (*connect._PROGRAM_MAGIC, b"#!"))
    prog = runnable()
    target = pins.platform_target()
    pin({target: sha(prog)})  # only the program is pinned
    svc = _svc()
    (tmp_path / "botport").write_bytes(prog)
    assert svc.choose_helper(str(tmp_path / "botport"))["check"]["method"] == "pinned"
    svc.uninstall(False)
    repacked = _archive([("anything/botport", prog)])
    (tmp_path / "repacked.tar.gz").write_bytes(repacked)
    assert _svc().choose_helper(str(tmp_path / "repacked.tar.gz"))["ready"] is True


def test_botport_for_another_kind_of_computer_is_named_as_that(home, tmp_path):
    other = next(t for t in pins.TARGETS if t != pins.platform_target())
    pin({other: sha(PROGRAM)})
    (tmp_path / "botport").write_bytes(PROGRAM)
    svc = _svc()
    e = err(svc.choose_helper, str(tmp_path / "botport"))
    assert e.code == "not_genuine" and e.message == "That is botport for another kind of computer."
    assert "for this computer" in e.hint and not (central(home) / "helper").exists()


def test_a_release_that_is_not_in_the_table_keeps_todays_behaviour(home, tmp_path):
    """No digests for the expected release (or another release named in the settings): the file is kept, it is
    not verified yet, and verifying it is GitHub's check, exactly as before the table existed."""
    pin({}, {})
    svc = _svc()
    (tmp_path / "botport").write_bytes(PROGRAM)
    res = svc.choose_helper(str(tmp_path / "botport"))
    assert res["helper"]["found"] is True and res["check"]["done"] is False and res["ready"] is False
    assert err(svc.trust_botport).code == "gh_missing"


def test_verify_botport_uses_the_table_first(fake, home, monkeypatch):
    """A botport that sits on PATH or is named in the settings: Verify botport passes without GitHub's tool when
    it is the pinned file; a miss never sends the owner to install that tool."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    svc = fake.svc
    target = pins.platform_target()
    pin({target: "f" * 64})
    e = err(svc.trust_botport)
    assert (e.code, e.message) == ("check_failed", f"This botport is not the release Hermes expects ({VERSION}).")
    assert e.hint == "Press Download botport and choose the new file." and fake.calls() == []
    e = err(svc.trust_botport, False, None, "gh")  # asked for on purpose: now its own plain error
    assert e.code == "gh_missing"
    pin({target: sha(Path(fake.exe).read_bytes())})
    c = _client(monkeypatch)
    r = c.post(P + "/engine/trust", json={}).json()
    assert r["trusted"] is True and r["method"] == "pinned" and r["attested"] is True
    assert r["setup"]["ready"] is True and r["setup"]["check"]["method"] == "pinned"
    assert c.post(P + "/engine/trust", json={"method": "gh"}).json()["error"]["code"] == "gh_missing"
    assert c.post(P + "/engine/trust", json={"method": "magic"}).json()["error"]["code"] == "bad_input"
    # a local build accepted in the terminal says so, and is not called verified
    pin({target: "f" * 64})
    assert svc.trust_botport(True)["method"] == "local"
    assert svc.setup_state()["check"] == {"done": True, "problem": None, "gh_found": False, "unattested": True,
                                          "method": "local"}


# =====================================================================================================================
# One download, found by itself; one cause
# =====================================================================================================================

def test_setup_names_the_download_for_this_computer(home, monkeypatch):
    svc = _svc()
    s = svc.setup_state()
    name = pins.asset_name(pins.platform_target())
    assert s["download"] == {"url": f"https://github.com/hudsonwa/botport/releases/download/{TAG}/{name}",
                             "file_name": name, "found": None, "dir": None}
    assert s["download_url"].endswith("/releases/tag/" + TAG)  # the release page stays, as the fallback
    assert s["grok_app"] == {"found": True, "download_url": None}
    monkeypatch.setattr(pins, "platform_target", lambda *a: None)
    s = _svc().setup_state()
    assert s["download"] is None and s["cause"] == "no_build" and s["ready"] is False
    assert s["problem"] == "There is no botport for this kind of computer yet."
    assert _svc().status(fresh=True)["setup"]["cause"] == "no_build"


def test_the_grok_apps_download_page_is_fixed_text(monkeypatch):
    assert connect.grok_app_download_url() is None or connect.grok_app_download_url().startswith("https://")
    monkeypatch.setattr(connect, "GROK_APP_DOWNLOAD_URLS", {"Darwin": "https://apps.example.invalid/grok",
                                                            "Linux": "http://apps.example.invalid/grok"})
    assert connect.grok_app_download_url("Darwin") == "https://apps.example.invalid/grok"
    assert connect.grok_app_download_url("Linux") is None  # not https: no button
    assert connect.grok_app_download_url("Windows") is None  # not known for that system: no button


def test_one_cause_when_twins_cannot_follow_grok(fake, home, tmp_path):
    svc = fake.svc
    assert svc.status(fresh=True)["setup"]["cause"] is None  # never set up: the Connect sheet covers it
    svc.trust_botport(True)
    profile(home)
    svc.create(["b-acme-0001"])
    st = svc.status(fresh=True)
    assert st["setup"] == {"ready": True, "grok_app_found": True, "helper_found": True, "helper_checked": True,
                           "problem": None, "cause": None}
    assert st["pairs"][0]["state"] == "in_step"
    # the Grok desktop app is gone
    gone = _svc(botport_path=str(fake.exe), grok_app_dir=str(tmp_path / "nowhere"))
    st = gone.status(fresh=True)
    assert st["setup"]["ready"] is False and st["setup"]["cause"] == "grok_app"
    assert st["setup"]["problem"] == "The Grok desktop app is not on this computer any more."
    p = st["pairs"][0]
    assert p["state"] == "error" and p["error"] == {"code": "grok_app_missing", "message": st["setup"]["problem"]}
    assert gone.setup_state()["cause"] == "grok_app" and gone.grok_list()["ready"] is False
    n = len(fake.calls())
    gone.status(fresh=True)
    assert len(fake.calls()) == n  # botport is not run for twins it cannot check anyway
    assert gone.checklist("acme-support")["blocked_by"] == (
        "Acme Support is not following Grok right now. The Grok desktop app is not on this computer any more.")
    # botport changed on disk: it has to be verified again
    svc = _svc(botport_path=str(fake.exe))
    with open(fake.exe, "a") as fh:
        fh.write("\n# updated\n")
    st = svc.status(fresh=True)
    assert st["setup"]["cause"] == "botport_unverified" and st["setup"]["ready"] is False
    assert st["setup"]["problem"] == "botport was updated and has to be verified again."
    assert st["pairs"][0]["error"]["code"] == "untrusted_binary"
    # verified again: the twins come back by themselves
    svc.trust_botport(True)
    st = svc.status(fresh=True)
    assert st["setup"]["ready"] is True and st["setup"]["cause"] is None and st["pairs"][0]["state"] == "in_step"
    # botport is gone
    os.unlink(fake.exe)
    st = _svc(botport_path=str(fake.exe)).status(fresh=True)
    assert st["setup"]["cause"] == "botport_missing" and st["setup"]["problem"] == "botport is no longer on this computer."
    assert st["pairs"][0]["error"]["code"] == "botport_missing" and st["pairs"][0]["state"] == "error"


def test_a_twin_that_fails_alone_says_the_one_thing_to_do(twin, home):
    mode(home, mode="error")  # botport answers, but cannot read this twin
    st = twin.status(fresh=True)
    assert st["setup"]["cause"] is None and st["setup"]["ready"] is True
    p = st["pairs"][0]
    assert p["state"] == "error" and p["error"]["hint"] == "Open the Grok app once, then press Try again."
    assert twin.checklist("acme-support")["blocked_by"] == (
        "First press Try again for Acme Support; Hermes cannot check it right now.")
    assert err(twin.switch, "acme-support", []).code == "blocked"


def test_the_download_is_found_in_downloads_by_exact_name(home, tmp_path):
    svc = _svc()
    name = pins.asset_name(pins.platform_target())
    stem = name[:-len(".tar.gz")]
    downloads = Path(os.environ["HOME"]) / "Downloads"
    assert svc.setup_state()["download"]["dir"] is None  # no Downloads folder: nothing to open the picker in
    downloads.mkdir()
    (downloads / "holiday.jpg").write_bytes(b"x")
    (downloads / (stem + "-copy.tar.gz")).write_bytes(b"x")  # not one of the exact names
    s = svc.setup_state()
    assert s["download"]["found"] is None and s["download"]["dir"] == str(downloads)
    e = err(svc.choose_helper, None, True)
    assert (e.code, e.message, e.hint) == ("not_found", "The download is not in Downloads any more.",
                                           "Press Download botport again.")
    (downloads / name).write_bytes(_archive([(f"{stem}/botport", PROGRAM)]))
    assert svc.setup_state()["download"]["found"] == {"name": name}
    # a second download of the same file: the newest one is the one shown and used
    second = downloads / f"{stem} (1).tar.gz"
    second.write_bytes(_archive([(f"{stem}/botport", PROGRAM)]))
    os.utime(downloads / name, (1000, 1000))
    assert svc.setup_state()["download"]["found"] == {"name": second.name}
    # a browser that unpacks on arrival leaves a folder
    os.unlink(downloads / name)
    os.unlink(second)
    (downloads / stem).mkdir()
    (downloads / stem / "botport").write_bytes(PROGRAM)
    assert svc.setup_state()["download"]["found"] == {"name": stem}
    assert "Downloads" not in json.dumps(svc.setup_state()["download"]["found"])  # a name, never a path
    # a link is never followed, neither as the file nor as the folder
    os.unlink(downloads / stem / "botport")
    (tmp_path / "elsewhere").write_bytes(PROGRAM)
    (downloads / stem / "botport").symlink_to(tmp_path / "elsewhere")
    assert svc.setup_state()["download"]["found"] is None
    assert err(svc.choose_helper, "/some/path", True).code == "bad_input"  # a path or Downloads, not both


def test_downloads_that_is_a_link_is_not_looked_into(home, tmp_path):
    name = pins.asset_name(pins.platform_target())
    real = tmp_path / "other-disk"
    real.mkdir()
    (real / name).write_bytes(_archive([("x/botport", PROGRAM)]))
    (Path(os.environ["HOME"]) / "Downloads").symlink_to(real)
    s = _svc().setup_state()
    assert s["download"]["found"] is None and s["download"]["dir"] is None


def test_use_it_verifies_the_found_download(home, monkeypatch):
    monkeypatch.setattr(connect, "_PROGRAM_MAGIC", (*connect._PROGRAM_MAGIC, b"#!"))
    prog = runnable()
    target = pins.platform_target()
    name = pins.asset_name(target)
    pin({target: sha(prog)})
    downloads = Path(os.environ["HOME"]) / "Downloads"
    downloads.mkdir()
    (downloads / name).write_bytes(_archive([("x/botport", prog + b"\n# not the release")]))
    _svc()
    c = _client(monkeypatch)
    r = c.post(P + "/engine/path", json={"from_downloads": True}).json()
    assert r["error"]["code"] == "not_genuine" and not (central(home) / "helper").exists()
    # a fresh download next to the bad one: the good one is taken, whichever is newer
    (downloads / name.replace(".tar.gz", " (1).tar.gz")).write_bytes(_archive([("x/botport", prog)]))
    r = c.post(P + "/engine/path", json={"from_downloads": True}).json()
    assert r["ready"] is True and r["check"]["method"] == "pinned"
    assert r["download"]["found"] is None  # looked for only while there is something to connect
    assert c.post(P + "/engine/path", json={"from_downloads": "yes"}).status_code == 400


# =====================================================================================================================
# A twin is started, then finished
# =====================================================================================================================

def test_the_sample_starts_unfinished_and_is_finished_by_a_paste(home, monkeypatch):
    svc = _svc()
    out = svc.demo_action("start")
    assert out["pairs"][0]["setup_left"] == ["instructions"]
    assert "Helper" not in json.dumps([out, svc.status(fresh=True), svc.grok_list()])
    example = svc.status()["sample"]["instructions_example"]
    c = _client(monkeypatch)
    r = c.post(P + "/pairs/acme-support/instructions", json={"text": example}).json()
    assert r == {"pair_id": "acme-support", "saved": True, "setup_left": []}
    assert svc.demo._twin("acme-support")["instructions"] == example
    pts = svc.restore_points("acme-support")["points"]
    first = next(p for p in pts if p["item"] == "persona.instructions")
    assert first["replaced_by"] == "paste" and first["text"] == "" and first["reason"] == "Before you added the instructions"
    # a second paste keeps the first one, readable before it is put back
    svc.set_instructions("acme-support", "You are Acme Support. Be brief.")
    pts = svc.restore_points("acme-support")["points"]
    assert example in [p["text"] for p in pts if p["item"] == "persona.instructions"]
    old = next(p for p in pts if p["text"] == example)
    assert svc.restore("acme-support", old["id"])["restored"] == ["persona.instructions"]
    assert svc.demo._twin("acme-support")["instructions"] == example
    # the owner's own text is not a change to sync, and nothing claims it follows Grok
    assert _pair(svc)["state"] == "in_step" and _pair(svc)["pending"]["to_grok"] == 0
    assert svc.check_changed() == []


def test_this_bot_has_no_instructions(home, monkeypatch):
    svc = _svc()
    svc.demo_action("start")
    c = _client(monkeypatch)
    r = c.post(P + "/pairs/acme-support/instructions", json={"none": True}).json()
    assert r == {"pair_id": "acme-support", "saved": False, "setup_left": []}
    assert svc.demo._twin("acme-support").get("instructions") is None  # nothing was written
    for body in ({"text": "x", "none": True}, {"text": ""}, {"text": "   \n"}, {}, {"text": 7},
                 {"text": "x", "accept_lines": "3"}, {"text": "x", "accept_lines": [True]},
                 {"text": "x", "accept_lines": [1.5]}, {"text": "x" * (200 * 1024 + 1)},
                 {"text": "fine\n<!-- botport:begin persona -->\nhidden section"}):
        r = c.post(P + "/pairs/acme-support/instructions", json=body).json()
        assert r["error"]["code"] == "bad_input" and "_" not in r["error"]["message"], body
    assert c.post(P + "/pairs/acme-support/instructions", json={"none": "yes"}).status_code == 400
    assert c.post(P + "/pairs/no-such-twin/instructions", json={"text": "x"}).json()["error"]["code"] == "pair_not_found"


def test_a_refused_paste_names_the_line_and_can_be_overruled_for_that_line(home, monkeypatch):
    svc = _svc()
    svc.demo_action("start")
    c = _client(monkeypatch)
    key = fake_key()
    text = "You are Acme Support.\nBe brief.\nUse this: " + key + "\nSay when you don't know."
    r = c.post(P + "/pairs/acme-support/instructions", json={"text": text}).json()
    assert r["error"] == {"code": "looks_like_secret", "lines": [3],
                          "message": "Line 3 looks like a password or key, so nothing was saved yet.",
                          "hint": "Take that line out, or save without it."}
    assert svc.demo._twin("acme-support").get("instructions") is None and _pair(svc)["setup_left"] == ["instructions"]
    # a line that is not flagged cannot unlock anything; neither can the wrong line
    for accept in ([1], [2, 4], []):
        r = c.post(P + "/pairs/acme-support/instructions", json={"text": text, "accept_lines": accept}).json()
        assert r["error"]["lines"] == [3], accept
    # two flagged lines, one accepted: still refused, for the other one only
    two = text + "\n\n\n\n\npassword: " + "another-" + "long-secret-value-123"
    r = c.post(P + "/pairs/acme-support/instructions", json={"text": two, "accept_lines": [3]}).json()
    assert r["error"]["lines"] == [9] and r["error"]["message"].startswith("Line 9 looks like")
    r = c.post(P + "/pairs/acme-support/instructions", json={"text": two}).json()
    assert r["error"]["lines"] == [3, 9] and r["error"]["message"].startswith("Lines 3 and 9 look like")
    assert r["error"]["hint"] == "Take those lines out, or save without them."
    assert svc.demo._twin("acme-support").get("instructions") is None
    # the owner looked at line 3 and says it is not a password
    r = c.post(P + "/pairs/acme-support/instructions", json={"text": text, "accept_lines": [3, 1]}).json()
    assert r == {"pair_id": "acme-support", "saved": True, "setup_left": []}
    assert svc.demo._twin("acme-support")["instructions"] == text
    ev = [e for e in svc.events()["events"] if e["type"] == "instructions"][-1]
    assert ev["payload"] == {"saved": True, "kept_lines": 1,
                             "message": "Instructions saved; 1 line kept that looked like a password or key"}
    # the text never travels: not in an answer, not in the activity log, not in what the page reads back
    seen = json.dumps([svc.events(), svc.status(fresh=True), svc.recent("acme-support"),
                       svc.restore_points("acme-support")])
    assert key not in seen
    assert key not in (central(home) / "events.jsonl").read_text()
    # and an earlier version that holds such a line shows it hidden
    svc.set_instructions("acme-support", "A clean version.")
    kept = next(p for p in svc.restore_points("acme-support")["points"] if "Be brief." in (p["text"] or ""))
    assert kept["text"].split("\n")[2] == "[not shown: looks like a password or key]"


def test_many_flagged_lines_are_counted_not_listed_without_end():
    from hermes_plugins.grok_bot_twin.twin.finish import lines_message
    assert lines_message([14]) == "Line 14 looks like a password or key, so nothing was saved yet."
    assert lines_message([14, 20, 31, 32, 33]) == (
        "Lines 14, 20 and 3 more look like a password or key, so nothing was saved yet.")
    e = EngineError("looks_like_secret", "m", extra={"lines": list(range(1, 40)) + ["x", True, -1]})
    assert e.as_dict()["lines"] == list(range(1, 21))  # at most 20, numbers only


def test_a_real_twin_is_started_then_finished(fake, home, monkeypatch):
    svc = fake.svc
    svc.trust_botport(True)
    prof = profile(home)
    made = svc.create(["b-acme-0001"])["pairs"][0]
    assert made["setup_left"] == ["instructions", "model"] and made["hermes"]["model_from"] is None
    assert made["hermes"]["name"] == "Acme Support" and made["grok"]["description"] == made["hermes"]["description"]
    assert "Settings" in made["model_hint"] and "Applies to" in made["model_hint"] and "Acme Support" in made["model_hint"]
    c = _client(monkeypatch)
    text = "You are Acme Support.\r\nBe friendly and brief."
    r = c.post(P + "/pairs/acme-support/instructions", json={"text": text}).json()
    assert r == {"pair_id": "acme-support", "saved": True, "setup_left": ["model"]}
    soul = (prof / "SOUL.md").read_text(encoding="utf-8")
    assert soul.startswith(SOUL) and "You are Acme Support.\nBe friendly and brief." in soul  # botport's part untouched
    reg = json.loads((central(home) / "pairs.json").read_text())["pairs"][0]
    assert reg["instructions"]["by"] == "owner"
    pts = svc.restore_points("acme-support")["points"]
    # the version from before the paste holds nothing: it is not listed (a list of blank "versions" on a new
    # twin was put back blind), but it is still kept, and putting it back works
    assert [p for p in pts if p["item"] == "persona.instructions"] == []
    own = [k for k in hs.kept_list(central(home), "acme-support")]
    assert [(k["why"], k["text"]) for k in own] == [("paste", "")]
    assert any(p["text"] is None for p in pts if p["item"] != "persona.instructions")  # botport's own kept copies
    assert _pair(svc)["restore_points"] == len(pts)
    # the earlier (empty) version can be put back: the twin is unfinished again, and that can be undone
    back = svc.restore("acme-support", own[0]["id"])
    assert back["restored"] == ["persona.instructions"] and back["restore_point"]
    # (what is left besides botport's own text is the plugin's note about which computer this copy is on)
    left = (prof / "SOUL.md").read_text(encoding="utf-8")
    assert left.startswith(SOUL) and hs._FLEET_RE.sub("", left) == SOUL and "grok-bot-twin:begin fleet" in left
    assert _pair(svc)["setup_left"] == ["instructions", "model"]
    assert svc.restore("acme-support", back["restore_point"])["restored"] == ["persona.instructions"]
    assert _pair(svc)["setup_left"] == ["model"]
    # choosing a model in Hermes (Settings, Model) finishes it
    (prof / "config.yaml").write_text("model:\n  default: example-model\n", encoding="utf-8")
    p = _pair(svc)
    assert p["setup_left"] == [] and p["hermes"]["model_from"] == "own" and "model_hint" not in p
    comp = svc.parity("acme-support")
    assert not any(c_["id"] == "model" for c_ in comp["checks"])
    # a failed save changes nothing and says so in plain words
    monkeypatch.setattr(hs, "write_instructions", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    assert err(svc.set_instructions, "acme-support", "new text").code == "unsafe_folder"


def test_a_new_twin_gets_the_owners_default_model(fake, home):
    (home / "config.yaml").write_text("model:\n  default: example-model\n  provider: example\n  api_key: "
                                      + fake_key() + "\n", encoding="utf-8")
    svc = fake.svc
    svc.trust_botport(True)
    prof = profile(home)
    made = svc.create(["b-acme-0001"])["pairs"][0]
    assert made["setup_left"] == ["instructions"] and made["hermes"]["model_from"] == "default"
    assert "model_hint" not in made
    assert fake_key() not in (prof / "config.yaml").read_text()
    # the owner picks another model for the twin later: it is their own from then on
    (prof / "config.yaml").write_text("model:\n  default: another-model\n", encoding="utf-8")
    assert _pair(svc)["hermes"]["model_from"] == "own"
    # the model is taken away: the step is back, with where to choose one
    os.unlink(prof / "config.yaml")
    p = _pair(svc)
    assert p["setup_left"] == ["instructions", "model"] and p["hermes"]["model_from"] is None and p["model_hint"]


def test_a_twin_that_botport_already_gave_the_default_model_counts_as_default(fake, home):
    """botport 0.2 copies the default model itself when it makes a twin (its own file format, quoted values):
    the plugin writes nothing about the model then, and still knows the twin started with the default. (What the
    plugin does add to that file is its own three lines that switch it on in the twin's Hermes bot; they go again
    when twinning stops.)"""
    (home / "config.yaml").write_text("model:\n  default: example-model\n  provider: example\n", encoding="utf-8")
    svc = fake.svc
    svc.trust_botport(True)
    prof = profile(home)
    by_botport = 'model:\n  default: "example-model"\n  provider: "example"\n'
    (prof / "config.yaml").write_text(by_botport, encoding="utf-8")
    made = svc.create(["b-acme-0001"])["pairs"][0]
    assert made["hermes"]["model_from"] == "default" and made["setup_left"] == ["instructions"]
    assert (prof / "config.yaml").read_text(encoding="utf-8") == by_botport + "plugins:\n  enabled:\n  - grok-bot-twin\n"
    # a twin whose Hermes bot already had a model of its own is not called "default"
    svc.remove("acme-support", False)
    assert (prof / "config.yaml").read_text(encoding="utf-8") == by_botport  # byte for byte what botport wrote
    (prof / "config.yaml").write_text("model: my-own-model\n", encoding="utf-8")
    assert svc.create(["b-acme-0001"])["pairs"][0]["hermes"]["model_from"] == "own"


# =====================================================================================================================
# The text of a skill the owner copies into Grok by hand
# =====================================================================================================================

def test_sample_skill_text_and_pasted_it(home, monkeypatch):
    svc = _svc()
    svc.demo_action("start")
    svc.demo_action("hermes-edit")
    svc.sync()
    p = _pair(svc)
    assert [w["item"] for w in p["waiting"]] == ["skill:fun-fact"]
    c = _client(monkeypatch)
    r = c.get(P + "/pairs/acme-support/skill-text?item=skill:fun-fact").json()
    assert r == {"pair_id": "acme-support", "item": "skill:fun-fact", "name": "fun-fact", "truncated": False,
                 "text": svc.demo._twin("acme-support")["skills"]["fun-fact"]["text"]}
    assert "End with a friendly sign-off." in r["text"]  # the Hermes version, not Grok's
    for item in ("skill:no-such", "skill:greet-visitors", "persona.name", "routine:Morning summary"):
        e = c.get(P + f"/pairs/acme-support/skill-text?item={item}").json()["error"]
        assert e["code"] == "not_found" and e["message"] == "That skill is not waiting to be copied to Grok.", item
    assert c.get(P + "/pairs/no-such-twin/skill-text?item=skill:fun-fact").json()["error"]["code"] == "pair_not_found"
    # "Done, I pasted it": gone from the list, not "kept in Hermes only", and back after the next edit
    r = c.post(P + "/pairs/acme-support/skip", json={"item": "skill:fun-fact", "copied": True}).json()
    assert r == {"pair_id": "acme-support", "skipped": []}
    p = _pair(svc)
    assert p["waiting"] == [] and p["skipped"] == [] and p["state"] == "in_step" and p["pending"]["to_grok_items"] == []
    # pressing it again, or after the plugin has already seen the text in Grok, is answered as done: no error
    # (before, the page showed a red "Could not note that" where nothing had failed)
    before = {f.name: f.read_bytes() for f in central(home).iterdir() if f.is_file()}
    for item in ("skill:fun-fact", "skill:no-such"):
        assert c.post(P + "/pairs/acme-support/skip", json={"item": item, "copied": True}).json() == {
            "pair_id": "acme-support", "skipped": []}
    assert {f.name: f.read_bytes() for f in central(home).iterdir() if f.is_file()} == before  # and nothing written
    assert _pair(svc)["waiting"] == []
    assert c.post(P + "/pairs/acme-support/skip", json={"item": "skill:fun-fact", "copied": True, "undo": True}
                  ).json()["error"]["code"] == "bad_input"
    svc.demo_action("hermes-edit")
    svc.sync()
    assert [w["item"] for w in _pair(svc)["waiting"]] == ["skill:fun-fact"]
    # approved but not sent yet: counted in to_grok, and named
    svc.approve("acme-support", "skill:fun-fact")
    p = _pair(svc)
    assert p["pending"]["to_grok"] == 1 and p["pending"]["to_grok_items"] == ["skill:fun-fact"]


def test_a_skill_that_holds_a_password_is_not_handed_over(home, monkeypatch):
    svc = _svc()
    svc.demo_action("start")
    svc.demo_action("hermes-edit")
    svc.sync()
    svc.skip("acme-support", "skill:fun-fact")  # kept in Hermes only: its text may still be asked for
    assert svc.skill_text("acme-support", "skill:fun-fact")["name"] == "fun-fact"
    t = svc.demo._twin("acme-support")
    t["skills"]["fun-fact"]["text"] += "\ntoken: " + fake_key()
    svc.demo._write_twin("acme-support", t)
    svc.invalidate()
    e = err(svc.skill_text, "acme-support", "skill:fun-fact")
    assert e.code == "looks_like_secret" and "does not hand it over" in e.message
    assert e.hint == "Take it out of the skill in Hermes first." and fake_key() not in json.dumps(e.as_dict())


def test_real_twin_skill_text_and_pasted_it(twin, home, monkeypatch):
    svc = twin
    d = home / "profiles" / "acme-support" / "skills" / "imported" / "pick-a-book"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# Pick a book\nSuggest one book.\n", encoding="utf-8")
    waiting = {"state": "needs_you", "pending": {"to_hermes": 0, "to_grok": 1, "in_flight": 0, "waiting_approval": 1},
               "waiting": [{"item": "skill:pick-a-book", "reason": "needs_approval", "to": "grok"}],
               "by_hand": [{"item": "skill:rate-a-book", "side": "grok", "what": "Copy the Hermes version into Grok"}]}
    mode(home, status=waiting)
    p = _pair(svc)
    assert p["pending"]["to_grok_items"] == ["skill:rate-a-book"] and p["state"] == "needs_you"
    assert svc.skill_text("acme-support", "skill:pick-a-book")["text"] == "# Pick a book\nSuggest one book.\n"
    assert err(svc.skill_text, "acme-support", "skill:rate-a-book").message == "Hermes could not find that skill's text."
    assert err(svc.skill_text, "acme-support", "skill:../../etc").code == "not_found"
    out = svc.skip("acme-support", "skill:pick-a-book", copied=True)
    assert out["skipped"] == []
    p = _pair(svc)
    assert p["waiting"] == [] and p["pending"]["waiting_approval"] == 0 and p["skipped"] == []
    assert json.loads((central(home) / "copied.json").read_text()) == {
        "acme-support": {"skill:pick-a-book": hs.text_digest("# Pick a book\nSuggest one book.\n")}}  # a digest, no text
    # the skill changes in Hermes later: it asks again
    (d / "SKILL.md").write_text("# Pick a book\nSuggest two books.\n", encoding="utf-8")
    assert [w["item"] for w in _pair(svc)["waiting"]] == ["skill:pick-a-book"]  # (the note no longer matches)
    # "keep in Hermes only" also covers what to_grok counts
    svc.skip("acme-support", "skill:rate-a-book")
    p = _pair(svc)
    assert p["pending"]["to_grok_items"] == [] and p["pending"]["to_grok"] == 0 and p["skipped"] == ["skill:rate-a-book"]


# =====================================================================================================================
# Only two things hold up moving routines
# =====================================================================================================================

def test_a_waiting_skill_or_a_held_password_does_not_block_the_move(home):
    svc = _svc()
    svc.demo_action("start")
    jobs = [j["job_id"] for j in svc.checklist("acme-support")["switch"]["hermes_jobs_paused"]]
    svc.demo_action("hermes-edit")
    svc.demo_action("secret")
    svc.sync()
    p = _pair(svc)
    assert p["state"] == "needs_you" and p["waiting"] and p["secrets_held"] == 1
    assert svc.checklist("acme-support")["blocked_by"] is None
    assert svc.switch("acme-support", jobs[:1])["resumed"] == jobs[:1]
    assert _pair(svc)["routines"] == {"in_grok": 2, "paused_in_hermes": 1, "on_in_hermes": 1}
    svc.demo_action("conflict")
    assert svc.checklist("acme-support")["blocked_by"] == 'First choose which version of the skill "greet-visitors" to keep.'
    e = err(svc.switch, "acme-support", jobs[1:])
    assert e.code == "blocked" and e.hint == "Then choose Move routines to Hermes again."
    assert svc.switch("acme-support", undo=True)["paused"] == jobs[:1]  # undo is never blocked
    # the both-places change is listed at once, so the page can show the two versions without waiting for a check
    assert [c["conflict_id"] for c in svc.conflicts("acme-support")["conflicts"]] == ["skill:greet-visitors"]
    svc.resolve("acme-support", "skill:greet-visitors", "grok")
    assert svc.checklist("acme-support")["blocked_by"] is None


# =====================================================================================================================
# Leaving lists only what exists, and says what keeps running
# =====================================================================================================================

def test_leaving_lists_only_what_exists(fake, home, monkeypatch):
    svc = fake.svc
    st = svc.status(fresh=True)
    assert st["has_data"] is False  # a botport that merely sits on this computer is not the plugin's data
    assert svc.uninstall(True)["goes"] == []
    svc.demo_action("start")
    assert svc.status(fresh=True)["has_data"] is True
    plan = svc.uninstall(True)
    assert plan["goes"] == ["The sample."] and plan["routines_on"] == 0
    assert plan["stays"] == ["Your Grok bots.", "Your Hermes bots and their chats."]
    assert plan["next"] == "Switch the plugin off in Settings → Capabilities → Plugins."
    assert "key" not in json.dumps([plan["goes"], plan["stays"]]).lower()
    svc.demo_action("remove")
    # only the plugin's own working files are left (the activity log, the remembered status): the first screen
    # no longer offers "Remove Grok Bot Twin…" for them, the terminal still clears them
    assert svc.status(fresh=True)["has_data"] is False
    assert svc.uninstall(True)["goes"] == ["Grok Bot Twin's own notes on this computer."]
    svc.trust_botport(True)
    profile(home)
    svc.create(["b-acme-0001"])
    plan = svc.uninstall(True)
    assert plan["goes"] == ["1 twin (Acme Support).", "Earlier versions of 1 twin.",
                            "Hermes' working copy of botport (the botport on this computer stays)."]
    svc.uninstall(False)
    assert sorted(p.name for p in central(home).iterdir()) == []
    st = svc.status(fresh=True)
    assert st["has_data"] is False and st["setup"]["helper_found"] is True and st["setup"]["cause"] is None


def test_ending_the_sample_forgets_what_was_kept_in_hermes_only(home):
    svc = _svc()
    svc.demo_action("start")
    svc.demo_action("hermes-edit")
    svc.sync()
    svc.skip("acme-support", "skill:fun-fact")
    assert _pair(svc)["waiting"] == [] and json.loads((central(home) / "skipped.json").read_text())
    svc.demo_action("remove")
    assert json.loads((central(home) / "skipped.json").read_text()) == {}
    svc.demo_action("start")
    svc.demo_action("hermes-edit")
    svc.sync()
    assert [w["item"] for w in _pair(svc)["waiting"]] == ["skill:fun-fact"]  # offered again


def test_stop_twinning_says_what_keeps_running(fake, home, monkeypatch):
    svc = fake.svc
    svc.trust_botport(True)
    prof = profile(home)
    jobs_file = prof / "cron" / "jobs.json"
    jobs_file.parent.mkdir(parents=True)
    made = [_job("a1b2c3d4e5f6", reason="Imported by botport from the Grok bot; resume once the Grok routine is off"),
            _job("a1b2c3d4e5f7", reason="Imported by botport from the Grok bot; resume once the Grok routine is off")]
    jobs_file.write_text(json.dumps({"jobs": made}))
    svc.create(["b-acme-0001"])
    plan = svc.remove("acme-support", True)
    assert plan["stays"] == ["Your bot in Grok, unchanged.", "Its Hermes bot and all its chats."]
    assert _pair(svc)["routines"] == {"in_grok": 0, "paused_in_hermes": 2, "on_in_hermes": 0}

    def resume(job_id, prof_name, ctx=None):
        data = json.loads(jobs_file.read_text())
        for j in data["jobs"]:
            if j["id"] == job_id:
                j["enabled"], j["state"] = True, "scheduled"
        jobs_file.write_text(json.dumps(data))
        return True, ""

    monkeypatch.setattr(importer, "resume_cron", resume)
    svc.switch("acme-support", ["a1b2c3d4e5f6"])
    plan = svc.remove("acme-support", True)
    assert plan["stays"][-1] == "Its 1 routine keeps running in Hermes."
    svc.switch("acme-support", ["a1b2c3d4e5f7"])
    plan = svc.remove("acme-support", True)
    assert plan["stays"] == ["Your bot in Grok, unchanged.", "Its Hermes bot and all its chats.",
                             "Its 2 routines keep running in Hermes."]
    assert _pair(svc)["routines"]["on_in_hermes"] == 2 and svc.uninstall(True)["routines_on"] == 2
    assert plan["skill_sync"] is False and plan["again"] == "You can twin it again later; it picks up this Hermes bot again."
    # skill sync on: the page reminds the owner of the sync bot. A rename in Grok changes nothing about "again"
    mode(home, name="Trail Guide", status={"mirror_bot": {"connected": True}})
    plan = svc.remove("acme-support", True)
    assert plan["skill_sync"] is True
    assert plan["again"] == "You can twin it again later; it picks up this Hermes bot again."
    # for real: the plugin's own notes about the twin go with the link
    svc.set_instructions("acme-support", "You are Trail Guide.")
    assert json.loads((central(home) / "kept.json").read_text())
    svc.remove("acme-support", False)
    assert json.loads((central(home) / "kept.json").read_text()) == {}
    assert "You are Trail Guide." in (prof / "SOUL.md").read_text()  # the Hermes bot keeps its instructions
    # "again" is what create then really does: the same Hermes bot is named to botport, which picks it up
    assert json.loads((central(home) / "former.json").read_text()) == {"b-acme-0001": "acme-support"}
    n = len(fake.calls())
    again = svc.create(["b-acme-0001"])["pairs"][0]
    creates = [c_ for c_ in fake.calls()[n:] if c_[:2] == ["twin", "create"]]
    assert creates and all("--profile=acme-support" in c_ for c_ in creates)
    assert again["hermes"]["profile"] == "acme-support" and "instructions" not in again["setup_left"]  # still there
    # the Hermes bot was deleted in the meantime: nothing to pick up, botport makes a new one as it sees fit
    svc.remove("acme-support", False)
    import shutil
    shutil.rmtree(prof)
    n = len(fake.calls())
    svc.create(["b-acme-0001"])
    assert not any("--profile=acme-support" in c_ for c_ in fake.calls()[n:] if "--dry-run" in c_)


# =====================================================================================================================
# Both sides of a twin, readable earlier versions, small fields
# =====================================================================================================================

def test_the_sample_shows_both_sides_of_one_twin(home):
    svc = _svc()
    svc.demo_action("start")
    p = _pair(svc)
    assert p["grok"]["description"] == p["hermes"]["description"] == "Answers questions about Acme widgets."
    assert p["hermes"]["name"] == p["grok"]["name"] == "Acme Support"
    svc.demo_action("grok-edit")
    p = _pair(svc)
    assert p["grok"]["description"] == "Answers questions about Acme widgets and gadgets." != p["hermes"]["description"]
    before = p["checked_at"]
    svc.sync()
    p = _pair(svc)
    assert p["grok"]["description"] == p["hermes"]["description"] == "Answers questions about Acme widgets and gadgets."
    assert p["checked_at"] >= before and p["mirror_bot"] == {"connected": True, "every_minutes": None}
    old = next(x for x in svc.restore_points("acme-support")["points"] if x["item"] == "persona.description")
    assert old["replaced_by"] == "grok" and old["text"] == "Answers questions about Acme widgets."
    # a password in Grok is hidden on the Grok side and never reaches the Hermes side; the named action takes it out
    svc.demo_action("secret")
    svc.sync()
    p = _pair(svc)
    assert fake_key() not in json.dumps(p) and "[not shown: looks like a password or key]" in p["grok"]["description"]
    assert p["hermes"]["description"] == "Answers questions about Acme widgets and gadgets."
    assert svc.demo_action("remove-secret")["did"] == "Took the fake password out of the sample bot's description in Grok"
    svc.sync()
    assert _pair(svc)["secrets_held"] == 0


def test_real_twin_sides_and_the_time_of_the_last_check(twin, home):
    svc = twin
    p = _pair(svc)
    assert p["hermes"] == {"profile": "acme-support", "name": "Acme Support",
                           "description": "Answers questions about Acme widgets.", "model_from": None}
    assert p["grok"]["description"] == "Answers questions about Acme widgets."  # as read from the Grok app's list
    assert p["routines"] == {"in_grok": 0, "paused_in_hermes": 0, "on_in_hermes": 0}
    assert p["mirror_bot"]["every_minutes"] is None and p["checked_at"]
    svc._checked.clear()
    svc.last_check_at = None
    assert _pair(svc)["checked_at"] == "2000-01-01T09:00:00Z"  # botport's own last sync, when nothing newer is known
    svc.sync("acme-support")
    assert _pair(svc)["checked_at"] > "2020"  # a manual check of this twin counts at once
    # which earlier version an action left behind is remembered, so the list can say why it is there
    svc._note_point("acme-support", "persona.name@8ef22c6d10ba", "choice")
    pt = next(x for x in svc.restore_points("acme-support")["points"] if x["id"] == "persona.name@8ef22c6d10ba")
    assert pt["replaced_by"] == "choice" and pt["text"] is None
    svc._note_point("acme-support", "persona.name@8ef22c6d10ba", "made-up")
    assert svc._point_notes("acme-support") == {"persona.name@8ef22c6d10ba": "choice"}


# =====================================================================================================================
# The real botport's own, stricter check can stop a twin's updates
# =====================================================================================================================

def test_a_twin_botport_stopped_updating_says_which_line_and_how_to_fix_it(twin, home):
    """botport keeps a copy of the Hermes bot's files before it changes anything there, and refuses while one of
    them holds a line it takes for a password or key (also a line of pasted instructions the owner accepted on
    the page). Its status then only says "a change is waiting". The twin's row must say what happened."""
    svc = twin
    key = fake_key()
    text = "You are Acme Support.\nBe brief.\nTracking code: " + key
    assert err(svc.set_instructions, "acme-support", text).lines == [3]
    assert svc.set_instructions("acme-support", text, accept_lines=[3])["saved"] is True
    assert _pair(svc)["state"] == "in_step"
    # a change arrives from Grok; botport refuses to apply it and points at a line of SOUL.md, counted over the
    # whole file: botport's own lines, the plugin's fleet note, the block's first line, then the third line of the
    # instructions
    soul_lines = (profile(home) / "SOUL.md").read_text(encoding="utf-8").split("\n")
    at = next(i for i, ln in enumerate(soul_lines, 1) if key in ln)
    refusal = {"code": "secret_refused", "message": f"live file SOUL.md: line {at} looks like it holds a secret "
               "(token-shaped value). botport will not copy it into a restore point, so nothing was applied"}
    waiting = {"state": "changes_waiting", "pending": {"to_hermes": 2, "to_grok": 0, "in_flight": 0, "waiting_approval": 0}}
    mode(home, status=waiting, sync_refused=refusal)
    out = svc.sync("acme-support")["pairs"][0]
    assert out["state"] == "error" and out["error"]["code"] == "secret_refused"
    p = _pair(svc)
    assert p["state"] == "error" and p["error"]["code"] == "secret_refused"
    assert (p["sentence"], p["tone"], p["needs"]) == ("Can't check right now", "bad", "you")  # R7: the server's words follow
    assert p["error"]["message"] == ("Updates from Grok stopped: line 3 of Acme Support's instructions looks like a "
                                     "password or key to botport.")
    assert "Earlier versions" in p["error"]["hint"] and "without that line" in p["error"]["hint"]
    assert json.loads((central(home) / "stuck.json").read_text())["acme-support"]["line"] == at
    assert key not in (central(home) / "stuck.json").read_text() and key not in json.dumps(p)
    # it survives a restart of Hermes (botport's own status would only say "updating")
    again = _svc(botport_path=config.get("botport_path"))
    assert _pair(again)["error"]["code"] == "secret_refused"
    # the owner saves the instructions without that line: the plugin tries the update again by itself
    mode(home)  # botport applies it now
    n = len(calls(home))
    assert again.set_instructions("acme-support", "You are Acme Support.\nBe brief.")["saved"] is True
    assert any(c_[:2] == ["twin", "sync"] for c_ in calls(home)[n:])
    p = _pair(again)
    assert p["state"] == "in_step" and p["error"] is None
    assert json.loads((central(home) / "stuck.json").read_text()) == {}
    # a refusal about another file names the file, and never guesses a line of the instructions
    mode(home, status=waiting, sync_refused={"code": "secret_refused",
                                             "message": "live file config.yaml: line 4 looks like it holds a secret"})
    again.sync("acme-support")
    p = _pair(again)
    assert "config.yaml, line 4" in p["error"]["message"] and "Try again" in p["error"]["hint"]
    # once nothing is waiting any more, the row says so and the timer's tick forgets the note
    mode(home, sync_refused={"code": "secret_refused", "message": "live file config.yaml: line 4 looks like a secret"})
    assert _pair(again)["state"] == "in_step"
    again.save_status()
    assert json.loads((central(home) / "stuck.json").read_text()) == {}
