"""Hermes' skill scanner and cron resume (for "Ready to switch").

Everything here runs only on demand (never in register()). Subprocesses use argument lists, stdin closed,
timeouts and capped output; untrusted bot names are passed as `--bot=NAME` so they can never become flags.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import osflags as _osflags
from . import safe
from .engine import EngineError, check_path_safety, resolve_tool, run_capped, tool_problem_text
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
        if os.name == "posix" and st.st_uid not in (os.getuid(), 0):
            return f"a folder above {what} is owned by another user"
        if st.st_mode & 0o022 and not st.st_mode & 0o1000:
            return f"a folder above {what} is writable by other users"
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def hermes_check() -> dict:
    """The Hermes CLI the plugin runs, resolved once, with its safety check:
    {"argv0": what is executed, "path": its absolute real path, "via": "interpreter"|"path", "problem": str|None}.
    In a Hermes process that is this interpreter with `-m hermes_cli.main` (the venv's interpreter path is kept
    as given, since resolving it would leave the venv; its real file and the folders on both paths are checked);
    else the `hermes` command on PATH, resolved to its real path."""
    try:
        import importlib.util
        has_cli = importlib.util.find_spec("hermes_cli") is not None
    except (ImportError, ValueError):
        has_cli = False
    if has_cli and sys.executable and os.path.isabs(sys.executable):
        real = os.path.realpath(sys.executable)
        problem = check_path_safety(real, "Hermes' Python") or _folders_safe(sys.executable, "Hermes' Python")
        if not problem and os.path.islink(sys.executable):
            try:
                if os.name == "posix" and os.lstat(sys.executable).st_uid not in (os.getuid(), 0):
                    problem = "Hermes' Python link is owned by another user"
            except OSError:
                problem = "Hermes' Python cannot be read"
        return {"argv0": sys.executable, "path": real, "via": "interpreter", "problem": problem}
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
    argv = [chk["argv0"], "-m", "hermes_cli.main"] if chk["via"] == "interpreter" else [chk["argv0"]]
    if profile and profile != "default":
        argv += ["-p", profile]
    return argv


def _env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ", "HERMES_HOME", "VIRTUAL_ENV")}
    env["NO_COLOR"] = "1"
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


def resume_cron(job_id: str, profile: str, ctx: Any = None) -> tuple[bool, str]:
    """Resume one paused Hermes cron job.

    When Hermes offers its tool-approval pipeline (ctx.dispatch_tool), that is the only path: a rejection or
    failure there stops here and is reported, never retried another way. The `hermes [-p P] cron resume ID`
    CLI is used only when that pipeline is genuinely unavailable (no ctx, or a ctx without dispatch_tool)."""
    if not _plain_id(job_id):
        return False, "invalid job id"
    if profile and profile != "default" and not _plain_id(profile):
        return False, "invalid profile name"
    dispatch = getattr(ctx, "dispatch_tool", None) if ctx is not None else None
    if callable(dispatch):
        here = getattr(ctx, "profile_name", None) or "default"
        if here != (profile or "default"):
            return False, (f"open Hermes in the {safe.clean_name(profile, 64)} profile to resume this job "
                           "(its approval step runs there)")
        try:
            raw = dispatch("cronjob_manage", {"action": "resume", "job_id": job_id})
        except Exception as exc:  # the approval pipeline failed: stop and report
            return False, safe.clean_summary(f"resume was not approved or failed: {type(exc).__name__}", 160)
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return False, "resume could not be confirmed"
        if data.get("error") or data.get("success") is False or data.get("approved") is False:
            return False, safe.clean_summary(data.get("error") or "resume was not approved", 160)
        return True, ""
    try:
        rc, out, err, _ = run_capped(hermes_argv(profile) + ["cron", "resume", job_id], 30.0, env=_env(),
                                     out_cap=64 * 1024)
    except EngineError as exc:
        return False, exc.message
    if rc == 0:
        return True, ""
    tail = _summary_lines(err or out, 1)
    return False, tail[0] if tail else f"hermes cron resume exited {rc}"


# How the importers mark the cron jobs they create from a Grok bot's routines (the job record's `paused_reason`,
# set when the job is created paused): botport's Hermes adapter ("Imported by botport from the Grok bot; ...")
# and Nous' agent-importers `hermes import-agent grok-bot` ("Imported from Grok Bot: resume once ...").
IMPORT_MARKERS = ("Imported by botport from the Grok bot", "Imported from Grok Bot:")
JOBS_FILE_CAP = 4 * 1024 * 1024


def _open_below_root(path: Path) -> Optional[int]:
    """Open a file below the Hermes root without following any link on the way: walk folder by folder from the
    root with dir_fd + O_NOFOLLOW|O_DIRECTORY, checking each folder (owned by the user, not group/world-writable),
    then open the file itself with O_NOFOLLOW|O_NONBLOCK. None when anything is missing or unsafe."""
    from . import store
    nofollow = _osflags.o_nofollow()
    dflags = os.O_RDONLY | _osflags.o_directory() | getattr(os, "O_CLOEXEC", 0)
    try:
        root = store.hermes_root().resolve()
        rel = Path(os.path.abspath(path)).relative_to(root)
    except (OSError, ValueError):
        return None
    parts = rel.parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        return None
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
        if os.name == "posix" and st.st_uid != os.getuid():
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


def paused_jobs_for(profile_home: Optional[Path], job_ids: Optional[list] = None) -> list[dict]:
    """Paused cron jobs of a twin's profile that belong to the twin, by stable id: the job ids recorded at create
    (`job_ids`), plus any job an importer marked as made from the bot's routines. Never by job name."""
    known = {j for j in (job_ids or []) if isinstance(j, str) and valid_id(j)}
    out = []
    for j in _load_jobs(profile_home):
        jid = _job_id(j)
        if not jid or not (jid in known or _imported(j)):
            continue
        if j.get("enabled", True) and j.get("state") != "paused":
            continue
        sched = j.get("schedule_display") or j.get("schedule")
        if isinstance(sched, dict):
            sched = sched.get("display") or sched.get("expr") or ""
        out.append({"job_id": jid, "name": safe.clean_name(j.get("name"), 120),
                    "schedule": safe.clean_name(sched if isinstance(sched, str) else "", 60)})
    return out[:100]
