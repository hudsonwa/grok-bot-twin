"""A simulated Windows for the plugin's file layer, so its Windows code paths run on macOS and Linux too.

What it changes while switched on (all undone by pytest's monkeypatch, or kept for the life of a child process):
- `osflags.WINDOWS` is True: files are opened the "look, open, compare" way, replace() retries, the lock is a
  region lock, nothing is chmod-ed.
- the calls Windows does not have are taken away (`os.fchmod`, `os.getuid`, `os.O_NOFOLLOW`, `os.O_DIRECTORY`),
  so any code that still reaches for one fails here exactly as it would on a real Windows machine.
- `msvcrt` is a small stand-in built on fcntl record locks (one byte, non-blocking, like msvcrt.locking).

What it cannot simulate: Windows' own file-sharing rules (an open file cannot be replaced or deleted), junctions,
`\\` path separators inside pathlib, and the text-mode `\\n` -> `\\r\\n` translation. Tests for those carry the
`windows` marker and run only on a real Windows machine.
"""
from __future__ import annotations

import errno
import os

REAL_WINDOWS = os.name == "nt"


class FakeMsvcrt:
    """msvcrt.locking for one byte, on fcntl: LK_NBLCK fails at once with EACCES when another process holds it."""
    LK_UNLCK, LK_LOCK, LK_NBLCK = 0, 1, 2

    @staticmethod
    def locking(fd: int, mode: int, nbytes: int) -> None:
        import fcntl
        start = os.lseek(fd, 0, os.SEEK_CUR)
        if mode == FakeMsvcrt.LK_UNLCK:
            fcntl.lockf(fd, fcntl.LOCK_UN, nbytes, start, os.SEEK_SET)
            return
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, nbytes, start, os.SEEK_SET)
        except OSError as exc:
            raise OSError(errno.EACCES, "Permission denied") from exc


def enable(setattr_, delattr_) -> None:
    """Switch the simulation on. `setattr_(obj, name, value)` and `delattr_(obj, name)` are monkeypatch's (tests)
    or the builtins (a child process that stays simulated until it ends)."""
    if REAL_WINDOWS:
        return
    from hermes_plugins.grok_bot_twin.twin import engine, osflags, store
    setattr_(osflags, "WINDOWS", True)
    setattr_(osflags, "_msvcrt", FakeMsvcrt)
    setattr_(store, "_POSIX", False)
    setattr_(engine, "_POSIX", False)
    try:
        from hermes_plugins.grok_bot_twin.twin import fleet_io
        setattr_(fleet_io, "_POSIX", False)
    except ImportError:  # a tree without the fleet code
        pass
    for name in ("fchmod", "getuid", "geteuid", "O_NOFOLLOW", "O_DIRECTORY"):
        if hasattr(os, name):
            delattr_(os, name)


def enable_for_process() -> None:
    enable(setattr, delattr)
