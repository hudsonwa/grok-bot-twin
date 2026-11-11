"""Twins on several machines when things go wrong part-way: a follower killed in the middle of a sync, a copy of
the plugin cut short, a kept Hermes bot half moved back, the sharing computer set up again, a wrong clock.

Each test stands for one such fault between two machines (throwaway folders,
invented twins). Real processes and real sockets on loopback, as in test_fleet_machines.py; "killed" means the
process ends at once with nothing cleaned up, as after `kill -9`, a helper restart or a power cut.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from fleet_testlib import TOOLS, Machine, free_port, wait_until, wire_link, wire_sync
from twin_testlib import config, home, service  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import fleet, fleet_follow as follow, fleet_profiles as fp

NAMES = ["Test Scout", "Test Quill", "Test Ranger", "Test Baker", "Test Pilot", "Test Diver"]
SLUGS = sorted(n.lower().replace(" ", "-") for n in NAMES)

# The follower as a process that dies at a chosen point: the Nth call of one function of the plugin's own code.
KILLED = r"""
import os, shutil, sys
sys.path.insert(0, sys.argv[1])
import fleet as tool
tool.load_plugin()
from hermes_plugins.grok_bot_twin.twin import fleet_profiles as fp
where, nth = sys.argv[2], int(sys.argv[3])
owner, name = (shutil, "copytree") if where == "copytree" else (fp, where)
real, calls = getattr(owner, name), [0]
def cut(*a, **kw):
    calls[0] += 1
    if calls[0] == nth:
        os._exit(9)
    return real(*a, **kw)
setattr(owner, name, cut)
sys.exit(tool.main(sys.argv[4:]))
"""


@pytest.fixture
def source(tmp_path):
    m = Machine(tmp_path, "mini", free_port())
    for name in NAMES:
        m.fx("add", name, f"{name} helps.")
    yield m
    m.stop()


@pytest.fixture
def surface(home, source, tmp_path, monkeypatch):  # noqa: F811
    """The test process as the machine that follows."""
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "share_port": source.port,
                         "machine_name": "Surface"})
    monkeypatch.setattr(follow, "MIN_BACKOFF_S", 0.3)
    monkeypatch.setattr(follow, "MAX_BACKOFF_S", 1.0)
    return service.get_service()


def _killed_link(follower: Machine, code: str, where: str, nth: int) -> int:
    argv = [sys.executable, "-c", KILLED, str(TOOLS), where, str(nth), "--home", str(follower.home),
            "--port", str(follower.port), "--name", follower.name, "link", code]
    return subprocess.run(argv, capture_output=True, text=True, timeout=60).returncode


def _profiles(m: Machine) -> list:
    root = m.home / "profiles"
    return sorted(p.name for p in root.iterdir() if not p.name.startswith(".")) if root.is_dir() else []


@pytest.mark.parametrize("where, nth", [
    ("place_plugin", 3),   # two bots whole, the third with its prompt file and no plugin copy yet
    ("write_soul", 2),     # the second has only its settings file: no prompt file says whose it is
    ("copytree", 5),       # in the middle of copying the plugin into a bot
    ("write_meta", 6),     # the last one, between two of its files
])
def test_a_follower_killed_while_making_its_twins_finishes_them_and_makes_no_second_set(source, tmp_path, where, nth):
    code = source.share()
    orchard = Machine(tmp_path, "Orchard", source.port, address=None)
    assert _killed_link(orchard, code, where, nth) == 9
    # cut short: the link is saved, every twin is noted as "being made", and some folders are there, some not
    st = orchard.state()
    assert st["link"]["rev"] is None and len(st["link"]["twins"]) == 6
    assert all(e.get("making") is True for e in st["link"]["twins"].values())
    half = _profiles(orchard)
    assert 0 < len(half) < 6 or where == "write_meta"
    # until the work is finished the page shows no twin that is not really there, and reports none as made
    f = orchard.json("fleet")
    assert f["role"] == "follower" and f["twins"] == []
    # the next sync (the helper started again) finishes the same folders: six twins, six Hermes bots, no "-2"
    orchard.run("fleet", "once")
    assert _profiles(orchard) == SLUGS
    st = orchard.state()
    held = st["link"]["twins"]
    assert sorted(e["profile"] for e in held.values()) == SLUGS
    assert not any("making" in e for e in held.values()) and all(e["state"] == "in_sync" for e in held.values())
    stamp = (orchard.profile("test-scout") / "plugins" / "grok-bot-twin" / fp.COPY_MARKER).read_text().strip()
    assert stamp and stamp != "unfinished"
    for slug, name in zip(SLUGS, sorted(NAMES)):
        prof = orchard.profile(slug)
        soul = orchard.soul(slug)
        assert f"# {name}\n" in soul and f"{name} helps." in soul  # (name and description; their layout is not this test's)
        assert soul.count(fp.TWIN_BEGIN) == 1 and soul.count(fp.FLEET_BEGIN) == 1
        assert "You are the Orchard copy of " + name in soul
        assert "grok-bot-twin" in (prof / "config.yaml").read_text(encoding="utf-8")
        copy = prof / "plugins" / "grok-bot-twin"
        assert (copy / fp.COPY_MARKER).read_text().strip() == stamp  # whole, and the same in every bot
        assert (copy / "plugin.yaml").is_file() and (copy / "twin" / "fleet.py").is_file()
        assert (copy / "desktop" / "plugin.js").is_file() and (copy / "dashboard").is_dir()
        assert not [p.name for p in prof.iterdir() if p.name.endswith(".tmp")]  # no leftover of a cut-short write
    f = orchard.json("fleet")
    assert f["source"]["state"] == "in_sync" and len(f["twins"]) == 6
    # and the machine that shares hears that all six are there
    orchard.run("fleet", "once", check=False)
    sf = source.json("fleet")
    row = next(m for m in sf["machines"] if m["name"] == "Orchard")
    assert (row["state"], row["twins"], row["failed"]) == ("in_sync", 6, 0)
    # applying the same list again changes nothing
    before = {p: (orchard.profile(p) / "SOUL.md").stat().st_mtime_ns for p in SLUGS}
    orchard.run("fleet", "once", check=False)
    assert before == {p: (orchard.profile(p) / "SOUL.md").stat().st_mtime_ns for p in SLUGS}


def test_a_name_noted_for_a_twin_that_the_owner_took_meanwhile_is_left_alone(source, surface, home):  # noqa: F811
    """Cut short before the folder was made; then the owner made a Hermes bot of exactly that name."""
    surface.fleet_link(source.share())
    mine = home / "profiles" / "test-abbot"
    mine.mkdir()
    (mine / "SOUL.md").write_text("My own bot. Not a twin.\n", encoding="utf-8")
    (mine / "config.yaml").write_text("model:\n  default: mine\n", encoding="utf-8")
    tid = "t-00000000000000aa"
    with fleet.locked():
        st = fleet.load()
        held = st["link"]["twins"]
        held[tid] = {"name": "Test Abbot", "description": "", "version": 1, "updated_at": None,
                     "profile": "test-abbot", "synced_at": None, "state": "in_sync", "error": None, "making": True}
        lst = {"rev": "99-0000abcd", "source": st["link"]["source"], "fleet": st["link"]["fleet"], "removed": [],
               "twins": [{"id": t, "name": e["name"], "description": e["description"], "version": e["version"],
                          "updated_at": e["updated_at"]} for t, e in held.items()]}
        saved = []
        counts = fleet.apply_list(st, lst, saved.append)
    assert counts["failed"] == 0 and counts["added"] == 1
    assert held[tid]["profile"] == "test-abbot-2" and "making" not in held[tid] and held[tid]["state"] == "in_sync"
    assert "# Test Abbot\n" in (home / "profiles" / "test-abbot-2" / "SOUL.md").read_text(encoding="utf-8")
    assert (mine / "SOUL.md").read_text(encoding="utf-8") == "My own bot. Not a twin.\n"
    assert (mine / "config.yaml").read_text(encoding="utf-8") == "model:\n  default: mine\n"
    assert sorted(p.name for p in mine.iterdir()) == ["SOUL.md", "config.yaml"]
    assert len(saved) == 1  # the other name was noted (and saved) before its folder was made
    # the same with a folder of that name that is half made but shared by two notes: neither takes it
    with fleet.locked():
        st = fleet.load()
        held = st["link"]["twins"]
        (home / "profiles" / "test-baron").mkdir()
        for t in ("t-00000000000000ba", "t-00000000000000bb"):
            held[t] = {"name": "Test Baron", "description": "", "version": 1, "updated_at": None,
                       "profile": "test-baron", "synced_at": None, "state": "in_sync", "error": None, "making": True}
        lst = {"rev": "99-0000abce", "source": st["link"]["source"], "fleet": st["link"]["fleet"], "removed": [],
               "twins": [{"id": t, "name": e["name"], "description": e["description"], "version": e["version"],
                          "updated_at": e["updated_at"]} for t, e in held.items()]}
        fleet.apply_list(st, lst)
    assert sorted(held[t]["profile"] for t in ("t-00000000000000ba", "t-00000000000000bb")) == ["test-baron", "test-baron-2"]


def test_unlinking_takes_a_half_made_folder_along_and_never_an_owners_bot(source, surface, home):  # noqa: F811
    surface.fleet_link(source.share())
    half = home / "profiles" / "test-abbot"
    half.mkdir()
    (half / "config.yaml").write_text("plugins:\n  enabled:\n  - grok-bot-twin\n", encoding="utf-8")
    (half / ".SOUL.md.ab12cd_3.tmp").write_text("# Test Ab", encoding="utf-8")  # a write that never finished
    mine = home / "profiles" / "test-baron"
    mine.mkdir()
    (mine / "config.yaml").write_text("model:\n  default: mine\n", encoding="utf-8")
    (mine / "notes.txt").write_text("the owner's", encoding="utf-8")
    with fleet.locked():
        st = fleet.load()
        for tid, prof in (("t-00000000000000aa", "test-abbot"), ("t-00000000000000ab", "test-baron")):
            st["link"]["twins"][tid] = {"name": prof, "description": "", "version": 1, "updated_at": None,
                                        "profile": prof, "synced_at": None, "state": "in_sync", "error": None,
                                        "making": True}
        fleet.save(st)
    assert fp.half_made(half) is True and fp.half_made(mine) is False
    surface.fleet_unlink(False)
    assert not half.exists()  # this machine's own unfinished work: gone with the link
    assert sorted(p.name for p in mine.iterdir()) == ["config.yaml", "notes.txt"]  # not this machine's: untouched
    assert (mine / "notes.txt").read_text(encoding="utf-8") == "the owner's"
    assert sorted(p.name for p in (home / "profiles").iterdir() if not p.name.startswith(".")) == ["test-baron"]


def test_a_new_twin_never_takes_the_name_a_kept_bot_comes_back_to(source, surface, home):  # noqa: F811
    surface.fleet_link(source.share())
    (home / "profiles" / "test-scout" / "memo.txt").write_text("kept with the bot", encoding="utf-8")
    surface.fleet_unlink(False)
    source.fx("add", "test scout", "A second bot whose folder name would be the same.")
    f = surface.fleet_link(source.code())
    by_name = {t["name"]: t["profile"] for t in f["twins"]}
    assert by_name["Test Scout"] == "test-scout" and by_name["test scout"] == "test-scout-2"
    assert len(set(by_name.values())) == 7
    assert (home / "profiles" / "test-scout" / "memo.txt").read_text(encoding="utf-8") == "kept with the bot"
    assert "# Test Scout\n" in (home / "profiles" / "test-scout" / "SOUL.md").read_text(encoding="utf-8")
    assert "# test scout\n" in (home / "profiles" / "test-scout-2" / "SOUL.md").read_text(encoding="utf-8")


def test_a_kept_bot_that_was_half_way_back_when_the_process_died_is_the_same_twin_again(source, surface, home):  # noqa: F811
    surface.fleet_link(source.share())
    (home / "profiles" / "test-scout" / "memo.txt").write_text("kept with the bot", encoding="utf-8")
    surface.fleet_unlink(False)
    st = fleet.load()
    rec = next(k for k in st["kept"] if k["profile"] == "test-scout")
    kept = home / "plugin-data" / "grok-bot-twin" / fleet.KEPT_DIR / rec["rid"]
    # the move back happened, the note of it did not (the process died between the two)
    os.replace(kept / "profile", home / "profiles" / "test-scout")
    f = surface.fleet_link(source.code())
    assert sorted(t["profile"] for t in f["twins"]) == SLUGS  # no test-scout-2
    assert (home / "profiles" / "test-scout" / "memo.txt").read_text(encoding="utf-8") == "kept with the bot"
    assert f["removed"] == [] and fleet.load()["kept"] == []
    assert fp.plugin_enabled(home / "profiles" / "test-scout") is True


def test_a_copy_of_the_plugin_that_was_cut_short_is_made_whole_next_time(tmp_path):
    src = fp.plugin_source()
    assert src is not None
    bot = tmp_path / "bot"
    dst = bot / "plugins" / "grok-bot-twin"
    (dst / "twin").mkdir(parents=True)
    (dst / fp.COPY_MARKER).write_text("unfinished\n", encoding="utf-8")
    (dst / "twin" / "fleet.py").write_text("# half a file", encoding="utf-8")
    assert fp.place_plugin(bot) is True
    assert (dst / fp.COPY_MARKER).read_text().strip() == fp._source_stamp(src)
    assert (dst / "plugin.yaml").is_file() and (dst / "twin" / "fleet.py").read_text(encoding="utf-8") != "# half a file"
    # while a copy is being made it already carries the mark, so a death at any point leaves it recognisable
    seen = []
    real = shutil.copytree

    def look(s, d, *a, **kw):  # (a folder inside a part, like skills/NAME, reaches this again from the real one)
        seen.append((bot2 / "plugins" / "grok-bot-twin" / fp.COPY_MARKER).read_text().strip())
        return real(s, d, *a, **kw)

    bot2 = tmp_path / "bot2"
    shutil.copytree = look
    try:
        assert fp.place_plugin(bot2) is True
    finally:
        shutil.copytree = real
    assert seen and set(seen) == {"unfinished"}
    # an install of the owner's own (no mark at all) is still never touched
    own = tmp_path / "bot3" / "plugins" / "grok-bot-twin"
    own.mkdir(parents=True)
    (own / "plugin.yaml").write_text("name: theirs\n", encoding="utf-8")
    assert fp.place_plugin(tmp_path / "bot3") is True
    assert sorted(p.name for p in own.iterdir()) == ["plugin.yaml"]


def _source_state(svc):
    return svc.fleet()["source"]


def test_a_sharing_computer_that_was_set_up_again_is_said_so_and_linking_again_works(source, surface, home, tmp_path):  # noqa: F811
    """Grok Bot Twin removed and installed anew on the Mac: it has a new identity and knows no linked computer.
    Before, the other computer said "has not answered ... catch up by themselves when it is back" for ever."""
    surface.fleet_link(source.share())
    souls = {p: (home / "profiles" / p / "SOUL.md").read_bytes() for p in SLUGS}
    old_id = source.state()["machine"]["id"]
    source.stop()
    aside = tmp_path / "old-data"
    os.replace(source.data, aside)  # (the plugin's whole data folder goes, as "Remove Grok Bot Twin" does)
    for name in NAMES[:2]:
        source.fx("add", name, f"{name} helps.")
    source.share()
    assert source.state()["machine"]["id"] != old_id and source.state()["share"]["followers"] == {}
    # one or two such answers could be anything at that address: still "not answering"; then it is said plainly
    s = wait_until(lambda: (lambda x: x if x["state"] == "error" else None)(_source_state(surface)), timeout=30,
                   what="the other computer to say it is no longer known")
    assert s["problem"]["code"] == "source_forgot" and s["offline_since"] is None
    # (said as a condition: the answers behind it are unsigned, so they prove nothing about who sent them)
    assert s["problem"]["message"] == "mini no longer answers as a computer linked to this one."
    assert s["problem"]["hint"].startswith("If Grok Bot Twin was set up again on mini, unlink this computer and link "
                                           "it again with a new link code.")
    # nothing was believed and nothing taken away: the twins are as they were, to the byte
    f = surface.fleet()
    assert len(f["twins"]) == 6 and all(t["state"] == "in_sync" for t in f["twins"])
    assert souls == {p: (home / "profiles" / p / "SOUL.md").read_bytes() for p in SLUGS}
    whats = [e["payload"].get("what") for e in surface.events()["events"] if e["type"] == "fleet"]
    assert "source_back" not in whats[whats.index("source_offline"):]  # "who are you?" is not a return
    # the terminal says the same
    from hermes_plugins.grok_bot_twin.twin import fleet_cli
    lines = fleet_cli.describe(f)
    assert lines[0] == "Surface follows mini: no longer answers as linked." and "link it again with a new link code" in lines[1]
    # the way out that the sentence names works: unlink here, link again with a new code
    surface.fleet_unlink(False)
    f = surface.fleet_link(source.code())
    assert f["source"]["state"] == "in_sync" and f["source"]["problem"] is None
    assert sorted(t["profile"] for t in f["twins"]) == ["test-quill", "test-scout"]
    assert len(f["removed"]) == 6  # the bots from before (and their chats) are still kept for 30 days


def test_a_stranger_at_the_address_changes_nothing_and_the_real_one_coming_back_heals_it(source, surface, home, tmp_path):  # noqa: F811
    surface.fleet_link(source.share())
    source.stop()
    real = tmp_path / "real-data"
    os.replace(source.data, real)
    source.fx("add", "Test Stranger", "Another install at the same address.")
    source.share()
    wait_until(lambda: _source_state(surface)["state"] == "error", timeout=30, what="the stranger to be noticed")
    assert sorted(t["name"] for t in surface.fleet()["twins"]) == sorted(NAMES)  # nothing of the stranger's came over
    assert not (home / "profiles" / "test-stranger").exists()
    # the real one is back at the address: its signed answer puts everything right, with no click
    source.stop()
    os.replace(source.data, tmp_path / "stranger-data")
    os.replace(real, source.data)
    source.start()
    s = wait_until(lambda: (lambda x: x if x["state"] == "in_sync" else None)(_source_state(surface)), timeout=30,
                   what="the link to heal")
    assert s["problem"] is None and fleet.load()["link"].get("why") is None
    assert sorted(t["name"] for t in surface.fleet()["twins"]) == sorted(NAMES)


@pytest.mark.parametrize("their_clock", ["2099-01-01T00:00:00Z", "2001-01-01T00:00:00Z"])
def test_the_time_a_linked_computer_was_last_in_sync_is_by_this_computers_clock(source, their_clock):
    """A linked computer whose clock is years (or hours) off: what the Mac shows about it must not come from that
    clock ("last in sync just now" for a machine gone for hours, or "25 years ago" for one seen a minute ago)."""
    from datetime import datetime, timezone
    code = source.share()
    status, linked, _ = wire_link(source.port, code, "m-cccccccccccc", "Orchard")
    status, lst, _ = wire_sync(source.port, linked["pass"])
    report = {"rev": lst["rev"], "at": their_clock, "twins": {t["id"]: t["version"] for t in lst["twins"]}, "failed": {}}
    assert wire_sync(source.port, linked["pass"], have=lst["rev"], applied=report)[0] == 200
    source.fx("add", "Test Later", "Added after that computer last synced.")  # it is now behind
    row = wait_until(lambda: (lambda m: m if m["state"] == "behind" else None)(source.json("fleet")["machines"][1]))
    when = datetime.fromisoformat(row["last_sync_at"].replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - when).total_seconds()) < 120 and row["last_sync_at"] != their_clock
    assert row["last_sync_at"] == source.state()["share"]["followers"]["m-cccccccccccc"]["in_sync_at"]


def _tiny_plugin(root, body: str):
    (root / "twin").mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text("name: grok-bot-twin\n", encoding="utf-8")
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "twin" / "code.py").write_text(body, encoding="utf-8")
    return root


def test_a_plugin_update_under_a_running_process_does_not_set_two_processes_replacing_every_copy_in_turn(tmp_path):
    """The plugin is updated while one process keeps running. That process remembered the stamp of the
    old version, a newer process worked out the new one, and from then on each replaced every twin's copy of the
    plugin on every change, writing its own stamp, so the other replaced them again."""
    import time
    src = _tiny_plugin(tmp_path / "install", "VERSION = 1\n")
    bot = tmp_path / "bot"
    copy = bot / "plugins" / "grok-bot-twin"
    assert fp.place_plugin(bot, src) is True
    old = (copy / fp.COPY_MARKER).read_text().strip()
    assert old == fp._source_stamp(src)
    # the update, while this process still remembers the old stamp
    (src / "twin" / "code.py").write_text("VERSION = 2\n", encoding="utf-8")
    remembered = dict(fp._stamps)
    new = fp._source_stamp(src, max_age=0)
    assert new != old
    # a newer process brings the copy up to date ...
    fp._stamps.clear()
    assert fp.place_plugin(bot, src) is True
    assert (copy / fp.COPY_MARKER).read_text().strip() == new and "VERSION = 2" in (copy / "twin" / "code.py").read_text()
    before = (copy / "twin" / "code.py").stat().st_ino, (copy / fp.COPY_MARKER).stat().st_mtime_ns
    # ... and the process that was already running (it remembers the old stamp) leaves that copy alone
    fp._stamps.clear()
    fp._stamps.update(remembered)
    assert fp.place_plugin(bot, src) is True
    assert ((copy / "twin" / "code.py").stat().st_ino, (copy / fp.COPY_MARKER).stat().st_mtime_ns) == before
    assert (copy / fp.COPY_MARKER).read_text().strip() == new
    # a copy the old process makes itself is stamped with what it really copied, not with what it remembers
    fp._stamps.clear()
    fp._stamps.update({k: (v[0], time.monotonic() - 10 * fp.STAMP_AGE_S) for k, v in remembered.items()})
    bot2 = tmp_path / "bot2"
    assert fp.place_plugin(bot2, src) is True
    assert (bot2 / "plugins" / "grok-bot-twin" / fp.COPY_MARKER).read_text().strip() == new
    assert "VERSION = 2" in (bot2 / "plugins" / "grok-bot-twin" / "twin" / "code.py").read_text()
    # and a copy of a really older version is still replaced, once
    (src / "twin" / "code.py").write_text("VERSION = 3\n", encoding="utf-8")
    fp._stamps.clear()
    assert fp.place_plugin(bot, src) is True and "VERSION = 3" in (copy / "twin" / "code.py").read_text()


RACER = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
os.environ["HERMES_HOME"] = sys.argv[2]
import fleet as tool
tool.load_plugin()
from pathlib import Path
from hermes_plugins.grok_bot_twin.twin import fleet_profiles as fp
while time.time() < float(sys.argv[4]):
    time.sleep(0.001)
ok = all(fp.place_plugin(Path(b)) for b in sys.argv[5:])
sys.exit(0 if ok else 3)
"""


def test_two_processes_bringing_the_same_copies_up_to_date_at_once_leave_every_one_whole(tmp_path):
    """After a plugin update the sharing process and a terminal command both refresh every twin's copy. Seen
    live: one copy was left marked unfinished; an empty one under a full stamp was possible too."""
    import time
    src = fp.plugin_source()
    hermes = tmp_path / "hermes-home"
    bots = [hermes / "profiles" / f"bot-{i}" for i in range(4)]
    for round_ in range(3):
        for bot in bots:  # every bot holds a copy of an older version
            old = bot / "plugins" / "grok-bot-twin"
            (old / "twin").mkdir(parents=True, exist_ok=True)
            (old / fp.COPY_MARKER).write_text(f"0ld{round_:013d}\n", encoding="utf-8")
            (old / "twin" / "stale.py").write_text("# from the version before", encoding="utf-8")
        go = str(time.time() + 1.5)
        procs = [subprocess.Popen([sys.executable, "-c", RACER, str(TOOLS), str(hermes), "x", go, *map(str, bots)])
                 for _ in range(3)]
        assert [p.wait(120) for p in procs] == [0, 0, 0]
        want = sorted(str(f.relative_to(src)) for f in fp._source_files(src))
        for bot in bots:
            copy = bot / "plugins" / "grok-bot-twin"
            assert (copy / fp.COPY_MARKER).read_text().strip() == fp._source_stamp(src)
            have = sorted(str(f.relative_to(copy)) for f in copy.rglob("*") if f.is_file() and f.name != fp.COPY_MARKER)
            assert have == want  # whole, and nothing of the old version left


def test_waking_up_with_a_request_open_is_not_taken_for_the_other_computer_being_gone(source, surface, monkeypatch):
    """A follower held for longer than a request lasts (as asleep) said "offline since now" and "back" within the
    same second on waking. The request did not fail because the Mac was gone but because this machine slept
    through it."""
    surface.fleet_link(source.share())
    follow._reset_for_tests()  # the test asks by hand from here on
    wait_until(lambda: surface.fleet()["source"]["state"] == "in_sync")
    before = len(surface.events()["events"])
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(follow, "_wall", lambda: clock["now"])

    def asleep(*a, **kw):
        clock["now"] += 50 + 15 + 3600  # an hour passed on the wall while the request was open
        raise follow.Unreachable()

    monkeypatch.setattr(follow, "_signed_request", asleep)
    assert follow.sync_once(surface, wait_s=50) == "slept"
    s = surface.fleet()["source"]
    assert s["state"] == "in_sync" and s["offline_since"] is None  # nothing noted, nothing shown
    assert [e["payload"].get("what") for e in surface.events()["events"][before:] if e["type"] == "fleet"] == []

    def gone(*a, **kw):
        clock["now"] += 6  # an ordinary failed try
        raise follow.Unreachable()

    monkeypatch.setattr(follow, "_signed_request", gone)
    assert follow.sync_once(surface, wait_s=0) == "offline"
    assert surface.fleet()["source"]["state"] == "offline"


def test_twin_ids_do_not_change_at_the_next_start_after_the_plugin_was_removed_and_set_up_again(home):  # noqa: F811
    """The fault: "Remove Grok Bot Twin" on the sharing computer, set up again, shared again, all under one
    running process. At that process's next start every twin had another id, and every linked computer took all
    its twins away and made them anew."""
    import hashlib
    import hmac
    from hermes_plugins.grok_bot_twin.twin import model, store
    svc = service.get_service()
    first = model.pair_ref("test-scout")
    salt_file = home / "plugin-data" / "grok-bot-twin" / store.SALT_FILE
    assert salt_file.is_file()
    svc.uninstall(False)  # the plugin's data goes; this process keeps running
    assert not salt_file.exists()
    again = model.pair_ref("test-scout")  # twinned and shared again, same process
    assert salt_file.is_file() and again != first  # a new install has new ids, and they are on disk at once
    assert model.pair_ref("test-scout") == again
    store._salts.clear()  # Hermes starts again
    assert model.pair_ref("test-scout") == again  # the id the other computers were given still holds
    # two processes after a removal: the one that did not make the new salt follows the file, not its memory
    other = "5a" * 32
    tmp = salt_file.with_name("new-salt")
    tmp.write_text(other + "\n", encoding="ascii")
    os.chmod(tmp, 0o600)
    os.replace(tmp, salt_file)
    want = "t-" + hmac.new(bytes.fromhex(other), b"test-scout", hashlib.sha256).hexdigest()[:16]
    assert model.pair_ref("test-scout") == want
