"""`/twin ...` slash command (CLI, TUI, Desktop, messaging). Returns text; never prints, never raises."""
from __future__ import annotations

import logging
import shlex
from typing import Any

from . import secrets as _secrets
from .engine import EngineError
from .model import STATE_LABELS, ago, plural, valid_id

log = logging.getLogger(__name__)

HELP = ("/twin                 status of every twin\n"
        "/twin sync [PAIR]     sync now\n"
        "/twin conflicts [PAIR] show conflicts\n"
        "/twin recent [PAIR]   recent activity (metadata only)\n"
        "/twin demo [start|remove|grok-edit|hermes-edit|conflict|secret]\n"
        "More (add twins, resolve, restore, mirror): hermes twin --help, or the Twins page in Hermes Desktop.")


LOCAL_PLATFORMS = {"", "local", "cli", "tui", "desktop"}
READ_ONLY = {"status", "recent", "help"}


def _local(platform: Any) -> bool:
    """True on this computer (CLI, TUI, Desktop). Messaging chats may only read status."""
    value = getattr(platform, "value", platform)
    return value is None or (isinstance(value, str) and value.strip().lower() in LOCAL_PLATFORMS)


def _svc():
    from .service import get_service
    return get_service()


def _line(p: dict) -> str:
    label = STATE_LABELS.get(p.get("state"), "?")
    reason = p.get("state_reason") or ""
    tail = f" ({reason})" if reason and reason != label else ""
    demo = " [demo]" if p.get("demo") else ""
    return f"- {p['grok']['name']}{demo} -> profile {p['hermes']['profile']}: {label}{tail}, synced {ago(p.get('last_sync_at'))}"


def _status() -> str:
    st = _svc().status()
    if not st["pairs"]:
        return "No twins yet. Try `/twin demo`, or add twins from Grok with `hermes twin add`."
    return "\n".join(["Grok Bot Twin"] + [_line(p) for p in st["pairs"][:20]])


def _sync(pair: str | None) -> str:
    res = _svc().sync(pair)
    if not res["pairs"]:
        return "Nothing to sync."
    out = []
    for r in res["pairs"]:
        bits = []
        if r["applied"]:
            bits.append("updated " + ", ".join(sorted({x["kind"] for x in r["applied"]})))
        if r["secrets_withheld"]:
            n = len({s["item"] for s in r["secrets_withheld"]})
            bits.append(f"{_secrets.held_back_text(n)} ({_secrets.NOTE})")
        if r["conflicts"]:
            bits.append(plural(len(r['conflicts']), 'conflict'))
        if r["waiting"]:
            bits.append(f"{plural(len(r['waiting']), 'change')} waiting for approval")
        out.append(f"- {r['pair_id']}: " + ("; ".join(bits) if bits else "in step"))
    return "\n".join(out)


def handle(raw_args: str = "", **kwargs: Any) -> str:
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
        if pair is not None and cmd != "demo" and not valid_id(pair):
            return "Twin ids look like acme-helper."
        if cmd == "sync":
            return _sync(pair)
        if cmd == "conflicts":
            svc = _svc()
            ids = svc.pair_ids()
            if not pair and len(ids) != 1:
                return "No twins yet." if not ids else f"You have {len(ids)} twins; say which one: /twin conflicts PAIR"
            pid = pair or ids[0]
            data = svc.conflicts(pid)
            if not data["conflicts"]:
                return "No conflicts."
            target = f"{pair} " if pair else ""
            return "\n".join([f"{plural(len(data['conflicts']), 'conflict')}:"] + [
                f"- {c['conflict_id']} (since {ago(c.get('since'))}): keep one side with "
                f"`hermes twin resolve {target}\"{c['conflict_id']}\" --keep hermes|grok`"
                for c in data["conflicts"][:10]])
        if cmd == "recent":
            svc = _svc()
            pid = pair or next(iter(svc.pair_ids()), None)
            if not pid:
                return "No twins yet."
            return "\n".join(f"- {ago(e['at'])}: {e['side']} {e['kind']}" for e in svc.recent(pid, 10)["entries"]) \
                or "Nothing yet."
        if cmd == "demo":
            action = pair or "start"
            svc = _svc()
            res = svc.demo_action(action)
            if action in ("start", "remove"):
                return "\n".join([res["did"] + "."] + [_line(p) for p in res["pairs"]])
            from .cli import demo_next_step
            return res["did"] + ". " + demo_next_step(svc).replace("`hermes twin sync`", "`/twin sync`")
        return HELP
    except EngineError as exc:
        return f"Grok Bot Twin: {exc.message}"
    except Exception as exc:  # the user sees text, never a traceback
        log.exception("/twin failed")
        return f"Grok Bot Twin: /twin failed ({exc.__class__.__name__})"
