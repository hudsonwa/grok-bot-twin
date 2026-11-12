"""Asking another computer, and doing what another computer asked.

Asking (`ask`, `check`): one small request to the mailbox on the sharing computer (tasks_hub.py), then waiting
for the answer. Outbound only, to the one address this computer already talks to for its twins, with its pass.

Doing (`Worker`): while this computer's switch is on, one Hermes process fetches the requests addressed to it
(a held request, like the twin list's) and runs each as ONE ordinary chat turn of the twin's own Hermes bot:

    hermes -p <the twin's Hermes bot> chat -Q --query-file -        (the request is passed on standard input)

(or, when this process is Hermes itself, through Hermes' own stdio interface, where a step that needs approval
waits for the owner of this computer: tasks_turn.py). This module adds no flag and no setting that changes what the bot may
do: no `--yolo`, no `-z`, no toolset list, no approval setting; and the variables through which a running Hermes
session could hand its own looser state to a child (HERMES_YOLO_MODE and friends) are taken out of the child's
environment. So a request runs under this computer's own rules, read from this computer's own config:
`approvals.mode`, its deny list, and `approvals.single_query_mode`, which by default refuses any step that would
need the owner's approval. Such a step is not done; the answer says so.

Before anything runs, this computer checks again by itself (the mailbox is not trusted for it): its switch is
on, the sender is one of the computers of its fleet, the text is within the limits, the sender is within the
rate limit, and the twin exists here. Every request, run or refused, is written to this computer's log.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional

from . import fleet, fleet_follow as follow, fleet_io as fio, fleet_profiles as fp, store, tasks, tasks_hub
from . import tasks_turn
from .model import REF_RE

log = logging.getLogger(__name__)

IDLE_LOOK_S = 5.0
HEARTBEAT_S = 20.0   # how often a computer that works on a request asks the mailbox whether it still knows it
POLL_WAIT_S = 50
OUTPUT_CAP = 256 * 1024
# What a running Hermes session may have set for itself. None of it may reach the chat turn of a request.
SCRUB_ENV = ("HERMES_YOLO_MODE", "HERMES_INTERACTIVE", "HERMES_GATEWAY_SESSION", "HERMES_EXEC_ASK",
             "HERMES_SESSION_PLATFORM", "HERMES_CRON_SESSION", "HERMES_SINGLE_QUERY_SESSION", "HERMES_ACCEPT_HOOKS",
             "HERMES_SESSION_KEY", "HERMES_SESSION_ID")
CHAT_WORDS = ("chat", "-Q", "--query-file", "-")
# Set for the chat turn of a request that came from another computer: that turn cannot ask a further computer
# (no chains, no loops between computers, and nothing a request says can make this computer ask in its name)
# and cannot answer an approval question (tasks.decide).
ASKED_ENV = tasks.ASKED_ENV
SENT_FROM = "[Sent from your computer {name} through Grok Bot Twin. Answer as you would answer here.]"

_stop = threading.Event()
_wake = threading.Event()
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
_conn: list = []
_recent: list = []  # ids of requests this process already took (a request never runs twice)
_rate = tasks.Rate()
_ask_rate = tasks.Rate()


# ---- reaching the mailbox -----------------------------------------------------------------------------------------------
def _endpoint(st: dict) -> Optional[tuple]:
    """(host, port) of the mailbox as this computer reaches it, or None when it has no fleet."""
    r = fleet.role(st)
    if r == "follower":
        return st["link"].get("host"), st["link"].get("port")
    if r == "source":
        inst = (st.get("share") or {}).get("instance")
        if isinstance(inst, dict):
            return inst.get("address"), inst.get("port")
    return None


def _local_request(inst: dict, path: str, body: dict, timeout: float) -> tuple:
    """A call from another Hermes process on the sharing computer itself to the one that serves, shown the
    instance secret from this computer's own state file (as `poke` is). Returns (status, answer)."""
    host, port, secret = inst.get("address"), inst.get("port"), inst.get("secret")
    if not fleet.net.kind_of(host) or not isinstance(port, int) or not isinstance(secret, str) or not secret:
        raise follow.Unreachable()
    if not fleet.net.is_own_address(host):  # a stale record after a move to another network: nobody else is told
        raise follow.Unreachable()
    conn = http.client.HTTPConnection(host, port, timeout=min(timeout, follow.CONNECT_TIMEOUT_S))
    try:
        conn.connect()
        conn.sock.settimeout(timeout)
        conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                     headers={"Content-Type": "application/json", "Connection": "close",
                              "Authorization": "Bearer " + secret})
        resp = conn.getresponse()
        raw = resp.read(follow.ANSWER_CAP + 1)
        obj = json.loads(raw.decode("utf-8")) if raw and len(raw) <= follow.ANSWER_CAP else None
        if not isinstance(obj, dict) or obj.get("schema") != fleet.SCHEMA:
            raise follow.Unreachable()
        return resp.status, obj
    except (OSError, http.client.HTTPException, ValueError, UnicodeDecodeError, RecursionError, AttributeError):
        raise follow.Unreachable() from None
    finally:
        conn.close()


def _call(st: dict, path: str, body: dict, timeout: float = 10.0, holder: Optional[list] = None) -> tuple:
    """(HTTP status, answer, wire error code). A linked computer signs the request with its own key and believes
    only an answer signed back by its sharing computer for this very request: anything else at that address
    counts as "could not be reached". Raises follow.Unreachable."""
    r = fleet.role(st)
    body = {"proto": fleet.PROTO, "at": int(time.time()), **body}
    if r == "follower":
        me = fleet.this_machine(st)
        status, obj, signed = follow._signed_request(st["link"], me["id"], path, body, timeout=timeout, holder=holder)
        if not signed:
            raise follow.Unreachable()
    elif r == "source" and isinstance((st.get("share") or {}).get("instance"), dict):
        status, obj = _local_request(st["share"]["instance"], path, body, timeout)
    else:
        raise follow.Unreachable()
    return status, obj, follow._wire_code(obj)


def _serving_here() -> bool:
    from . import fleet_server as server
    return server.serving() is not None


# ---- asking ----------------------------------------------------------------------------------------------------------------
def _result(task_id: str, machine: dict, twin: str, state: str, code: Optional[str] = None, seconds: Any = None,
            reply: str = "") -> dict:
    name = machine["name"]
    out = {"id": task_id, "machine": {"id": machine["id"], "name": name}, "twin": twin, "state": state,
           "label": tasks.label(state, name, seconds, code),
           "seconds": int(round(seconds)) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
           else None, "reply": reply if state == "done" else "", "error": None}
    if state not in ("done",) + tasks.OPEN_STATES:
        out["error"] = tasks.message(code or "task_failed", name=name)
    elif state == "waiting_approval":
        out["hint"] = (f"A step needs your approval on {name}. Approve or refuse it there (Twins page, "
                       f"Requests); then ask how the request went.")
    elif state == "done" and code == "only_safe":
        m = tasks.message("only_safe", name=name)
        out["hint"] = " ".join(x for x in (m["message"], m.get("hint")) if x)
    return out


def _follow(st: dict, task_id: str, machine: dict, twin: str, wait_s: float, seen: Optional[str] = None,
            known: str = "queued") -> dict:
    """Wait for a request to end, keep this computer's log in step, and return the result. A request that
    starts waiting for approval is reported at once (`seen` None); looking again at one that was already
    waiting (`seen`) waits for what comes after. An unreachable mailbox while waiting is "could not be reached"."""
    deadline = time.monotonic() + max(0.0, wait_s)
    stop_at_waiting = seen is None
    while True:
        left = deadline - time.monotonic()
        hold = int(min(tasks_hub.MAX_WAIT_S, max(0, left)))
        try:
            status, obj, wire = _call(st, "/v1/task/wait", {"id": task_id, "wait_s": hold, "seen": seen},
                                      timeout=hold + 15)
        except follow.Unreachable:
            # the sharing computer did not answer just now: the request itself may well go on. Nothing is
            # written off; the asker is told what was last known and can look again.
            return _result(task_id, machine, twin, known if known in tasks.OPEN_STATES else "running")
        if status != 200:
            code = wire if wire in tasks.CODES else "tasks_unreachable"
            tasks.log_update("out", task_id, "failed", code)
            return _result(task_id, machine, twin, "failed", code)
        state = obj.get("state") if obj.get("state") in tasks.STATES else "failed"
        code = obj.get("code") if obj.get("code") in tasks.CODES else None
        seconds = tasks.whole_seconds(obj.get("seconds"))
        reply = tasks.clean_reply(obj.get("reply")) if state == "done" else ""
        tasks.log_update("out", task_id, state, code, seconds, reply)
        if state in tasks.END_STATES or (state == "waiting_approval" and stop_at_waiting) \
                or deadline - time.monotonic() <= 0.5:
            return _result(task_id, machine, twin, state, code, seconds, reply)
        seen = known = state


def ask(svc: Any, machine: Any, text: Any, twin: Any = None, wait_s: float = tasks.ASK_WAIT_S,
        profile: Optional[str] = None) -> dict:
    """Ask the copy of a twin on another computer to do something, and wait for the answer. Returns the result
    (also for a request that was refused or failed: `error` then says why in plain words). Raises TaskError only
    for what is wrong on this side before anything is sent (no such computer, nothing to ask, too long)."""
    if tasks.asked_turn():
        raise tasks.TaskError("passed_on")
    st = fleet.load()
    target = tasks.find_machine(st, machine)
    twin_id, twin_name = tasks.find_twin(st, twin, profile if profile is not None else svc.profile())
    if not isinstance(text, str) or not text.strip():
        raise tasks.TaskError("empty")
    if len(text.strip()) > tasks.REQUEST_CAP:
        raise tasks.TaskError("too_long")
    clean = tasks.clean_request(text)
    if not clean:
        raise tasks.TaskError("empty")
    local_id = tasks.new_id()

    def refused(code: str) -> dict:
        tasks.log_entry("out", local_id, target["name"], twin_name, clean, "refused", code=code)
        return _result(local_id, target, twin_name, "refused", code)

    if not _ask_rate.allow(target["id"]):
        return refused("too_many")
    if fleet.role(st) == "source" and not _serving_here():
        svc.fleet()  # nobody on this computer serves right now: take over (as the page's own read does)
        st = fleet.load()
    try:
        status, obj, wire = _call(st, "/v1/task/ask", {"to": target["id"], "twin": twin_id, "text": clean})
    except follow.Unreachable:
        return refused("tasks_unreachable")
    if status != 200 or not isinstance(obj.get("id"), str) or not tasks.TASK_ID_RE.match(obj["id"]):
        return refused(wire if wire in tasks.CODES else "tasks_unreachable")
    task_id = obj["id"]
    tasks.log_entry("out", task_id, target["name"], twin_name, clean, "queued")
    svc.emit("tasks", None, {"what": "asked", "count": 1})
    out = _follow(st, task_id, target, twin_name, wait_s)
    svc.emit("tasks", None, {"what": "answered" if out["state"] in tasks.END_STATES else "waiting", "count": 1})
    return out


def check(svc: Any, task_id: Any, wait_s: float = 0) -> dict:
    """How a request asked from this computer is doing now (after "waiting for approval" or "still working")."""
    if not isinstance(task_id, str) or not tasks.TASK_ID_RE.match(task_id):
        raise tasks.TaskError("unknown_task")
    row = next((r for r in reversed(tasks.read_log()) if r.get("id") == task_id and r.get("dir") == "out"), None)
    if row is None:
        raise tasks.TaskError("unknown_task")
    st = fleet.load()
    name = fp.machine_name(row.get("peer"))
    machine = next((m for m in tasks.machines(st) if m["name"] == name), {"id": "m-000000000000", "name": name})
    twin = tasks.short(row.get("twin"), 100)
    if row.get("state") in tasks.END_STATES:
        # ended already: the answer itself is not kept in full; the log's short form is what is left
        return _result(task_id, machine, twin, row["state"], row.get("code"), row.get("seconds"),
                       tasks.short(row.get("reply")))
    return _follow(st, task_id, machine, twin, wait_s,
                   seen=row.get("state") if wait_s > 0 and row.get("state") in tasks.OPEN_STATES else None,
                   known=row.get("state"))


def refresh_open(svc: Any, limit: int = 3) -> None:
    """Bring this computer's still-open "asked from here" log lines up to date (the page calls this before it
    shows the log). At most `limit` small requests; never raises."""
    try:
        rows = [r for r in tasks.read_log() if r.get("dir") == "out" and r.get("state") in tasks.OPEN_STATES]
        too_old = tasks.RUN_LIMIT_S + tasks.APPROVAL_LIMIT_S + tasks.TASK_TTL_S
        for r in rows[-limit:]:
            asked = tasks.parse_iso(r.get("asked_at"))
            if asked is None or (tasks.now_dt() - asked).total_seconds() > too_old:
                tasks.log_update("out", r.get("id"), "timeout", "task_timeout")  # nobody will answer it any more
                continue
            with contextlib.suppress(tasks.TaskError):
                check(svc, r.get("id"), 0)
    except Exception:
        log.debug("grok-bot-twin: could not refresh the request log", exc_info=True)


# ---- one chat turn of the twin's own Hermes bot ---------------------------------------------------------------------------
# The last one is the process in which Hermes runs a bot's plugins when that bot is not the one Hermes was started
# for (a twin opened in Hermes Desktop): it is started as `python -m hermes_cli.plugin_host_child` with Hermes' own
# Python. The plugin's copy in it may be the one that does the requests, so it counts as Hermes too; taken for
# another program, it ran every request in the single-question mode, where a step that needs approval is refused
# at once and the owner is never asked.
HERMES_MODULES = ("hermes_cli.main", "tui_gateway.entry", "tui_gateway.server", "gateway.run",
                  "hermes_cli.plugin_host_child")


def in_hermes() -> bool:
    """Is this process Hermes itself (its command line, its gateway, the process behind its Desktop or terminal
    UI, the process it runs a bot's plugins in)? Told from what is already loaded, never by importing: importing
    Hermes' entry module from another program makes Hermes set itself up for that program's data folder."""
    if any(name in sys.modules for name in HERMES_MODULES):
        return True
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    return getattr(spec, "name", "") in HERMES_MODULES


def hermes_command() -> Optional[list]:
    """How to start this computer's own Hermes: the same program this process runs in when it is Hermes, else
    `hermes` from PATH after the plugin's usual ownership check. None when there is none."""
    if in_hermes():  # this process is Hermes: use the very same program
        # -P: the working folder is kept off the child's import path; turn_env() hands it this process's own
        return [sys.executable, "-P", "-m", "hermes_cli.main"]
    from .engine import resolve_tool
    path, problem = resolve_tool("hermes")
    if path is None or problem:
        return None
    return [path]


def turn_command(profile: str) -> Optional[list]:
    base = hermes_command()
    if base is None or not fp.PROFILE_ID_RE.match(profile):
        return None
    return base + ["-p", profile, *CHAT_WORDS]


def turn_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    if in_hermes():
        # Hermes is started in more than one way (some put its code on the import path themselves): the turn's
        # own Hermes process is given the same import path this one has, so it finds the same code.
        paths = [p for p in sys.path if p and os.path.isabs(p)] + env.get("PYTHONPATH", "").split(os.pathsep)
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(p for p in paths if p))
    env["HERMES_HOME"] = str(store.hermes_root())
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env[ASKED_ENV] = "1"
    return env


# ---- the line that heads a request when it arrives as a chat turn ---------------------------------------------------------
# SENT_FROM above is how the line has always opened; SENT_LINE is the whole line now.
# Worded against three things a model can do:
# - asked from one computer about another, a twin took "it" for the asking computer and tried to pass the request
#   on, so the answer described the wrong computer;
# - in a twin's first chat ever Hermes adds a note (introduce yourself, mention /help, offer a profile), and that
#   offer went back to the other computer with the answer;
# - a flat "no greeting" would overrule the way a twin is set up to speak: hence "in your usual voice", and the
#   note is named instead (the offer can still get through now and then).
SENT_LEAD = SENT_FROM.split("{name}")[0]  # "[Sent from your computer "
SENT_LINE = (SENT_LEAD + "{name} through Grok Bot Twin. Answer it here, as you would answer here: \"you\", \"it\" "
             "and \"this computer\" in the request mean this computer, and the request is never passed on to another "
             "computer. The user reads the answer on {name}, not here: answer in your usual voice and stop when the "
             "request is answered. For this turn, skip any note that asks you to introduce yourself, mention "
             "commands or offer something.]")


def turn_cwd() -> str:
    """Where a request's chat turn starts: the owner's home folder (as a chat opened in Hermes would), never
    the plugin's or Hermes' own folders."""
    try:
        home = os.path.expanduser("~")
        return home if os.path.isdir(home) else str(store.hermes_root())
    except Exception:
        return str(store.hermes_root())


def run_turn(profile: str, text: str, should_stop: Callable[[], Optional[str]],
             limit_s: Optional[float] = None) -> dict:
    """Run one chat turn of `profile` with `text` and return {"ok": True, "reply"} or {"ok": False, "code"}.
    `should_stop` is asked twice a second and returns a code to stop for (the switch went off, Hermes is
    closing), else None. Never raises."""
    argv = turn_command(profile)
    if argv is None:
        return {"ok": False, "code": "no_hermes"}
    limit_s = tasks.RUN_LIMIT_S if limit_s is None else limit_s
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        env = turn_env()
        # `hermes chat` may start the Hermes that then serves every chat on this computer for a while; the
        # variable that marks a request's turn must not live on in it...
        env.pop(ASKED_ENV, None)
        tasks.mark_single(True)  # ...so while it runs, no chat here passes a request on or answers a question
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, cwd=turn_cwd(), **kwargs)
    except OSError:
        tasks.mark_single(False)
        return {"ok": False, "code": "no_hermes"}
    started = time.monotonic()
    data: Optional[bytes] = text.encode("utf-8")
    out = b""
    why: Optional[str] = None
    try:
        while True:
            try:
                out, _ = proc.communicate(input=data, timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                data = None
            except (OSError, ValueError):
                break
            why = should_stop() or ("task_timeout" if time.monotonic() - started > limit_s else None)
            if why:
                _end_process(proc)
                break
    finally:
        tasks.mark_single(False)
    if why:
        return {"ok": False, "code": why}
    reply = out[:OUTPUT_CAP].decode("utf-8", "replace").strip() if out else ""
    if proc.returncode != 0 or not reply:
        return {"ok": False, "code": "task_failed"}
    return {"ok": True, "reply": reply}


def _end_process(proc: "subprocess.Popen") -> None:
    """End the chat turn and everything it started (a switch that goes off stops the whole request, not only the
    program that was started first)."""
    with contextlib.suppress(Exception):
        if os.name != "nt":
            import signal
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    try:
        proc.communicate(timeout=5)
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()
            proc.communicate(timeout=5)
    with contextlib.suppress(Exception):
        if os.name != "nt":  # whatever of its group outlived the polite ending
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # Windows' own taskkill.exe from the system folder, never looked up on PATH
            from . import osflags as _osflags
            subprocess.run([os.path.join(_osflags.windows_system_folder(), "taskkill.exe"), "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ---- doing what another computer asked --------------------------------------------------------------------------------------
def _clean_task(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not tasks.TASK_ID_RE.match(raw["id"]):
        return None
    frm = fleet.clean_machine({**raw["from"], "platform": "other"}) if isinstance(raw.get("from"), dict) else None
    twin = raw.get("twin")
    text = raw.get("text")
    if frm is None or not isinstance(twin, str) or not REF_RE.match(twin) or not isinstance(text, str):
        return None
    return {"id": raw["id"], "from": {"id": frm["id"], "name": frm["name"]}, "twin": twin, "text": text}


def do_task(svc: Any, raw: Any, report: Callable[..., None],
            stopping: Callable[[], bool], wanted: Optional[Callable[[str, str], Optional[bool]]] = None) -> None:
    """Check one fetched request by this computer's own rules, run it, and report how it went.
    `report(task_id, state, code, seconds, reply)` tells the mailbox; this computer's log is written here.
    `wanted(task_id, state)` asks the mailbox, every HEARTBEAT_S while the turn runs, whether it still knows the
    request (False: it does not; None: it could not be asked). A request the mailbox has forgotten (the sharing
    computer's Hermes was started again, the request ran out of time there) is stopped: nobody can receive its
    answer any more, and until it ends this computer fetches nothing else."""
    task = _clean_task(raw)
    if task is None:
        return
    tid, sender = task["id"], task["from"]
    if tid in _recent:
        return
    _recent.append(tid)
    del _recent[:-200]
    st = fleet.load()
    twins = tasks.twins_here(st)
    twin = twins.get(task["twin"])
    twin_name = twin["name"] if twin else ""
    text = tasks.clean_request(task["text"])
    known = {m["id"]: m for m in tasks.machines(st) if not m["this"]}

    def refuse(code: str) -> None:
        tasks.log_entry("in", tid, sender["name"], twin_name, text, "refused", code=code)
        report(tid, "refused", code, 0, "")

    # the name this computer knows the sender by (never the one the request carries)
    if sender["id"] not in known:
        return refuse("unknown_machine")
    sender = {"id": sender["id"], "name": known[sender["id"]]["name"]}
    if not tasks.accepting():
        return refuse("tasks_off")
    if not text or len(task["text"].strip()) > tasks.REQUEST_CAP:
        return refuse("too_long" if text else "empty")
    if not _rate.allow(sender["id"]):
        return refuse("too_many")
    if twin is None or not twin["profile"] or not fio.is_real_dir(fp.profiles_root() / twin["profile"]):
        return refuse("no_twin_there")
    tasks.log_entry("in", tid, sender["name"], twin_name, text, "running")
    svc.emit("tasks", None, {"what": "arrived", "count": 1})
    started = time.monotonic()

    now_state = {"state": "running"}
    telling = threading.Lock()       # one message to the mailbox at a time: a look never overtakes a real change
    forgotten = threading.Event()
    over = threading.Event()

    def look() -> None:
        while not over.wait(HEARTBEAT_S):
            with telling:
                if over.is_set():
                    return
                try:
                    known = wanted(tid, now_state["state"])
                except Exception:
                    known = None
            if known is False:
                forgotten.set()
                return

    def should_stop() -> Optional[str]:
        if stopping():
            return "stopped"
        if forgotten.is_set():
            return "unknown_task"
        return None if tasks.accepting() else "switched_off"

    def on_wait(question: Optional[dict]) -> None:
        # a step waits for this computer's owner (or stopped waiting). The question stays on this computer:
        # the asker is told only "waiting for approval".
        tasks.set_pending(tid, question)
        state = "waiting_approval" if question is not None else "running"
        tasks.log_update("in", tid, state)
        with telling:
            now_state["state"] = state
            report(tid, state, None, None, "")
        svc.emit("tasks", None, {"what": "needs_approval" if question is not None else "resumed", "count": 1})

    words = SENT_LINE.format(name=fp.note_machine(sender["name"])) + "\n" + text
    if wanted is not None:
        threading.Thread(target=look, name="grok-bot-twin-request-look", daemon=True).start()
    try:
        res = tasks_turn.run_turn(twin["profile"], words, should_stop, on_wait, lambda: tasks.decision(tid))
        if res is None:  # this process is not Hermes (or its interface did not start): the single-question mode
            res = run_turn(twin["profile"], words, should_stop)
            if res.get("ok") and tasks.mode() == "safe" and "BLOCKED" in str(res.get("reply") or ""):
                # there Hermes refuses such a step by itself (its own default); the asker still hears why
                res["not_approved"] = "only_safe"
    finally:
        over.set()
    tasks.set_pending(tid, None)
    seconds = round(time.monotonic() - started, 1)
    if res.get("ok"):
        reply = tasks.clean_reply(res.get("reply"))
        code = res.get("not_approved") if res.get("not_approved") in tasks_hub.NOT_APPROVED else None
        tasks.log_update("in", tid, "done", code, seconds, reply)
        report(tid, "done", code, seconds, reply)
    else:
        code = res.get("code") if res.get("code") in tasks.CODES else "task_failed"
        state = {"task_timeout": "timeout", "switched_off": "cancelled", "stopped": "cancelled",
                 "unknown_task": "cancelled"}.get(code, "failed")
        tasks.log_update("in", tid, state, code, seconds)
        report(tid, state, code, seconds, "")
    svc.emit("tasks", None, {"what": "finished", "count": 1})


def _report_http(tid: str, state: str, code: Optional[str], seconds: Any, reply: str) -> None:
    body = {"id": tid, "state": state}
    if code:
        body["code"] = code
    if seconds is not None:
        body["seconds"] = seconds
    if reply:
        body["reply"] = reply
    for pause in (0, 2, 5):  # the answer is worth a second try; after that this computer's own log still has it
        if pause:
            _stop.wait(pause)
        try:
            status, _obj, _wire = _call(fleet.load(), "/v1/task/update", body, timeout=10)
            if status in (200, 404):
                return
        except follow.Unreachable:
            continue


def _known(status: int, obj: Any) -> Optional[bool]:
    if status == 404:
        return False
    if status == 200:
        return not (isinstance(obj, dict) and obj.get("ended") is True)
    return None


def _wanted_http(tid: str, state: str) -> Optional[bool]:
    """Does the mailbox still know this request? (see do_task) One short try: this is only a look."""
    try:
        status, obj, _wire = _call(fleet.load(), "/v1/task/update", {"id": tid, "state": state}, timeout=8)
    except follow.Unreachable:
        return None
    return _known(status, obj)


def _wanted_local(tid: str, state: str) -> Optional[bool]:
    st = fleet.load()
    me = fleet.this_machine(st)
    return _known(*tasks_hub.hub.update({"id": me["id"], "name": me["name"]}, {"id": tid, "state": state})[:2])


def _report_local(tid: str, state: str, code: Optional[str], seconds: Any, reply: str) -> None:
    st = fleet.load()
    me = fleet.this_machine(st)
    body = {"id": tid, "state": state, "code": code, "seconds": seconds, "reply": reply}
    tasks_hub.hub.update({"id": me["id"], "name": me["name"]}, body)


def _poll_once(svc: Any, st: dict, accept: bool, wait_s: int) -> Optional[bool]:
    """One fetch. True: a request was handled. False: none arrived. None: the mailbox could not be reached."""
    r = fleet.role(st)
    if r == "source":
        if not _serving_here():
            return None
        me = fleet.this_machine(st)
        _status, obj = tasks_hub.hub.poll({"id": me["id"], "name": me["name"], "inside": _stop.is_set},
                                          {"accept": accept, "wait_s": wait_s})
        report, wanted = _report_local, _wanted_local
    elif r == "follower":
        try:
            status, obj, _wire = _call(st, "/v1/task/poll", {"accept": accept, "wait_s": wait_s},
                                       timeout=wait_s + 15, holder=_conn)
        except follow.Unreachable:
            return None
        if status != 200:
            return None
        report, wanted = _report_http, _wanted_http
    else:
        return None
    if not accept or not isinstance(obj.get("task"), dict):
        return False
    do_task(svc, obj["task"], report, _stop.is_set, wanted)
    return True


def _may_work(st: dict) -> bool:
    r = fleet.role(st)
    return r == "follower" or (r == "source" and _serving_here())


def _loop(get_service: Callable) -> None:
    told_off = False
    backoff = IDLE_LOOK_S
    while not _stop.is_set():
        try:
            st = fleet.load() if fleet.exists() else None
            if st is None or not _may_work(st):
                _wake.wait(30)
                _wake.clear()
                continue
            with fio.exclusive(store.central_dir() / "tasks-worker.lock") as got:
                if not got:  # another Hermes process on this computer does the work
                    _wake.wait(30)
                    _wake.clear()
                    continue
                tasks.close_stale("in")  # (only the process that holds this lock could still be running them)
                while not _stop.is_set():
                    st = fleet.load()
                    if not _may_work(st):
                        break
                    if not tasks.accepting():
                        if not told_off:  # say it once, so askers hear "switched off" and not silence
                            told_off = _poll_once(get_service(), st, False, 0) is not None
                        _wake.wait(IDLE_LOOK_S)
                        _wake.clear()
                        continue
                    told_off = False
                    got_one = _poll_once(get_service(), st, True, POLL_WAIT_S)
                    if got_one is None:
                        _wake.wait(backoff)
                        _wake.clear()
                        backoff = min(60.0, backoff * 2)
                    else:
                        backoff = IDLE_LOOK_S
        except Exception:
            log.debug("grok-bot-twin: the request worker stumbled", exc_info=True)
            _wake.wait(30)
            _wake.clear()


def ensure_started(get_service: Callable) -> bool:
    """Start this process's worker thread, once, and only on a computer whose switch file exists (the owner has
    used the switch at least once) and that has a fleet. A computer that never switched this on runs no thread
    and makes no request for it. True when a thread was started."""
    global _thread
    with _lock:
        if _stop.is_set() or (_thread is not None and _thread.is_alive()):
            return False
        try:
            if not fleet.exists() or not os.path.lexists(store.central_dir() / tasks.SWITCH_FILE):
                return False
            if fleet.role(fleet.load()) == "none":
                return False
        except OSError:
            return False
        t = threading.Thread(target=_loop, name="grok-bot-twin-requests", daemon=True, args=(get_service,))
        t.start()
        _thread = t
        return True


def kick() -> None:
    _wake.set()


def switch_changed(get_service: Callable) -> None:
    """The owner flipped the switch in this process: start (or wake) the worker, and when it went off end a
    held fetch at once and tell the mailbox, so nothing more arrives and askers hear "switched off"."""
    on = tasks.accepting()
    ensure_started(get_service)
    if not on:
        follow_abort()
        with contextlib.suppress(Exception):
            st = fleet.load()
            if fleet.role(st) == "follower" or (fleet.role(st) == "source" and not _serving_here()):
                _call(st, "/v1/task/poll", {"accept": False, "wait_s": 0}, timeout=5)
            elif fleet.role(st) == "source":
                me = fleet.this_machine(st)
                tasks_hub.hub.poll({"id": me["id"], "name": me["name"]}, {"accept": False, "wait_s": 0})
    kick()


def follow_abort() -> None:
    for conn in list(_conn):
        sock = conn.sock
        if sock is None:
            continue
        with contextlib.suppress(Exception):
            import socket
            sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            sock.close()


def running() -> bool:
    return _thread is not None and _thread.is_alive() and not _stop.is_set()


def stop() -> None:
    _stop.set()
    _wake.set()
    follow_abort()
    with tasks_hub.hub.cond:  # a fetch held inside this process (the sharing computer's own worker)
        tasks_hub.hub.cond.notify_all()


def _reset_for_tests() -> None:
    global _thread, _rate, _ask_rate
    stop()
    if _thread is not None:
        _thread.join(8)
    _thread = None
    _stop.clear()
    _wake.clear()
    del _recent[:]
    _rate, _ask_rate = tasks.Rate(), tasks.Rate()
    tasks_hub.reset()
