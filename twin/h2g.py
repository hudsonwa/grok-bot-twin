"""Hermes -> Grok: a twin's name, description, instructions and routines, made in Grok by the owner's Mirror bot.

0.3.0 items 3, 4 and 6. Nothing here drives the Grok app. Every change in Grok is made by a `GrokWriter`:

- `FakeGrokWriter`: a pretend Grok account kept in a JSON file (tests, the sample, a dry run of the whole loop).
- the real writer (h2g_mirror.py) hands a signed change request to the owner's Mirror bot, which the owner
  pre-approved to change the bots they chose; the bot makes the change with its own bot tools and answers with a
  receipt. Whatever the writer, this module decides WHAT may change and keeps the record to undo it.

The rules, in the order they are checked:

1. Per-bot allowlist. The owner names each twin, and for each twin the items (name, description, instructions,
   routines) the Mirror may change in Grok. Nothing else is ever sent. Off by default; `allow()` / `disallow()`.
2. The owner's direction (the "What syncs" panel): an item moves Hermes -> Grok only while it is set to
   "Hermes -> Grok" or "Both ways". (Without the panel module, the allowlist alone decides.)
3. Change detection: the Hermes side now (SOUL.md, the twin's cron jobs) against the BASE, which is what Grok held
   the last time it was read or changed through here. Equal -> nothing to send. When Grok changed too since the
   base (the writer can read it back), the item is a CONFLICT and is never sent: the owner chooses a side first.
4. Preview. `preview()` lists each change with its before / after (and a line diff for instructions) and gives it a
   `change_id`: a digest of exactly what would be sent. `apply(change_id)` re-computes the plan and refuses when it
   is not the same one (something changed since the owner looked).
5. Secrets: a text that looks like a password or key is never sent (the plugin's own detector); that item is held.
6. Undo: every applied change keeps the values it replaced. `undo(change_id)` sends them back, only while Grok
   still holds what the change put there (otherwise the owner would lose a newer edit made in Grok).
7. Routines run in ONE place. Each routine has a runner, "hermes" or "grok". A routine copied to Grok arrives
   paused unless its runner is Grok. Moving the runner is a handover in a fixed order: first the old side is paused
   (and, for Grok, the writer's receipt says so), only then the new side is switched on. If the second step fails,
   the routine runs nowhere for a while; never in both places.

Files: `h2g.json` in the plugin's data folder (owner-only, atomic). Values kept for undo are capped like the
items themselves (instructions 200 KB).
"""
from __future__ import annotations

import difflib
import functools
import hashlib
import json
import re
import secrets as _random
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from . import safe, store
from . import secrets as _secrets
from .model import now_iso, valid_id

V = 1
STATE_FILE = "h2g.json"
FAKE_FILE = "h2g-fake-grok.json"
ITEMS = ("name", "description", "instructions", "routines")
TEXT_ITEMS = ("name", "description", "instructions")
CAPS = {"name": 100, "description": 2000, "instructions": 200 * 1024}
ROUTINE_NAME_CAP = 120
ROUTINE_PROMPT_CAP = 8000
MAX_ROUTINES = 50
MAX_CHANGES = 200
RUNNERS = ("hermes", "grok")
CHANGE_RE = re.compile(r"^c-[0-9a-f]{16}\Z")
RKEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")
CRON_RE = re.compile(r"^[0-9A-Za-z*/,\-#?]+(?: [0-9A-Za-z*/,\-#?]+){4,6}\Z")
_CTRL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏ -‮⁠-⁯﻿\ud800-\udfff]")
_LOCK = threading.RLock()

# words for the owner (the page and the terminal show these unchanged)
NOT_ALLOWED = "The Mirror may not change this bot in Grok: allow it first (hermes twin to-grok allow)."
ITEM_NOT_ALLOWED = "The Mirror may not change the {item} of this bot in Grok."
NOT_WANTED = "The {item} is set to stay as it is in Grok (What syncs: choose Hermes → Grok or Both ways)."
CONFLICT = ("The {item} changed in Grok AND in Hermes since they were last the same. Nothing is sent until you "
            "choose which one to keep.")
HELD_SECRET = "The {item} holds something that looks like a password or key, so Hermes does not send it to Grok."
STALE = "Something changed since you looked at this preview. Look again, then apply the new one."
NO_UNDO = "Grok no longer holds what this change put there (it was changed in Grok since), so undo would lose that."
NOTHING = "Hermes and Grok already match: nothing to send."
REPLACES_ALL = ("Hermes cannot read the {item} in Grok, so this diff is against the last version Hermes knows of. "
                "Applying REPLACES the {item} in Grok with the whole Hermes text.")
SRC_LABEL = {"grok": "Grok now", "applied": "Last sent to Grok", "hermes": "Hermes when you allowed it"}
UNKNOWN_BEFORE = ("Hermes never knew what Grok held before this change (it cannot read it), so it cannot put it "
                  "back. Change it in Grok itself.")
GAVE_UP = "No answer from the Mirror: Hermes stopped waiting. If the Mirror makes it later, the next check shows it."
PENDING = ("The Mirror has not made the last change yet (it does on its next run). Nothing more is sent until it "
           "answers.")
EMPTY = "The {item} is empty in Hermes. Hermes never empties it in Grok: change it in Grok itself if you want that."
NO_ROUTINES = ("Hermes has no routines for this twin now, so Grok's are left as they are (Hermes never removes "
               "every routine at once).")


class H2GError(Exception):
    """Something the owner can act on; `code` is a short word for the page and the REST answer."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---- the writer: the only thing that touches Grok ------------------------------------------------------------------

class GrokWriter(Protocol):
    """What the Mirror (or a fake of it) does in Grok. Every method takes the Grok bot's id (`bot`).

    read(bot) -> {"name", "description", "instructions", "routines": [routine]} as Grok holds it now, or None when
        the writer cannot read it back (then this module goes by its base alone, and says so).
    apply(bot, ops, change_id) -> receipt {"state": "done" | "sent" | "failed", "message": str, "at": iso,
        "done": [index of op done], "failed": [index]}. "sent": the request is on its way (the Mirror makes it on its
        next run); `poll(change_id, n_ops)` later answers the final receipt; `forget(change_id)` (optional) takes an
        answered or abandoned request away so it can never be made again.
    permission(bot) -> "ready" | "not_set_up" | "no_permission" (the owner's pre-approval as the writer knows it).
    """

    kind: str

    def read(self, bot: str) -> Optional[dict]: ...

    def apply(self, bot: str, ops: list, change_id: str) -> dict: ...

    def poll(self, change_id: str, n: Optional[int] = None) -> Optional[dict]: ...

    def permission(self, bot: str) -> str: ...


# an op is one of:
#   {"op": "set", "item": "name"|"description"|"instructions", "value": str}
#   {"op": "routine.put", "key": rkey, "name", "schedule", "prompt", "enabled": bool | None}   (add or replace;
#                                                    None: keep its on/off as it is in Grok, a new one paused)
#   {"op": "routine.remove", "key": rkey}
#   {"op": "routine.enable", "key": rkey, "enabled": bool}


class FakeGrokWriter:
    """A pretend Grok account: {"bots": {bot_id: {"name", "description", "instructions", "routines": {key: r}}},
    "allowed": [bot_id], "requests": [...]} in a JSON file (or in memory when `path` is None). `delay=True` makes
    apply answer "sent" and finish on the next poll, like the Mirror's routine does. `fail_ops` makes the op kinds
    it names fail (to test half-done handovers)."""

    kind = "fake"

    def __init__(self, path: Optional[Path] = None, delay: bool = False, fail_ops: tuple = ()):
        self.path = Path(path) if path is not None else None
        self.delay = delay
        self.fail_ops = tuple(fail_ops)
        self._mem: dict = {"bots": {}, "allowed": [], "requests": {}}

    def _load(self) -> dict:
        if self.path is None:
            return self._mem
        data = store.read_json(self.path, None)
        if not isinstance(data, dict):
            data = {"bots": {}, "allowed": [], "requests": {}}
        for k, d in (("bots", {}), ("allowed", []), ("requests", {})):
            data.setdefault(k, d)
        return data

    def _save(self, data: dict) -> None:
        if self.path is None:
            self._mem = data
        else:
            store.atomic_write_json(self.path, data)

    # test and sample helpers
    def put_bot(self, bot: str, name: str = "", description: str = "", instructions: str = "",
                routines: Optional[list] = None, allowed: bool = True) -> None:
        data = self._load()
        data["bots"][bot] = {"name": name, "description": description, "instructions": instructions,
                             "routines": {r["key"]: dict(r) for r in (routines or [])}}
        if allowed and bot not in data["allowed"]:
            data["allowed"].append(bot)
        self._save(data)

    def edit_in_grok(self, bot: str, **fields: Any) -> None:
        """The owner changes the bot in Grok itself."""
        data = self._load()
        data["bots"][bot].update(fields)
        self._save(data)

    def permission(self, bot: str) -> str:
        data = self._load()
        if bot not in data["bots"]:
            return "not_set_up"
        return "ready" if bot in data["allowed"] else "no_permission"

    def read(self, bot: str) -> Optional[dict]:
        b = self._load()["bots"].get(bot)
        if b is None:
            return None
        return {"name": b["name"], "description": b["description"], "instructions": b["instructions"],
                "routines": [dict(r) for r in b["routines"].values()]}

    def _do(self, data: dict, bot: str, ops: list) -> dict:
        b = data["bots"][bot]
        done, failed = [], []
        for i, op in enumerate(ops):
            if op["op"] in self.fail_ops:
                failed.append(i)
                continue
            if op["op"] == "set":
                b[op["item"]] = op["value"]
            elif op["op"] == "routine.put":
                was = b["routines"].get(op["key"]) or {}
                b["routines"][op["key"]] = dict({k: op[k] for k in ("key", "name", "schedule", "prompt")},
                                                enabled=bool(was.get("enabled")) if op["enabled"] is None
                                                else op["enabled"])
            elif op["op"] == "routine.remove":
                b["routines"].pop(op["key"], None)
            elif op["op"] == "routine.enable":
                if op["key"] in b["routines"]:
                    b["routines"][op["key"]]["enabled"] = bool(op["enabled"])
                else:
                    failed.append(i)
                    continue
            done.append(i)
        state = "done" if not failed else "failed"
        return {"state": state, "done": done, "failed": failed, "at": now_iso(),
                "message": "" if not failed else f"{len(failed)} of {len(ops)} changes were not made in Grok."}

    def apply(self, bot: str, ops: list, change_id: str) -> dict:
        data = self._load()
        if self.permission(bot) != "ready":
            return {"state": "failed", "done": [], "failed": list(range(len(ops))), "at": now_iso(),
                    "message": "The Mirror has no permission to change this bot."}
        if self.delay:
            data["requests"][change_id] = {"bot": bot, "ops": ops}
            self._save(data)
            return {"state": "sent", "done": [], "failed": [], "at": now_iso(),
                    "message": "The Mirror makes this change on its next run."}
        rec = self._do(data, bot, ops)
        self._save(data)
        return rec

    def forget(self, change_id: str) -> None:
        data = self._load()
        if data["requests"].pop(change_id, None) is not None:
            self._save(data)

    def poll(self, change_id: str, n: Optional[int] = None) -> Optional[dict]:
        data = self._load()
        req = data["requests"].pop(change_id, None)
        if req is None:
            return None
        rec = self._do(data, req["bot"], req["ops"])
        self._save(data)
        return rec


# ---- the Hermes side, as plain values --------------------------------------------------------------------------------

def clean_text(item: str, value: Any) -> str:
    if not isinstance(value, str):
        return ""
    v = _CTRL.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    if item == "name":
        v = " ".join(v.split())
    else:
        v = v.strip("\n")
    return v[:CAPS[item]]


def routine_key(name: str) -> str:
    """A routine's key on both sides: its name in lower case, hyphens for anything else (the same routine in Grok
    and in Hermes is matched by name, as the importer made it)."""
    k = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:64]
    return k or "routine"


def clean_routine(r: Any) -> Optional[dict]:
    if not isinstance(r, dict):
        return None
    name = " ".join(_CTRL.sub("", str(r.get("name") or "")).split())[:ROUTINE_NAME_CAP]
    sched = " ".join(str(r.get("schedule") or "").split())
    prompt = clean_text("description", str(r.get("prompt") or ""))[:ROUTINE_PROMPT_CAP] if r.get("prompt") else ""
    if not name or not CRON_RE.match(sched) or not prompt:
        return None
    key = r.get("key") if isinstance(r.get("key"), str) and RKEY_RE.match(r["key"]) else routine_key(name)
    return {"key": key, "name": name, "schedule": sched, "prompt": prompt, "enabled": r.get("enabled") is True}


def clean_side(side: Any) -> dict:
    side = side if isinstance(side, dict) else {}
    out = {k: clean_text(k, side.get(k)) for k in TEXT_ITEMS}
    rs = [clean_routine(r) for r in (side.get("routines") or [])[:MAX_ROUTINES]] \
        if isinstance(side.get("routines"), list) else []
    out["routines"] = {r["key"]: r for r in rs if r}
    return out


def read_grok(writer: Any, bot: str) -> Optional[dict]:
    """The writer's read-back, cleaned, with `known`: the items it could actually read. The Mirror can read a bot's
    name and description but (so far) not its routines, and Grok keeps no separate instructions field: an item it
    could not read is unknown, never "empty in Grok"."""
    g = writer.read(bot) if bot else None
    if not isinstance(g, dict):
        return None
    out = clean_side({k: v for k, v in g.items() if v is not None})
    out["known"] = {k for k in ITEMS if g.get(k) is not None}
    return out if out["known"] else None


def effective_runners(stored: dict, hermes_routines: dict, grok_routines: Optional[dict]) -> dict:
    """Where each routine runs. A routine with no runner chosen yet keeps running where it is switched on now, and
    nothing here ever switches one on or off by itself: on in Hermes -> "hermes"; paused in Hermes and known in
    Grok -> "grok" or "nowhere" by Grok's switch; paused in Hermes and unknown in Grok -> "keep" (Grok keeps its
    on/off as it is)."""
    runner = {k: v for k, v in stored.items() if v in RUNNERS or v == "nowhere"}
    for k, r in hermes_routines.items():
        if k in runner:
            continue
        if r["enabled"]:
            runner[k] = "hermes"
        elif grok_routines is not None and k in grok_routines:
            runner[k] = "grok" if grok_routines[k]["enabled"] else "nowhere"
        else:
            runner[k] = "keep"
    return runner


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _new_id(seed: Any) -> str:
    """A request id used once: the same edit sent again later never meets the old request's receipt."""
    return "c-" + _digest({"seed": seed, "at": now_iso(), "n": _random.token_hex(8)})[:16]


def _serial(fn: Callable) -> Callable:
    """One change at a time across threads and processes (CLI, page, background helper): the plugin's own lock."""
    @functools.wraps(fn)
    def run(self: Any, *a: Any, **k: Any) -> Any:
        with _LOCK, store.locked():
            return fn(self, *a, **k)
    return run


def text_diff(before: str, after: str, limit: int = 400, label: str = "Grok now") -> list[str]:
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), label, "Hermes", lineterm="", n=2))
    if len(lines) > limit:
        lines = lines[:limit] + [f"... {len(lines) - limit} more lines"]
    return [safe.cap(l, 400) for l in lines]


def _no_switch(r: Optional[dict]) -> Optional[dict]:
    return None if r is None else {k: v for k, v in r.items() if k != "enabled"}


def _no_switch_all(rs: dict) -> dict:
    return {k: _no_switch(r) for k, r in rs.items()}


def _routine_view(r: Optional[dict]) -> Optional[dict]:
    return None if r is None else {"name": r["name"], "schedule": r["schedule"], "enabled": r["enabled"],
                                    "prompt": safe.cap(r["prompt"], 2000)}


# ---- the planner -----------------------------------------------------------------------------------------------------

class H2G:
    """One plugin data folder's Hermes -> Grok state.

    `hermes(pid)` -> {"name", "description", "instructions", "routines": [{"name","schedule","prompt","enabled"}]}
        (the twin's Hermes side now) ; `bot(pid)` -> the Grok bot id of the twin, "" when unknown ;
    `wanted(pid, item)` -> the owner's direction for that item (sync_panel.wanted), or None without the panel ;
    `pause_hermes(pid, rkey)` / `resume_hermes(pid, rkey)` -> True when the twin's Hermes job for that routine is
        paused / switched on after the call (single-runner rule) ; `mark_synced(pid, item)` tells the panel.
    """

    def __init__(self, central: Path, writer: GrokWriter, hermes: Callable[[str], dict], bot: Callable[[str], str],
                 wanted: Optional[Callable[[str, str], Optional[str]]] = None,
                 pause_hermes: Optional[Callable[[str, str], bool]] = None,
                 resume_hermes: Optional[Callable[[str, str], bool]] = None,
                 mark_synced: Optional[Callable[[str, str], None]] = None):
        self.central = Path(central)
        self.writer = writer
        self._hermes = hermes
        self._bot = bot
        self._wanted = wanted
        self._pause = pause_hermes or (lambda pid, k: False)
        self._resume = resume_hermes or (lambda pid, k: False)
        self._mark = mark_synced or (lambda pid, item: None)

    # -- state file
    def _path(self) -> Path:
        return self.central / STATE_FILE

    def _read(self) -> dict:
        d = store.read_json(self._path(), None)
        if not isinstance(d, dict) or d.get("v") != V:
            d = {"v": V}
        for k in ("allow", "base", "runner", "keep_grok"):
            v = d.get(k)
            d[k] = {p: e for p, e in v.items() if isinstance(e, dict)} if isinstance(v, dict) else {}
        ch = d.get("changes") if isinstance(d.get("changes"), list) else []
        d["changes"] = [c for c in ch if isinstance(c, dict) and isinstance(c.get("id"), str)
                        and isinstance(c.get("ops"), list) and all(isinstance(o, dict) for o in c["ops"])]
        for c in d["changes"]:
            if not isinstance(c.get("before"), dict):
                c["before"] = {}
            if not isinstance(c.get("before_src"), dict):
                c["before_src"] = {}
        return d

    def _write(self, d: dict) -> None:
        d["changes"] = d["changes"][-MAX_CHANGES:]
        store.atomic_write_json(self._path(), d)

    # -- allowlist (rule 1)
    def allowed(self, pid: str) -> list:
        a = self._read()["allow"].get(pid)
        return [i for i in ITEMS if isinstance(a, dict) and i in (a.get("items") or [])]

    def allow(self, pid: str, items: Any) -> dict:
        if not valid_id(pid):
            raise H2GError("bad_input", "Not a twin.")
        items = list(ITEMS) if items in ("all", None) else items
        if not isinstance(items, (list, tuple)) or not items or not all(i in ITEMS for i in items):
            raise H2GError("bad_input", "Choose from: " + ", ".join(ITEMS) + ".")
        bot = self._bot(pid)
        if not bot:
            raise H2GError("no_bot", "Hermes does not know which Grok bot this twin follows.")
        with _LOCK, store.locked():
            d = self._read()
            d["allow"][pid] = {"items": [i for i in ITEMS if i in items], "bot": bot, "at": now_iso()}
            if pid not in d["base"]:  # the first base is Grok as the writer reads it (or Hermes now: nothing to send)
                d["base"][pid] = self._first_base(pid, bot)
            self._write(d)
        return self.info(pid)

    def disallow(self, pid: str, items: Any = "all") -> dict:
        with _LOCK, store.locked():
            d = self._read()
            a = d["allow"].get(pid) or {}
            keep = [] if items in ("all", None) else [i for i in (a.get("items") or []) if i not in items]
            if keep:
                a["items"] = keep
                d["allow"][pid] = a
            else:
                d["allow"].pop(pid, None)
            self._write(d)
        return self.info(pid)

    def _first_base(self, pid: str, bot: str) -> dict:
        """What Grok holds when the owner first allows: the read-back for what the writer can read, and Hermes as it
        is now for the rest (so only an edit made in Hermes from now on is sent; nothing older is pushed)."""
        g = read_grok(self.writer, bot)
        h = clean_side(self._hermes(pid))
        known = g["known"] if g is not None else set()
        side = {k: (g[k] if k in known else h[k]) for k in TEXT_ITEMS}
        side["routines"] = list(g["routines"].values()) if "routines" in known else []
        return {"side": side, "at": now_iso(), "from": "grok" if known else "hermes", "known": sorted(known),
                "src": {k: ("grok" if k in known else "hermes") for k in ITEMS}}

    # -- detection (rules 2, 3, 5)
    def _item_open(self, pid: str, item: str, allowed: list) -> Optional[str]:
        if item not in allowed:
            return ITEM_NOT_ALLOWED.format(item=item)
        if self._wanted is not None:
            w = self._wanted(pid, item)
            if w not in ("hermes_to_grok", "both"):
                return NOT_WANTED.format(item=item)
        return None

    def _plan(self, pid: str) -> dict:
        d = self._read()
        allowed = self.allowed(pid)
        bot = (d["allow"].get(pid) or {}).get("bot") or self._bot(pid)
        if not allowed or not bot:
            raise H2GError("not_allowed", NOT_ALLOWED)
        perm = self.writer.permission(bot)
        base = clean_side((d["base"].get(pid) or {}).get("side"))
        src = (d["base"].get(pid) or {}).get("src")
        src = src if isinstance(src, dict) else {}
        now_h = clean_side(self._hermes(pid))
        grok = read_grok(self.writer, bot)
        runner = {k: v for k, v in (d["runner"].get(pid) or {}).items() if v in RUNNERS or v == "nowhere"}
        changes, held, ops, off = [], [], [], []
        kept = (d.get("keep_grok") or {}).get(pid) or {}
        for item in TEXT_ITEMS:
            why = self._item_open(pid, item, allowed)
            if now_h[item] == base[item] or kept.get(item) == _digest(now_h[item]):
                continue
            if why:  # changed in Hermes, but the owner did not choose to send this item: say so, send nothing
                off.append({"item": item, "why": why})
                continue
            known = grok is not None and item in grok["known"]
            if known and grok[item] != base[item] and grok[item] != now_h[item]:
                held.append({"item": item, "code": "conflict", "why": CONFLICT.format(item=item)})
                continue
            if not now_h[item].strip():
                held.append({"item": item, "code": "empty", "why": EMPTY.format(item=item)})
                continue
            if _secrets.has_secret(now_h[item]):
                held.append({"item": item, "code": "secret", "why": HELD_SECRET.format(item=item)})
                continue
            if known and grok[item] == now_h[item]:
                continue  # already the same in Grok (made there by hand): only the base moves, in apply
            ch = {"item": item, "before": safe.cap(base[item], 2000) if item != "instructions" else None,
                  "after": safe.cap(now_h[item], 2000) if item != "instructions" else None}
            if not known:  # Grok's text is not read: say plainly what the base is and that it is replaced whole
                ch["grok_unknown"] = True
                ch["note"] = REPLACES_ALL.format(item=item)
            if item == "instructions":
                ch["diff"] = text_diff(base[item], now_h[item],
                                       label="Grok now" if known else SRC_LABEL.get(src.get(item), SRC_LABEL["hermes"]))
            changes.append(ch)
            ops.append({"op": "set", "item": item, "value": now_h[item]})
        why = self._item_open(pid, "routines", allowed)
        if not why:
            self._plan_routines(now_h, base, grok, runner, changes, held, ops)
        elif "routines" in allowed and _no_switch_all(now_h["routines"]) != _no_switch_all(base["routines"]):
            off.append({"item": "routines", "why": why})
        pending = [c["id"] for c in d["changes"] if c.get("pid") == pid and c.get("state") == "sent"]
        plan = {"pid": pid, "bot": bot, "permission": perm, "ops": ops, "changes": changes, "held": held, "off": off,
                "pending": pending[-1] if pending else None,
                "grok_read": sorted(grok["known"]) if grok is not None else [], "writer": getattr(self.writer, "kind", "?")}
        plan["change_id"] = "c-" + _digest({"pid": pid, "bot": bot, "ops": ops, "base": base})[:16]
        plan["_base"], plan["_hermes"], plan["_grok"], plan["_src"] = base, now_h, grok, src
        return plan

    def _plan_routines(self, now_h: dict, base: dict, grok: Optional[dict], runner: dict, changes: list,
                       held: list, ops: list) -> None:
        hr, br = now_h["routines"], base["routines"]
        gr = grok["routines"] if grok is not None and "routines" in grok["known"] else None
        runner = effective_runners(runner, hr, gr)
        if not hr and br:
            held.append({"item": "routines", "code": "empty", "why": NO_ROUTINES})
            return
        for key in sorted(set(hr) | set(br)):
            h, b = hr.get(key), br.get(key)
            # what Grok should hold: the Hermes routine, switched on in Grok only when Grok is its runner
            keep = runner.get(key) == "keep"
            want = None if h is None else dict(h, enabled=None if keep else runner.get(key) == "grok")
            have = None if b is None else dict(b)
            if want == have or (keep and want is not None and have is not None and
                                _no_switch(want) == _no_switch(have)):
                continue
            g = gr.get(key) if gr is not None else None
            if gr is not None and _no_switch(g) != _no_switch(have) and g != want and \
                    not (keep and _no_switch(g) == _no_switch(want)):
                held.append({"item": f"routine:{key}", "code": "conflict",
                             "why": CONFLICT.format(item=f"routine \"{(h or b)['name']}\"")})
                continue
            if want is not None and _secrets.has_secret(want["prompt"]):
                held.append({"item": f"routine:{key}", "code": "secret",
                             "why": HELD_SECRET.format(item=f"routine \"{want['name']}\"")})
                continue
            if gr is not None and (g == want or (keep and g is not None and _no_switch(g) == _no_switch(want))):
                continue
            changes.append({"item": f"routine:{key}", "before": _routine_view(have), "after": _routine_view(want),
                            "grok_unknown": gr is None and have is None})
            ops.append({"op": "routine.remove", "key": key} if want is None else dict(want, op="routine.put"))

    @_serial
    def resolve(self, pid: str, item: str, keep: str) -> dict:
        """A held conflict: keep="hermes" sends the Hermes version over Grok's on the next apply (the base becomes
        Grok's version, so the plan sees only Hermes' change); keep="grok" leaves Grok as it is and stops asking
        until the Hermes version changes again (bringing Grok's version into Hermes is the Grok -> Hermes side's job)."""
        if item not in TEXT_ITEMS or keep not in ("hermes", "grok"):
            raise H2GError("bad_input", "Say name, description or instructions, and hermes or grok.")
        d = self._read()
        bot = (d["allow"].get(pid) or {}).get("bot") or self._bot(pid)
        grok = read_grok(self.writer, bot)
        if grok is None or item not in grok["known"]:
            raise H2GError("no_read", "The Mirror cannot read this from Grok now, so Hermes cannot compare.")
        with store.locked():
            d = self._read()
            b = d["base"].setdefault(pid, {"side": clean_side({}), "at": None, "from": "hermes"})
            side = clean_side(b.get("side"))
            side[item] = grok[item]
            b["side"] = {**{k: side[k] for k in TEXT_ITEMS}, "routines": list(side["routines"].values())}
            b["at"] = now_iso()
            b.setdefault("src", {})[item] = "grok"
            kg = d.setdefault("keep_grok", {}).setdefault(pid, {})
            if keep == "grok":
                kg[item] = _digest(clean_side(self._hermes(pid))[item])
            else:
                kg.pop(item, None)
            self._write(d)
        return self.preview(pid)

    # -- what the owner sees
    def info(self, pid: str) -> dict:
        d = self._read()
        a = d["allow"].get(pid) or {}
        base = d["base"].get(pid) or {}
        mine = [c for c in d["changes"] if c.get("pid") == pid]
        return {"pair_id": pid, "allowed": self.allowed(pid), "bot_known": bool(a.get("bot") or self._bot(pid)),
                "writer": getattr(self.writer, "kind", "?"), "base_at": base.get("at"), "base_from": base.get("from"),
                "runner": self._runners_now(pid, d),
                "changes": [{k: c.get(k) for k in ("id", "at", "state", "items", "undo_of", "undone_by", "message")}
                            for c in mine[-20:]]}

    def _runners_now(self, pid: str, d: dict) -> dict:
        a = d["allow"].get(pid) or {}
        if "routines" not in (a.get("items") or []):
            return {}
        g = read_grok(self.writer, a.get("bot") or "")
        gr = g["routines"] if g is not None and "routines" in g["known"] else None
        return effective_runners(d["runner"].get(pid) or {}, clean_side(self._hermes(pid))["routines"], gr)

    def preview(self, pid: str) -> dict:
        plan = self._plan(pid)
        out = {k: v for k, v in plan.items() if not k.startswith("_") and k != "ops"}
        out["nothing"] = not plan["ops"]
        if plan["ops"] or plan["held"]:
            out["sentence"] = (f"{len(plan['ops'])} change(s) to make in Grok" +
                               (f", {len(plan['held'])} held" if plan["held"] else ""))
        elif plan["off"]:
            out["sentence"] = "Nothing is sent: " + plan["off"][0]["why"]
        else:
            out["sentence"] = NOTHING
        if plan["pending"]:
            out["blocked"] = PENDING
        elif plan["permission"] != "ready":
            out["blocked"] = {"not_set_up": "The Mirror is not set up for this bot yet.",
                              "no_permission": "The Mirror has no permission to change this bot: set up "
                                               "pre-approval in Grok first."}.get(plan["permission"], "The Mirror cannot be reached.")
        return out

    # -- apply (rule 4) and undo (rule 6)
    @_serial
    def apply(self, pid: str, change_id: str) -> dict:
        """`change_id` is the preview's id (what the owner saw). The request then goes out under an id of its own,
        used once, which the answer names (`change_id` in the answer, used for undo)."""
        if not isinstance(change_id, str) or not CHANGE_RE.match(change_id):
            raise H2GError("bad_input", "Not a change id.")
        plan = self._plan(pid)
        if plan["change_id"] != change_id:
            raise H2GError("stale", STALE)
        if plan["pending"]:
            raise H2GError("pending", PENDING)
        if plan["permission"] != "ready":
            raise H2GError("no_permission", self.preview(pid).get("blocked") or NOT_ALLOWED)
        if not plan["ops"]:
            self._settle_base(pid, plan)
            return {"state": "done", "change_id": change_id, "sentence": NOTHING}
        plan = dict(plan, change_id=_new_id(change_id), preview_id=change_id)
        receipt = self.writer.apply(plan["bot"], plan["ops"], plan["change_id"])
        return self._record(pid, plan, receipt, undo_of=None)

    def _record(self, pid: str, plan: dict, receipt: dict, undo_of: Optional[str]) -> dict:
        base = plan["_base"]
        before = {}
        for op in plan["ops"]:
            if op["op"] == "set":
                before[op["item"]] = base[op["item"]]
            else:
                before["routine:" + op["key"]] = base["routines"].get(op["key"])
        src = plan.get("_src") or {}
        entry = {"id": plan["change_id"], "pid": pid, "bot": plan["bot"], "at": now_iso(), "ops": plan["ops"],
                 "before": before, "items": sorted({c["item"] for c in plan["changes"]}), "undo_of": undo_of,
                 "before_src": {k: src.get(k, "hermes") for k in before if k in TEXT_ITEMS},
                 "kind": plan.get("kind", "change"), "preview_id": plan.get("preview_id"),
                 "state": receipt.get("state") if receipt.get("state") in ("done", "sent", "failed") else "failed",
                 "message": safe.clean_summary(receipt.get("message"), 300)}
        with store.locked():
            d = self._read()
            d["changes"] = [c for c in d["changes"] if c.get("id") != entry["id"]] + [entry]
            self._write(d)
        if entry["state"] != "sent":
            self._finish(entry, receipt)
        return {"state": entry["state"], "change_id": entry["id"], "items": entry["items"],
                "message": entry["message"], "sentence": {"done": "Made in Grok.", "sent": "Sent to the Mirror: it "
                "makes the change on its next run.", "failed": entry["message"] or "Grok was not changed."}[entry["state"]]}

    def _finish(self, entry: dict, receipt: dict) -> None:
        """A final receipt: the base moves by the ops that were done (only those), and the panel hears of it."""
        done = set(receipt.get("done") or []) if isinstance(receipt.get("done"), list) else set()
        with store.locked():
            d = self._read()
            b = d["base"].setdefault(entry["pid"], {"side": clean_side({}), "at": None, "from": "hermes"})
            side = clean_side(b.get("side"))
            for i, op in enumerate(entry["ops"]):
                if i not in done:
                    continue
                if op["op"] == "set":
                    side[op["item"]] = op["value"]
                elif op["op"] == "routine.put":
                    was = side["routines"].get(op["key"]) or {}
                    side["routines"][op["key"]] = dict({k: op[k] for k in ("key", "name", "schedule", "prompt")},
                                                       enabled=bool(was.get("enabled")) if op["enabled"] is None
                                                       else op["enabled"])
                elif op["op"] == "routine.remove":
                    side["routines"].pop(op["key"], None)
                elif op["op"] == "routine.enable" and op["key"] in side["routines"]:
                    side["routines"][op["key"]]["enabled"] = bool(op["enabled"])
            b["side"] = {**{k: side[k] for k in TEXT_ITEMS}, "routines": list(side["routines"].values())}
            b["at"], b["from"] = now_iso(), "applied"
            bs = b.setdefault("src", {}) if isinstance(b.get("src"), dict) else b.setdefault("src", {})
            for i, op in enumerate(entry["ops"]):
                if i in done and op["op"] == "set":
                    bs[op["item"]] = "applied"
            for c in d["changes"]:
                if c.get("id") == entry["id"]:
                    c["state"] = "done" if receipt.get("state") == "done" else "failed"
                    c["done"] = sorted(done)
                    c["message"] = safe.clean_summary(receipt.get("message"), 300)
            self._write(d)
        forget = getattr(self.writer, "forget", None)
        if forget is not None:  # an answered request leaves the shared folder: it can never be made again
            forget(entry["id"])
        for item in {("routines" if op["op"].startswith("routine") else op["item"])
                     for i, op in enumerate(entry["ops"]) if i in done}:
            self._mark(entry["pid"], item)

    def _settle_base(self, pid: str, plan: dict) -> None:
        """Nothing to send: for each item Grok was READ to hold the same as Hermes (made there by hand), the base
        becomes that common value. Nothing else moves: a held, unknown or not-chosen item keeps its base."""
        g, h = plan["_grok"], plan["_hermes"]
        if g is None:
            return
        same = [k for k in TEXT_ITEMS if k in g["known"] and g[k] == h[k]]
        same_r = g["routines"] == h["routines"] if "routines" in g["known"] else False
        if not same and not same_r:
            return
        with store.locked():
            d = self._read()
            b = d["base"].setdefault(pid, {"side": clean_side({}), "at": None, "from": "hermes"})
            side = clean_side(b.get("side"))
            for k in same:
                side[k] = h[k]
                b.setdefault("src", {})[k] = "grok"
            if same_r:
                side["routines"] = dict(h["routines"])
            b["side"] = {**{k: side[k] for k in TEXT_ITEMS}, "routines": list(side["routines"].values())}
            b["at"] = now_iso()
            self._write(d)

    @_serial
    def poll(self) -> list:
        """Final receipts for changes that were "sent" (the Mirror makes them on its next run)."""
        out = []
        for c in [c for c in self._read()["changes"] if c.get("state") == "sent"]:
            rec = self.writer.poll(c["id"], len(c["ops"]))
            if rec and rec.get("state") in ("done", "failed"):
                self._finish(c, rec)
                out.append({"change_id": c["id"], "state": rec["state"]})
        return out

    @_serial
    def undo(self, pid: str, change_id: str) -> dict:
        if not isinstance(change_id, str) or not CHANGE_RE.match(change_id):
            raise H2GError("bad_input", "Not a change id.")
        d = self._read()
        if any(x.get("pid") == pid and x.get("state") == "sent" for x in d["changes"]):
            raise H2GError("pending", PENDING)
        c = next((c for c in d["changes"] if c.get("id") == change_id and c.get("pid") == pid), None)
        if c is None or c.get("state") != "done":
            raise H2GError("no_change", "There is no change of this twin with that id that was made in Grok.")
        if c.get("undone_by"):
            raise H2GError("undone", "That change was undone already.")
        if c.get("kind") == "move":
            raise H2GError("is_move", "That change moved a routine. To move it back, choose where it runs again.")
        bot = c["bot"]
        grok = read_grok(self.writer, bot)
        ops, skipped = [], []
        for op in c["ops"]:
            if op["op"] == "set":
                if grok is not None and op["item"] in grok["known"] and grok[op["item"]] != op["value"]:
                    raise H2GError("changed_in_grok", NO_UNDO)
                if (c.get("before_src") or {}).get(op["item"], "hermes") == "hermes":
                    skipped.append(op["item"])  # Grok's text before this change was never known: nothing true to put back
                    continue
                was = c["before"].get(op["item"])
                was = was if isinstance(was, str) else ""
                if not was.strip():
                    skipped.append(op["item"])  # it was empty before: Hermes never empties a text in Grok
                    continue
                ops.append({"op": "set", "item": op["item"], "value": was})
            else:
                key = op["key"]
                was = c["before"].get("routine:" + key)
                if grok is not None and "routines" in grok["known"]:
                    now_g = grok["routines"].get(key)
                    put = {k: op[k] for k in ("key", "name", "schedule", "prompt", "enabled")} \
                        if op["op"] == "routine.put" else None
                    if _no_switch(now_g) != _no_switch(put):  # on/off moves with the runner, not with this change
                        raise H2GError("changed_in_grok", NO_UNDO)
                # an undone routine comes back paused in Grok when Grok is not its runner
                runner = (d["runner"].get(pid) or {}).get(key)
                ops.append({"op": "routine.remove", "key": key} if was is None else
                           dict(was, enabled=None if runner in (None, "keep") else
                                bool(was.get("enabled")) and runner == "grok", op="routine.put"))
        if not ops:
            raise H2GError("empty", UNKNOWN_BEFORE)
        base = clean_side((d["base"].get(pid) or {}).get("side"))
        uid = _new_id({"undo": change_id})
        receipt = self.writer.apply(bot, ops, uid)
        bsrc = (d["base"].get(pid) or {}).get("src")
        plan = {"change_id": uid, "bot": bot, "ops": ops, "_base": base, "_src": bsrc if isinstance(bsrc, dict) else {},
                "changes": [{"item": i} for i in c.get("items") or []]}
        out = self._record(pid, plan, receipt, undo_of=change_id)
        if skipped:
            out["not_undone"] = skipped
            out["sentence"] += (" Not put back: " + ", ".join(skipped) + ". " + UNKNOWN_BEFORE)
        if out["state"] in ("done", "sent"):
            with store.locked():
                d = self._read()
                for x in d["changes"]:
                    if x.get("id") == change_id:
                        x["undone_by"] = uid
                self._write(d)
        return out

    # -- routines run in one place (rule 7)
    def runner(self, pid: str, rkey: str) -> str:
        """"hermes", "grok", or "nowhere" (a handover that stopped half way: paused on both sides, never on in both)."""
        d = self._read()
        r = (d["runner"].get(pid) or {}).get(rkey)
        if r in RUNNERS or r == "nowhere":
            return r
        return self._runners_now(pid, d).get(rkey) or "keep"

    @_serial
    def move_runner(self, pid: str, rkey: str, to: str) -> dict:
        """Hand a routine over to Grok or back to Hermes: pause the old side first, then switch on the new one."""
        if to not in RUNNERS or not isinstance(rkey, str) or not RKEY_RE.match(rkey):
            raise H2GError("bad_input", "Say which routine, and grok or hermes.")
        if "routines" not in self.allowed(pid):
            raise H2GError("not_allowed", ITEM_NOT_ALLOWED.format(item="routines"))
        d = self._read()
        if any(x.get("pid") == pid and x.get("state") == "sent" for x in d["changes"]):
            raise H2GError("pending", PENDING)
        bot = (d["allow"].get(pid) or {}).get("bot") or self._bot(pid)
        base = clean_side((d["base"].get(pid) or {}).get("side"))
        if rkey not in base["routines"]:
            raise H2GError("not_in_grok", "This routine is not in Grok yet: send it to Grok first (it arrives paused).")
        if self.runner(pid, rkey) == to:
            return {"state": "done", "runner": to, "sentence": f"It already runs in {to.capitalize()}."}
        if to == "grok":
            if not self._pause(pid, rkey):  # step 1: Hermes stops first, or nothing happens
                raise H2GError("hermes_not_paused", "Hermes could not pause its copy of this routine, so it is not "
                                                    "switched on in Grok.")
            self._set_runner(pid, rkey, "nowhere")
            op = {"op": "routine.enable", "key": rkey, "enabled": True}
        else:
            op = {"op": "routine.enable", "key": rkey, "enabled": False}
        cid = _new_id({"move": rkey, "to": to})
        receipt = self.writer.apply(bot, [op], cid)
        plan = {"change_id": cid, "bot": bot, "ops": [op], "_base": base, "changes": [{"item": f"routine:{rkey}"}],
                "kind": "move"}
        out = self._record(pid, plan, receipt, undo_of=None)
        if out["state"] == "failed":
            # to Grok: Hermes is paused and Grok did not start -> runs nowhere (honest, never twice)
            out["runner"] = "nowhere" if to == "grok" else "grok"
            out["sentence"] = ("Grok did not switch it on; Hermes stays paused, so it runs nowhere until you try "
                               "again or move it back." if to == "grok" else
                               "Grok did not pause it, so Hermes does not switch it on.")
            return out
        if out["state"] == "sent":
            out["runner"] = "nowhere"
            self._set_pending_move(pid, rkey, to, cid)
            return out
        return self._complete_move(pid, rkey, to, out)

    def _complete_move(self, pid: str, rkey: str, to: str, out: dict) -> dict:
        if to == "hermes":
            if not self._resume(pid, rkey):  # Grok is paused (receipt says so); Hermes failed to start
                self._set_runner(pid, rkey, "nowhere")
                out["runner"] = "nowhere"
                out["sentence"] = "Grok paused it, but Hermes could not switch its copy on: it runs nowhere now."
                return out
        self._set_runner(pid, rkey, to)
        out["runner"] = to
        out["sentence"] = f"It runs in {to.capitalize()} now, and only there."
        return out

    def _set_runner(self, pid: str, rkey: str, to: str) -> None:
        with store.locked():
            d = self._read()
            r = d["runner"].setdefault(pid, {})
            if to == "nowhere":
                r[rkey] = "nowhere"
            else:
                r[rkey] = to
            r.pop("_move:" + rkey, None)
            self._write(d)

    def _set_pending_move(self, pid: str, rkey: str, to: str, cid: str) -> None:
        with store.locked():
            d = self._read()
            d["runner"].setdefault(pid, {})["_move:" + rkey] = {"to": to, "change": cid}
            self._write(d)

    @_serial
    def give_up(self, pid: str) -> dict:
        """Stop waiting for a Mirror that does not answer: its requests leave the shared folder (if it has not copied
        them yet, it never makes them), the base does not move, and a half-made routine move stays on the safe side
        (to Grok: Hermes stays paused; to Hermes: Hermes is not switched on)."""
        d = self._read()
        mine = [c for c in d["changes"] if c.get("pid") == pid and c.get("state") == "sent"]
        if not mine:
            return {"state": "done", "sentence": "Nothing is waiting for the Mirror."}
        forget = getattr(self.writer, "forget", None)
        for c in mine:
            c["state"], c["message"] = "failed", GAVE_UP
            if forget is not None:
                forget(c["id"])
        self._write(d)
        self.finish_moves()
        return {"state": "done", "sentence": GAVE_UP, "gave_up": [c["id"] for c in mine]}

    @_serial
    def finish_moves(self) -> list:
        """After poll(): a handover whose Grok step is done now completes its Hermes step."""
        self.poll()
        out = []
        d = self._read()
        for pid, r in d["runner"].items():
            for k, v in list(r.items()):
                if not k.startswith("_move:") or not isinstance(v, dict):
                    continue
                c = next((c for c in d["changes"] if c.get("id") == v.get("change")), None)
                if c is None or c.get("state") == "sent":
                    continue
                rkey = k[6:]
                if c.get("state") == "done":
                    out.append(self._complete_move(pid, rkey, v["to"], {"state": "done", "change_id": c["id"]}))
                else:
                    self._set_runner(pid, rkey, "nowhere" if v["to"] == "grok" else "grok")
        return out
