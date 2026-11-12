"""The first minutes on a computer that has Hermes and nothing else (no Grok app, no botport): what the fresh-install
pass on a Linux, a macOS and a Windows machine found.

- plain `hermes twin` named botport as the next step on a computer that can never use it; the next step there is
  the link;
- `hermes twin doctor` told a linked computer to open the Grok app and to install `gh`, and said nothing about
  the link;
- on Windows a plugin installed from an administrator window cannot be opened by the same account's ordinary
  programs: `background on` waited 45 s and then promised a start that could not happen.
"""
from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from twin_testlib import ROOT, cli, config, home, service, svc  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import (bgagent, bgagent_windows as bgw, fleet, fleet_cli,
                                               fleet_profiles as fp, osflags)

PAGE = "the Twins page in Hermes Desktop"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def no_grok(home, tmp_path):
    """A computer without the Grok app (the conftest gives every other test one)."""
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"),
                         "grok_app_dir": str(tmp_path / "no-grok-app-here")})
    service.reset_service()
    return service.get_service()


def _follower(name: str = "mini", state: str = "in_sync", problem: dict | None = None) -> dict:
    src = {"name": name, "state": state, "last_sync_at": None}
    if problem:
        src["problem"] = problem
    return {"role": "follower", "this_machine": {"name": "Surface", "id": "m-1"}, "source": src, "twins": [],
            "machines": [], "removed": [], "can_share": False}


# ---- the first look ----------------------------------------------------------------------------------------------
def test_first_look_without_the_grok_app_names_the_link_as_the_one_next_step(no_grok):
    rc, out, err = run([])
    lines = out.splitlines()
    assert rc == 0 and err == ""
    assert lines[0] == ("Grok Bot Twin: the Grok app is not on this computer. Next: link it to the computer with "
                        "your Grok bots: hermes twin link CODE")
    assert out.count("Next:") == 1
    assert "Share my twins with my other computers" in lines[1] and "hermes twin share on" in lines[1]
    assert "hermes twin demo" in lines[2] and "sample" in lines[2]
    assert "botport" not in out and "hermes twin setup" not in out  # never the way for this computer
    assert run(["status"])[1] == out


def test_first_look_with_the_grok_app_is_unchanged(svc):
    rc, out, _ = run([])
    assert rc == 0
    assert out.splitlines()[0] == f"Grok Bot Twin: not connected to Grok yet. Next: open {PAGE}, or run hermes twin setup."
    assert "hermes twin link" not in out


# ---- doctor ------------------------------------------------------------------------------------------------------
def test_doctor_without_the_grok_app_offers_the_link_and_never_asks_for_gh(no_grok, monkeypatch):
    monkeypatch.setattr(service, "find_gh", lambda: (None, None))
    doc = no_grok.doctor()
    checks = {c["id"]: c for c in doc["checks"]}
    assert "linked_to" not in doc and "fleet_link" not in checks
    assert "hermes twin link CODE" in checks["grok_app"]["fix"]
    assert checks["gh_path"]["ok"] is False and checks["gh_path"]["fix"].startswith("Not needed here")
    assert "Install" not in checks["gh_path"]["fix"]
    assert checks["botport"]["fix"].startswith("Not needed here")
    rc, out, _ = run(["doctor"])
    lines = out.splitlines()
    assert rc == 0 and "gh auth login" not in out and "hermes twin setup" not in out
    assert lines[0] == (" (the Grok app is not on this computer. To have your twins here, link it to the computer "
                        "with your Grok bots: hermes twin link CODE. Lines marked – matter only on that computer)")
    assert "     (not needed here: botport is only for the computer with the Grok app)" in lines
    assert "fix: Not needed" not in out  # a reason is never printed as something to do


def test_doctor_with_the_grok_app_still_says_how_to_get_gh(svc, monkeypatch):
    monkeypatch.setattr(service, "find_gh", lambda: (None, None))
    checks = {c["id"]: c for c in svc.doctor()["checks"]}
    assert "gh auth login" in checks["gh_path"]["fix"]


def test_doctor_on_a_linked_computer_shows_the_link_first_and_asks_for_nothing(no_grok, monkeypatch):
    monkeypatch.setattr(service, "find_gh", lambda: (None, None))
    monkeypatch.setattr(no_grok, "fleet", lambda: _follower())
    doc = no_grok.doctor()
    first = doc["checks"][0]
    assert doc["linked_to"] == "mini"
    assert first["id"] == "fleet_link" and first["ok"] is True and first["message"] == "Surface follows mini: in sync"
    assert not first.get("optional")
    by = {c["id"]: c for c in doc["checks"]}
    for cid in ("botport", "grok_app", "gh_path"):
        assert by[cid]["ok"] is False and by[cid]["optional"] and by[cid]["fix"].startswith("Not needed here"), cid
    rc, out, _ = run(["doctor"])
    lines = out.splitlines()
    assert rc == 0
    assert lines[0] == (" (this computer gets its twins from mini: lines marked – matter only on the computer with "
                        "the Grok app; nothing to do for them here)")
    assert lines[1] == " ✓ Surface follows mini: in sync"
    assert "Open the Grok app" not in out and "gh auth login" not in out and "hermes twin setup" not in out
    assert "fix: Not needed" not in out and "     (not needed here: this computer's twins come over the link)" in lines


def test_doctor_on_a_linked_computer_says_when_the_link_is_in_trouble(no_grok, monkeypatch):
    trouble = {"message": "mini has not answered since 21:14.", "hint": "It catches up by itself when mini is back."}
    monkeypatch.setattr(no_grok, "fleet", lambda: _follower(state="offline", problem=trouble))
    first = no_grok.doctor()["checks"][0]
    assert first["id"] == "fleet_link" and first["ok"] is False and not first.get("optional")
    assert first["fix"] == "It catches up by itself when mini is back."


def test_doctor_never_fails_because_the_fleet_cannot_be_read(no_grok, monkeypatch):
    def boom():
        raise RuntimeError("no fleet today")
    monkeypatch.setattr(no_grok, "fleet", boom)
    doc = no_grok.doctor()
    assert "linked_to" not in doc and all(c["id"] != "fleet_link" for c in doc["checks"])


# ---- Windows: installed from an administrator window ---------------------------------------------------------------
def test_the_access_list_look_is_a_no_op_off_windows(tmp_path):
    if osflags.WINDOWS:
        pytest.skip("this is the macOS/Linux answer")
    assert osflags.closed_to_ordinary_programs(tmp_path) is False
    assert bgw.admin_install_problem() is None and bgw.admin_install_problem(note=True) is None


def test_background_on_refuses_at_once_when_the_plugin_is_closed_to_ordinary_programs(tmp_path, monkeypatch):
    monkeypatch.setattr(osflags, "closed_to_ordinary_programs", lambda path: True)
    ran = []
    root = tmp_path / "hermes-folder"
    root.mkdir()
    with pytest.raises(bgagent.AgentError) as exc:
        bgw.install(root=root, run=lambda argv: ran.append(argv) or (0, ""), hermes="C:\\hermes.exe")
    assert exc.value.code == "admin_install"
    assert "administrator window" in exc.value.message and "Nothing was changed" in exc.value.message
    assert ran == [] and list(root.iterdir()) == []  # no task, no file


def test_background_on_prints_the_one_line_that_fixes_it(home, monkeypatch):
    monkeypatch.setattr(osflags, "closed_to_ordinary_programs", lambda path: True)

    def refuse():
        raise bgagent.AgentError("admin_install", bgw.MSG_ADMIN_INSTALL)
    monkeypatch.setattr(bgagent, "install", refuse)
    rc, out, _ = run(["background", "on"])
    lines = out.splitlines()
    assert rc == 1 and lines[0].startswith("✗ Grok Bot Twin was installed from an administrator window")
    assert lines[1] == (f'  In an administrator window run:  icacls "{bgw.plugin_folder()}" /reset /T /C /Q'
                        "   and then: hermes twin background on")
    rc, out, _ = run(["background", "on", "--json"])
    import json
    doc = json.loads(out)
    assert rc == 1 and doc["ok"] is False and doc["code"] == "admin_install" and "icacls" in doc["fix"]


def test_the_first_look_and_doctor_say_it_too_on_windows(no_grok, monkeypatch):
    monkeypatch.setattr(osflags, "closed_to_ordinary_programs", lambda path: True)
    monkeypatch.setattr(osflags, "WINDOWS", True)
    check = [c for c in no_grok.doctor()["checks"] if c["id"] == "windows_admin_install"]
    assert len(check) == 1 and check[0]["ok"] is False and not check[0].get("optional")
    assert "administrator window" in check[0]["message"]
    assert check[0]["fix"] == f'In an administrator window run:  icacls "{bgw.plugin_folder()}" /reset /T /C /Q'
    rc, out, err = run([])
    assert rc == 0 and err.startswith(" ! Grok Bot Twin was installed from an administrator window")
    assert "icacls" in err and "icacls" not in out
    rc, out, err = run(["status", "--json"])
    assert err == ""  # never next to output a program reads


def test_no_note_when_the_install_is_an_ordinary_one(no_grok, monkeypatch):
    monkeypatch.setattr(osflags, "WINDOWS", True)
    monkeypatch.setattr(osflags, "closed_to_ordinary_programs", lambda path: False)
    assert run([])[2] == ""
    assert all(c["id"] != "windows_admin_install" for c in no_grok.doctor()["checks"])


# ---- the docs say how, in one line ------------------------------------------------------------------------------
def test_readme_gives_the_other_computer_one_install_line_and_one_link_line():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    part = (ROOT / "docs" / "SHARING.md").read_text(encoding="utf-8")  # moved out of the README (R14b)
    part = part.split("## A twin's memory on your other computers", 1)[0]
    text = " ".join((readme + part).split())  # where a line breaks is not what is pinned here
    part = " ".join(part.split())
    # the other computers install with the one `next` line at the top of the README, not the catalog name (0.2.1)
    assert "`next` install line" in part and "(../README.md)" in part and "0.2.1" in part
    assert "hermes plugins install grok-bot-twin --enable" not in part
    assert "On your other computers:** the same line" in " ".join(readme.split())
    assert "hermes twin link 7KQ4-M2XD-P9RA" in part
    assert "no Grok app" in part and "a scheduled task of your own account on Windows" in part
    assert "optional; Linux and macOS" not in text
    assert "hermes plugins install grok-bot-twin && hermes plugins enable grok-bot-twin" not in text


def test_the_disclosure_names_the_windows_helper():
    text = " ".join((ROOT / "docs" / "DISCLOSURE.md").read_text(encoding="utf-8").split())
    for word in ("schtasks.exe", "whoami.exe", "taskkill.exe", "one scheduled task of your own account"):
        assert word in text, word


# ---- updating the plugin on a computer that is already linked -----------------------------------------------------
# The fault: after the plugin was replaced, the background helper kept following with the code it
# had started with, and each twin's own copy of the plugin stayed the old one.
def test_the_update_look_changes_with_the_files_and_never_reads_them(tmp_path):
    src = tmp_path / "plugin"
    (src / "twin").mkdir(parents=True)
    (src / "plugin.yaml").write_text("name: grok-bot-twin\n", encoding="utf-8")
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "twin" / "a.py").write_text("x = 1\n", encoding="utf-8")
    first = fp.install_look(src)
    assert first and fp.install_look(src) == first
    (src / "twin" / "a.py").write_text("x = 1234\n", encoding="utf-8")
    assert fp.install_look(src) != first
    (src / "twin" / "__pycache__").mkdir()
    (src / "twin" / "__pycache__" / "a.pyc").write_bytes(b"\0")
    second = fp.install_look(src)
    (src / "twin" / "__pycache__" / "b.pyc").write_bytes(b"\0")
    assert fp.install_look(src) == second  # what Python writes by itself is not an update


def test_an_update_counts_once_the_same_new_files_were_seen_twice():
    looks = iter(["a", "a", "b", "c", "c", None, "c", "c", "a"])
    watch = fp.UpdateWatch(look=lambda: next(looks))
    assert watch.changed() is False                       # a: as at the start
    assert watch.changed() is False and watch.pending == "b"   # b: new, seen once
    assert watch.changed() is False and watch.pending == "c"   # c: still being written
    assert watch.changed() is True                        # c again: the update is in place
    assert watch.changed() is False and watch.pending is None  # None: the folder is being replaced, look later
    assert watch.changed() is False and watch.changed() is True
    assert watch.changed() is False                       # back to what this process runs: nothing to do


def test_mirrored_twins_get_the_installed_plugin_again_and_an_owners_install_is_left_alone(home):
    src = fp.plugin_source()
    assert src is not None
    profiles = fp.profiles_root()
    stale = profiles / "test-scout" / "plugins" / "grok-bot-twin"
    stale.mkdir(parents=True)
    (stale / fp.COPY_MARKER).write_text("an-older-version\n", encoding="utf-8")
    (stale / "plugin.yaml").write_text("name: grok-bot-twin\nversion: old\n", encoding="utf-8")
    own = profiles / "test-quill" / "plugins" / "grok-bot-twin"
    own.mkdir(parents=True)
    (own / "plugin.yaml").write_text("name: grok-bot-twin\nversion: the owner's own\n", encoding="utf-8")
    (profiles / "no-copy").mkdir()
    st = {"link": {"twins": {"t-1": {"profile": "test-scout"}, "t-2": {"profile": "test-quill"},
                             "t-3": {"profile": "no-copy"}, "t-4": {"profile": "default"},
                             "t-5": {"profile": "../outside"}, "t-6": "not a twin"}}}
    assert fleet.refresh_mirrored_copies(st) == 2
    assert (stale / fp.COPY_MARKER).read_text(encoding="utf-8").strip() == fp._source_stamp(src)
    assert (stale / "plugin.yaml").read_text(encoding="utf-8") == (src / "plugin.yaml").read_text(encoding="utf-8")
    assert (own / "plugin.yaml").read_text(encoding="utf-8") == "name: grok-bot-twin\nversion: the owner's own\n"
    assert not (profiles / "no-copy" / "plugins").exists() and not (profiles / "default").exists()
    assert fleet.refresh_mirrored_copies({"link": None}) == 0 and fleet.refresh_mirrored_copies({}) == 0


def test_a_process_that_runs_from_a_twins_copy_never_hands_its_version_on(home, monkeypatch):
    profiles = fp.profiles_root()
    mine = profiles / "test-scout" / "plugins" / "grok-bot-twin"  # where this process "runs from": a marked copy
    assert fp.place_plugin(profiles / "test-scout") and (mine / fp.COPY_MARKER).is_file()
    other = profiles / "test-quill" / "plugins" / "grok-bot-twin"
    other.mkdir(parents=True)
    (other / fp.COPY_MARKER).write_text("a-newer-version\n", encoding="utf-8")
    (other / "plugin.yaml").write_text("name: grok-bot-twin\nversion: newer\n", encoding="utf-8")
    monkeypatch.setattr(fp, "plugin_source", lambda: mine)
    st = {"link": {"twins": {"t-1": {"profile": "test-scout"}, "t-2": {"profile": "test-quill"}}}}
    assert fleet.refresh_mirrored_copies(st) == 0
    assert (other / "plugin.yaml").read_text(encoding="utf-8") == "name: grok-bot-twin\nversion: newer\n"
    assert (other / fp.COPY_MARKER).read_text(encoding="utf-8") == "a-newer-version\n"


def test_the_printed_fix_is_never_a_line_that_a_shell_would_read_differently(monkeypatch):
    monkeypatch.setattr(osflags, "closed_to_ordinary_programs", lambda path: True)
    plain = bgw.admin_install_problem()
    assert plain is not None and plain[1].startswith('In an administrator window run:  icacls "')
    from pathlib import PureWindowsPath
    for odd in ("C:\\Users\\example\\$(calc)\\plugins\\grok-bot-twin", "C:\\Users\\example\\%USERNAME%\\p",
                "C:\\Users\\example\\a`b\\p", 'C:\\Users\\example\\a"b\\p', "C:\\Users\\example\\a\nb\\p"):
        monkeypatch.setattr(bgw, "plugin_folder", lambda odd=odd: PureWindowsPath(odd))
        fix = bgw.admin_install_problem(note=True)[1]
        assert fix == bgw.ADMIN_INSTALL_FIX_PLAIN and odd not in fix and "$(" not in fix, odd
    monkeypatch.setattr(bgw, "plugin_folder", lambda: PureWindowsPath("C:\\Users\\example\\Some One\\.hermes\\plugins\\grok-bot-twin"))
    assert bgw.admin_install_problem()[1] == ('In an administrator window run:  icacls '
                                             '"C:\\Users\\example\\Some One\\.hermes\\plugins\\grok-bot-twin" /reset /T /C /Q')


def test_only_the_helper_counts_as_started_by_the_helper(monkeypatch):
    import argparse
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    plain = argparse.Namespace(twin_command="fleet", what="run")
    under_linux_unit = fleet_cli.started_by_helper(plain)  # True only when the test itself runs inside that unit
    assert under_linux_unit is False
    assert fleet_cli.started_by_helper(argparse.Namespace(twin_command="background", action="run")) is True
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.grok-bot-twin.follower.twin-lab-fresh-d90000")
    assert fleet_cli.started_by_helper(plain) is True
    monkeypatch.setenv("XPC_SERVICE_NAME", "application.com.apple.Terminal.1")
    assert fleet_cli.started_by_helper(plain) is False


class _Loop:
    """fleet_cli.run with everything around the loop taken away: no threads, no sockets, no signal handlers."""

    def __init__(self, monkeypatch, by_helper: bool):
        self.ticks = 0
        outer = self

        class Svc:
            def fleet_tick(self):
                outer.ticks += 1
                raise KeyboardInterrupt  # what Ctrl-C does: the loop ends cleanly

            def fleet(self):
                return {}

        class Changed:
            pending = None

            def changed(self):
                return True

        monkeypatch.setattr(fleet_cli, "_svc", lambda: Svc())
        monkeypatch.setattr(fleet_cli.signal, "signal", lambda *a: None)
        monkeypatch.setattr(fleet_cli.follow, "ensure_started", lambda *a, **k: None)
        from hermes_plugins.grok_bot_twin.twin import tasks_run  # (the loop imports it when it ends)
        for mod in (fleet_cli.follow, fleet_cli.server, tasks_run):
            monkeypatch.setattr(mod, "stop", lambda *a, **k: None)
        monkeypatch.setattr(fp, "UpdateWatch", Changed)
        monkeypatch.setattr(fleet_cli, "started_by_helper", lambda args=None: by_helper)

    def run(self):
        import argparse
        out = io.StringIO()
        with redirect_stdout(out):
            rc = fleet_cli.run(argparse.Namespace(twin_command="fleet", what="run"))
        return rc, out.getvalue()


def test_the_helpers_loop_ends_after_an_update_so_that_the_new_version_starts(home, monkeypatch):
    loop = _Loop(monkeypatch, by_helper=True)
    rc, out = loop.run()
    assert rc == fleet_cli.RESTART_STATUS == 75 and loop.ticks == 0
    assert out.strip().endswith("Grok Bot Twin was updated on this computer: starting again with the new version")
    # the helper's unit starts it again after this status: it is not one of the "the command is gone" ones
    assert str(fleet_cli.RESTART_STATUS) not in bgagent.NO_RESTART_STATUS.split()


def test_a_loop_typed_into_a_terminal_says_so_and_carries_on(home, monkeypatch):
    loop = _Loop(monkeypatch, by_helper=False)
    rc, out = loop.run()
    assert rc == 0 and loop.ticks == 1  # it went on to its ordinary look
    assert "this loop still runs the version from before" in out and "Stop it (Ctrl-C) and start it again" in out
    assert out.strip().endswith("stopped")
