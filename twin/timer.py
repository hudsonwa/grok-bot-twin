"""Lazy background sync loop: every sync_interval_seconds (min 5) one service tick (check + sync; see tick). Never
raises, never backs off. Starts on a hook/REST call once there is work (a twin, or sharing/linked), ends when none is
left, and through ctx.on_unload. The same check also runs right away when the Grok app rewrites its bot list (see
watch.py: a second thread, only with a real twin, that sleeps until that one file changes, then calls kick()).
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from . import config, watch

log = logging.getLogger(__name__)

_stop = threading.Event()
_wake = threading.Event()
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
_state = {"ticks": 0, "last_error": None}


def _interval() -> float:
    try:
        return float(max(config.MIN_INTERVAL, int(config.get("sync_interval_seconds"))))
    except (TypeError, ValueError):
        return 60.0


def _loop(get_service: Callable) -> None:
    delay = 1.0
    while not _stop.is_set():
        _wake.wait(delay)
        _wake.clear()
        if _stop.is_set():
            break
        try:
            svc = get_service()
            if not svc.has_background_work():
                watch.stop()  # nothing left: both threads end; a later call starts them again
                break
            svc.tick()
            _state["last_error"] = None
        except Exception as exc:  # never let the thread die; said once per new problem
            code = getattr(exc, "code", None) or exc.__class__.__name__
            if code != _state["last_error"]:
                log.warning("grok-bot-twin: background check failed (%s)", code, exc_info=True)
            _state["last_error"] = code
        _state["ticks"] += 1
        delay = _interval()


def ensure_started(get_service: Callable) -> bool:
    """Start the loop once per process, once there is work. Returns True when a new thread was started."""
    global _thread
    with _lock:
        if _stop.is_set():
            return False
        try:
            svc = get_service()
            if not svc.has_background_work():
                return False  # a fresh install: no thread; the next hook or page call looks again
            if svc.watch_folder() is not None:  # the fast path (a real twin, the setting on)
                watch.ensure_started(lambda: get_service().watch_folder(), kick)
        except Exception:
            return False
        if _thread is not None and _thread.is_alive():
            return False
        t = config.thread(_loop, "grok-bot-twin-sync", (get_service,))
        t.start()
        _thread = t
        return True


def kick() -> None:
    """Run a check now (debouncing is the caller's job)."""
    _wake.set()


def stop() -> None:
    _stop.set()
    _wake.set()
    watch.stop()
    from . import fleet_api
    fleet_api.stop()


def running() -> bool:
    return _thread is not None and _thread.is_alive() and not _stop.is_set()


def _fast_note() -> str:
    if not config.get("watch_grok_changes"):
        return ""
    return "; a name or description changed in Grok is picked up right away"


def describe() -> str:
    if not running():
        return (f"Background check starts with the first chat or Twins page visit once there is a twin (every "
                f"{int(_interval())} s{_fast_note()})")
    msg = f"Background check every {int(_interval())} s, {_state['ticks']} done{_fast_note()}"
    if _state["last_error"]:
        msg += f"; last problem: {_state['last_error']}"
    return msg


def _reset_for_tests() -> None:
    global _thread
    stop()
    if _thread is not None:
        _thread.join(3)
    _thread = None
    watch._reset_for_tests()
    from . import fleet_api
    fleet_api._reset_for_tests()
    _stop.clear()
    _wake.clear()
