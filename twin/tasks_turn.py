"""One chat turn of a twin's Hermes bot in which a step may wait for the owner's approval on this computer.

tasks_run.py can run a request as Hermes' single-question mode, where a step that needs approval is simply
refused. This module is the better way, used when this process is Hermes itself: Hermes' own interface for
programs (developer guide, "Programmatic integration"), the JSON messages its terminal UI and its Desktop speak.
There Hermes ASKS before a risky step, and waits. Two ways in, tried in this order:

1. Attached (`twin/turn_bridge.py`): Hermes is usually already running on this computer (its Desktop is open,
   a chat was just used). Then one process owns the Hermes folder and a program has to attach to it, the way
   `hermes chat` does. The question comes as an event and is answered with Hermes' own call:

       <- {"method": "event", "params": {"type": "approval.request", "payload": {"prompt_id", "command", ...}}}
       -> {"method": "approval.respond", "params": {"prompt_id", "choice": "once"}}          (or "deny")

2. Stand-alone (`python -m tui_gateway.entry`), for a Hermes that has no such client or cannot be attached to.
   The question comes as a request and is answered under its id:

       <- {"id": "srq-7", "method": "approval", "params": {"command": "rm -rf build", ...}}
       -> {"id": "srq-7", "result": {"choice": "once"}}

What this module does with that question, in both, and nothing more:

- It tells the caller "a step waits for approval" (the asking computer then shows "waiting for approval on
  Surface") and remembers the question for this computer's own Twins page and `hermes twin requests`.
- It answers ONLY with what the owner chose on THIS computer (tasks.decision, written by this computer's page
  or terminal; no route from another computer can write it), and only "once" or "deny": a request from another
  computer never earns a standing permission ("for this session", "always").
- The chat it opens is marked as a request's turn (tasks.mark_turn) for as long as the turn runs, so that
  nothing that chat starts can answer the question through the plugin or pass the request on to a third computer.
- When this computer's owner chose "Only safe requests" (tasks.mode, read when the question arrives), every
  such question is answered "deny" at once: nothing waits, and the asker is told why.
- A step too long to be shown whole on this computer (tasks.STEP_CAP characters) is refused at once: the owner is
  never asked to approve a command whose end was cut off.
- Apart from that it never answers by itself. No answer in Hermes' own time limit (approvals.timeout, 300 s by default) and
  Hermes withdraws the question and refuses the step ("silence is not consent"). A request for a password or a
  secret is declined at once or left to run out. A question the twin puts in words ("which one?") gets an empty
  answer, so the turn goes on without one: no words are ever made up for the owner.

Whether a step needs approval at all is Hermes' decision by this computer's own settings (approvals.mode, its
deny list, its guardian check). Nothing here changes them: no `/yolo`, no setting, and the same cleaned
environment as tasks_run.turn_env. If neither way starts, the caller falls back to the single-question mode.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from . import fleet_profiles as fp, store, tasks

log = logging.getLogger(__name__)

READY_LIMIT_S = 90        # Hermes must say "ready" and open a chat within this
FRAME_CAP = 2 * 1024 * 1024
ANSWERS = ("once", "deny")  # the only two choices ever sent
DECLINE = {"code": -32601, "message": "not available for a request from another computer"}
# (next to this file, not under tools/: a twin's own Hermes bot carries a copy of the plugin's twin/ folder only)
BRIDGE = Path(__file__).resolve().parent / "turn_bridge.py"


def transports() -> list:
    """How Hermes' interface can be reached from this process: [(command, dialect)], best first; empty when this
    process is not Hermes (the standalone runner), in which case the caller uses the single-question mode."""
    from . import tasks_run
    if not tasks_run.in_hermes():
        return []
    out = []
    if BRIDGE.is_file():
        # -P: the bridge's own folder (the plugin's modules) is kept off its import path
        out.append(([sys.executable, "-P", str(BRIDGE)], "attached"))
    out.append(([sys.executable, "-P", "-m", "tui_gateway.entry"], "stdio"))
    return out


def _reader(stream: Any, frames: "queue.Queue") -> None:
    try:
        for line in stream:
            if len(line) > FRAME_CAP:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                frames.put(obj)
    except (OSError, ValueError):
        pass
    frames.put(None)


def _text(value: Any, cap: int = 300) -> str:
    return tasks.short(value, cap) if isinstance(value, str) else ""


def run_turn(profile: str, text: str, should_stop: Callable[[], Optional[str]],
             on_wait: Callable[[Optional[dict]], None], answer: Callable[[], Optional[str]],
             limit_s: Optional[float] = None) -> Optional[dict]:
    """Run one chat turn of `profile` with `text` through Hermes' interface, attached or stand-alone.

    `on_wait(question)` is called with {"command", "description"} when a step starts waiting for approval and
    with None when it stops waiting; `answer()` returns "once", "deny" or None (the owner has not chosen yet).
    Returns {"ok": True, "reply", "not_approved": None | "approval_denied" | "approval_timeout"} /
    {"ok": False, "code"}; or None when Hermes could not be reached this way and nothing was sent, so the caller
    may fall back to the single-question mode. Never raises."""
    for argv, dialect in transports():
        out = _run(argv, dialect, profile, text, should_stop, on_wait, answer, limit_s)
        if out is not None:
            return out
    return None


def _run(argv: list, dialect: str, profile: str, text: str, should_stop: Callable[[], Optional[str]],
         on_wait: Callable[[Optional[dict]], None], answer: Callable[[], Optional[str]],
         limit_s: Optional[float] = None) -> Optional[dict]:
    from . import tasks_run
    if not fp.PROFILE_ID_RE.match(profile):
        return None
    attached = dialect == "attached"
    limit_s = tasks.RUN_LIMIT_S if limit_s is None else limit_s
    env = tasks_run.turn_env()
    env["HERMES_HOME"] = str(store.hermes_root() / "profiles" / profile)  # the twin's own Hermes bot
    if attached:
        # Attaching may START the Hermes that then serves every chat on this computer for a while. The variable
        # that marks a request's turn must not live on in it (the owner's own chats would be taken for requests):
        # an attached turn is marked by its chat's number instead (tasks.mark_turn, below).
        env.pop(tasks.ASKED_ENV, None)
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, cwd=tasks_run.turn_cwd(), text=True, encoding="utf-8", errors="replace",
                                bufsize=1, **kwargs)
    except OSError:
        return None
    frames: "queue.Queue" = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, frames), name="grok-bot-twin-turn", daemon=True).start()
    said: list = []  # the last few lines Hermes wrote for a person: kept for this computer's own log only
    threading.Thread(target=_errors, args=(proc.stderr, said), name="grok-bot-twin-turn-err", daemon=True).start()
    sent = {"n": 0}

    def send(obj: dict) -> bool:
        try:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def call(method: str, params: dict) -> int:
        sent["n"] += 1
        send({"jsonrpc": "2.0", "id": sent["n"], "method": method, "params": params})
        return sent["n"]

    started = time.monotonic()
    working = 0.0            # seconds spent not waiting for the owner
    last = started
    create_id = submit_id = None
    session = None
    submitted = False
    waiting: Optional[dict] = None   # the open approval question: how to answer it
    chats: list = []                 # this turn's chat, as Hermes numbers it
    generation: Optional[int] = None  # the run Hermes is on in that chat (it wants it named to stop the run)
    not_approved: Optional[str] = None
    unanswered = False       # Hermes closed a question that this module had not answered
    out: Optional[dict] = None

    def reply_to(question: dict, choice: str) -> None:
        if attached:
            call("approval.respond", {"session_id": session, "execution_generation": question.get("generation"),
                                      "prompt_id": question.get("prompt_id"), "choice": choice})
        else:
            send({"jsonrpc": "2.0", "id": question["rpc_id"], "result": {"choice": choice}})

    try:
        while out is None:
            try:
                f = frames.get(timeout=0.5)
            except queue.Empty:
                f = {}
            now = time.monotonic()
            if waiting is None:
                working += now - last
            last = now
            if f is None:  # Hermes' interface ended
                out = {"ok": False, "code": "task_failed"} if submitted else None
                break
            why = should_stop()
            if why:
                out = {"ok": False, "code": why}
                break
            if not submitted and now - started > READY_LIMIT_S:
                break  # never got as far as sending the request: the caller may fall back
            if working > limit_s or now - started > limit_s + tasks.APPROVAL_LIMIT_S + 120:
                out = {"ok": False, "code": "task_timeout"}
                break
            if waiting is not None:
                choice = answer()
                if choice not in ANSWERS and tasks.mode() == "safe":
                    # the owner chose "Only safe requests" while this step waited: it does not wait any longer
                    reply_to(waiting, "deny")
                    not_approved = "only_safe"
                    waiting = None
                    on_wait(None)
                elif choice in ANSWERS:
                    reply_to(waiting, choice)
                    if choice == "deny":
                        not_approved = not_approved or "approval_denied"
                    waiting = None
                    on_wait(None)
            if not f:
                continue
            method, params = f.get("method"), f.get("params") if isinstance(f.get("params"), dict) else {}
            if method == "event":
                kind = params.get("type")
                payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
                mine = params.get("session_id") == session and session is not None
                gen = params.get("execution_generation", payload.get("execution_generation"))
                if mine and isinstance(gen, int) and not isinstance(gen, bool):
                    generation = gen
                if kind == "gateway.ready" and create_id is None:
                    call("client.capabilities", {"server_requests": True})
                    create_id = call("session.create", {})
                elif kind == "approval.request" and attached and mine:
                    question = {"prompt_id": payload.get("prompt_id"),
                                "generation": payload.get("execution_generation", params.get("execution_generation"))}
                    # one at a time: a second question while one is open is refused. So is a step too long to
                    # be shown whole (tasks.step_whole): the owner is never asked about something cut short
                    safe = tasks.mode() == "safe"  # "Only safe requests": refused at once, nothing waits
                    if waiting is not None or safe or not tasks.step_whole(payload.get("command")):
                        reply_to(question, "deny")
                        not_approved = "only_safe" if safe else (not_approved or "approval_denied")
                    else:
                        waiting = question
                        on_wait({"command": tasks.step_text(payload.get("command")),
                                 "description": _text(payload.get("description"))})
                elif kind == "approval.settled" and attached and waiting is not None \
                        and payload.get("prompt_id") == waiting.get("prompt_id"):
                    unanswered = True  # closed by Hermes: its time ran out, or it was answered in Hermes itself
                    waiting = None
                    on_wait(None)
                elif kind == "clarify.request" and attached and mine:
                    # a question for a person other than an approval: no answer is made up
                    call("clarify.respond", {"session_id": session, "prompt_id": payload.get("prompt_id"), "answer": "",
                                             "execution_generation": payload.get("execution_generation",
                                                                                 params.get("execution_generation"))})
                elif kind == "request.cancel" and not attached and waiting is not None and payload.get("id") in (
                        waiting.get("rpc_id"), waiting.get("request_id"), None):
                    not_approved = not_approved or "approval_timeout"  # Hermes withdrew it: nobody answered
                    waiting = None
                    on_wait(None)
                elif kind == "message.complete" and submitted and mine:
                    reply = payload.get("text")
                    if unanswered and not_approved is None and isinstance(reply, str) and "BLOCKED" in reply:
                        not_approved = "approval_timeout"
                    out = {"ok": True, "reply": reply, "not_approved": not_approved} \
                        if isinstance(reply, str) and reply.strip() else {"ok": False, "code": "task_failed"}
                elif kind == "error" and submitted and params.get("session_id") in (session, None):
                    out = {"ok": False, "code": "task_failed"}
            elif method == "approval" and "id" in f and not attached:
                # a second question while one is open (one at a time), or a step too long to be shown whole
                safe = tasks.mode() == "safe"  # "Only safe requests": refused at once, nothing waits
                if waiting is not None or safe or not tasks.step_whole(params.get("command")):
                    send({"jsonrpc": "2.0", "id": f["id"], "result": {"choice": "deny"}})
                    not_approved = "only_safe" if safe else (not_approved or "approval_denied")
                    continue
                waiting = {"rpc_id": f["id"], "request_id": params.get("request_id")}
                on_wait({"command": tasks.step_text(params.get("command")),
                         "description": _text(params.get("description"))})
            elif isinstance(method, str) and "id" in f:
                # any other question Hermes asks a person (a password, a secret, a choice): never answered here
                send({"jsonrpc": "2.0", "id": f["id"], "result": {}} if method == "clarify"
                     else {"jsonrpc": "2.0", "id": f["id"], "error": DECLINE})
            elif "id" in f and f.get("id") == create_id:
                result = f.get("result") if isinstance(f.get("result"), dict) else {}
                session = result.get("session_id")
                if not isinstance(session, str) or not session:
                    break  # no chat was opened: nothing was sent, the caller may fall back
                # this chat is a request's turn: marked before the request is handed to it (tasks.asked_turn)
                chats = [c for c in (session, result.get("stored_session_id")) if isinstance(c, str)]
                tasks.mark_turn(chats, True)
                submit = {"session_id": session, "text": text}
                if attached:
                    submit["submission_id"] = uuid.uuid4().hex
                submit_id = call("prompt.submit", submit)
                submitted = True
            elif "id" in f and f.get("id") == submit_id and "error" in f:
                out = {"ok": False, "code": "task_failed"}
    except Exception:
        log.debug("grok-bot-twin: the chat turn stumbled", exc_info=True)
        out = {"ok": False, "code": "task_failed"} if submitted else None
    finally:
        if waiting is not None:
            try:  # leaving with a question open: refuse it, never leave it to be answered by nobody
                reply_to(waiting, "deny")
                on_wait(None)
            except Exception:
                pass
        if submitted and session and not (out or {}).get("ok"):
            # Leaving before the turn finished (the switch went off, time ran out, the request is no longer
            # wanted, Hermes is closing here): tell Hermes to stop the turn, as its own Stop does. Closing this
            # side alone leaves a step that already runs to run on inside a Hermes that stays (a
            # command would go on for its full time after the switch is off; with this it ends within a second).
            try:
                stop = {"session_id": session}
                if generation is not None:  # (without it a running Hermes answers "stale" and stops nothing)
                    stop["execution_generation"] = generation
                stop_id = call("session.interrupt", stop)
                end = time.monotonic() + 4
                while time.monotonic() < end:
                    try:
                        f = frames.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if f is None or f.get("id") == stop_id:
                        break
            except Exception:
                pass
        _end(proc)
        tasks.mark_turn(chats, False)
    if out is None:  # nothing was sent this way; why is worth a line in this computer's own Hermes log
        log.warning("grok-bot-twin: Hermes' interface (%s) did not start for a request: %s", dialect,
                 tasks.short(" | ".join(said[-2:]), 300) or "no reason given")
    return out


def _errors(stream: Any, said: list) -> None:
    try:
        for line in stream:
            line = line.strip()
            if line:
                said.append(line[:300])
                del said[:-5]
    except (OSError, ValueError):
        pass


def _end(proc: "subprocess.Popen") -> None:
    """Close Hermes' interface: its input first (it ends by itself on that), then the whole group if it lingers."""
    for step in ("close", "wait", "term", "wait", "kill"):
        try:
            if step == "close":
                time.sleep(0.3)  # (a last answer just written is read before the input closes)
                proc.stdin.close()
            elif step == "wait":
                proc.wait(timeout=4)
                return
            elif step == "term" and os.name != "nt":
                import signal
                os.killpg(proc.pid, signal.SIGTERM)
            elif step == "term":
                proc.terminate()
            elif os.name != "nt":
                import signal
                os.killpg(proc.pid, signal.SIGKILL)  # the whole group, not only the program that was started first
            else:
                proc.kill()
        except Exception:
            continue
