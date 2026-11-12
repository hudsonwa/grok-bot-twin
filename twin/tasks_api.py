"""What the Twins page, `/twin` and `hermes twin` call for requests between the owner's computers.

`TasksMixin` adds to TwinService: the view (the switch, the other computers, the log), the switch itself, asking
another computer, and looking at a request again. Also here: the terminal commands `hermes twin ask` / `hermes
twin requests`. There is no model tool for this: only the owner asks another computer (Twins page, `/twin ask` in
a chat on this computer, `hermes twin ask`), never a model on its own. Rules and limits: tasks.py. The mailbox: tasks_hub.py. Running a request: tasks_run.py.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from . import config, fleet, tasks, tasks_run
from .model import ago as _ago
from .connect import BadInput
from .engine import EngineError

log = logging.getLogger(__name__)

BAD_REQUEST = "That request was not understood."
HOME_NETWORK_NOTE = ("Your computers are linked over your home network: what is asked and what comes back can be "
                     "read by others on that network.")
def _get_service() -> Any:
    from .service import get_service
    return get_service()


class TasksMixin:
    """Mixed into TwinService."""

    def tasks(self, refresh: bool = True) -> dict:
        """The whole picture for this computer: the switch, who could ask or be asked, and the log."""
        st = fleet.load() if fleet.exists() else fleet._blank()
        r = fleet.role(st)
        if refresh and r != "none":
            tasks_run.refresh_open(self)
            tasks_run.ensure_started(_get_service)
        sw = tasks.switch()
        me = fleet.this_machine(st)
        takes = tasks.takes_requests(st)  # what this computer last heard of each one's own switch (None: not known)
        others = [{"id": m["id"], "name": m["name"], "takes_requests": takes.get(m["id"])}
                  for m in tasks.machines(st) if not m["this"]]
        rows = tasks.log_view()
        return {"switch": {"on": sw["on"], "since": sw["since"], "label": tasks.SWITCH_LABEL,
                           # what carries this computer's link: "tailscale" | "lan" | "loopback" | None. On
                           # "lan" requests and answers are signed but can be read on that network.
                           "network": tasks.link_kind(st)},
                "linked": r != "none", "this_machine": {"name": me["name"]}, "machines": others,
                "working": sw["on"] and r != "none" and tasks_run.running(),
                # steps that wait for the owner of THIS computer, each with the question Hermes asks
                "waiting": [{"id": x["id"], "peer": x["peer"], "twin": x["twin"], "text": x["text"],
                             "command": x["approval"]["command"], "description": x["approval"]["description"],
                             "asked_at": x["approval"]["asked_at"], "q": x["approval"]["q"]}
                            for x in rows if x["approval"]],
                # what THIS computer does when a step needs approval (chosen here only)
                "mode": tasks.mode_view(),
                "log": rows,
                "limits": {"request_chars": tasks.REQUEST_CAP, "reply_chars": tasks.REPLY_CAP,
                           "per_minute": tasks.RATE_PER_MIN, "per_hour": tasks.RATE_PER_HOUR}}

    def tasks_switch(self, on: Any) -> dict:
        if on is not True and on is not False:
            raise BadInput(BAD_REQUEST)
        was = tasks.switch()["on"]
        tasks.set_switch(on)
        tasks_run.switch_changed(_get_service)
        if was != on:
            fleet.hub.bump()  # (on the sharing computer: the linked computers hear of its switch at once)
            self.emit("tasks", None, {"what": "switch_on" if on else "switch_off", "count": 1})
        return self.tasks(refresh=False)

    def tasks_mode(self, value: Any, own_page: bool = False) -> dict:
        """The owner of this computer chooses what happens here when a step needs approval: "here" or "safe"
        (tasks.set_mode: this computer only, never from a request's own turn)."""
        if not isinstance(value, str) or value not in tasks.MODES:
            raise BadInput(BAD_REQUEST)
        if own_page is not True:  # the terminal: never a request's own turn, never a command a chat started
            if tasks.asked_turn(env=True):
                raise tasks.TaskError("not_from_a_request")
            if not _owner_terminal():
                raise tasks.TaskError("answer_owner_only")
        was = tasks.mode()
        now = tasks.set_mode(value, own_page=own_page is True)
        if was != now:
            tasks_run.kick()
            self.emit("tasks", None, {"what": "mode_" + now, "count": 1})
        return self.tasks(refresh=False)

    def tasks_ask(self, machine: Any, text: Any, twin: Any = None, wait_s: Any = None,
                  profile: Optional[str] = None) -> dict:
        for value in (machine, text):
            if not isinstance(value, str):
                raise BadInput(BAD_REQUEST)
        if twin is not None and not isinstance(twin, str):
            raise BadInput(BAD_REQUEST)
        wait = tasks.ASK_WAIT_S if not isinstance(wait_s, (int, float)) or isinstance(wait_s, bool) \
            else max(0.0, min(600.0, float(wait_s)))
        return tasks_run.ask(self, machine, text, twin, wait, profile)

    def tasks_approve(self, task_id: Any, choice: Any, q: Any = None, own_page: bool = False) -> dict:
        """The owner of this computer answers the step of one request that waits for approval: "once" (do this
        one step) or "deny". Nothing else is accepted: a request from another computer never earns a standing
        permission. The answer is handed to Hermes' own approval question by the process that runs the turn.
        `q` (from `waiting[i].q`) says which question the owner was shown; with it the answer cannot land on a
        later step that opened in the meantime. `own_page`: called by the Twins page's route (tasks.decide)."""
        if not isinstance(task_id, str) or choice not in tasks.CHOICES or not (q is None or (isinstance(q, str)
                                                                                         and len(q) <= 16)):
            raise BadInput(BAD_REQUEST)
        if own_page is not True:  # the terminal: never a request's own turn, never a command a chat started
            if tasks.asked_turn(env=True):
                raise tasks.TaskError("not_from_a_request")
            if not _owner_terminal():
                raise tasks.TaskError("answer_owner_only")
        tasks.decide(task_id, choice, q, own_page=own_page is True)
        self.emit("tasks", None, {"what": "approved" if choice == "once" else "denied", "count": 1})
        return self.tasks(refresh=False)

    def tasks_check(self, task_id: Any, wait_s: Any = 0) -> dict:
        if not isinstance(task_id, str):
            raise BadInput(BAD_REQUEST)
        wait = 0.0 if not isinstance(wait_s, (int, float)) or isinstance(wait_s, bool) \
            else max(0.0, min(600.0, float(wait_s)))
        return tasks_run.check(self, task_id, wait)


# ---- where the command came from --------------------------------------------------------------------------------------------
def _owner_terminal() -> bool:
    """Is this command the owner's own, typed in a terminal, and started by no chat? Hermes hands a chat's marks
    (its number, its origin) to every command the chat starts, so a command that carries any of them was started
    by a chat (and so by a model) and is refused. A new process holds no per-chat values of Hermes' own, so its
    environment is all there is to read (tasks._chat_numbers also asks Hermes, where it is loaded)."""
    import os
    return not any(os.environ.get(n) for n in config.SURFACE_VARS) and not tasks._chat_numbers()


# ---- terminal ---------------------------------------------------------------------------------------------------------------
HELP_LINES = """\
Ask your other computers
  ask COMPUTER TEXT      ask this twin's copy on another computer to do something
  requests [on|off]      let your other computers ask this one (off by default); shows the log
  requests approve|deny REQUEST   answer a step that waits for your approval on this computer
  requests mode [here|safe]   what happens here when a step needs approval (default: ask on this computer)
"""


def _out(text: str = "") -> None:
    try:
        print(text, flush=True)
    except BrokenPipeError:  # the reader went away (`| head`): nothing more to say
        pass


def describe_result(out: dict) -> list:
    lines = [out.get("label") or ""]
    if out.get("reply"):
        lines.append(out["reply"])
    err = out.get("error")
    if isinstance(err, dict):
        lines.append(" ".join(x for x in (err.get("message"), err.get("hint")) if x))
    if out.get("hint"):
        lines.append(out["hint"])
    if out.get("state") in tasks.OPEN_STATES and out.get("id"):
        lines.append(f"Look again with:  hermes twin ask --check {out['id']}")
    return [ln for ln in lines if ln]


def describe_mode(view: dict) -> list:
    md = view.get("mode") or {}
    lines = [f"{md.get('label')}, on {view['this_machine']['name']}:"]
    for c in md.get("choices") or []:
        mark = "*" if c["value"] == md.get("value") else " "
        lines.append(f" {mark} {c['value']:<7} {c['label']}. {c['note']}")
    lines.append("Change it with:  hermes twin requests mode here|safe   (on this computer only)")
    return lines


def describe_view(view: dict) -> list:
    sw = view["switch"]
    name = view["this_machine"]["name"]
    lines = [f"\"{sw['label']}\" is {'ON' if sw['on'] else 'off'} on {name}."]
    if not view["linked"]:
        lines.append("This computer is not linked to another one yet, so nothing can ask it.")
    elif sw["on"]:
        lines.append("Each request is one chat turn for the twin here, under this computer's own Hermes rules. "
                     "Switch it off with:  hermes twin requests off")
        if sw.get("network") == "lan":
            lines.append(HOME_NETWORK_NOTE)
        md = view.get("mode") or {}
        chosen = next((c for c in md.get("choices") or [] if c["value"] == md.get("value")), None)
        if chosen:
            lines.append(f"{md['label']}: {chosen['label']}. {chosen['note']} "
                         f"Change it with:  hermes twin requests mode")
    else:
        lines.append("Nothing from your other computers runs here. Switch it on with:  hermes twin requests on")
    for w in view.get("waiting") or []:
        lines.append(f"WAITING FOR YOU: {w['twin']} was asked by {w['peer']}: {w['text']}")
        lines.append(f"  One step needs your approval ({w['description'] or 'a step Hermes checks'}): {w['command']}")
        step = f" --step {w['q']}" if w.get("q") else ""
        lines.append(f"  Do it once:  hermes twin requests approve {w['id']}{step}      "
                     f"Refuse:  hermes twin requests deny {w['id']}{step}")
    if view["log"]:
        lines.append("Requests (newest first):")
        for r in view["log"][:20]:
            who = {"out": f"to {r['peer']}", "in": f"from {r['peer']}",
                   "relay": f"{r['peer']} asked {r['peer_to']}"}[r["dir"]]
            text = f": \"{r['text']}\"" if r["text"] else ""
            when = f"{_ago(r['asked_at'])}, " if r["asked_at"] else ""  # (in words, as the page: no raw stamp, no id)
            lines.append(f"  {when}{who}{text}")
            lines.append(f"      {r['label']}")
    else:
        lines.append("No requests yet.")
    return lines


def cmd_ask(args: Any) -> int:
    svc = _get_service()
    if not _owner_terminal():
        # only the owner asks another computer: never a command a chat (or its model) started, local or not
        raise tasks.TaskError("local_only")
    if getattr(args, "check", None):
        out = svc.tasks_check(args.check, getattr(args, "wait", None) or 0)
    else:
        text = " ".join(args.text or []).strip()
        if not args.machine or not text:
            raise EngineError("usage", "Say which computer and what to ask: hermes twin ask COMPUTER TEXT")
        out = svc.tasks_ask(args.machine, text, getattr(args, "twin", None), getattr(args, "wait", None),
                            profile=getattr(args, "as_profile", None))
    if args.json:
        _out(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        for line in describe_result(out):
            _out(line)
    return 0 if out.get("state") == "done" else (2 if out.get("state") in tasks.OPEN_STATES else 1)


def cmd_requests(args: Any) -> int:
    svc = _get_service()
    what = getattr(args, "what", None) or "show"
    if what in ("approve", "deny"):
        if not getattr(args, "id", None):
            raise EngineError("usage", "Say which request: hermes twin requests shows the line to copy for each "
                                       "one that waits.")
        view = svc.tasks_approve(args.id, "once" if what == "approve" else "deny", getattr(args, "step", None))
        if not args.json:
            _out("Approved: that one step goes ahead." if what == "approve" else "Refused: that step is not done.")
            return 0
    elif what == "mode":
        value = getattr(args, "id", None)
        if value is not None and value not in tasks.MODES:
            raise EngineError("usage", "Choose one of: hermes twin requests mode here | safe")
        view = svc.tasks_mode(value) if value is not None else svc.tasks(refresh=False)
        if not args.json:
            for line in describe_mode(view):
                _out(line)
            return 0
    else:
        view = svc.tasks_switch(what == "on") if what in ("on", "off") else svc.tasks()
    if args.json:
        _out(json.dumps(view, ensure_ascii=False, indent=1))
    else:
        for line in describe_view(view):
            _out(line)
    return 0


def setup(subs: Any) -> None:
    p = subs.add_parser("ask", help="Ask a twin's copy on another of your computers to do something")
    p.add_argument("machine", nargs="?", metavar="COMPUTER", help="The other computer's name")
    p.add_argument("text", nargs="*", metavar="TEXT", help="What the twin there should do")
    p.add_argument("--twin", help="Which twin to ask (default: the only one, or the one of this Hermes bot)")
    p.add_argument("--as-profile", dest="as_profile", help="Ask as the twin of this Hermes bot")
    p.add_argument("--check", metavar="REQUEST", help="Look at an earlier request again")
    p.add_argument("--wait", type=float, help="How many seconds to wait for the answer (default 100)")
    p.add_argument("--json", action="store_true")
    p = subs.add_parser("requests", help="Let your other computers ask this one to do things (off by default)")
    p.add_argument("what", nargs="?", choices=("on", "off", "show", "approve", "deny", "mode"), default="show")
    p.add_argument("id", nargs="?", metavar="REQUEST", help="With approve or deny: the request, as "
                                                            "`hermes twin requests` shows it. With mode: here "
                                                            "(ask on this computer) or safe (only safe requests)")
    p.add_argument("--step", metavar="MARK", help="With approve or deny: the step you were shown, so that your "
                                                  "answer cannot count for a later one")
    p.add_argument("--json", action="store_true")


HANDLERS = {"ask": cmd_ask, "requests": cmd_requests}
