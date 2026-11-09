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
from typing import Any, Iterator, Optional

from . import osflags as _osflags
from .model import PLUGIN_ID, valid_id

try:  # POSIX only; Windows falls back to the in-process lock.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

_LOCK = threading.RLock()
_JSON_CAP = 8 * 1024 * 1024


def hermes_root() -> Path:
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except Exception:
        env = os.environ.get("HERMES_HOME", "").strip()
        return Path(env).expanduser() if env else Path.home() / ".hermes"


def central_dir() -> Path:
    """<root>/plugin-data/grok-bot-twin (shared by every profile of this Hermes install)."""
    try:
        base = hermes_root() / "plugin-data" / PLUGIN_ID
    except Exception:
        from plugins.plugin_storage import plugin_data_dir  # fallback
        base = Path(plugin_data_dir(PLUGIN_ID))
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
    return root / "profiles" / profile


class UnsafePath(OSError):
    """A folder on the way to the plugin's data is a link, owned by someone else, or writable by others."""


_POSIX = os.name == "posix"


def _strict_ok(st: os.stat_result) -> Optional[str]:
    """At or below the Hermes root: a real folder (never a link), owned by the user, not group/world-writable."""
    if stat.S_ISLNK(st.st_mode):
        return "is a symbolic link"
    if not stat.S_ISDIR(st.st_mode):
        return "is not a folder"
    if _POSIX and st.st_uid != os.getuid():
        return "is owned by another user"
    if _POSIX and st.st_mode & 0o022:
        return "is writable by group or others"
    return None


def _system_ok(st: os.stat_result) -> Optional[str]:
    """Above the Hermes root: owned by the user or root, not group/world-writable unless sticky (like /tmp). A link
    is accepted only when root owns it (system links such as /var or /tmp on macOS); its target is checked by the
    resolved walk."""
    if stat.S_ISLNK(st.st_mode):
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
    for comp in comps[strict_from:]:
        made = False
        try:
            st = os.lstat(comp)
        except FileNotFoundError:
            try:
                os.mkdir(comp, 0o700)
                made = True
            except FileExistsError:
                pass
            st = os.lstat(comp)
        why = _strict_ok(st)
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
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


def _open_capped(path: Path):
    """Open a regular file for reading without following a link (O_NOFOLLOW, so no check-then-open race);
    None when it is missing, a link, not a regular file or over the size cap."""
    try:
        fd = os.open(path, os.O_RDONLY | _osflags.o_nofollow() | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _JSON_CAP:
            os.close(fd)
            return None
        return os.fdopen(fd, encoding="utf-8")
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
_salts: dict[str, bytes] = {}


def _read_salt(path: Path) -> Optional[bytes]:
    """The salt file's bytes: opened once without following a link, then fstat on that fd (regular file, the
    user's, owner-only); None when missing. Raises UnsafePath when the file exists but is unsafe or malformed."""
    try:
        fd = os.open(path, os.O_RDONLY | _osflags.o_nofollow() | getattr(os, "O_NONBLOCK", 0))
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
    first use with an atomic create-if-absent (temp file + link, so two processes always agree on one salt)."""
    base = central_dir()
    key = str(base)
    with _LOCK:
        if key in _salts:
            return _salts[key]
        path = base / SALT_FILE
        salt = _read_salt(path)
        if salt is None:
            import secrets
            fd, tmp = tempfile.mkstemp(prefix=f".{SALT_FILE}.", suffix=".tmp", dir=str(base))
            try:
                with os.fdopen(fd, "w", encoding="ascii") as fh:
                    os.fchmod(fh.fileno(), 0o600)
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
        _salts[key] = salt
        return salt


def append_jsonl(path: Path, obj: dict, cap: int = 500) -> None:
    """Append one line; when the file grows past cap+100 lines keep the newest cap (atomic rewrite)."""
    with locked():
        ensure_dir(path.parent)
        line = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _osflags.o_nofollow(), 0o600)
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


def remove_tree(base: Path, path: Path) -> None:
    if path.exists() and contained(base, path) and path != base:
        shutil.rmtree(path, ignore_errors=True)


_depth = 0


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """Serialise writers: re-entrant thread lock + advisory file lock (CLI, gateway and dashboard
    processes can overlap). The file lock is taken once per outermost holder."""
    global _depth
    with _LOCK:
        fh = None
        try:
            if _depth == 0 and fcntl is not None:
                lock_path = central_dir() / ".lock"
                fh = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT | _osflags.o_nofollow(), 0o600), "a+")
                with contextlib.suppress(OSError):
                    os.chmod(lock_path, 0o600)
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            _depth += 1
            yield
        finally:
            _depth = max(0, _depth - 1)
            if fh is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()
