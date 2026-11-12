"""`hermes twin transcripts ...`: full transcripts for one twin (memory layer 3; see transcripts.py)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import store
from . import transcripts as tr

ACTIONS = ("status", "on", "off", "to-grok-on", "to-grok-off", "refresh", "add-grok", "search", "export",
           "to-hermes", "forget")
CHANGES = ("on", "off", "to-grok-on", "to-grok-off", "refresh", "add-grok", "export", "to-hermes", "forget")
# refused in the chat turn of a request from another computer: every change, and every read of what is kept
# (transcripts stay on this computer; only `status`, which shows counts and switches, answers there)
REMOTE_REFUSED = CHANGES + ("search",)


def setup(subs: Any) -> None:
    from .cli import _twin_arg
    p = subs.add_parser("transcripts", help="Full transcripts of a twin: kept here, encrypted, searchable "
                                            "(off until you switch them on)")
    p.add_argument("action", choices=ACTIONS, nargs="?", default="status",
                   help="status | on | off | to-grok-on | to-grok-off | refresh | add-grok --file FILE | "
                        "search -q WORDS | export [--file FILE] | to-hermes | forget")
    _twin_arg(p)
    p.add_argument("--file", help="add-grok: a Grok export with conversations; export: where to write the JSON")
    p.add_argument("--query", "-q", help="search: the words to look for")
    p.add_argument("--side", choices=("grok", "hermes", "both"), default="both",
                   help="search, export: only Grok's or only Hermes' conversations")
    p.add_argument("--yes", "-y", action="store_true", help="forget: do not ask")
    p.add_argument("--json", action="store_true", help="print it as JSON")


def _profile(svc, pid: str) -> str:
    for p in (svc.status().get("pairs") or []):
        if p.get("pair_id") == pid:
            return (p.get("hermes") or {}).get("profile") or ""
    return ""


def cmd_transcripts(a) -> int:
    from . import tasks
    from .cli import _err, _one, _out, _svc
    from .engine import EngineError
    act = a.action
    if act in REMOTE_REFUSED and tasks.asked_turn():
        # (the chat turn of a request from another computer never changes a twin's setup, nor reads what is kept)
        raise EngineError("refused", "A request from another computer cannot read or change full transcripts.",
                          "Do it yourself, on the Twins page or in your own terminal.")
    svc = _svc()
    pid = _one(svc, a.pair)
    side = a.side if a.side in tr.SIDES else None
    try:
        if act == "on":
            tr.switch(pid, True)
            if not a.json:
                _out("Full transcripts are on for this twin. Next: `refresh` adds its Hermes chats so far; "
                     "`add-grok --file FILE` adds a Grok export.")
                return 0
        elif act == "off":
            tr.switch(pid, False)
            if not a.json:
                _out("Full transcripts are off for this twin. What was kept stays here; `forget --yes` deletes it.")
                return 0
        elif act == "to-grok-on":
            tr.switch_to_grok(pid, True)
            if not a.json:
                _out("Sending to Grok is on for this twin. Nothing is sent until the Mirror carries it; "
                     "`to-grok-off` stops it.")
                return 0
        elif act == "to-grok-off":
            tr.switch_to_grok(pid, False)
        elif act == "refresh":
            prof = _profile(svc, pid)
            r = tr.ingest_hermes(pid, store.profile_home(prof)) if prof else {"new": 0, "updated": 0}
            if r.get("note"):
                _err(f"✗ {r['note']}")
                return 1
            _out(f"Hermes side: {r['new']} new, {r['updated']} updated conversation(s).")
        elif act == "add-grok":
            if not a.file:
                _err("✗ Say which Grok export: --file FILE")
                return 2
            r = tr.ingest_grok_file(pid, Path(a.file).expanduser())
            if r.get("note"):
                _err(f"✗ {r['note']}")
                return 1
            _out(f"Grok side: {r['new']} new, {r['updated']} updated conversation(s).")
        elif act == "search":
            if not a.query:
                _err("✗ Say what to look for: --query WORDS")
                return 2
            # the owner at their own terminal (a request from another computer was refused above); masked all the
            # same if that ever changes (keys masked, password / PIN / code / card / instruction lines held back)
            r = tr.search(pid, a.query, limit=tr.MAX_HITS, side=side, redact=bool(tasks.asked_turn()))
            if a.json:
                _out(json.dumps(r, ensure_ascii=False, indent=1))
                return 0
            if not r["hits"]:
                _out(r.get("note") or "Nothing found.")
            for h in r["hits"]:
                _out(f"[{'Grok' if h['side'] == 'grok' else 'Hermes'}] {h['when'] or ''} {h['title']}\n"
                     f"   {h['role']}: {h['text']}")
            return 0
        elif act == "export":
            r = tr.export_json(pid, Path(a.file).expanduser() if a.file else None, side=side)
            _out(f"Wrote {r['conversations']} conversation(s), readable by you only: {r['path']}")
            return 0
        elif act == "to-hermes":
            prof = _profile(svc, pid)
            r = tr.import_to_hermes(pid, prof)
            _out(f"Made {len(r['imported'])} Grok conversation(s) into Hermes chats of the twin"
                 + (f"; {len(r['skipped'])} were already there" if r["skipped"] else "")
                 + (f"; {len(r['failed'])} could not be imported" if r["failed"] else "") + ".")
            return 0 if not r["failed"] else 1
        elif act == "forget":
            if not a.yes:
                _out("This deletes every kept conversation of this twin on this computer (the Grok bot and the Hermes "
                     "chats themselves stay). Run again with --yes to do it.")
                return 0
            r = tr.forget(pid)
            _out(f"Forgot {r['forgotten']} conversation(s); full transcripts are off for this twin.")
            return 0
        v = tr.view(pid)
        if a.json:
            _out(json.dumps(v, ensure_ascii=False, indent=1))
            return 0
        _out(f"Full transcripts: {'on' if v['on'] else 'off'}; to Grok: {'on' if v['to_grok'] else 'off'}; "
             f"kept: {v['conversations']['grok']} from Grok, {v['conversations']['hermes']} from Hermes.")
        _out(v["sentence"])
        return 0
    except tr.TranscriptsError as exc:
        _err(f"✗ {exc.message}")
        return 1


HANDLERS = {"transcripts": cmd_transcripts}
