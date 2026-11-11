"""Small file helpers for the fleet code that behave the same on macOS, Linux and Windows.

The rest of the plugin opens files with O_NOFOLLOW and dir_fd, which Windows does not have. A machine that
follows another one may well be a Windows machine, so everything the fleet code reads or writes goes through
these instead: pathlib, an lstat check that refuses links, a size cap, UTF-8, "\\n" line endings on every platform,
and atomic replacement (a temp file in the same folder, then os.replace, retried briefly when Windows reports
the target busy). Folders are made with the store's own fail-closed mkdir.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from . import osflags as _osflags
from . import store

MOVE_TRIES = 15
TEXT_CAP = 1024 * 1024
JSON_CAP = 8 * 1024 * 1024
_POSIX = os.name == "posix"


def is_real_dir(path: Path) -> bool:
    """A folder that is not a link."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and not _osflags.is_link(st)  # on Windows a junction is a link too


def read_text(path: Path, cap: int = TEXT_CAP) -> Optional[str]:
    """The text of a regular file (never a link) of at most `cap` bytes; None when missing, unsafe, too large or
    not UTF-8. Line endings are read as they are and "\\r\\n" is folded to "\\n"; a byte-order mark at the start
    (Windows editors and tools add one) is not part of the text."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > cap:
            return None
        if _POSIX and st.st_uid != os.getuid():
            return None
        with open(path, "rb") as fh:
            data = fh.read(cap + 1)
        if len(data) > cap:
            return None
        return data.decode("utf-8-sig").replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError):
        return None


def _replace(tmp: str, path: Path) -> None:
    last: Optional[OSError] = None
    for _ in range(20):  # Windows: the target may be open in another process for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.05)
    raise last if last else OSError("could not replace the file")


def _style(path: Path) -> tuple:
    """(starts with a byte-order mark, uses "\\r\\n") for an existing file; (False, False) for a new one."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64 * 1024)
    except OSError:
        return False, False
    return head.startswith(b"\xef\xbb\xbf"), b"\r\n" in head


def write_text(path: Path, text: str) -> None:
    """Atomic, owner-only write of a text file with "\\n" line endings. The folder chain is checked (and made) by
    the store first; an existing target must be a regular file, never a link. A file that already uses Windows
    line endings (or starts with a byte-order mark) keeps them, so the owner's own text in it is not rewritten."""
    path = Path(path)
    store.ensure_dir(path.parent)
    bom = crlf = False
    with contextlib.suppress(FileNotFoundError):
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise store.UnsafePath(f"refusing to write {path.name}: it is not a regular file")
        bom, crlf = _style(path)
    data = text.replace("\r\n", "\n").encode("utf-8")
    if crlf:
        data = data.replace(b"\n", b"\r\n")
    if bom:
        data = b"\xef\xbb\xbf" + data
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            if _POSIX:
                os.fchmod(fh.fileno(), 0o600)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    text = read_text(path, JSON_CAP)
    if text is None:
        return default
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return default


def write_json(path: Path, obj: Any) -> None:
    write_text(path, json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


def move_dir(src: Path, dst: Path) -> None:
    """Move a folder (a rename on the same disk; a copy and delete across disks). `dst` must not exist.

    All or nothing on one disk: when the rename is refused (on Windows: a program still has a file in the folder
    open, a chat with that bot say) nothing is moved and the error is raised, after a few tries. Falling back to
    copy-and-delete there would leave a copy at `dst` and a half-deleted folder at `src`, and every later try
    would then find `dst` in its way."""
    if os.path.lexists(dst):
        raise FileExistsError(str(dst))
    store.ensure_dir(Path(dst).parent)
    for attempt in range(MOVE_TRIES):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if attempt == MOVE_TRIES - 1:
                raise
            time.sleep(0.1)  # a virus scanner or the search index had a file open for a moment
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.move(str(src), str(dst))  # another disk
            return


def remove_dir(path: Path, inside: Path) -> None:
    """Delete a folder tree, only when it really sits inside `inside` and is not a link."""
    path, inside = Path(path), Path(inside)
    if not is_real_dir(path) or path == inside or not store.contained(inside, path):
        return
    store.rmtree(path)  # also deletes read-only files, which Windows would otherwise leave behind


LOCK_WAIT_S = 15.0


def lock_file(path: Path, timeout: Optional[float] = None):
    """Windows: take a lock file, waiting up to `timeout` (LOCK_WAIT_S) for another process to let go of it.
    Returns the open file (hand it to unlock_file); None on macOS and Linux, which lock through the store. A
    lock that cannot be opened or had in time raises OSError: the caller must not go on and write without it."""
    msvcrt = _osflags._msvcrt
    if not _osflags.WINDOWS or msvcrt is None:
        return None
    end = time.monotonic() + (LOCK_WAIT_S if timeout is None else timeout)
    store.ensure_dir(Path(path).parent)
    while True:
        fh = None
        try:
            fh = open(path, "a+b")
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return fh
        except OSError:
            if fh is not None:
                fh.close()
            if time.monotonic() >= end:
                raise
            time.sleep(0.05)


def unlock_file(fh: Any) -> None:
    if fh is None:
        return
    with contextlib.suppress(Exception):
        fh.seek(0)
        _osflags._msvcrt.locking(fh.fileno(), _osflags._msvcrt.LK_UNLCK, 1)
    with contextlib.suppress(Exception):
        fh.close()


@contextlib.contextmanager
def exclusive(path: Path):
    """Hold a lock file for as long as the block runs, or yield False at once when another process holds it
    (fcntl on macOS and Linux, msvcrt on Windows). Used so that only one Hermes process follows the source."""
    store.ensure_dir(Path(path).parent)
    fh = open(path, "a+b")
    got = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
        except (OSError, ImportError):
            got = False
        yield got
    finally:
        if got:
            with contextlib.suppress(Exception):
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
