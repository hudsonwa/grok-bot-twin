"""A twin's routines on the owner's other computers: copied to each, switched on on exactly one.

A routine is a scheduled job of a twin's Hermes bot that is a prompt on a repeating schedule. The computer with
the Grok app (the sharing computer) reads them from its twins' own Hermes bots and sends them to the linked
computers inside the signed sync they already make (one optional key, `routines`). A computer that does not
send the key is answered exactly as before.

What travels (`_wire_item` is the allowlist): an opaque id, the twin's id, the routine's name, prompt and
schedule, skill names, whether the owner paused it, and where it runs. The prompt goes through the plugin's
text filter (password-like text masked). Never: scripts and script-only jobs, working folders, model or
address pins, delivery targets, chat ids, outputs, run history, the job's own id.

One computer runs a routine. The sharing computer keeps, per routine, where the owner wants it (`runs_on`) and
the one computer that may have it switched on now (`holder`). A move is a handover: the holder pauses its
Hermes job and says so in its next signed request; only then is the new computer told `go`. The new computer
switches its job on only for a signed list that names it and says `go`. So a routine can be switched on
nowhere for a while (the old computer is off), but never on two computers.

A linked computer keeps copies paused unless its own switch "routines may run on this computer" is on (off by
default): what the sharing computer sends can then at most add paused jobs. Before the plugin changes or
removes a Hermes job on a linked computer it keeps the job as it was (a restore point, 30 days, 30 at most).

Files: `routines.json` in the plugin's data folder (owner-only), and each bot's own `cron/jobs.json`, changed
under Hermes' own lock (routines_jobs.py).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from . import fleet, fleet_io as fio, fleet_profiles as fp, hermes_side as hs, routines_jobs as rj, safe, store
from .engine import EngineError
from .model import REF_RE, ago, iso, now_dt, now_iso, parse_iso, plural

log = logging.getLogger(__name__)

V = 1
STATE_FILE = "routines.json"
MAX_ROUTINES = 200
MAX_REMOVED = 500
MAX_RESTORE = 30
RESTORE_DAYS = 30
REMOVED_DAYS = 90
NAME_CAP = 120
PROMPT_CAP = 8000
TICK_S = 30.0
MOVE_ASK_S = 90  # a move asked for on a linked computer is repeated to the sharing computer for this long
ID_RE = re.compile(r"^j-[0-9a-f]{16}\Z")
UID_RE = re.compile(r"^u-[0-9a-f]{12}\Z")
CRON_RE = re.compile(r"^[0-9A-Za-z*/,\-#?]+(?: [0-9A-Za-z*/,\-#?]+){4,6}\Z")
SKILL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}\Z")
ZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]{0,31}(?:/[A-Za-z0-9_+\-]{1,31}){0,2}\Z")
MAX_MINUTES = 366 * 24 * 60
WIRE_BUDGET = 512 * 1024  # bytes of routine text in one answer; the answer rides in the sync, which caps at 2 MiB

SENTENCE = ("Routines copy to every linked computer. Each one runs on one computer only; "
            "the others keep it paused and say where it runs.")
SENTENCE_OFF_SOURCE = "Routines stay on this computer. Switch this on to copy them to your linked computers."
SENTENCE_OFF_LINKED = "Routines are not copied to this computer."
WHAT_SYNCS = ["each routine's name, schedule and prompt", "the names of the skills it uses",
              "which computer runs it", "whether you paused it"]
NEVER = ["routines that run a script", "one-off reminders", "what a routine produced and when it ran",
         "where it delivers", "its model", "passwords and keys"]
MESSAGES = {
    "unknown_routine": ("That routine is no longer there.", None),
    "unknown_machine": ("That computer is not linked.", None),
    "cannot_run_there": ("{name} does not let routines run on it yet.",
                         "On {name}, switch on \"Routines may run on this computer\", then try again."),
    "not_reachable": ("{name} is not reachable right now, so the routine cannot start there.",
                      "Open Hermes on {name}, then try again. The routine keeps running where it is."),
    "still_running_there": ("{name} has not confirmed that it stopped this routine.",
                            "It moves by itself as soon as {name} is back."),
    "routines_off": ("Routines are not copied yet.", "Switch on Routines first."),
    "not_linked": ("This computer is not linked to another one.", None),
    "unknown_restore": ("That restore point is no longer kept.", None),
    "older_source": ("{name} has an older Grok Bot Twin, so routines do not copy yet.",
                     "Update Grok Bot Twin on {name}: Capabilities → Plugins → Update. Routines then follow by themselves."),
    "busy": ("Hermes is working on this bot's routines right now.", "Try again in a moment."),
    "ask_there": ("From this computer a routine can be run here, or back on the computer with the Grok app.",
                  "To run it on {name}, say so on {name} or on the computer with the Grok app."),
}


class RoutinesError(EngineError):
    def __init__(self, code: str, **fmt: Any) -> None:
        msg, hint = MESSAGES[code]
        super().__init__(code, msg.format(**fmt), hint.format(**fmt) if hint else None)


def problem(code: str, **fmt: Any) -> dict:
    msg, hint = MESSAGES[code]
    out = {"code": code, "message": msg.format(**fmt)}
    if hint:
        out["hint"] = hint.format(**fmt)
    return out


# ---- state file ------------------------------------------------------------------------------------------------------
def state_path() -> Path:
    return store.central_dir() / STATE_FILE


NO_GEN = "00000000"


def _blank_src() -> dict:
    return {"gen": NO_GEN, "counter": 0, "rev": "0-00000000", "digest": "", "items": {},
            "removed": {}, "local_only": 0, "local": {}, "peers": {}, "stamps": {}}


def _new_src() -> dict:
    """The sharing computer's list, made for the first time: with a generation of its own, made once. A linked
    computer takes an older list of the same generation for what it is (one that arrived late)."""
    return {**_blank_src(), "gen": secrets.token_hex(4)}


def _blank_dst() -> dict:
    return {"gen": None, "rev": None, "supported": None, "source_on": None, "items": {}, "can_run": {},
            "move": None, "heard_at": None, "stamps": {}}


class Unreadable(OSError):
    """The routines file is there and cannot be understood right now: nothing is concluded from it and nothing
    is written over it."""


def load() -> dict:
    """The routines state, always with every key. An unreadable file counts as empty; nothing is written."""
    raw = fio.read_json(state_path(), None)
    R = {"on": None, "can_run": False, "src": None, "dst": None, "restore": []}
    if not isinstance(raw, dict):
        if os.path.lexists(state_path()):
            R["unreadable"] = True  # (looked at by everything that would write: see `save`)
        return R
    if isinstance(raw.get("on"), bool):
        R["on"] = raw["on"]
    R["can_run"] = raw.get("can_run") is True
    for side, blank in (("src", _blank_src), ("dst", _blank_dst)):
        if isinstance(raw.get(side), dict):
            b = blank()
            b.update({k: v for k, v in raw[side].items() if k in b})
            for key, val in blank().items():
                if isinstance(val, dict) and not isinstance(b[key], dict):
                    b[key] = val
            R[side] = b
    if isinstance(raw.get("restore"), list):
        R["restore"] = [r for r in raw["restore"] if isinstance(r, dict) and isinstance(r.get("id"), str)
                        and UID_RE.match(r["id"])][:MAX_RESTORE]
    return R


def save(R: dict) -> None:
    if R.get("unreadable"):
        raise Unreadable("the routines file cannot be read; it is left as it is")
    fio.write_json(state_path(), R)


def _writable(R: dict) -> dict:
    """`R`, for a caller that is about to change things by it: refused when the file could not be read."""
    if R.get("unreadable"):
        raise Unreadable("the routines file cannot be read; nothing is changed by it")
    return R


def exists() -> bool:
    return state_path().exists()


def is_on(R: dict, role: str) -> bool:
    """The item's switch on this computer: off until switched on where the routines come from, on (keep copies)
    on a linked computer until switched off there."""
    return R["on"] is True if role == "source" else R["on"] is not False


# ---- text, ids -------------------------------------------------------------------------------------------------------
def routine_id(twin_id: str, job_id: str) -> str:
    return "j-" + hashlib.sha256(f"grok-bot-twin routine\n{twin_id}\n{job_id}".encode("utf-8")).hexdigest()[:16]


def _clean_schedule(raw: Any, shown: Any = None) -> Optional[dict]:
    """A repeating schedule in Hermes' own shape, or None (one-off, unknown or not well formed)."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind == "cron":
        expr = raw.get("expr")
        if not isinstance(expr, str) or not CRON_RE.match(expr.strip()):
            return None
        out = {"kind": "cron", "expr": expr.strip()}
    elif kind == "interval":
        minutes = raw.get("minutes")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 0 < minutes <= MAX_MINUTES:
            return None
        out = {"kind": "interval", "minutes": minutes}
    else:
        return None
    display = raw.get("display") if isinstance(raw.get("display"), str) else shown
    out["display"] = safe.clean_name(display if isinstance(display, str) else "", 80) or \
        (out.get("expr") or f"every {out.get('minutes')}m")
    return out


def _clean_skills(raw: Any) -> list:
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for s in raw if isinstance(raw, list) else []:
        if isinstance(s, str) and SKILL_RE.match(s) and ".." not in s and s not in out:
            out.append(s)
    return out[:8]


def _clean_zone(raw: Any) -> Optional[str]:
    return raw if isinstance(raw, str) and ZONE_RE.match(raw) else None


def _spec_of_job(job: dict) -> Optional[dict]:
    """What of a Hermes job may be a routine on another computer, or None when it must stay where it is: it
    runs a script, needs a folder or a program of this computer, is a one-off, or is finished."""
    if any(job.get(k) for k in ("script", "no_agent", "monitor_script", "monitor_url", "workdir", "interpreter",
                                "context_from")):
        return None
    if job.get("state") in ("completed", "error") and not job.get("enabled", True):
        return None
    schedule = _clean_schedule(job.get("schedule"), job.get("schedule_display"))
    prompt = hs.shown(job.get("prompt"), PROMPT_CAP) if isinstance(job.get("prompt"), str) else ""
    skills = _clean_skills(job.get("skills") or job.get("skill"))
    if schedule is None or not (prompt or skills):
        return None
    name = safe.clean_name(job.get("name"), NAME_CAP) or "Routine"
    return {"name": name, "prompt": prompt, "schedule": schedule, "skills": skills,
            "timezone": _clean_zone(job.get("timezone"))}


SPEC_KEYS = ("name", "prompt", "schedule", "skills", "timezone")


# ---- the sharing computer: its list ---------------------------------------------------------------------------------------
def _twin_home(profile: Any) -> Optional[Path]:
    try:
        home = fp.profile_dir(profile)
    except Exception:
        return None
    return home if fio.is_real_dir(home) else None


def _name_of(st: dict, mid: Any) -> str:
    me = fleet.this_machine(st)
    if mid == me["id"]:
        return me["name"]
    for m in _machines(st):
        if m["id"] == mid:
            return m["name"]
    return "a computer that is no longer linked"


def _machines(st: dict) -> list:
    """The computers of the fleet as this one knows them: [{"id", "name", "this"}], the sharing one first."""
    me = fleet.this_machine(st)
    if st.get("link"):
        out, seen = [], set()
        for m in st["link"].get("fleet") or []:
            m = fleet.clean_machine(m)
            if m and m["id"] not in seen:
                seen.add(m["id"])
                out.append({"id": m["id"], "name": m["name"], "this": m["id"] == me["id"]})
        if me["id"] not in seen:
            out.append({"id": me["id"], "name": me["name"], "this": True})
        return out
    if st.get("share"):
        return [{"id": m["id"], "name": m["name"], "this": m["id"] == me["id"]} for m in fleet.fleet_members(st)]
    return [{"id": me["id"], "name": me["name"], "this": True}]


def _scan(R: dict, st: dict) -> None:
    """Bring the list in line with the routines of this computer's twins. Called with the fleet lock held."""
    src = R["src"]
    me = fleet.this_machine(st)["id"]
    now = now_iso()
    seen, local_only, unread = set(), 0, set()
    twins = st["share"]["list"].get("twins") or {}
    for gone in [t for t in list(src["stamps"]) + list(src["local"]) if t not in twins]:
        src["stamps"].pop(gone, None)
        src["local"].pop(gone, None)
    for tid, t in sorted(twins.items()):
        home = _twin_home(t.get("profile")) if isinstance(t, dict) else None
        if home is None:
            unread.add(tid)  # its bot's folder is not there right now: nothing of this twin is added or removed
            continue
        stamp = list(rj.stamp(home))
        if stamp and src["stamps"].get(tid) == stamp:
            # the job file is as it was at the last look: what is known of this twin's routines stands
            seen.update(rid for rid, it in src["items"].items() if it.get("twin") == tid)
            local_only += int(src["local"].get(tid) or 0)
            continue
        jobs = rj.load(home)
        if jobs is None:
            unread.add(tid)  # cannot be read right now: nothing of this twin is added or removed
            continue
        src["stamps"][tid] = stamp
        mine = 0
        for job in jobs[:rj.MAX_JOBS]:
            jid = job.get("id")
            if not isinstance(jid, str) or not jid:
                continue
            spec = _spec_of_job(job)
            if spec is None:
                mine += 1
                continue
            rid = routine_id(tid, jid)
            if len(seen) >= MAX_ROUTINES and rid not in src["items"]:
                continue
            seen.add(rid)
            it = src["items"].get(rid)
            if not isinstance(it, dict):
                src["items"][rid] = {**spec, "twin": tid, "job": jid, "on": rj.runnable(job), "version": 1,
                                     "updated_at": now, "runs_on": me, "holder": me, "epoch": 0, "moved_at": 0}
                src["removed"].pop(rid, None)
                continue
            changed = any(it.get(k) != spec[k] for k in SPEC_KEYS)
            if it.get("holder") == me and it.get("runs_on") == me:
                on = rj.runnable(job)  # here the Hermes job itself is the owner's switch
                changed = changed or on != it.get("on")
                it["on"] = on
            if changed:
                it.update(spec, version=int(it.get("version") or 1) + 1, updated_at=now)
        src["local"][tid] = mine
        local_only += mine
    for rid in [r for r, it in src["items"].items() if r not in seen and it.get("twin") not in unread]:
        del src["items"][rid]
        src["removed"][rid] = now
    src["local_only"] = local_only
    cut = iso(now_dt() - timedelta(days=REMOVED_DAYS))
    for rid in [r for r, at in src["removed"].items() if not isinstance(at, str) or at < cut]:
        del src["removed"][rid]
    while len(src["removed"]) > MAX_REMOVED:
        del src["removed"][min(src["removed"], key=lambda r: src["removed"][r])]


def _counter_of(rev: Any) -> int:
    try:
        return int(str(rev).split("-", 1)[0])
    except ValueError:
        return -1


def _own_job(st: dict, it: dict, seen: Optional[dict] = None) -> tuple:
    """(the bot's folder, the Hermes job, whether its file could be read) of a routine on the sharing computer;
    (None, None, True) when the bot is not there. `seen`: the files already read in this pass, by folder."""
    t = (st["share"]["list"].get("twins") or {}).get(it.get("twin"))
    home = _twin_home(t.get("profile")) if isinstance(t, dict) else None
    if home is None:
        return None, None, True
    if seen is not None and home in seen:
        jobs = seen[home]
    else:
        jobs = rj.load(home)
        if seen is not None:
            seen[home] = jobs
    return home, (rj.find(jobs, it.get("job")) if jobs is not None else None), jobs is not None


def _pause_own(st: dict, it: dict, reason: str, seen: Optional[dict] = None) -> bool:
    """Pause this computer's own job of a routine and LOOK that it is paused. True only when the file says so.
    Nothing is written (and Hermes' lock is not taken) when the job is paused already."""
    home, job, readable = _own_job(st, it, seen)
    if home is None:
        return True  # the twin's bot is gone from this computer: nothing can run here
    if job is None:
        return readable  # readable and the job is not in it: nothing to run
    if not rj.runnable(job) and (not rj.ours(job) or job.get("paused_reason") == reason):
        return True
    if seen is not None:
        seen.pop(home, None)
    try:
        def change(jobs: list) -> tuple:
            j = rj.find(jobs, it.get("job"))
            if j is None or not rj.runnable(j):
                if j is not None and rj.ours(j) and j.get("paused_reason") != reason:
                    j["paused_reason"] = reason
                    return True, None
                return False, None
            return rj.set_paused(j, reason), None
        rj.edit(home, change)
    except OSError:
        return False
    _home, job, readable = _own_job(st, it)
    return readable and (job is None or not rj.runnable(job))


HOME_PAUSED = "Paused where it ran before it came back to this computer; resume it in Hermes to run it."
HERE_PAUSED = "Paused when it moved to this computer; resume it in Hermes here to run it."


def _resume_own(st: dict, it: dict, seen: Optional[dict] = None) -> None:
    """A routine is this computer's to run again: its job is switched on, if it was this plugin that paused it.
    One the owner had paused where it ran comes back paused, as the owner's own pause."""
    home, job, _readable = _own_job(st, it, seen)
    if home is None or job is None or not rj.ours(job):
        return
    on = bool(it.get("on"))
    if seen is not None:
        seen.pop(home, None)

    def change(jobs: list) -> tuple:
        j = rj.find(jobs, it.get("job"))
        if j is None or not rj.ours(j):
            return False, None
        if on:
            return rj.set_running(j), None
        j["paused_reason"] = HOME_PAUSED
        return True, None
    try:
        rj.edit(home, change)
    except OSError:
        log.debug("grok-bot-twin: a routine could not be switched on again here", exc_info=True)


def _peer_ok(src: dict, st: dict, mid: str) -> bool:
    """A linked computer that keeps copies and lets routines run."""
    p = src["peers"].get(mid)
    return mid in (st["share"].get("followers") or {}) and isinstance(p, dict) and p.get("on") is True \
        and p.get("can_run") is True


def _progress(R: dict, st: dict) -> None:
    """Carry every handover as far as it can go, and keep this computer's own jobs in line. The one place that
    lets a routine start somewhere: `holder` changes only here (and in a forced move). Fleet lock held."""
    src = R["src"]
    me = fleet.this_machine(st)["id"]
    followers = st["share"].get("followers") or {}
    nxt = int(src.get("counter") or 0) + 1  # the number the next published list will carry
    seen: dict = {}  # the job files read in this pass
    for rid, it in src["items"].items():
        to, holder = it.get("runs_on"), it.get("holder")
        if to != me and (not R["on"] or not _peer_ok(src, st, to)):
            # where it should run no longer takes routines (unlinked, switched off there, or the item is off):
            # it comes home, by the same handover
            it.update(runs_on=me, epoch=int(it.get("epoch") or 0) + 1, moved_at=nxt)
            to = me
        if holder != to:
            if holder == me:
                if _pause_own(st, it, f"{rj.MARK}runs on {_name_of(st, to)}", seen):
                    it["holder"] = to
            else:
                p = src["peers"].get(holder) if isinstance(src["peers"].get(holder), dict) else {}
                confirmed = p.get("left") is True or (
                    holder in followers and _counter_of(p.get("applied")) >= int(it.get("moved_at") or 0)
                    and rid not in (p.get("running") or []))
                if confirmed:
                    it["holder"] = to
            if it.get("holder") == to:
                it["granted_at"] = nxt
        if it.get("holder") == me and to == me:
            _resume_own(st, it, seen)
            _home, job, _readable = _own_job(st, it, seen)
            if job is not None:
                it["on"] = rj.runnable(job)
        else:
            # not this computer's to run: its own job stays paused, also after someone resumed it by hand
            if not _pause_own(st, it, f"{rj.MARK}runs on {_name_of(st, to)}", seen) and it.get("holder") != me:
                log.debug("grok-bot-twin: a routine that runs elsewhere could not be paused here")
            p = src["peers"].get(to) if it.get("holder") == to else None
            since = max(int(it.get("granted_at") or 0), int(it.get("moved_at") or 0))
            if isinstance(p, dict) and _counter_of(p.get("applied")) >= since:
                # where it runs, the Hermes job itself is the owner's switch, and that computer says which it
                # is: switched on there, or paused there BY ITS OWNER (a pause of the plugin's own, or a job
                # that could not be switched on yet, is neither, and changes nothing here)
                if rid in (p.get("running") or []):
                    it["on"] = True
                elif rid in (p.get("paused") or []):
                    it["on"] = False
    for mid in [m for m in src["peers"] if m not in followers and not any(
            it.get("holder") == m for it in src["items"].values())]:
        del src["peers"][mid]


def _wire_item(rid: str, it: dict) -> dict:
    """THE ALLOWLIST of what a linked computer is sent about one routine: named fields only."""
    return {"id": rid, "twin": it["twin"], "name": it["name"], "prompt": it["prompt"],
            "schedule": {k: it["schedule"][k] for k in ("kind", "expr", "minutes", "display") if k in it["schedule"]},
            "skills": list(it.get("skills") or []), "timezone": it.get("timezone"), "on": bool(it.get("on")),
            "version": int(it.get("version") or 1), "updated_at": it.get("updated_at"),
            "runs_on": it["runs_on"], "go": it.get("holder") == it["runs_on"], "epoch": int(it.get("epoch") or 0)}


def _wire(R: dict, st: dict) -> dict:
    src = R["src"]
    on = R["on"] is True
    items, size = [], 0
    for rid, it in sorted(src["items"].items())[:MAX_ROUTINES] if on else []:
        item = _wire_item(rid, it)
        size += len(json.dumps(item))  # (as it is sent: every character outside ASCII counts six)
        if size > WIRE_BUDGET:
            break  # the rest is not listed: a linked computer keeps such a copy paused
        items.append(item)
    removed = sorted(set(src["removed"]) | (set() if on else set(src["items"])))[:MAX_REMOVED + MAX_ROUTINES]
    me = fleet.this_machine(st)["id"]
    can = {me: True}
    for mid in sorted(st["share"].get("followers") or {}):
        can[mid] = _peer_ok(src, st, mid)
    return {"on": on, "list": items, "removed": removed, "can_run": can}


def _stamp_rev(R: dict, st: dict) -> bool:
    """Give the list a new rev when what would be sent changed. True when it did."""
    src = R["src"]
    digest = hashlib.sha256(json.dumps(_wire(R, st), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    if digest == src.get("digest") and fleet.REV_RE.match(str(src.get("rev"))):
        return False
    src["counter"] = int(src.get("counter") or 0) + 1
    src["digest"] = digest
    src["rev"] = f"{src['counter']}-{digest[:8]}"
    return True


def _settled_source(R: dict, st: dict) -> bool:
    """Nothing for the sharing computer's look to do: every routine is held where it should run, the same twins
    are shared as at the last look, and none of their job files has another date or size."""
    src = R["src"]
    if src is None or not fleet.REV_RE.match(str(src.get("rev"))) or R["on"] is not True:
        return False
    me = fleet.this_machine(st)["id"]
    followers = st["share"].get("followers") or {}
    for it in src["items"].values():
        to = it.get("runs_on")
        if it.get("holder") != to or (to != me and not _peer_ok(src, st, to)):
            return False
    if any(m not in followers for m in src["peers"]):
        return False
    twins = st["share"]["list"].get("twins") or {}
    if set(src["stamps"]) != set(twins):
        return False
    for tid, t in twins.items():
        home = _twin_home(t.get("profile")) if isinstance(t, dict) else None
        if home is None or list(rj.stamp(home)) != src["stamps"].get(tid) or not src["stamps"].get(tid):
            return False
    return True


def tend_source(force: bool = False) -> bool:
    """One pass on the sharing computer: read the twins' routines, carry handovers on, publish. True when the
    list changed. Reads a few file dates when nothing changed."""
    st = fleet.load()
    if st.get("link") or not st.get("share"):
        return False
    R = load()
    if R.get("unreadable") or (R["src"] is None and R["on"] is not True):
        return False  # never switched on (or not readable right now): nothing is read, nothing is changed
    if not force and _settled_source(R, st):
        return False  # a few file dates were looked at: nothing changed, nothing is on its way
    with fleet.locked():
        st = fleet.load()
        if st.get("link") or not st.get("share"):
            return False  # (also with sharing off and nobody linked any more: what ran elsewhere still comes home)
        R = _writable(load())
        before = json.dumps(R, sort_keys=True)
        if R["src"] is None:
            R["src"] = _new_src()
        if R["on"] is True:
            _scan(R, st)
        _progress(R, st)
        _progress(R, st)  # (a routine that came home in the first pass is switched on in the second)
        changed = _stamp_rev(R, st)
        if changed or json.dumps(R, sort_keys=True) != before:
            save(R)
    if changed:
        fleet.announce()
    return changed


def _clean_report(raw: Any) -> Optional[dict]:
    """What a linked computer says about itself, reduced to switches, a rev and ids."""
    if not isinstance(raw, dict) or raw.get("v") != V:
        return None
    applied = raw.get("applied") if isinstance(raw.get("applied"), str) and fleet.REV_RE.match(raw["applied"]) else None
    running = sorted({r for r in (raw.get("running") if isinstance(raw.get("running"), list) else [])[:MAX_ROUTINES]
                      if isinstance(r, str) and ID_RE.match(r)})
    paused = sorted({r for r in (raw.get("paused") if isinstance(raw.get("paused"), list) else [])[:MAX_ROUTINES]
                     if isinstance(r, str) and ID_RE.match(r)} - set(running))
    out = {"on": raw.get("on") is True, "can_run": raw.get("can_run") is True, "applied": applied,
           "running": running, "paused": paused}
    mv = raw.get("move")
    if isinstance(mv, dict) and isinstance(mv.get("id"), str) and ID_RE.match(mv["id"]) \
            and isinstance(mv.get("to"), str) and fleet.MACHINE_ID_RE.match(mv["to"]):
        out["move"] = {"id": mv["id"], "to": mv["to"]}
    return out


def heard(machine_id: str, raw: Any) -> None:
    """A linked computer's signed request carried the routines key: note what it reports, carry handovers on.
    Writes only when something changed. Never raises (a sync must not fail over routines)."""
    try:
        rep = _clean_report(raw)
        if rep is None:
            return
        move = rep.pop("move", None)
        R = _writable(load())
        old = (R["src"] or {}).get("peers", {}).get(machine_id)
        if R["src"] is not None and isinstance(old, dict) and move is None \
                and all(old.get(k) == rep[k] for k in rep) and not old.get("left"):
            return
        if R["src"] is None and R["on"] is not True:
            return
        with fleet.locked():
            st = fleet.load()
            if fleet.role(st) != "source" or machine_id not in (st["share"].get("followers") or {}):
                return
            R = _writable(load())
            if R["src"] is None:
                R["src"] = _new_src()
            R["src"]["peers"][machine_id] = {**rep, "at": now_iso()}
            # a linked computer may ask for a routine to run on ITSELF or back on this computer, a few times
            # a minute; where else it runs is said on this computer or on the one that is to run it
            if move is not None and move["to"] in (machine_id, fleet.this_machine(st)["id"]) \
                    and _may_ask(machine_id):
                try:
                    _move(R, st, move["id"], move["to"])
                except EngineError:
                    pass  # asked for something that cannot be: nothing changes, its page says why
            _progress(R, st)
            _progress(R, st)
            changed = _stamp_rev(R, st)
            save(R)
        if changed:
            fleet.announce()
    except Exception:
        log.debug("grok-bot-twin: a routines report could not be taken in", exc_info=True)


_asks: dict = {}  # machine id -> when it last asked for moves (monotonic), to cap how often one computer may


def _may_ask(machine_id: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _asks.get(machine_id, []) if now - t < 60]
    ok = len(recent) < 12
    if ok:
        recent.append(now)
    _asks[machine_id] = recent
    if len(_asks) > 64:
        _asks.pop(next(iter(_asks)))
    return ok


def peer_left(machine_id: str) -> None:
    """A linked computer unlinked ITSELF (its own signed word): it paused its routines before it said so, so
    what it held may run elsewhere. (A computer removed from here says nothing: that needs the owner's word.)"""
    try:
        R = load()
        if R["src"] is None:
            return
        with fleet.locked():
            R = load()
            if R["src"] is None:
                return
            R["src"]["peers"][machine_id] = {"on": False, "can_run": False, "applied": None, "running": [],
                                             "left": True, "at": now_iso()}
            save(R)
        tend_source(force=True)  # now, not at the next look: what it ran starts on this computer again
    except Exception:
        log.debug("grok-bot-twin: a goodbye could not be noted for routines", exc_info=True)


def _asked(body: Any) -> Optional[dict]:
    rt = body.get("routines") if isinstance(body, dict) else None
    return rt if isinstance(rt, dict) and rt.get("v") == V else None


def tell(body: Any) -> dict:
    """The routines key for a signed "nothing changed" answer: {} for a computer that did not send the key (it
    is then answered exactly as before this existed); the rev, and the whole list when that computer holds
    another one."""
    try:
        rt = _asked(body)
        if rt is None:
            return {}
        R = load()
        if R["src"] is None:
            R["src"] = _blank_src()
        src = R["src"]
        out = {"v": V, "gen": src.get("gen"), "rev": src["rev"], "on": R["on"] is True}
        if rt.get("have") != src["rev"] or rt.get("gen") != src.get("gen"):
            st = fleet.load()
            if st.get("share"):
                out.update(_wire(R, st))
        return {"routines": out}
    except Exception:
        log.debug("grok-bot-twin: the routines answer could not be made", exc_info=True)
        return {}


def news(body: Any) -> Optional[dict]:
    """`tell` when that computer holds another list than the current one (its waiting request is then answered
    at once), else None."""
    out = tell(body)
    return out if "list" in (out.get("routines") or {}) else None


# ---- a linked computer -------------------------------------------------------------------------------------------------
def _clean_item(raw: Any) -> Optional[dict]:
    """One routine as it arrived, type-checked, capped and cleaned again; None when it is not one."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not ID_RE.match(raw["id"]):
        return None
    if not isinstance(raw.get("twin"), str) or not REF_RE.match(raw["twin"]):
        return None
    schedule = _clean_schedule(raw.get("schedule"))
    prompt = hs.shown(raw.get("prompt"), PROMPT_CAP) if isinstance(raw.get("prompt"), str) else ""
    skills = _clean_skills(raw.get("skills"))
    if schedule is None or not (prompt or skills):
        return None
    if not isinstance(raw.get("runs_on"), str) or not fleet.MACHINE_ID_RE.match(raw["runs_on"]):
        return None
    version, epoch = raw.get("version"), raw.get("epoch")
    return {"id": raw["id"], "twin": raw["twin"], "name": safe.clean_name(raw.get("name"), NAME_CAP) or "Routine",
            "prompt": prompt, "schedule": schedule, "skills": skills, "timezone": _clean_zone(raw.get("timezone")),
            "on": raw.get("on") is True,
            "version": version if isinstance(version, int) and not isinstance(version, bool) and 0 < version < 10 ** 9 else 1,
            "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), str)
            and len(raw["updated_at"]) <= 40 and parse_iso(raw["updated_at"]) else None,
            "runs_on": raw["runs_on"], "go": raw.get("go") is True,
            "epoch": epoch if isinstance(epoch, int) and not isinstance(epoch, bool) and 0 <= epoch < 10 ** 9 else 0}


def _clean_answer(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict) or raw.get("v") != V:
        return None
    rev = raw.get("rev")
    if not isinstance(rev, str) or not fleet.REV_RE.match(rev):
        return None
    gen = raw.get("gen") if isinstance(raw.get("gen"), str) and re.match(r"^[0-9a-f]{8}\Z", raw["gen"]) else None
    out: dict = {"gen": gen, "rev": rev, "on": raw.get("on") is True, "list": None}
    if isinstance(raw.get("list"), list):
        items = {}
        for x in raw["list"][:MAX_ROUTINES]:
            it = _clean_item(x)
            if it is not None:
                items[it["id"]] = it
        out["list"] = items
        out["removed"] = {r for r in (raw.get("removed") if isinstance(raw.get("removed"), list) else [])
                          [:MAX_REMOVED + MAX_ROUTINES] if isinstance(r, str) and ID_RE.match(r)}
        can = raw.get("can_run") if isinstance(raw.get("can_run"), dict) else {}
        out["can_run"] = {m: v is True for m, v in list(can.items())[:fleet.MAX_MACHINES + 1]
                          if isinstance(m, str) and fleet.MACHINE_ID_RE.match(m)}
    return out


def _held_twins(st: dict) -> dict:
    """twin id -> the folder of its Hermes bot on this linked computer."""
    out = {}
    for tid, e in ((st.get("link") or {}).get("twins") or {}).items():
        if isinstance(e, dict) and not e.get("making") and e.get("profile"):
            home = _twin_home(e["profile"])
            if home is not None:
                out[tid] = home
    return out


def _keep_restore(R: dict, what: str, job: dict, profile: str, twin_name: str) -> None:
    """Before a Hermes job is changed or removed here: keep it as it was."""
    name = safe.clean_name(job.get("name"), NAME_CAP) or "Routine"
    words = {"updated": "as it was before it changed", "removed": "as it was before it was removed"}[what]
    R["restore"].insert(0, {"id": "u-" + secrets.token_hex(6), "at": now_iso(), "what": what,
                            "label": f"{name} ({safe.clean_name(twin_name, 100) or 'a twin'}), {words}",
                            "profile": profile, "job": job})
    cut = iso(now_dt() - timedelta(days=RESTORE_DAYS))
    R["restore"] = [r for r in R["restore"] if isinstance(r.get("at"), str) and r["at"] >= cut][:MAX_RESTORE]


def _reason(st: dict, it: dict, me: str, R: dict) -> str:
    if it["runs_on"] != me:
        return f"{rj.MARK}runs on {_name_of(st, it['runs_on'])}"
    if not it["go"]:
        return f"{rj.MARK}starts here when the computer it ran on has stopped it"
    if not R["can_run"]:
        return f"{rj.MARK}waiting for \"Routines may run on this computer\""
    return f"{rj.MARK}paused; resume it in Hermes on this computer to run it"


LOCAL_ONLY_KEYS = ("script", "monitor_script", "monitor_url", "workdir", "context_from")


def _apply(R: dict, st: dict, may_start: bool = False) -> None:
    """Make this computer's Hermes jobs match the list it holds: a paused copy of every routine, switched on
    only where the list says this computer runs it. Idempotent. Called with the fleet lock held.

    `may_start`: only the moment a signed list has just arrived may switch a job ON. Every other look (the
    half-minute one, a switch on this computer) may pause, and when it finds a job that should be switched on
    it asks for the list again instead of believing the copy of it in this computer's own file."""
    dst = R["dst"]
    refetch = False
    me = fleet.this_machine(st)["id"]
    homes = _held_twins(st)
    keep = is_on(R, "follower") and dst.get("source_on") is not False
    by_twin: dict = {tid: [] for tid in homes}
    for rid, d in dst["items"].items():
        by_twin.setdefault(d.get("twin"), []).append(rid)
    twins_meta = (st.get("link") or {}).get("twins") or {}
    for tid, rids in by_twin.items():
        home = homes.get(tid)
        if home is None:
            for rid in rids:
                dst["items"][rid]["job"] = None  # the twin's bot is not on this computer (yet)
            continue
        if not rids and not any(j.get(rj.COPY_KEY) for j in (rj.load(home) or [])):
            continue  # nothing of this twin is copied here, and nothing was
        profile = home.name
        twin_name = (twins_meta.get(tid) or {}).get("name") or ""

        def change(jobs: list) -> tuple:
            changed = False
            for j in jobs:
                # a copy this plugin made that its notes no longer know (the notes were lost, say): if nothing
                # lists it, it is paused; if its routine is listed, it is taken up again below. Never left running
                if j.get(rj.COPY_KEY) and j.get(rj.COPY_KEY) not in rids and rj.runnable(j):
                    changed = rj.set_paused(j, f"{rj.MARK}no longer copied to this computer") or changed
            for rid in rids:
                d = dst["items"][rid]
                job = rj.find(jobs, d.get("job")) if d.get("job") else None
                if job is None:
                    job = next((j for j in jobs if j.get(rj.COPY_KEY) == rid and isinstance(j.get("id"), str)), None)
                    if job is not None:
                        d["job"] = job["id"]
                if d.get("gone") or not keep:
                    if job is not None:
                        if d.get("gone") == "removed" and keep:
                            _keep_restore(R, "removed", dict(job), profile, twin_name)
                        jobs.remove(job)
                        changed = True
                    d["job"] = None
                    continue
                spec = {**{k: d[k] for k in SPEC_KEYS}, "copy_of": rid}
                mine = d["runs_on"] == me and d["go"] and R["can_run"] and not d.get("unlisted")
                reason = _reason(st, d, me, R)
                if job is None:
                    job = rj.new_record(spec, reason)
                    jobs.append(job)
                    d["job"] = job["id"]
                    changed = True
                else:
                    if job.get(rj.COPY_KEY) != rid:
                        job[rj.COPY_KEY] = rid
                        changed = True
                    before = dict(job)
                    if rj.apply_spec(job, spec):
                        _keep_restore(R, "updated", before, profile, twin_name)
                        changed = True
                    for k in LOCAL_ONLY_KEYS:  # a copy is a prompt on a schedule, whatever was done to it here
                        if job.get(k):
                            job[k] = None
                            changed = True
                    if job.get("no_agent"):
                        job["no_agent"] = False
                        changed = True
                if mine:
                    # this computer runs it, and its Hermes job is the owner's switch from here on: a job the
                    # owner paused or resumed HERE is left alone. Only a pause this plugin made is lifted
                    if d["on"]:
                        if not rj.runnable(job) and rj.ours(job):
                            if may_start:
                                changed = rj.set_running(job) or changed
                            else:
                                nonlocal refetch
                                refetch = True
                    elif rj.ours(job):  # the routine was paused when it came here: that is the owner's pause now
                        job["paused_reason"] = HERE_PAUSED
                        changed = True
                elif rj.runnable(job):
                    changed = rj.set_paused(job, reason) or changed
                elif rj.ours(job) and job.get("paused_reason") != reason:
                    job["paused_reason"] = reason
                    changed = True
            return changed, None
        try:
            rj.edit(home, change)
            dst["stamps"][tid] = list(rj.stamp(home))
        except OSError:
            dst["stamps"][tid] = None  # (looked at again at the next look)
            log.debug("grok-bot-twin: a bot's routines could not be changed right now", exc_info=True)
    for rid in [r for r, d in dst["items"].items() if (d.get("gone") or not keep) and not d.get("job")]:
        del dst["items"][rid]
    if refetch:
        dst["rev"] = None  # asked for again: the list that arrives is what may switch the job on


def _running(R: dict, st: dict, paused: Optional[list] = None) -> list:
    """The routines whose Hermes job is switched on on this computer right now, read from the files. A file
    that cannot be read counts as "could be running" for every routine in it. `paused`, when given, receives
    the routines this computer is to run whose job its OWNER paused here (not a pause of the plugin's)."""
    dst = R["dst"] or {}
    homes = _held_twins(st)
    me = fleet.this_machine(st)["id"]
    cache: dict = {}
    out = []
    for rid, d in (dst.get("items") or {}).items():
        home = homes.get(d.get("twin"))
        if home is None or not d.get("job"):
            continue
        if home not in cache:
            cache[home] = rj.load(home)
        jobs = cache[home]
        job = rj.find(jobs, d["job"]) if jobs is not None else None
        if jobs is None or rj.runnable(job):
            out.append(rid)
        elif paused is not None and job is not None and not rj.ours(job) and d.get("runs_on") == me \
                and d.get("go") and not d.get("unlisted") and not d.get("gone"):
            paused.append(rid)
    for home in homes.values():  # (a copy that carries its routine's id and is switched on counts, known or not)
        if home not in cache:
            cache[home] = rj.load(home)
        for j in cache[home] or []:
            rid = j.get(rj.COPY_KEY)
            if isinstance(rid, str) and ID_RE.match(rid) and rj.runnable(j) and rid not in out:
                out.append(rid)
    return sorted(out)


_lists: list = []  # (when, rev) of the whole lists that arrived lately: request_key's guard against a tight circle
_quiet = [0.0]     # until when (monotonic) this computer leaves the key out, after an answer it could not use


def request_key(st: dict) -> Optional[dict]:
    """What a linked computer adds to its signed sync request. Reads only."""
    try:
        if not st.get("link"):
            return None
        if load().get("unreadable"):
            return None  # this computer's routines file cannot be read right now: nothing is said by it
        now = time.monotonic()
        if now < _quiet[0]:
            return None  # what came back last could not be used: not asked for again for a minute
        if len(_lists) >= 3 and len({rev for _t, rev in _lists[-3:]}) == 1 and now - _lists[-3][0] < 20:
            return None  # the same list arrived three times in a row: it is not asked for again for a while
        R = load()
        dst = R["dst"] or _blank_dst()
        paused: list = []
        running = _running(R, st, paused)
        out = {"v": V, "gen": dst.get("gen"), "have": dst.get("rev"), "on": is_on(R, "follower"),
               "can_run": R["can_run"], "applied": dst.get("rev"), "running": running, "paused": sorted(paused)}
        mv = dst.get("move")
        if isinstance(mv, dict) and isinstance(mv.get("at"), (int, float)) and time.time() - mv["at"] < MOVE_ASK_S:
            out["move"] = {"id": mv.get("id"), "to": mv.get("to")}
        return out
    except Exception:
        log.debug("grok-bot-twin: the routines report could not be made", exc_info=True)
        return None


def heard_answer(obj: dict) -> None:
    """A signed "nothing changed" answer of the sharing computer arrived on this linked computer: take in its
    routines key (or note that it has none: an older build there). Never raises."""
    try:
        if obj.get("changed") is True:
            return  # (the twin list answer never carries it)
        ans = _clean_answer(obj.get("routines")) if "routines" in obj else None
        if ans is None and "routines" in obj:
            # a key that is not what this build understands (a newer or a broken sharing computer): the sharing
            # computer would answer every waiting request at once, so this computer stops asking for a minute
            _quiet[0] = time.monotonic() + 60
        R = _writable(load())
        dst = R["dst"]
        if ans is None:
            if dst is None or dst.get("supported") is not False:
                with fleet.locked():
                    R = _writable(load())
                    R["dst"] = R["dst"] or _blank_dst()
                    R["dst"]["supported"] = False
                    save(R)
            return
        if dst is not None and dst.get("supported") is True and ans["list"] is None and dst.get("rev") == ans["rev"]:
            return
        with fleet.locked():
            st = fleet.load()
            if not st.get("link"):
                return
            R = _writable(load())
            dst = R["dst"] = R["dst"] or _blank_dst()
            dst["supported"] = True
            dst["heard_at"] = now_iso()
            if ans["list"] is not None and ans["gen"] == dst.get("gen") and dst.get("rev") \
                    and _counter_of(ans["rev"]) < _counter_of(dst["rev"]):
                # an older list of the same sharing computer, arriving late (two requests were under way at
                # once, say): what it says is no longer so. A routine it would switch on may have moved since
                save(R)
                return
            if ans["list"] is not None:
                dst["gen"] = ans["gen"]
                dst["source_on"] = ans["on"]
                dst["can_run"] = ans["can_run"]
                old = dst["items"]
                new = {}
                for rid, it in ans["list"].items():
                    new[rid] = {**it, "job": (old.get(rid) or {}).get("job")}
                for rid, d in old.items():
                    if rid in new:
                        continue
                    if rid in ans["removed"] or not ans["on"]:
                        new[rid] = {**d, "gone": "removed" if ans["on"] else "off"}
                    else:  # merely missing from the list: kept, and never switched on
                        new[rid] = {**d, "unlisted": True}
                dst["items"] = new
                mv = dst.get("move")
                if isinstance(mv, dict) and (new.get(mv.get("id")) or {}).get("runs_on") == mv.get("to"):
                    dst["move"] = None
                try:
                    _apply(R, st, may_start=True)
                except Exception:  # (the rev is kept all the same: what really runs here is read from the files)
                    log.debug("grok-bot-twin: the routines list could not be applied in full", exc_info=True)
                dst["rev"] = ans["rev"]
                _lists.append((time.monotonic(), ans["rev"]))
                del _lists[:-8]
            save(R)
    except Exception:
        # (the list was not taken in, so the next request would ask for it again at once: wait half a minute)
        _quiet[0] = time.monotonic() + 30
        log.debug("grok-bot-twin: the routines of the sharing computer could not be taken in", exc_info=True)


def tend_linked() -> None:
    """A linked computer's own look, between answers: its switches changed, a copy was edited or resumed by
    hand, a twin's bot arrived. Reads a few file dates when nothing changed."""
    st = fleet.load()
    if not st.get("link"):
        return
    R = load()
    dst = R["dst"]
    if dst is None or not dst.get("items"):
        return
    homes = _held_twins(st)
    flags = [is_on(R, "follower"), R["can_run"]]
    if dst["stamps"].get("flags") == flags and all(
            dst["stamps"].get(tid) == list(rj.stamp(home)) for tid, home in homes.items()
            if any(d.get("twin") == tid for d in dst["items"].values())):
        return
    with fleet.locked():
        st = fleet.load()
        if not st.get("link"):
            return
        R = _writable(load())
        if R["dst"] is None:
            return
        _apply(R, st)
        R["dst"]["stamps"]["flags"] = [is_on(R, "follower"), R["can_run"]]
        save(R)
    if R["dst"].get("rev") is None:
        _after_change("follower")  # a job waits to be switched on: the list is asked for again now


def link_dropped(st: dict) -> bool:
    """This computer stops following: every copy it holds is paused first (its bots are about to be moved aside
    or left behind as ordinary ones, and none may go on running by itself), "routines may run on this computer"
    is off again (a later link starts with it off, as a first one does), and what was known of the list is
    forgotten. True only when every copy was SEEN paused in its file: the goodbye that follows lets what this
    computer ran start elsewhere. Called with the fleet lock held, before the twins are taken away."""
    stopped = True
    try:
        R = load()
        known = {d.get("job") for d in ((R["dst"] or {}).get("items") or {}).values() if d.get("job")}
        for home in _held_twins(st).values():
            def copy(j: dict) -> bool:
                return bool(j.get(rj.COPY_KEY)) or j.get("id") in known
            jobs = rj.load(home)
            if jobs is not None and not any(copy(j) and (rj.runnable(j) or rj.ours(j)) for j in jobs):
                continue  # nothing of this bot is a copy that could run

            def change(jobs: list) -> tuple:
                changed = False
                for j in jobs:
                    if copy(j) and (rj.runnable(j) or rj.ours(j)):
                        changed = rj.set_paused(j, f"{rj.MARK}this computer was unlinked") or changed
                return changed, None
            try:
                rj.edit(home, change)
            except OSError:
                log.debug("grok-bot-twin: a routine could not be paused at the unlink", exc_info=True)
            after = rj.load(home)
            if after is None or any(copy(j) and rj.runnable(j) for j in after):
                stopped = False
        if not R.get("unreadable") and (R["dst"] is not None or R["can_run"]):
            R["dst"] = None
            R["can_run"] = False
            save(R)
    except Exception:
        stopped = False
        log.debug("grok-bot-twin: routines could not be put away at the unlink", exc_info=True)
    return stopped


# ---- what the owner does ------------------------------------------------------------------------------------------------
def _find_machine(st: dict, ref: Any) -> Optional[dict]:
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    ms = _machines(st)
    if ref.lower() in ("here", "this", "this computer"):
        return next((m for m in ms if m["this"]), None)
    return next((m for m in ms if m["id"] == ref), None) or \
        next((m for m in ms if m["name"].lower() == ref.lower()), None)


def _move(R: dict, st: dict, rid: Any, to: Any, force: bool = False) -> None:
    """The owner says where a routine runs (on the sharing computer; fleet lock held). Only `runs_on` changes
    here: the routine starts there when its holder has stopped it (`_progress`)."""
    src = R["src"]
    me = fleet.this_machine(st)["id"]
    it = src["items"].get(rid) if isinstance(rid, str) else None
    if not isinstance(it, dict):
        raise RoutinesError("unknown_routine")
    if R["on"] is not True:
        raise RoutinesError("routines_off")
    followers = st["share"].get("followers") or {}
    if to != me and to not in followers:
        raise RoutinesError("unknown_machine")
    if to != me:
        name = _name_of(st, to)
        if not _peer_ok(src, st, to):
            raise RoutinesError("cannot_run_there", name=name)
        seen = parse_iso((followers.get(to) or {}).get("last_seen_at"))
        if seen is None or (now_dt() - seen).total_seconds() > fleet.OFFLINE_AFTER_S:
            raise RoutinesError("not_reachable", name=name)
    holder = it.get("holder")
    if force:
        # the owner's own word that a computer which is no longer linked does not run it any more
        if to != me or holder == me or holder in followers:
            raise RoutinesError("still_running_there", name=_name_of(st, holder))
        it.update(runs_on=me, holder=me, epoch=int(it.get("epoch") or 0) + 1,
                  moved_at=int(src.get("counter") or 0) + 1)
        return
    if it.get("runs_on") == to:
        return
    nxt = int(src.get("counter") or 0) + 1
    it.update(runs_on=to, epoch=int(it.get("epoch") or 0) + 1, moved_at=nxt)
    if holder == to:
        it["granted_at"] = nxt  # (it never left: what that computer reports counts from this list on)


def _mine(R: dict) -> dict:
    """`R` for something the owner asked for: said in plain words when the file cannot be read right now."""
    if R.get("unreadable"):
        raise RoutinesError("busy")
    return R


class RoutinesMixin:
    """The local API of the routines item (the Twins page and `hermes twin routines` call these)."""

    def routines(self) -> dict:
        st = fleet.load()
        return view(st, load())

    def routines_switch(self, on: Any = None, can_run: Any = None) -> dict:
        if on is not None and not isinstance(on, bool) or can_run is not None and not isinstance(can_run, bool):
            raise EngineError("bad_input", "That request was not understood.")
        with fleet.locked():
            st = fleet.load()
            role = fleet.role(st)
            if role == "none":
                raise RoutinesError("not_linked")
            R = _mine(load())
            if on is not None:
                R["on"] = on
            if can_run is not None and role == "follower":
                R["can_run"] = can_run
            if role == "follower" and R["dst"] is not None:
                if on is True:
                    R["dst"]["rev"] = None  # the copies are fetched anew
                _apply(R, st)
            save(R)
        _after_change(role)
        return self.routines()

    def routines_move(self, rid: Any, to: Any, force: Any = False) -> dict:
        st = fleet.load()
        role = fleet.role(st)
        if role == "none":
            raise RoutinesError("not_linked")
        m = _find_machine(st, to)
        if m is None:
            raise RoutinesError("unknown_machine")
        rid = _find_routine(st, load(), rid)
        if role == "source":
            with fleet.locked():
                st = fleet.load()
                R = _mine(load())
                if R["src"] is None:
                    raise RoutinesError("routines_off")
                _move(R, st, rid, m["id"], force is True)
                _progress(R, st)
                _progress(R, st)
                changed = _stamp_rev(R, st)
                save(R)
            if changed:
                fleet.announce()
            return self.routines()
        # a linked computer asks the sharing one (which decides), in its next signed request, sent now
        with fleet.locked():
            R = _mine(load())
            dst = R["dst"]
            if dst is None or rid not in dst["items"]:
                raise RoutinesError("unknown_routine")
            if m["this"] and not R["can_run"]:
                raise RoutinesError("cannot_run_there", name=m["name"])
            if not m["this"] and m["id"] != (st["link"].get("source") or {}).get("id"):
                raise RoutinesError("ask_there", name=m["name"])
            before = dst.get("rev")
            dst["move"] = {"id": rid, "to": m["id"], "at": time.time()}
            save(R)
        _after_change(role)
        end = time.monotonic() + 8.0
        while time.monotonic() < end:  # the answer normally holds the new picture already
            time.sleep(0.25)
            now = load()["dst"] or {}
            if now.get("rev") != before or now.get("move") is None:
                break
        return self.routines()

    def routines_restore(self, uid: Any) -> dict:
        with fleet.locked():
            R = _mine(load())
            point = next((r for r in R["restore"] if r.get("id") == uid), None) if isinstance(uid, str) else None
            if point is None or not isinstance(point.get("job"), dict):
                raise RoutinesError("unknown_restore")
            home = _twin_home(point.get("profile"))
            if home is None:
                raise RoutinesError("unknown_restore")
            job = dict(point["job"])
            # put back as a routine of its own, paused: it no longer follows the other computer, and it starts
            # only when the owner resumes it in Hermes
            job["id"] = secrets.token_hex(6)
            job.pop(rj.COPY_KEY, None)
            job.update(enabled=False, state="paused", paused_at=now_iso(), next_run_at=None,
                       paused_reason="Put back from a restore point of Grok Bot Twin; resume it in Hermes to run it.")
            try:
                rj.edit(home, lambda jobs: (jobs.append(job) is None, None))
            except OSError:
                raise RoutinesError("busy") from None
            R["restore"] = [r for r in R["restore"] if r.get("id") != uid]
            save(R)
        return self.routines()


def _find_routine(st: dict, R: dict, ref: Any) -> Any:
    """A routine by id, or by its name (the terminal): the id, or the reference itself when nothing fits."""
    if not isinstance(ref, str):
        return ref
    items = ((R["src"] or {}).get("items") if fleet.role(st) == "source" else (R["dst"] or {}).get("items")) or {}
    if ref in items:
        return ref
    hits = [rid for rid, it in items.items() if str(it.get("name", "")).lower() == ref.strip().lower()]
    return hits[0] if len(hits) == 1 else ref


def _after_change(role: str) -> None:
    """A switch or a wish changed on this computer: act on it now, not at the next look."""
    try:
        if role == "source":
            tend_source(force=True)
        else:
            from . import fleet_follow
            fleet_follow.ask_again()
            fleet_follow.kick()
    except Exception:
        log.debug("grok-bot-twin: routines could not be tended after a change", exc_info=True)


# ---- the picture for the page and the terminal ----------------------------------------------------------------------
def _row(st: dict, rid: str, it: dict, twin_name: str, machines: list, can: dict, holder: Any, running: Optional[bool],
         is_source: bool) -> dict:
    me = next((m for m in machines if m["this"]), {"id": "", "name": "this computer"})
    by_id = {m["id"]: m for m in machines}
    to = it.get("runs_on")
    where = by_id.get(to) or {"id": to, "name": _name_of(st, to), "this": False}
    go = holder == to
    waiting = None
    if not go:
        hname = _name_of(st, holder) if is_source else "the computer it ran on"
        waiting = hname
        there = "here" if where["this"] else f"to {where['name']}"
        label = f"moving {there}: waiting for {'this computer' if holder == me['id'] else hname} to stop it"
        state = "moving"
    elif not it.get("on") or running is False:
        label = "paused here" if where["this"] else f"paused on {where['name']}"
        state = "off"
    else:
        label = "runs here" if where["this"] else f"runs on {where['name']}"
        state = "running"
    targets = [{"id": m["id"], "name": m["name"], "this": m["this"]} for m in machines
               if m["id"] != to and can.get(m["id"]) is True]
    return {"id": rid, "twin": it.get("twin"), "twin_name": twin_name, "name": it.get("name"),
            "schedule": (it.get("schedule") or {}).get("display") or "", "on": bool(it.get("on")),
            "runs_on": {"id": where["id"], "name": where["name"], "this": bool(where.get("this"))},
            "state": state, "label": label, "waiting_for": waiting, "can_move_to": targets,
            "can_force": bool(is_source and not go and holder != me["id"] and holder not in by_id)}


def view(st: dict, R: dict) -> dict:
    role = fleet.role(st)
    on = is_on(R, role) if role != "none" else False
    machines = _machines(st) if role != "none" else []
    out = {"role": role, "on": on, "can_run": True if role == "source" else R["can_run"], "supported": True,
           "state": "off", "sentence": SENTENCE if on else (SENTENCE_OFF_SOURCE if role != "follower"
                                                           else SENTENCE_OFF_LINKED),
           "what_syncs": list(WHAT_SYNCS), "never": list(NEVER), "machines": [], "routines": [], "local_only": 0,
           "restore": [{"id": r["id"], "at": r.get("at"), "what": r.get("what"), "label": r.get("label")}
                       for r in R["restore"]], "problem": None}
    if role == "source":
        src = R["src"] or _blank_src()
        me = fleet.this_machine(st)["id"]
        can = {me: True, **{m: _peer_ok(src, st, m) for m in (st["share"].get("followers") or {})}}
        out["machines"] = [{**m, "can_run": can.get(m["id"], False)} for m in machines]
        out["local_only"] = int(src.get("local_only") or 0) if on else 0
        twins = st["share"]["list"].get("twins") or {}
        if on:
            for rid, it in sorted(src["items"].items(), key=lambda kv: (str(kv[1].get("name")).lower(), kv[0])):
                p = src["peers"].get(it.get("runs_on")) if it.get("runs_on") != me else None
                running = None
                if isinstance(p, dict) and it.get("holder") == it.get("runs_on") \
                        and _counter_of(p.get("applied")) >= _counter_of(src.get("rev")):
                    running = rid in (p.get("running") or [])
                out["routines"].append(_row(st, rid, it, (twins.get(it.get("twin")) or {}).get("name") or "",
                                            machines, can, it.get("holder"), running, True))
        out["state"] = "in_sync" if on else "off"
    elif role == "follower":
        dst = R["dst"] or _blank_dst()
        src_name = fp.machine_name((st["link"].get("source") or {}).get("name"))
        me = fleet.this_machine(st)["id"]
        heard = {**{m: v for m, v in (dst.get("can_run") or {}).items()}, me: R["can_run"] and on}
        out["machines"] = [{**m, "can_run": heard.get(m["id"]) is True} for m in machines]
        # from here a routine can be asked to run on THIS computer, or back on the sharing one; where else it
        # runs is said on the sharing computer or on the computer that is to run it
        src_id = (st["link"].get("source") or {}).get("id")
        can = {m: v for m, v in heard.items() if m in (me, src_id)}
        twins = st["link"].get("twins") or {}
        if dst.get("supported") is False:
            out.update(supported=False, state="older_source", problem=problem("older_source", name=src_name))
        elif on and dst.get("source_on") is False:
            out["state"] = "off_there"
            out["sentence"] = f"Routines are not copied from {src_name} yet. Switch on Routines there."
        elif on:
            out["state"] = "in_sync" if dst.get("rev") else "waiting"
            live = set(_running(R, st))
            for rid, it in sorted(dst["items"].items(), key=lambda kv: (str(kv[1].get("name")).lower(), kv[0])):
                if it.get("gone") or it.get("unlisted"):
                    continue  # (a copy the other computer no longer lists stays paused and is not shown)
                holder = it.get("runs_on") if it.get("go") else None
                running = (rid in live) if it.get("runs_on") == me and it.get("go") else None
                row = _row(st, rid, it, (twins.get(it.get("twin")) or {}).get("name") or "", machines, can, holder,
                           running, False)
                mv = dst.get("move")
                if isinstance(mv, dict) and mv.get("id") == rid and row["state"] != "moving":
                    row.update(state="moving", label=f"asking {src_name} to move it")
                out["routines"].append(row)
    return out


def lines(v: dict) -> list:
    """The routines in words, for the terminal."""
    out = ["What syncs:",
           "  Twins: each twin's name and description, and its note about your computers. Always.",
           f"  Routines: {'on' if v['on'] else 'off'}. {v['sentence']}"
           + ("" if v["on"] else "  (hermes twin fleet routines on)")]
    if v.get("problem"):
        out.append(f"  {v['problem']['message']} {v['problem'].get('hint', '')}".rstrip())
        return out
    if v["role"] == "follower":
        out.append("  Routines may run on this computer: " + ("yes." if v["can_run"] else
                   "no (switch on with: hermes twin routines run-here on)."))
    for r in v["routines"]:
        out.append(f"  {r['name']} ({r['twin_name']}, {r['schedule']}): {r['label']}")
    if v["on"] and not v["routines"] and v["state"] == "in_sync":
        out.append("  No routine to copy yet.")
    if v["local_only"]:
        n = v["local_only"]
        out.append(f"  {plural(n, 'routine')} {'stays' if n == 1 else 'stay'} on this computer "
                   f"(a script, or a one-off).")
    if v["restore"]:
        out.append(f"  Restore points ({len(v['restore'])}): " + "; ".join(
            f"{r['id']} {r['label']}, {ago(r['at'])}" for r in v["restore"][:5]))
    return out


# ---- the terminal ------------------------------------------------------------------------------------------------------
def cmd_routines(args: Any) -> int:
    from .service import get_service
    svc = get_service()
    what, a, b = args.what, args.a, args.b
    if what in ("on", "off"):
        v = svc.routines_switch(on=what == "on")
    elif what == "run-here":
        if a not in ("on", "off"):
            raise EngineError("usage", "Say on or off: hermes twin routines run-here on")
        v = svc.routines_switch(can_run=a == "on")
    elif what == "move":
        if not a or not b:
            raise EngineError("usage", "Say which routine and where: hermes twin routines move ROUTINE COMPUTER")
        v = svc.routines_move(a, b, bool(args.force))
    elif what == "restore":
        if not a:
            v = svc.routines()
            if not args.json:
                for r in v["restore"]:
                    print(f"{r['id']}  {r['label']}, {ago(r['at'])}", flush=True)
                if not v["restore"]:
                    print("No restore point is kept.", flush=True)
                return 0
        else:
            v = svc.routines_restore(a)
    else:
        if what == "tend":  # one pass now (live tests; the background look does the same every half minute)
            tick(force=True)
        v = svc.routines()
    if args.json:
        print(json.dumps(v, ensure_ascii=False, indent=1), flush=True)
        return 0
    for line in lines(v):
        print(line, flush=True)
    return 0


def setup(subs: Any) -> None:
    p = subs.add_parser("routines", help="Routines on your other computers: what copies, and where each runs")
    p.add_argument("what", nargs="?", choices=("show", "on", "off", "run-here", "move", "restore", "tend"),
                   default="show")
    p.add_argument("a", nargs="?", metavar="ROUTINE")
    p.add_argument("b", nargs="?", metavar="COMPUTER")
    p.add_argument("--force", action="store_true",
                   help="Start it here although a computer that is no longer linked has not confirmed it stopped")
    p.add_argument("--json", action="store_true")


HANDLERS = {"routines": cmd_routines}


# ---- the background look -----------------------------------------------------------------------------------------------
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_lock = threading.Lock()


def tick(force: bool = False) -> None:
    """One look, on either kind of computer. Never raises."""
    try:  # (without the files, both tend_* stop at their first look)
        st = fleet.load()
        if st.get("link"):
            tend_linked()
        elif st.get("share"):
            tend_source(force)
    except Exception:
        log.debug("grok-bot-twin: the routines look failed", exc_info=True)


def _has_work() -> bool:
    """Work for the half-minute look: routines switched on where they come from, or arrived here (as tend_*)."""
    if not fleet.exists() or not exists():
        return False
    st, R = fleet.load(), load()
    if st.get("link"):
        return bool(R["dst"] and R["dst"].get("items"))
    return bool(st.get("share")) and (R["src"] is not None or R["on"] is True)


def _loop() -> None:
    while not _stop.wait(TICK_S) and _has_work():
        tick()  # (the thread ends with nothing left to look after; the background check starts it again when needed)


def ensure_started() -> None:
    """Start the half-minute look once per process, only while it has work: no thread otherwise."""
    global _thread
    with _lock:
        if _stop.is_set() or (_thread is not None and _thread.is_alive()) or not _has_work():
            return
        _thread = threading.Thread(target=_loop, name="grok-bot-twin-routines", daemon=True)
        _thread.start()


def stop() -> None:
    _stop.set()


def _reset_for_tests() -> None:
    global _thread
    _quiet[0] = 0.0
    del _lists[:]
    _stop.set()
    if _thread is not None:
        _thread.join(2)
    _thread = None
    _stop.clear()
