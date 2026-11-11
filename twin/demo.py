"""DemoEngine: the whole botport.twin.v1 contract on invented fixtures, persisted in <central>/demo/.

No botport, no Grok account, no network, and the real Hermes profile is never written: the "Hermes side"
of a demo twin lives in demo/hermes/<pair>.json, and its paused cron jobs exist only in the demo world.

Layout:
  demo/world.json            roster of demo pairs + profile
  demo/grok/<grok_id>.json   the pretend Grok side (what the Grok app would hold)
  demo/hermes/<pair>.json    the demo twin's Hermes side + sync baseline, conflicts, held secrets, activity
  demo/check.json            stat signatures seen at the last sync (metadata-only change check)
  demo/restore/<pair>.json   restore points: old Hermes copies kept when they were replaced (ITEM@DIGEST12)
  demo/preview-skills/...    skill files written for the import preview, scanned by the skill scanner
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import os
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from . import osflags as _osflags
from . import secrets as _secrets
from . import store
from .engine import Engine, EngineError
from .model import (MIRROR_NOTE, SAMPLE_PROFILE, coverage, derive_state, iso, now_iso, parse_iso, plural, valid_id,
                    valid_item_id)

T0 = "2000-01-03T09:00:00Z"  # fixture creation time (static fixture data lives in the year 2000)
MAX_POINTS = 20
MAX_ACTIVITY = 60
CONFLICT_SKILL = "greet-visitors"
HERMES_EDIT_SKILL = "fun-fact"
HIDDEN = "[not shown: looks like a password or key]"
# The demo's fake key is never written to disk: the pretend Grok side stores this token and the value is
# built in memory when the pretend Grok app is read (so tests can prove the value is absent from every file).
_FAKE_KEY_TOKEN = "{{demo-fake-key}}"
MIRROR_STEPS = (
    {"id": "mirror_bot", "what": "In the Grok app, make a small sync bot (if you have none yet) and paste the "
                                 "instructions below as its instructions."},
    {"id": "share_folder", "what": "Give the sync bot's Google Drive connection access to the shared folder."},
    {"id": "add_pair", "what": "In the sync bot's chat, attach the guide file and this twin's pass (a small file), "
                               "and ask it to add this bot."},
    {"id": "test", "what": "Press Test on the sync bot's routine in Grok, then press Check connection."},
)
DEMO_CHARTER = ("# Sync bot instructions (sample)\nThis is the sample. With a real twin, the full instructions for "
                "your sync bot appear here, ready to copy.\n")


# What took an earlier version's place, by the reason it was kept under.
_WHY = {"Before an update from Grok": "grok", "Before you kept the Grok version": "choice",
        "Before you put an earlier version back": "put_back", "Before you changed the instructions": "paste",
        "Before you added the instructions": "paste"}


def _fake_key() -> str:
    return "sk-" + "demo" + "0123456789abcdefABCDEF0123456789"[:32]


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace(_FAKE_KEY_TOKEN, _fake_key())
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _collapse(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace(_fake_key(), _FAKE_KEY_TOKEN)
    if isinstance(value, dict):
        return {k: _collapse(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_collapse(v) for v in value]
    return value

# ---- invented fixtures ---------------------------------------------------------------------------
_GROK_FIXTURES: dict[str, dict] = {
    "b-acme-0001": {
        "grok_id": "b-acme-0001",
        "name": "Acme Support",
        "description": "Answers questions about Acme widgets.",
        "instructions": ("You are Acme Support.\n"
                         "Be friendly and brief.\n"
                         "Say when you don't know."),
        "skills": {
            "greet-visitors": ("# Greet visitors\n"
                               "Say a friendly hello to each visitor.\n"
                               "Ask for their name and use it.\n"
                               "Keep it short."),
            "fun-fact": ("# Fun fact\n"
                         "Share one short, friendly fun fact when asked."),
        },
        "routines": ["Morning summary", "Water reminder"],
        "routine_schedules": {"Morning summary": "0 8 * * *", "Water reminder": "0 15 * * *"},
        "connectors": ["example-files"],
    },
    "b-recipe-0002": {
        "grok_id": "b-recipe-0002",
        "name": "Recipe Planner",
        "description": "Plans the week's meals from what is already in the fridge.",
        "instructions": ("You are Recipe Planner.\n"
                         "Suggest meals that use up what is in the fridge first.\n"
                         "Ask about allergies before the first plan."),
        "skills": {
            "pantry-check": "# Pantry check\nAsk what is in the fridge and cupboard, then group it by use-by date.",
            "shopping-list": "# Shopping list\nTurn the week's plan into one list grouped by shop aisle.",
        },
        "routines": ["Sunday meal plan"],
        "routine_schedules": {"Sunday meal plan": "0 17 * * 0"},
        "connectors": [],
    },
}
# Stored dates stay in the year 2000 (fixture rule); `offset_s` places each entry relative to the moment the
# demo started, so recent activity reads "12 min ago" instead of a date in 2000.
_FIXTURE_ACTIVITY = [
    {"at": "2000-01-03T09:30:00Z", "offset_s": -40 * 60, "side": "both", "kind": "sync",
     "summary": "Twin created from Grok"},
    {"at": "2000-01-04T14:10:00Z", "offset_s": -25 * 60, "side": "grok", "kind": "name_change",
     "summary": "The bot was renamed in Grok"},
    {"at": "2000-01-05T08:00:00Z", "offset_s": -6 * 60, "side": "grok", "kind": "routine_change",
     "summary": "Routine ran in Grok: Morning summary"},
]
_DESCRIPTION_EDITS = [
    "Answers questions about Acme widgets and gadgets.",
    "Answers simple questions about Acme widgets.",
    "Answers questions about Acme widgets.",
]
# The conflict: one neutral word changed on each side of the same line.
_CONFLICT_GROK = ("# Greet visitors\n"
                  "Say a warm hello to each visitor.\n"
                  "Ask for their name and use it.\n"
                  "Keep it short.")
_CONFLICT_HERMES = ("# Greet visitors\n"
                    "Say a cheerful hello to each visitor.\n"
                    "Ask for their name and use it.\n"
                    "Keep it short.")


# The Add-twins list shows when each fixture bot last changed, relative to the moment the demo started
# (stored dates stay in the year 2000), so it reads "12 min ago" instead of a date in 2000.
_LIST_OFFSET_S = {"b-acme-0001": -12 * 60, "b-recipe-0002": -3 * 3600}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s or "bot")[:48]


def profile_slug(name: Any) -> str:
    """A Hermes profile name from a bot name: lower case, [a-z0-9_-], at most 64 characters."""
    s = re.sub(r"[^a-z0-9_-]+", "-", str(name or "").lower())
    s = re.sub(r"-{2,}", "-", s).strip("-_")[:64].strip("-_")
    if s and _osflags.reserved_name(s):  # "con", "aux", "com1"...: Windows cannot have a folder called that
        s = f"{s[:60]}-bot"
    return s or "bot"


def unique_profile(name: Any, used: Any) -> str:
    """profile_slug(name), with -2, -3, ... added while that profile already backs a twin."""
    base = cand = profile_slug(name)
    n = 2
    # compared without case: "Acme" and "acme" are one folder on Windows and on a standard macOS disk
    used = {str(u).lower() for u in used}
    while cand in used:
        tail = f"-{n}"
        cand = base[:64 - len(tail)].rstrip("-_") + tail
        n += 1
    return cand


def _digest(text: Optional[str]) -> str:
    if text is None:
        return "-"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _stat(path: Path) -> list:
    try:
        st = path.stat()
        return [st.st_mtime_ns, st.st_size, st.st_ino]
    except OSError:
        return [0, 0, 0]


class DemoEngine(Engine):
    name = "demo"

    def __init__(self, base: Optional[Path] = None) -> None:
        self.central = store.central_dir() if base is None else Path(base)
        self.dir = self.central / "demo"
        self.reserved = lambda: set()  # ids of real twins (set by the service)

    # ---- files -----------------------------------------------------------------------------------
    def _p(self, *parts: str) -> Path:
        return store.safe_child(self.dir, *parts)

    def exists(self) -> bool:
        return (self.dir / "world.json").is_file()

    def _world(self) -> dict:
        w = store.read_json(self._p("world.json"), None)
        if not isinstance(w, dict):
            raise EngineError("demo_missing", "The sample is not on.",
                              "On the Twins page press See a sample, or run hermes twin demo.")
        w.setdefault("pairs", {})
        return w

    def _grok(self, gid: str) -> Optional[dict]:
        if not valid_id(gid):
            return None
        g = store.read_json(self._p("grok", f"{gid}.json"), None)
        return _expand(g) if isinstance(g, dict) else None

    def _write_grok(self, gid: str, data: dict, bump: bool = True) -> None:
        path = self._p("grok", f"{gid}.json")
        old = _stat(path)[0]
        data["updated_at"] = now_iso() if bump else data.get("updated_at", T0)
        store.atomic_write_json(path, _collapse(data))
        if bump:  # make sure the metadata check sees a change even on coarse-mtime filesystems
            ns = max(time.time_ns(), old + 1_000_000)
            os.utime(path, ns=(ns, ns))

    def _twin(self, pid: str) -> dict:
        w = self._world()
        if not valid_id(pid) or pid not in w["pairs"]:
            raise EngineError("pair_not_found", f"No sample twin called {pid}.")
        t = store.read_json(self._p("hermes", f"{pid}.json"), None)
        if not isinstance(t, dict):
            raise EngineError("internal", "The sample twin could not be read.",
                              "End the sample and start it again (hermes twin demo remove, then hermes twin demo).")
        return t

    def _write_twin(self, pid: str, t: dict) -> None:
        store.atomic_write_json(self._p("hermes", f"{pid}.json"), t)

    # ---- world lifecycle -------------------------------------------------------------------------
    def start(self, profile: str = SAMPLE_PROFILE) -> dict:
        """Start the sample. It always ends with the Acme Support sample twin present: a world that lost that twin
        (the owner removed it) is rebuilt from the fixtures instead of answering "already running"."""
        with store.locked():
            if self.exists():
                try:
                    w = self._world()
                    if any(isinstance(m, dict) and m.get("grok_id") == "b-acme-0001" for m in w["pairs"].values()):
                        return {"created": False, "pairs": list(w["pairs"])}
                except EngineError:
                    pass
                store.remove_tree(self.central, self.dir)  # twin-less or unreadable: start again from the fixtures
            store.ensure_dir(self.dir)
            for gid, fx in _GROK_FIXTURES.items():
                g = {k: copy.deepcopy(v) for k, v in fx.items() if k != "routine_schedules"}
                g["skills"] = {n: {"text": t, "changed_at": T0} for n, t in fx["skills"].items()}
                self._write_grok(gid, g, bump=False)
            store.atomic_write_json(self._p("world.json"), {"version": 1, "created_at": now_iso(),
                                                            "profile": profile, "pairs": {}})
            pid = self._create_pair("b-acme-0001", profile, at=T0)
            t = self._twin(pid)
            t["activity"] = copy.deepcopy(_FIXTURE_ACTIVITY) + [
                {"at": now_iso(), "side": "hermes", "kind": "sync", "summary": "Sample started"}]
            t["last_sync_at"] = t["last_change_at"] = now_iso()
            self._write_twin(pid, t)
            self._save_sig(pid)
            return {"created": True, "pairs": [pid]}

    def hide_real(self) -> bool:
        """"Hide my real bots while the sample is on" (the owner's explicit choice; off by default)."""
        if not self.exists():
            return False
        try:
            return self._world().get("hide_real") is True
        except EngineError:
            return False

    def set_hide_real(self, value: bool) -> bool:
        with store.locked():
            w = self._world()
            w["hide_real"] = bool(value)
            store.atomic_write_json(self._p("world.json"), w)
            return w["hide_real"]

    def pair_ids(self) -> list[str]:
        if not self.exists():
            return []
        try:
            return list(self._world()["pairs"])
        except EngineError:
            return []

    def _create_pair(self, gid: str, profile: str, at: Optional[str] = None, from_export: bool = False) -> str:
        """A demo twin always has a (pretend) sync bot connected. `from_export`: the twin started from a one-time
        bot export, so it holds the instructions; a twin made later from the cache alone (Recipe Planner) does
        not, and its instructions stay on the by-hand list, as with a real bot."""
        g = self._grok(gid)
        if g is None:
            raise EngineError("pair_not_found", f"No sample Grok bot {gid}.")
        w = self._world()
        pid = _slug(g["name"])
        if pid in self.reserved():
            pid = f"demo-{pid}"  # a real twin already uses this id: the demo must never shadow it
        n = 2
        while pid in w["pairs"] or pid in self.reserved():
            pid = f"{_slug(g['name'])}-{n}"
            n += 1
        fx = _GROK_FIXTURES.get(gid, {})
        twin: dict[str, Any] = {"pair_id": pid, "grok_id": gid, "profile": profile, "base": {}, "conflicts": {},
                                "held": {}, "secrets_withheld_total": 0, "last_sync_at": at or now_iso(),
                                "last_change_at": at or now_iso(), "pack_gen": 0, "mirror_connected": True,
                                "approved": {}, "in_flight": {}, "activity": [], "skills": {},
                                "instructions_by_hand": not from_export, "instructions_by": None}
        reports: list = []
        for item in self._items(g, twin):
            text = self._get(g, item)
            hold, rep = _secrets.withhold(text or "", item, self._kind(item))
            if hold:
                twin["held"][item] = {"g": _digest(text), "reports": rep}
                reports += rep
                continue
            self._set(twin, item, text, at or now_iso())
            twin["base"][item] = {"g": _digest(text), "h": _digest(text)}
        twin["secrets_withheld_total"] = len(reports)
        twin["jobs"] = [{"job_id": f"demo-{pid}-job-{i + 1}", "name": f"{g['name']}: {r}",
                         "schedule": fx.get("routine_schedules", {}).get(r, "0 9 * * *"), "paused": True}
                        for i, r in enumerate(g.get("routines", []))]
        w["pairs"][pid] = {"grok_id": gid, "profile": profile, "created_at": at or now_iso()}
        store.atomic_write_json(self._p("world.json"), w)
        self._write_twin(pid, twin)
        return pid

    # ---- item helpers ----------------------------------------------------------------------------
    @staticmethod
    def _items(g: dict, t: dict) -> list[str]:
        items = ["persona.name", "persona.description"]
        if not t.get("instructions_by_hand"):  # a cache-only twin never carries instructions (by hand)
            items.append("persona.instructions")
        names = set((g.get("skills") or {}).keys()) | set((t.get("skills") or {}).keys())
        names |= {k.split(":", 1)[1] for k in (t.get("base") or {}) if k.startswith("skill:")}
        return items + [f"skill:{n}" for n in sorted(names)]

    @staticmethod
    def _get(side: dict, item: str) -> Optional[str]:
        if item.startswith("persona."):
            v = side.get(item.split(".", 1)[1])
            return v if isinstance(v, str) else None
        if item.startswith("skill:"):
            s = (side.get("skills") or {}).get(item.split(":", 1)[1])
            return s.get("text") if isinstance(s, dict) else None
        return None

    @staticmethod
    def _set(side: dict, item: str, text: Optional[str], at: str) -> None:
        if item.startswith("persona."):
            side[item.split(".", 1)[1]] = text
        elif item.startswith("skill:"):
            skills = side.setdefault("skills", {})
            name = item.split(":", 1)[1]
            if text is None:
                skills.pop(name, None)
            else:
                skills[name] = {"text": text, "changed_at": at}

    @staticmethod
    def _kind(item: str) -> str:
        return item.split(".", 1)[1] if item.startswith("persona.") else item.split(":", 1)[0]

    def _plan(self, g: Optional[dict], t: dict) -> list[dict]:
        """Per-item comparison against the last-sync baseline (3-way)."""
        plan = []
        if g is None:
            return plan
        for item in self._items(g, t):
            G, H = self._get(g, item), self._get(t, item)
            base = t["base"].get(item, {"g": None, "h": None})
            gd, hd = _digest(G), _digest(H)
            gch, hch = gd != base.get("g"), hd != base.get("h")
            if not gch and not hch:
                continue
            if gch and hch:
                plan.append({"item": item, "op": "same" if gd == hd else "conflict", "G": G, "H": H})
            elif gch:
                held = t["held"].get(item)
                op = "held" if held and held.get("g") == gd else "g2h"
                plan.append({"item": item, "op": op, "G": G, "H": H})
            else:
                op = "h2g" if item.startswith("skill:") else "local"
                plan.append({"item": item, "op": op, "G": G, "H": H})
        return plan

    # ---- restore points (item-scoped, like botport: the old Hermes copy, named ITEM@DIGEST12) ----------
    def _rp_path(self, pid: str) -> Path:
        return self._p("restore", f"{pid}.json")

    def _rp_list(self, pid: str) -> list[dict]:
        data = store.read_json(self._rp_path(pid), {}) or {}
        pts = data.get("points") if isinstance(data, dict) else None
        return [p for p in pts if isinstance(p, dict) and valid_item_id(p.get("id")) and isinstance(p.get("text"), str)
                and valid_item_id(p.get("item"))] if isinstance(pts, list) else []

    def _keep(self, pid: str, item: str, text: Optional[str], reason: str, why: Optional[str] = None) -> Optional[str]:
        """Keep the Hermes copy that is about to be replaced. Returns its restore point id (None: nothing kept)."""
        if text is None:
            return None
        rid = f"{item}@{_digest(text)}"
        pts = [p for p in self._rp_list(pid) if p["id"] != rid]
        pts.append({"id": rid, "item": item, "at": now_iso(), "reason": reason, "text": _collapse(text),
                    "why": why or _WHY.get(reason)})
        store.ensure_dir(self._p("restore"))
        store.atomic_write_json(self._rp_path(pid), {"points": pts[-MAX_POINTS:]})
        return rid

    def _points(self, pid: str) -> list[dict]:
        out = []
        for p in reversed(self._rp_list(pid)):
            item = p["item"]
            out.append({"id": p["id"], "item": item, "kind": self._kind(item),
                        "name": item.split(":", 1)[1] if ":" in item else self._kind(item),
                        "at": p.get("at"), "reason": p.get("reason") or "Kept", "scope": "item",
                        # that version, as the page may show it (password-like lines hidden), and what replaced it
                        "text": _expand(p["text"]), "replaced_by": p.get("why")})
        return out

    def _settle(self, t: dict, g: Optional[dict]) -> None:
        """Drop open conflicts that are no longer edited on both sides (recomputed, never assumed)."""
        live = {p["item"] for p in self._plan(g, t) if p["op"] == "conflict"}
        for item in [i for i in t["conflicts"] if i not in live]:
            t["conflicts"].pop(item, None)

    # ---- signatures (metadata-only change check) -------------------------------------------------
    def _sig(self, pid: str, gid: str) -> list:
        return [_stat(self._p("grok", f"{gid}.json")), _stat(self._p("hermes", f"{pid}.json"))]

    def _save_sig(self, pid: str) -> None:
        w = self._world()
        sigs = store.read_json(self._p("check.json"), {}) or {}
        sigs[pid] = self._sig(pid, w["pairs"][pid]["grok_id"])
        store.atomic_write_json(self._p("check.json"), sigs)

    def _activity(self, t: dict, side: str, kind: str, summary: str) -> None:
        t.setdefault("activity", []).append({"at": now_iso(), "side": side, "kind": kind, "summary": summary[:120]})
        t["activity"] = t["activity"][-MAX_ACTIVITY:]

    def note_activity(self, pid: str, side: str, kind: str, summary: str) -> None:
        with store.locked():
            t = self._twin(pid)
            clean = pid not in self.check()["changed"]
            self._activity(t, side, kind, summary)
            self._write_twin(pid, t)
            if clean:
                self._save_sig(pid)  # an activity marker alone is not a change to sync

    # ---- contract: info / list / create ----------------------------------------------------------
    def info(self) -> dict:
        return {"available": True, "version": "demo", "problem": None, "features": ["twin-parity", "twin-pack"]}

    def grok_list(self, only: Optional[list[str]] = None) -> dict:
        import fnmatch
        if not self.exists():
            return {"app_found": False, "bots": []}
        w = self._world()
        twinned = {v["grok_id"]: k for k, v in w["pairs"].items()}
        start = parse_iso(w.get("created_at"))
        bots = []
        for gid in _GROK_FIXTURES:
            g = self._grok(gid)
            if g is None:
                continue
            if only and not any(fnmatch.fnmatch(g["name"].lower(), p.lower()) for p in only):
                continue
            bots.append({"grok_id": gid, "name": g["name"], "description": _secrets.redact(g.get("description") or "", HIDDEN),
                         "updated_at": self._list_time(gid, g.get("updated_at"), start), "routines": list(g.get("routines", [])),
                         "twinned_pair_id": twinned.get(gid),
                         "fields": {"name": "seconds", "description": "seconds", "instructions": "by_hand",
                                    "skills": "mirror", "routines": "names"}})
        return {"app_found": True, "bots": bots}

    @staticmethod
    def _list_time(gid: str, at: Any, start: Any) -> Any:
        """A fixture time (before the demo started) is shown relative to the demo start; a real edit as is."""
        when = parse_iso(at)
        if start is None or when is None or when >= start:
            return at
        return iso(start + timedelta(seconds=_LIST_OFFSET_S.get(gid, -3600)))

    def twinned(self) -> dict:
        """Demo pair id -> the demo Grok bot it twins."""
        if not self.exists():
            return {}
        try:
            return {k: v.get("grok_id") for k, v in self._world()["pairs"].items() if isinstance(v, dict)}
        except EngineError:
            return {}

    def profiles(self) -> dict:
        """Hermes profile -> demo pair id, for every demo twin."""
        if not self.exists():
            return {}
        try:
            return {v.get("profile"): k for k, v in self._world()["pairs"].items() if isinstance(v, dict)}
        except EngineError:
            return {}

    def create(self, grok_id: str, profile: Optional[str], dry_run: bool) -> dict:
        with store.locked():
            g = self._grok(grok_id)
            if g is None:
                raise EngineError("pair_not_found", f"No sample Grok bot {grok_id}.")
            w = self._world()
            twinned = {v["grok_id"]: k for k, v in w["pairs"].items()}
            if grok_id in twinned and not dry_run:
                return {"pairs": [], "skipped": [{"grok_id": grok_id, "reason": "already_twinned"}]}
            used = {v.get("profile"): k for k, v in w["pairs"].items()}
            # like botport: a new twin never goes into a profile that already backs another twin. A sample twin
            # carries the reserved sample label, never a real Hermes profile, unless the caller names one.
            prof = profile or unique_profile(SAMPLE_PROFILE, used)
            if prof in used and used[prof] != twinned.get(grok_id):
                return {"pairs": [], "skipped": [{"grok_id": grok_id, "reason": "profile_already_paired",
                                                  "pair_id": used[prof]}]}
            items, withheld = [], []
            for item in self._items(g, {}):
                text = self._get(g, item) or ""
                hold, rep = _secrets.withhold(text, item, self._kind(item))
                withheld += rep
                # like a real bot: instructions are not in the Grok app's cache (by hand); skills come through
                # the sync bot (connected in the demo)
                action = ("held_back" if hold else "by_hand" if item == "persona.instructions"
                          else "via_mirror" if item.startswith("skill:") else "create")
                entry = {"item": item, "kind": self._kind(item), "name": item.split(":", 1)[-1].replace("persona.", ""),
                         "action": action, "owner": "grok",
                         # secret detection runs first, the preview is masked before anyone sees it
                         "preview": _secrets.redact(text, HIDDEN)[:400]}
                if item.startswith("skill:"):
                    entry["scan"] = self._scan_preview(grok_id, entry["name"], text)
                items.append(entry)
            for r in g.get("routines", []):
                items.append({"item": f"routine:{r}", "kind": "routine", "name": r, "action": "by_hand", "owner": "grok"})
            for c in g.get("connectors", []):
                items.append({"item": f"connector:{c}", "kind": "connector", "name": c, "action": "by_hand",
                              "owner": "grok"})
            pair = {"pair_id": twinned.get(grok_id) or _slug(g["name"]), "grok": {"id": grok_id, "name": g["name"]},
                    "hermes": {"profile": prof, "created": False, "adopted": False}, "items": items,
                    "secrets_withheld": withheld, "schedules_paused": len(g.get("routines", [])),
                    "restore_point": None, "demo": True}
            if dry_run:
                return {"pairs": [pair], "skipped": []}
            pid = self._create_pair(grok_id, prof)
            t = self._twin(pid)
            self._activity(t, "hermes", "sync", "Twin created from Grok (sample)")
            self._write_twin(pid, t)
            self._save_sig(pid)
            pair["pair_id"] = pid
            pair["hermes"]["created"] = True
            return {"pairs": [pair], "skipped": []}

    def _scan_preview(self, gid: str, name: str, text: str) -> Optional[str]:
        """Write the skill as it would be imported and run Hermes' own skill scanner on it."""
        from .importer import scan_skill_dir
        try:
            d = store.ensure_dir(self._p("preview-skills", gid, _slug(name)))
            store.atomic_write_text(d / "SKILL.md", f"---\nname: {_slug(name)}\ndescription: Imported from a "
                                                    f"demo Grok bot\n---\n\n{_secrets.redact(text, HIDDEN)}\n")
            return scan_skill_dir(d)
        except (OSError, ValueError):
            return None

    # ---- contract: check / sync / status ---------------------------------------------------------
    def check(self) -> dict:
        if not self.exists():
            return {"changed": [], "checked_at": now_iso()}
        w = self._world()
        sigs = store.read_json(self._p("check.json"), {}) or {}
        changed = [pid for pid, meta in w["pairs"].items() if sigs.get(pid) != self._sig(pid, meta["grok_id"])]
        return {"changed": changed, "checked_at": now_iso()}

    def sync(self, pair: Optional[str], direction: str = "g2h", dry_run: bool = False) -> dict:
        with store.locked():
            pids = [pair] if pair else self.pair_ids()
            return {"pairs": [self._sync_one(pid, direction, dry_run) for pid in pids]}

    def _sync_one(self, pid: str, direction: str, dry_run: bool) -> dict:
        started = time.monotonic()
        t = self._twin(pid)
        g = self._grok(t["grok_id"])
        res: dict[str, Any] = {"pair_id": pid, "applied": [], "waiting": [], "secrets_withheld": [],
                               "conflicts": [], "to_grok": 0, "restore_point": None}
        if g is None:
            res.update(state="grok_missing", wall_ms=0)
            return res
        # something held back because of a password stops being held as soon as Grok holds other text (for
        # instance the text from before): what Grok holds now is judged afresh below
        for item in [i for i, h in t["held"].items() if _digest(self._get(g, i)) != h.get("g")]:
            if not dry_run:
                t["held"].pop(item, None)
        plan = self._plan(g, t)
        at = now_iso()
        for p in plan:
            item, op, G, H = p["item"], p["op"], p["G"], p["H"]
            kind = self._kind(item)
            name = item.split(":", 1)[1] if ":" in item else kind
            if op == "conflict":
                if item not in t["conflicts"]:
                    t["conflicts"][item] = {"since": at}
                res["conflicts"].append(item)
            elif op == "same":
                t["base"][item] = {"g": _digest(G), "h": _digest(H)}
                t["conflicts"].pop(item, None)
            elif op == "local":
                base = t["base"].setdefault(item, {"g": _digest(G), "h": None})
                base["h"] = _digest(H)
            elif op == "held":
                res["secrets_withheld"] += t["held"][item]["reports"]
            elif op == "g2h":
                if direction not in ("g2h", "both"):
                    res["waiting"].append({"item": item, "reason": "direction"})
                    continue
                hold, rep = _secrets.withhold(G or "", item, kind, "g2h")
                if hold:
                    if not dry_run:
                        t["held"][item] = {"g": _digest(G), "reports": rep}
                        t["secrets_withheld_total"] = int(t.get("secrets_withheld_total") or 0) + len(rep)
                    res["secrets_withheld"] += rep
                    continue
                t["held"].pop(item, None)
                if not dry_run and H is not None and H != G:
                    res["restore_point"] = self._keep(pid, item, H, "Before an update from Grok") \
                        or res["restore_point"]
                self._set(t, item, G, at)
                t["base"][item] = {"g": _digest(G), "h": _digest(G)}
                res["applied"].append({"item": item, "kind": kind, "name": name, "direction": "g2h"})
            elif op == "h2g":
                hold, rep = _secrets.withhold(H or "", item, kind, "h2g")
                if hold:
                    res["secrets_withheld"] += rep
                    continue
                if (t.get("approved") or {}).get(item) != _digest(H):
                    # every Hermes skill bound for Grok needs the owner's approval first
                    res["waiting"].append({"item": item, "reason": "needs_approval", "to": "grok",
                                           "detail": f"You changed the skill {name} in Hermes."})
                    continue
                if direction not in ("h2g", "both"):
                    continue
                self._set(g, item, H, at)
                t["approved"].pop(item, None)
                t["base"][item] = {"g": _digest(H), "h": _digest(H)}
                res["applied"].append({"item": item, "kind": kind, "name": name, "direction": "h2g"})
        if not dry_run:
            if res["applied"]:
                t["last_change_at"] = at
                names = ", ".join(sorted({a["kind"] for a in res["applied"]}))
                side = "grok" if any(a["direction"] == "g2h" for a in res["applied"]) else "hermes"
                self._activity(t, side, "sync", f"Updated: {names}")
            if any(a["direction"] == "h2g" for a in res["applied"]):
                self._write_grok(t["grok_id"], g)
            t["last_sync_at"] = at
            self._write_twin(pid, t)
            self._save_sig(pid)
        st = self._status_one(pid)
        res["state"] = st["state"]
        if direction == "g2h":
            res["to_grok"] = st["pending"]["to_grok"]
        res["wall_ms"] = int((time.monotonic() - started) * 1000)
        return res

    def status(self, pair: Optional[str] = None) -> dict:
        pids = [pair] if pair else self.pair_ids()
        return {"pairs": [self._status_one(pid) for pid in pids]}

    def _status_one(self, pid: str) -> dict:
        t = self._twin(pid)
        g = self._grok(t["grok_id"])
        plan = self._plan(g, t) if g else []
        approved = t.get("approved") or {}
        to_h = sum(1 for p in plan if p["op"] == "g2h")
        h2g = [p for p in plan if p["op"] == "h2g"]
        ready = [p for p in h2g if approved.get(p["item"]) == _digest(p["H"])]
        waiting = [{"item": p["item"], "reason": "needs_approval", "to": "grok",
                    "detail": f"You changed the skill {p['item'].split(':', 1)[1]} in Hermes."}
                   for p in h2g if p not in ready and not _secrets.has_secret(p["H"] or "")]
        conflicts = len(t["conflicts"]) + sum(1 for p in plan if p["op"] == "conflict" and p["item"] not in t["conflicts"])
        held = list(t["held"].values())
        name = (g or {}).get("name") or t.get("name") or pid
        connected = bool(t.get("mirror_connected"))
        left = self.setup_left(t)
        by_hand = [{"item": "persona.instructions", "side": "hermes", "name": "instructions",
                    "what": "Bring the instructions over yourself (the Grok app does not keep them on this "
                            "computer)"}
                   ] if left and g else []
        by_hand += [{"item": f"routine:{r}", "side": "hermes", "name": r,
                    "what": f"Routine \"{r}\" arrives switched off in Hermes: turn it on there and off in Grok when "
                            "you move"}
                   for r in (g or {}).get("routines", [])]
        by_hand += [{"item": f"connector:{c}", "side": "hermes", "name": c,
                     "what": f"Connect \"{c}\" in Hermes yourself; its login stays in Grok"}
                    for c in (g or {}).get("connectors", [])]
        if not connected:
            by_hand += [{"item": p["item"], "side": "grok", "name": p["item"].split(":", 1)[1],
                         "what": f"Copy the Hermes version of the skill \"{p['item'].split(':', 1)[1]}\" into Grok "
                                 "yourself"}
                        for p in ready]
        withheld = [r for h in held for r in (h.get("reports") or [])]
        jobs = t.get("jobs", [])
        p = {"pair_id": pid, "demo": True,
             "grok": {"id": t["grok_id"], "name": name, "present": g is not None,
                      "description": _secrets.redact((g or {}).get("description") or "", HIDDEN)},
             # what the twin's Hermes bot has now (a description held back because of a password is not there)
             "hermes": {"profile": t.get("profile") or "default", "name": t.get("name") or "",
                        "description": t.get("description") or "", "model_from": None},
             "setup_left": left,
             "routines": {"in_grok": len((g or {}).get("routines", [])),
                          "paused_in_hermes": sum(1 for j in jobs if j.get("paused")),
                          "on_in_hermes": sum(1 for j in jobs if not j.get("paused"))},
             "last_sync_at": t.get("last_sync_at"), "last_change_at": t.get("last_change_at"),
             "pending": {"to_hermes": to_h, "to_grok": len(ready), "in_flight": 0, "waiting_approval": len(waiting),
                         "to_grok_items": [x["item"] for x in ready]},
             "waiting": waiting, "conflicts": conflicts, "secrets_held": len(held),
             "coverage": coverage(connected), "by_hand": by_hand, "restore_points": len(self._points(pid)),
             "secrets_withheld_total": int(t.get("secrets_withheld_total") or 0), "secrets_withheld": withheld,
             "mirror_bot": {"connected": connected, "every_minutes": None}, "error": None,
             "parity": None, "switched": self._switched_view(t)}
        p["state"], p["state_reason"] = derive_state(p)
        return p

    # ---- conflicts / resolve ---------------------------------------------------------------------
    def conflicts(self, pair: str) -> dict:
        t = self._twin(pair)
        g = self._grok(t["grok_id"]) or {}
        out = []
        # also what changed in both places since the last check (the count in status already includes it)
        found = dict(t["conflicts"])
        for p in self._plan(g, t) if g else []:
            if p["op"] == "conflict":
                found.setdefault(p["item"], {"since": now_iso()})
        for item, meta in sorted(found.items()):
            G, H = self._get(g, item), self._get(t, item)
            shown_g = _secrets.redact(G or "", HIDDEN)
            shown_h = _secrets.redact(H or "", HIDDEN)
            diff = "".join(difflib.unified_diff((shown_g + "\n").splitlines(True), (shown_h + "\n").splitlines(True),
                                                fromfile="grok", tofile="hermes"))
            out.append({"conflict_id": item, "kind": self._kind(item), "name": item.split(":", 1)[-1],
                        "since": meta.get("since"), "hermes": {"digest": _digest(H), "text": shown_h},
                        "grok": {"digest": _digest(G), "text": shown_g}, "diff": diff, "text_truncated": False})
        return {"conflicts": out}

    def resolve(self, pair: str, conflict_id: str, keep: str) -> dict:
        """Keep Grok: the Grok copy replaces the Hermes one, and the old Hermes copy is kept as a restore point.
        Keep Hermes: Hermes keeps its copy and the item goes on the list for Grok (by hand, or through the
        sync bot); the pretend Grok side is not touched, so there is no restore point (botport behaves alike)."""
        if keep not in ("hermes", "grok"):
            raise EngineError("usage", "keep must be hermes or grok")
        with store.locked():
            t = self._twin(pair)
            g = self._grok(t["grok_id"])
            if conflict_id not in t["conflicts"] and not any(
                    p["item"] == conflict_id and p["op"] == "conflict" for p in (self._plan(g, t) if g else [])):
                raise EngineError("usage", "That choice was already made.")
            if g is None:
                raise EngineError("pair_not_found", "The Grok side of this sample twin is missing.")
            G, H = self._get(g, conflict_id), self._get(t, conflict_id)
            winner = H if keep == "hermes" else G
            hold, _rep = _secrets.withhold(winner or "", conflict_id, self._kind(conflict_id))
            if hold:
                raise EngineError("secret_refused", "That version holds something that looks like a password or "
                                                    "key, so it is not copied (this check can miss things).",
                                  "Remove it from the text first.")
            at = now_iso()
            rp = None
            if keep == "hermes":
                if conflict_id.startswith("skill:"):
                    # Grok's copy counts as seen; Hermes' copy is a change for Grok, approved by this choice
                    t["base"][conflict_id] = {"g": _digest(G), "h": _digest(G)}
                    t.setdefault("approved", {})[conflict_id] = _digest(H)
                else:  # name/description/instructions: Hermes keeps its own copy (Grok is changed by hand)
                    t["base"][conflict_id] = {"g": _digest(G), "h": _digest(H)}
                summary = "Kept the Hermes version (Grok keeps its own until you send or copy it)"
            else:
                rp = self._keep(pair, conflict_id, H, "Before you kept the Grok version")
                self._set(t, conflict_id, G, at)
                t["base"][conflict_id] = {"g": _digest(G), "h": _digest(G)}
                t.get("approved", {}).pop(conflict_id, None)
                summary = "Kept the Grok version (the earlier Hermes version is under Earlier versions)"
            t["conflicts"].pop(conflict_id, None)
            t["last_change_at"] = at
            self._activity(t, "hermes", "skill_change", summary)
            self._write_twin(pair, t)
            self._save_sig(pair)
            return {"applied": [{"item": conflict_id, "keep": keep}], "restore_point": rp}

    # ---- restore ---------------------------------------------------------------------------------
    def restore_points(self, pair: str) -> dict:
        self._twin(pair)
        return {"points": self._points(pair)}

    def restore(self, pair: str, point_id: str) -> dict:
        """Put one kept Hermes copy back. The Grok side is not touched: a restored skill then shows as a Hermes
        change waiting for approval to go to Grok, and other state (conflicts, secrets held back) is
        recomputed, not cleared."""
        if not valid_item_id(point_id):
            raise EngineError("usage", "That earlier version is not there any more.")
        with store.locked():
            t = self._twin(pair)
            point = next((p for p in self._rp_list(pair) if p["id"] == point_id), None)
            if point is None:
                raise EngineError("usage", "That earlier version is not there any more.")
            item, text = point["item"], _expand(point["text"])
            if f"{item}@{_digest(text)}" != point_id:
                raise EngineError("usage", "That earlier version no longer matches its record, so it was not used.")
            cur = self._get(t, item)
            if cur == text:
                return {"restored": [], "restore_point": None,
                        "note": "Nothing to put back: Hermes already has that version."}
            undo = self._keep(pair, item, cur, "Before you put an earlier version back")
            self._set(t, item, text, now_iso())
            t.get("approved", {}).pop(item, None)
            self._settle(t, self._grok(t["grok_id"]))
            t["last_change_at"] = now_iso()
            name = item.split(":", 1)[1] if ":" in item else self._kind(item)
            self._activity(t, "hermes", "skill_change" if item.startswith("skill:") else "sync",
                           f"Put back the earlier Hermes version of {name}")
            self._write_twin(pair, t)
            self._save_sig(pair)
            return {"restored": [item], "restore_point": undo}

    # ---- pack / approve / mirror / remove / recent / parity / doctor --------------------------------
    def pack(self, pair: str, digest: Optional[dict] = None) -> dict:
        """"Send to Grok": approved Hermes skills go to the pretend Grok side at once (with a real bot the
        sync bot's routine installs them). The activity digest is included only when `digest` is given (the
        owner turned it on for this twin; counts + times, titles only with their separate opt-in)."""
        with store.locked():
            res = self._sync_one(pair, "h2g", False)
            t = self._twin(pair)
            t["pack_gen"] = int(t.get("pack_gen") or 0) + 1
            skills = sum(1 for a in res["applied"] if a["kind"] == "skill")
            entries = len((digest or {}).get("entries") or [])
            self._activity(t, "hermes", "sync", f"Sent to Grok: {plural(skills, 'skill')}, {entries} digest {'entry' if entries == 1 else 'entries'}")
            self._write_twin(pair, t)
            self._save_sig(pair)
            return {"pairs": [res], "pack": {"gen": t["pack_gen"], "items": skills + (1 if entries else 0),
                                             "written_to": "state:demo/pack"},
                    "skills": skills, "digest_entries": entries,
                    "note": "Sample: the invented Grok side took it at once. With a real bot: " + MIRROR_NOTE + "."}

    def approve(self, pair: str, item: str) -> dict:
        if not valid_item_id(item):
            raise EngineError("usage", "That item is not one of this twin's.")
        with store.locked():
            st = self._status_one(pair)
            if item not in {w["item"] for w in st["waiting"]}:
                raise EngineError("usage", "That item is not waiting to be sent.")
            t = self._twin(pair)
            t.setdefault("approved", {})[item] = _digest(self._get(t, item))
            self._activity(t, "hermes", "skill_change", f"Ready to send to Grok: {item.split(':', 1)[-1]}")
            self._write_twin(pair, t)
            entry = self._sync_one(pair, "g2h", False)
            return {"approved": [{"item": item, "to": "remote"}], "sync": entry}

    def mirror(self, pair: str, pair_folder: Optional[str] = None, off: bool = False) -> dict:
        """Demo: there is no real sync bot; the pretend Grok side stands in for it. The folder is not used."""
        with store.locked():
            t = self._twin(pair)
            # the demo twin starts connected: asking for the steps again changes nothing and logs nothing
            if bool(t.get("mirror_connected")) != (not off):
                t["mirror_connected"] = not off
                self._activity(t, "hermes", "sync", "Skill sync turned off" if off else "Skill sync turned on (sample)")
                self._write_twin(pair, t)
                self._save_sig(pair)
        if off:
            return {"pair_id": pair, "connected": False, "steps": [], "charter": ""}
        return {"pair_id": pair, "connected": True, "steps": [dict(s) for s in MIRROR_STEPS],
                "charter": DEMO_CHARTER}

    def remove(self, pair: Optional[str] = None, dry_run: bool = False) -> Any:
        """remove() with no pair drops the whole demo world (returns the pair ids it held);
        remove(pair) stops one demo twin (contract shape)."""
        if pair is None:
            with store.locked():
                pids = list(self._world()["pairs"]) if self.exists() else []
                if not dry_run:
                    store.remove_tree(self.central, self.dir)
                return pids
        with store.locked():
            w = self._world()
            if pair not in w["pairs"]:
                raise EngineError("pair_not_found", f"No sample twin called {pair}.")
            removed = [f"state:demo/hermes/{pair}.json", f"state:demo/restore/{pair}.json"]
            last = len(w["pairs"]) == 1
            if last:
                removed.append("state:demo")  # the last sample twin: the whole sample ends with it
            if not dry_run:
                if last:
                    store.remove_tree(self.central, self.dir)
                else:
                    w["pairs"].pop(pair, None)
                    store.atomic_write_json(self._p("world.json"), w)
                    self._p("hermes", f"{pair}.json").unlink(missing_ok=True)
                    self._rp_path(pair).unlink(missing_ok=True)
                    sigs = store.read_json(self._p("check.json"), {}) or {}
                    sigs.pop(pair, None)
                    store.atomic_write_json(self._p("check.json"), sigs)
            # a sample twin never had a Hermes profile of its own: nothing of the owner's is kept or removed
            return {"pair_id": pair, "dry_run": bool(dry_run), "removed": removed, "profile_kept": False,
                    "sample": True, "sample_ended": last}

    def recent(self, pair: str, limit: int = 10) -> dict:
        t = self._twin(pair)
        start = parse_iso((self._world() or {}).get("created_at"))
        shown = []
        for e in t.get("activity") or []:
            if not isinstance(e, dict):
                continue
            e = dict(e)
            off = e.pop("offset_s", None)
            if start is not None and isinstance(off, int) and not isinstance(off, bool):
                # fixture entry: stored in the year 2000, shown relative to when the demo started
                e["at"] = iso(start + timedelta(seconds=max(-86400, min(0, off))))
            shown.append(e)
        entries = sorted(shown, key=lambda e: e.get("at") or "", reverse=True)
        return {"entries": entries[: max(1, min(int(limit), 50))]}

    def parity(self, pair: str) -> dict:
        """Compare with Grok (no model call), like `botport twin parity`. The sample has no model of its own, so
        there is no model check here."""
        t = self._twin(pair)
        g = self._grok(t["grok_id"]) or {}
        checks = []
        for item in self._items(g, t):
            same = _digest(self._get(g, item)) == _digest(self._get(t, item))
            nm = item.split(":", 1)[1] if ":" in item else self._kind(item)
            checks.append({"id": item, "name": f"{self._kind(item)} {nm}" if ":" in item else nm, "pass": same,
                           "detail": "the same in Grok and Hermes" if same else "differs between Grok and Hermes"})
        if t.get("instructions_by_hand"):
            if self.setup_left(t):
                checks.append({"id": "persona.instructions", "name": "instructions", "pass": False,
                               "action": "set_instructions",
                               "detail": "you bring these over yourself: the Grok app does not keep the "
                                         "instructions on this computer"})
            else:
                checks.append({"id": "persona.instructions", "name": "instructions", "pass": True,
                               "detail": "you keep these yourself; Hermes cannot compare them with Grok"})
        jobs = {j["name"].split(": ", 1)[-1] for j in t.get("jobs", [])}
        for r in g.get("routines", []):
            ok = r in jobs
            checks.append({"id": f"routine:{r}", "name": f"routine {r}", "pass": ok,
                           "detail": "a Hermes routine with this name exists (switched off until you move)" if ok
                           else "not set up in Hermes yet"})
        return {"kind": "setup", "passed": sum(1 for c in checks if c["pass"]), "total": len(checks),
                "checks": checks}

    def doctor(self) -> dict:
        return {"checks": [{"id": "demo", "ok": self.exists(), "message": "Sample is on" if self.exists()
                            else "Sample is off", "fix": "" if self.exists() else "hermes twin demo"}]}

    # ---- finishing the sample twin: instructions, a skill's text, "I pasted it into Grok" -----------
    @staticmethod
    def setup_left(t: dict) -> list:
        """What is left before a sample twin is finished: the instructions, until the owner pasted them or said
        there are none. The sample never asks for a model."""
        if not t.get("instructions_by_hand"):
            return []  # started from a one-time export: it holds the instructions already
        if (t.get("instructions") or "").strip() or t.get("instructions_by") == "none":
            return []
        return ["instructions"]

    @staticmethod
    def instructions_example() -> str:
        """The sample bot's instructions as they stand in the pretend Grok app (invented text the page offers to
        paste in the walk-through)."""
        return _GROK_FIXTURES["b-acme-0001"]["instructions"]

    def set_instructions(self, pair: str, text: Optional[str]) -> Optional[str]:
        """Save pasted instructions into the sample twin (text=None: "this bot has no instructions"). The version
        before is kept under Earlier versions. They are the owner's own from here on: never compared with Grok,
        never sent there."""
        with store.locked():
            t = self._twin(pair)
            clean = pair not in self.check()["changed"]
            rp = None
            if text is None:
                t["instructions_by"] = "none"
            else:
                old = t.get("instructions") if isinstance(t.get("instructions"), str) else ""
                rp = self._keep(pair, "persona.instructions", old, "Before you changed the instructions"
                                if old.strip() else "Before you added the instructions", "paste")
                t["instructions"] = text
                t["instructions_by"] = "owner"
                t["instructions_by_hand"] = True
                t["last_change_at"] = now_iso()
                self._activity(t, "hermes", "sync", "Instructions saved")
            self._write_twin(pair, t)
            if clean:
                self._save_sig(pair)  # the owner's own text is not a change to sync
            return rp

    def skill_text(self, pair: str, name: str) -> Optional[tuple]:
        """(text, truncated) of the Hermes version of one skill of a sample twin, or None."""
        s = (self._twin(pair).get("skills") or {}).get(name)
        text = s.get("text") if isinstance(s, dict) else None
        return (text, False) if isinstance(text, str) else None

    def copied_to_grok(self, pair: str, item: str) -> None:
        """The owner pasted the Hermes version of a skill into Grok by hand: the pretend Grok side now holds it,
        so the skill stops asking until it changes again."""
        with store.locked():
            t = self._twin(pair)
            g = self._grok(t["grok_id"])
            H = self._get(t, item)
            if g is None or H is None or not item.startswith("skill:"):
                raise EngineError("usage", "That skill is not waiting to be copied to Grok.")
            at = now_iso()
            self._set(g, item, H, at)
            t["base"][item] = {"g": _digest(H), "h": _digest(H)}
            t.get("approved", {}).pop(item, None)
            t["conflicts"].pop(item, None)
            self._activity(t, "hermes", "skill_change", f"Copied to Grok by hand: {item.split(':', 1)[1]}")
            self._write_grok(t["grok_id"], g)
            self._write_twin(pair, t)
            self._save_sig(pair)

    # ---- demo-only: checklist + switch + simulate ------------------------------------------------
    def checklist(self, pair: str) -> dict:
        st = self._status_one(pair)
        t = self._twin(pair)
        g = self._grok(t["grok_id"]) or {}
        return {"by_hand": st["by_hand"],
                "switch": {"grok_routines_to_turn_off": [{"name": r} for r in g.get("routines", [])],
                           "hermes_jobs_paused": [{"job_id": j["job_id"], "name": j["name"], "schedule": j["schedule"]}
                                                  for j in t.get("jobs", []) if j.get("paused")],
                           "hermes_jobs_resumed": [{"job_id": j["job_id"], "name": j["name"],
                                                    "schedule": j["schedule"]}
                                                   for j in t.get("jobs", []) if not j.get("paused")]}}

    @staticmethod
    def _switched_view(t: dict) -> Optional[dict]:
        sw = t.get("switched")
        if not isinstance(sw, dict) or not parse_iso(sw.get("at")):
            return None
        on = {j["job_id"] for j in t.get("jobs", []) if not j.get("paused")}
        ids = [j for j in (sw.get("job_ids") or []) if j in on]
        return {"at": sw["at"], "routines_on": len(ids)} if ids else None

    def switch(self, pair: str, job_ids: list[str]) -> dict:
        with store.locked():
            t = self._twin(pair)
            jobs = {j["job_id"]: j for j in t.get("jobs", [])}
            resumed, failed = [], []
            for jid in job_ids:
                j = jobs.get(jid)
                if j is None:
                    failed.append({"job_id": jid, "error": "not a routine of this twin"})
                    continue
                j["paused"] = False
                resumed.append(jid)
            if resumed:
                old = t.get("switched") if isinstance(t.get("switched"), dict) else {}
                t["switched"] = {"at": old.get("at") or now_iso(),
                                 "job_ids": sorted(set(old.get("job_ids") or []) | set(resumed))}
                self._activity(t, "hermes", "routine_change",
                               f"Turned on {plural(len(resumed), 'routine')} in the sample")
                self._write_twin(pair, t)
            return {"resumed": resumed, "failed": failed}

    def unswitch(self, pair: str) -> dict:
        """Undo "Move routines to Hermes": switch off again exactly the routines that move turned on."""
        with store.locked():
            t = self._twin(pair)
            sw = t.get("switched") if isinstance(t.get("switched"), dict) else {}
            paused = []
            for j in t.get("jobs", []):
                if j["job_id"] in (sw.get("job_ids") or []) and not j.get("paused"):
                    j["paused"] = True
                    paused.append(j["job_id"])
            t.pop("switched", None)
            if paused:
                self._activity(t, "hermes", "routine_change",
                               f"Switched off {plural(len(paused), 'routine')} in the sample again")
            self._write_twin(pair, t)
            return {"paused": paused, "failed": []}

    def simulate(self, action: str) -> dict:
        with store.locked():
            w = self._world()
            pid = next((p for p, m in w["pairs"].items() if m["grok_id"] == "b-acme-0001"), None)
            if pid is None:
                raise EngineError("pair_not_found", "The Acme Support sample twin is gone.",
                                  "Start the sample again: press See a sample on the Twins page, or run "
                                  "hermes twin demo.")
            g = self._grok("b-acme-0001")
            t = self._twin(pid)
            at = now_iso()
            if action == "grok-edit":
                cur = g.get("description") or ""
                idx = next((i for i, d in enumerate(_DESCRIPTION_EDITS) if d == cur), -1)
                g["description"] = _DESCRIPTION_EDITS[(idx + 1) % len(_DESCRIPTION_EDITS)]
                self._write_grok("b-acme-0001", g)
                what = "Changed the sample bot's description in Grok"
            elif action == "conflict":
                name = CONFLICT_SKILL
                g.setdefault("skills", {})[name] = {"text": _CONFLICT_GROK, "changed_at": at}
                self._write_grok("b-acme-0001", g)
                t.setdefault("skills", {})[name] = {"text": _CONFLICT_HERMES, "changed_at": at}
                self._activity(t, "hermes", "skill_change", f"Skill edited in Hermes: {name}")
                self._write_twin(pid, t)
                what = f"Changed the skill {name} in both places"
            elif action == "secret":
                g["description"] = (g.get("description") or "").split(" Test key:")[0] + \
                    f" Test key: {_fake_key()}"
                self._write_grok("b-acme-0001", g)  # stored as a token; the value is never written
                what = "Pasted a fake password into the sample bot's description in Grok"
            elif action == "remove-secret":
                g["description"] = (g.get("description") or "").split(" Test key:")[0]
                self._write_grok("b-acme-0001", g)
                what = "Took the fake password out of the sample bot's description in Grok"
            elif action == "hermes-edit":
                name = HERMES_EDIT_SKILL
                cur = (t.get("skills") or {}).get(name, {}).get("text") or ""
                extra = "\nEnd with a friendly sign-off."
                t.setdefault("skills", {})[name] = {"text": cur + extra if extra not in cur else cur.replace(extra, ""),
                                                    "changed_at": at}
                self._activity(t, "hermes", "skill_change", f"Skill edited in Hermes: {name}")
                self._write_twin(pid, t)
                what = f"Changed the skill {name} in Hermes"
            else:
                raise EngineError("usage", "That is not one of the sample's actions.")
            return {"pair_id": pid, "did": what}
