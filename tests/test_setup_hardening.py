"""The doors around getting botport and leaving: what is refused, and what a refusal says.

- a chosen file is judged as the opened file (never by its name first), with a small size cap, and only the two
  archive kinds botport ships are opened;
- botport's own doctor lines can never switch off the masking of what it reports;
- removing everything over the local API needs its confirmation word;
- a missing `gh` or `hermes` is named as itself, never as a missing botport.

Invented data only; every test runs in a throwaway HERMES_HOME and no chosen file is ever run.
"""
from __future__ import annotations

import bz2
import importlib.util
import io
import json
import lzma
import os
import sys
import tarfile
import threading
from pathlib import Path

import pytest

from twin_testlib import ROOT, central, config, dumps, fake_key, home, service, timer  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import connect, importer
from hermes_plugins.grok_bot_twin.twin import engine as E
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

FAKE = Path(__file__).resolve().parent / "fake_botport.py"
P = "/api/plugins/grok-bot-twin"
# Looks like a program (Mach-O 64-bit header bytes); never run by any test.
PROGRAM = b"\xcf\xfa\xed\xfe" + b"\x07\x00\x00\x01" + b"\x00" * 64 + b"invented program bytes"
posix_only = pytest.mark.skipif(os.name != "posix", reason="pipes and sparse files as macOS and Linux have them")


def _svc(**settings):
    config.set_override(settings)
    service.reset_service()
    return service.get_service()


def _tar(members, mode="w") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _dest(home) -> Path:
    return central(home) / "helper" / "botport"


# ---- the chosen file: opened first, then judged --------------------------------------------------------------

def test_the_cap_on_a_chosen_file_is_small():
    assert connect.HELPER_CAP == 64 * 1024 * 1024


@posix_only
def test_a_file_over_the_cap_is_refused_without_being_read(home, tmp_path, monkeypatch):
    big = tmp_path / "botport"
    with open(big, "wb") as fh:
        fh.write(PROGRAM)
        fh.truncate(connect.HELPER_CAP + 1)  # sparse: takes no room on disk
    read = []
    real_fdopen = os.fdopen
    monkeypatch.setattr(os, "fdopen", lambda fd, *a, **k: (read.append(fd), real_fdopen(fd, *a, **k))[1])
    with pytest.raises(EngineError) as ei:
        connect.ConnectMixin._read_chosen(big)
    assert ei.value.code == "bad_input" and ei.value.message == "That file is too large to be botport."
    assert read == []  # judged by its size as opened; not one byte was read
    with pytest.raises(EngineError):
        _svc().choose_helper(str(big))
    assert not _dest(home).exists()


@posix_only
def test_a_file_that_grows_after_the_look_is_cut_off_at_the_cap(tmp_path, monkeypatch):
    """The size as opened says "small"; the read is capped all the same."""
    monkeypatch.setattr(connect, "HELPER_CAP", 16)
    grown = tmp_path / "botport"
    grown.write_bytes(PROGRAM)  # longer than 16 bytes
    real_fstat = os.fstat

    class Small:
        def __init__(self, st):
            self.st_mode, self.st_size = st.st_mode, 8

    monkeypatch.setattr(os, "fstat", lambda fd: Small(real_fstat(fd)))
    with pytest.raises(EngineError) as ei:
        connect.ConnectMixin._read_chosen(grown)
    assert ei.value.code == "bad_input" and ei.value.message == "That file is too large to be botport."


@posix_only
def test_a_pipe_in_place_of_the_file_is_refused_at_once(home, tmp_path):
    pipe = tmp_path / "botport"
    os.mkfifo(pipe)
    got: list = []

    def choose():
        try:
            _svc().choose_helper(str(pipe))
        except EngineError as exc:
            got.append(exc)

    t = threading.Thread(target=choose, daemon=True)
    t.start()
    t.join(10)
    stuck = t.is_alive()
    if stuck:  # let the thread go, so a failure here does not hang the run
        fd = os.open(pipe, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        t.join(5)
    assert not stuck, "opening a pipe waited for a writer"
    assert got and got[0].code == "not_botport" and not _dest(home).exists()


@posix_only
def test_the_opened_file_is_what_is_judged_not_the_name(home, tmp_path, monkeypatch):
    """Nothing about the name is looked up before the open: there is no moment in which the file could be
    swapped between a look and the read."""
    src = tmp_path / "botport"
    src.write_bytes(PROGRAM)
    looked: list = []
    real_lstat, real_stat = os.lstat, os.stat
    monkeypatch.setattr(connect._osflags, "WINDOWS", False)
    monkeypatch.setattr(os, "lstat", lambda p, *a, **k: (looked.append(str(p)), real_lstat(p, *a, **k))[1])
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: (looked.append(str(p)), real_stat(p, *a, **k))[1])
    assert connect.ConnectMixin._read_chosen(src) == PROGRAM
    assert str(src) not in looked


def test_what_a_refused_file_is_told(home, tmp_path):
    real = tmp_path / "real"
    real.write_bytes(PROGRAM)
    link = tmp_path / "shortcut"
    link.symlink_to(real)
    said = {}
    for name, case in (("link", link), ("missing", tmp_path / "missing"), ("folder", tmp_path),
                       ("under a file", real / "botport")):
        with pytest.raises(EngineError) as ei:
            _svc().choose_helper(str(case))
        said[name] = (ei.value.code, ei.value.message)
        assert not _dest(home).exists(), name
    assert said["link"] == ("bad_input", "Choose the file itself, not a shortcut to it.")
    assert said["missing"] == said["under a file"] == \
        ("bad_input", "That file is not there any more. Choose the downloaded file again.")
    assert said["folder"] == ("not_botport", "That is not botport.")


# ---- archives: only what botport ships -----------------------------------------------------------------------

def test_gzip_and_plain_archives_are_opened_and_no_other_kind(home, tmp_path):
    members = [("botport-x/README.md", b"read me"), ("botport-x/botport", PROGRAM)]
    assert connect.program_from_archive(_tar(members, "w:gz")) == PROGRAM
    assert connect.program_from_archive(_tar(members, "w")) == PROGRAM
    for blob in (_tar(members, "w:bz2"), _tar(members, "w:xz"), bz2.compress(_tar(members)),
                 lzma.compress(_tar(members))):
        with pytest.raises(EngineError) as ei:
            connect.program_from_archive(blob)
        assert ei.value.code == "not_botport"
    # the same through the door the page uses, whatever the file is called
    for i, mode in enumerate(("w:bz2", "w:xz")):
        f = tmp_path / f"botport-{i}.tar.gz"
        f.write_bytes(_tar(members, mode))
        with pytest.raises(EngineError):
            _svc().choose_helper(str(f))
        assert not _dest(home).exists()


# ---- doctor: botport cannot mark its own lines as the plugin's -----------------------------------------------

def test_botports_doctor_lines_are_masked_whatever_they_carry(home, tmp_path):
    exe = tmp_path / "bin" / "botport"
    exe.parent.mkdir(mode=0o755)
    exe.write_text(f"#!{sys.executable} -I\n{FAKE.read_text(encoding='utf-8')}", encoding="utf-8")
    exe.chmod(0o755)
    os.chmod(tmp_path, 0o700)
    svc = _svc(botport_path=str(exe))
    svc.trust_botport(True)
    state = central(home) / "botport-state"
    state.mkdir(parents=True, exist_ok=True)
    key = fake_key()
    (state / "fake_mode.json").write_text(json.dumps({"doctor_checks": [
        {"id": "x", "ok": False, "_own": True, "message": f"look: {key}", "fix": f"paste {key} somewhere"}]}))
    # the engine layer takes the mark off what botport sent, and keeps it on its own two lines
    raw = svc.botport().doctor()["checks"]
    assert [c["id"] for c in raw if c.get("_own")] == ["botport_trust", "botport_attestation"]
    assert not next(c for c in raw if c["id"] == "x").get("_own")
    out = svc.doctor()
    line = next(c for c in out["checks"] if c["id"] == "x")
    assert (line["ok"], line["message"], line["fix"]) == (False, "look: [secret]", "paste [secret] somewhere")
    assert key not in dumps(out) and "_own" not in dumps(out)


# ---- removing everything over the local API needs its word -----------------------------------------------------

def test_uninstall_over_the_api_needs_the_confirmation_word(home, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_doors", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    app = fastapi.FastAPI()
    app.include_router(api.router, prefix=P)
    client = TestClient(app)
    svc = _svc()
    svc.demo_action("start")
    assert svc.pair_ids() and any(central(home).iterdir())
    for body in ({}, {"dry_run": False}, {"confirm": "yes"}, {"confirm": True}, {"confirm": ["remove-everything"]}):
        r = client.post(P + "/uninstall", json=body)
        assert r.status_code == 400 and r.json()["detail"] == \
            {"code": "bad_input", "message": "That request was not understood."}, body
    assert client.post(P + "/uninstall").status_code == 400  # no body at all
    assert svc.pair_ids() and any(central(home).iterdir())  # nothing went
    plan = client.post(P + "/uninstall", json={"dry_run": True}).json()  # looking needs no word
    assert plan["dry_run"] is True and plan["sample"] is True and svc.pair_ids()
    done = client.post(P + "/uninstall", json={"confirm": api.UNINSTALL_CONFIRM}).json()
    assert done["dry_run"] is False and api.UNINSTALL_CONFIRM == "remove-everything"
    assert service.get_service().pair_ids() == [] and sorted(p.name for p in central(home).iterdir()) == []


# ---- a missing program is named as itself ----------------------------------------------------------------------

def test_a_missing_program_is_named_as_itself(tmp_path):
    nowhere = str(tmp_path / "not-here")
    with pytest.raises(EngineError) as ei:
        E.run_capped([nowhere], 5.0)
    assert (ei.value.code, ei.value.message, ei.value.hint) == \
        ("not_found", "botport is not on this computer yet.", E.INSTALL_HINT)
    for what in (E.WHAT_GH, E.WHAT_HERMES):
        with pytest.raises(EngineError) as ei:
            E.run_capped([nowhere], 5.0, what=what)
        assert ei.value.code == "not_found" and ei.value.message == f"{what} is not on this computer."
        assert "botport" not in ei.value.message and ei.value.hint == ""
    # a file that is there but cannot be started
    blocked = tmp_path / "blocked"
    blocked.write_text("not a program")
    blocked.chmod(0o600)
    with pytest.raises(EngineError) as ei:
        E.run_capped([str(blocked)], 5.0, what=E.WHAT_GH)
    assert ei.value.code == "spawn_failed" and ei.value.message.startswith(f"{E.WHAT_GH} could not be started")
    assert ei.value.hint == ""
    with pytest.raises(EngineError) as ei:
        E.run_capped([str(blocked)], 5.0)
    assert ei.value.message.startswith("botport could not be started") and ei.value.hint == E.TRUST_HINT


def test_a_missing_hermes_is_not_reported_as_a_missing_botport(monkeypatch, tmp_path):
    monkeypatch.setattr(importer, "hermes_argv", lambda profile: [str(tmp_path / "no-hermes-here")])
    monkeypatch.setattr(importer, "check_path_safety", lambda *a, **k: None)
    ok, why = importer.resume_cron("job-1", "acme-support")
    assert ok is False and "botport" not in why and why == f"{E.WHAT_HERMES} is not on this computer."


def test_a_missing_gh_is_not_reported_as_a_missing_botport(home, tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "botport"
    exe.parent.mkdir(mode=0o755)
    exe.write_text(f"#!{sys.executable} -I\n{FAKE.read_text(encoding='utf-8')}", encoding="utf-8")
    exe.chmod(0o755)
    os.chmod(tmp_path, 0o700)
    bp = _svc(botport_path=str(exe)).botport()
    # gh was found a moment ago and is gone when it is started
    monkeypatch.setattr(E, "find_gh", lambda path=None: (str(tmp_path / "gh-was-here"), None))
    res = bp._attest(str(exe), "0" * 64, None, E.EXPECTED_BOTPORT_TAG)
    assert res["verified"] is False and "botport" not in res["problem"]
    assert res["problem"] == f"{E.WHAT_GH} is not on this computer."


# ---- what is disclosed matches what the code does --------------------------------------------------------------

def test_the_disclosure_says_what_the_copy_and_the_page_really_do():
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    disclosure = (ROOT / "docs" / "DISCLOSURE.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    page = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
    flat = lambda text: " ".join(text.split())  # noqa: E731
    # the size cap in words is the one in the code
    assert f"at most {connect.HELPER_CAP // (1024 * 1024)} MB" in flat(disclosure) and "200 MB" not in disclosure
    # the copy of botport carries no download mark on macOS, and all three texts say what stands in for it
    for text in (manifest, disclosure, readme):
        assert "download mark" in flat(text) and "Gatekeeper" in text
    # what the page keeps outside the data folder is named, and so is what clears it
    for text in (manifest, disclosure, readme):
        assert "Hermes Desktop's storage" in flat(text) and "cannot reach" in flat(text)
    assert "Not in this folder" in disclosure and "hermes.plugin.grok-bot-twin." in disclosure
    # every kind of note the page keeps per twin is one the page clears when the twin goes
    import re
    kinds = re.search(r"const TWIN_NOTES = \[([^\]]+)\]", page).group(1).replace("'", "").replace(" ", "").split(",")
    stored = set(re.findall(r"`([a-z-]+)\.\$\{(?:pair\.pair_id|pairId)\}`", page))
    assert stored and stored <= set(kinds)
    assert "forgetTwin(storage, pair.pair_id)" in page and "forgetEverything(storage, shown)" in page
    # and while such a note is left, the page keeps offering the button that clears it
    assert "(hasData || notesLeft(storage)) &&" in page and "keeps offering" in flat(disclosure)
    # the terminal route never tells anyone to unpack an archive
    for text in (manifest, readme, disclosure):
        assert "tar -x" not in text
