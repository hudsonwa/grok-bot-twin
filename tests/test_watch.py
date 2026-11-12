"""The fast check: a name or description changed in Grok wakes the background check right away.

What is proved here: the watcher looks at file names, dates and sizes only (it works on files nobody may read),
it reacts to the bot-list file and to nothing else in the folder, it is paced, it watches nothing without a real
twin or when switched off, and it ends with the plugin. With BOTPORT_TEST_BIN set, a rename in a fixture Grok
folder reaches the Hermes twin through the real helper with no manual sync, and the time it took is printed.

Invented bots only; every folder is under tmp_path.
"""
from __future__ import annotations

import base64
import json
import os
import select
import threading
import time
from pathlib import Path

import pytest

from twin_testlib import central, config, home, plugin, service, svc, timer  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import watch
from hermes_plugins.grok_bot_twin.twin.engine import EngineError
from test_ux_flows import fake  # noqa: F401
from test_service_real_botport import BIN, rig  # noqa: F401

REAL_ENSURE_STARTED = timer.ensure_started  # other fixtures replace it; these tests start the real threads
HAVE_KQUEUE = hasattr(select, "kqueue")


def blob_name(key: str) -> str:
    return base64.b32encode(key.encode()).decode().rstrip("=").lower() + ".blob"


ROSTER = blob_name("sand.client.slice.account.demo-account.roster.last-roster")
REPLICA = blob_name("sand.client.slice.account.demo-account.transcript.replicas.b-acme-0001")


def write_roster(folder: Path, name: str = "Acme Support", in_place: bool = True) -> None:
    data = json.dumps({"schemaVersion": 1, "value": [{"id": "b-acme-0001", "name": name}]})
    target = folder / ROSTER
    if in_place:
        target.write_text(data)
    else:  # the way a careful app saves: a new file moved over the old one
        tmp = folder / "roster.tmp"
        tmp.write_text(data)
        os.replace(tmp, target)


@pytest.fixture
def cache(tmp_path):
    folder = tmp_path / "grok-app" / watch.CACHE_SUBDIR
    folder.mkdir(parents=True)
    write_roster(folder)
    return folder


class Run:
    """A watcher on its own thread, with the wake-ups it passed on."""

    def __init__(self, folder_fn, force_poll=False):
        self.fired: list[float] = []
        self.w = watch.Watcher(folder_fn, lambda: self.fired.append(time.monotonic()), force_poll=force_poll)
        self.t = threading.Thread(target=self.w.run, daemon=True)
        self.t.start()

    def wait_scans(self, n, timeout=5.0):
        end = time.monotonic() + timeout
        while self.w.scans < n and time.monotonic() < end:
            time.sleep(0.02)
        assert self.w.scans >= n

    def wait_fires(self, n, timeout=5.0):
        end = time.monotonic() + timeout
        while len(self.fired) < n and time.monotonic() < end:
            time.sleep(0.02)
        return len(self.fired) >= n

    def close(self):
        self.w.stop()
        self.t.join(3)
        assert not self.t.is_alive()


@pytest.fixture
def run_watch():
    made = []

    def start(folder_fn, force_poll=False):
        r = Run(folder_fn, force_poll)
        made.append(r)
        return r
    yield start
    for r in made:
        r.close()


# ---- names, dates and sizes only --------------------------------------------------------------------------------

def test_only_the_bot_list_file_counts():
    assert watch.is_roster_name(ROSTER)
    assert watch.is_roster_name(blob_name("sand.client.slice.account.second-login.roster.last-roster"))
    for name in (REPLICA, "sand-secrets.json", "Cookies", ROSTER[:-5], ROSTER + ".tmp", "..blob", "a" * 500 + ".blob",
                 blob_name("other.app.roster.last-roster"), blob_name("sand.client.slice.account.x.roster.other"),
                 "not base32!.blob", "ñ.blob", ""):
        assert not watch.is_roster_name(name), name
    assert not watch.is_roster_name(None)


def test_signature_is_names_dates_and_sizes(cache, tmp_path):
    (cache / REPLICA).write_text("{}")
    (cache / "sand-secrets.json").write_text("{}")
    sig = watch.roster_signature(cache)
    st = (cache / ROSTER).stat()
    assert sig == ((ROSTER, st.st_mtime_ns, st.st_size, st.st_ino),)
    (cache / REPLICA).write_text('{"more": 1}')  # a chat in Grok: not the bot list
    assert watch.roster_signature(cache) == sig
    write_roster(cache, "Acme Support Desk")
    assert watch.roster_signature(cache) != sig
    assert watch.roster_signature(tmp_path / "missing") is None
    assert watch.roster_signature(cache / ROSTER) is None  # a file, not a folder


def test_a_linked_bot_list_is_not_followed(cache, tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}")
    (cache / ROSTER).unlink()
    (cache / ROSTER).symlink_to(elsewhere)
    assert watch.roster_signature(cache) == ()


def test_no_content_is_read(cache, run_watch, monkeypatch):
    """The bot-list file is made unreadable: the signature and the wake-up still work, so nothing reads it."""
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    (cache / ROSTER).chmod(0o000)
    with pytest.raises(PermissionError):
        (cache / ROSTER).read_bytes()
    assert watch.roster_signature(cache)
    monkeypatch.setattr(watch, "POLL_S", 0.1)
    for force_poll in (False, True):
        r = run_watch(lambda: cache, force_poll)
        r.wait_scans(1)
        os.utime(cache / ROSTER, ns=(1, time.time_ns()))  # a new date is all a watcher may notice here
        (cache / REPLICA).write_text("x")
        os.replace(cache / REPLICA, cache / "moved.blob")  # stir the folder so the event watcher looks again
        assert r.wait_fires(1), f"force_poll={force_poll}"
        r.close()
    import inspect
    src = inspect.getsource(watch)
    assert "open(" not in src.replace("os.open(", "").replace("def _open(", "").replace("self._open(", "")
    assert "os.read(self._r" in src and src.count("os.read(") == 1  # the only read is the wake-up pipe


# ---- pacing ------------------------------------------------------------------------------------------------------

def test_pace_first_at_once_then_spread_out():
    p = watch.Pace()
    assert p.due(100.0) == 100.0
    p.fired(100.0, held=False)
    assert p.due(100.5) == 102.0           # a second change half a second later waits for the gap
    p.fired(102.0, held=True)
    assert p.gap == 4.0 and p.due(102.1) == 106.0
    for _ in range(10):
        p.fired(p.due(p.last + 0.1), held=True)
    assert p.gap == watch.MAX_GAP          # constant rewriting: one check every 30 s at most
    quiet = p.last + 120.0
    assert p.due(quiet) == quiet           # after a quiet spell a change goes through at once again
    p.fired(quiet, held=False)
    assert p.gap == watch.MIN_GAP


# ---- the watcher itself ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("force_poll", [False, True])
def test_a_change_of_the_bot_list_wakes_the_check(cache, run_watch, monkeypatch, force_poll):
    monkeypatch.setattr(watch, "POLL_S", 0.1)
    r = run_watch(lambda: cache, force_poll)
    assert r.w.mode == ("poll" if force_poll or not HAVE_KQUEUE else "events")
    r.wait_scans(1)
    assert r.fired == [] and r.w.watching
    t0 = time.monotonic()
    write_roster(cache, "Acme Support Desk")
    assert r.wait_fires(1, 3.0)
    assert r.fired[0] - t0 < 1.5, "the first change is passed on at once"
    # a second change right away is held for the gap, then passed on (saved as a new file moved over the old one)
    write_roster(cache, "Acme Front Desk", in_place=False)
    assert r.wait_fires(2, watch.MIN_GAP + 3.0)
    assert r.fired[1] - r.fired[0] >= watch.MIN_GAP - 0.05


@pytest.mark.parametrize("force_poll", [False, True])
def test_other_files_do_not_wake_the_check(cache, run_watch, monkeypatch, force_poll):
    monkeypatch.setattr(watch, "POLL_S", 0.1)
    r = run_watch(lambda: cache, force_poll)
    r.wait_scans(1)
    for i in range(5):  # chats, settings and whatever else the app saves next to the bot list
        (cache / REPLICA).write_text(f"{{\"n\": {i}}}")
        (cache / "sand-secrets.json").write_text(str(i))
        time.sleep(0.05)
    time.sleep(0.8)
    assert r.fired == []
    write_roster(cache, "Acme Support Desk")
    assert r.wait_fires(1, 3.0)


@pytest.mark.skipif(not HAVE_KQUEUE, reason="kernel file events are a macOS/BSD feature")
def test_waiting_costs_nothing_while_nothing_changes(cache, run_watch):
    r = run_watch(lambda: cache)
    r.wait_scans(1)
    time.sleep(0.3)
    scans, cpu = r.w.scans, time.thread_time()
    t = time.process_time()
    time.sleep(1.5)
    assert r.w.scans == scans, "no timer runs while the folder is quiet: the thread sleeps until the system calls"
    assert time.process_time() - t < 0.2 and cpu >= 0


def test_nothing_to_watch_then_a_twin_appears(cache, run_watch):
    box = {"folder": None}
    r = run_watch(lambda: box["folder"])
    time.sleep(0.3)
    assert r.w.scans == 0 and not r.w.watching  # no real twin: the folder is not even listed
    write_roster(cache, "Changed while nobody watched")
    box["folder"] = cache
    r.w.poke()
    r.wait_scans(1)
    assert r.w.watching and r.fired == []  # what it finds first is the starting point, not a change
    write_roster(cache, "Acme Support Desk")
    assert r.wait_fires(1, 3.0)
    box["folder"] = None
    r.w.poke()
    time.sleep(0.3)
    scans = r.w.scans
    write_roster(cache, "After the last twin was removed")
    time.sleep(0.6)
    assert r.w.scans == scans and len(r.fired) == 1 and not r.w.watching


def test_a_folder_that_appears_later_is_picked_up(tmp_path, run_watch, monkeypatch):
    monkeypatch.setattr(watch, "IDLE_S", 0.2)
    monkeypatch.setattr(watch, "POLL_S", 0.1)
    folder = tmp_path / "grok-app" / watch.CACHE_SUBDIR
    r = run_watch(lambda: folder)
    time.sleep(0.3)
    assert not r.w.watching
    folder.mkdir(parents=True)
    write_roster(folder)  # the app was installed and signed in: for a twin that waited, that is a change
    assert r.wait_fires(1, 3.0) and r.w.watching


def test_a_missing_folder_is_looked_for_every_few_seconds_not_twice_a_minute(tmp_path, run_watch, monkeypatch):
    # (the page's "Grok is not on this computer any more" stayed 27 s after Grok was back: the look was every 30 s)
    assert watch.GONE_S <= 5.0 < watch.IDLE_S
    monkeypatch.setattr(watch, "GONE_S", 0.1)
    folder = tmp_path / "grok-app" / watch.CACHE_SUBDIR
    r = run_watch(lambda: folder)  # IDLE_S stays 30 s: only the short look can find the folder in time
    time.sleep(0.3)
    assert not r.w.watching
    folder.mkdir(parents=True)
    write_roster(folder)
    assert r.wait_fires(1, 3.0) and r.w.watching


def test_a_failing_callback_never_ends_the_watcher(cache, run_watch, monkeypatch):
    monkeypatch.setattr(watch, "MIN_GAP", 0.1)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("boom")
    w = watch.Watcher(lambda: cache, boom)
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    try:
        for i in range(2):
            end = time.monotonic() + 3
            while w.scans < 1 and time.monotonic() < end:
                time.sleep(0.02)
            write_roster(cache, f"Acme {i}")
            end = time.monotonic() + 3
            while len(calls) < i + 1 and time.monotonic() < end:
                time.sleep(0.02)
            time.sleep(0.3)
        assert len(calls) == 2 and t.is_alive()
    finally:
        w.stop()
        t.join(3)


# ---- the service decides what may be watched -----------------------------------------------------------------------

def test_nothing_is_watched_without_a_real_twin(svc):
    assert svc.watch_folder() is None
    svc.demo_action("start")  # the sample is not a reason to look at the Grok app
    assert svc.watch_folder() is None
    st = svc.status(fresh=True)
    assert st["fast_check"] == {"on": False, "mode": "off"}


def test_watched_with_a_real_twin_and_off_by_setting(fake, home, tmp_path):  # noqa: F811
    app = tmp_path / "grok-app"
    (app / watch.CACHE_SUBDIR).mkdir(parents=True)
    config.set_override({"botport_path": str(fake.exe), "grok_app_dir": str(app)})
    service.reset_service()
    s = service.get_service()
    s.trust_botport(True)
    assert s.watch_folder() is None
    s.create(["b-acme-0001"])
    assert s.watch_folder() == app / watch.CACHE_SUBDIR
    assert s.status(fresh=True)["fast_check"] == {"on": False, "mode": "off"}  # no background thread in this process yet
    config.set_override({"botport_path": str(fake.exe), "grok_app_dir": str(app), "watch_grok_changes": False})
    assert s.watch_folder() is None
    assert "right away" not in timer.describe()
    config.set_override({"botport_path": str(fake.exe), "grok_app_dir": str(app), "watch_grok_changes": "yes"})
    assert s.watch_folder() is not None and "right away" in timer.describe()
    s.remove("acme-support")
    assert s.watch_folder() is None


def test_setting_default_and_parsing(home):
    assert config.DEFAULTS["watch_grok_changes"] is True
    for raw, want in ((None, True), ("off", False), (False, False), ("true", True), ("", True)):
        config.set_override({} if raw is None else {"watch_grok_changes": raw})
        assert config.get("watch_grok_changes") is want, raw


def test_timer_and_watcher_start_and_end_together(fake, home, tmp_path, monkeypatch):  # noqa: F811
    """With a real twin (the fake helper), a change of the bot-list file runs the check within a second or two,
    long before the timed check (60 s) would."""
    app = tmp_path / "grok-app"
    folder = app / watch.CACHE_SUBDIR
    folder.mkdir(parents=True)
    write_roster(folder)
    config.set_override({"botport_path": str(fake.exe), "grok_app_dir": str(app)})
    service.reset_service()
    s = service.get_service()
    s.trust_botport(True)
    s.create(["b-acme-0001"])
    ticks: list[float] = []
    real_tick = s.tick
    monkeypatch.setattr(s, "tick", lambda: ticks.append(time.monotonic()) or real_tick())
    assert REAL_ENSURE_STARTED(lambda: s) is True
    try:
        assert watch.running() and timer.running()
        assert REAL_ENSURE_STARTED(lambda: s) is False  # one of each per process
        end = time.monotonic() + 5
        while (not ticks or not watch._watcher.watching) and time.monotonic() < end:
            time.sleep(0.05)
        assert ticks and watch._watcher.watching, "the first timed check ran and the watch is on"
        assert s.status(fresh=True)["fast_check"] == {"on": True, "mode": watch._watcher.mode}
        time.sleep(0.5)
        n, t0 = len(ticks), time.monotonic()
        write_roster(folder, "Acme Support Desk")
        end = time.monotonic() + 5
        while len(ticks) == n and time.monotonic() < end:
            time.sleep(0.02)
        assert len(ticks) > n, "the change woke the check"
        assert ticks[n] - t0 < 3.0
        calls = [c[:2] for c in fake.calls()]
        assert ["twin", "check"] in calls
    finally:
        t, wt = timer._thread, watch._thread
        plugin._unload()
        t.join(3)
        wt.join(3)
    assert not t.is_alive() and not wt.is_alive()
    names = [th.name for th in threading.enumerate()]
    assert "grok-bot-twin-sync" not in names and "grok-bot-twin-watch" not in names
    assert REAL_ENSURE_STARTED(lambda: s) is False and not watch.running()  # stays stopped after unload


# ---- the same rule as the page: no move while the twin needs its owner ----------------------------------------------

def test_move_to_hermes_is_refused_while_the_twin_needs_attention(svc):
    svc.demo_action("start")
    jobs = [j["job_id"] for j in svc.checklist("acme-support")["switch"]["hermes_jobs_paused"]]
    svc.demo_action("conflict")
    blocked = svc.checklist("acme-support")["blocked_by"]
    assert blocked == 'First choose which version of the skill "greet-visitors" to keep.'
    with pytest.raises(EngineError) as exc:
        svc.switch("acme-support", jobs)
    assert exc.value.code == "blocked" and exc.value.message == blocked
    assert exc.value.hint == "Then choose Move routines to Hermes again."
    assert svc.checklist("acme-support")["switch"]["hermes_jobs_resumed"] == []  # nothing was turned on
    assert svc.switch("acme-support", undo=True) == {"paused": [], "failed": []}  # undo is never blocked


# ---- the real helper: a rename in the Grok folder reaches the twin with no manual sync ------------------------------

@pytest.mark.skipif(not (BIN and os.path.isfile(BIN) and os.access(BIN, os.X_OK)),
                    reason="set BOTPORT_TEST_BIN to a botport build to run this")
def test_a_rename_in_grok_reaches_the_twin_in_seconds(rig, capsys):  # noqa: F811
    s, world, hermes_home = rig["svc"], rig["world"], rig["home"]
    s.trust_botport(True)
    s.create(["b-acme-0001"])
    s.check_changed()
    s.tick()
    s.tick()  # settled: the next timed check would find nothing
    assert REAL_ENSURE_STARTED(lambda: s) is True
    try:
        end = time.monotonic() + 10
        while not (watch._watcher and watch._watcher.watching and timer._state["ticks"] >= 1) and time.monotonic() < end:
            time.sleep(0.05)
        assert watch._watcher.watching
        time.sleep(1.0)
        soul = hermes_home / "profiles" / "acme-helper" / "SOUL.md"
        assert "spare parts" not in soul.read_text()
        t0 = time.monotonic()
        world.step("rename")  # what the Grok app does when the owner renames the bot: it rewrites its bot list
        t_cmd = time.monotonic() - t0
        end = time.monotonic() + 15
        while "spare parts" not in soul.read_text() and time.monotonic() < end:
            time.sleep(0.02)
        took = time.monotonic() - t0
        assert "spare parts" in soul.read_text(), "the new description reached the Hermes twin by itself"
        assert took < 10.0, f"took {took:.2f} s"
        end = time.monotonic() + 10
        name = None
        while name != "Acme Support Desk" and time.monotonic() < end:
            name = next(p for p in (s.cached_status() or {}).get("pairs", []) if p["pair_id"] == "acme-helper")["grok"]["name"]
            time.sleep(0.05)
        assert name == "Acme Support Desk"
        with capsys.disabled():
            print(f"\n[fast check, {watch._watcher.mode}] rename in the Grok folder -> Hermes twin updated: "
                  f"{took:.2f} s (of which the fixture's own rename command: {t_cmd:.2f} s)")
    finally:
        t, wt = timer._thread, watch._thread
        plugin._unload()
        t.join(5)
        wt.join(5)


# ---- what the copy says is what the code does -----------------------------------------------------------------------

def test_the_claim_in_seconds_is_backed_by_the_default(home):
    """"In seconds" may stand in the README only while the fast check is on by default and
    disclosed; whoever turns the default off has to change the copy in the same commit."""
    import yaml
    from twin_testlib import ROOT
    m = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    entry = m["config_schema"]["watch_grok_changes"]
    assert entry["type"] == "bool" and entry["default"] is True is config.DEFAULTS["watch_grok_changes"]
    assert m["config_schema"]["sync_interval_seconds"]["default"] == config.DEFAULTS["sync_interval_seconds"] == 60
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "GUIDE.md").read_text(encoding="utf-8")  # How fast and Settings moved there (R14b)
    disclosure = (ROOT / "docs" / "DISCLOSURE.md").read_text(encoding="utf-8")
    assert "in seconds" in readme
    assert watch.MAX_GAP < config.DEFAULTS["sync_interval_seconds"]  # the paced worst case beats the timed check
    assert watch.POLL_S >= 2.0, "no polling loop faster than every 2 s, and only where the system cannot wake us"
    # the new read and the second thread are disclosed (in the one disclosure), and the switch is in the guide
    assert "sand-client-persistence" in disclosure and "watch_grok_changes" in disclosure
    assert "grok-bot-twin-watch" in disclosure and "opened for reading" in disclosure
    assert "`watch_grok_changes`" in guide and "## How fast is it?" in guide
