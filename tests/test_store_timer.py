"""Atomic writes + permissions, and the lazy background thread."""
from __future__ import annotations

import os
import stat
import threading
import time

import pytest

from twin_testlib import central, home, plugin, store, svc, timer  # noqa: F401


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_atomic_write_perms(home, tmp_path):
    d = tmp_path / "a" / "b" / "c"
    store.atomic_write_json(d / "x.json", {"k": 1})
    assert store.read_json(d / "x.json") == {"k": 1}
    assert _mode(d / "x.json") == 0o600
    for p in (d, d.parent, d.parent.parent):
        assert _mode(p) == 0o700
    assert not [f for f in os.listdir(d) if f.endswith(".tmp")]  # no temp leftovers
    store.atomic_write_text(d / "x.json", "replaced")
    assert (d / "x.json").read_text() == "replaced" and _mode(d / "x.json") == 0o600


def test_atomic_write_cleans_up_on_failure(home, tmp_path):
    class Boom:
        pass
    with pytest.raises(TypeError):
        store.atomic_write_json(tmp_path / "y.json", {"bad": Boom()})
    assert not list(tmp_path.glob(".y.json.*"))
    assert not (tmp_path / "y.json").exists()


def test_symlinks_not_followed(home, tmp_path):
    target = tmp_path / "outside.json"
    target.write_text('{"secret": 1}')
    link = tmp_path / "link.json"
    link.symlink_to(target)
    assert store.read_json(link, "default") == "default"
    store.atomic_write_json(link, {"new": 1})  # replaces the link itself, never writes through it
    assert target.read_text() == '{"secret": 1}' and not link.is_symlink()
    jl = tmp_path / "events.jsonl"
    jl.symlink_to(target)
    with pytest.raises(OSError):
        store.append_jsonl(jl, {"a": 1})
    assert target.read_text() == '{"secret": 1}'


def test_plugin_files_are_private(svc, home):
    svc.demo_action("start")
    svc.demo_action("grok-edit")
    svc.sync()
    base = central(home)
    for p in [base, *base.rglob("*")]:
        if p.is_dir():
            assert _mode(p) == 0o700, p
        elif p.is_file():
            assert _mode(p) == 0o600, p


def test_events_capped(svc, home):
    for i in range(650):
        svc.emit("changed", None, {"n": i})
    lines = (central(home) / "events.jsonl").read_text().splitlines()
    assert len(lines) <= 600


def test_timer_lazy_start_and_stop(svc, monkeypatch):
    calls = []
    monkeypatch.setattr(svc, "tick", lambda: calls.append(time.monotonic()) or 0)
    assert not timer.running()
    assert timer.ensure_started(lambda: svc) is True
    assert timer.running()
    assert timer.ensure_started(lambda: svc) is False  # one thread per process
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert calls, "first check runs about a second after start"
    t = timer._thread
    plugin._unload()  # what ctx.on_unload calls
    t.join(3)
    assert not t.is_alive() and not timer.running()
    assert timer.ensure_started(lambda: svc) is False  # stays stopped after unload
    names = [th.name for th in threading.enumerate()]
    assert "grok-bot-twin-sync" not in names


def test_session_start_kicks_once(svc, monkeypatch):
    svc.demo_action("start", "default")  # on purpose: the sample only joins a real profile when it is named
    kicks = []
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    monkeypatch.setattr(timer, "kick", lambda: kicks.append(1))
    plugin._on_session_start(session_id="a")
    plugin._on_session_start(session_id="b")  # debounced (30 s)
    assert kicks == [1]


def test_session_end_records_marker_only(svc, home):
    svc.demo_action("start", "default")
    plugin._on_session_end(session_id="a", completed=True)
    data = store.read_json(central(home) / "activity.json")
    assert set(data["acme-support"]) == {"last_at", "kind", "turns"}


def test_missing_open_flag_fails_closed(monkeypatch):
    import pytest as _pytest
    from hermes_plugins.grok_bot_twin.twin import osflags
    monkeypatch.delattr(osflags.os, "O_NOFOLLOW", raising=False)
    with _pytest.raises(osflags.UnsupportedPlatform):
        osflags.o_nofollow()


def test_sample_never_joins_the_current_profile_by_default(svc, home, monkeypatch):
    """The sample lives under its own reserved label: no session hook fires for it and no marker is written."""
    svc.demo_action("start")
    kicks = []
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    monkeypatch.setattr(timer, "kick", lambda: kicks.append(1))
    plugin._on_session_start(session_id="a")
    plugin._on_session_end(session_id="a", completed=True)
    assert kicks == [] and not (central(home) / "activity.json").exists()
