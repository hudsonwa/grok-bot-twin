"""Engine interface + BotportEngine (the external `botport` CLI, contract botport.twin.v1, see botport docs/TWIN.md).

How botport is run (every call):
- argv list only, never a shell; every option value is passed inline (`--pair=ID`), so a value can never be read as
  another option; stdin is /dev/null; a timeout (20 s default) kills the whole process group; stdout is capped at
  2 MB, stderr at 64 KB; the child sees a minimal environment (PATH, HOME, LANG, HERMES_GATEWAY_LOCK_DIR, NO_COLOR):
  no tokens or keys are passed through.
- stdout must be exactly one `botport.twin.v1` envelope; its shape and types are checked, unknown fields are kept
  here (internally only: the service rebuilds every result it hands on from an allowlist), strings are lightly cleaned (NUL/control characters dropped, length capped). Exit 1 with `ok:true` means "the
  owner's attention is needed" (a conflict, a failed parity or doctor check) and is not a failure: the data gets
  `_attention: true`. Error `locked` becomes EngineError("locked", "Busy, try again in a moment"); nothing retries.

Trust (fail closed):
- botport is resolved to an absolute real path (symlinks resolved). `trust()` is the explicit owner step: it checks
  the binary and every parent folder are owned by the user or root and not writable by group/others, hashes it,
  makes a private copy and checks the copy's digest against the source's, verifies the GitHub build attestation of
  that copy when required, and only then asks the copy for `version --json` (nothing executes the binary before the
  digest and attestation checks pass), checks the schema and the required features (never the version string), and
  writes a trust record (path, SHA-256, size, mtime, version, features, attestation)
  as an atomic 0600 JSON file in the plugin's central folder.
- Before EVERY run the same ownership/permission checks are repeated on the real path and its parents, the path must
  still resolve to itself (no symlink swapped in), its SHA-256 is recomputed and compared with the record, and the
  private copy is hashed again. Any mismatch raises EngineError("untrusted_binary", reason, hint).
- Copy-verify-exec: what runs is the private copy (`<central>/botport-bin/botport-<sha256 prefix>`, folder 0700, file
  0700), whose digest was just checked. This was chosen over fd-based exec because it works the same on macOS and
  Linux: macOS has no fexecve and exec through /dev/fd is unreliable there (and breaks code-signature checks), while a
  fresh copy keeps the binary's own (ad-hoc) signature. The copy is never rewritten in place (a new file is renamed
  over it), which macOS requires for a binary that may be running.
- `attestation_command(path)` gives the exact `gh attestation verify` argv (repo hudsonwa/botport, signer workflow
  release.yml, and `--source-ref refs/tags/<tag>` pinning the expected release: EXPECTED_BOTPORT_TAG, overridable
  by the botport_release_tag setting or `hermes twin setup --tag`); `verify_attestation()` runs it on the verified copy (gh's digest check covers the exact bytes we
  run) and records the result; doctor reports it. `trust()` refuses to trust without a passing attestation unless
  the owner passes the explicit opt-out `allow_unattested=True`, which is recorded and shown in doctor and status.
- `gh` is resolved once to its absolute real path (`resolve_tool`) and must pass the same ownership/permission
  check as botport before it runs.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import osflags as _osflags
from . import safe
from .model import SCHEMA

TIMEOUT_S = 20.0
CHECK_TIMEOUT_S = 5.0
LONG_TIMEOUT_S = 60.0
ATTEST_TIMEOUT_S = 90.0
STDOUT_CAP = 2 * 1024 * 1024
STDERR_CAP = 64 * 1024
STR_CAP = 256 * 1024
LIST_CAP = 2000
DEPTH_CAP = 16
VALUE_CAP = 1024

# The botport release this plugin version expects. `hermes twin setup` pins it in `gh attestation verify
# --source-ref refs/tags/<tag>`; the owner may override it with the botport_release_tag setting or --tag.
EXPECTED_BOTPORT_TAG = "v0.2.0"
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
ATTEST_REPO = "hudsonwa/botport"
ATTEST_WORKFLOW = "hudsonwa/botport/.github/workflows/release.yml"
INSTALL_HINT = (f"Install botport {EXPECTED_BOTPORT_TAG} from its GitHub release, check it with `gh attestation verify "
                "<path> --repo hudsonwa/botport --signer-workflow hudsonwa/botport/.github/workflows/release.yml "
                f"--source-ref refs/tags/{EXPECTED_BOTPORT_TAG}`, then run `hermes twin setup`. Or try the demo: "
                "`hermes twin demo`.")
TRUST_HINT = "Check and trust botport again: `hermes twin setup` (it verifies the GitHub build attestation)."
UNATTESTED_TEXT = "botport trusted WITHOUT attestation (local build)"

# Features this plugin cannot work without (check these, not the version string).
REQUIRED_FEATURES = ("grok-list", "twin-create", "twin-check", "twin-sync", "twin-status", "twin-conflicts",
                     "twin-resolve", "twin-restore", "twin-recent", "twin-parity", "twin-doctor")
# Methods that need a feature a botport build may not have yet.
OPTIONAL_FEATURES = {"approve": "twin-approve", "mirror": "twin-mirror", "remove": "twin-remove", "demo": "twin-demo"}
DEMO_STEPS = ("rename", "secret", "conflict", "reset")

_ENV_KEEP = ("PATH", "HOME", "LANG")
_ENV_OPTIONAL = ("HERMES_GATEWAY_LOCK_DIR",)
_POSIX = os.name == "posix"


class EngineError(Exception):
    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = safe.clean_name(code, 40) or "internal"
        self.message = safe.clean_summary(message, 300) or "Engine error"
        self.hint = safe.clean_summary(hint, 300)

    def as_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.hint:
            d["hint"] = self.hint
        return d


class Engine:
    """Interface. Return values are the `data` objects of the contract."""
    name = "engine"

    def info(self) -> dict: raise NotImplementedError
    def grok_list(self, only: Optional[list[str]] = None) -> dict: raise NotImplementedError
    def create(self, grok_id: str, profile: Optional[str], dry_run: bool) -> dict: raise NotImplementedError
    def check(self) -> dict: raise NotImplementedError
    def sync(self, pair: Optional[str], direction: str = "g2h", dry_run: bool = False) -> dict: raise NotImplementedError
    def status(self, pair: Optional[str] = None) -> dict: raise NotImplementedError
    def conflicts(self, pair: str) -> dict: raise NotImplementedError
    def resolve(self, pair: str, conflict_id: str, keep: str) -> dict: raise NotImplementedError
    def approve(self, pair: str, item: str) -> dict: raise NotImplementedError
    def restore_points(self, pair: str) -> dict: raise NotImplementedError
    def restore(self, pair: str, point_id: str) -> dict: raise NotImplementedError
    def pack(self, pair: str) -> dict: raise NotImplementedError
    def recent(self, pair: str, limit: int = 10) -> dict: raise NotImplementedError
    def mirror(self, pair: str, pair_folder: Optional[str] = None, off: bool = False) -> dict: raise NotImplementedError
    def remove(self, pair: str, dry_run: bool = False) -> dict: raise NotImplementedError
    def parity(self, pair: str) -> dict: raise NotImplementedError
    def doctor(self) -> dict: raise NotImplementedError


# --------------------------------------------------------------------------------------------------
# locating, checking and hashing the binary

def find_botport(configured: str = "") -> Optional[str]:
    """Absolute real path (symlinks resolved) of the configured or PATH botport, or None."""
    if configured:
        p = Path(configured).expanduser()
        if not p.is_absolute():
            return None
    else:
        found = shutil.which("botport")
        if not found:
            return None
        p = Path(found)
    real = os.path.realpath(str(p))
    if os.path.isfile(real) and os.access(real, os.X_OK):
        return real
    return None


def _home_label(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if home and home != "/" and path.startswith(home + os.sep) else path


def _owner_ok(st: os.stat_result) -> bool:
    return not _POSIX or st.st_uid in (os.getuid(), 0)


def check_path_safety(path: str, what: str = "botport") -> Optional[str]:
    """None when `path` is a real (no symlinks), regular, executable file that only the user or root can change:
    the file and every parent folder owned by the user or root and not writable by group/others (a parent may be
    world-writable only with the sticky bit, like /tmp). Otherwise a one-line reason."""
    if not os.path.isabs(path):
        return f"{what}'s path is not absolute"
    if os.path.realpath(path) != path:
        return f"{what}'s path now goes through a symbolic link"
    try:
        st = os.lstat(path)
    except OSError:
        return f"{what} is missing"
    if not stat.S_ISREG(st.st_mode):
        return f"{what} is not a regular file"
    if not _POSIX:
        return None
    if not _owner_ok(st):
        return f"{what} is owned by another user"
    if st.st_mode & 0o022:
        return f"{what} is writable by other users"
    if not st.st_mode & 0o100:
        return f"{what} is not executable"
    d = os.path.dirname(path)
    while True:
        try:
            ds = os.lstat(d)
        except OSError:
            return f"a folder above {what} cannot be read"
        if stat.S_ISLNK(ds.st_mode) or not stat.S_ISDIR(ds.st_mode):
            return f"a folder above {what} is a link"
        if not _owner_ok(ds):
            return f"a folder above {what} is owned by another user"
        if ds.st_mode & 0o022 and not ds.st_mode & stat.S_ISVTX:
            return f"a folder above {what} is writable by other users"
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


_TOOLS: dict[tuple[str, str], Optional[str]] = {}
_TOOLS_LOCK = threading.Lock()


def resolve_tool(name: str, search_path: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """The `gh` / `hermes` command the plugin runs: its absolute real path (shutil.which + realpath, resolved once
    per PATH and then reused) and the result of the same ownership/permission check as botport's
    (check_path_safety, repeated on every use). Returns (path, None) when safe, (path, reason) when the file or a
    folder above it is unsafe, (None, reason) when it is not on PATH."""
    sp = os.environ.get("PATH", "") if search_path is None else search_path
    key = (name, sp)
    with _TOOLS_LOCK:
        if key not in _TOOLS:
            found = shutil.which(name, path=sp or None)
            _TOOLS[key] = os.path.realpath(found) if found else None
        real = _TOOLS[key]
    if real is None:
        return None, f"{name} was not found on PATH"
    return real, check_path_safety(real, name)


def tool_problem_text(name: str, path: Optional[str], problem: Optional[str]) -> str:
    """One line for a refused tool (shown in errors and doctor)."""
    if path is None:
        return problem or f"{name} was not found on PATH"
    return f"refusing to run {name} at {_home_label(path)}: {problem}"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | _osflags.o_nofollow())
    with os.fdopen(fd, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def attestation_command(path: str, tag: Optional[str] = None) -> list[str]:
    """The exact argv that checks botport's GitHub build attestation (repo + release workflow identity; gh checks the
    file's digest against the attested one). With `tag`, the source ref must be that release tag too."""
    argv = ["gh", "attestation", "verify", str(path), "--repo", ATTEST_REPO, "--signer-workflow", ATTEST_WORKFLOW]
    if tag:
        if not valid_tag(tag):
            raise EngineError("usage", "the botport release tag looks like v0.2.0")
        argv += ["--source-ref", f"refs/tags/{tag}"]
    return argv


def valid_tag(tag: Any) -> bool:
    """A plain release tag like v0.2.0 (nothing else can reach gh's argv)."""
    return isinstance(tag, str) and bool(TAG_RE.match(tag))


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------------------------------
# running a child and reading its envelope

def _read_capped(stream, cap: int, sink: list, flag: dict) -> None:
    total = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            if total < cap:
                sink.append(chunk[: cap - total])
            total += len(chunk)
            if total > cap:
                flag["over"] = True
    except (OSError, ValueError):
        pass


def _kill(proc: subprocess.Popen) -> None:
    with contextlib.suppress(OSError, ProcessLookupError):
        if _POSIX:
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover
            proc.kill()
    with contextlib.suppress(OSError):
        proc.kill()


def run_capped(argv: list[str], timeout: float, env: Optional[dict] = None,
               out_cap: int = STDOUT_CAP) -> tuple[int, bytes, bytes, bool]:
    """Run argv (list, no shell) with stdin closed; returns (rc, stdout, stderr, over_cap).
    Raises EngineError('timeout') after `timeout` seconds (the child's whole process group is killed).
    A child that floods stdout past the cap is killed at once (over_cap=True)."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and "\x00" not in a for a in argv):
        raise EngineError("internal", "bad command line")
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, close_fds=True, start_new_session=True, shell=False)
    except FileNotFoundError:
        raise EngineError("not_found", "botport was not found", INSTALL_HINT)
    except OSError as exc:
        raise EngineError("spawn_failed", f"could not start botport ({exc.__class__.__name__})")
    out: list[bytes] = []
    err: list[bytes] = []
    flags: dict = {}
    t1 = threading.Thread(target=_read_capped, args=(proc.stdout, out_cap, out, flags), daemon=True)
    t2 = threading.Thread(target=_read_capped, args=(proc.stderr, STDERR_CAP, err, {}), daemon=True)
    t1.start(); t2.start()
    deadline = time.monotonic() + timeout
    rc: Optional[int] = None
    while rc is None:
        try:
            rc = proc.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            if flags.get("over"):
                _kill(proc)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=2)
                rc = proc.returncode if proc.returncode is not None else -9
                break
            if time.monotonic() >= deadline:
                _kill(proc)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=2)
                for s in (proc.stdout, proc.stderr):
                    with contextlib.suppress(Exception):
                        s.close()
                raise EngineError("timeout", f"botport did not answer within {int(timeout)} s")
    t1.join(2); t2.join(2)
    for s in (proc.stdout, proc.stderr):
        with contextlib.suppress(Exception):
            s.close()
    return rc, b"".join(out), b"".join(err), bool(flags.get("over"))


_DROP = {c: None for c in list(range(0, 9)) + [11, 12] + list(range(14, 32)) + [127]}


def _light(value: Any, depth: int = 0) -> Any:
    """Light cleaning of botport's data (display cleaning happens in the service): drop NUL/control characters other
    than tab and newline, cap strings, lists and nesting."""
    if depth > DEPTH_CAP:
        return None
    if isinstance(value, str):
        text = value.translate(_DROP)
        return text if len(text) <= STR_CAP else text[:STR_CAP] + "…"
    if isinstance(value, dict):
        return {str(k).translate(_DROP)[:120]: _light(v, depth + 1) for k, v in list(value.items())[:LIST_CAP]}
    if isinstance(value, list):
        return [_light(v, depth + 1) for v in value[:LIST_CAP]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return None


def parse_envelope(rc: int, stdout: bytes, stderr: bytes, over: bool, command: str) -> dict:
    """Validate one botport.twin.v1 envelope and return its `data` (cleaned lightly, with `_warnings` and, on exit 1,
    `_attention: true`). Raises EngineError on any deviation or on `ok:false`."""
    command = safe.clean_name(command, 40)
    if over:
        raise EngineError("output_too_large", "botport output exceeded 2 MB")
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        last = stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] or [""]
        raise EngineError(f"exit_{rc}" if rc else "empty_output",
                          f"botport {command} gave no JSON" + (f": {last[0]}" if last[0] else ""))
    try:
        env = json.loads(text)
    except (ValueError, RecursionError):
        raise EngineError("bad_output", f"botport {command} printed something that is not JSON")
    if not isinstance(env, dict):
        raise EngineError("bad_output", f"botport {command} returned a non-object")
    if env.get("schema") != SCHEMA:
        raise EngineError("schema_mismatch", f"botport speaks {safe.clean_name(env.get('schema'), 40) or 'an old schema'}, "
                          f"this plugin needs {SCHEMA}", "Update botport to 0.2 or newer.")
    ok = env.get("ok")
    warnings = env.get("warnings", [])
    error = env.get("error")
    if (not isinstance(ok, bool) or not isinstance(warnings, list)
            or not isinstance(env.get("command", ""), str)
            or not (error is None or isinstance(error, dict))):
        raise EngineError("bad_output", f"botport {command} returned a malformed envelope")
    if not ok:
        err = error if isinstance(error, dict) else {}
        code = err.get("code") if isinstance(err.get("code"), str) else f"exit_{rc}"
        if code == "locked":
            raise EngineError("locked", "Busy, try again in a moment")
        if code == "account_choice":
            raise account_choice_error(err.get("message"))
        msg = err.get("message") if isinstance(err.get("message"), str) else ""
        hint = err.get("hint") if isinstance(err.get("hint"), str) else ""
        raise EngineError(code, msg or "botport reported a failure", hint)
    if rc not in (0, 1):
        raise EngineError("bad_output", f"botport {command} said ok but exited with {rc}")
    data = env.get("data")
    if not isinstance(data, dict):
        raise EngineError("bad_output", f"botport {command} returned no data")
    data = _light(data)
    data["_warnings"] = [
        {"code": safe.clean_name(w.get("code"), 40), "message": safe.clean_summary(w.get("message"), 200)}
        for w in warnings[:20] if isinstance(w, dict)]
    if rc == 1:
        data["_attention"] = True
    return data


ACCOUNT_RE = re.compile(r"^[0-9a-f]{8}$")
_LABEL_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{8}(?![0-9A-Za-z])")


def account_choice_error(message: Any) -> EngineError:
    """botport's `account_choice`: the Grok app holds several accounts. The owner picks one by its opaque label
    (the first 8 hex characters of a hash; the account itself is never shown) in the grok_account setting."""
    labels = list(dict.fromkeys(_LABEL_RE.findall(message if isinstance(message, str) else "")))[:10]
    listed = f" (labels: {', '.join(labels)})" if labels else ""
    return EngineError("account_choice",
                       f"The Grok app on this computer holds more than one account{listed}. Set the grok_account "
                       "plugin setting to your account's label, then try again.",
                       "Plugin settings: grok_account = the 8-character label of your Grok account.")


def _arg(name: str, value: Any) -> str:
    """One inline option (`--name=value`): never split, never read as another option."""
    if not isinstance(value, str) or not value or len(value) > VALUE_CAP or any(c in value for c in "\x00\n\r"):
        raise EngineError("usage", f"bad value for {name}")
    return f"--{name}={value}"


# --------------------------------------------------------------------------------------------------

class BotportEngine(Engine):
    name = "botport"

    def __init__(self, path: str, hermes_home: Path, state_dir: Path, grok_app_dir: str = "",
                 trust_dir: Optional[Path] = None, account: str = "") -> None:
        self.path = os.path.realpath(str(Path(path).expanduser())) if path else ""
        self.hermes_home = Path(hermes_home)
        self.state_dir = Path(state_dir)
        self.grok_app_dir = grok_app_dir
        self.account = account if isinstance(account, str) and ACCOUNT_RE.match(account) else ""
        self.trust_dir = Path(trust_dir) if trust_dir else self.state_dir.parent
        self.trust_path = self.trust_dir / "botport-trust.json"
        self.exec_dir = self.trust_dir / "botport-bin"
        self._info: Optional[tuple[float, dict]] = None
        self._lock = threading.RLock()

    # -- environment ----------------------------------------------------------------------------------
    def _env(self) -> dict:
        env = {k: os.environ[k] for k in _ENV_KEEP if os.environ.get(k)}
        env.setdefault("PATH", "/usr/bin:/bin")
        for k in _ENV_OPTIONAL:
            if os.environ.get(k):
                env[k] = os.environ[k]
        env["NO_COLOR"] = "1"
        # botport keeps all twin state in --state-dir; pointing its XDG folders inside that folder too means no
        # code path can ever touch the owner's global botport config or state.
        xdg = self.state_dir / "xdg"
        for k, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"),
                       ("XDG_DATA_HOME", "data")):
            env[k] = str(xdg / sub)
        return env

    # -- trust record ---------------------------------------------------------------------------------
    def _read_record(self) -> Optional[dict]:
        from . import store
        try:
            st = os.lstat(self.trust_path)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode) or (_POSIX and (st.st_uid != os.getuid() or st.st_mode & 0o077)):
            raise EngineError("untrusted_binary", "botport's trust record has unsafe permissions", TRUST_HINT)
        rec = store.read_json(self.trust_path)
        if not isinstance(rec, dict) or not isinstance(rec.get("sha256"), str) or not isinstance(rec.get("path"), str):
            return None
        return rec

    def _write_record(self, rec: dict) -> None:
        from . import store
        store.ensure_dir(self.trust_dir)
        store.atomic_write_json(self.trust_path, rec)

    def _copy_path(self, sha: str) -> Path:
        return self.exec_dir / f"botport-{sha[:16]}"

    def _ensure_exec_dir(self) -> None:
        from . import store
        try:
            store.ensure_dir(self.exec_dir)  # every folder from the Hermes root down: real, the user's, private
        except OSError:
            raise EngineError("untrusted_binary", "botport's private copy folder is not safe", TRUST_HINT)
        st = os.lstat(self.exec_dir)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or not _owner_ok(st):
            raise EngineError("untrusted_binary", "botport's private copy folder is not safe", TRUST_HINT)
        if _POSIX and st.st_mode & 0o077:
            os.chmod(self.exec_dir, 0o700)

    def _make_copy(self, src: str, sha: str) -> str:
        """Copy the bytes of `src` that hash to `sha` into a fresh 0700 file; never rewrite an existing copy in place."""
        self._ensure_exec_dir()
        fd = os.open(src, os.O_RDONLY | _osflags.o_nofollow())
        with os.fdopen(fd, "rb") as fh:
            data = fh.read()
        if hashlib.sha256(data).hexdigest() != sha:
            raise EngineError("untrusted_binary", "botport changed while it was being copied", TRUST_HINT)
        dest = self._copy_path(sha)
        tfd, tmp = tempfile.mkstemp(prefix=".botport-", dir=str(self.exec_dir))
        try:
            with os.fdopen(tfd, "wb") as out:
                if _POSIX:
                    os.fchmod(out.fileno(), 0o700)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, dest)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return str(dest)

    def _verified_copy(self, sha: str) -> str:
        """Path of the private copy after checking it is ours, private, and hashes to `sha` (made if missing/bad)."""
        self._ensure_exec_dir()
        dest = self._copy_path(sha)
        for attempt in (0, 1):
            try:
                st = os.lstat(dest)
                good = (stat.S_ISREG(st.st_mode) and _owner_ok(st) and not (_POSIX and st.st_mode & 0o077)
                        and sha256_file(str(dest)) == sha)
            except OSError:
                good = False
            if good:
                return str(dest)
            if attempt == 0:
                with contextlib.suppress(OSError):
                    os.unlink(dest)
                self._make_copy(self.path, sha)
        raise EngineError("untrusted_binary", "botport's private copy does not match the trusted digest", TRUST_HINT)

    def _verify(self) -> tuple[str, dict]:
        """Full pre-run check; returns (path to execute, trust record). Fails closed."""
        if not self.path:
            raise EngineError("not_found", "botport was not found", INSTALL_HINT)
        rec = self._read_record()
        if rec is None:
            raise EngineError("untrusted_binary", "botport has not been checked and trusted yet", TRUST_HINT)
        if rec["path"] != self.path:
            raise EngineError("untrusted_binary", "botport's path differs from the trusted one", TRUST_HINT)
        att = rec.get("attestation") if isinstance(rec.get("attestation"), dict) else {}
        if att.get("verified") is not True and rec.get("allow_unattested") is not True:
            raise EngineError("untrusted_binary", "botport was trusted without a verified attestation", TRUST_HINT)
        problem = check_path_safety(self.path)
        if problem:
            raise EngineError("untrusted_binary", problem, TRUST_HINT)
        try:
            size = os.stat(self.path).st_size
            digest = sha256_file(self.path)
        except OSError:
            raise EngineError("untrusted_binary", "botport cannot be read", TRUST_HINT)
        if size != rec.get("size") or digest != rec["sha256"]:
            raise EngineError("untrusted_binary", "botport changed since it was trusted (SHA-256 differs)", TRUST_HINT)
        with self._lock:
            return self._verified_copy(digest), rec

    def trust(self, allow_unattested: bool = False, tag: Optional[str] = None) -> dict:
        """The explicit setup step. Nothing executes the binary until its digest and its GitHub build attestation
        have been checked. Order: resolve -> ownership/permission checks -> copy into the private folder -> hash the
        copy and compare it with the source's hash -> `gh attestation verify` on that copy -> only then
        `version --json` on the verified copy -> write the trust record.
        The attestation is mandatory unless the owner passes the explicit opt-out `allow_unattested=True` (a local
        build): that is recorded in the trust record (`allow_unattested: true`) and shown by doctor and status.
        `tag` (default EXPECTED_BOTPORT_TAG) is the release tag the attestation must come from (`--source-ref
        refs/tags/<tag>`); the verified tag is recorded as `release_tag` and shown by doctor.
        Returns trust_status()."""
        tag = EXPECTED_BOTPORT_TAG if tag is None else tag
        if not valid_tag(tag):
            raise EngineError("usage", "the botport release tag looks like v0.2.0")
        if not self.path or not os.path.isfile(self.path):
            raise EngineError("not_found", "botport was not found", INSTALL_HINT)
        problem = check_path_safety(self.path)
        if problem:
            raise EngineError("untrusted_binary", problem,
                              "Install botport where only you (or root) can change it, then try again.")
        st = os.stat(self.path)
        digest = sha256_file(self.path)
        with self._lock:
            exe = self._verified_copy(digest)
        try:
            same = sha256_file(exe) == digest
        except OSError:
            same = False
        if not same:
            raise EngineError("untrusted_binary", "botport's private copy does not match its source", TRUST_HINT)
        old = None
        with contextlib.suppress(EngineError):
            old = self._read_record()
        attestation: dict = {"verified": False}
        if (old and old.get("sha256") == digest and isinstance(old.get("attestation"), dict)
                and old["attestation"].get("verified") is True and old["attestation"].get("tag") == tag):
            attestation = old["attestation"]  # these exact bytes were verified before, from the same release tag
        elif not allow_unattested:
            attestation = self._attest(exe, digest, None, tag)
            if not attestation.get("verified"):
                raise EngineError("attestation_failed", attestation.get("problem") or "attestation not verified",
                                  "Install botport from its GitHub release (gh attestation verify must pass). For a "
                                  "local build only: hermes twin setup --allow-unattested")
        unattested = attestation.get("verified") is not True
        # Digest and attestation (or the owner's explicit opt-out) passed: the verified copy may run now.
        data = self._exec(exe, ["version"], CHECK_TIMEOUT_S)
        if data.get("schema") != SCHEMA:
            raise EngineError("engine_too_old", f"botport does not speak {SCHEMA}", "Update botport to 0.2 or newer.")
        features = [f for f in data.get("features", []) if isinstance(f, str)][:60] \
            if isinstance(data.get("features"), list) else []
        missing = [f for f in REQUIRED_FEATURES if f not in features]
        if missing:
            raise EngineError("engine_too_old", "botport lacks features this plugin needs: " + ", ".join(missing),
                              "Update botport to 0.2 or newer.")
        version = safe.clean_name(data.get("version"), 30)
        rec = {"path": self.path, "sha256": digest, "size": st.st_size, "mtime": int(st.st_mtime),
               "version": version, "schema": SCHEMA, "features": features, "trusted_at": _now(),
               "attestation": attestation, "allow_unattested": unattested,
               "release_tag": tag if not unattested else None}
        self._write_record(rec)
        self._info = None
        self._prune_copies(Path(exe).name)
        return self.trust_status()

    def _prune_copies(self, keep: str) -> None:
        """Drop private copies of earlier trusted builds (each is a full binary)."""
        with contextlib.suppress(OSError):
            for entry in os.scandir(self.exec_dir):
                if entry.name.startswith(("botport-", ".botport-")) and entry.name != keep \
                        and entry.is_file(follow_symlinks=False):
                    with contextlib.suppress(OSError):
                        os.unlink(entry.path)

    def untrust(self) -> None:
        with contextlib.suppress(OSError):
            os.unlink(self.trust_path)
        self._info = None

    def trust_status(self, verify: bool = False) -> dict:
        """{"trusted", "path", "sha256", "version", "features", "trusted_at", "attestation":{"verified",...},
        "problem"}. With verify=True the full pre-run check runs (hashes the binary)."""
        out: dict = {"trusted": False, "path": self.path or None, "sha256": None, "version": None, "features": [],
                     "trusted_at": None, "attestation": {"verified": False}, "allow_unattested": False,
                     "release_tag": None, "problem": None}
        try:
            rec = self._read_record()
        except EngineError as exc:
            out["problem"] = exc.message
            return out
        if rec is None:
            out["problem"] = "botport has not been checked and trusted yet"
            return out
        out.update({k: rec.get(k) for k in ("sha256", "version", "features", "trusted_at")})
        if isinstance(rec.get("attestation"), dict):
            out["attestation"] = rec["attestation"]
        out["allow_unattested"] = rec.get("allow_unattested") is True and out["attestation"].get("verified") is not True
        att_tag = out["attestation"].get("tag")
        if out["attestation"].get("verified") is True and valid_tag(att_tag):
            out["release_tag"] = att_tag
        if out["attestation"].get("verified") is not True and not out["allow_unattested"]:
            out["problem"] = "botport was trusted without a verified attestation"
            return out
        if rec.get("path") != self.path:
            out["problem"] = "botport's path differs from the trusted one"
            return out
        if verify:
            try:
                self._verify()
            except EngineError as exc:
                out["problem"] = exc.message
                return out
        out["trusted"] = True
        return out

    def _attest(self, exe: str, digest: str, version: Optional[str], tag: Optional[str] = None) -> dict:
        gh, problem = resolve_tool("gh", self._env().get("PATH"))
        res: dict = {"verified": False, "at": _now(), "sha256": digest, "repo": ATTEST_REPO,
                     "workflow": ATTEST_WORKFLOW, "tag": tag}
        if gh is None:
            res["problem"] = "the GitHub CLI (gh) is not on PATH"
            return res
        if problem:
            res["problem"] = tool_problem_text("gh", gh, problem)
            return res
        argv = attestation_command(exe, tag)
        argv[0] = gh
        try:
            rc, _out, err, _over = run_capped(argv, ATTEST_TIMEOUT_S, env=self._env(), out_cap=256 * 1024)
        except EngineError as exc:
            res["problem"] = exc.message
            return res
        if rc == 0:
            res["verified"] = True
        else:
            last = err.decode("utf-8", errors="replace").strip().splitlines()[-1:] or ["gh attestation verify failed"]
            res["problem"] = safe.clean_summary(last[0], 200)
        return res

    def verify_attestation(self, tag: Optional[str] = None) -> dict:
        """Run `gh attestation verify` on the exact (verified) bytes we execute and record the result.
        `tag` (e.g. "v0.2.0") pins the release tag; default: the tag recorded at setup, else EXPECTED_BOTPORT_TAG."""
        exe, rec = self._verify()
        if tag is None:
            tag = rec.get("release_tag") if valid_tag(rec.get("release_tag")) else EXPECTED_BOTPORT_TAG
        if not valid_tag(tag):
            raise EngineError("usage", "the botport release tag looks like v0.2.0")
        res = self._attest(exe, rec["sha256"], rec.get("version"), tag)
        rec2 = self._read_record()
        if rec2 and rec2.get("sha256") == rec["sha256"]:
            if res.get("verified"):
                rec2["attestation"] = res
                rec2["allow_unattested"] = False
                rec2["release_tag"] = tag
            elif rec2.get("allow_unattested") is True or not (rec2.get("attestation") or {}).get("verified"):
                rec2["attestation"] = res
            else:  # a failed re-check (e.g. gh offline) of bytes verified before: reported to the caller and kept
                rec2["last_attest_check"] = res
            self._write_record(rec2)
        return res

    # -- running ------------------------------------------------------------------------------------
    def _globals(self) -> list[str]:
        g = ["--json", _arg("hermes-home", str(self.hermes_home)), _arg("state-dir", str(self.state_dir))]
        if self.grok_app_dir:
            g.append(_arg("grok-app-dir", str(self.grok_app_dir)))
        return g

    def _account(self) -> list[str]:
        """`--account=LABEL` for the commands that read the Grok cache, when the owner chose an account."""
        return [_arg("account", self.account)] if self.account else []

    def _exec(self, exe: str, args: list[str], timeout: float) -> dict:
        from . import store
        try:
            store.ensure_dir(self.state_dir)
            for sub in ("config", "state", "cache", "data"):  # the XDG folders _env points botport at
                store.ensure_dir(self.state_dir / "xdg" / sub)
        except OSError as exc:
            raise EngineError("unsafe_folder", str(exc) or "botport's state folder is not safe",
                              "Make the plugin's data folders real folders owned by you and not writable by others.")
        argv = [exe, *args, *self._globals()]
        rc, out, err, over = run_capped(argv, timeout, env=self._env())
        return parse_envelope(rc, out, err, over, " ".join(args[:2]))

    def run(self, args: list[str], timeout: float = TIMEOUT_S) -> dict:
        exe, _rec = self._verify()
        return self._exec(exe, args, timeout)

    def _need(self, method: str) -> None:
        feat = OPTIONAL_FEATURES[method]
        rec = None
        with contextlib.suppress(EngineError):
            rec = self._read_record()
        if rec is not None and feat not in (rec.get("features") or []):
            raise EngineError("unsupported", f"this botport cannot do {feat} yet", "Update botport to 0.2 or newer.")

    # -- contract commands -------------------------------------------------------------------------
    def info(self) -> dict:
        if self._info and time.monotonic() - self._info[0] < 300:
            return self._info[1]
        ts = self.trust_status()
        info = {"available": False, "version": None, "problem": None, "features": [], "trusted": ts["trusted"],
                "attested": bool(ts["attestation"].get("verified")), "allow_unattested": bool(ts["allow_unattested"]),
                "release_tag": ts.get("release_tag"), "missing_features": []}
        try:
            data = self.run(["version"], timeout=CHECK_TIMEOUT_S)
            features = [f for f in data.get("features", []) if isinstance(f, str)][:60] \
                if isinstance(data.get("features"), list) else []
            missing = [f for f in REQUIRED_FEATURES if f not in features]
            info.update({"available": not missing and data.get("schema") == SCHEMA,
                         "version": safe.clean_name(data.get("version"), 30) or None,
                         "features": features, "missing_features": missing,
                         "problem": ("botport lacks features this plugin needs: " + ", ".join(missing)) if missing
                         else None})
        except EngineError as exc:
            info["problem"] = exc.message
            info["problem_code"] = exc.code
        self._info = (time.monotonic(), info)
        return info

    def grok_list(self, only: Optional[list[str]] = None) -> dict:
        return self.run(["grok", "list", *[_arg("only", g) for g in only or []], *self._account()])

    def create(self, grok_id: str, profile: Optional[str], dry_run: bool, from_export: Optional[str] = None,
               approve: Optional[str] = None) -> dict:
        args = ["twin", "create", _arg("grok-id", grok_id), *self._account()]
        if profile:
            args.append(_arg("profile", profile))
        if from_export:
            args.append(_arg("from-export", str(from_export)))
        if approve:
            if approve not in ("runnable", "all"):
                raise EngineError("usage", "approve must be runnable or all")
            args.append(_arg("approve", approve))
        if dry_run:
            args.append("--dry-run")
        return self.run(args, timeout=LONG_TIMEOUT_S)

    def create_all(self, only: Optional[list[str]] = None, dry_run: bool = False) -> dict:
        args = ["twin", "create", "--all", *[_arg("only", g) for g in only or []], *self._account()]
        if dry_run:
            args.append("--dry-run")
        return self.run(args, timeout=LONG_TIMEOUT_S)

    def check(self) -> dict:
        return self.run(["twin", "check"], timeout=CHECK_TIMEOUT_S)

    def sync(self, pair: Optional[str], direction: str = "g2h", dry_run: bool = False,
             only: Optional[list[str]] = None) -> dict:
        """`direction` is accepted for backwards compatibility and ignored (botport decides by ownership)."""
        args = ["twin", "sync", _arg("pair", pair) if pair else "--all", *[_arg("only", g) for g in only or []],
                *self._account()]
        if dry_run:
            args.append("--dry-run")
        return self.run(args, timeout=TIMEOUT_S if pair else LONG_TIMEOUT_S)

    def status(self, pair: Optional[str] = None, only: Optional[list[str]] = None) -> dict:
        return self.run(["twin", "status", _arg("pair", pair) if pair else "--all",
                         *[_arg("only", g) for g in only or []], *self._account()])

    def conflicts(self, pair: str) -> dict:
        return self.run(["twin", "conflicts", _arg("pair", pair)])

    def resolve(self, pair: str, conflict_id: str, keep: str) -> dict:
        if keep not in ("hermes", "grok"):
            raise EngineError("usage", "keep must be hermes or grok")
        return self.run(["twin", "resolve", _arg("pair", pair), _arg("conflict", conflict_id), _arg("keep", keep)])

    def approve(self, pair: str, item: str) -> dict:
        """-> {"approved":[{"item","to"}],"sync":{sync entry}}"""
        self._need("approve")
        return self.run(["twin", "approve", _arg("pair", pair), _arg("item", item), *self._account()])

    def restore_points(self, pair: str) -> dict:
        return self.run(["twin", "restore-points", _arg("pair", pair)])

    def restore(self, pair: str, point_id: str, dry_run: bool = False) -> dict:
        args = ["twin", "restore", _arg("pair", pair), _arg("point", point_id)]
        if dry_run:
            args.append("--dry-run")
        return self.run(args)

    def pack(self, pair: str) -> dict:
        """botport has no pack command: "Send to Grok" is a sync of that twin, plus a small `pack` summary."""
        data = self.sync(pair)
        pairs = [p for p in data.get("pairs", []) if isinstance(p, dict)] if isinstance(data.get("pairs"), list) else []
        items = sum(p.get("to_grok") for p in pairs if isinstance(p.get("to_grok"), int))
        data["pack"] = {"gen": None, "items": items, "written_to": None}
        return data

    def recent(self, pair: str, limit: int = 10) -> dict:
        n = max(1, min(50, int(limit)))
        return self.run(["twin", "recent", _arg("pair", pair), _arg("limit", str(n))])

    def mirror(self, pair: str, pair_folder: Optional[str] = None, off: bool = False,
               rotate_key: bool = False) -> dict:
        """-> {"pair_id","connected",("briefs","pair_key","steps":[{"id","what"}],"charter")};
        with rotate_key: {"pair_id","key_rotated":true,"pair_key"} (`twin mirror --rotate-key`)."""
        self._need("mirror")
        if rotate_key:
            if off or pair_folder:
                raise EngineError("usage", "rotate the key on its own")
            return self.run(["twin", "mirror", _arg("pair", pair), "--rotate-key"])
        if off == bool(pair_folder):
            raise EngineError("usage", "give either a pair folder or off")
        if off:
            return self.run(["twin", "mirror", _arg("pair", pair), "--off"])
        folder = os.path.abspath(os.path.expanduser(str(pair_folder)))
        return self.run(["twin", "mirror", _arg("pair", pair), _arg("pair-folder", folder)])

    def remove(self, pair: str, dry_run: bool = False) -> dict:
        """-> {"pair_id","dry_run","profile_kept":true,"removed":[label]}"""
        self._need("remove")
        args = ["twin", "remove", _arg("pair", pair)]
        if dry_run:
            args.append("--dry-run")
        return self.run(args)

    def parity(self, pair: str) -> dict:
        return self.run(["twin", "parity", _arg("pair", pair)])

    def demo(self, step: Optional[str] = None) -> dict:
        """`botport twin demo [--step rename|secret|conflict|reset]` (invented bots, own folders only)."""
        self._need("demo")
        args = ["twin", "demo"]
        if step:
            if step not in DEMO_STEPS:
                raise EngineError("usage", "unknown demo step")
            args.append(_arg("step", step))
        return self.run(args, timeout=LONG_TIMEOUT_S)

    def doctor(self) -> dict:
        """botport's own checks plus `botport_trust` and `botport_attestation`. Never raises for an untrusted binary:
        that becomes a failed check."""
        ts = self.trust_status(verify=True)
        checks: list[dict] = []
        data: dict = {}
        if ts["trusted"]:
            checks.append({"id": "botport_trust", "ok": True,
                           "message": f"botport matches the trusted copy (SHA-256 {str(ts['sha256'])[:12]}…)",
                           "fix": ""})
        else:
            checks.append({"id": "botport_trust", "ok": False, "message": ts["problem"] or "botport is not trusted",
                           "fix": TRUST_HINT})
        att = ts.get("attestation") or {}
        want = ts.get("release_tag") or EXPECTED_BOTPORT_TAG
        cmd = " ".join(attestation_command(_home_label(self.path) if self.path else "<path to botport>", want))
        if att.get("verified"):
            msg = "GitHub build attestation verified" + (f" (release {ts['release_tag']})" if ts.get("release_tag")
                                                         else " (no release tag pinned; run hermes twin setup)")
        elif ts.get("allow_unattested"):
            msg = UNATTESTED_TEXT + (f"; last check: {att['problem']}" if att.get("problem") else "")
        else:
            msg = "GitHub build attestation not verified" + (f": {att['problem']}" if att.get("problem") else "")
        checks.append({"id": "botport_attestation", "ok": bool(att.get("verified")), "message": msg,
                       "fix": "" if att.get("verified") else f"Run: {cmd}, then hermes twin setup"})
        if ts["trusted"]:
            try:
                data = self.run(["twin", "doctor"])
            except EngineError as exc:
                checks.append({"id": "botport_doctor", "ok": False, "message": exc.message, "fix": exc.hint})
        got = data.get("checks") if isinstance(data.get("checks"), list) else []
        out = dict(data)
        out["checks"] = checks + [c for c in got if isinstance(c, dict)]
        if not all(c.get("ok") for c in out["checks"]):
            out["_attention"] = True
        out.setdefault("_warnings", [])
        return out
