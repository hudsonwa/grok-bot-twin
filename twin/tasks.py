"""Ask a twin's copy on another of the owner's computers to do something ("do X on Surface").

This module is everything that is not a socket and not a Hermes run: the switch, the limits, the log, the
cleaning of what travels, and the plain sentences. The mailbox on the sharing computer is in tasks_hub.py, the
asking side and the worker that runs a request on the asked computer are in tasks_run.py.

The rules, each enforced here or named where it is:

- OFF by default. One switch per computer ("Let my other computers ask this one to do things"), kept in
  `tasks.json` in the plugin's data folder on that computer only. No other computer can flip it. While it is off
  this computer fetches no requests at all, and a request that still arrives is refused before anything runs.
- Only linked computers: a request rides the link that already exists (the sharing computer's server, bound to
  the Tailscale or home-network address only; the asked computer makes outbound connections only). Every call
  is signed with that computer's own key and every answer is signed back, as for the twin list; a recorded
  request cannot be sent again. There is no new listening socket and no new secret.
- A request is one chat turn for the twin's own Hermes bot on the asked computer, run by that computer's own
  Hermes with its own approval rules and tools (tasks_run.py passes no flag and sets no variable that changes
  them). A step that needs approval waits for the owner there; the asker is told "waiting for approval".
- Small and slow on purpose: at most REQUEST_CAP characters per request, REPLY_CAP per answer, RATE_PER_MIN /
  RATE_PER_HOUR requests per asking computer, one request at a time per computer, QUEUE_CAP waiting.
- What travels: the text the owner typed (password-like text masked before it leaves), the twin's opaque id,
  and back only the twin's final answer (control characters dropped, password-like text masked, capped) with a
  state and a duration. No transcript, no tool output, no file, no session id, no path of the plugin's own.
- A step that needs the owner's approval waits for the owner of the ASKED computer: the question Hermes asks
  (tasks_turn.py) is kept in `tasks-approvals.json` on that computer, shown on its Twins page, and answered only
  from there ("once" or "deny"). Neither the question nor the power to answer it ever travels.
- What happens when a step needs approval is that computer's own setting, in the same file as its switch and
  as local as the switch: "here" (the default, the rule above) or "safe" (such a step is refused at once).
  No route, no request and no other computer can set it; a request's own chat turn cannot either.
- Every request is written to this computer's log (`tasks-log.json`, the newest LOG_CAP): on the asking
  computer, on the asked computer, and on the sharing computer when it only passes one along. The Twins page
  shows the log next to the switch.
"""
from __future__ import annotations

import hmac
import json
import math
import logging
import os
import re
import secrets as _token
import sys
import threading
import time
from typing import Any, Optional

from . import fleet, fleet_io as fio, fleet_profiles as fp, store
from . import hermes_side as hs
from .engine import EngineError
from .model import REF_RE, now_dt, now_iso, parse_iso

log = logging.getLogger(__name__)

SWITCH_FILE = "tasks.json"
LOG_FILE = "tasks-log.json"
APPROVALS_FILE = "tasks-approvals.json"
TURNS_FILE = "tasks-turns.json"   # the chats that are a request's turn right now (see asked_turn)
TURN_MARK_S = 3600                # a mark left behind by a Hermes that ended is forgotten after this
CHOICES = ("once", "deny")   # what the owner of the asked computer may answer; never "for this session" or "always"
SWITCH_LABEL = "Let my other computers ask this one to do things"
# What this computer does when a step of a request needs approval. Chosen on this computer only (set_mode).
MODES = ("here", "safe")
MODE_LABEL = "When a step needs approval"
MODE_CHOICES = (
    ("here", "Ask on this computer", "A step waits until you answer it here."),
    ("safe", "Only safe requests", "A step that would need approval is refused at once. Requests that only read "
                                   "still work."),
)
MARK_RE = re.compile(r"[0-9a-f]{8}")
REQUEST_CAP = 2000        # characters of one request
REPLY_CAP = 6000          # characters of one answer
REPLY_WIRE_CAP = 12000    # bytes of the answer once it is JSON text (the server reads at most 16 KiB)
LOG_CAP = 200             # entries kept per computer
LOG_TEXT_CAP = 300        # characters of a request or an answer kept in the log
RATE_PER_MIN = 6
RATE_PER_HOUR = 40
QUEUE_CAP = 4             # requests waiting for one computer
RUN_LIMIT_S = 180         # one chat turn
APPROVAL_LIMIT_S = 300    # how long a step may wait for the owner's approval on the asked computer
TASK_TTL_S = 900          # a finished request is forgotten by the mailbox after this
ASK_WAIT_S = 100          # how long the asking chat waits before it says "still working"
TASK_ID_RE = re.compile(r"^k-[0-9a-f]{16}$")

# states a request moves through (the wire carries these words, never sentences)
OPEN_STATES = ("queued", "running", "waiting_approval")
END_STATES = ("done", "failed", "refused", "timeout", "cancelled")
STATES = OPEN_STATES + END_STATES
# why a request did not run or did not finish (fixed codes; each computer says them in its own words)
CODES = ("tasks_off", "tasks_unreachable", "unknown_machine", "ask_self", "no_twin_there", "too_long", "empty",
         "too_many", "busy", "not_linked", "task_timeout", "task_failed", "no_hermes", "approval_denied",
         "approval_timeout", "unknown_task", "switched_off", "stopped", "clock_off", "local_only", "passed_on",
         "step_changed", "not_from_a_request", "only_safe", "answer_owner_only")
# Set for the chat turn of a request that came from another computer (tasks_run.turn_env). Such a turn cannot ask
# a further computer and cannot answer an approval question, its own included.
ASKED_ENV = "GROK_BOT_TWIN_ASKED_TURN"

MESSAGES = {
    "tasks_off": ("{name} does not take requests from your other computers.",
                  "On {name}, open Twins and switch on \"" + SWITCH_LABEL + "\"."),
    "tasks_unreachable": ("Hermes could not reach {name}.",
                          "Check that {name} is on and that Hermes is open there."),
    "unknown_machine": ("None of your linked computers is called {asked}.", "Your computers: {names}."),
    "ask_self": ("{name} is this computer.", "Ask here directly, or name one of your other computers: {names}."),
    "no_twin_there": ("{name} holds no copy of this twin.", "Twins reach your other computers a few seconds after "
                                                           "they are linked."),
    "no_twin_here": ("Hermes cannot tell which twin to ask.", "Name one of your twins: {twins}."),
    "too_long": ("That request is too long to send.", "Say it in 2,000 characters or fewer."),
    "empty": ("There is nothing to ask.", "Say what the twin there should do."),
    "too_many": ("Too many requests to {name} in a short time.", "Try again in a minute."),
    "busy": ("{name} is still working on earlier requests.", "Try again in a minute."),
    "not_linked": ("This computer is not linked to another one.", "Link your computers on the Twins page first."),
    "task_timeout": ("{name} did not finish in time.", "Ask again, or look at the twin's chat on {name}."),
    "task_failed": ("The twin on {name} could not answer.", "Open the twin's chat on {name} to see why."),
    "no_hermes": ("Hermes could not start a chat with the twin on {name}.",
                  "Open Hermes on {name} once, then ask again."),
    "approval_denied": ("A step was not approved on {name}, so it was not done.", ""),
    "approval_timeout": ("A step waited for your approval on {name} and nobody answered, so it was not done.",
                         "Ask again and approve it on {name}."),
    "unknown_task": ("That request is no longer known.", "Ask again."),
    "switched_off": ("Requests were switched off on {name} while this one waited.", ""),
    "stopped": ("Hermes on {name} stopped before the request finished.", "Ask again."),
    "local_only": ("Requests to your other computers are sent only by you, from Hermes on this computer.",
                   "Type /twin ask yourself in a Hermes chat on this computer (not a messaging app), or run "
                   "hermes twin ask in a terminal of your own."),
    "passed_on": ("This request came from another of your computers, so it is not passed on to a third.",
                  "Ask that computer directly."),
    "step_changed": ("The step that waits for you is not the one you answered.",
                     "Look at it again, then answer."),
    "not_from_a_request": ("A request from another computer cannot approve steps on this one.",
                           "Answer on this computer's Twins page or in a terminal of your own."),
    "only_safe": ("{name} takes only safe requests, so a step that needed approval was refused at once.",
                  "To allow such steps, change \"" + MODE_LABEL + "\" on {name} (Twins page)."),
    "answer_owner_only": ("Only you can answer that step, not a chat.",
                          "Use this computer's Twins page or a terminal of your own."),
    "clock_off": ("The clocks of your computers differ by more than 15 minutes, so the request was not accepted.",
                  "Set the date and time to automatic on this computer and on the one with the Grok app."),
}


def message(code: str, **fmt: Any) -> dict:
    msg, hint = MESSAGES.get(code, MESSAGES["task_failed"])
    fmt.setdefault("name", "that computer")
    fmt.setdefault("asked", "that")
    fmt.setdefault("names", "none yet")
    fmt.setdefault("twins", "none yet")
    out = {"code": code if code in MESSAGES else "task_failed", "message": msg.format(**fmt)}
    if hint:
        out["hint"] = hint.format(**fmt)
    return out


class TaskError(EngineError):
    """A problem the local API and the tool answer as {"error": {...}}, in plain words."""

    def __init__(self, code: str, **fmt: Any) -> None:
        m = message(code, **fmt)
        super().__init__(m["code"], m["message"], m.get("hint", ""))


# ---- the label the asking chat shows ------------------------------------------------------------------------------------
def whole_seconds(value: Any) -> Optional[int]:
    """How long a request took, as a computer may believe it from another: a whole number from 0 to a day, or
    None (not a number, not finite, out of range)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) \
            or not 0 <= value <= 86400:
        return None
    return int(round(value))


def seconds_text(seconds: Any) -> str:
    try:
        s = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return ""
    return f"{s} s" if s < 120 else f"{s // 60} min"


def label(state: str, name: str, seconds: Any = None, code: Optional[str] = None) -> str:
    """One short line for the asking chat: "done on Surface - 12 s", "waiting for approval on Surface"."""
    name = fp.machine_name(name)
    took = seconds_text(seconds)
    if state == "done":
        tail = "; a step was not approved there" if code in ("approval_denied", "approval_timeout") else ""
        if code == "only_safe":
            tail = f"; a step was refused: {name} takes only safe requests"
        return (f"done on {name} - {took}" if took else f"done on {name}") + tail
    if state == "waiting_approval":
        return f"waiting for approval on {name}"
    if state == "queued":  # (it is busy with an earlier request, or has not looked yet)
        return f"waiting for {name} to pick it up"
    if state == "running":
        return f"still working on {name}"
    if code == "tasks_off":
        return f"not sent: requests are switched off on {name}"
    if code in ("too_many", "busy"):
        return f"not sent: too many requests to {name}"
    if code in ("tasks_unreachable", "stopped"):
        return f"not done: {name} could not be reached"
    if code in ("approval_denied", "approval_timeout"):
        return f"not done on {name}: a step was not approved there"
    why = OUT_REASONS.get(code or "")
    return f"not done on {name}: {why.format(name=name)}" if why else f"not done on {name}"


# Why a request was not done, as the ASKING computer's chat line and list say it. The asked computer's own list
# says the same about "here" (IN_REASONS). A bare "not done on NAME" left the owner with no reason and no next
# step, although both computers knew the reason. One short line: no brackets, no id, nothing of the request.
OUT_REASONS = {
    "task_failed": "its twin there could not answer; its chat on {name} says why",
    "no_hermes": "Hermes could not start a chat with the twin there; open Hermes on {name} once",
    "task_timeout": "its twin there did not finish in time; its chat on {name} shows how far it got",
    "switched_off": "requests were switched off there while it waited",
    "no_twin_there": "{name} holds no copy of this twin yet",
    "clock_off": "your computers' clocks differ by more than 15 minutes",
}


# ---- cleaning what travels ----------------------------------------------------------------------------------------------
def clean_request(value: Any) -> str:
    """The request as it may leave this computer and as the asked computer reads it again: control characters
    dropped (new lines kept), password-like text masked, capped. Empty when there is nothing left."""
    if not isinstance(value, str):
        return ""
    return _NOTE_OPENING.sub(lambda m: "(" + m.group(0)[1:], hs.shown(value, REQUEST_CAP))


# How the plugin's own notes in a chat open and close ("[Grok Bot Twin: ...]", "[end Grok Bot Twin]", "[Sent from
# your computer ...]"). In a request these words keep their meaning but lose the bracket, so that nothing a
# request says can pass for a note of the plugin's (or close the real one that follows it).
_NOTE_OPENING = re.compile(r"(?i)\[\s*(?:(?:end\s+)?grok\s+bot\s+twin|sent\s+from\s+your\s+computer)")


def _fit(text: str, chars: int, wire: int) -> str:
    text = text[:chars]
    while len(json.dumps(text)) > wire:
        text = text[: max(1, len(text) * 3 // 4)]
    return text


CUT_NOTE = "\n[cut: the answer was longer]"


def clean_reply(value: Any) -> str:
    """An answer as it may leave the asked computer and as the asking computer reads it again: the same filter,
    capped in characters and in bytes on the wire. A cut answer says so. Cleaning twice changes nothing."""
    if not isinstance(value, str):
        return ""
    text = hs.shown(value[: REPLY_CAP * 8], REPLY_CAP * 8)
    if len(value) <= REPLY_CAP * 8 and _fit(text, REPLY_CAP, REPLY_WIRE_CAP) == text:
        return text
    return _fit(text, REPLY_CAP - len(CUT_NOTE), REPLY_WIRE_CAP - 64).rstrip() + CUT_NOTE


def short(value: Any, cap: int = LOG_TEXT_CAP) -> str:
    text = " ".join(hs.shown(value, cap * 2).split()) if isinstance(value, str) else ""
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


_PATH_TOKEN = re.compile(r"""(?<![^\s"'=(,;|&<>])(?:~|\.{1,2}|[A-Za-z]:)?[/\\][^\s"'|&;<>()]*""")
_PATH_SEP = re.compile(r"([/\\])")
_SECRET_NAME_BEFORE = re.compile(r"""(?i)(?:key|token|secret|password|passwd|bearer)\s*[:=]?\s*["']?$""")
_HELD = re.compile("\u27e6(\\d+)\u27e7")


STEP_CAP = 400            # characters of a step as the owner is shown it before answering


def step_text(value: Any, cap: int = STEP_CAP) -> str:
    """The step Hermes asks about, as the owner of this computer reads it before answering. Password-like text
    is hidden as everywhere else, but a file path is shown: as one word, a long path with digits in it looks
    random to that filter, and an owner asked to approve "rm -rf [not shown]" cannot decide. Each
    part of a path is still checked by itself, and what follows a word like `token=` is never taken for a path.
    This text stays on this computer (the asking computer only hears that a step waits)."""
    if not isinstance(value, str):
        return ""
    value = value[: cap * 4].replace("\u27e6", "(").replace("\u27e7", ")")
    held: list = []

    def hold(m: "re.Match") -> str:
        if _SECRET_NAME_BEFORE.search(value[max(0, m.start() - 24):m.start()]):
            return hs.HIDDEN_LINE if len(m.group(0)) >= 12 else m.group(0)  # a value, whatever it looks like
        held.append("".join(p if p in "/\\" else hs.shown(p, cap * 2) for p in _PATH_SEP.split(m.group(0))))
        return "\u27e6%d\u27e7" % (len(held) - 1)

    text = " ".join(hs.shown(_PATH_TOKEN.sub(hold, value), cap * 4).split())
    text = _HELD.sub(lambda m: held[int(m.group(1))] if int(m.group(1)) < len(held) else "", text)
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def step_whole(value: Any, cap: int = STEP_CAP) -> bool:
    """Can the owner of this computer read ALL of the step before answering? False for a step longer than the
    page and the terminal show. Such a step is refused at once, never offered with its end cut off: a command
    can be padded so that the part that matters lies behind the cut, and "Do it once" would then approve
    something the owner never saw. (A step that is no text at all is shown by Hermes' own name for it.)"""
    if not isinstance(value, str):
        return True
    # Whole means: what is shown is ALL of it. step_text reads only the first `cap * 4` characters of a step, so
    # a longer one is never whole, however short it looks once blanks are folded (a command padded with two
    # thousand spaces showed as its harmless beginning, with no sign of a cut); and the text as shown must be
    # the text cleaned without any limit.
    if len(value) > cap * 4:
        return False
    full = step_text(value, max(cap, len(value)) + 16)
    return len(full) <= cap and step_text(value, cap) == full


def new_id() -> str:
    return "k-" + _token.token_hex(8)


# ---- the switch ---------------------------------------------------------------------------------------------------------
def _switch_path():
    return store.central_dir() / SWITCH_FILE


def switch() -> dict:
    """{"on": bool, "since": iso|None}. Anything but a stored `true` is off: a missing, unreadable or damaged
    file means off."""
    raw = fio.read_json(_switch_path(), None)
    on = isinstance(raw, dict) and raw.get("on") is True
    since = raw.get("since") if on and isinstance(raw.get("since"), str) and parse_iso(raw.get("since")) else None
    return {"on": on, "since": since}


def link_kind(st: dict) -> Optional[str]:
    """What carries this computer's link to the sharing computer: "tailscale", "loopback" (the same computer),
    "lan" (a plain home network), or None when it has no link. Requests work over each; over a plain home
    network they are signed (nobody there can make or repeat one) but readable, and the page says so."""
    r = fleet.role(st)
    if r == "follower":
        return fleet.net.kind_of(st["link"].get("host"))
    if r == "source":
        sh = st.get("share") or {}
        inst = sh.get("instance") if isinstance(sh.get("instance"), dict) else {}
        return fleet.net.kind_of(inst.get("address") or sh.get("address"))
    return None


def mode() -> str:
    """What this computer does when a step of a request needs approval: "here" or "safe". Read from this
    computer's own file every time. Anything but a stored "safe" is "here": a missing, unreadable or damaged
    file, and a "linked" stored by an older version (approving from another computer, removed), all mean the
    owner answers on this computer."""
    try:
        raw = fio.read_json(_switch_path(), None)
    except Exception:
        return "here"
    value = raw.get("mode") if isinstance(raw, dict) else None
    return "safe" if value == "safe" else "here"


def mode_view() -> dict:
    return {"value": mode(), "label": MODE_LABEL,
            "choices": [{"value": v, "label": lab, "note": note} for v, lab, note in MODE_CHOICES]}


def set_mode(value: Any, own_page: bool = False) -> str:
    """The owner of THIS computer chooses the mode. There is no other way to set it: no route of the share
    server and no request carries it. Refused inside the chat turn of a request from another computer (a
    request must not be able to loosen the rule its own steps wait under).
    `own_page`: called by this computer's Twins page (see decide)."""
    if not isinstance(value, str) or value not in MODES:
        raise ValueError("mode must be here or safe")
    if asked_turn(env=not own_page):
        raise TaskError("not_from_a_request")
    with fleet.locked():
        cur = switch()
        if mode() != value:
            doc: dict = {"on": cur["on"], "since": cur["since"]}
            if value != "here":
                doc["mode"] = value
            fio.write_json(_switch_path(), doc)
    return mode()


def accepting() -> bool:
    try:
        return switch()["on"]
    except Exception:
        return False


def set_switch(on: bool) -> dict:
    if on is not True and on is not False:
        raise ValueError("on must be true or false")
    with fleet.locked():
        cur = switch()
        if cur["on"] != on:
            doc: dict = {"on": on, "since": now_iso() if on else None}
            if mode() != "here":
                doc["mode"] = mode()  # the mode is the owner's standing choice: the switch does not change it
            fio.write_json(_switch_path(), doc)
    return switch()


def link_dropped() -> None:
    """This computer's link is gone (it unlinked itself, or the sharing computer removed it): its switch is off
    again and what it heard about the other computers' switches is forgotten, so a later link starts as a first
    one does, with requests off until the owner says otherwise. Called from fleet.drop_link with the store's
    lock held. Never raises."""
    try:
        if switch()["on"]:
            # the stricter "Only safe requests" stays (an unlink, which the sharing computer can cause, never
            # loosens anything)
            doc: dict = {"on": False, "since": None}
            if mode() == "safe":
                doc["mode"] = "safe"
            fio.write_json(_switch_path(), doc)
        if heard():
            fio.write_json(_heard_path(), {"machines": {}})
    except Exception:
        log.debug("grok-bot-twin: could not switch requests off after the unlink", exc_info=True)


# ---- what this computer last heard about the other computers' switches -----------------------------------------------------
HEARD_FILE = "tasks-heard.json"   # {"machines": {machine id: true|false}}; a computer that is not in it: not known


def _heard_path():
    return store.central_dir() / HEARD_FILE


def clean_heard(raw: Any) -> dict:
    """{machine id: bool} from whatever was sent or stored: ids of the fleet's own kind and plain true / false
    only, at most one per computer a fleet can hold."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for mid, on in list(raw.items())[:64]:
        if isinstance(mid, str) and fleet.MACHINE_ID_RE.match(mid) and isinstance(on, bool):
            out[mid] = on
        if len(out) > fleet.MAX_MACHINES:
            break
    return out


def heard() -> dict:
    raw = fio.read_json(_heard_path(), None)
    return clean_heard(raw.get("machines")) if isinstance(raw, dict) else {}


def set_heard(machines: Any) -> bool:
    """Keep what was heard (the sharing computer: from its own mailbox; a linked computer: from the sharing
    computer's signed answer). Written only when it changed. Never raises."""
    try:
        new = clean_heard(machines)
        if new == heard():
            return False
        with fleet.locked():
            fio.write_json(_heard_path(), {"machines": new})
        return True
    except Exception:
        log.debug("grok-bot-twin: could not note which computers take requests", exc_info=True)
        return False


def takes_requests(st: dict) -> dict:
    """{machine id: True | False | None} for the owner's OTHER computers: whether each one's own switch was on
    when this computer last heard. None: not known (never heard, or not heard lately).

    - The sharing computer hears it first hand: a linked computer fetches requests only while its switch is on.
      One that keeps its twins in step and fetches nothing has it off (the default).
    - A linked computer hears it from the sharing computer's signed answers (`takes_requests`), and believes it
      only while it is in sync with that computer."""
    r = fleet.role(st)
    out: dict = {}
    try:
        if r == "source":
            from . import fleet_server, tasks_hub
            live = tasks_hub.hub.picture(st) if fleet_server.serving() is not None else None
            known = heard() if live is None else live
            for mid, f in ((st.get("share") or {}).get("followers") or {}).items():
                seen = parse_iso(f.get("last_seen_at")) if isinstance(f, dict) else None
                fresh = seen is not None and (now_dt() - seen).total_seconds() <= fleet.OFFLINE_AFTER_S
                out[mid] = known.get(mid) if (live is not None or fresh) else None
        elif r == "follower" and st["link"].get("state") == "in_sync":
            out = dict(heard())
    except Exception:
        log.debug("grok-bot-twin: could not tell which computers take requests", exc_info=True)
    return out


# ---- the log ------------------------------------------------------------------------------------------------------------
_log_lock = threading.RLock()
LOG_KEYS = ("id", "dir", "peer", "peer_to", "twin", "text", "state", "code", "asked_at", "ended_at", "seconds",
            "reply", "answered")


def _log_path():
    return store.central_dir() / LOG_FILE


def read_log() -> list:
    raw = fio.read_json(_log_path(), None)
    rows = raw.get("entries") if isinstance(raw, dict) else None
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)][-LOG_CAP:] \
        if isinstance(rows, list) else []


def log_entry(direction: str, task_id: str, peer: str, twin: str = "", text: str = "", state: str = "queued",
              peer_to: Optional[str] = None, code: Optional[str] = None) -> None:
    """Write a new line of this computer's log. `direction`: "out" (asked from here), "in" (asked of this
    computer) or "relay" (passed along by the sharing computer; no text is kept for those). Never raises."""
    row = {"id": task_id, "dir": direction, "peer": fp.machine_name(peer), "peer_to": fp.machine_name(peer_to)
           if peer_to else None, "twin": short(twin, 100), "text": "" if direction == "relay" else short(text),
           "state": state if state in STATES else "queued", "code": code if code in CODES else None,
           "asked_at": now_iso(), "ended_at": now_iso() if state in END_STATES else None, "seconds": None,
           "reply": ""}
    try:
        with _log_lock, fleet.locked():
            rows = [r for r in read_log() if not (r.get("id") == task_id and r.get("dir") == direction)]
            rows.append(row)
            fio.write_json(_log_path(), {"entries": rows[-LOG_CAP:]})
    except Exception:
        log.debug("grok-bot-twin: could not write the request log", exc_info=True)


def log_missed(rows: Any) -> int:
    """Another computer asked this one to do something while its switch was off: the sharing computer says who
    and when (never what), and this computer's own log gets a line for each. Everything is cleaned again here.
    Returns how many lines were written. Never raises."""
    n = 0
    for row in rows[:20] if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("from"), str):
            continue
        tid = row.get("id") if isinstance(row.get("id"), str) and TASK_ID_RE.match(row["id"]) else new_id()
        log_entry("in", tid, row["from"], state="refused", code="tasks_off")
        n += 1
    return n


def log_update(direction: str, task_id: str, state: str, code: Optional[str] = None, seconds: Any = None,
               reply: Optional[str] = None) -> None:
    """Note how a logged request went on. Unknown ids are ignored; an ended entry is not reopened. Never raises."""
    try:
        with _log_lock, fleet.locked():
            rows = read_log()
            for r in rows:
                if r.get("id") != task_id or r.get("dir") != direction:
                    continue
                if r.get("state") in END_STATES or r.get("state") == state and r.get("code") == code:
                    return
                r["state"] = state if state in STATES else r.get("state")
                r["code"] = code if code in CODES else None
                if state in END_STATES:
                    r["ended_at"] = now_iso()
                    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
                        r["seconds"] = max(0, int(round(seconds)))
                    if reply and direction != "relay":
                        r["reply"] = short(reply)
                fio.write_json(_log_path(), {"entries": rows[-LOG_CAP:]})
                return
    except Exception:
        log.debug("grok-bot-twin: could not update the request log", exc_info=True)


def log_view(limit: int = 50) -> list:
    """The log for the page and the terminal: newest first, closed shape, every text cleaned again."""
    out = []
    waiting = pending()
    for r in reversed(read_log()):
        d = r.get("dir") if r.get("dir") in ("in", "out", "relay") else "in"
        state = r.get("state") if r.get("state") in STATES else "failed"
        code = r.get("code") if r.get("code") in CODES else None
        peer = fp.machine_name(r.get("peer"))
        peer_to = fp.machine_name(r.get("peer_to")) if r.get("peer_to") else None
        where = peer_to if d == "relay" else (peer if d == "out" else None)
        out.append({"id": r["id"] if TASK_ID_RE.match(r["id"]) else "k-0000000000000000", "dir": d, "peer": peer,
                    "peer_to": peer_to, "twin": short(r.get("twin"), 100), "text": short(r.get("text")),
                    "state": state, "code": code,
                    "asked_at": r.get("asked_at") if parse_iso(r.get("asked_at")) else None,
                    "ended_at": r.get("ended_at") if parse_iso(r.get("ended_at")) else None,
                    "seconds": r.get("seconds") if isinstance(r.get("seconds"), int) else None,
                    "reply": short(r.get("reply")),
                    "label": label(state, where, r.get("seconds"), code) if where else in_label(state, code),
                    "approval": waiting.get(r["id"]) if d == "in" and state == "waiting_approval" else None})
        if len(out) >= limit:
            break
    return out


def in_label(state: str, code: Optional[str] = None) -> str:
    """The same line as `label`, for a request this computer was asked to do."""
    if state == "done" and code == "only_safe":
        return "done here; a step was refused: only safe requests are taken here"
    if state == "done":
        return "done here; a step was not approved" if code in ("approval_denied", "approval_timeout") else "done here"
    if state == "waiting_approval":
        return "waiting for your approval here"
    if state in ("queued", "running"):
        return "working on it here"
    if state == "cancelled" and code == "switched_off":  # it had started
        return "stopped: requests were switched off here"
    if state == "cancelled" and code == "unknown_task":
        return "stopped: the asking computer no longer waits for it"
    if code in ("tasks_off", "switched_off"):
        return "refused: requests are switched off here"
    if code in ("too_many", "busy"):
        return "refused: too many requests"
    if code in ("approval_denied", "approval_timeout"):
        return "not done: a step was not approved here"
    return IN_REASONS.get(code or "", "not done here")


# Why a request was not done, as the ASKED computer's own list says it. The asker is told the same reason about
# "NAME" (MESSAGES); here it is about "here", so the owner of this computer need not guess ("Not done here" alone
# left them guessing). One short line each: no brackets, no id, nothing of the request itself.
IN_REASONS = {
    "task_failed": "not done: the twin here could not answer; open its chat to see why",
    "no_hermes": "not done: Hermes could not start a chat with the twin here",
    "task_timeout": "not done: the twin here did not finish in time",
    "stopped": "not done: Hermes here stopped before the request finished",
    "no_twin_there": "refused: this computer holds no copy of that twin",
    "unknown_machine": "refused: this computer does not know the computer that asked",
    "too_long": "refused: the request was too long",
    "empty": "refused: the request was empty",
    "clock_off": "refused: the clocks of your computers differ by more than 15 minutes",
    "passed_on": "refused: a request from another computer is not passed on to a third",
}


def close_stale(direction: str = "in", older_than_s: float = 0.0) -> int:
    """Requests this computer was working on when its Hermes process ended can never finish: mark them stopped
    (called when a worker starts, which is the only process that could still be running them). Returns how many."""
    n = 0
    try:
        with _log_lock, fleet.locked():
            rows = read_log()
            for r in rows:
                if r.get("dir") != direction or r.get("state") not in OPEN_STATES:
                    continue
                asked = parse_iso(r.get("asked_at"))
                if asked is not None and (now_dt() - asked).total_seconds() < older_than_s:
                    continue
                r.update(state="cancelled", code="stopped", ended_at=now_iso())
                n += 1
            if n:
                fio.write_json(_log_path(), {"entries": rows[-LOG_CAP:]})
            if n and direction == "in":
                fio.write_json(_approvals_path(), {"pending": {}, "decisions": {}})
    except Exception:
        log.debug("grok-bot-twin: could not close old requests", exc_info=True)
    return n


# ---- a step that waits for the owner's approval on this computer -----------------------------------------------------------
def _approvals_path():
    return store.central_dir() / APPROVALS_FILE


def _approvals() -> dict:
    raw = fio.read_json(_approvals_path(), None)
    out = {"pending": {}, "decisions": {}}
    if isinstance(raw, dict):
        for key in out:
            if isinstance(raw.get(key), dict):
                out[key] = {k: v for k, v in raw[key].items()
                            if isinstance(k, str) and TASK_ID_RE.match(k) and isinstance(v, dict)}
    return out


def set_pending(task_id: str, question: Optional[dict]) -> Optional[dict]:
    """Remember (or, with None, forget) the question Hermes asks about one request. An older answer for that
    request is dropped either way, so an answer always belongs to the question that is open. Returns the
    question as kept ({"command", "description", "q"}), or None. Never raises."""
    kept = None
    try:
        with _log_lock, fleet.locked():
            doc = _approvals()
            doc["decisions"].pop(task_id, None)
            if question is None:
                doc["pending"].pop(task_id, None)
            else:
                # `q` names this very question: an answer that says which question it is for (the page and the
                # terminal pass back what they showed) cannot land on a later one of the same request
                kept = {"command": step_text(question.get("command")),
                        "description": short(question.get("description")), "asked_at": now_iso(),
                        "q": _token.token_hex(4)}
                doc["pending"][task_id] = kept
            fio.write_json(_approvals_path(), doc)
    except Exception:
        log.debug("grok-bot-twin: could not note the approval question", exc_info=True)
        return None
    return dict(kept) if kept else None


def pending() -> dict:
    """{task id: {"command", "description", "asked_at", "q"}} of the steps that wait for this computer's owner.
    A step the owner has answered is not listed any more (Hermes is told within half a second)."""
    out = {}
    doc = _approvals()
    for tid, q in doc["pending"].items():
        if tid in doc["decisions"]:
            continue
        out[tid] = {"command": step_text(q.get("command")), "description": short(q.get("description")),
                    "asked_at": q.get("asked_at") if parse_iso(q.get("asked_at")) else None,
                    "q": q["q"] if isinstance(q.get("q"), str) and MARK_RE.fullmatch(q["q"]) else ""}
    return out


# ---- is this the chat turn of a request? -----------------------------------------------------------------------------------
SESSION_VARS = ("HERMES_SESSION_ID", "HERMES_SESSION_KEY", "HERMES_UI_SESSION_ID")


def _turns_path():
    return store.central_dir() / TURNS_FILE


def _marked_chats() -> dict:
    raw = fio.read_json(_turns_path(), None)
    rows = raw.get("chats") if isinstance(raw, dict) else None
    out = {}
    for sid, at in (rows.items() if isinstance(rows, dict) else ()):
        when = parse_iso(at)
        if isinstance(sid, str) and 8 <= len(sid) <= 200 and when is not None \
                and (now_dt() - when).total_seconds() < TURN_MARK_S:
            out[sid] = at
    return out


def mark_turn(chats: Any, on: bool) -> None:
    """Note (or forget) the Hermes chats that are the turn of a request from another computer. When a turn runs
    inside the Hermes that was already running, the variable ASKED_ENV cannot mark it (that Hermes was started
    long before); its chat's own number can, because Hermes hands that number to everything the chat starts.
    Never raises."""
    ids = [c for c in (chats or []) if isinstance(c, str) and 8 <= len(c) <= 200]
    if not ids:
        return
    try:
        with _log_lock, fleet.locked():
            rows = _marked_chats()
            for c in ids:
                if on:
                    rows[c] = now_iso()
                else:
                    rows.pop(c, None)
            _write_turns(rows, _single_since())
    except Exception:
        log.debug("grok-bot-twin: could not mark the request's chat", exc_info=True)


def _write_turns(chats: dict, single: Optional[str]) -> None:
    doc: dict = {"chats": dict(list(chats.items())[-50:])}
    if single:
        doc["single"] = single
    fio.write_json(_turns_path(), doc)


def _single_since() -> Optional[str]:
    """Since when a request's turn runs here in Hermes' single-question mode, or None. Forgotten by itself a
    little after the longest time such a turn may take."""
    raw = fio.read_json(_turns_path(), None)
    at = raw.get("single") if isinstance(raw, dict) else None
    when = parse_iso(at) if isinstance(at, str) else None
    if when is None or not 0 <= (now_dt() - when).total_seconds() < RUN_LIMIT_S + 60:
        return None
    return at


def mark_single(on: bool) -> None:
    """Note (or forget) that a request's turn runs here in Hermes' single-question mode. That turn has no chat
    number the plugin could know beforehand and no process of its own that a variable could mark for sure, so
    while it runs NO chat on this computer may pass a request on or answer an approval question (the owner's
    page and own terminal, which are not chats, still may). Never raises."""
    try:
        with _log_lock, fleet.locked():
            _write_turns(_marked_chats(), now_iso() if on else None)
    except Exception:
        log.debug("grok-bot-twin: could not mark the request's turn", exc_info=True)


def _chat_numbers(extra: Any = (), env: bool = True) -> list:
    """The numbers Hermes gives the chat this code runs in: from Hermes' own per-chat values when this process
    is Hermes (looked up, never imported), and from the environment (what a command started by a chat gets).

    `env=False` is for a call that is not a chat and runs INSIDE the long-running Hermes: the Twins page's own
    route. That process's environment is not the caller's: Hermes keeps the number of whichever chat is working
    there in it (HERMES_SESSION_ID, for the commands that chat starts), a request's turn included. So there only
    a value Hermes bound to this very call counts, and one that merely repeats the environment does not."""
    found = [x for x in extra if isinstance(x, str) and x]
    ctx = sys.modules.get("gateway.session_context")
    for name in SESSION_VARS:
        value = ""
        held = os.environ.get(name, "")
        if ctx is not None:
            try:
                value = ctx.get_session_env(name, "") or ""
            except Exception:
                value = ""
        if env:
            value = value or held
        elif value == held:
            value = ""  # Hermes' lookup falls back to the environment: that is the process's, not this call's
        if isinstance(value, str) and value:
            found.append(value)
    return found


def asked_turn(extra: Any = (), in_chat: bool = False, env: bool = True) -> bool:
    """Is this code running in (or started by) the chat turn of a request from another computer? Such a turn
    must not ask a further computer and must not answer an approval question, its own included. `in_chat`:
    the caller knows it runs inside some chat (a tool call) even when Hermes gives no chat number. `env=False`:
    the caller is the Twins page's route inside the running Hermes (see _chat_numbers)."""
    if os.environ.get(ASKED_ENV):
        return True
    numbers = _chat_numbers(extra, env)
    if not numbers and not in_chat:
        return False
    try:
        if _single_since():
            return True  # a single-question turn of a request runs here: it could be any chat (see mark_single)
        marked = _marked_chats()
    except Exception:
        return False
    return any(sid in number for sid in marked for number in numbers)


def decide(task_id: Any, choice: Any, q: Optional[str] = None, own_page: bool = False) -> None:
    """The owner of THIS computer answers the open question of one request: "once" or "deny". `q` is the mark
    of the question the owner was shown (`waiting[i].q`): when given, the answer counts only for that very
    question, never for a later step of the same request that the owner has not seen. Raises TaskError when
    there is no such open question, ValueError for any other choice.

    Refused inside the chat turn of a request that came from another computer: a request must not be able to
    approve its own step through the plugin's own commands. `own_page`: the answer comes from this computer's
    Twins page (its route in the running Hermes, behind Hermes' own sign-in), which is no chat: the number of
    the chat that happens to be working in that Hermes, the waiting request's own, must not refuse the owner."""
    if choice not in CHOICES:
        raise ValueError("choice must be once or deny")
    if asked_turn(env=not own_page):
        raise TaskError("not_from_a_request")
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise TaskError("unknown_task")
    with _log_lock, fleet.locked():
        doc = _approvals()
        if task_id not in doc["pending"]:
            raise TaskError("unknown_task")
        if q is not None and not (isinstance(q, str) and q and hmac.compare_digest(
                q.encode("utf-8"), str(doc["pending"][task_id].get("q") or "").encode("utf-8"))):
            raise TaskError("step_changed")
        doc["decisions"][task_id] = {"choice": choice, "at": now_iso()}
        fio.write_json(_approvals_path(), doc)


def decision(task_id: str) -> Optional[str]:
    """The owner's answer for the open question of this request, or None. Read by the worker twice a second."""
    try:
        doc = _approvals()
        d = doc["decisions"].get(task_id)
        if task_id in doc["pending"] and isinstance(d, dict) and d.get("choice") in CHOICES:
            return d["choice"]
    except Exception:
        pass
    return None


# ---- limits ---------------------------------------------------------------------------------------------------------------
class Rate:
    """At most `per_min` in any minute and `per_hour` in any hour, per key. In memory; a restart forgets."""

    def __init__(self, per_min: Optional[int] = None, per_hour: Optional[int] = None) -> None:
        self.per_min, self.per_hour = per_min, per_hour
        self._seen: dict = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        per_min = RATE_PER_MIN if self.per_min is None else self.per_min
        per_hour = RATE_PER_HOUR if self.per_hour is None else self.per_hour
        with self._lock:
            times = [t for t in self._seen.get(key, []) if now - t < 3600]
            ok = len(times) < per_hour and len([t for t in times if now - t < 60]) < per_min
            if ok:
                times.append(now)
            self._seen[key] = times
            if len(self._seen) > 64:
                for k in [k for k, v in self._seen.items() if not v]:
                    del self._seen[k]
            return ok


# ---- who is who -----------------------------------------------------------------------------------------------------------
def machines(st: dict) -> list:
    """The computers of the fleet as this one knows them: [{"id", "name", "role", "this"}], this one included."""
    r = fleet.role(st)
    me = fleet.this_machine(st)
    if r == "source":
        rows = fleet.fleet_members(st)
    elif r == "follower":
        rows = [m for m in st["link"].get("fleet", []) if isinstance(m, dict)]
        if not any(m.get("id") == st["link"]["source"].get("id") for m in rows):
            rows = [{**st["link"]["source"], "role": "source"}] + rows
        if not any(m.get("id") == me["id"] for m in rows):
            rows.append({**me, "role": "follower"})
    else:
        return []
    out = []
    for m in rows:
        cm = fleet.clean_machine(m)
        if cm:
            out.append({"id": cm["id"], "name": me["name"] if cm["id"] == me["id"] else cm["name"],
                        "role": m.get("role") if m.get("role") in ("source", "follower") else "follower",
                        "this": cm["id"] == me["id"]})
    return out


def _fold(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower()) if isinstance(text, str) else ""


def find_machine(st: dict, asked: Any) -> dict:
    """The computer the owner named: its id, its exact name, its name without case, spaces and dashes, or a
    beginning or a part of at least four characters that fits exactly one computer ("surface" for
    "Demo-Surface-Tablet"). Anything that fits none or several is refused with the list of names.
    Raises TaskError."""
    rows = machines(st)
    if not rows:
        raise TaskError("not_linked")
    others = [m for m in rows if not m["this"]]
    names = ", ".join(m["name"] for m in others) or "none yet"
    asked_s = asked.strip() if isinstance(asked, str) else ""
    want = _fold(asked_s)
    hits = [m for m in rows if m["id"] == asked_s or m["name"] == asked_s]
    if not hits and want:
        hits = [m for m in rows if _fold(m["name"]) == want]
    if not hits and len(want) >= 3:
        hits = [m for m in rows if _fold(m["name"]).startswith(want)]
    if not hits and len(want) >= 4:  # "surface" for "Demo-Surface-Tablet", when only one computer fits
        hits = [m for m in rows if want in _fold(m["name"])]
    if len(hits) != 1:
        # (what was typed is echoed in a computer name's plain characters only)
        raise TaskError("unknown_machine", asked=fp.machine_name(asked_s, "that"), names=names)
    if hits[0]["this"]:
        raise TaskError("ask_self", name=hits[0]["name"], names=names)
    return hits[0]


def twins_here(st: dict) -> dict:
    """The twins this computer holds, by id: {"t-…": {"name", "profile"}} (profile None when it has none)."""
    r = fleet.role(st)
    if r == "source":
        rows = st["share"]["list"]["twins"]
    elif r == "follower":
        rows = st["link"]["twins"]
    else:
        return {}
    out = {}
    for tid, t in rows.items():
        if isinstance(tid, str) and REF_RE.match(tid) and isinstance(t, dict):
            prof = t.get("profile")
            out[tid] = {"name": fleet.clean_text(t.get("name"), fleet.NAME_CAP, True) or "Twin",
                        "profile": prof if isinstance(prof, str) and fp.PROFILE_ID_RE.match(prof) else None}
    return out


def find_twin(st: dict, asked: Any, profile: Optional[str]) -> tuple:
    """(twin id, name) of the twin to ask: the one the owner named, else the twin whose Hermes bot this chat is
    with, else the only twin there is. Raises TaskError."""
    twins = twins_here(st)
    listed = ", ".join(sorted(t["name"] for t in twins.values())[:20]) or "none yet"
    asked_s = asked.strip() if isinstance(asked, str) else ""
    if asked_s:
        want = _fold(asked_s)
        hits = [tid for tid, t in twins.items() if tid == asked_s or t["name"] == asked_s] or \
            [tid for tid, t in twins.items() if want and _fold(t["name"]) == want]
        if len(hits) == 1:
            return hits[0], twins[hits[0]]["name"]
        raise TaskError("no_twin_here", twins=listed)
    mine = [tid for tid, t in twins.items() if profile and t["profile"] == profile]
    if len(mine) == 1:
        return mine[0], twins[mine[0]]["name"]
    if len(twins) == 1:
        tid = next(iter(twins))
        return tid, twins[tid]["name"]
    raise TaskError("no_twin_here", twins=listed)
