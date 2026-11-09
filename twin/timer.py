"""Lazy background sync loop: metadata-only check every sync_interval_seconds (min 5), then a
Grok->Hermes sync of the pairs that changed. Never raises; backs off (up to 10 min) on repeated
engine errors. Started on the first hook/REST call, stopped through ctx.on_unload.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from . import config
from .engine import EngineError

log = logging.getLogger(__name__)

MAX_BACKOFF = 600.0
_stop = threading.Event()
_wake = threading.Event()
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
_state = {"ticks": 0, "errors": 0, "last_error": None, "last_tick_at": None, "next_in": None}


def _interval() -> float:
    try:
        return float(max(config.MIN_INTERVAL, int(config.get("sync_interval_seconds"))))
    except (TypeError, ValueError):
        return 60.0


def _loop(get_service: Callable) -> None:
    delay = 1.0
    failures = 0
    while not _stop.is_set():
        _state["next_in"] = delay
        _wake.wait(delay)
        _wake.clear()
        if _stop.is_set():
            break
        try:
            get_service().tick()
            failures = 0
            _state["last_error"] = None
        except EngineError as exc:
            failures += 1
            _state["errors"] += 1
            _state["last_error"] = exc.code
            if failures == 1 or failures % 10 == 0:
                log.warning("grok-bot-twin: background check failed (%s): %s", exc.code, exc.message)
        except Exception as exc:  # never let the thread die
            failures += 1
            _state["errors"] += 1
            _state["last_error"] = exc.__class__.__name__
            log.exception("grok-bot-twin: background check crashed")
        _state["ticks"] += 1
        _state["last_tick_at"] = time.time()
        base = _interval()
        delay = base if failures == 0 else min(MAX_BACKOFF, base * (2 ** min(failures, 10)))


def ensure_started(get_service: Callable) -> bool:
    """Start the loop once per process. Returns True when a new thread was started."""
    global _thread
    with _lock:
        if _stop.is_set() or (_thread is not None and _thread.is_alive()):
            return False
        try:
            from agent.memory_provider import spawn_context_thread  # keeps HERMES_HOME/profile contextvars
            t = spawn_context_thread(_loop, name="grok-bot-twin-sync", daemon=True, args=(get_service,))
        except Exception:
            t = threading.Thread(target=_loop, name="grok-bot-twin-sync", daemon=True, args=(get_service,))
        t.start()
        _thread = t
        return True


def kick() -> None:
    """Run a check now (debouncing is the caller's job)."""
    _wake.set()


def stop() -> None:
    _stop.set()
    _wake.set()


def running() -> bool:
    return _thread is not None and _thread.is_alive() and not _stop.is_set()


def describe() -> str:
    if not running():
        return f"Background check starts with the first chat or Twins page visit (every {int(_interval())} s)"
    msg = f"Background check every {int(_interval())} s, {_state['ticks']} done"
    if _state["last_error"]:
        msg += f"; last problem: {_state['last_error']} (backing off)"
    return msg


def _reset_for_tests() -> None:
    global _thread
    stop()
    if _thread is not None:
        _thread.join(3)
    _thread = None
    _stop.clear()
    _wake.clear()
