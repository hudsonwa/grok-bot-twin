"""Central data dir, atomic JSON files (0600, dirs 0700), capped JSONL, and a process+file lock."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from . import config, osflags as _osflags
from .model import PLUGIN_ID, valid_id

try:  # POSIX only: who owns a group (see _private_group)
    import grp as _grp  # type: ignore
    import pwd as _pwd  # type: ignore
except ImportError:  # pragma: no cover
    _grp = _pwd = None  # type: ignore

_LOCK = threading.RLock()
_JSON_CAP = 8 * 1024 * 1024


def hermes_root() -> Path:
    return config.default_hermes_root()


def central_dir() -> Path:
    """<root>/plugin-data/grok-bot-twin (shared by every profile of this Hermes install)."""
    base = hermes_root() / "plugin-data" / PLUGIN_ID
    ensure_dir(base)
    return base


def profile_home(profile: str) -> Path:
    """<root> for the default profile, else <root>/profiles/<name>. A name that is not a plain profile
    id (path parts, '..', a leading '-') raises ValueError, so an engine-provided name can never point
    outside the profiles folder."""
    root = hermes_root()
    if not profile or profile == "default":
        return root
    if not valid_id(profile) or profile.startswith("-"):
        raise ValueError("not a valid profile name")
    if _osflags.WINDOWS and _osflags.reserved_name(profile):
        # CON, COM1, "name." ...: Windows would open a device, or quietly another folder ("name")
        raise ValueError("not a profile name Windows can keep as a folder")
    return root / "profiles" / profile


class UnsafePath(OSError):
    """A folder on the way to the plugin's data is a link, owned by someone else, or writable by others."""


_POSIX = os.name == "posix"


_private_groups: dict = {}


def _private_group(gid: int) -> bool:
    """True when `gid` is the user's own private group: their primary group, named like them, that nobody else
    belongs to (neither listed as a member nor having it as primary group). Fedora, Ubuntu, Arch and others give
    every account such a group, which is why their default umask may be 002: "writable by the group" then still
    means "writable by this user only". Any doubt (no account database, an unknown id) answers False."""
    if not _POSIX or _pwd is None or _grp is None:
        return False
    key = (os.getuid(), gid)
    if key not in _private_groups:
        try:
            me = _pwd.getpwuid(os.getuid())
            group = _grp.getgrgid(gid)
            ok = (gid == me.pw_gid and group.gr_name == me.pw_name
                  and all(member == me.pw_name for member in group.gr_mem)
                  and not any(u.pw_gid == gid and u.pw_uid != me.pw_uid for u in _pwd.getpwall()))
        except (KeyError, OSError, AttributeError):
            ok = False
        _private_groups[key] = ok
    return _private_groups[key]


def _strict_ok(st: os.stat_result, hermes_made: bool = False) -> Optional[str]:
    """At or below the Hermes root: a real folder (never a link), owned by the user, not group/world-writable.
    `hermes_made`: a folder Hermes itself makes with the account's umask (`profiles` and the bots in it). There,
    and only there, group-writable is accepted when the group is the user's own private group (_private_group);
    writable by others never is."""
    if _osflags.is_link(st):
        return "is a symbolic link"
    if not stat.S_ISDIR(st.st_mode):
        return "is not a folder"
    if _POSIX and st.st_uid != os.getuid():
        return "is owned by another user"
    if _POSIX and st.st_mode & 0o002:
        return "is writable by group or others"
    if _POSIX and st.st_mode & 0o020 and not (hermes_made and _private_group(st.st_gid)):
        return "is writable by group or others"
    return None


def _system_ok(st: os.stat_result) -> Optional[str]:
    """Above the Hermes root: owned by the user or root, not group/world-writable unless sticky (like /tmp). A link
    is accepted only when root owns it (system links such as /var or /tmp on macOS); its target is checked by the
    resolved walk."""
    if _osflags.is_link(st):
        return None if (not _POSIX or st.st_uid == 0) else "is a symbolic link"
    if not stat.S_ISDIR(st.st_mode):
        return "is not a folder"
    if _POSIX and st.st_uid not in (os.getuid(), 0):
        return "is owned by another user"
    if _POSIX and st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:
        return "is writable by group or others"
    return None


def _refuse(path: str, why: str) -> UnsafePath:
    return UnsafePath(f"refusing to use the folder {os.path.basename(path) or path}: it {why}")


def _components(path: str) -> list[str]:
    """'/a/b/c' -> ['/', '/a', '/a/b', '/a/b/c'] (absolute, normalised, links not resolved)."""
    path = os.path.normpath(os.path.abspath(path))
    out = [path]
    while True:
        parent = os.path.dirname(out[-1])
        if parent == out[-1]:
            break
        out.append(parent)
    return list(reversed(out))


def _check_system_chain(top: str) -> None:
    """Check every existing folder from / down to `top` with the lenient (system ancestor) rule, both as written
    (lstat, so a user-owned link is refused) and as resolved (so a root-owned link's target is checked too)."""
    for walk in (_components(top), _components(os.path.realpath(top))):
        for comp in walk:
            try:
                st = os.lstat(comp)
            except FileNotFoundError:
                return
            why = _system_ok(st)
            if why:
                raise _refuse(comp, why)


def ensure_dir(path: Path) -> Path:
    """mkdir -p that fails closed. Folders above the Hermes root (/, /Users, a sticky /tmp...) must be owned by the
    user or root and not writable by others; the Hermes root and every folder below it down to `path` must be a
    real folder (lstat, never followed), owned by the user and not group/world-writable. Missing folders are made
    one at a time with 0700 (mkdir, then an lstat check, never following a link); the leaf ends up 0700.
    One exception, for Linux accounts whose umask is 002: `profiles` and a bot's folder in it, which Hermes makes
    itself, may be group-writable when that group is the user's own private group (_private_group).
    A path outside the Hermes root is checked with the same rules, its existing ancestors treated as system ones.
    Raises UnsafePath (an OSError) on any unsafe component."""
    target = os.path.normpath(os.path.abspath(str(path)))
    try:
        root = os.path.normpath(os.path.abspath(str(hermes_root())))
    except Exception:
        root = ""
    comps = _components(target)
    inside = bool(root) and (target == root or target.startswith(root.rstrip(os.sep) + os.sep))
    if inside:
        strict_from = len(_components(root)) - 1  # index of the Hermes root in comps
    else:
        # outside the Hermes root: existing ancestors are system ones; the folders this call creates and the
        # leaf are held to the strict rule.
        strict_from = len(comps)
        for i, comp in enumerate(comps):
            if not os.path.lexists(comp):
                strict_from = i
                break
        strict_from = min(strict_from, len(comps) - 1)
    _check_system_chain(comps[strict_from - 1] if strict_from > 0 else comps[0])
    # Hermes' own folders for its bots: <root>/profiles and <root>/profiles/<name> (made with the umask)
    bots = os.path.join(root, "profiles") if inside else ""
    for comp in comps[strict_from:]:
        hermes_made = bool(bots) and (comp == bots or os.path.dirname(comp) == bots)
        made = False
        try:
            st = os.lstat(comp)
        except FileNotFoundError:
            try:
                _osflags.mkdir_private(comp)
                made = True
            except FileExistsError:
                pass
            st = os.lstat(comp)
        why = _strict_ok(st, hermes_made)
        if why:
            raise _refuse(comp, why)
        if made or comp == target:
            if _POSIX and stat.S_IMODE(st.st_mode) != 0o700:
                os.chmod(comp, 0o700)
                if _strict_ok(os.lstat(comp)):  # swapped for a link between the checks
                    raise _refuse(comp, "changed while it was being checked")
    return Path(path)


def contained(base: Path, path: Path) -> bool:
    """True when path resolves inside base (no traversal, no symlink escape)."""
    try:
        base_r = base.resolve(strict=False)
        path_r = path.resolve(strict=False)
        return path_r == base_r or base_r in path_r.parents
    except (OSError, RuntimeError):
        return False


def safe_child(base: Path, *parts: str) -> Path:
    path = base.joinpath(*parts)
    if not contained(base, path):
        raise ValueError("path escapes the plugin data dir")
    return path


def atomic_write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        # newline="\n": the same bytes on every system (Windows would otherwise write \r\n)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            _osflags.make_private(fh.fileno())
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _osflags.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


def _open_capped(path: Path):
    """Open a regular file for reading without following a link (O_NOFOLLOW, so no check-then-open race; on
    Windows see osflags.open_nofollow); None when it is missing, a link, not a regular file or over the size cap.
    A byte-order mark at the start (Windows tools add one) is skipped."""
    try:
        fd = _osflags.open_nofollow(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _JSON_CAP:
            os.close(fd)
            return None
        return os.fdopen(fd, encoding="utf-8-sig")
    except OSError:
        with contextlib.suppress(OSError):
            os.close(fd)
        return None


def read_json(path: Path, default: Any = None) -> Any:
    fh = _open_capped(path)
    if fh is None:
        return default
    try:
        with fh:
            return json.loads(fh.read(_JSON_CAP + 1))
    except (OSError, ValueError, RecursionError):
        return default


SALT_FILE = "ref-salt"
_SALT_BYTES = 32  # 256 bits (>= 128 required)
_salts: dict = {}  # folder -> (salt, what the salt file looked like when it was read)


def _salt_look(path: Path) -> Optional[tuple]:
    """What tells one salt file from another without reading it; None when there is none."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns, st.st_size


def _read_salt(path: Path) -> Optional[bytes]:
    """The salt file's bytes: opened once without following a link, then fstat on that fd (regular file, the
    user's, owner-only); None when missing. Raises UnsafePath when the file exists but is unsafe or malformed."""
    try:
        fd = _osflags.open_nofollow(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UnsafePath(f"refusing to use the twin id salt: {exc.__class__.__name__}")
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_size > 256
                or (_POSIX and (st.st_uid != os.getuid() or st.st_mode & 0o077))):
            raise UnsafePath("refusing to use the twin id salt: unsafe file")
        raw = os.read(fd, 257).decode("ascii", errors="replace").strip()
    finally:
        os.close(fd)
    try:
        salt = bytes.fromhex(raw)
    except ValueError:
        salt = b""
    if len(salt) < 16:
        raise UnsafePath("refusing to use the twin id salt: malformed")
    return salt


def ref_salt() -> bytes:
    """The per-install random salt (256 bits) behind the opaque twin ids: <central>/ref-salt, hex, 0600, made on
    first use with an atomic create-if-absent (temp file + link, so two processes always agree on one salt).

    What a process remembers counts only while the file is still the one it read. "Remove Grok Bot Twin" deletes
    the file with the rest of the plugin's data; a Hermes process that kept running used to go on with the salt
    it remembered, made no new file, and after its next start every twin had another id: every linked computer
    then took all its twins away and made them anew, chats left behind in the kept place."""
    base = central_dir()
    key = str(base)
    with _LOCK:
        path = base / SALT_FILE
        look = _salt_look(path)
        got = _salts.get(key)
        if got is not None and look is not None and got[1] == look:
            return got[0]
        salt = _read_salt(path)
        if salt is None:
            import secrets
            fd, tmp = tempfile.mkstemp(prefix=f".{SALT_FILE}.", suffix=".tmp", dir=str(base))
            try:
                with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:
                    _osflags.make_private(fh.fileno())
                    fh.write(secrets.token_hex(_SALT_BYTES) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                with contextlib.suppress(FileExistsError):
                    os.link(tmp, path)  # never replaces a salt another process made first
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            salt = _read_salt(path)
            if salt is None:
                raise UnsafePath("could not create the twin id salt")
            look = _salt_look(path)
        _salts[key] = (salt, look)
        return salt


def append_jsonl(path: Path, obj: dict, cap: int = 500) -> None:
    """Append one line; when the file grows past cap+100 lines keep the newest cap (atomic rewrite)."""
    with locked():
        ensure_dir(path.parent)
        line = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        fd = _osflags.open_nofollow(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        lines = read_jsonl_lines(path)
        if len(lines) > cap + 100:
            atomic_write_text(path, "\n".join(lines[-cap:]) + "\n")


def read_jsonl_lines(path: Path) -> list[str]:
    fh = _open_capped(path)
    if fh is None:
        return []
    try:
        with fh:
            return [ln.rstrip("\n") for ln in fh.read(_JSON_CAP + 1).splitlines() if ln.strip()]
    except (OSError, ValueError):
        return []


def read_jsonl(path: Path) -> list[dict]:
    out = []
    for ln in read_jsonl_lines(path):
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _rm_readonly(func: Any, target: Any, _exc: Any) -> None:
    """rmtree's second try for one entry: Windows refuses to delete a read-only file (a git pack file inside a
    copied plugin folder, say), so the read-only mark is cleared first. Still failing: left behind, like before."""
    with contextlib.suppress(OSError):
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        func(target)


def rmtree(path: Any) -> None:
    """shutil.rmtree that also deletes read-only files on Windows and never raises."""
    shutil.rmtree(path, onerror=_rm_readonly)


def remove_tree(base: Path, path: Path) -> None:
    if path.exists() and contained(base, path) and path != base:
        rmtree(path)


_depth = 0
_owner: Optional[int] = None  # the thread inside locked(), else None


# THE LOCK ORDER (one for the whole plugin): a module's own lock, if any, is taken BEFORE locked(), never while holding
# it (else two threads can wait on each other forever; not_inside() checks it); only a memory-only lock that calls
# nothing may be taken inside.
# Never wait on another program under locked(): a `hermes cron` call runs outside it, and the state is read again after.
def not_inside(what: str) -> None:
    if _owner == threading.get_ident():
        raise RuntimeError(f"lock order: {what} is taken before store.locked(), never inside it")


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """Serialise writers: re-entrant thread lock + file lock (CLI, gateway, dashboard and the background
    follower can overlap). The file lock is taken once per outermost holder: flock on macOS/Linux, a one-byte
    region lock on Windows (osflags.lock_file)."""
    global _depth, _owner
    with _LOCK:
        fd = None
        try:
            if _depth == 0 and _osflags.can_lock():
                lock_path = central_dir() / ".lock"
                fd = _osflags.open_nofollow(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    if _POSIX:
                        with contextlib.suppress(OSError):
                            os.chmod(lock_path, 0o600)
                    _osflags.lock_file(fd)
                except BaseException:
                    os.close(fd)
                    fd = None
                    raise
            _depth += 1
            _owner = threading.get_ident()
            yield
        finally:
            _depth = max(0, _depth - 1)
            _owner = _owner if _depth else None
            if fd is not None:
                with contextlib.suppress(OSError):
                    _osflags.unlock_file(fd)
                os.close(fd)


@contextlib.contextmanager
def update(read: Callable[[], Any], write: Callable[[Any], None]) -> Iterator[Any]:
    """`with store.update(load, save) as d:` read, change d in place, write, under locked() (an error writes nothing)."""
    with locked():
        data = read()
        yield data
        write(data)
