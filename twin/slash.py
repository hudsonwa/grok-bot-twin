"""`/twin ...` slash command (CLI, TUI, Desktop, messaging). Returns text; never prints, never raises.

Replies name the Twins page first and a command second, in the page's words.
"""
from __future__ import annotations

import logging
import shlex
from typing import Any

from .engine import EngineError
from .model import STATE_LABELS, ago, item_kind, item_name, plural, status_sentence, valid_id

log = logging.getLogger(__name__)

HELP = ("/twin                    your twins and what to do next\n"
        "/twin check [TWIN]       check Grok now\n"
        "/twin conflicts [TWIN]   what changed in both places\n"
        "/twin recent [TWIN]      recent activity (when and what kind; never content)\n"
        "/twin ask COMPUTER: TEXT ask this twin's copy on another of your computers to do something there\n"
        "/twin requests           whether other computers may ask this one, and the log\n"
        "/twin demo [start|remove|grok-edit|hermes-edit|conflict|secret]   a sample twin with invented data\n"
        "Everything else (connect your Grok bots, choose a version, earlier versions, skill sync) is on the "
        "Twins page in Hermes Desktop, or: hermes twin --help.")

NO_TWINS = ("No twins yet. Open the Twins page in Hermes Desktop and press Connect my Grok bots. "
            "To look around first, `/twin demo` starts a sample with invented data.")

# Hermes' own local surfaces. Anything else (a messaging app, a webhook, the API server) and a missing, empty or
# unknown platform count as NOT on this computer: the check fails closed.
LOCAL_PLATFORMS = frozenset({"local", "cli", "tui", "desktop"})
READ_ONLY = {"status", "recent", "help"}
_RECENT_KINDS = {"sync": "check", "skill_change": "skill change", "name_change": "name change",
                 "routine_change": "routine change", "session": "chat in Hermes"}
_SIDES = {"grok": "Grok", "hermes": "Hermes", "both": "both"}


def _local(platform: Any) -> bool:
    """True on this computer (CLI, TUI, Desktop: Hermes names the platform of every surface). Messaging chats,
    and any caller whose platform is missing or unknown, may only read status."""
    value = getattr(platform, "value", platform)
    return isinstance(value, str) and value.strip().lower() in LOCAL_PLATFORMS


def _svc():
    from .service import get_service
    return get_service()


def _line(p: dict) -> str:
    label = status_sentence(p)  # the sentence the twin's row on the Twins page shows
    reason = p.get("state_reason") or ""
    tail = (f" ({reason})" if p.get("state") == "error" and reason
            and reason.lower() != STATE_LABELS["error"].lower() else "")
    sample = " [sample]" if p.get("sample") or p.get("demo") else ""
    return f"- {p['grok']['name']}{sample}: {label}{tail}, checked {ago(p.get('last_sync_at'))}"


def _status() -> str:
    st = _svc().status()
    if not st["pairs"]:
        if (st.get("fleet") or {}).get("role") == "follower":  # this computer's twins are copies from another one
            from .fleet_cli import describe
            return "\n".join(["Grok Bot Twin"] + describe(_svc().fleet()))
        return NO_TWINS
    lines = ["Grok Bot Twin"] + [_line(p) for p in st["pairs"][:20]]
    if any(p.get("state") in ("needs_you", "error") for p in st["pairs"]):
        lines.append("Open the Twins page in Hermes Desktop to sort out what needs your attention.")
    return "\n".join(lines)


def _sync(pair: str | None) -> str:
    from .cli import _names, _sync_bits
    svc = _svc()
    names = _names(svc)
    res = svc.sync(pair)
    if not res["pairs"]:
        return "No twins to check yet."
    out = []
    attention = False
    for r in res["pairs"]:
        name = names.get(r["pair_id"], r["pair_id"])
        if r.get("error"):
            out.append(f"- {name}: can't check right now")
            attention = True
            continue
        bits, needs = _sync_bits(r)
        attention = attention or needs
        out.append(f"- {name}: " + ("; ".join(bits) if bits else "up to date"))
    if attention:
        out.append("Open the Twins page in Hermes Desktop to sort it out.")
    return "\n".join(out)


def _ask(raw: str) -> str:
    """`/twin ask Surface: what is your free disk space` (the colon is optional when the name is one word)."""
    from .tasks_api import describe_result
    rest = raw.strip()[3:].strip()
    machine, sep, text = rest.partition(":")
    if not sep:
        machine, _, text = rest.partition(" ")
    if not machine.strip() or not text.strip():
        return "Say which computer and what to ask, like: /twin ask Surface: how much disk space is free?"
    return "\n".join(describe_result(_svc().tasks_ask(machine.strip(), text.strip())))


def handle(raw_args: str = "", **kwargs: Any) -> str:
    try:
        if (raw_args or "").strip().lower().startswith("ask ") or (raw_args or "").strip().lower() == "ask":
            if not _local(kwargs.get("platform")):
                return "From a messaging chat, /twin only shows status and recent activity. Run this in Hermes on your computer."
            return _ask(raw_args or "")
        if (raw_args or "").strip().lower() == "requests":
            if not _local(kwargs.get("platform")):  # the log holds what was asked and answered
                return "From a messaging chat, /twin only shows status and recent activity. Run this in Hermes on your computer."
            from .tasks_api import describe_view
            return "\n".join(describe_view(_svc().tasks()))
    except EngineError as exc:
        return f"Grok Bot Twin: {exc.message}" + (f" {exc.hint}" if getattr(exc, "hint", "") else "")
    except Exception:
        log.exception("/twin ask failed")
        return "Grok Bot Twin: something went wrong. Try again, or open the Twins page in Hermes Desktop."
    try:
        args = shlex.split(raw_args or "")[:4]
    except ValueError:
        return HELP
    try:
        if not args or args[0] == "status":
            return _status()
        cmd, rest = args[0], args[1:]
        if cmd not in READ_ONLY and not _local(kwargs.get("platform")):
            return "From a messaging chat, /twin only shows status and recent activity. Run this in Hermes on your computer."
        pair = rest[0] if rest else None
        if pair is not None and cmd != "demo":
            # TWIN is the bot's name ("Acme Support", quoted or not) or the twin's short name
            from .cli import _resolve
            pair = _resolve(_svc(), " ".join(rest))
            if not valid_id(pair):
                return "That is not one of your twins."
        if cmd in ("check", "sync"):  # (`sync`: the older name still works)
            return _sync(pair)
        if cmd == "conflicts":
            svc = _svc()
            ids = svc.pair_ids()
            if not pair and len(ids) != 1:
                if not ids:
                    return NO_TWINS
                from .cli import _call, _twin_names
                called = _twin_names(svc)
                return (f"You have {len(ids)} twins; say which one: /twin conflicts TWIN "
                        f"({', '.join(_call(called, i) for i in ids[:20])})")
            pid = pair or ids[0]
            data = svc.conflicts(pid)
            if not data["conflicts"]:
                return "Nothing changed in both places."
            if pair:
                from .cli import _call, _twin_names
            target = f"{_call(_twin_names(svc), pid)} " if pair else ""
            lines = [f"{plural(len(data['conflicts']), 'item')} changed in both places:"]
            lines += [f"- {item_name(c['conflict_id'])} ({item_kind(c['conflict_id']) or 'item'}), "
                      f"since {ago(c.get('since'))}" for c in data["conflicts"][:10]]
            first = data["conflicts"][0]["conflict_id"]
            lines.append("Choose which version to keep on the Twins page in Hermes Desktop. Or in a terminal: "
                         f"`hermes twin resolve {target}\"{first}\" --keep hermes` (or `--keep grok`).")
            return "\n".join(lines)
        if cmd == "recent":
            svc = _svc()
            pid = pair or next(iter(svc.pair_ids()), None)
            if not pid:
                return NO_TWINS
            return "\n".join(f"- {ago(e['at'])}: {_SIDES.get(e['side'], 'Hermes')}, "
                             f"{_RECENT_KINDS.get(e['kind'], 'other change')}"
                             for e in svc.recent(pid, 10)["entries"]) or "Nothing yet."
        if cmd == "demo":
            action = pair or "start"
            svc = _svc()
            res = svc.demo_action(action)
            did = res["did"].rstrip(".") + "."
            if action in ("start", "remove"):
                return "\n".join([did] + [_line(p) for p in res["pairs"]])
            from .cli import demo_next_step
            return did + " " + demo_next_step(svc).replace("`hermes twin check`", "`/twin check`")
        return HELP
    except EngineError as exc:
        return f"Grok Bot Twin: {exc.message}" + (f" {exc.hint}" if getattr(exc, "hint", "") else "")
    except Exception:  # the user sees plain text, never a traceback or a class name
        log.exception("/twin failed")
        return "Grok Bot Twin: something went wrong. Try again, or open the Twins page in Hermes Desktop."
