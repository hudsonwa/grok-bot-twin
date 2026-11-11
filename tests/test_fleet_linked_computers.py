"""Sharing between computers, on the side of the linked ones: one test for each fault, with the case that
shows it. Invented twins and computers only; sockets on loopback.

- with a share port set in Settings, a twin's chat opened a second share port on the default number and requests
  were refused with "switched off on NAME" (one Hermes process serving every bot)
- after "Remove Grok Bot Twin" on the Mac the other computers promised "catch up when it is switched on again"
- after an unlink Hermes' own list still showed the bots (folders a running Hermes made again)
- two removal sentences left out that a computer which is off loses its copies at its next contact
- a "stays" fact under "Removed" on a linked computer
- "unlinked from mini on mini"
- nothing told a linked computer's page when a twin's name or description last changed
(requests between computers are in test_tasks_reasons_and_switch.py, the Grok side in
test_grok_side_plain_answers.py)
"""
from __future__ import annotations

import sys
import types

import pytest

from fleet_testlib import Machine, free_port, wait_until
from twin_testlib import config, home, service, store  # noqa: F401
from test_fleet_units import SRC, T1, T2, _apply, _linked, _list  # noqa: F401
from test_fleet_relink_and_wording import _hermes_made_it_again
from hermes_plugins.grok_bot_twin.twin import fleet, fleet_api, fleet_cli, fleet_follow as follow
from hermes_plugins.grok_bot_twin.twin import fleet_profiles as fp

SCOUT = {"id": T1, "name": "Test Scout", "description": "Finds trails.", "version": 1, "updated_at": None}
QUILL = {"id": T2, "name": "Test Quill", "description": "Drafts short notes.", "version": 1, "updated_at": None}


@pytest.fixture
def source(tmp_path):
    m = Machine(tmp_path, "mini", free_port())
    m.fx("add", "Test Scout", "Finds trails.")
    m.fx("add", "Test Quill", "Drafts short notes.")
    yield m
    m.stop()


@pytest.fixture
def surface(home, source, tmp_path, monkeypatch):  # noqa: F811
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "share_port": source.port,
                         "machine_name": "Surface"})
    monkeypatch.setattr(follow, "MIN_BACKOFF_S", 0.3)
    monkeypatch.setattr(follow, "MAX_BACKOFF_S", 1.0)
    return service.get_service()


# ---- one Hermes process that serves every bot --------------------------------------------------------------------------
def test_a_twins_chat_inside_a_process_that_serves_every_bot_reads_the_main_folders_settings(home, monkeypatch):  # noqa: F811
    """Hermes Desktop with "one process for every bot": the process's HERMES_HOME is the main folder, and the bot a
    chat belongs to is named for the current task only. The plugin took such a chat for the main folder's own,
    read the twin's (empty) settings through Hermes and got the default share port."""
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n  - grok-bot-twin\n  entries:\n    grok-bot-twin:\n      settings:\n"
        "        machine_name: Mac mini\n        share_port: 18802\n", encoding="utf-8")
    twin_home = home / "profiles" / "test-scout"
    twin_home.mkdir(parents=True)
    (twin_home / "config.yaml").write_text("plugins:\n  enabled:\n  - grok-bot-twin\n", encoding="utf-8")
    monkeypatch.setattr(store, "hermes_root", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(home))  # the process is the main folder's ...
    current = {"home": None}
    consts = types.ModuleType("hermes_constants")
    consts.get_hermes_home_override = lambda: current["home"]
    consts.get_hermes_home = lambda: current["home"] or str(home)
    cfg = types.ModuleType("hermes_cli.config")
    # Hermes' own reader answers for the bot in use, as the real one does
    cfg.load_config_readonly = lambda: (
        {} if current["home"] else {"plugins": {"entries": {"grok-bot-twin": {"settings": {
            "machine_name": "Mac mini", "share_port": 18802}}}}})
    monkeypatch.setitem(sys.modules, "hermes_constants", consts)
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", cfg)
    config.set_override(None)
    assert config._inside_a_bot() == (None, None) and config.get("share_port") == 18802
    current["home"] = str(twin_home)  # ... and this task is a chat with the twin
    # (asked at once, inside the five seconds one answer is remembered: the two are never mixed up)
    assert config._inside_a_bot() == (home, str(twin_home))
    assert config.get("share_port") == 18802 and config.get("machine_name") == "Mac mini"
    assert follow._port_setting() == 18802
    from hermes_plugins.grok_bot_twin.twin import fleet_server
    assert fleet_server._port() == 18802  # no second share port on the default number
    # what only the twin's own bot sets still fills in what the main folder leaves open
    cfg.load_config_readonly = lambda: {"plugins": {"entries": {"grok-bot-twin": {"settings": {
        "share_port": 18777, "sync_interval_seconds": 30}}}}}
    config.set_override(None)
    assert config.get("share_port") == 18802 and config.get("sync_interval_seconds") == 30
    current["home"] = None
    config.set_override(None)


# ---- Grok Bot Twin removed on the Mac ------------------------------------------------------------------------------
def test_removal_on_the_mac_is_said_as_such_on_a_linked_computer_and_nothing_is_taken_away(source, surface, home):  # noqa: F811
    surface.fleet_link(source.share())
    wait_until(lambda: follow.running() and follow._state["holding"], what="the follower loop")
    soul = (home / "profiles" / "test-scout" / "SOUL.md").read_bytes()
    # "Remove Grok Bot Twin" on the Mac: it notes that it is leaving, then switches sharing off (the plugin's own
    # order; the standalone runner has no uninstall, so the two steps are done here as the plugin does them)
    st = source.state()
    st["share"]["leaving"] = True
    source.write_state(st)
    source.run("share", "off")
    f = wait_until(lambda: (lambda x: x if x["source"]["state"] == "error" else None)(surface.fleet()),
                   what="the removal to be noticed")
    p = f["source"]["problem"]
    assert p["code"] == "source_removed"
    assert p["message"] == "Grok Bot Twin was removed on mini, so your twins here no longer follow it."
    assert p["hint"] == ("They stay as they are. If it is set up again on mini, unlink this computer and link it "
                         "again with a new link code.")
    assert "catch up" not in p["message"] + p["hint"] and "switched on again" not in p["message"] + p["hint"]
    assert len(f["twins"]) == 2 and (home / "profiles" / "test-scout" / "SOUL.md").read_bytes() == soul
    assert fleet_cli.describe(f)[0] == "Surface follows mini: Grok Bot Twin was removed there."
    assert "removed on the other computer" in fleet_api.mirrored_summary()["state"]
    # the Mac stops listening altogether: still the same sentence, not "has not answered ... when it is back"
    wait_until(lambda: not source.listening(), what="mini to stop listening")
    source.stop()
    assert follow.sync_once(surface) == "offline" and follow.sync_once(surface) == "offline"
    f = surface.fleet()
    assert f["source"]["state"] == "error" and f["source"]["problem"]["code"] == "source_removed"
    whats = [e["payload"].get("what") for e in surface.events()["events"] if e["type"] == "fleet"]
    assert "source_back" not in whats
    # had it only been a slip (sharing switched on again with the same data), a signed answer puts all right
    source.run("share", "on")
    assert source.state()["share"]["leaving"] is False
    source.start()
    wait_until(source.listening, what="mini to listen again")
    wait_until(lambda: surface.fleet()["source"]["state"] == "in_sync", what="back in sync")
    assert surface.fleet()["source"]["problem"] is None


def test_a_plain_sharing_off_still_says_switch_it_on_again(source, surface, home):  # noqa: F811
    surface.fleet_link(source.share())
    wait_until(lambda: follow.running() and follow._state["holding"], what="the follower loop")
    source.run("share", "off")
    f = wait_until(lambda: (lambda x: x if x["source"]["state"] == "sharing_off" else None)(surface.fleet()),
                   what="sharing off to be noticed")
    assert f["source"]["problem"]["code"] == "sharing_off"


# ---- who unlinked, said once -----------------------------------------------------------------------------------------
def test_the_terminal_says_who_unlinked_without_naming_the_mac_twice(home, tmp_path):  # noqa: F811
    _linked(home, tmp_path)
    with store.locked():
        st = fleet.load()
        fleet.drop_link(st, "source")
        fleet.save(st)
    lines = fleet_cli.describe(service.get_service().fleet())
    said = next(x for x in lines if x.startswith("It was unlinked"))
    assert said.startswith("It was unlinked from mini by mini, ") and " on mini" not in said
    with store.locked():
        st = fleet.load()
        st["last_unlink"]["by"] = "this"
        fleet.save(st)
    said = next(x for x in fleet_cli.describe(service.get_service().fleet()) if x.startswith("It was unlinked"))
    assert said.startswith("It was unlinked from mini here, ")


# ---- when a new name or description arrived here -------------------------------------------------------------------------
def test_a_linked_computers_row_can_say_when_a_new_description_arrived(home, tmp_path):  # noqa: F811
    svc = _linked(home, tmp_path)
    _apply(_list([SCOUT, QUILL]))
    rows = {t["name"]: t for t in svc.fleet()["twins"]}
    # made, not changed: the row can say "arrived 14 sec ago" (it said "changed" of copies that had just arrived)
    assert rows["Test Scout"]["changed"] == "new" and fleet.parse_iso(rows["Test Scout"]["changed_at"])
    _apply(_list([dict(SCOUT, description="Finds trails and huts.", version=2), QUILL], rev="4-0a0b0c0d"))
    rows = {t["name"]: t for t in svc.fleet()["twins"]}
    assert fleet.parse_iso(rows["Test Scout"]["changed_at"]) and rows["Test Scout"]["changed"] == "description"
    assert rows["Test Scout"]["description"] == "Finds trails and huts." and rows["Test Quill"]["changed"] == "new"
    first = rows["Test Scout"]["changed_at"]
    _apply(_list([dict(SCOUT, description="Finds trails and huts.", version=2),
                  dict(QUILL, name="Test Quill Pen", version=2)], rev="5-0a0b0c0d"))
    rows = {t["name"]: t for t in svc.fleet()["twins"]}
    assert rows["Test Quill Pen"]["changed"] == "name" and rows["Test Scout"]["changed_at"] == first  # (not touched)


# ---- the removal sentences --------------------------------------------------------------------------------------------
def test_removal_sentences_say_when_an_absent_computer_loses_its_copies_and_keep_stays_under_stays(home, tmp_path):  # noqa: F811
    svc = _linked(home, tmp_path)
    _apply(_list([SCOUT, QUILL]))
    plan = svc.uninstall(True)
    assert "The link to mini. Its 2 twins here stop following." in plan["goes"]
    assert "The 2 twins on this computer, as ordinary Hermes bots." in plan["stays"]
    assert not any("stay" in line for line in plan["goes"])  # no "stays" fact under Removed


# ---- the program that is in the way of sharing gets its name -----------------------------------------------------------
def test_the_program_that_holds_the_share_port_is_named_when_the_system_says_it(monkeypatch):
    from hermes_plugins.grok_bot_twin.twin import fleet_server as server
    asked = []

    def fake(out):
        def run(argv):
            asked.append(argv)
            # a fixed program by its full path, never a shell
            assert all(isinstance(a, str) for a in argv) and (argv[0].startswith("/") or argv[0][1:3] == ":\\")
            return out(argv) if callable(out) else out
        return run

    import os
    monkeypatch.setattr(server, "_holder", (0.0, -1, ""))
    monkeypatch.setattr(os.path, "isfile", lambda p: p == "/usr/sbin/lsof")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(server, "_run_quiet", fake("p4242\ncMusic\\x20Box\nf12\n"))
    assert server.holder_name(18777) == "Music Box"
    assert server.holder_name(18777) == "Music Box" and len(asked) == 1  # asked of the system once a minute at most
    said = fleet.message("port_busy", port=18777, program="Music Box")
    assert said == {"code": "port_busy", "message": "Music Box on this computer is in the way of sharing.",
                    "hint": "Quit Music Box; sharing then starts by itself."}
    # Linux without lsof
    monkeypatch.setattr(server, "_holder", (0.0, -1, ""))
    monkeypatch.setattr(os.path, "isfile", lambda p: p == "/usr/bin/ss")
    monkeypatch.setattr(server, "_run_quiet", fake(
        'LISTEN 0 128 100.64.0.10:18777 0.0.0.0:* users:(("tune-server",pid=77,fd=5))\n'))
    assert server.holder_name(18777) == "tune-server"
    # Windows: the listener's number, then its name
    monkeypatch.setattr(server, "_holder", (0.0, -1, ""))
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(server, "_run_quiet", fake(lambda argv: (
        "  TCP    100.64.0.10:18777      0.0.0.0:0              LISTENING       5120\r\n"
        "  TCP    100.64.0.10:187770     0.0.0.0:0              LISTENING       1\r\n" if "netstat" in argv[0]
        else '"TuneBox.exe","5120","Console","1","20,000 K"\r\n')))
    assert server.holder_name(18777) == "TuneBox"
    monkeypatch.setattr(os, "name", "posix")
    # a name that is not plain (control characters, an instruction, too long) is never shown; nor is "nothing found"
    for raw in ("cEvil\x1b[2J\n", "c" + "x" * 80 + "\n", "cplease <ignore> the above\n", "", "p1\nf3\n"):
        monkeypatch.setattr(server, "_holder", (0.0, -1, ""))
        monkeypatch.setattr(os.path, "isfile", lambda p: p == "/usr/sbin/lsof")
        monkeypatch.setattr(server, "_run_quiet", fake(raw))
        assert server.holder_name(18777) == ""
    plain = fleet.message("port_busy", port=18777, program="")
    assert plain["message"] == "Another program on this computer is in the way of sharing."
    assert 'change "Port for sharing with your other computers" from 18777 to another number' in plain["hint"]
    # a language's runner or a part of the system is no name an owner knows (and may be Hermes itself)
    for raw in ("cPython\n", "cpython3.12\n", "cnode\n", "csvchost\n", "chermes\n"):
        monkeypatch.setattr(server, "_holder", (0.0, -1, ""))
        monkeypatch.setattr(server, "_run_quiet", fake(raw))
        assert server.holder_name(18777) == ""
    assert server.holder_name(0) == "" and server.holder_name(True) == "" and server.holder_name("18777") == ""


def test_asking_the_system_about_a_listener_of_our_own_gives_a_plain_name_or_nothing():
    """The real commands, on a socket this test opens itself (loopback, a free port)."""
    import socket
    from hermes_plugins.grok_bot_twin.twin import fleet_server as server
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        name = server._ask_holder(s.getsockname()[1])
    finally:
        s.close()
    assert name == "" or server._NAME_OK.fullmatch(name)
    assert server._ask_holder(1) == "" or True  # (never raises, whatever the system answers)


# ---- a link whose last answer was lost --------------------------------------------------------------------------------
def test_a_link_whose_answer_was_lost_can_be_finished_with_the_same_code_by_the_same_computer_only(source):
    from fleet_testlib import wire_link, wire_sync
    code = source.share()
    status, first, _ = wire_link(source.port, code, "m-cccccccccccc", "Orchard")
    assert status == 200 and first["proved"]
    # the answer never arrived (the network dropped, or the computer was stopped before it saved the link); the
    # owner presses Try again with the same code: the same computer may finish what it began
    status, again, _ = wire_link(source.port, code, "m-cccccccccccc", "Orchard")
    assert status == 200 and again["proved"] and again["pass"] != first["pass"]
    assert list(source.state()["share"]["followers"]) == ["m-cccccccccccc"]  # one computer, once
    # only the newest pass counts; the lost one opens nothing
    got: dict = {}
    status, _obj, _ = wire_sync(source.port, first["pass"], answer=got)
    assert status == 401 and not got["signed"]
    status, lst, _ = wire_sync(source.port, again["pass"], answer=got)
    assert status == 200 and got["signed"] and len(lst["twins"]) == 2
    # any other computer: the code is used, as before, and it does not count as a try of the first one's
    status, obj, _ = wire_link(source.port, code, "m-dddddddddddd", "Other")
    assert (status, obj["error"]["code"]) == (403, "code_used")
    # once that computer has synced, the code is used for it too
    status, _obj, _ = wire_sync(source.port, again["pass"], have=lst["rev"],
                                applied={"rev": lst["rev"], "at": "2000-01-01T00:00:00Z",
                                         "twins": {t["id"]: t["version"] for t in lst["twins"]}, "failed": {}})
    assert status == 200
    status, obj, _ = wire_link(source.port, code, "m-cccccccccccc", "Orchard")
    assert (status, obj["error"]["code"]) == (403, "code_used")
    assert list(source.state()["share"]["followers"]) == ["m-cccccccccccc"]


def test_the_second_use_of_a_code_keeps_its_ten_minutes_its_five_tries_and_ends_with_a_new_code(source):
    from fleet_testlib import wire_link
    code = source.share()
    assert wire_link(source.port, code, "m-cccccccccccc")[0] == 200
    st = source.state()
    old = st["share"]["old_codes"][-1]
    assert old["state"] == "used" and old["by"] == "m-cccccccccccc" and old["until"] and old["tries"] == 1
    # wrong guesses in that computer's name use up the same five tries (one was used by the link itself)
    wrong = "2345-6789-ABCD" if code.replace("-", "") != "23456789ABCD" else "3456-789A-BCDE"
    seen = [wire_link(source.port, wrong, "m-cccccccccccc")[1]["error"]["code"] for _ in range(5)]
    assert seen[:4] == ["code_wrong"] * 4 and seen[4] == "too_many_tries"
    status, obj, _ = wire_link(source.port, code, "m-cccccccccccc")
    assert status == 403 and obj["error"]["code"] in ("too_many_tries", "code_used")
    # a new code ends the old one's second life at once; a code past its ten minutes has none
    new = source.code()
    assert wire_link(source.port, code, "m-cccccccccccc")[0] == 403
    assert wire_link(source.port, new, "m-eeeeeeeeeeee")[0] == 200
    st = source.state()
    st["share"]["old_codes"][-1]["until"] = "2000-01-01T00:00:00Z"
    source.stop()
    source.write_state(st)
    source.start()
    wait_until(source.listening, what="mini to listen again")
    status, obj, _ = wire_link(source.port, new, "m-eeeeeeeeeeee")
    assert (status, obj["error"]["code"]) == (403, "code_used")


# ---- Hermes' own list went on showing a bot that was moved away --------------------------------------------------------
def _mark(home, profile):  # noqa: F811
    return home / "profiles" / ".deleted" / profile


def test_a_twin_moved_away_is_noted_the_way_hermes_notes_a_bot_that_is_gone_and_the_note_goes_when_it_returns(home, tmp_path):  # noqa: F811
    """A running Hermes made the folder again within seconds of an unlink and its list of bots went on showing
    "test-scout", which could not be opened. Hermes does not list, serve or re-make a bot whose name has a note in
    `profiles/.deleted` (its own rename leaves one for the same reason); the plugin now leaves that note."""
    svc = _linked(home, tmp_path)
    _apply(_list([SCOUT, QUILL]))
    assert not (home / "profiles" / ".deleted").exists()
    with store.locked():
        st = fleet.load()
        fleet.drop_link(st, "this")
        fleet.save(st)
    for prof in ("test-scout", "test-quill"):
        assert _mark(home, prof).read_text(encoding="utf-8") == fp.MOVED_MARK and not (home / "profiles" / prof).exists()
        assert not fp.name_taken(prof)  # the plugin's own note does not make the name taken
    # what a running Hermes still made again in the meantime is set aside as before, and the twins are back under
    # their own names with no note left
    _hermes_made_it_again(home / "profiles" / "test-scout", soul=False)
    assert _mark(home, "test-scout").is_file()  # (a leftover of Hermes' is no reason to take the note away)
    svc = _linked(home, tmp_path)
    _apply(_list([SCOUT, QUILL]))
    assert sorted(t["profile"] for t in svc.fleet()["twins"]) == ["test-quill", "test-scout"]
    assert not (home / "profiles" / ".deleted").exists()  # no note, and no empty folder left behind either
    assert (home / "profiles" / "test-scout" / "SOUL.md").is_file()
    # a bot of the owner's put at a kept twin's name by hand is never hidden: the note goes at the next look
    with store.locked():
        st = fleet.load()
        fleet.drop_link(st, "this")
        fleet.save(st)
    own = home / "profiles" / "test-quill"
    own.mkdir()
    (own / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    assert _mark(home, "test-quill").is_file()
    with store.locked():
        st = fleet.load()
        fleet.prune_kept(st)
    assert not _mark(home, "test-quill").exists() and _mark(home, "test-scout").is_file()


def test_the_note_is_only_ever_the_plugins_own_and_never_hides_a_bot_of_the_owners(home, tmp_path):  # noqa: F811
    svc = _linked(home, tmp_path)
    _apply(_list([SCOUT, QUILL]))
    # 1. Hermes' own note (the owner deleted a bot of that name in Hermes) is never written over or taken away
    assert fp.mark_moved("default") is False and fp.mark_moved("../x") is False and fp.mark_moved(None) is False
    (home / "profiles" / ".deleted").mkdir()
    _mark(home, "old-bot").write_text("deleted\n", encoding="utf-8")
    assert fp.mark_moved("old-bot") is False and fp.clear_moved("old-bot") is False
    assert _mark(home, "old-bot").read_text(encoding="utf-8") == "deleted\n" and fp.name_taken("old-bot")
    # 2. never while a bot with anything of the owner's sits at the name
    assert fp.mark_moved("test-scout") is False and not _mark(home, "test-scout").exists()
    # 3. a twin removed on the Mac: noted; "Keep this bot" brings it back as an ordinary bot, and the note goes
    _apply(_list([SCOUT], removed=[{"id": T2, "removed_at": "2000-01-01T10:00:00Z"}], rev="4-0a0b0c0d"))
    assert _mark(home, "test-quill").is_file()
    rid = svc.fleet()["removed"][0]["rid"]
    svc.fleet_restore(rid)
    assert not _mark(home, "test-quill").exists() and (home / "profiles" / "test-quill" / "SOUL.md").is_file()
    # 4. a note that is a link, or longer than the plugin's own text, is not the plugin's
    with store.locked():
        st = fleet.load()
        fleet.drop_link(st, "this")
        fleet.save(st)
    assert _mark(home, "test-scout").is_file()
    _mark(home, "test-scout").write_text(fp.MOVED_MARK + "and more\n", encoding="utf-8")
    assert fp.clear_moved("test-scout") is False and fp.name_taken("test-scout")
    # 5. Remove Grok Bot Twin takes the plugin's notes with it and leaves Hermes' own
    _mark(home, "test-scout").write_text(fp.MOVED_MARK, encoding="utf-8")
    svc.uninstall(False)
    assert not _mark(home, "test-scout").exists() and _mark(home, "old-bot").is_file()


def test_the_note_goes_when_a_kept_bots_thirty_days_are_over(home, tmp_path):  # noqa: F811
    _linked(home, tmp_path)
    _apply(_list([SCOUT]))
    with store.locked():
        st = fleet.load()
        fleet.drop_link(st, "this")
        assert _mark(home, "test-scout").is_file()
        st["kept"][0]["kept_until"] = "2000-01-01T00:00:00Z"
        assert fleet.prune_kept(st) is True
        fleet.save(st)
    assert not _mark(home, "test-scout").exists()


# ---- a stale entry: an unlink the Mac could not hear ---------------------------------------------------------------------
def test_an_unlink_the_mac_could_not_hear_is_told_again_later_and_then_forgotten(source, surface, home):  # noqa: F811
    surface.fleet_link(source.share())
    wait_until(lambda: len(source.state()["share"]["followers"]) == 1, what="mini to list the computer")
    source.stop()  # the Mac is off (or its sharing is): nothing listens
    wait_until(lambda: not source.listening(), what="mini to stop listening")
    surface.fleet_unlink(False)
    gb = fleet.load()["goodbye"]
    assert gb and gb["tries"] == 1 and fleet.load()["link"] is None
    assert len(source.state()["share"]["followers"]) == 1  # the Mac still lists it: this is what stayed for ever
    assert follow.retry_goodbye() is None  # not due yet: nothing is asked before its time
    assert follow.retry_goodbye(force=True) is False and fleet.load()["goodbye"]["tries"] == 2
    assert fleet.load()["goodbye"]["next_at"] > gb["next_at"]  # less and less often
    source.start()
    wait_until(source.listening, what="mini to listen again")
    assert follow.retry_goodbye(force=True) is True
    assert fleet.load()["goodbye"] is None  # heard: the address and pass kept for it are gone
    wait_until(lambda: source.state()["share"]["followers"] == {}, what="mini to drop the computer from its list")
    assert follow.retry_goodbye(force=True) is None


def test_an_unsigned_yes_to_a_goodbye_does_not_count_as_heard(home, monkeypatch):  # noqa: F811
    """Another computer at the Mac's address could answer "200" to the goodbye. Without the Mac's signature that is
    not the Mac having heard, so the goodbye is kept and said again."""
    ln = {"host": "127.0.0.1", "port": 9, "pass": "p" * 43, "source": {"id": "m-aaaaaaaaaaaa", "name": "mini"}}
    for answer, heard in (((200, {"unlinked": True}, False), False), ((200, {"unlinked": True}, True), True),
                          ((401, {"error": {"code": "unlinked"}}, False), True), ((503, {}, False), False)):
        monkeypatch.setattr(follow, "_counted_request", lambda *a, _r=answer, **k: _r)
        assert follow.say_goodbye(ln) is heard, answer


def test_a_kept_goodbye_ends_with_a_new_link_or_after_thirty_days(source, surface, home):  # noqa: F811
    code = source.share()
    surface.fleet_link(code)
    source.stop()
    wait_until(lambda: not source.listening(), what="mini to stop listening")
    surface.fleet_unlink(False)
    assert fleet.load()["goodbye"]
    with store.locked():
        st = fleet.load()
        st["goodbye"]["since"] = "2000-01-01T00:00:00Z"
        fleet.save(st)
    assert follow.retry_goodbye() is False and fleet.load()["goodbye"] is None  # given up, nothing kept
    # an unlink that WAS heard keeps nothing; linking again clears an older one
    source.start()
    wait_until(source.listening, what="mini to listen again")
    with store.locked():
        st = fleet.load()
        st["goodbye"] = {"host": "127.0.0.1", "port": source.port, "pass": "x" * 43, "since": fleet.now_iso(),
                         "tries": 3, "next_at": 0}
        fleet.save(st)
    surface.fleet_link(source.code())
    assert fleet.load()["goodbye"] is None
    surface.fleet_unlink(False)
    assert fleet.load()["goodbye"] is None and fleet.load()["link"] is None


# ---- nothing left to remove after the sample, and when a computer was last seen -----------------------------------------
def test_after_end_sample_on_a_computer_with_nothing_else_there_is_nothing_to_remove(home):  # noqa: F811
    svc = service.get_service()
    assert svc.status(fresh=True)["has_data"] is False
    svc.demo_action("start")
    assert svc.status(fresh=True)["has_data"] is True
    svc.demo_action("remove")
    left = sorted(p.name for p in (home / "plugin-data" / "grok-bot-twin").iterdir())
    assert left and set(left) <= set(svc.WORKING_FILES)  # the plugin's own working files only
    assert svc.status(fresh=True)["has_data"] is False     # so the first screen offers no "Remove Grok Bot Twin…"
    svc.uninstall(False)                                   # the terminal's uninstall still clears them
    assert sorted(p.name for p in (home / "plugin-data" / "grok-bot-twin").iterdir()) == []


def test_with_sharing_off_the_terminal_says_when_a_computer_was_last_seen(home, tmp_path):  # noqa: F811
    f = {"role": "source", "can_share": True, "this_machine": {"id": "m-3f9a1c0b7d2e", "name": "mini", "platform": "macos"},
         "share": {"on": False, "state": "off", "network": None, "address": None, "port": 18777, "since": None,
                   "problem": None, "code": None},
         "source": None, "twins": [], "removed": [], "last_unlink": None,
         "machines": [{"id": "m-3f9a1c0b7d2e", "name": "mini", "platform": "macos", "role": "source", "this": True,
                       "state": "in_sync", "last_seen_at": None, "last_sync_at": None, "twins": 0, "failed": 0},
                      {"id": "m-77aa01bc9d10", "name": "Surface", "platform": "windows", "role": "follower",
                       "this": False, "state": "offline", "last_seen_at": "2000-01-01T09:00:00Z",
                       "last_sync_at": None, "twins": 2, "failed": 0},
                      {"id": "m-0123456789ab", "name": "Orchard", "platform": "linux", "role": "follower",
                       "this": False, "state": "never_synced", "last_seen_at": None, "last_sync_at": None,
                       "twins": 0, "failed": 0}]}
    text = "\n".join(fleet_cli.describe(f))
    assert "  Surface, Windows: last seen " in text and "Surface, Windows: waiting" not in text
    assert "  Orchard, Linux: waiting for sharing to be switched on" in text  # never seen: nothing else to say
    assert "Surface last seen " in fleet_cli._headline(f)
