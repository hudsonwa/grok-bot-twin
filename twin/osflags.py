"""The few places where macOS/Linux and Windows differ when the plugin touches files.

macOS and Linux: files are opened with O_NOFOLLOW (a link is never followed), made owner-only with fchmod, locked
with flock. A missing flag fails closed with a clear error instead of silently turning into 0.

Windows has none of those calls, so the same promises are kept another way:
- open_nofollow() looks at the name first (lstat: a symbolic link or a junction is refused), opens it, then checks
  that the opened file is the one it looked at. Files are opened in binary mode, so no line ending is rewritten.
- make_private() does nothing: a file under the user's own Hermes folder inherits that folder's access list, which
  on a normal Windows profile already admits only the user, SYSTEM and Administrators.
- lock_file() takes a one-byte region lock (msvcrt.locking) on the plugin's lock file.
- replace() retries for a moment when another program has the target open (a "sharing violation", which Windows
  reports as "access denied"); on macOS/Linux a rename over an open file just works.

WINDOWS is a module attribute so the tests can switch the Windows branches on while running on macOS/Linux
(everything in them except the real msvcrt works there too).
"""
from __future__ import annotations

import errno
import os
import stat
import time
from typing import Any, Optional

WINDOWS = os.name == "nt"
LOCK_WAIT_S = 30.0
REPLACE_WAIT_S = 3.0
_STEP_S = 0.05
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
_LINK_TAGS = (0xA000000C, 0xA0000003)  # a symbolic link, a junction ("mount point")
# Names Windows keeps for devices, whatever the folder and whatever the case: a folder cannot be called this.
WINDOWS_RESERVED = frozenset(["con", "prn", "aux", "nul", "conin$", "conout$"]
                             + [f"com{i}" for i in range(10)] + [f"lpt{i}" for i in range(10)])

try:  # macOS / Linux
    import fcntl as _fcntl  # type: ignore
except ImportError:  # pragma: no cover  (Windows)
    _fcntl = None  # type: ignore
try:  # Windows
    import msvcrt as _msvcrt  # type: ignore
except ImportError:
    _msvcrt = None  # type: ignore

_os_replace = os.replace  # the tests put a failing one here


class UnsupportedPlatform(OSError):
    """This operating system lacks a call the plugin needs to open files safely."""


def _need(name: str) -> int:
    value = getattr(os, name, None)
    if not value:
        raise UnsupportedPlatform(f"unsupported platform: os.{name} is not available, so files cannot be opened "
                                  "safely (Grok Bot Twin supports macOS, Linux and Windows)")
    return value


def posix() -> bool:
    """macOS or Linux (owner and permission bits mean something); False on Windows, real or simulated."""
    return os.name == "posix" and not WINDOWS


def o_nofollow() -> int:
    """O_NOFOLLOW itself (macOS and Linux only; fails closed elsewhere). Use open_nofollow() to open a file."""
    return _need("O_NOFOLLOW")


def o_directory() -> int:
    return _need("O_DIRECTORY")


def is_link(st: os.stat_result) -> bool:
    """A symbolic link; on Windows also a junction (a folder that points somewhere else)."""
    if stat.S_ISLNK(st.st_mode):
        return True
    if getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
        return getattr(st, "st_reparse_tag", 0) in _LINK_TAGS
    return False


def reserved_name(name: Any) -> bool:
    """True for a file or folder name Windows cannot hold: a device name such as CON or COM1 (any case, also with an
    extension), or a name ending in a dot or a space. Checked on every system, so a twin's folder name that works on
    one of the owner's machines works on all of them."""
    if not isinstance(name, str) or not name:
        return True
    if name[-1] in ". ":
        return True
    return name.split(".", 1)[0].strip().lower() in WINDOWS_RESERVED


def same_file(a: os.stat_result, b: os.stat_result) -> bool:
    """Whether two stat results name one file. Where the system reports no file number (0) nobody can tell, so
    the answer is no: the open is refused instead of trusted."""
    if not a.st_ino or not b.st_ino:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _open_checked(path: Any, flags: int, mode: int) -> int:
    """Open without following a link where the system has no O_NOFOLLOW: look (lstat), open, compare."""
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    for _ in range(20):
        try:
            before: Optional[os.stat_result] = os.lstat(path)
        except FileNotFoundError:
            if not flags & os.O_CREAT:
                raise
            before = None
        if before is not None and is_link(before):
            raise OSError(errno.ELOOP, "refusing to open a link", str(path))
        try:
            # a file that was not there is made with O_EXCL, so nothing put there in between is opened
            fd = os.open(path, flags | os.O_EXCL if before is None else flags & ~os.O_EXCL, mode)
        except FileExistsError:
            if flags & os.O_EXCL and before is not None:
                raise
            continue  # made by someone else between the look and the open: look again
        except FileNotFoundError:
            if before is None:
                raise
            continue  # removed between the look and the open: look again
        if before is not None:
            try:
                if not same_file(before, os.fstat(fd)):
                    raise OSError(errno.ELOOP, "the file changed while it was being opened", str(path))
            except OSError:
                os.close(fd)
                raise
        return fd
    raise OSError(errno.EAGAIN, "the file kept changing while it was being opened", str(path))


def open_nofollow(path: Any, flags: int, mode: int = 0o600) -> int:
    """os.open that never follows a link at the last part of `path`. Raises OSError."""
    if WINDOWS:
        return _open_checked(path, flags & ~getattr(os, "O_NONBLOCK", 0), mode)
    return os.open(path, flags | o_nofollow(), mode)


def windows_system_folder() -> str:
    """Windows' own system folder, asked from Windows itself (GetSystemDirectoryW) and not from the environment:
    a changed SystemRoot variable cannot point the plugin's system tools (schtasks, whoami, taskkill, netstat,
    tasklist, wscript) somewhere else. The variables are only what is left where Windows does not answer."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        n = ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260)  # type: ignore[attr-defined]
        if 0 < n < 260 and os.path.isabs(buf.value):
            return buf.value
    except (ImportError, AttributeError, OSError):
        pass
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.join(root, "System32")


def _elevated() -> bool:
    """Windows: this process runs "as administrator" (an elevated prompt, a remote shell, SYSTEM)."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError):
        return False


_ADMIN_OWNERS = ("S-1-5-32-544", "S-1-5-18")  # the Administrators group, SYSTEM


def closed_to_ordinary_programs(path: Any) -> bool:
    """Windows: `path` has an access list of its own (nothing inherited) AND belongs to the Administrators group
    or SYSTEM. That is what a folder made with mode 0700 from an administrator window looks like, and Hermes
    makes an installed plugin's folder that way: the same account's ordinary programs (Hermes Desktop, the
    background helper's scheduled task) are then refused by Windows. A plugin installed over
    a remote shell is then "unreadable" to the account's own Hermes. Only looks (two read-only system calls); False
    on macOS and Linux, and whenever Windows does not answer."""
    if not WINDOWS:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        adv, k32 = ctypes.windll.advapi32, ctypes.windll.kernel32  # type: ignore[attr-defined]
        owner, sd = ctypes.c_void_p(), ctypes.c_void_p()
        adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                              ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        # 1 = a file or folder; 1 | 4 = its owner and its access list
        if adv.GetNamedSecurityInfoW(str(path), 1, 1 | 4, ctypes.byref(owner), None, None, None,
                                     ctypes.byref(sd)) != 0 or not sd.value:
            return False
        try:
            control, revision = wintypes.WORD(), wintypes.DWORD()
            adv.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
                                                         ctypes.POINTER(wintypes.DWORD)]
            if not adv.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision)):
                return False
            if not control.value & 0x1000:  # SE_DACL_PROTECTED: the list is the folder's own
                return False
            text = wintypes.LPWSTR()
            adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
            if not owner.value or not adv.ConvertSidToStringSidW(owner, ctypes.byref(text)):
                return False
            try:
                return str(text.value) in _ADMIN_OWNERS
            finally:
                k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            k32.LocalFree(sd)
    except Exception:
        return False


def mkdir_private(path: Any) -> None:
    """os.mkdir for one of the plugin's own folders: mode 0700 on macOS and Linux.

    Windows, from Python 3.13 on, turns mode 0700 into an access list of its own: SYSTEM, Administrators and the
    folder's owner, nothing inherited. That is the same promise as 0700 as long as the owner is the user. For a
    folder made from an elevated process the owner is the Administrators group, not the user, and the user's own
    ordinary programs (Hermes Desktop, the background helper's scheduled task) are then refused: a
    twin made over a remote shell could not be opened by the same account's scheduled task.
    So an elevated process makes the folder the plain way: it takes the access list of the folder it is made in,
    which under the user's profile names the user, SYSTEM and Administrators, like Hermes' own folders."""
    if WINDOWS and _elevated():
        os.mkdir(path)
    else:
        os.mkdir(path, 0o700)


def make_private(fd: int, mode: int = 0o600) -> None:
    """Owner-only permissions on an open file (macOS/Linux). Windows: nothing to do, see the module text."""
    if not WINDOWS:
        os.fchmod(fd, mode)


def replace(src: Any, dst: Any) -> None:
    """os.replace; on Windows it is tried again for up to REPLACE_WAIT_S while another program holds `dst` open."""
    if not WINDOWS:
        _os_replace(src, dst)
        return
    deadline = time.monotonic() + REPLACE_WAIT_S
    while True:
        try:
            _os_replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_STEP_S)


def is_terminal(stream: Any) -> bool:
    """Whether a person can be asked on this stream. On Windows `isatty()` is also true for the NUL device (what a
    program started with "no input" gets), so the stream must be a real console there."""
    try:
        if stream is None or not stream.isatty():
            return False
    except (AttributeError, ValueError, OSError):
        return False
    if not WINDOWS or _msvcrt is None or not hasattr(_msvcrt, "get_osfhandle"):
        return True
    try:
        import ctypes
        mode = ctypes.c_ulong()
        handle = _msvcrt.get_osfhandle(stream.fileno())
        return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))  # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError, ValueError):
        return True


def can_lock() -> bool:
    return (_msvcrt if WINDOWS else _fcntl) is not None


def lock_file(fd: int) -> None:
    """Take the lock that keeps two Hermes processes from writing the plugin's files at once. Blocks; on Windows
    it gives up with an OSError after LOCK_WAIT_S."""
    if not WINDOWS:
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
        return
    if _msvcrt is None:
        return
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        os.lseek(fd, 0, os.SEEK_SET)  # the lock covers one byte from the current position: always byte 0
        try:
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_STEP_S)


def unlock_file(fd: int) -> None:
    if not WINDOWS:
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        return
    if _msvcrt is None:
        return
    os.lseek(fd, 0, os.SEEK_SET)
    _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
