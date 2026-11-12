"""R1 (C3): one lock order. Hermes -> Grok's apply/undo/resolve run inside store.locked(); status() takes
TwinService._lock and then writes under store.locked(). H2G must never call back into status() (that was an ABBA
deadlock), and status() refuses to be entered inside store.locked()."""
from __future__ import annotations

import threading

import pytest

from twin_testlib import home, service, store, svc  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import h2g_mirror
from hermes_plugins.grok_bot_twin.twin.engine import EngineError
from test_h2g_service import PID, _no_panel_choice, _shared, real  # noqa: F401

WAIT = 5.0


class _Deadlock(Exception):
    pass


class _BoundedRLock:
    """TwinService._lock, but a wait longer than WAIT raises instead of hanging the suite (the old code's deadlock)."""

    def __init__(self) -> None:
        self._l = threading.RLock()

    def __enter__(self) -> "_BoundedRLock":
        if not self._l.acquire(timeout=WAIT):
            raise _Deadlock("waited on TwinService._lock while holding store.locked()")
        return self

    def __exit__(self, *exc: object) -> None:
        self._l.release()


def test_R1_apply_and_status_on_two_threads_never_deadlock(real, tmp_path, monkeypatch):  # noqa: F811
    svc, _prof, w = real
    svc.h2g_test_writer = w
    svc.to_grok_allow(PID, "all")
    svc.h2g_test_writer = None
    _shared(svc, tmp_path)  # a real twin now reads Grok through the Mirror's writer (whose read asks the service)
    svc._lock = _BoundedRLock()
    a_holds_lock, b_holds_store = threading.Event(), threading.Event()
    reads, errors = [], []

    orig_read = h2g_mirror.MirrorFolderWriter.read

    def read(self, bot):  # thread B, inside apply's store.locked()
        reads.append(bot)
        b_holds_store.set()
        a_holds_lock.wait(WAIT)
        return orig_read(self, bot)

    orig_demo = type(svc)._demo_status

    def demo_status(self):  # thread A, inside status()'s TwinService._lock: then the write status() makes
        a_holds_lock.set()
        b_holds_store.wait(WAIT)
        with store.locked():  # (as _remember -> _save_registry does)
            pass
        return orig_demo(self)

    monkeypatch.setattr(h2g_mirror.MirrorFolderWriter, "read", read)
    monkeypatch.setattr(type(svc), "_demo_status", demo_status)

    def run(fn):
        def go():
            try:
                fn()
            except EngineError:
                pass  # (the change id is made up: "stale" is fine, the point is that it returns)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        return threading.Thread(target=go, daemon=True)

    a = run(lambda: svc.status(fresh=True))
    b = run(lambda: svc.to_grok_apply(PID, "c-0123456789abcdef"))
    a.start()
    b.start()
    a.join(4 * WAIT)
    b.join(4 * WAIT)
    assert not a.is_alive() and not b.is_alive()
    assert reads, "apply never read the Grok side"
    assert errors == []


def test_R1_status_refuses_inside_store_locked(svc):  # noqa: F811
    svc.status()  # outside the lock: fine
    with store.locked():
        with pytest.raises(RuntimeError, match="lock order"):
            svc.status()
        with store.locked():  # nested: still inside
            with pytest.raises(RuntimeError, match="lock order"):
                svc.status(fresh=True)
    svc.status(fresh=True)  # left: fine again


def test_R1_the_check_is_per_thread(svc):  # noqa: F811
    """Another thread inside store.locked() does not stop this one from taking TwinService._lock first."""
    inside, done = threading.Event(), threading.Event()

    def hold():
        with store.locked():
            inside.set()
            done.wait(WAIT)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    try:
        assert inside.wait(WAIT)
        store.not_inside("TwinService._lock")  # not this thread: no refusal
    finally:
        done.set()
        t.join(WAIT)
