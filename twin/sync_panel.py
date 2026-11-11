"""What syncs between a Grok bot and its twin: one panel per twin, seven rows, a direction each.

The panel owns the owner's choice (which way each item goes); the parts of the plugin that move things own whether a
direction can work at all and when it last ran. Each part plugs in with `provide(...)` and acts only on `wanted(...)`.
Until a part has plugged in, the rows below describe this version exactly, and a direction that does not work yet
cannot be chosen: it says "Needs the Mirror" or "Not in this version yet".

Local only: nothing here goes to another computer or to Grok.

The choices are kept in <plugin-data>/sync-panel.json:
    {"v": 1, "twins": {pair_id: {item: {"direction": d, "at": iso}}}, "synced": {pair_id: {item: {"at", "direction"}}}}
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import safe, store
from .engine import EngineError
from .model import now_iso, parse_iso, valid_id

SCHEMA = "grok-bot-twin.syncpanel.v1"
FILE = "sync-panel.json"

ITEMS: tuple[tuple[str, str], ...] = (
    ("name", "Name"),
    ("description", "Description"),
    ("instructions", "Instructions"),
    ("skills", "Skills"),
    ("routines", "Routines"),
    ("memory_facts", "Memory facts"),
    ("session_briefs", "Session briefs"),
)
KEYS = tuple(k for k, _ in ITEMS)
LABELS = dict(ITEMS)
PRIVATE = frozenset({"memory_facts", "session_briefs"})
DIRECTIONS = ("grok_to_hermes", "hermes_to_grok", "both", "off")
ARROWS = {"grok_to_hermes": "Grok → Hermes", "hermes_to_grok": "Hermes → Grok", "both": "Both ways", "off": "Off"}
CHOICE_STATES = ("ok", "needs_mirror", "not_yet")
MIRROR_STATES = ("ready", "not_set_up", "no_permission")

WHY = {
    "needs_mirror": "Needs the Mirror, with your permission to change this bot in Grok.",
    "not_yet": "Not in this version yet.",
}
MIRROR_SENTENCE = {
    "ready": "The Mirror is set up: the small bot in your Grok account that carries changes for your twins.",
    "not_set_up": "The Mirror is not set up yet: it is the small bot in your Grok account that carries changes.",
    "no_permission": "The Mirror is set up, but you have not given it permission to change your other Grok bots.",
    "unknown": "No twins yet.",
}

# What each direction means, said once per item: what choosing it does (or would do).
_NOUN = {"name": "the name", "description": "the description", "instructions": "the instructions",
         "skills": "skills", "routines": "routines", "memory_facts": "memory facts",
         "session_briefs": "session briefs"}


def _sentence(key: str, direction: str) -> str:
    n = _NOUN[key]
    if key in PRIVATE:
        return {
            "grok_to_hermes": f"{n[0].upper()}{n[1:]} from Grok reach this twin; none go back to Grok.",
            "hermes_to_grok": f"{n[0].upper()}{n[1:]} from this twin reach Grok; none come back.",
            "both": f"{n[0].upper()}{n[1:]} go both ways, so either side can carry on.",
            "off": f"No {n} are copied either way.",
        }[direction]
    return {
        "grok_to_hermes": f"Changes to {n} made in Grok reach this twin; a change made in Hermes stays in Hermes.",
        "hermes_to_grok": f"Changes to {n} made in Hermes are made in Grok too, by the Mirror; Grok's changes stay "
                          "in Grok.",
        "both": f"Changes to {n} go both ways; when both sides changed, you choose which to keep.",
        "off": (f"{n[0].upper()}{n[1:]} stays as it is on each side." if key in ("name", "description") else
                f"{n[0].upper()}{n[1:]} stay as they are on each side."),
    }[direction]


@dataclass(frozen=True)
class Ctx:
    pair_id: str
    name: str
    mirror: str            # ready | not_set_up | no_permission
    skill_sync: bool       # this twin's skill sync is connected (0.2.1)
    created_at: Optional[str]
    imported_at: Optional[str]   # the last time its setup export was imported


@dataclass
class Provider:
    directions: Callable[[Ctx], dict]
    last_synced: Optional[Callable[[Ctx], Any]] = None
    on_change: Optional[Callable[[Ctx, str], None]] = None
    note: Optional[Callable[[Ctx], str]] = None
    default: Optional[Callable[[Ctx], str]] = None
    sentence: Optional[Callable[[Ctx, str], Optional[str]]] = None   # a part's own words for a direction
    live: bool = False  # `default` reads the part's own switches: the row always shows them, whatever was saved here


_lock = threading.RLock()
_providers: dict[str, Provider] = {}
_mirror_probe: Optional[Callable[[str], str]] = None


def provide(key: str, directions: Callable[[Ctx], dict], last_synced: Optional[Callable[[Ctx], Any]] = None,
            on_change: Optional[Callable[[Ctx, str], None]] = None, note: Optional[Callable[[Ctx], str]] = None,
            default: Optional[Callable[[Ctx], str]] = None,
            sentence: Optional[Callable[[Ctx, str], Optional[str]]] = None, live: bool = False) -> None:
    """A part of the plugin that moves `key` plugs in here (replacing the description of 0.2.1 below). `live`: the
    part has its own switches, also changed outside this panel (its own command): `default` reads them, and the row
    shows what they say, never an older choice saved here."""
    if key not in KEYS:
        raise ValueError(f"unknown item {key!r}")
    with _lock:
        _providers[key] = Provider(directions, last_synced, on_change, note, default, sentence, live)


def set_mirror_probe(fn: Optional[Callable[[str], str]]) -> None:
    """The Mirror's pre-approval state per twin ("ready" | "not_set_up" | "no_permission"), once the Mirror knows it."""
    global _mirror_probe
    _mirror_probe = fn


# ---- 0.2.1 as it is: the default rows -------------------------------------------------------------------------
def _fixed(ok: tuple, mirror: tuple = (), rest: str = "not_yet") -> Callable[[Ctx], dict]:
    def directions(ctx: Ctx) -> dict:
        return {d: "ok" if d in ok else ("needs_mirror" if d in mirror else rest) for d in DIRECTIONS}
    return directions


def _persona_last(ctx: Ctx) -> Any:
    return (ctx.created_at, "grok_to_hermes") if ctx.created_at else None


def _instructions_last(ctx: Ctx) -> Any:
    return (ctx.imported_at, "grok_to_hermes") if ctx.imported_at else None


def _skills_dirs(ctx: Ctx) -> dict:
    if ctx.skill_sync:
        return {"grok_to_hermes": "not_yet", "hermes_to_grok": "not_yet", "both": "ok", "off": "not_yet"}
    return {"grok_to_hermes": "needs_mirror", "hermes_to_grok": "needs_mirror", "both": "needs_mirror", "off": "ok"}


def _skills_note(ctx: Ctx) -> str:
    if ctx.skill_sync:
        return ("Skills from Grok arrive by themselves; yours go to Grok when you press Send to Grok. "
                "To stop, use Skill sync… in this twin's Details.")
    return ""


def _defaults() -> dict:
    persona = _fixed(("grok_to_hermes",), ("hermes_to_grok", "both"))
    return {
        "name": Provider(persona, _persona_last),
        "description": Provider(persona, _persona_last),
        "instructions": Provider(_fixed(("grok_to_hermes",), ("hermes_to_grok", "both")), _instructions_last,
                                 note=lambda c: "" if c.imported_at else
                                 "Not brought over yet: import the setup export the bot writes about itself.",
                                 sentence=lambda c, d: "Instructions come over from Grok each time you import the "
                                 "setup export the bot writes; a change made in Hermes stays in Hermes."
                                 if d == "grok_to_hermes" else None),
        "skills": Provider(_skills_dirs, note=_skills_note,
                           default=lambda c: "both" if c.skill_sync else "off"),
        "routines": Provider(_fixed(("off",))),
        "memory_facts": Provider(_fixed(("off",))),
        "session_briefs": Provider(_fixed(("off",))),
    }


def _provider(key: str) -> Provider:
    with _lock:
        return _providers.get(key) or _defaults()[key]


# ---- the file ---------------------------------------------------------------------------------------------------
def _path():
    return store.central_dir() / FILE


def _read() -> dict:
    raw = store.read_json(_path(), {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    twins = raw.get("twins") if isinstance(raw.get("twins"), dict) else {}
    synced = raw.get("synced") if isinstance(raw.get("synced"), dict) else {}
    return {"v": 1, "twins": twins, "synced": synced}


def _entry(table: dict, pid: str, key: str) -> dict:
    t = table.get(pid)
    e = t.get(key) if isinstance(t, dict) else None
    return e if isinstance(e, dict) else {}


def wanted(pair_id: str, key: str) -> Optional[str]:
    """The owner's chosen direction for that twin's item, or None when the owner never chose (the default holds)."""
    if key not in KEYS:
        return None
    d = _entry(_read()["twins"], pair_id, key).get("direction")
    return d if d in DIRECTIONS else None


def mark_synced(pair_id: str, key: str, direction: str, at: Optional[str] = None) -> None:
    """A part of the plugin moved something for this item just now (or at `at`)."""
    if key not in KEYS or direction not in ("grok_to_hermes", "hermes_to_grok") or not valid_id(pair_id):
        raise ValueError("mark_synced: bad item or direction")
    with _lock, store.locked():
        f = _read()
        f["synced"].setdefault(pair_id, {})[key] = {"at": at if parse_iso(at) else now_iso(), "direction": direction}
        store.atomic_write_json(_path(), f)


# ---- the twins on this computer ---------------------------------------------------------------------------------
def _contexts(svc: Any) -> list[Ctx]:
    try:
        reg = svc._registry()
    except Exception:
        reg = {}
    try:
        pairs = svc.status().get("pairs") or []
    except Exception:
        pairs = []
    out = []
    seen = set()
    for p in pairs if isinstance(pairs, list) else []:
        if not isinstance(p, dict) or p.get("demo") or not valid_id(p.get("pair_id")) or p["pair_id"] in seen:
            continue
        pid = p["pair_id"]
        if pid not in reg:
            continue  # only twins the plugin made (the sample is never one)
        seen.add(pid)
        r = reg.get(pid) or {}
        grok = p.get("grok") if isinstance(p.get("grok"), dict) else {}
        name = safe.clean_name(grok.get("name"), 60) or safe.clean_name(r.get("name"), 60) or pid
        skill = (p.get("mirror_bot") or {}).get("connected") is True if isinstance(p.get("mirror_bot"), dict) else False
        ins = r.get("instructions") if isinstance(r.get("instructions"), dict) else {}
        imported = ins.get("at") if ins.get("from") == "export" and parse_iso(ins.get("at")) else None
        mirror = "ready" if skill else "not_set_up"
        if _mirror_probe is not None:
            try:
                m = _mirror_probe(pid)
                mirror = m if m in MIRROR_STATES else mirror
            except Exception:
                pass
        out.append(Ctx(pid, name, mirror, skill, r.get("created_at") if parse_iso(r.get("created_at")) else None,
                       imported))
        if len(out) >= 100:
            break
    return out


def _directions(prov: Provider, ctx: Ctx) -> dict:
    try:
        got = prov.directions(ctx) or {}
    except Exception:
        got = {}
    out = {d: (got.get(d) if got.get(d) in CHOICE_STATES else "not_yet") for d in DIRECTIONS}
    # never "needs the Mirror" when the Mirror is there: then the part itself is what is missing
    if ctx.mirror == "ready":
        out = {d: ("not_yet" if s == "needs_mirror" else s) for d, s in out.items()}
    return out


def _default_direction(prov: Provider, ctx: Ctx, dirs: dict) -> str:
    if prov.default:
        try:
            d = prov.default(ctx)
            if d in DIRECTIONS:
                return d
        except Exception:
            pass
    for d in ("grok_to_hermes", "off", "both", "hermes_to_grok"):
        if dirs.get(d) == "ok":
            return d
    return "off"


def _said(prov: Provider, key: str, ctx: Ctx, direction: str) -> str:
    if prov.sentence:
        try:
            own = safe.clean_summary(prov.sentence(ctx, direction) or "", 300)
            if own:
                return own
        except Exception:
            pass
    return _sentence(key, direction)


def _row(key: str, ctx: Ctx, f: dict) -> dict:
    prov = _provider(key)
    dirs = _directions(prov, ctx)
    chosen = None if prov.live and prov.default else _entry(f["twins"], ctx.pair_id, key).get("direction")
    # a choice that can no longer work (the Mirror went, a part left) is not shown as in effect
    direction = chosen if chosen in DIRECTIONS and dirs.get(chosen) == "ok" else _default_direction(prov, ctx, dirs)
    at, last_dir = None, None
    s = _entry(f["synced"], ctx.pair_id, key)
    if parse_iso(s.get("at")) and s.get("direction") in ("grok_to_hermes", "hermes_to_grok"):
        at, last_dir = s["at"], s["direction"]
    if prov.last_synced:
        try:
            got = prov.last_synced(ctx)
            if isinstance(got, (tuple, list)) and len(got) == 2 and parse_iso(got[0]) and \
                    got[1] in ("grok_to_hermes", "hermes_to_grok", None) and (not at or parse_iso(got[0]) > parse_iso(at)):
                at, last_dir = got[0], got[1]
        except Exception:
            pass
    if dirs[direction] == "ok":
        state = "ok"
    else:
        state = "needs_mirror" if "needs_mirror" in dirs.values() else "not_yet"
    # a row where only the direction in effect can be had: say what would open it up
    others = [d for d in DIRECTIONS if d != direction]
    if state == "ok" and all(dirs[d] != "ok" for d in others):
        state = "needs_mirror" if any(dirs[d] == "needs_mirror" for d in others) else "not_yet"
    note = ""
    if prov.note:
        try:
            note = safe.clean_summary(prov.note(ctx) or "", 300)
        except Exception:
            note = ""
    choices = []
    for d in DIRECTIONS:
        c = {"direction": d, "state": dirs[d], "sentence": _said(prov, key, ctx, d)}
        if dirs[d] != "ok":
            c["why"] = WHY[dirs[d]]
        choices.append(c)
    row = {"key": key, "label": LABELS[key], "direction": direction, "state": state, "private": key in PRIVATE,
           "last_synced_at": at, "last_direction": last_dir, "sentence": _said(prov, key, ctx, direction),
           "choices": choices}
    if note:
        row["note"] = note
    return row


def _twin(ctx: Ctx, f: dict) -> dict:
    return {"id": ctx.pair_id, "name": ctx.name, "mirror": ctx.mirror, "items": [_row(k, ctx, f) for k in KEYS]}


def view(svc: Any) -> dict:
    """GET /sync/panel: every twin on this computer, seven rows each. Reads local files only."""
    ctxs = _contexts(svc)
    f = _read()
    if not ctxs:
        mirror = "unknown"
    elif any(c.mirror == "ready" for c in ctxs):
        mirror = "ready"
    elif any(c.mirror == "no_permission" for c in ctxs):
        mirror = "no_permission"
    else:
        mirror = "not_set_up"
    return {"schema": SCHEMA, "mirror": {"state": mirror, "sentence": MIRROR_SENTENCE[mirror]},
            "twins": [_twin(c, f) for c in ctxs]}


def choose(svc: Any, pair_id: Any, key: Any, direction: Any) -> dict:
    """POST /sync/panel: the owner chose `direction` for one item of one twin."""
    if not isinstance(key, str) or key not in KEYS or not isinstance(direction, str) or direction not in DIRECTIONS:
        raise EngineError("bad_input", "That request was not understood.")
    if not valid_id(pair_id):
        raise EngineError("bad_input", "That is not one of your twins.")
    ctx = next((c for c in _contexts(svc) if c.pair_id == pair_id), None)
    if ctx is None:
        raise EngineError("no_twin", "That twin is not on this computer.")
    with _lock:
        f0 = _read()
        before = _row(key, ctx, f0)
        saved = _entry(f0["twins"], pair_id, key).get("direction")
        # (a live part: the row already shows its real switches, so "nothing changed" is true whatever was saved)
        if before["direction"] == direction and (saved in (None, direction) or _provider(key).live):
            said = f"Nothing changed: {LABELS[key]} for {ctx.name} was already set to {ARROWS[direction]}."
        else:
            dirs = _directions(_provider(key), ctx)
            if dirs[direction] != "ok":
                raise EngineError("not_available", WHY[dirs[direction]])
            prov = _provider(key)
            if prov.on_change:
                prov.on_change(ctx, direction)  # may refuse with an EngineError: then nothing is saved
            with store.locked():
                f = _read()
                f["twins"].setdefault(pair_id, {})[key] = {"direction": direction, "at": now_iso()}
                store.atomic_write_json(_path(), f)
            said = f"{LABELS[key]} for {ctx.name}: {ARROWS[direction]}. {_said(prov, key, ctx, direction)}"
    return {**view(svc), "said": {"sentence": said}}


def forget_twin(pair_id: str) -> None:
    """A twin went: its choices go with it."""
    with _lock, store.locked():
        f = _read()
        gone = [f["twins"].pop(pair_id, None), f["synced"].pop(pair_id, None)]
        if any(g is not None for g in gone):
            store.atomic_write_json(_path(), f)


# ---- the terminal -----------------------------------------------------------------------------------------------
_ITEM_WORDS = {**{k: k for k in KEYS}, **{v.lower(): k for k, v in ITEMS}, "memory": "memory_facts",
               "facts": "memory_facts", "briefs": "session_briefs",
               "memory-facts": "memory_facts", "session-briefs": "session_briefs"}
_DIR_WORDS = {**{d: d for d in DIRECTIONS}, "from-grok": "grok_to_hermes", "to-grok": "hermes_to_grok",
              "both-ways": "both"}


def item_word(word: str) -> Optional[str]:
    return _ITEM_WORDS.get(str(word or "").strip().lower())


def direction_word(word: str) -> Optional[str]:
    return _DIR_WORDS.get(str(word or "").strip().lower())


def lines(twin: dict, ago: Callable[[Optional[str]], str]) -> list[str]:
    """The panel of one twin, as the terminal prints it."""
    out = [f"What syncs for {twin['name']}"]
    width = max(len(LABELS[k]) for k in KEYS)
    for it in twin["items"]:
        now = ARROWS[it["direction"]]  # (what cannot be chosen is said on the lines under it)
        when = f"last synced {ago(it['last_synced_at'])}" if it["last_synced_at"] else "not synced yet"
        if it["last_synced_at"] and it["last_direction"]:
            when += f" ({ARROWS[it['last_direction']]})"
        out.append(f"  {it['label']:<{width}}  {now:<13}  {when}")
        out.append(f"  {'':<{width}}  {it['sentence']}")
        if it.get("note"):
            out.append(f"  {'':<{width}}  {it['note']}")
        closed = [c for c in it["choices"] if c["state"] != "ok"]
        for st in ("needs_mirror", "not_yet"):
            ds = [ARROWS[c["direction"]] for c in closed if c["state"] == st]
            if ds:
                out.append(f"  {'':<{width}}  {', '.join(ds)}: {WHY[st]}")
    if any(it["private"] for it in twin["items"]):
        out.append("Memory, session briefs and conversations start off for every twin. Only you switch them on; "
                   "nothing private moves without your choice.")
    out.append("Change one: hermes twin syncs [TWIN] ITEM from-grok|to-grok|both|off")
    return out


def _reset_for_tests() -> None:
    global _mirror_probe
    with _lock:
        _providers.clear()
    _mirror_probe = None
