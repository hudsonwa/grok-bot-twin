"""F4 (C2): one lock order, and no lock held while Hermes' `hermes cron` runs.

Order 1: an h2g change (h2g._serial, holding the plugin's lock) tells the What syncs panel (sync_panel.mark_synced).
Order 2: allowing an item (_h2g_panel_follow) writes the What syncs row. Both used to take a lock of their own next
to store.locked(), in opposite order: two threads could wait on each other forever. Invented bot only.
"""
from __future__ import annotations

import threading

import pytest

from twin_testlib import plugin  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import h2g, h2g_service, importer, store, sync_panel

BOT = "bot-test-scout"
PID = "test-scout"
ROUTINE = {"name": "Morning digest", "schedule": "0 9 * * *", "prompt": "Say good morning.", "enabled": True}


def _locks_free() -> bool:
    """True when no thread holds the plugin's lock (asked from another thread: the lock is re-entrant)."""
    locks = [store._LOCK] + [lk for lk in (getattr(h2g, "_LOCK", None), getattr(sync_panel, "_lock", None)) if lk]
    free = []

    def probe():
        for lk in locks:
            got = lk.acquire(timeout=0.5)
            free.append(got)
            if got:
                lk.release()
    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(5)
    return len(free) == len(locks) and all(free)


def test_F4_cron_runs_with_no_lock_held(tmp_path, monkeypatch):
    seen: list = []

    def fake_run(argv, timeout, **kw):
        seen.append((argv[-2], _locks_free()))
        return 0, b"", b"", False
    monkeypatch.setattr(importer, "run_capped", fake_run)
    monkeypatch.setattr(importer, "hermes_argv", lambda profile=None: ["hermes"])
    monkeypatch.setattr(importer, "_env", lambda: {})

    writer = h2g.FakeGrokWriter(path=tmp_path / "fake.json")
    writer.put_bot(BOT, name="Test Scout", description="Scouts invented things.", instructions="Be brief.")
    hermes = {"name": "Test Scout", "description": "Scouts invented things.", "instructions": "Be brief.",
              "routines": [ROUTINE]}
    (tmp_path / "central").mkdir()
    x = h2g.H2G(tmp_path / "central", writer, hermes=lambda p: hermes, bot=lambda p: BOT,
                pause_hermes=lambda p, k: importer.pause_cron("job-1", "default", None)[0],
                resume_hermes=lambda p, k: importer.resume_cron("job-1", "default", None)[0])
    x.allow(PID, "all")
    p = x.preview(PID)
    x.apply(PID, p["change_id"])
    assert x.move_runner(PID, "morning-digest", "grok")["runner"] == "grok"
    assert x.move_runner(PID, "morning-digest", "hermes")["runner"] == "hermes"
    writer.delay = True  # the Mirror answers later: the Hermes step runs from finish_moves (the timer's path)
    assert x.move_runner(PID, "morning-digest", "grok")["state"] == "sent"
    writer.delay = False
    assert x.finish_moves()[0]["runner"] == "grok"
    writer.delay = True
    assert x.move_runner(PID, "morning-digest", "hermes")["state"] == "sent"
    writer.delay = False
    assert x.finish_moves()[0]["runner"] == "hermes"
    assert [a for a, _ in seen] == ["pause", "resume", "pause", "resume"]
    assert all(free for _, free in seen), seen


def test_F4_a_change_while_hermes_pauses_switches_nothing_on_in_grok(tmp_path):
    """The lock is free while Hermes pauses its copy, so the move checks again after: another move of this twin's
    routine in between means nothing is switched on in Grok (never two runners)."""
    writer = h2g.FakeGrokWriter()
    writer.put_bot(BOT, name="Test Scout", description="Scouts invented things.", instructions="Be brief.")
    hermes = {"name": "Test Scout", "description": "Scouts invented things.", "instructions": "Be brief.",
              "routines": [ROUTINE]}
    (tmp_path / "central").mkdir()

    def pause(p, k):  # meanwhile, another window moved it (here: straight into the record)
        x._set_runner(p, k, "hermes")
        return True
    x = h2g.H2G(tmp_path / "central", writer, hermes=lambda p: hermes, bot=lambda p: BOT, pause_hermes=pause)
    x.allow(PID, "all")
    x.apply(PID, x.preview(PID)["change_id"])
    with pytest.raises(h2g.H2GError) as e:
        x.move_runner(PID, "morning-digest", "grok")
    assert e.value.code == "moved"
    assert writer.read(BOT)["routines"][0]["enabled"] is False


def test_F4_mark_synced_and_panel_follow_never_deadlock(monkeypatch):
    """Thread A is inside an h2g change (holding the plugin's lock) and calls the panel's mark_synced; thread B is
    in _h2g_panel_follow, about to take store.locked(). With two lock orders this hangs; with one it finishes.
    (Last in this file: on the old code the two stuck threads keep holding their locks.)"""
    meet = threading.Barrier(2)
    real = store.locked
    b_thread: list = []

    def locked():
        if threading.current_thread() in b_thread and not getattr(b_thread[0], "met", False):
            b_thread[0].met = True
            meet.wait(3)  # B holds whatever it took before store.locked(); A holds the plugin's lock now
        return real()
    monkeypatch.setattr(store, "locked", locked)

    def order_1(self):
        meet.wait(3)
        sync_panel.mark_synced(PID, "name", "hermes_to_grok")
    a = threading.Thread(target=h2g._serial(order_1), args=(None,), daemon=True)
    b = threading.Thread(target=h2g_service.H2GMixin._h2g_panel_follow, args=(None, PID, ["name"], []), daemon=True)
    b_thread.append(b)
    a.start()
    b.start()
    a.join(6)
    b.join(6)
    assert not a.is_alive() and not b.is_alive(), "deadlock: the two lock orders waited on each other"
    f = sync_panel._read()
    assert f["synced"][PID]["name"]["direction"] == "hermes_to_grok"
    assert f["twins"][PID]["name"]["direction"] == "both"
