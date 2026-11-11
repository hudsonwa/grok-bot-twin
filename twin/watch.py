"""Fast path for "a name or description changed in Grok": wake the background check the moment the Grok app
rewrites its bot list, instead of waiting for the next timed check.

What this module looks at, and nothing else:
- the NAMES of the files in the Grok app's `sand-client-persistence` folder, and
- the date, size and file number of the bot-list file(s) in it (`<base32 of "...roster.last-roster">.blob`).
No file content is ever read here: nothing is opened for reading. Reading the bot list stays the botport helper's
job, after its own checks.

How it waits:
- macOS / BSD: it sleeps in the kernel until that folder or a bot-list file changes (kqueue; the descriptors are
  opened event-only and never read). No timer runs while nothing changes, so the cost while idle is zero.
- elsewhere (Linux): it compares the same dates and sizes every POLL_S seconds (a few microseconds of work).

A change only wakes the existing background check (timer.kick); the check, the copy and every safety rule are the
same as for a timed check. Wake-ups are paced: the first is immediate, and if the bot list keeps being rewritten
they spread out to at most one every MAX_GAP seconds. The timed check (sync_interval_seconds) stays as the safety
net. Nothing is watched while there is no real twin, or when the `watch_grok_changes` setting is off.
"""
from __future__ import annotations

import base64
import contextlib
import logging
import os
import select
import stat
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

CACHE_SUBDIR = "sand-client-persistence"
KEY_PREFIX = "sand.client.slice.account."
ROSTER_SUFFIX = ".roster.last-roster"

POLL_S = 2.0        # the stat-only fallback where the kernel cannot tell us (no kqueue)
REARM_S = 30.0      # with kernel events: how long one wait may last before the folder is looked at again anyway
IDLE_S = 30.0       # nothing to watch (no real twin, switched off): look again this often
GONE_S = 3.0        # there are twins but the Grok folder is missing: one look this often, so "Grok is back" is
                    # noticed within seconds (the page's notice stayed for half a minute after Grok was back)
SETTLE_S = 0.25     # after an event: let a burst of writes finish before looking
FOLDER_TTL_S = 10.0  # how long the "is there anything to watch" answer is reused
MIN_GAP = 2.0       # closest two wake-ups may be
MAX_GAP = 30.0      # widest spacing under constant rewriting
MAX_ENTRIES = 4096  # folder entries looked at per scan
MAX_ROSTERS = 16    # bot-list files tracked (one per signed-in account)
MAX_NAME = 400

_UNSET = object()


def is_roster_name(name: str) -> bool:
    """True for the file name the Grok app gives a bot-list slice. Decided from the name alone."""
    if not isinstance(name, str) or not name.endswith(".blob") or len(name) > MAX_NAME:
        return False
    stem = name[:-5]
    if not stem or not stem.isascii() or not stem.isalnum():
        return False
    try:
        key = base64.b32decode(stem.upper() + "=" * (-len(stem) % 8)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return key.startswith(KEY_PREFIX) and key.endswith(ROSTER_SUFFIX)


def roster_signature(folder: Path) -> Optional[tuple]:
    """(name, date, size, file number) of each bot-list file in `folder`; None when the folder is not there.
    Names and file metadata only; links are not followed and nothing is opened."""
    rows = []
    try:
        with os.scandir(folder) as it:
            for n, entry in enumerate(it):
                if n >= MAX_ENTRIES or len(rows) >= MAX_ROSTERS:
                    break
                if not is_roster_name(entry.name):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    rows.append((entry.name, st.st_mtime_ns, st.st_size, st.st_ino))
    except OSError:
        return None
    return tuple(sorted(rows))


class Pace:
    """Spacing of wake-ups. A change after a quiet spell is passed on at once. A change that arrives while the gap
    since the last wake-up is still running is held until the gap ends, and the gap then doubles (2, 4, 8 ... 30 s),
    so a bot list that is rewritten over and over costs at most one check every MAX_GAP seconds. The gap returns to
    MIN_GAP as soon as a change arrives with no hold."""

    def __init__(self) -> None:
        self.gap = MIN_GAP
        self.last: Optional[float] = None

    def due(self, now: float) -> float:
        return now if self.last is None else max(now, self.last + self.gap)

    def fired(self, now: float, held: bool) -> None:
        self.gap = min(MAX_GAP, self.gap * 2) if held else MIN_GAP
        self.last = now


class _PollWaiter:
    """No kernel events here: sleep POLL_S (or until poked), then the caller compares dates and sizes again."""
    mode = "poll"

    def __init__(self) -> None:
        self._evt = threading.Event()

    def arm(self, folder: Path, names: list) -> bool:
        return True

    def wait(self, timeout: float) -> bool:
        self._evt.wait(min(timeout, POLL_S))
        self._evt.clear()
        return False

    def idle(self, timeout: float) -> None:
        self._evt.wait(timeout)
        self._evt.clear()

    def wake(self) -> None:
        self._evt.set()

    def close(self) -> None:
        pass


class _KqueueWaiter:
    """Sleeps in the kernel until the folder or one of the bot-list files changes. Descriptors are opened
    event-only (O_EVTONLY): they cannot be read from, and they do not keep a disk from being ejected."""
    mode = "events"
    _NOTES = (select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE
              if hasattr(select, "kqueue") else 0)

    def __init__(self) -> None:
        self._kq = select.kqueue()
        self._r, self._w = os.pipe()
        for fd in (self._r, self._w):
            os.set_blocking(fd, False)
        self._kq.control([select.kevent(self._r, select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0, 0)
        self._fds: list = []

    def _open(self, path: str, flags: int) -> bool:
        try:
            fd = os.open(path, flags | getattr(os, "O_EVTONLY", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            return False
        try:
            self._kq.control([select.kevent(fd, select.KQ_FILTER_VNODE, select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                            self._NOTES)], 0, 0)
        except OSError:
            os.close(fd)
            return False
        self._fds.append(fd)
        return True

    def arm(self, folder: Path, names: list) -> bool:
        """Watch the folder and the named files in it. False when the folder cannot be watched."""
        self._disarm()
        if not self._open(str(folder), getattr(os, "O_DIRECTORY", 0)):
            return False
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        for name in names[:MAX_ROSTERS]:
            self._open(os.path.join(str(folder), name), nofollow)
        return True

    def _disarm(self) -> None:
        for fd in self._fds:  # closing a descriptor also takes it out of the queue
            with contextlib.suppress(OSError):
                os.close(fd)
        self._fds = []

    def _drain(self) -> None:
        with contextlib.suppress(OSError):
            while os.read(self._r, 64):
                pass

    def wait(self, timeout: float) -> bool:
        """Block until something watched changes (True), or until the timeout or a poke (False)."""
        try:
            events = self._kq.control(None, 8, max(0.0, timeout))
        except OSError:
            return False
        hit = any(ev.filter == select.KQ_FILTER_VNODE for ev in events)
        if any(ev.filter == select.KQ_FILTER_READ for ev in events):
            self._drain()
        return hit

    def idle(self, timeout: float) -> None:
        self._disarm()
        self.wait(timeout)

    def wake(self) -> None:
        with contextlib.suppress(OSError):
            os.write(self._w, b"x")

    def close(self) -> None:
        self._disarm()
        for fd in (self._r, self._w):
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            self._kq.close()


def make_waiter(force_poll: bool = False):
    if not force_poll and hasattr(select, "kqueue"):
        try:
            return _KqueueWaiter()
        except OSError:
            log.debug("grok-bot-twin: kernel file events are not available, using date/size checks", exc_info=True)
    return _PollWaiter()


class Watcher:
    """One thread. `folder_fn()` gives the folder to watch (None: nothing to watch right now); `on_change()` is
    called, paced, when the bot list in it changed."""

    def __init__(self, folder_fn: Callable[[], Optional[Path]], on_change: Callable[[], None],
                 force_poll: bool = False, clock: Callable[[], float] = time.monotonic) -> None:
        self._folder_fn = folder_fn
        self._on_change = on_change
        self._clock = clock
        self._stop = threading.Event()
        self._waiter = make_waiter(force_poll)
        self._pace = Pace()
        self._folder: tuple = (None, None)  # (asked at, answer)
        self.fires = 0
        self.scans = 0
        self.watching = False

    @property
    def mode(self) -> str:
        return self._waiter.mode

    def _current_folder(self) -> Optional[Path]:
        at, val = self._folder
        now = self._clock()
        if at is None or now - at >= FOLDER_TTL_S:
            try:
                val = self._folder_fn()
            except Exception:
                log.debug("grok-bot-twin: could not tell what to watch", exc_info=True)
                val = None
            self._folder = (now, val)
        return val

    def poke(self) -> None:
        """Something changed on our side (a twin was made or removed, a setting changed): look again now."""
        self._folder = (None, None)
        self._waiter.wake()

    def stop(self) -> None:
        self._stop.set()
        self._waiter.wake()

    def run(self) -> None:
        last: object = _UNSET
        due: Optional[float] = None   # when a held change may be passed on
        held = False
        try:
            while not self._stop.is_set():
                folder = self._current_folder()
                if folder is None:
                    last, due, held, self.watching = _UNSET, None, False, False
                    self._waiter.idle(IDLE_S)
                    continue
                first = roster_signature(folder)
                armed = first is not None and self._waiter.arm(folder, [row[0] for row in first])
                sig = roster_signature(folder) if armed else first  # looked at again once the watch is in place
                self.scans += 1
                self.watching = sig is not None
                now = self._clock()
                if last is not _UNSET and sig != last and due is None:
                    due = self._pace.due(now)
                    held = due > now
                last = sig
                if due is not None and now >= due:
                    self._pace.fired(now, held)
                    due, held = None, False
                    self.fires += 1
                    try:
                        self._on_change()
                    except Exception:
                        log.debug("grok-bot-twin: could not wake the background check", exc_info=True)
                if sig is None or not armed:
                    # folder not there (yet): looked for again soon; a watch that could not be set, less often
                    self._waiter.idle((GONE_S if sig is None else IDLE_S) if due is None else max(0.0, due - now))
                    continue
                timeout = REARM_S if due is None else max(0.0, due - now)
                if self._waiter.wait(timeout) and not self._stop.is_set():
                    self._stop.wait(SETTLE_S)
        finally:
            self.watching = False
            self._waiter.close()


_lock = threading.Lock()
_watcher: Optional[Watcher] = None
_thread: Optional[threading.Thread] = None


def ensure_started(folder_fn: Callable[[], Optional[Path]], on_change: Callable[[], None]) -> bool:
    """Start the watcher once per process. Returns True when a new thread was started."""
    global _watcher, _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        w = Watcher(folder_fn, on_change)
        try:
            from agent.memory_provider import spawn_context_thread  # keeps HERMES_HOME/profile contextvars
            t = spawn_context_thread(w.run, name="grok-bot-twin-watch", daemon=True)
        except Exception:
            t = threading.Thread(target=w.run, name="grok-bot-twin-watch", daemon=True)
        t.start()
        _watcher, _thread = w, t
        return True


def poke() -> None:
    w = _watcher
    if w is not None:
        w.poke()


def stop() -> None:
    w = _watcher
    if w is not None:
        w.stop()


def running() -> bool:
    return _thread is not None and _thread.is_alive()


def mode() -> str:
    """How the running watcher waits: "events" (the system tells it) or "poll" (dates and sizes every POLL_S);
    "off" when it is not running."""
    w = _watcher
    return w.mode if w is not None and running() else "off"


def _reset_for_tests() -> None:
    global _watcher, _thread
    stop()
    if _thread is not None:
        _thread.join(3)
    _watcher, _thread = None, None
