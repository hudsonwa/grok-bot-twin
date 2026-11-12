"""Linking a computer again, a twin's own Hermes bot, and the words said about linked computers: one test for
each fault, with the case that shows it. Invented twins and computers only; no network (the two-machine
cases are in test_fleet_machines.py and test_tasks_machines.py).

- Remove Grok Bot Twin left the fleet note and the plugin's switch in each twin's Hermes bot
- linking again under a running Hermes Desktop gave `name-2` beside an empty folder
- "in sync 31 sec ago" three seconds after a twin had arrived
- a refused request did not show on the asked computer
- inside a twin's own Hermes bot the plugin read that bot's settings (another computer name, the default share
  port)
- words: brackets, folder names and "source" in sentences the owner reads
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from twin_testlib import ROOT, central, config, home, service, store  # noqa: F401
from test_ux_flows import fake  # noqa: F401
from test_setup_download_and_twin_facts import twin  # noqa: F401
from test_fleet_units import SRC, T1, T2, _apply, _linked, _list, _registry
from hermes_plugins.grok_bot_twin.twin import fleet, fleet_cli, fleet_follow as follow, fleet_net as net
from hermes_plugins.grok_bot_twin.twin import fleet_profiles as fp, tasks, tasks_hub
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

SCOUT = {"id": T1, "name": "Test Scout", "description": "Finds trails.", "version": 1, "updated_at": None}
QUILL = {"id": T2, "name": "Test Quill", "description": "Drafts short notes.", "version": 1, "updated_at": None}
HERMES_SOUL = "You are Hermes.\n"  # stands in for Hermes' own starting prompt file in these tests


def _relink(home, tmp_path):  # noqa: F811
    with store.locked():
        st = fleet.load()
        n = fleet.drop_link(st, "this")
        fleet.save(st)
    return n


def _session_db(path, chats: int = 0, open_tabs: int = 0) -> None:
    """A fresh Hermes session database as Hermes makes it: no chat, but rows of Hermes'
    own in a handful of tables (one of them on no list of known tables)."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sessions (id TEXT)")
    con.execute("CREATE TABLE messages (id TEXT, session_id TEXT)")
    con.execute("CREATE TABLE schema_version (v INTEGER)")
    con.execute("INSERT INTO schema_version VALUES (1)")
    con.execute("CREATE TABLE runtime_epoch (singleton INTEGER, epoch INTEGER, instance_id TEXT)")
    con.execute("INSERT INTO runtime_epoch VALUES (1, 1, 'x')")
    con.execute("CREATE TABLE hosted_room_policy_capacity (singleton INTEGER, max_active_events INTEGER)")
    con.execute("INSERT INTO hosted_room_policy_capacity VALUES (1, 8)")
    for i in range(chats):
        con.execute("INSERT INTO sessions VALUES (?)", (f"s{i}",))
        con.execute("INSERT INTO messages VALUES (?, ?)", (f"m{i}", f"s{i}"))
    for i in range(open_tabs):  # a chat tab that was open in Hermes Desktop: noted again, nothing said in it
        con.execute("INSERT INTO sessions VALUES (?)", (f"tab{i}",))
    con.commit()
    con.close()


def _hermes_made_it_again(prof, soul: bool = True) -> None:
    """What a running Hermes Desktop puts where a twin's bot has just been moved away."""
    for sub in ("logs", "sessions", "memories", "skills", "cron", "workspace"):
        (prof / sub).mkdir(parents=True, exist_ok=True)
    (prof / "logs" / "agent.log").write_text("started\n", encoding="utf-8")
    _session_db(prof / "state.db")
    (prof / "projects.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)  # not on any list of known files
    (prof / "gateway.lock").write_text("123\n", encoding="utf-8")
    if soul:
        (prof / "SOUL.md").write_text(HERMES_SOUL, encoding="utf-8")


# ---- linking again brings the same Hermes bot back, once ----------------------------------------------------------
def test_linking_again_puts_the_kept_bot_back_under_its_own_name_and_sets_hermes_leftover_aside(
        home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fp.config, "hermes_default_soul", lambda text: text == HERMES_SOUL)
    svc = _linked(home, tmp_path)
    assert _apply(_list([SCOUT, QUILL]))["added"] == 2
    scout = home / "profiles" / "test-scout"
    (scout / "memories").mkdir(exist_ok=True)
    (scout / "memories" / "MEMORY.md").write_text("the owner's chat left this", encoding="utf-8")
    assert _relink(home, tmp_path) == 2 and not scout.exists()
    for name in ("test-scout", "test-quill"):
        _hermes_made_it_again(home / "profiles" / name, soul=name == "test-scout")
    assert fp.is_leftover(scout) and not fp.is_shell(scout)  # the old empty-folder rule did not cover this
    blank = home / "profiles" / "blank-db"
    blank.mkdir()
    sqlite3.connect(blank / "state.db").close()  # made a moment ago: no table in it yet
    (blank / "state.db").touch()
    assert fp.is_leftover(blank)
    blank.rename(tmp_path / "blank-db")
    kept_before = sorted(p.name for p in (central(home) / "fleet-kept").iterdir())
    _linked(home, tmp_path)
    assert _apply(_list([SCOUT, QUILL])) == {"added": 2, "updated": 0, "removed": 0, "failed": 0}
    view = svc.fleet()
    assert sorted(t["profile"] for t in view["twins"]) == ["test-quill", "test-scout"]  # not test-scout-2
    assert all("note" not in t for t in view["twins"]) and view["removed"] == []
    assert sorted(p.name for p in (home / "profiles").iterdir()) == ["test-quill", "test-scout"]  # no ghost bots
    assert (scout / "memories" / "MEMORY.md").read_text(encoding="utf-8") == "the owner's chat left this"
    assert fp.is_mirrored(scout) and fp.plugin_enabled(scout)
    # nothing was deleted: what Hermes had made is in the kept place, whole, under ids of its own
    aside = [p for p in (central(home) / "fleet-kept").iterdir() if p.name not in kept_before]
    assert len(aside) == 2 and all(fleet.RID_RE.match(p.name) for p in aside)
    assert sorted((p / "profile" / "projects.db").is_file() for p in aside) == [True, True]
    assert any((p / "profile" / "SOUL.md").is_file() for p in aside)
    assert fleet.load()["kept"] == []  # and the page's "Kept Hermes bots" does not list them


def test_a_bot_folder_whose_database_hermes_is_just_making_is_waited_for_before_the_twin_gets_another_name(
        home, tmp_path, monkeypatch):  # noqa: F811
    """Hermes makes a moved bot's folder again a moment after Unlink, and
    for some seconds its new session database cannot be read. A link made in that time took the folder for
    the owner's and gave the twins back as `test-quill-2`."""
    import time
    svc = _linked(home, tmp_path)
    assert _apply(_list([SCOUT]))["added"] == 1
    assert _relink(home, tmp_path) == 1
    scout = home / "profiles" / "test-scout"
    scout.mkdir()
    _session_db(scout / "state.db")  # what Hermes has put there by the time anyone can look
    real, t0, calls = fp._has_chat, time.monotonic(), []

    def not_yet(db):  # ...but for the first two seconds it cannot be read
        calls.append(time.monotonic() - t0)
        return None if calls[-1] < 2.0 else real(db)
    monkeypatch.setattr(fp, "_has_chat", not_yet)
    _linked(home, tmp_path)
    assert _apply(_list([SCOUT]))["added"] == 1
    (row,) = svc.fleet()["twins"]
    assert row["profile"] == "test-scout" and len(calls) >= 3  # its own name back, after looking again
    # a file that is no database at all is not waited for: about a second, as before, and hands off
    odd = home / "profiles" / "odd"
    odd.mkdir()
    (odd / "state.db").write_bytes(b"not a database at all" * 500)
    monkeypatch.setattr(fp, "_has_chat", real)
    t1 = time.monotonic()
    assert not fp.is_leftover(odd) and time.monotonic() - t1 < 4
    # and one that never becomes readable is given up after SETTLE_S, the folder left alone
    monkeypatch.setattr(fp, "SETTLE_S", 1.5)
    monkeypatch.setattr(fp, "_has_chat", lambda db: None)
    never = home / "profiles" / "never"
    never.mkdir()
    _session_db(never / "state.db")
    t2 = time.monotonic()
    assert not fp.is_leftover(never) and 1.4 < time.monotonic() - t2 < 5 and (never / "state.db").is_file()


def _desktop_made_it_again(prof):
    """What Hermes Desktop 0.22.7 had put where a twin's bot had just been moved away, as it is after
    Unlink and, half a minute later, Link again, with the twin's chat still open in a tab. Returns the
    connection that is still writing to the session database."""
    for sub in ("platforms/pairing", "cache", "plugins", "plugin-update-checks", "runtime/bot_live_delivery",
                "logs/update_receipts", "cron/output", "skills"):
        (prof / sub).mkdir(parents=True, exist_ok=True)
    # (skills/.curator_state: Hermes' own note, written some minutes after it began serving the folder again)
    for rel in ("gateway.lock", "state.db.fts_rebuild.lock", "cache/schema_columns.json", "skills/.curator_state",
                "plugin-update-checks/last-run", "plugin-update-checks/results.json",
                "runtime/bot_live_delivery/.lock", "logs/update_receipts/latest.json", "cron/ticker_heartbeat",
                "cron/ticker_last_success", "cron/.tick.lock", "cron/.jobs.lock"):
        (prof / rel).write_text("{}\n", encoding="utf-8")
    sqlite3.connect(prof / "cron" / "executions.db").close()
    _session_db(prof / "state.db", open_tabs=1)
    con = sqlite3.connect(prof / "state.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE gateway_heartbeats (at TEXT)")
    con.execute("INSERT INTO gateway_heartbeats VALUES ('now')")
    con.commit()
    if os.name == "nt":  # (there a folder with a file still open cannot be moved at all)
        con.close()
        return None
    con.execute("BEGIN IMMEDIATE")  # ...and it is in the middle of its next write
    con.execute("INSERT INTO gateway_heartbeats VALUES ('next')")
    return con


def test_link_again_under_hermes_desktop_with_a_chat_tab_open_gives_every_twin_its_own_name_back(
        home, tmp_path):  # noqa: F811
    """Unlink, then Link again in Hermes Desktop gave Test Lark back as "Test Lark 2", with the
    sentence "A Hermes bot called Test Lark was already here" although none ever was, beside stray empty bots.
    What stood at the old names was Hermes Desktop's own: `platforms/pairing`, a `gateway.lock`, and a session
    database, still being written, that named the open chat tab and held no message."""
    svc = _linked(home, tmp_path)
    assert _apply(_list([SCOUT, QUILL]))["added"] == 2
    scout, quill = home / "profiles" / "test-scout", home / "profiles" / "test-quill"
    (scout / "memories").mkdir(exist_ok=True)
    (scout / "memories" / "MEMORY.md").write_text("the owner's chat left this", encoding="utf-8")
    assert _relink(home, tmp_path) == 2 and not scout.exists()
    writing = [_desktop_made_it_again(p) for p in (scout, quill)]
    try:
        assert not fp.is_shell(scout) and fp.is_leftover(scout) and fp.is_leftover(scout, patient=False)
        assert not fp.bot_in_the_way("Test Scout")
        (scout / "skills" / "notes.md").write_text("how I take notes", encoding="utf-8")  # the owner's: hands off
        assert not fp.is_leftover(scout) and fp.bot_in_the_way("Test Scout")
        (scout / "skills" / "notes.md").rename(tmp_path / "notes.md")
        (scout / "skills" / "deeper").mkdir()
        (scout / "skills" / "deeper" / ".curator_state").write_text("{}", encoding="utf-8")  # not where Hermes keeps it
        assert not fp.is_leftover(scout)
        (scout / "skills" / "deeper" / ".curator_state").rename(tmp_path / "moved-out")
        assert fp.is_leftover(scout)
        kept_before = sorted(p.name for p in (central(home) / "fleet-kept").iterdir())
        _linked(home, tmp_path)
        assert _apply(_list([SCOUT, QUILL])) == {"added": 2, "updated": 0, "removed": 0, "failed": 0}
        view = svc.fleet()
        assert sorted(t["profile"] for t in view["twins"]) == ["test-quill", "test-scout"]  # no "Test Scout 2"
        assert all("note" not in t for t in view["twins"])
        assert sorted(p.name for p in (home / "profiles").iterdir()) == ["test-quill", "test-scout"]  # no stray bot
        assert (scout / "memories" / "MEMORY.md").read_text(encoding="utf-8") == "the owner's chat left this"
        # nothing was deleted: what Hermes Desktop had made is in the kept place, whole
        aside = [p for p in (central(home) / "fleet-kept").iterdir() if p.name not in kept_before]
        assert len(aside) == 2 and all((p / "profile" / "platforms" / "pairing").is_dir() for p in aside)
        assert all((p / "profile" / "gateway.lock").is_file() for p in aside) and fleet.load()["kept"] == []
    finally:
        for con in writing:
            if con is not None:
                con.close()


def test_a_twin_sent_to_a_second_name_gets_it_freed_too_and_the_sentence_is_said_only_while_true(
        home, tmp_path):  # noqa: F811
    svc = _linked(home, tmp_path)
    _apply(_list([SCOUT]))
    scout, second = home / "profiles" / "test-scout", home / "profiles" / "test-scout-2"
    (scout / "memories").mkdir(exist_ok=True)
    (scout / "memories" / "MEMORY.md").write_text("the owner's chat left this", encoding="utf-8")
    _relink(home, tmp_path)
    scout.mkdir()
    (scout / "SOUL.md").write_text("You are my own trail planner.\n", encoding="utf-8")  # the owner's, made since
    held = [_desktop_made_it_again(second)]  # and Hermes Desktop's own leftover where the twin goes instead
    try:
        _linked(home, tmp_path)
        assert _apply(_list([SCOUT]))["added"] == 1
        (row,) = svc.fleet()["twins"]
        assert row["profile"] == "test-scout-2" and "so this twin is Test Scout 2" in row["note"]  # true here
        assert (second / "memories" / "MEMORY.md").is_file() and not (second / "gateway.lock").exists()
        assert (scout / "SOUL.md").read_text(encoding="utf-8") == "You are my own trail planner.\n"
        assert sorted(p.name for p in (home / "profiles").iterdir()) == ["test-scout", "test-scout-2"]
        # the owner's bot goes; Hermes Desktop makes its folder again: no bot called Test Scout is here, so the
        # page does not say one is
        scout.rename(tmp_path / "the-owners-bot-deleted")
        held.append(_desktop_made_it_again(scout))
        (row,) = svc.fleet()["twins"]
        assert row["profile"] == "test-scout-2" and "note" not in row
        assert fleet.clash_note("test-scout-2", "Test Scout") is None
    finally:
        for con in held:
            if con is not None:
                con.close()


@pytest.mark.parametrize("what", ["config", "chat", "prompt", "memory", "job", "link", "skill", "session", "unreadable"])
def test_a_folder_with_anything_of_the_owners_is_never_set_aside(home, tmp_path, monkeypatch, what):  # noqa: F811
    monkeypatch.setattr(fp.config, "hermes_default_soul", lambda text: text == HERMES_SOUL)
    svc = _linked(home, tmp_path)
    _apply(_list([SCOUT]))
    _relink(home, tmp_path)
    prof = home / "profiles" / "test-scout"
    _hermes_made_it_again(prof)
    if what == "config":
        (prof / "config.yaml").write_text("model:\n  default: some-model\n", encoding="utf-8")
    elif what == "chat":
        (prof / "state.db").unlink()
        _session_db(prof / "state.db", chats=1)
    elif what == "prompt":
        (prof / "SOUL.md").write_text("You are my trail planner. Be brief.\n", encoding="utf-8")
    elif what == "memory":
        (prof / "memories" / "MEMORY.md").write_text("likes maps", encoding="utf-8")
    elif what == "skill":  # (a bot made with `hermes profile create` and never used yet has its skills)
        (prof / "skills" / "notes").mkdir()
        (prof / "skills" / "notes" / "SKILL.md").write_text("how to take notes", encoding="utf-8")
    elif what == "session":
        (prof / "sessions" / "s1.json").write_text("{}", encoding="utf-8")
    elif what == "unreadable":  # a session database nobody can read: it may hold a chat, so hands off
        (prof / "state.db").write_bytes(b"not a database at all" * 500)
    elif what == "job":
        (prof / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "j1"}]}), encoding="utf-8")
    else:
        (prof / "notes").symlink_to(tmp_path)
    before = sorted(str(p.relative_to(prof)) for p in prof.rglob("*"))
    assert not fp.is_leftover(prof) and not fp.set_aside("test-scout", central(home) / "fleet-kept")
    _linked(home, tmp_path)
    assert _apply(_list([SCOUT]))["added"] == 1
    (row,) = svc.fleet()["twins"]
    assert row["profile"] == "test-scout-2" and sorted(str(p.relative_to(prof)) for p in prof.rglob("*")) == before
    # said in people's words, with the name Hermes now shows for the twin, and no folder name
    assert row["note"] == ("A Hermes bot called Test Scout was already here, so this twin is Test Scout 2. "
                           "The other one was left as it is.")
    meta = (home / "profiles" / "test-scout-2" / "profile.yaml").read_text(encoding="utf-8")
    assert "display_name: Test Scout 2" in meta
    # ... and said again after every later unlink and link (it used to go missing the second time)
    _relink(home, tmp_path)
    _linked(home, tmp_path)
    _apply(_list([SCOUT]))
    (row,) = svc.fleet()["twins"]
    assert row["profile"] == "test-scout-2" and "so this twin is Test Scout 2" in row["note"]
    assert sorted(p.name for p in (home / "profiles").iterdir()) == ["test-scout", "test-scout-2"]


def test_a_twin_whose_bot_was_deleted_here_is_made_again_where_it_was(home, tmp_path, monkeypatch):  # noqa: F811
    """The twin's Hermes bot is deleted on this computer while Hermes is open; Hermes makes the folder again
    before the next look at the list. The twin is made again under its own name, not beside that folder."""
    monkeypatch.setattr(fp.config, "hermes_default_soul", lambda text: text == HERMES_SOUL)
    svc = _linked(home, tmp_path)
    _apply(_list([SCOUT, QUILL]))
    scout = home / "profiles" / "test-scout"
    scout.rename(tmp_path / "deleted-by-the-owner")
    _hermes_made_it_again(scout)
    assert _apply(_list([SCOUT, QUILL]))["added"] == 1
    view = svc.fleet()
    assert sorted(t["profile"] for t in view["twins"]) == ["test-quill", "test-scout"]
    assert all("note" not in t for t in view["twins"]) and fp.is_mirrored(scout)
    assert sorted(p.name for p in (home / "profiles").iterdir()) == ["test-quill", "test-scout"]
    (aside,) = list((central(home) / "fleet-kept").iterdir())
    assert (aside / "profile" / "projects.db").is_file() and fleet.load()["kept"] == []
    # a bot the owner made there since is still left alone, and the twin goes beside it
    quill = home / "profiles" / "test-quill"
    quill.rename(tmp_path / "also-deleted")
    quill.mkdir()
    (quill / "SOUL.md").write_text("You are my own note taker.\n", encoding="utf-8")
    assert _apply(_list([SCOUT, QUILL]))["added"] == 1
    assert (quill / "SOUL.md").read_text(encoding="utf-8") == "You are my own note taker.\n"
    row = next(t for t in svc.fleet()["twins"] if t["name"] == "Test Quill")
    assert row["profile"] == "test-quill-2" and "so this twin is Test Quill 2" in row["note"]


def test_the_name_hermes_shows_for_a_twin_under_a_numbered_folder():
    assert fp.shown_name("test-scout", "Test Scout") == "Test Scout"
    assert fp.shown_name("test-scout-2", "Test Scout") == "Test Scout 2"
    assert fp.shown_name("test-scout-11", "Test  Scout") == "Test Scout 11"
    assert fp.shown_name("test-scout-2", "Test Scout 2") == "Test Scout 2"   # its own name, not a clash
    assert fp.shown_name("test-scout-2", "Trail Guide") == "Trail Guide"     # renamed since: no number, no note
    assert fp.shown_name("test-scout-2", "Test") == "Test"
    assert fleet.clash_note("test-scout", "Test Scout") is None and fleet.clash_note("test-scout-2", "Test") is None
    assert fleet.clash_note(None, "Test Scout") is None and fleet.clash_note("test-scout-2", "") is None


def test_keep_this_bot_also_gets_its_old_name_back(home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fp.config, "hermes_default_soul", lambda text: text == HERMES_SOUL)
    svc = _linked(home, tmp_path)
    _apply(_list([SCOUT]))
    assert _apply(_list([], removed=[{"id": T1, "removed_at": "2000-01-02T09:00:00Z"}], rev="4-0a0b0c0d"))["removed"] == 1
    _hermes_made_it_again(home / "profiles" / "test-scout")
    (kept,) = svc.fleet()["removed"]
    out = svc.fleet_restore(kept["rid"])
    assert out["removed"] == [] and fp.twin_text(home / "profiles" / "test-scout") == ""  # an ordinary bot now
    assert "# Test Scout" in (home / "profiles" / "test-scout" / "SOUL.md").read_text(encoding="utf-8")


# ---- Remove Grok Bot Twin on a computer that never shared ---------------------------------------------------------
def test_remove_grok_bot_twin_takes_the_note_and_the_switch_out_of_every_twin(twin, home):  # noqa: F811
    svc = twin
    prof = home / "profiles" / "acme-support"
    svc.fleet_tick()  # what every Hermes start does: the twin's own bot gets the note and the plugin's switch
    owners = (prof / "SOUL.md").read_text(encoding="utf-8").split(fp.FLEET_BEGIN)[0]
    assert fp.soul_fleet_note(prof) and fp.plugin_enabled(prof) and (prof / "plugins" / "grok-bot-twin").is_dir()
    assert not fleet.exists()  # this computer never shared and never linked
    svc.uninstall(False)
    soul = (prof / "SOUL.md").read_text(encoding="utf-8")
    assert fp.FLEET_BEGIN not in soul and "fleet note" not in soul and soul == owners
    assert fp.plugin_enabled(prof) is False and "grok-bot-twin" not in (prof / "config.yaml").read_text(encoding="utf-8")
    assert not (prof / "plugins" / "grok-bot-twin").exists()
    assert prof.is_dir()  # the Hermes bot itself stays


# ---- when this computer last heard from the other one --------------------------------------------------------------
def test_the_time_of_the_last_answer_is_never_older_than_the_last_change(home, tmp_path):  # noqa: F811
    svc = _linked(home, tmp_path)
    with store.locked():
        st = fleet.load()
        st["link"].update(state="in_sync", last_contact_at="2000-01-01T09:00:10Z", last_sync_at="2000-01-01T09:00:41Z")
        fleet.save(st)
    view = svc.fleet()
    assert view["source"]["last_contact_at"] == "2000-01-01T09:00:41Z"
    assert next(m for m in view["machines"] if m["this"])["last_seen_at"] == "2000-01-01T09:00:41Z"
    with store.locked():
        st = fleet.load()
        st["link"].update(last_contact_at="2000-01-01T09:02:00Z")
        fleet.save(st)
    assert svc.fleet()["source"]["last_contact_at"] == "2000-01-01T09:02:00Z"
    with store.locked():
        st = fleet.load()
        st["link"].update(last_contact_at=None, last_sync_at=None)
        fleet.save(st)
    assert svc.fleet()["source"]["last_contact_at"] is None


# ---- settings inside a twin's own Hermes bot ------------------------------------------------------------------------
def test_inside_a_twins_own_bot_the_settings_are_the_main_hermes_folders(home, monkeypatch):  # noqa: F811
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n  - grok-bot-twin\n  entries:\n    grok-bot-twin:\n      settings:\n"
        "        machine_name: MacBook Pro\n        share_port: 18793\n", encoding="utf-8")
    twin_home = home / "profiles" / "test-scout"
    twin_home.mkdir(parents=True)
    (twin_home / "config.yaml").write_text("plugins:\n  enabled:\n  - grok-bot-twin\n", encoding="utf-8")
    monkeypatch.setattr(store, "hermes_root", lambda: home)
    config.set_override(None)
    assert config._inside_a_bot() == (None, None)  # the main folder's own process reads its file the usual way
    monkeypatch.setenv("HERMES_HOME", str(twin_home))
    config.set_override(None)
    assert config.get("machine_name") == "MacBook Pro" and config.get("share_port") == 18793
    assert follow._port_setting() == 18793  # (linking again from there looked on the default port: "could not reach")
    assert fleet.this_machine(fleet.load())["name"] == "MacBook Pro"
    # a twin's folder that has just been moved away (unlink while it was the open bot) changes nothing, and Hermes'
    # own reader is not asked about it (it would set the folder up again)
    import sys
    import types
    asked = []
    fake_cfg = types.ModuleType("hermes_cli.config")
    fake_cfg.load_config_readonly = lambda: asked.append(1) or {}
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_cfg)
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / "gone"))
    config.set_override(None)
    assert config.get("machine_name") == "MacBook Pro" and asked == []
    monkeypatch.setenv("HERMES_HOME", str(twin_home))
    config.set_override(None)
    assert config.get("machine_name") == "MacBook Pro" and asked == [1]
    # a main file that cannot be read, or is a link, counts as no settings
    (home / "config.yaml").unlink()
    (home / "config.yaml").symlink_to(twin_home / "config.yaml")
    config.set_override(None)
    assert config.get("machine_name") == "" and config.get("share_port") == 18777


def test_a_twins_chat_in_a_hermes_that_serves_all_bots_reads_the_main_settings_too(home, monkeypatch):  # noqa: F811
    """Hermes Desktop serves every bot from one process: HERMES_HOME names the main folder and the bot of each
    chat is marked per chat (hermes_constants.get_hermes_home). The fault: with the computer named
    "Studio" in the main settings, a twin's chat there was told "This chat runs on <the computer's own name>" beside
    a fleet note that said "You are the Studio copy", and for a few seconds the page and the loop were handed the twin
    bot's settings (the default share port, no name)."""
    import sys
    import types
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n  - grok-bot-twin\n  entries:\n    grok-bot-twin:\n      settings:\n"
        "        machine_name: Studio\n        share_port: 18793\n", encoding="utf-8")
    twin_home = home / "profiles" / "test-scout"
    twin_home.mkdir(parents=True)
    (twin_home / "config.yaml").write_text("plugins:\n  enabled:\n  - grok-bot-twin\n", encoding="utf-8")
    monkeypatch.setattr(store, "hermes_root", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(home))  # the one process is the main folder's
    chat_of = {"home": home}  # which bot's chat the code is working for right now
    fake_constants = types.ModuleType("hermes_constants")
    fake_constants.get_hermes_home = lambda: chat_of["home"]
    fake_constants.get_hermes_home_override = lambda: None if chat_of["home"] == home else chat_of["home"]
    fake_constants.get_default_hermes_root = lambda: home
    fake_cfg = types.ModuleType("hermes_cli.config")
    own = {str(home): {"plugins": {"entries": {"grok-bot-twin": {"settings": {"machine_name": "Studio",
                                                                               "share_port": 18793}}}}},
           str(twin_home): {"plugins": {"entries": {"grok-bot-twin": {"settings": {"inject_context": False}}}}}}
    fake_cfg.load_config_readonly = lambda: own[str(chat_of["home"])]
    monkeypatch.setitem(sys.modules, "hermes_constants", fake_constants)
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_cfg)
    config.set_override(None)
    assert config._inside_a_bot() == (None, None)
    assert config.get("machine_name") == "Studio" and config.get("inject_context") is True
    chat_of["home"] = twin_home  # a turn of the twin's chat, in the same process, within the same 5 seconds
    assert config._inside_a_bot() == (home, str(twin_home))
    assert config.get("machine_name") == "Studio" and config.get("share_port") == 18793
    assert fleet.this_machine(fleet.load())["name"] == "Studio"
    assert config.get("inject_context") is False  # the twin bot's own file still fills in what the main one leaves open
    chat_of["home"] = home  # back at the page and the loop: never the twin bot's settings
    assert config.get("inject_context") is True and config.get("share_port") == 18793


def test_a_computers_own_name_is_tidied(monkeypatch):
    import socket
    for host, want in (("example-pc_box.local", "example pc box"), ("Example-MacBook-Pro.lan", "Example MacBook Pro"),
                       ("mini", "mini"), ("---", "This computer"), ("", "This computer")):
        monkeypatch.setattr(socket, "gethostname", lambda host=host: host)
        assert fleet.default_machine_name() == want


# ---- the plugin's copy inside each twin follows the installed plugin ------------------------------------------------
def test_copies_of_the_plugin_are_made_from_the_install_never_from_an_old_copy(home, tmp_path, monkeypatch):  # noqa: F811
    main = home / "plugins" / "grok-bot-twin"
    copy = home / "profiles" / "test-scout" / "plugins" / "grok-bot-twin"
    for d in (main, copy):
        (d / "twin").mkdir(parents=True)
        (d / "plugin.yaml").write_text("name: grok-bot-twin\n", encoding="utf-8")
        (d / "__init__.py").write_text("", encoding="utf-8")
    (copy / fp.COPY_MARKER).write_text("0123456789abcdef\n", encoding="utf-8")
    real = fp.plugin_source()
    monkeypatch.setattr(fp, "__file__", str(copy / "twin" / "fleet_profiles.py"))
    assert fp.plugin_source() == main          # this code runs from a twin's copy: the install is the source
    (main / "plugin.yaml").unlink()
    assert fp.plugin_source() == copy.resolve()  # no install to be found: as before
    monkeypatch.setattr(fp, "__file__", str(main / "twin" / "fleet_profiles.py"))
    assert fp.plugin_source() is None and real == ROOT


def test_a_following_computers_twins_get_the_current_plugin_at_start_and_after_an_update(
        home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fp, "_install_sig", None)
    svc = _linked(home, tmp_path)
    _apply(_list([SCOUT, QUILL]))
    marker = home / "profiles" / "test-scout" / "plugins" / "grok-bot-twin" / fp.COPY_MARKER
    current = marker.read_text(encoding="utf-8")
    marker.write_text("0000000000000000\n", encoding="utf-8")  # a copy from before the plugin was updated
    theirs = home / "profiles" / "my-own"
    (theirs / "plugins" / "grok-bot-twin").mkdir(parents=True)
    svc.fleet_tick()
    assert marker.read_text(encoding="utf-8") == current
    assert list((theirs / "plugins" / "grok-bot-twin").iterdir()) == []  # not a twin's folder: not touched
    # the plugin is updated while Hermes runs: the copies follow at the next background check, before any restart
    marker.write_text("1111111111111111\n", encoding="utf-8")
    svc.fleet_tick()
    assert marker.read_text(encoding="utf-8") == "1111111111111111\n"  # nothing changed on disk: nothing is done
    monkeypatch.setattr(fp, "_install_sig", (("plugin.yaml", 1, 1),))  # as if the installed files were other ones
    svc.fleet_tick()
    assert marker.read_text(encoding="utf-8") == current and fp._install_sig != (("plugin.yaml", 1, 1),)
    assert fp.install_changed() is False


# ---- a refused request shows on the asked computer too --------------------------------------------------------------
def test_a_refused_request_is_in_the_asked_computers_own_log(home):  # noqa: F811
    me, surface, orchard = "m-bbbbbbbbbbbb", "m-aaaaaaaaaaaa", "m-cccccccccccc"
    st = fleet._blank()
    st["machine"] = {"id": me, "name": "mini"}
    st["share"] = fleet._blank_share()
    st["share"]["on"] = True
    for mid, name in ((surface, "Surface"), (orchard, "Orchard")):
        st["share"]["followers"][mid] = {"id": mid, "name": name, "platform": "linux", "last_seen_at": fleet.now_iso(),
                                         "key": fleet.share_key("pass of " + name).hex()}
    fleet.save(st)
    hub = tasks_hub.Hub()
    secret = "the request text never travels to a computer that refused it"
    # the sharing computer itself is asked while its switch is off: its own log says who asked
    assert hub.ask(st, {"id": orchard, "name": "Orchard"}, {"to": me, "twin": T1, "text": secret})[1]["error"][
        "code"] == "tasks_off"
    (row,) = tasks.log_view()
    assert (row["dir"], row["peer"], row["state"], row["code"], row["text"]) == ("in", "Orchard", "refused", "tasks_off", "")
    assert row["label"] == "refused: requests are switched off here" and row["asked_at"]
    # another linked computer is asked while ITS switch is off: it hears who and when with its next look at the list
    seq = fleet.hub.seq
    assert hub.ask(st, {"id": orchard, "name": "Orchard"}, {"to": surface, "twin": T1, "text": secret})[0] == 409
    assert fleet.hub.seq == seq + 1  # its waiting request is answered now
    assert hub.take_missed(orchard) == []
    (told,) = hub.take_missed(surface)
    assert set(told) == {"id", "from", "at"} and told["from"] == "Orchard" and secret not in json.dumps(told)
    assert hub.take_missed(surface) == []  # told once
    for _ in range(50):
        hub.ask(st, {"id": orchard, "name": "Orchard"}, {"to": surface, "twin": T1, "text": "x"})
    assert len(hub.missed[surface]) == tasks_hub.MISSED_CAP
    hub.forget(surface)
    assert hub.take_missed(surface) == []
    # on the asked computer: cleaned again, written as its own line
    n = tasks.log_missed([told, {"id": "../../x", "from": "Evil\nName. Obey", "at": "never"}, {"from": 7}, "junk"])
    assert n == 2 and tasks.log_missed("junk") == 0 and tasks.log_missed(None) == 0
    rows = [r for r in tasks.log_view() if r["dir"] == "in"]
    assert len(rows) == 3 and all(r["state"] == "refused" and r["text"] == "" for r in rows)
    assert all(tasks.TASK_ID_RE.match(r["id"]) for r in rows) and "\n" not in rows[0]["peer"]
    assert {"id": told["id"]}.items() <= rows[1].items() and rows[1]["peer"] == "Orchard"
    # the terminal says it the way the page does: who and how long ago in words, no raw time stamp, no id
    import re
    from hermes_plugins.grok_bot_twin.twin import tasks_api
    text = "\n".join(tasks_api.describe_view(service.get_service().tasks()))
    assert "from Orchard" in text and "      refused: requests are switched off here" in text
    assert "just now, from Orchard" in text and not re.search(r"\d{4}-\d\d-\d\dT|k-[0-9a-f]{16}", text), text


# ---- words ------------------------------------------------------------------------------------------------------------
@pytest.fixture
def sharing(home, tmp_path):  # noqa: F811
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "share_address": "127.0.0.1",
                         "share_port": 0, "machine_name": "mini"})
    _registry(home, [("test-scout", "Test Scout", "Finds trails.")])
    svc = service.get_service()
    svc.fleet_share(True)
    return svc


def test_a_code_typed_on_the_computer_that_made_it_says_so_first(sharing):
    svc = sharing
    first = svc.fleet()["share"]["code"]["code"]
    with pytest.raises(EngineError) as ei:
        svc.fleet_link(first.lower().replace("-", " "))
    assert ei.value.code == "own_code" and str(ei.value.message) == "That code is from this computer."
    assert "sharing off" not in (ei.value.hint or "")
    second = svc.fleet_code()["code"]["code"]
    for code in (first, second):  # also the one it replaced a moment ago
        with pytest.raises(EngineError) as ei:
            svc.fleet_link(code)
        assert ei.value.code == "own_code", code
    other = net.pretty_code(net.make_code("127.0.0.1"))
    with pytest.raises(EngineError) as ei:
        svc.fleet_link(other)
    assert ei.value.code == "sharing_here"  # a code from somewhere else: this computer shares, so it cannot follow
    assert svc.fleet()["role"] == "source"


def test_the_terminal_says_it_in_the_pages_words(sharing):
    svc = sharing
    with store.locked():
        st = fleet.load()
        st["share"]["followers"]["m-77aa01bc9d10"] = {
            "id": "m-77aa01bc9d10", "name": "MacBook Pro", "platform": "macos", "key": "0" * 64, "linked_at": None,
            "last_seen_at": fleet.now_iso(), "applied": {"rev": st["share"]["list"]["rev"], "at": fleet.now_iso(),
                                                         "twins": {}, "failed": {}}}
        fleet.save(st)
    text = "\n".join(fleet_cli.describe(svc.fleet()))
    assert "mini shares its twins on this computer only." in text
    assert "127.0.0.1" not in text and "port" not in text and "(macos)" not in text  # (they are in --json)
    assert "  MacBook Pro, Mac: in sync" in text
    off = svc.fleet_share(False)
    text = "\n".join(fleet_cli.describe(off))
    assert "Sharing is switched off on mini; linked computers stay linked." in text
    # (as the page says it for a computer that was seen before: when, and no promise that it waits)
    assert "  MacBook Pro, Mac: last seen " in text and "in sync" not in text
    assert "MacBook Pro last seen " in fleet_cli._headline(off) and "in sync" not in fleet_cli._headline(off)


def test_no_brackets_in_the_sentences_and_one_way_to_join_names(sharing, home, tmp_path):  # noqa: F811
    svc = sharing
    with store.locked():
        st = fleet.load()
        for mid, name in (("m-77aa01bc9d10", "Surface"), ("m-0123456789ab", "Orchard"), ("m-aaaaaaaaaaaa", "MacBook Pro")):
            st["share"]["followers"][mid] = {"id": mid, "name": name, "platform": "linux", "key": "0" * 64,
                                             "linked_at": None, "last_seen_at": None, "applied": {}}
        fleet.save(st)
    plan = svc.fleet_remove_machine("m-77aa01bc9d10", True)
    assert plan["goes"] == ["The 1 twin on Surface, when it is next on. It is kept there for 30 days with its chats."]
    assert svc.fleet_copies_line() == ("Its copies on MacBook Pro, Orchard and Surface, when each is next on. "
                                       "Kept there 30 days, with chats.")
    assert svc.fleet_staying_lines() == ["The twins on MacBook Pro, Orchard and Surface; they stop following."]
    for line in plan["goes"] + plan["stays"] + [plan["sentence"], plan["undo"], svc.fleet_copies_line()]:
        assert "(" not in line and "&" not in line, line


def test_unlink_says_what_is_kept_without_brackets(home, tmp_path):  # noqa: F811
    svc = _linked(home, tmp_path)
    _apply(_list([SCOUT, QUILL]))
    plan = svc.fleet_unlink(True)
    assert plan["goes"] == ["The 2 twins on this computer. They are kept for 30 days with their chats."]
    _apply(_list([SCOUT], removed=[{"id": T2, "removed_at": "2000-01-02T09:00:00Z"}], rev="4-0a0b0c0d"))
    assert svc.fleet_unlink(True)["goes"] == ["The 1 twin on this computer. It is kept for 30 days with its chats."]


def test_the_fleet_note_says_source_nowhere(home):  # noqa: F811
    note = fp.fleet_note("Orchard", "Test Scout", ["mini", "Surface"], "mini", False)
    assert note.splitlines() == [
        fp.NOTE_HEADER, "You are the Orchard copy of Test Scout, the Hermes twin of a Grok bot.",
        "Copies of this twin also run on: mini, Surface.",
        "The computer with the Grok app is mini; your name and description follow it."]
    own = fp.fleet_note("mini", "Test Scout", [], "mini", True)
    assert own.splitlines()[-1] == "This computer, mini, has the Grok app; the copies follow it."
    assert "source" not in note and "source" not in own and len(note.split()) <= 46


def test_which_computer_to_update_and_how(home):  # noqa: F811
    here = fleet.message("update_here", name="mini")
    there = fleet.message("update_there", name="mini")
    assert here["hint"].startswith("Update Grok Bot Twin on this computer: Capabilities → Plugins → Update.")
    assert there["hint"].startswith("Update Grok Bot Twin on mini: Capabilities → Plugins → Update.")
    for m in (here, there, fleet.message("version_mismatch")):
        assert "(" not in m["hint"] and "(" not in m["message"]
