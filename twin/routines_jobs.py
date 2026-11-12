"""Hermes' own file of scheduled jobs for one Hermes bot, as the routines sync reads and changes it.

Hermes keeps a bot's routines in `<bot folder>/cron/jobs.json` (`{"jobs": [...]}`) and guards every change with
a lock on `<bot folder>/cron/.jobs.lock`. This module takes that same lock for each of its own changes, so a
pause written here cannot be overwritten by Hermes' scheduler saving an older picture of the file (and the other
way round). A lock that cannot be had in time raises OSError and nothing is written: a routine is never reported
as paused when the pause may not have reached the file.

The plugin's one reading (`parse`, also for importer.py and fleet_profiles.py) and only writer of that file. Not
through `hermes cron`: it cannot mark a pause as the plugin's (`paused_reason`) or a copy as one (COPY_KEY). A shape
`parse` does not know is refused with a clear message, never guessed at or written over.

Standard library only, so it works where Hermes itself is not importable (the standalone tools, the tests).
Nothing here starts a routine by itself: `resume` only clears the pause; Hermes' scheduler works out the next
run from the schedule, as it does for every job whose next run is missing.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from . import fleet_io as fio, osflags as _osflags

try:  # (absent on Windows)
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None

MARK = "Grok Bot Twin: "  # how a pause made by this plugin starts (the job's `paused_reason`)
COPY_KEY = "grok_bot_twin_routine"  # in a copy's own record: which routine it is a copy of (Hermes keeps keys it
#                                     does not know). So a copy is known again when the plugin's own notes are lost
LOCK_WAIT_S = 12.0
MAX_JOBS = 1000


def cron_dir(home: Path) -> Path:
    return Path(home) / "cron"


def jobs_file(home: Path) -> Path:
    return cron_dir(home) / "jobs.json"


def stamp(home: Path) -> tuple:
    """Cheap "did it change": the file's date and size; () when there is none."""
    try:
        st = os.lstat(jobs_file(home))
    except OSError:
        return ()
    return (st.st_mtime_ns, st.st_size)


UNKNOWN_SHAPE = "Hermes' routines file (cron/jobs.json) is not in the shape this plugin knows"


def parse(raw: Any) -> tuple:
    """(the whole document, its jobs) of Hermes' jobs file, read as JSON: `{"jobs": [job, ...], ...}` (or the bare
    list of jobs Hermes itself still reads), at most MAX_JOBS jobs, each an object with a text "id" whose
    "enabled", "state" and "paused_reason", where present, are a yes/no, a text and a text (or null): the fields
    this module reads and changes. Raises ValueError (UNKNOWN_SHAPE and what is off) for anything else."""
    doc = {"jobs": raw} if isinstance(raw, list) else raw
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), list):
        raise ValueError(f"{UNKNOWN_SHAPE}: no list of jobs")
    jobs = doc["jobs"]
    if len(jobs) > MAX_JOBS:
        raise ValueError(f"{UNKNOWN_SHAPE}: more than {MAX_JOBS} jobs")
    for j in jobs:
        if not (isinstance(j, dict) and isinstance(j.get("id"), str) and j["id"]
                and isinstance(j.get("enabled", True), bool) and isinstance(j.get("state", ""), (str, type(None)))
                and isinstance(j.get("paused_reason"), (str, type(None)))):
            raise ValueError(f"{UNKNOWN_SHAPE}: a job without an id, or with fields of another kind")
    return doc, list(jobs)


def _read(home: Path) -> tuple:
    """(the whole document, its jobs); ({}, []) when there is no file. Raises OSError when the file is there and
    cannot be read or understood: then nothing is concluded from it and nothing is written over it."""
    path = jobs_file(home)
    if not os.path.lexists(path):
        return {}, []
    raw = fio.read_json(path, None)
    if raw is None:
        raise OSError("Hermes' routines file (cron/jobs.json) cannot be read")
    try:
        return parse(raw)
    except ValueError as exc:
        raise OSError(str(exc)) from None


def load(home: Path) -> Optional[list]:
    """The bot's jobs; [] when it has none; None when its file cannot be read or understood (never guessed at)."""
    try:
        return _read(home)[1]
    except OSError:
        return None


def runnable(job: Any) -> bool:
    """Hermes' own rule for "the scheduler may fire this job"."""
    if not isinstance(job, dict):
        return False
    paused = str(job.get("state") or "").strip() == "paused" or bool(job.get("paused_at"))
    return bool(job.get("enabled", True)) and not paused and job.get("state") not in ("completed", "error")


def ours(job: Any) -> bool:
    """Paused by this plugin (and so its to switch on again)."""
    reason = job.get("paused_reason") if isinstance(job, dict) else None
    return isinstance(reason, str) and reason.startswith(MARK)


@contextlib.contextmanager
def _locked(home: Path) -> Iterator[None]:
    cdir = cron_dir(home)
    if not fio.is_real_dir(Path(home)):
        raise OSError("that Hermes bot's folder is not there")
    if os.path.lexists(cdir) and not fio.is_real_dir(cdir):
        raise OSError("the routines folder is not a real folder")
    from . import store
    store.ensure_dir(cdir)
    fh = open(cdir / ".jobs.lock", "a+b")  # (bytes: nothing is written to it, and no line ends are at stake)
    try:
        fh.seek(0)
        end = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                if _fcntl is not None:
                    _fcntl.flock(fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                elif _osflags._msvcrt is not None:
                    _osflags._msvcrt.locking(fh.fileno(), _osflags._msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= end:
                    raise
                time.sleep(0.05)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                if _fcntl is not None:
                    _fcntl.flock(fh, _fcntl.LOCK_UN)
                elif _osflags._msvcrt is not None:
                    fh.seek(0)
                    _osflags._msvcrt.locking(fh.fileno(), _osflags._msvcrt.LK_UNLCK, 1)
    finally:
        fh.close()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def edit(home: Path, fn: Callable[[list], Any]) -> Any:
    """Change the bot's jobs under Hermes' own lock: `fn(jobs)` edits the list in place and returns
    (changed, result). Raises OSError when the lock cannot be had or the file cannot be understood."""
    with _locked(home):
        doc, jobs = _read(home)
        changed, result = fn(jobs)
        if changed:
            out = dict(doc)
            out["jobs"] = jobs
            out["updated_at"] = _now()
            fio.write_text(jobs_file(home), json.dumps(out, indent=2, ensure_ascii=False) + "\n")
        return result


def find(jobs: list, job_id: Any) -> Optional[dict]:
    for j in jobs:
        if j.get("id") == job_id:
            return j
    return None


def new_record(spec: dict, reason: str) -> dict:
    """A paused job the way Hermes writes a new one (`spec`: name, prompt, schedule, skills, timezone)."""
    now = _now()
    skills = list(spec.get("skills") or [])
    schedule = dict(spec["schedule"])
    job = {"id": secrets.token_hex(6), "name": spec["name"], "prompt": spec["prompt"], "skills": skills,
           "skill": skills[0] if skills else None, "model": None, "provider": None, "base_url": None,
           "script": None, "no_agent": False, "monitor_script": None, "monitor_url": None, "monitor_state": None,
           "context_from": None, "schedule": schedule,
           "schedule_display": schedule.get("display") or "",
           "repeat": {"times": 1 if schedule.get("kind") == "once" else None, "completed": 0},
           "enabled": False, "state": "paused", "paused_at": now, "paused_reason": reason, "created_at": now,
           "next_run_at": None, "last_run_at": None, "last_status": None, "last_error": None,
           "last_delivery_error": None, "last_delivery_unverified": None, "last_failure": None, "failure_streak": 0,
           "deliver": "local", "origin": None, "enabled_toolsets": None, "workdir": None}
    if spec.get("timezone"):
        job["timezone"] = spec["timezone"]
    if spec.get("copy_of"):
        job[COPY_KEY] = spec["copy_of"]
    return job


def apply_spec(job: dict, spec: dict) -> bool:
    """Bring a copy's name, prompt, schedule, skills and time zone in line; True when something changed."""
    before = json.dumps(job, sort_keys=True, default=str)
    skills = list(spec.get("skills") or [])
    schedule = dict(spec["schedule"])
    if job.get("schedule") != schedule:
        job["schedule"] = schedule
        job["schedule_display"] = schedule.get("display") or ""
        job["next_run_at"] = None  # worked out again by Hermes from the new schedule
        job.pop("pending_slot", None)
    job.update(name=spec["name"], prompt=spec["prompt"], skills=skills, skill=skills[0] if skills else None)
    if spec.get("timezone"):
        if job.get("timezone") != spec["timezone"]:
            job["timezone"] = spec["timezone"]
            job["next_run_at"] = None
    elif "timezone" in job:
        del job["timezone"]
        job["next_run_at"] = None
    return json.dumps(job, sort_keys=True, default=str) != before


def set_paused(job: dict, reason: str) -> bool:
    """Pause (Hermes' own fields). True when the record changed."""
    if not runnable(job) and job.get("paused_reason") == reason and job.get("enabled") is False:
        return False
    if job.get("state") in ("completed", "error") and not job.get("enabled", True):
        return False  # a finished job is left as Hermes left it
    job.update(enabled=False, state="paused", paused_at=job.get("paused_at") or _now(), paused_reason=reason)
    job.pop("pending_slot", None)
    return True


def set_running(job: dict) -> bool:
    """Clear the pause. The next run is left to Hermes: its scheduler works a missing one out from the schedule
    at its next look (as it does for every job whose next run is missing), counted from then. So a run that
    came due while the job was paused is not kept, and nothing fires the moment the job is switched on. This
    module asks nothing of Hermes' own code."""
    if runnable(job):
        return False
    if job.get("state") in ("completed", "error"):
        return False
    job.update(enabled=True, state="scheduled", paused_at=None, paused_reason=None, next_run_at=None)
    job.pop("pending_slot", None)
    return True
