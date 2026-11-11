"""`hermes twin instructions`: Grok's instructions kept in step (twin/instr_sync.py) from the terminal.

    hermes twin instructions [TWIN]                 where it stands (and the conflict, line by line)
    hermes twin instructions [TWIN] check           read the newest setup export now
    hermes twin instructions [TWIN] on|off          follow Grok's instructions, or stop
    hermes twin instructions [TWIN] keep grok [CODE] | keep hermes    answer a card (CODE: the one shown with it)
    hermes twin instructions downloads on|off       also look in Downloads in the background (off by default)
    hermes twin instructions request                what to paste into each bot's chat once, so its export stays fresh
"""
from __future__ import annotations

import json
from typing import Any

from .engine import EngineError
from .model import ago

ACTIONS = ("show", "check", "on", "off", "keep", "downloads", "request")
LABEL = {"off": "Off", "waiting": "Waiting for the first setup export", "in_step": "In step",
         "applied": "Updated from Grok", "hermes_ahead": "Changed in Hermes", "conflict": "Changed in both places",
         "held": "Not copied"}
ASKING = "Waiting for your choice"
USAGE = ('Say what to do: hermes twin instructions [TWIN] [check | on | off | keep grok|hermes], '
         'hermes twin instructions downloads on|off, or hermes twin instructions request')


def _print(text: str = "") -> None:
    print(text, flush=True)


def _show(v: dict, name: str) -> None:
    c = v.get("conflict")
    label = ASKING if c and c.get("reason") not in (None, "both", "first_read") else LABEL.get(v['state'], v['state'])
    _print(f"{name}: {label}. {v['why']}".rstrip())
    if v.get("checked_at"):
        where = {"mirror": "the shared folder", "downloads": "Downloads"}.get(v.get("source") or "", "")
        _print(f"  Last read {ago(v['checked_at'])}" + (f" from {where}" if where else "")
               + (f" (export written {ago(v['file_at'])})" if v.get("file_at") else ""))
    if v.get("applied_at"):
        _print(f"  Last change from Grok {ago(v['applied_at'])}; to undo it: hermes twin versions \"{name}\"")
    if v.get("left_out"):
        _print(f"  Left out, looked like a password or key: line {', '.join(str(n) for n in v['left_out'][:20])}")
    if c:
        _print("  Lines that differ (- Hermes, + Grok):")
        for d in c["diff"][:200]:
            mark = {"hermes": "-", "grok": "+", "both": " "}.get(d["side"])
            _print("    ..." if mark is None else f"    {mark} {d['text']}")
        _print("  Keep one: hermes twin instructions " + f'"{name}" keep grok {c.get("grok_digest") or ""}'.rstrip()
               + "   or   keep hermes")
        _print("  (To put the two together, change them in Hermes first, then keep hermes.)")


def cmd_instructions(args: Any) -> int:
    from . import cli, instr_sync, mirror_exports, tasks
    vals = list(getattr(args, "args", None) or [])
    pair = None
    if vals and vals[0].lower() not in ACTIONS:
        pair = vals.pop(0)
    action = vals.pop(0).lower() if vals else "show"
    value = vals.pop(0).lower() if vals else None
    code = vals.pop(0) if vals and action == "keep" and value == "grok" else None
    if vals or action not in ACTIONS or (action in ("keep", "downloads") and value is None) \
            or (action not in ("keep", "downloads") and value is not None):
        raise EngineError("usage", USAGE)
    svc = cli._svc()
    if action == "request":
        _print("Paste this once into each bot's own chat in the Grok app. Its setup export then stays up to date in "
               "Grok, and skill sync brings it here. It adds one routine to the bot (every 6 hours); to stop it, ask the "
               "bot to delete that routine:\n")
        _print(instr_sync.keep_fresh_request())
        if mirror_exports.exports_dir(svc.central) is None:
            _print("\nSkill sync is not set up on this computer yet: hermes twin skill-sync")
        return 0
    if action in ("on", "off", "keep", "downloads") and tasks.asked_turn():
        raise EngineError("refused", "A request from another computer cannot change how a twin's instructions sync.",
                          "Do it yourself, on the Twins page or in your own terminal.")
    if action == "downloads":
        if value not in ("on", "off"):
            raise EngineError("usage", "Say on or off: hermes twin instructions downloads on")
        v = svc.instructions_sync_settings(value == "on")
        _print(json.dumps(v) if args.json else
               ("Setup exports saved to Downloads from now on are read too." if v["downloads"]
                else "Hermes no longer looks in Downloads."))
        return 0
    pid = cli._one(svc, cli._twin(pair) if pair else None)
    name = cli._twin_names(svc).get(pid) or pid
    if action == "check":
        note = (svc.instructions_sync_now(pid) or {}).get("note")
        v = svc.instructions_sync_info(pid)
        if note and not args.json:
            _print(note)
    elif action in ("on", "off"):
        v = svc.instructions_sync_set(pid, action == "on")
    elif action == "keep":
        if value not in ("grok", "hermes"):
            raise EngineError("usage", "Say which to keep: keep grok, or keep hermes")
        if value == "grok" and code is None:
            # (without the code shown with the card: the version read just now, so an export that arrives between
            # this read and the answer is not taken)
            code = (svc.instructions_sync_info(pid).get("conflict") or {}).get("grok_digest") or ""
        v = svc.instructions_conflict_resolve(pid, value, None, code)
    else:
        v = svc.instructions_sync_info(pid)
    if args.json:
        _print(json.dumps(v, indent=2))
    else:
        _show(v, name)
    return 0


def setup(subs: Any) -> None:
    p = subs.add_parser("instructions",
                        help="Grok's instructions kept in step: [TWIN] check, on|off, keep grok|hermes")
    p.add_argument("args", nargs="*", metavar="[TWIN] [ACTION]")
    p.add_argument("--json", action="store_true")


HANDLERS = {"instructions": cmd_instructions}
