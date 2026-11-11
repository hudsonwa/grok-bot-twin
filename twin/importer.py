"""Hermes' skill scanner and cron resume (for "Move routines to Hermes").

Everything here runs only on demand (never in register()). Subprocesses use argument lists, stdin closed,
timeouts and capped output; untrusted bot names are passed as `--bot=NAME` so they can never become flags.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import osflags as _osflags
from . import safe
from .engine import (WHAT_HERMES, EngineError, check_path_safety, resolve_tool, run_capped,
                     tool_problem_text)
from .model import valid_id

PROBE_TIMEOUT = 15.0


def _plain_id(value: Any) -> bool:
    """A profile or job id that can never be read as an option: [A-Za-z0-9._-], no leading '-'."""
    return valid_id(value) and not value.startswith("-")


def _folders_safe(path: str, what: str) -> Optional[str]:
    """Every folder above `path` (as given, links not followed) owned by the user or root and not writable by
    group/others (sticky folders like /tmp excepted): the same rule check_path_safety applies to botport's."""
    d = os.path.dirname(path)
    while True:
        try:
            st = os.lstat(d)
        except OSError:
            return f"a folder above {what} cannot be read"
        if _osflags.posix() and st.st_uid not in (os.getuid(), 0):
            return f"a folder above {what} is owned by another user"
        # permission bits mean something on macOS/Linux only (Windows reports every folder as 0777)
        if _osflags.posix() and st.st_mode & 0o022 and not st.st_mode & 0o1000:
            return f"a folder above {what} is writable by other users"
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def hermes_code_root() -> tuple:
    """(folder, problem): the folder that holds the `hermes_cli` package this very process uses, and what is
    wrong with it, if anything. A child that is to run Hermes' own command line must import that package and no
    other: `python -m hermes_cli.main` alone looks in the working folder first, which is whatever folder Hermes
    happened to be started in. (None, None) when this process has no `hermes_cli`."""
    try:
        import importlib.util
        spec = importlib.util.find_spec("hermes_cli")
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return None, None
    origin = spec.origin if isinstance(getattr(spec, "origin", None), str) else None
    if not origin or not os.path.isabs(origin):
        places = [p for p in (spec.submodule_search_locations or []) if isinstance(p, str) and os.path.isabs(p)]
        origin = os.path.join(places[0], "__init__.py") if places else None
    if not origin:
        return None, "Hermes' own code cannot be found on this computer"
    package = os.path.dirname(os.path.realpath(origin))
    # the package folder and every folder above it: only the user or root may change them (the rule for botport)
    return os.path.dirname(package), _folders_safe(os.path.join(package, "__init__.py"), "Hermes' code")


def hermes_check() -> dict:
    """The Hermes CLI the plugin runs, resolved once, with its safety check:
    {"argv0": what is executed, "path": its absolute real path, "via": "interpreter"|"path", "problem": str|None,
    "root": the folder of Hermes' own code (interpreter only)}.
    In a Hermes process that is this interpreter with `-P -m hermes_cli.main` (the venv's interpreter path is kept
    as given, since resolving it would leave the venv; its real file and the folders on both paths are checked,
    and so are the folders of the `hermes_cli` this process uses); else the `hermes` command on PATH, resolved
    to its real path."""
    root, root_problem = hermes_code_root()
    if root and sys.executable and os.path.isabs(sys.executable):
        real = os.path.realpath(sys.executable)
        problem = check_path_safety(real, "Hermes' Python") or _folders_safe(sys.executable, "Hermes' Python")
        if not problem and os.path.islink(sys.executable):
            try:
                if _osflags.posix() and os.lstat(sys.executable).st_uid not in (os.getuid(), 0):
                    problem = "Hermes' Python link is owned by another user"
            except OSError:
                problem = "Hermes' Python cannot be read"
        return {"argv0": sys.executable, "path": real, "via": "interpreter", "problem": problem or root_problem,
                "root": root}
    path, problem = resolve_tool("hermes")
    return {"argv0": path, "path": path, "via": "path", "problem": problem}


def hermes_argv(profile: Optional[str] = None) -> list[str]:
    """How to call the Hermes CLI (see hermes_check); refused when it is missing or fails the safety check.
    A profile that is not a plain id (e.g. '--help', '../x') is refused."""
    if profile and profile != "default" and not _plain_id(profile):
        raise EngineError("bad_input", "That is not a valid Hermes profile name.")
    chk = hermes_check()
    if not chk["argv0"]:
        raise EngineError("hermes_not_found", "The hermes command was not found.")
    if chk["problem"]:
        raise EngineError("untrusted_binary", tool_problem_text("hermes", chk["path"], chk["problem"]),
                          "Install Hermes where only you (or root) can change it.")
    # -P: the working folder is kept off the child's import path (it is whatever folder Hermes was started in,
    # and a `hermes_cli` folder lying there would otherwise be run instead of Hermes); _env() names the folder
    # of the Hermes this process runs, so the child imports that one
    argv = [chk["argv0"], "-P", "-m", "hermes_cli.main"] if chk["via"] == "interpreter" else [chk["argv0"]]
    if profile and profile != "default":
        argv += ["-p", profile]
    return argv


def _env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ", "HERMES_HOME", "VIRTUAL_ENV")}
    env["NO_COLOR"] = "1"
    chk = hermes_check()
    if chk["via"] == "interpreter" and chk.get("root"):
        env["PYTHONPATH"] = chk["root"]  # (see hermes_argv) nothing else is added to the child's import path
    return env


def _summary_lines(raw: bytes, limit: int = 12) -> list[str]:
    lines = [safe.clean_summary(ln, 160) for ln in raw.decode("utf-8", errors="replace").splitlines()]
    return [ln for ln in lines if ln][-limit:]


def scan_skill_dir(path: Path) -> Optional[str]:
    """Verdict from Hermes' skill scanner ('safe'|'caution'|'dangerous'), or None when unavailable."""
    try:
        from tools.skills_guard import scan_skill  # lazy: optional, private-but-documented scanner
    except Exception:
        return None
    try:
        verdict = getattr(scan_skill(Path(path), source="community"), "verdict", None)
        return verdict if verdict in ("safe", "caution", "dangerous") else None
    except Exception:
        return None


def _cron_action(action: str, job_id: str, profile: str, ctx: Any = None) -> tuple[bool, str]:
    """Resume or pause one Hermes cron job (`action` is "resume" or "pause").

    When Hermes offers its tool-approval pipeline (ctx.dispatch_tool), that is the only path: a rejection or
    failure there stops here and is reported, never retried another way. The `hermes [-p P] cron <action> ID`
    CLI is used only when that pipeline is genuinely unavailable (no ctx, or a ctx without dispatch_tool)."""
    if action not in ("resume", "pause"):
        return False, "unknown action"
    if not _plain_id(job_id):
        return False, "invalid job id"
    if profile and profile != "default" and not _plain_id(profile):
        return False, "invalid profile name"
    dispatch = getattr(ctx, "dispatch_tool", None) if ctx is not None else None
    if callable(dispatch):
        here = getattr(ctx, "profile_name", None) or "default"
        if here != (profile or "default"):
            return False, (f"open Hermes in the {safe.clean_name(profile, 64)} profile to {action} this job "
                           "(its approval step runs there)")
        try:
            raw = dispatch("cronjob_manage", {"action": action, "job_id": job_id})
        except Exception as exc:  # the approval pipeline failed: stop and report
            return False, safe.clean_summary(f"{action} was not approved or failed: {type(exc).__name__}", 160)
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return False, f"{action} could not be confirmed"
        if data.get("error") or data.get("success") is False or data.get("approved") is False:
            return False, safe.clean_summary(data.get("error") or f"{action} was not approved", 160)
        return True, ""
    try:
        rc, out, err, _ = run_capped(hermes_argv(profile) + ["cron", action, job_id], 30.0, env=_env(),
                                     out_cap=64 * 1024, what=WHAT_HERMES)
    except EngineError as exc:
        return False, exc.message
    if rc == 0:
        return True, ""
    tail = _summary_lines(err or out, 1)
    return False, tail[0] if tail else f"hermes cron {action} exited {rc}"


def resume_cron(job_id: str, profile: str, ctx: Any = None) -> tuple[bool, str]:
    """Resume one paused Hermes cron job (see _cron_action)."""
    return _cron_action("resume", job_id, profile, ctx)


def pause_cron(job_id: str, profile: str, ctx: Any = None) -> tuple[bool, str]:
    """Pause one Hermes cron job again (the undo of "Move routines to Hermes"): same id rules and the same approval path
    as resume_cron."""
    return _cron_action("pause", job_id, profile, ctx)


# ---- a routine from a setup export: made switched off, or not made at all ----------------------------------------
CREATE_TIMEOUT = 60.0
# How the paused reason of a routine made from a setup export starts (see IMPORT_MARKERS below).
SETUP_EXPORT_MARKER = "Imported by Grok Bot Twin from the bot's setup export"
_help_seen: dict = {}


def cron_create_flags(profile: str) -> Optional[set]:
    """Which of `--paused` and `--timezone` this Hermes' `cron create` knows (read from its own help, once per
    Hermes program). None when the help cannot be read: nothing is created then."""
    try:
        argv = hermes_argv(profile)
    except EngineError:
        return None
    key = tuple(argv[:1])
    if key in _help_seen:
        return _help_seen[key]
    try:
        rc, out, err, _ = run_capped(argv + ["cron", "create", "--help"], CREATE_TIMEOUT, env=_env(),
                                     out_cap=64 * 1024, what=WHAT_HERMES)
    except EngineError:
        return None
    if rc != 0:
        return None
    text = (out + b"\n" + err).decode("utf-8", errors="replace")
    # (whole options: "--paused-reason" alone is not "--paused")
    flags = {f for f in ("--paused", "--paused-reason", "--timezone") if re.search(re.escape(f) + r"(?![\w-])", text)}
    _help_seen[key] = flags
    return flags


def create_paused_cron(profile: str, name: str, schedule: str, prompt: str, timezone: str = "",
                       reason: str = SETUP_EXPORT_MARKER) -> tuple[bool, str]:
    """Make one Hermes routine in a twin's own profile, switched off: `hermes -p PROFILE cron create --paused`.
    Every value travels as `--option=value` and the two free texts after `--`, so nothing in a name or a prompt
    can be read as an option. A Hermes that does not know `--paused` makes nothing (the caller asks first with
    cron_create_flags, and the command itself fails on an unknown option): a routine is never made switched on.
    Nothing is retried and no text is logged."""
    if not _plain_id(profile) or profile == "default":
        return False, "invalid profile name"
    if not all(isinstance(v, str) and v and "\x00" not in v for v in (name, schedule, prompt)):
        return False, "invalid routine"
    if not isinstance(timezone, str) or "\x00" in timezone or not isinstance(reason, str):
        return False, "invalid routine"
    try:  # (text that cannot be written as UTF-8, half a surrogate pair, never reaches a command line)
        for v in (name, schedule, prompt, timezone, reason):
            v.encode("utf-8")
    except UnicodeError:
        return False, "invalid routine"
    try:  # (a Hermes that is missing or fails its safety check is a reason this one was not added, not a crash)
        argv = hermes_argv(profile) + ["cron", "create", f"--name={name}", "--deliver=local"]
        if timezone:
            argv.append(f"--timezone={timezone}")
        argv += ["--paused", f"--paused-reason={reason}", "--", schedule, prompt]
        rc, out, err, _ = run_capped(argv, CREATE_TIMEOUT, env=_env(), out_cap=64 * 1024, what=WHAT_HERMES)
    except EngineError as exc:
        if exc.code == "timeout":  # (the caller looks at the routine list in any case: it may have been made)
            return False, "Hermes did not answer in time: look in Hermes whether it was added"
        return False, exc.message
    if rc == 0:
        return True, ""
    # (Hermes' own last line can repeat the prompt: only the exit code is passed on)
    return False, f"Hermes could not add it (hermes cron create ended with {rc})"


def routine_digest(profile_home: Optional[Path], name: Any) -> Optional[str]:
    """A short digest of one routine as Hermes holds it now (its schedule and its prompt; whether it is switched
    on does not count). None when no routine, or more than one, has that name."""
    import hashlib
    hits = [j for j in _load_jobs(profile_home) if isinstance(name, str) and j.get("name") == name]
    if len(hits) != 1 or not isinstance(hits[0].get("prompt"), str):
        return None
    sched = hits[0].get("schedule")
    when = json.dumps(sched, sort_keys=True) if isinstance(sched, (dict, list)) else str(sched)
    return hashlib.sha256(f"{when}\n{hits[0]['prompt']}".encode("utf-8", errors="replace")).hexdigest()[:12]


def job_states(profile_home: Optional[Path]) -> dict:
    """{job id: is switched off} of every routine in a profile, whatever its name."""
    return {jid: not (j.get("enabled", True) and j.get("state") != "paused")
            for j in _load_jobs(profile_home) if (jid := _job_id(j))}


def job_names(profile_home: Optional[Path]) -> dict:
    """{job name: [(job id, is switched off)]} of every routine in a profile (names as Hermes holds them)."""
    out: dict = {}
    for j in _load_jobs(profile_home):
        name = j.get("name")
        if isinstance(name, str) and name:
            off = not (j.get("enabled", True) and j.get("state") != "paused")
            out.setdefault(name, []).append((_job_id(j), off))
    return out


# How the importers mark the cron jobs they create from a Grok bot's routines (the job record's `paused_reason`,
# set when the job is created paused): botport's Hermes adapter ("Imported by botport from the Grok bot; ...")
# and Nous' agent-importers `hermes import-agent grok-bot` ("Imported from Grok Bot: resume once ...").
# ... and the plugin's own "Import setup export" (twin/setup_export.py), which makes them with `cron create --paused`
# (SETUP_EXPORT_MARKER, above).
IMPORT_MARKERS = ("Imported by botport from the Grok bot", "Imported from Grok Bot:", SETUP_EXPORT_MARKER)
JOBS_FILE_CAP = 4 * 1024 * 1024


def _open_below_root(path: Path) -> Optional[int]:
    """Open a file below the Hermes root without following any link on the way: walk folder by folder from the
    root with dir_fd + O_NOFOLLOW|O_DIRECTORY, checking each folder (owned by the user, not group/world-writable),
    then open the file itself with O_NOFOLLOW|O_NONBLOCK. None when anything is missing or unsafe."""
    from . import store
    try:
        root = store.hermes_root().resolve()
        rel = Path(os.path.abspath(path)).relative_to(root)
    except (OSError, ValueError):
        try:  # the same root, written the way HERMES_HOME spells it (a short 8.3 name on Windows, a linked
            # folder above it); nothing below the root is resolved, so a link there is still never followed
            rel = Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(store.hermes_root())))
        except (OSError, ValueError):
            return None
    parts = rel.parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        return None
    if _osflags.WINDOWS:
        return _open_below_root_by_name(root, parts)
    nofollow = _osflags.o_nofollow()
    dflags = os.O_RDONLY | _osflags.o_directory() | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(root, dflags)
    except OSError:
        return None
    try:
        if store._strict_ok(os.fstat(fd)):
            return None
        for part in parts[:-1]:
            nfd = os.open(part, dflags | nofollow, dir_fd=fd)
            os.close(fd)
            fd = nfd
            if store._strict_ok(os.fstat(fd)):
                return None
        return os.open(parts[-1], os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
                       dir_fd=fd)
    except OSError:
        return None
    finally:
        os.close(fd)


def _open_below_root_by_name(root: Path, parts: tuple) -> Optional[int]:
    """The same walk where folders cannot be opened and held (Windows): every folder from the root down is looked
    at by name without following it (a link or a junction is refused), then the file is opened the checked way
    (osflags.open_nofollow)."""
    from . import store
    cur = str(root)
    try:
        if store._strict_ok(os.lstat(cur)):
            return None
        for part in parts[:-1]:
            cur = os.path.join(cur, part)
            if store._strict_ok(os.lstat(cur)):
                return None
        return _osflags.open_nofollow(os.path.join(cur, parts[-1]), os.O_RDONLY)
    except OSError:
        return None


def _read_jobs_file(path: Path) -> Optional[bytes]:
    """Open jobs.json once with O_NOFOLLOW|O_NONBLOCK (a link or a FIFO can never be followed or block us), fstat
    that fd (a regular file, owned by the user, at most JOBS_FILE_CAP bytes), then read from the same fd.
    None when missing or unsafe."""
    fd = _open_below_root(Path(path))
    if fd is None:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > JOBS_FILE_CAP:
            return None
        if _osflags.posix() and st.st_uid != os.getuid():
            return None
        chunks, total = [], 0
        while total <= JOBS_FILE_CAP:
            chunk = os.read(fd, min(1 << 20, JOBS_FILE_CAP + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return None if total > JOBS_FILE_CAP else b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _load_jobs(profile_home: Optional[Path]) -> list[dict]:
    """<profile>/cron/jobs.json (Hermes' cron store: {"jobs": [...]}), read only; [] when absent or unsafe."""
    if profile_home is None:
        return []
    path = Path(profile_home) / "cron" / "jobs.json"
    raw = _read_jobs_file(path)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return [j for j in jobs if isinstance(j, dict)][:1000] if isinstance(jobs, list) else []


def _job_id(j: dict) -> str:
    jid = j.get("id")
    return jid if isinstance(jid, str) and valid_id(jid) else ""


def _imported(j: dict) -> bool:
    reason = j.get("paused_reason")
    return isinstance(reason, str) and reason.startswith(IMPORT_MARKERS)


def imported_job_ids(profile_home: Optional[Path]) -> list[str]:
    """Ids of the cron jobs an importer made from the bot's routines (marked by their paused_reason): recorded
    in the plugin's registry when a twin is created, so they stay known after a resume clears the marker."""
    return [jid for j in _load_jobs(profile_home) if (jid := _job_id(j)) and _imported(j)][:200]


def _jobs_for(profile_home: Optional[Path], job_ids: Optional[list], paused: bool, marked: bool) -> list[dict]:
    known = {j for j in (job_ids or []) if isinstance(j, str) and valid_id(j)}
    out = []
    for j in _load_jobs(profile_home):
        jid = _job_id(j)
        if not jid or not (jid in known or (marked and _imported(j))):
            continue
        is_paused = not (j.get("enabled", True) and j.get("state") != "paused")
        if is_paused != paused:
            continue
        sched = j.get("schedule_display") or j.get("schedule")
        if isinstance(sched, dict):
            sched = sched.get("display") or sched.get("expr") or ""
        out.append({"job_id": jid, "name": safe.clean_name(j.get("name"), 120),
                    "schedule": safe.clean_name(sched if isinstance(sched, str) else "", 60)})
    return out[:100]


def paused_jobs_for(profile_home: Optional[Path], job_ids: Optional[list] = None) -> list[dict]:
    """Paused cron jobs of a twin's profile that belong to the twin, by stable id: the job ids recorded at create
    (`job_ids`), plus any job an importer marked as made from the bot's routines. Never by job name."""
    return _jobs_for(profile_home, job_ids, paused=True, marked=True)


def running_jobs_for(profile_home: Optional[Path], job_ids: Optional[list] = None) -> list[dict]:
    """The twin's cron jobs that are switched on, by the stable ids recorded for it (never by name; an importer's
    marker is gone once a job was resumed, so only recorded ids count)."""
    return _jobs_for(profile_home, job_ids, paused=False, marked=False)
