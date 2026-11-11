"""Facts memory between a Grok bot and its Hermes twin (memory layer 1). Off for every twin until switched on.

The format. A Hermes bot keeps its notes in `memories/MEMORY.md` (what it learned) and `memories/USER.md` (what it
knows about its owner), separated by Hermes' own "§" lines. With facts switched on, every note that is shared ends
with a short trailer that says which fact it is, where it was first written and when:

    Prefers short answers in the morning. (fact g3a91c0e, from Grok, 2000-01-02)

The id is eight characters: "g" for a fact first written on the Grok side, "h" for one first written in Hermes.
The trailer is plain words, so the model reads where a fact came from too. A note without a trailer is a new
Hermes fact and gets one at the next sync (only while Hermes → Grok is on; otherwise it is left exactly as it is).

How it merges. This computer remembers, per fact, a digest of the text both sides last agreed on (never the text),
in <plugin folder>/mem-facts/<twin>.json. Each sync compares three things per fact id: what was agreed, what
Hermes has now, what Grok has now:
- the same on both sides: agreed;
- changed on one side only: the change goes to the other side;
- new on one side: it is added to the other;
- gone from one side and unchanged on the other: it is removed from the other (deletions are honoured both ways,
  and a removed fact is remembered for 60 days so a late copy does not bring it back);
- changed on both sides, or changed on one and removed on the other: a conflict. Both sides stay as they are until
  the owner keeps the Hermes or the Grok version.
With only one direction switched on, nothing goes the other way and the side that sends wins a conflict.

Privacy. A fact crosses only if the 0.2 memory check passes (`mem_layers.held`): nothing that looks like it holds a
password, key, code or card number, reads like an instruction to the model, looks like this plugin's own notes,
has invisible characters or is too long. The check runs on the side that sends and again on the side that
receives. A held fact stays where it is, counted and never quoted. A fact from Grok that would not fit in Hermes'
memory size is not added (counted as "no room").

Before a memory file is rewritten, the file as it was is kept as a restore point (the last 10 per file, owner-only)
and the write happens under Hermes' own lock for that file. Between the owner's computers the notes travel with the
0.2 memory sync, which already carries MEMORY.md and USER.md, encrypted.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets as _random
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import fleet_io as fio
from . import mem_layers as ml
from . import safe, store, xs_memory
from .model import now_iso, parse_iso

KINDS = {"memory": "MEMORY.md", "user": "USER.md"}
FID_RE = re.compile(r"^[gh][0-9a-f]{7}\Z")
TAG_RE = re.compile(r" \(fact ([gh][0-9a-f]{7}), from (Grok|Hermes), ([0-9]{4}-[0-9]{2}-[0-9]{2})\)\Z")
ANY_TAG_RE = re.compile(r"(?i)\(\s*fact\s+[a-z0-9]{4,12}\s*,\s*from\b")
LEDGER_DIR = "mem-facts"
POINTS_DIR = "mem-facts-points"
MAX_POINTS = 10
TOMB_DAYS = 60
MAX_TOMBS = 2000
MAX_FACTS = 400
MAX_CONFLICTS = 50
SHOWN = 300

_guard = threading.RLock()


# ---- the trailer -------------------------------------------------------------------------------------------------------
def tag(text: str, fid: str, source: str, when: str) -> str:
    return f"{text} (fact {fid}, from {'Grok' if source == 'grok' else 'Hermes'}, {when[:10]})"


def parse(entry: str) -> tuple:
    """(text without the trailer, fact id or None, "grok"/"hermes" or None, date or None)."""
    m = TAG_RE.search(entry)
    if not m:
        return entry, None, None, None
    return entry[:m.start()], m.group(1), "grok" if m.group(2) == "Grok" else "hermes", m.group(3)


def digest(text: str) -> str:
    return hashlib.sha256(b"grok-bot-twin fact\n" + text.encode("utf-8", "replace")).hexdigest()[:24]


def new_id(prefix: str) -> str:
    return prefix + _random.token_hex(4)[:7]


def grok_fid(gid: Any, known: Any = ()) -> str:
    """Our id for a Grok memory: its own id when it is one of ours, else one made from Grok's id. An "h" id (first
    written in Hermes) counts as ours only when this twin's ledger already knows it: otherwise Grok could make its
    own fact read "from Hermes" in the twin's memory."""
    if isinstance(gid, str) and FID_RE.match(gid) and (gid[0] == "g" or gid in known):
        return gid
    return "g" + hashlib.sha256(b"grok memory id\n" + str(gid).encode("utf-8", "replace")).hexdigest()[:7]


def why_held(text: Any) -> Optional[str]:
    """The privacy check for one fact, plus: a fact may not carry a trailer of its own (it would fake its id)."""
    why = ml.held(text)
    if why is None and ANY_TAG_RE.search(text):
        return "mark"
    return why


# ---- this computer's ledger --------------------------------------------------------------------------------------------
def _ledger_path(profile: str) -> Path:
    return store.central_dir() / LEDGER_DIR / f"{ml.check_profile(profile)}.json"


def _blank() -> dict:
    return {"v": 1, "base": {}, "tombs": {}, "conflicts": [], "held": {}, "no_room": 0, "last": None,
            "state": "off", "counts": {}}


def load(profile: str) -> dict:
    raw = fio.read_json(_ledger_path(profile), None)
    led = _blank()
    if not isinstance(raw, dict):
        return led
    for fid, b in list((raw.get("base") or {}).items())[:MAX_FACTS * 2]:
        if FID_RE.match(str(fid)) and isinstance(b, dict) and isinstance(b.get("d"), str) and b.get("kind") in KINDS:
            led["base"][fid] = {"d": b["d"][:24], "kind": b["kind"],
                                "gid": str(b["gid"])[:200] if b.get("gid") is not None else None}
    for fid, at in list((raw.get("tombs") or {}).items())[:MAX_TOMBS]:
        if FID_RE.match(str(fid)) and parse_iso(at):
            led["tombs"][fid] = at
    for c in (raw.get("conflicts") or [])[:MAX_CONFLICTS]:
        if isinstance(c, dict) and FID_RE.match(str(c.get("fid"))) and c.get("kind") in KINDS:
            led["conflicts"].append({k: c.get(k) for k in ("id", "fid", "kind", "hermes", "grok", "at", "why")})
    for k in ("held", "counts"):
        if isinstance(raw.get(k), dict):
            led[k] = {str(a)[:20]: int(b) for a, b in raw[k].items() if isinstance(b, int)}
    led["no_room"] = raw.get("no_room") if isinstance(raw.get("no_room"), int) else 0
    led["last"] = raw.get("last") if parse_iso(raw.get("last")) else None
    led["state"] = raw.get("state") if isinstance(raw.get("state"), str) else "off"
    if isinstance(raw.get("key"), str) and len(raw["key"]) <= 80:
        led["key"] = raw["key"]
    m = raw.get("moved")
    if isinstance(m, dict) and parse_iso(m.get("at")) and m.get("way") in ("grok_to_hermes", "hermes_to_grok"):
        led["moved"] = {"at": m["at"], "way": m["way"]}
    return led


def _save(profile: str, led: dict) -> None:
    store.ensure_dir(store.central_dir() / LEDGER_DIR)
    fio.write_json(_ledger_path(profile), led)


# ---- the Hermes side ---------------------------------------------------------------------------------------------------
def _home(profile: str) -> Path:
    home = store.profile_home(ml.check_profile(profile))
    if not fio.is_real_dir(home):
        raise ml.LayerError("no_bot", "That twin has no Hermes bot on this computer yet.", "")
    return home


def _mem_path(home: Path, kind: str) -> Path:
    return home / "memories" / KINDS[kind]


def _point(profile: str, kind: str, entries: list) -> None:
    d = store.central_dir() / POINTS_DIR / profile
    store.ensure_dir(d)
    name = f"{kind}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{time.time_ns() % 10**9:09d}{_random.token_hex(2)}.md"
    store.atomic_write_text(d / name, xs_memory.DELIM.join(entries))
    mine = sorted(p for p in os.listdir(d) if p.startswith(kind + "-") and p.endswith(".md"))
    for old in mine[:-MAX_POINTS]:
        target = d / old
        if store.contained(d, target) and not target.is_symlink():
            target.unlink()


def points(profile: str) -> list:
    d = store.central_dir() / POINTS_DIR / ml.check_profile(profile)
    if not fio.is_real_dir(d):
        return []
    names = [p for p in os.listdir(d) if p.endswith(".md") and not p.startswith(".")]
    return sorted(names, key=lambda p: (p.split("-", 1)[-1], p), reverse=True)  # newest first, either file


def restore(profile: str, point: str) -> dict:
    """Put a memory file back as it was before a sync rewrote it."""
    if not isinstance(point, str) or point not in points(profile):
        raise ml.LayerError("unknown_point", "That earlier version is no longer kept.", "")
    kind = point.split("-", 1)[0]
    if kind not in KINDS:
        raise ml.LayerError("unknown_point", "That earlier version is no longer kept.", "")
    text = fio.read_text(store.central_dir() / POINTS_DIR / profile / point, 4 * 1024 * 1024) or ""
    home = _home(profile)
    path = _mem_path(home, kind)
    with xs_memory._mem_lock(path) as got:
        if not got:
            raise ml.LayerError("memory_busy", "Hermes is writing that twin's memory right now.", "Try again.")
        now = xs_memory._read(path)
        if now is not None:
            _point(profile, kind, now)
        entries = [e for e in text.split(xs_memory.DELIM) if e.strip()]
        xs_memory._write(path, entries)
    with _guard, _ledger_lock(profile) as got:
        if not got:
            raise ml.LayerError("memory_busy", "That twin's memory facts are being synced right now.", "Try again.")
        # the restored facts are wanted again: a removal remembered for them no longer holds
        led = load(profile)
        for e in entries:
            fid = parse(e)[1]
            if fid:
                led["tombs"].pop(fid, None)
        _save(profile, led)
    return {"ok": True, "restored": kind}


def forget(profile: str) -> None:
    """Stop twinning: this twin's ledger (conflict texts), restore points (whole copies of its memory files) and the
    file stand-in for its Grok side go. Its MEMORY.md / USER.md stay as they are."""
    profile = ml.check_profile(profile)
    root = store.central_dir()
    with _guard:
        store.remove_tree(root / POINTS_DIR, root / POINTS_DIR / profile)
        store.remove_tree(root / ml.STUB_DIR, root / ml.STUB_DIR / profile)
        for name in (f"{profile}.json", f"{profile}.lock"):
            f = root / LEDGER_DIR / name
            if f.is_file() and not f.is_symlink():
                f.unlink()


# ---- one sync ----------------------------------------------------------------------------------------------------------
def _grok_side(raw: list, by_gid: dict, known: Any = ()) -> dict:
    """{fid: {"text", "kind", "gid", "time", "held"}} of what the Grok bot holds (checked on receipt). A Grok id we
    already know maps to its fact; a fact with no id is known by its text."""
    out: dict = {}
    for f in raw[:MAX_FACTS * 2]:
        if not isinstance(f, dict):
            continue
        text, kind = f.get("text"), f.get("kind") if f.get("kind") in KINDS else "memory"
        if not isinstance(text, str):
            continue
        text = text.strip()
        gid = f.get("id")
        if not isinstance(gid, (str, int)) or isinstance(gid, bool) or str(gid) == "":
            gid = "x-" + hashlib.sha256(f"{kind}\n{text}".encode("utf-8", "replace")).hexdigest()[:16]
        gid = str(gid)[:200]
        fid = by_gid.get(gid) or grok_fid(gid, known)
        if fid in out:
            continue
        out[fid] = {"text": text, "kind": kind, "gid": gid, "time": f.get("time") if parse_iso(f.get("time")) else None,
                    "held": why_held(text)}
    return out


def _hermes_side(files: dict) -> tuple:
    """({fid: {"text", "kind", "source", "date", "held"}}, [(kind, text)] untagged notes). A second line with the
    same fact id is a copy: it is not a fact of its own (the rebuild leaves it out)."""
    out, loose = {}, []
    for kind, entries in files.items():
        for e in entries:
            text, fid, src, date = parse(e)
            if fid:
                if fid not in out:
                    out[fid] = {"text": text, "kind": kind, "source": src, "date": date, "held": why_held(text)}
            else:
                loose.append((kind, text))
    return out, loose


def _conflict(led: dict, fid: str, kind: str, h: Optional[str], g: Optional[str], why: str) -> None:
    for c in led["conflicts"]:
        if c["fid"] == fid:
            c.update({"hermes": h, "grok": g, "why": why})
            return
    if len(led["conflicts"]) < MAX_CONFLICTS:
        led["conflicts"].append({"id": "c" + _random.token_hex(5), "fid": fid, "kind": kind,
                                 "hermes": h, "grok": g, "at": now_iso(), "why": why})


def _ledger_lock(profile: str) -> Any:
    store.ensure_dir(store.central_dir() / LEDGER_DIR)
    return xs_memory._file_lock(store.central_dir() / LEDGER_DIR / f"{profile}.lock", 10.0)


def sync(profile: str, grok: Any = None, read_only_grok: bool = False, key: Optional[str] = None) -> dict:
    """One sync of one twin's facts. Returns counts only (never a fact's text). `read_only_grok`: the Grok side can
    only be read and may be partial (a setup export): nothing goes Hermes -> Grok and nothing is removed from Hermes
    this time. `key`: the twin's pair id, whose switch in the "What syncs" panel decides (default: the profile, for
    a build without the panel)."""
    ml.check_profile(profile)
    key = key or profile
    to_h, to_g = ml.allowed(key, "facts", "to_hermes"), ml.allowed(key, "facts", "to_grok")
    if read_only_grok:
        to_g = False
    if not (to_h or to_g):
        return {"state": "off"}
    with _guard, _ledger_lock(profile) as got:
        if not got:
            return {"state": "busy"}
        led = load(profile)
        res = _sync(profile, led, grok or ml.transport(profile), to_h, to_g, additive=read_only_grok)
        led["state"] = res["state"]
        led["key"] = key
        if res["state"] == "synced":
            led["last"] = now_iso()
            led["counts"] = {f"{side}_{k}": v for side in ("to_hermes", "to_grok") for k, v in res[side].items()}
            for side, way in (("to_hermes", "grok_to_hermes"), ("to_grok", "hermes_to_grok")):
                if any(res[side].values()):
                    led["moved"] = {"at": led["last"], "way": way}
                    ml.mark_synced(key, "facts", way)
        _save(profile, led)
        res["conflicts"] = len(led["conflicts"])
        return res


def _size(entries: list) -> int:
    return len(xs_memory.DELIM.join(entries))


def _sync(profile: str, led: dict, grok: Any, to_h: bool, to_g: bool, additive: bool = False) -> dict:
    res = {"state": "synced", "to_hermes": {"added": 0, "updated": 0, "removed": 0},
           "to_grok": {"added": 0, "updated": 0, "removed": 0}, "held": {}, "no_room": 0}
    try:
        home = _home(profile)
    except ml.LayerError:
        res["state"] = "no_bot"
        return res
    graw = grok.read_facts()
    if graw is None:
        res["state"] = "grok_unavailable"
        return res
    held: dict = {}

    def count(why: str) -> None:
        held[why] = held.get(why, 0) + 1

    base = led["base"]
    G = _grok_side(graw, {b["gid"]: fid for fid, b in base.items() if b.get("gid")}, base)
    before = {"base": {k: dict(v) for k, v in base.items()}, "tombs": dict(led["tombs"]),
              "conflicts": [dict(c) for c in led["conflicts"]]}
    paths = {k: _mem_path(home, k) for k in KINDS}
    limits = xs_memory._limits(home)
    with xs_memory._mem_lock(paths["memory"]) as got1, xs_memory._mem_lock(paths["user"]) as got2:
        if not (got1 and got2):
            res["state"] = "busy"
            return res
        files = {k: xs_memory._read(p) for k, p in paths.items()}
        if any(v is None for v in files.values()):
            res["state"] = "unreadable"
            return res
        H, loose = _hermes_side(files)
        today = now_iso()
        trailer = len(tag("", "h0000000", "hermes", today))

        # untagged Hermes notes: the same text as a fact Hermes already has is a copy (left out); the same text as a
        # fact Grok has that was never agreed takes that fact's id; else, while Hermes -> Grok is on and the file
        # has room for the trailer, a new Hermes fact; else left exactly as it is
        retag: dict = {}
        h_by_text = {(h["kind"], h["text"]): fid for fid, h in H.items()}
        g_by_text = {(g["kind"], g["text"]): fid for fid, g in G.items() if fid not in base and fid not in H}
        room = {k: limits[k] - _size(files[k]) for k in KINDS}
        for kind, text in loose:
            if (kind, text) in h_by_text:
                retag[(kind, text)] = h_by_text[(kind, text)]
                continue
            why = why_held(text)
            fid = g_by_text.pop((kind, text), None)
            if fid is None and to_g:
                if why is not None:
                    count(why)  # stays as written, never sent, counted
                elif room[kind] >= trailer:
                    fid = new_id("h")
                else:
                    res["no_room"] += 1
            if fid is None:
                continue
            room[kind] -= trailer
            H[fid] = {"text": text, "kind": kind, "source": "grok" if fid in G else "hermes", "date": today[:10],
                      "held": why}
            h_by_text[(kind, text)] = fid
            retag[(kind, text)] = fid

        open_c = {c["fid"] for c in led["conflicts"]}
        g_adds, g_updates, g_deletes = [], [], []
        h_set: dict = {}   # fid -> (kind, text, source, date) to write in Hermes
        h_del: set = set()
        prev_base: dict = {}  # fid -> what was agreed before this sync (to undo a Hermes change that does not fit)
        for fid in sorted(set(base) | set(H) | set(G)):
            h, g = H.get(fid), G.get(fid)
            if fid in open_c:
                if h and g and h["text"] == g["text"]:  # settled by hand on both sides
                    led["conflicts"] = [c for c in led["conflicts"] if c["fid"] != fid]
                    base[fid] = {"d": digest(h["text"]), "kind": h["kind"], "gid": g["gid"]}
                elif not h and not g:  # removed on both sides: nothing left to choose
                    led["conflicts"] = [c for c in led["conflicts"] if c["fid"] != fid]
                    base.pop(fid, None)
                    led["tombs"][fid] = today
                continue
            b = base.get(fid)
            bd = b["d"] if b else None
            hd = digest(h["text"]) if h else None
            gd = digest(g["text"]) if g else None

            def take_g() -> None:  # Grok's version into Hermes
                if not to_h:
                    return
                if g["held"]:
                    count(g["held"])
                    return
                prev_base[fid] = b
                h_set[fid] = (g["kind"], g["text"], "grok" if fid.startswith("g") else "hermes",
                              (g["time"] or today)[:10])
                res["to_hermes"]["updated" if h else "added"] += 1
                base[fid] = {"d": gd, "kind": g["kind"], "gid": g["gid"]}

            def take_h() -> None:  # Hermes' version to Grok
                if not to_g:
                    return
                if h["held"]:
                    count(h["held"])
                    return
                item = {"id": (g or {}).get("gid") or (b or {}).get("gid") or fid, "text": h["text"],
                        "kind": h["kind"], "time": today}
                (g_updates if g else g_adds).append(item)
                res["to_grok"]["updated" if g else "added"] += 1
                base[fid] = {"d": hd, "kind": h["kind"], "gid": item["id"]}

            def drop_h() -> None:
                if to_h and not additive:
                    h_del.add(fid)
                    res["to_hermes"]["removed"] += 1
                    base.pop(fid, None)
                    led["tombs"][fid] = today

            def drop_g() -> None:
                if to_g:
                    g_deletes.append(g["gid"])
                    res["to_grok"]["removed"] += 1
                    base.pop(fid, None)
                    led["tombs"][fid] = today

            def differ(why: str) -> None:
                # a version that may not cross is never written down, not even to be shown: it stays where it is
                if (h and h["held"]) or (g and g["held"]):
                    count((h or {}).get("held") or g["held"])
                    return
                _conflict(led, fid, (h or g)["kind"], h["text"] if h else None, g["text"] if g else None, why)

            both = to_h and to_g
            if h and g:
                if hd == gd:
                    base[fid] = {"d": hd, "kind": h["kind"], "gid": g["gid"]}
                elif bd == hd:
                    take_g()
                elif bd == gd:
                    take_h()
                elif both:
                    differ("changed on both sides")
                else:
                    take_g() if to_h else take_h()
            elif h:
                if additive:
                    continue  # a setup export may leave facts out: absence there removes nothing
                if bd is None:
                    if fid in led["tombs"] and fid.startswith("g"):
                        drop_h()  # removed on Grok earlier; a late copy here does not bring it back
                    else:
                        take_h()
                elif bd == hd:
                    drop_h()
                elif both:
                    differ("changed in Hermes, removed on Grok")
                else:
                    drop_h() if to_h else take_h()
            elif g:
                if bd is None:
                    if fid in led["tombs"]:
                        drop_g()
                    else:
                        take_g()
                elif bd == gd:
                    drop_g()
                elif both:
                    differ("removed in Hermes, changed on Grok")
                else:
                    take_g() if to_h else drop_g()
            else:
                base.pop(fid, None)
                led["tombs"].setdefault(fid, today)

        # Hermes files: rebuild in place with trailers; a fact that moved file keeps its old line until it fits in
        # the new one; nothing makes a file larger than Hermes allows
        new_files: dict = {}
        moved_from: dict = {}
        updated_here: dict = {k: [] for k in KINDS}  # (index, fid, original entry) of lines changed in place
        seen: set = set()  # across both files: a fact is written once
        for kind in KINDS:
            out = []
            for e in files[kind]:
                text, fid, src, date = parse(e)
                if fid is None:
                    fid = retag.get((kind, text))
                    if fid is None:
                        out.append(e)
                        continue
                    src, date = ("grok" if fid in G else "hermes"), today[:10]
                if fid in seen or fid in h_del or fid in moved_from:
                    continue  # a copy, a removal, or a second copy in the other file
                seen.add(fid)
                if fid in h_set and h_set[fid][0] == kind:
                    _, t, s_, d = h_set.pop(fid)
                    updated_here[kind].append((len(out), fid, e))
                    out.append(tag(t, fid, s_, d))
                    continue
                if fid in h_set:  # moves to the other file
                    moved_from[fid] = (kind, e)
                    continue
                out.append(tag(text, fid, src or "hermes", date or today))
            new_files[kind] = out
        for fid, (kind, t, s_, d) in list(h_set.items()):
            entry = tag(t, fid, s_, d)
            if _size(new_files[kind] + [entry]) > limits[kind]:
                res["no_room"] += 1
                if fid in moved_from:
                    old_kind, old_entry = moved_from[fid]
                    new_files[old_kind].append(old_entry)
                    res["to_hermes"]["updated"] -= 1
                else:
                    res["to_hermes"]["added"] -= 1
                if prev_base.get(fid):
                    base[fid] = prev_base[fid]
                else:
                    base.pop(fid, None)
                continue
            new_files[kind].append(entry)
        for kind in KINDS:  # a change in place that makes the file too large is undone, newest first
            while _size(new_files[kind]) > max(limits[kind], _size(files[kind])) and updated_here[kind]:
                i, fid, orig = updated_here[kind].pop()
                new_files[kind][i] = orig
                res["no_room"] += 1
                res["to_hermes"]["updated"] -= 1
                if prev_base.get(fid):
                    base[fid] = prev_base[fid]

        # Grok first: if it cannot be written, Hermes is left as it was and nothing is believed agreed
        if g_adds or g_updates or g_deletes:
            ok = grok.write_facts(g_adds, g_updates, g_deletes)
            if not ok:
                res["state"] = "grok_unavailable"
                led.update(before)
                return res
            ids = ok.get("ids") if isinstance(ok, dict) else None
            if isinstance(ids, dict):  # Grok gave a fact its own id: remember it, so the next read finds the fact
                for fid, b in base.items():
                    new = ids.get(b.get("gid"))
                    if isinstance(new, (str, int)) and not isinstance(new, bool) and str(new):
                        b["gid"] = str(new)[:200]
        for kind in KINDS:
            if new_files[kind] != files[kind]:
                _point(profile, kind, files[kind])
                xs_memory._write(paths[kind], new_files[kind])
    res["held"] = dict(held)
    led["held"] = dict(held)
    _prune(led)
    led["no_room"] = res["no_room"]
    return res


def _prune(led: dict) -> None:
    cut = time.time() - TOMB_DAYS * 86400
    keep = {}
    for fid, at in led["tombs"].items():
        dt = parse_iso(at)
        if dt is not None and dt.timestamp() > cut:
            keep[fid] = at
    if len(keep) > MAX_TOMBS:
        keep = dict(sorted(keep.items(), key=lambda kv: kv[1])[-MAX_TOMBS:])
    led["tombs"] = keep


# ---- the owner's choices -----------------------------------------------------------------------------------------------
def resolve(profile: str, conflict_id: Any, keep: Any, grok: Any = None, key: Optional[str] = None) -> dict:
    """Settle a conflict: keep "hermes" or "grok"; then sync again so the kept version reaches the other side."""
    if keep not in ("hermes", "grok"):
        raise ml.LayerError("bad_keep", "Keep either the Hermes or the Grok version.", "")
    with _guard, _ledger_lock(ml.check_profile(profile)) as got:
        if not got:
            raise ml.LayerError("memory_busy", "That twin's memory facts are being synced right now.", "Try again.")
        led = load(profile)
        c = next((c for c in led["conflicts"] if c["id"] == conflict_id), None)
        if c is None:
            raise ml.LayerError("unknown_conflict", "That is already settled.", "")
        fid = c["fid"]
        led["conflicts"] = [x for x in led["conflicts"] if x["id"] != conflict_id]
        led["tombs"].pop(fid, None)
        gid = (led["base"].get(fid) or {}).get("gid")
        winner, loser = (c["hermes"], c["grok"]) if keep == "hermes" else (c["grok"], c["hermes"])
        if loser is None:
            led["base"].pop(fid, None)  # the winner looks new to the loser's side: it is added there
        else:
            # the loser's text is "what was agreed": the winner is then a change, and goes across
            led["base"][fid] = {"d": digest(loser), "kind": c["kind"], "gid": gid}
            if winner is None:
                pass  # the winner removed it: unchanged loser + removed winner = removed on the loser's side too
        _save(profile, led)
    return sync(profile, grok, key=key or led.get("key"))


def view(profile: str, key: Optional[str] = None) -> dict:
    led = load(profile)
    way = ml.direction(key or led.get("key") or profile, "facts")
    return {
        "twin": profile, "direction": way, "direction_words": ml.WAY_WORDS[way],
        "sentence": ml.SENTENCES["facts"], "state": led["state"], "last": led["last"],
        "facts": len(led["base"]), "counts": led["counts"],
        "held": ml.held_words(led["held"]), "no_room": led["no_room"],
        "conflicts": [{"id": c["id"], "kind": c["kind"], "why": c["why"],
                       "hermes": safe.cap(c["hermes"], SHOWN) if c["hermes"] is not None else None,
                       "grok": safe.cap(c["grok"], SHOWN) if c["grok"] is not None else None}
                      for c in led["conflicts"]],
        "points": points(profile)[:MAX_POINTS],
    }


# ---- the setup export (until the Mirror can read memories itself) ------------------------------------------------------
class ExportTransport:
    """A Grok bot's memories as its setup export lists them: read only. Writing to Grok needs the Mirror, so a
    write is refused (and Hermes is then left unchanged too)."""

    name = "export"

    def __init__(self, bot: Any) -> None:
        self.facts = facts_from_export(bot)

    def read_facts(self) -> list:
        return list(self.facts)

    def write_facts(self, adds: list, updates: list, deletes: list) -> bool:
        return False


def facts_from_export(bot: Any) -> list:
    """The `memories` and `user_facts` of one bot of a grok-bot-export document as facts (strings, or objects with
    "text"/"content" and an optional "id"). Nothing is kept here; the merge applies the privacy check."""
    out = []
    if not isinstance(bot, dict):
        return out
    for key, kind in (("memories", "memory"), ("user_facts", "user")):
        items = bot.get(key)
        if not isinstance(items, list):
            continue
        for i, it in enumerate(items[:MAX_FACTS]):
            if isinstance(it, str):
                text, gid = it, None
            elif isinstance(it, dict):
                text = it.get("text") if isinstance(it.get("text"), str) else it.get("content")
                gid = it.get("id")
            else:
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            text = text.strip()
            gid = str(gid)[:200] if isinstance(gid, (str, int)) else "x-" + hashlib.sha256(
                f"{kind}\n{text}".encode("utf-8", "replace")).hexdigest()[:16]
            out.append({"id": gid, "text": text, "kind": kind, "time": None})
    return out


# The sentence the owner's request to the Grok bot carries instead of "leave memories empty", while facts are on
# Grok -> Hermes for that twin (the setup export's own REQUEST asks for empty lists).
EXPORT_REQUEST = ('Put what you remember in "memories" (one short fact per item, as {"id": "<your id for it>", '
                  '"text": "<the fact>"}) and what you know about me in "user_facts" the same way. Leave out '
                  'passwords, codes, keys and card numbers.')


def sync_from_export(profile: str, bot: Any, key: Optional[str] = None) -> dict:
    """Grok -> Hermes from a setup export, only while facts are switched on Grok -> Hermes (or both) for this twin.
    Whoever reads a grok-bot-export for a twin (Import setup export, the instructions re-read) calls this; it
    does nothing while the switch is off."""
    if not ml.allowed(key or profile, "facts", "to_hermes"):
        return {"state": "off"}
    return sync(profile, ExportTransport(bot), read_only_grok=True, key=key)


# ---- the service, the panel and the background ------------------------------------------------------------------------
def _pairs(svc: Any) -> dict:
    """{pair id: Hermes profile} of the real twins on this computer."""
    out = {}
    try:
        for p in (svc.status().get("pairs") or [])[:200]:
            if not isinstance(p, dict) or p.get("demo"):
                continue
            prof = (p.get("hermes") or {}).get("profile") if isinstance(p.get("hermes"), dict) else None
            if isinstance(p.get("pair_id"), str) and isinstance(prof, str) and ml.PROFILE_RE.match(prof):
                out[p["pair_id"]] = prof
    except Exception:
        pass
    return out


def runs_here() -> bool:
    """Facts are merged with Grok only on a computer that is not a follower: a linked computer gets them through
    the 0.2 memory sync (MEMORY.md / USER.md), so two computers never give the same note two ids."""
    try:
        from . import fleet
        return not fleet.exists() or fleet.role(fleet.load()) != "follower"
    except Exception:
        return True


def sync_twin(svc: Any, pair_id: str, grok: Any = None) -> dict:
    prof = _pairs(svc).get(pair_id)
    if prof is None:
        raise ml.LayerError("no_twin", "That twin is not on this computer.", "")
    if not runs_here():
        return {"state": "follower"}
    return sync(prof, grok, key=pair_id)


INTERVAL_S = 900.0
_last_tick = [0.0]


def tick(get_service: Any) -> None:
    """With the plugin's background check: every 15 minutes, while the Mirror is plugged in and facts are on for a
    twin, one sync per such twin. Never raises; nothing at all without the Mirror."""
    try:
        if not ml.mirror_ready() or time.monotonic() - _last_tick[0] < INTERVAL_S or not runs_here():
            return
        _last_tick[0] = time.monotonic()
        svc = get_service()
        for pid, prof in _pairs(svc).items():
            if ml.direction(pid, "facts") != "off":
                sync(prof, key=pid)
    except Exception:
        pass


def _panel_dirs(ctx: Any) -> dict:
    # Grok -> Hermes too needs a real Grok side: nothing in this build reads a bot's memories from a setup export
    # yet, so offering it before the Mirror would be a switch that never moves a fact.
    later = "ok" if ml.mirror_ready() else "needs_mirror"
    return {"grok_to_hermes": later, "hermes_to_grok": later, "both": later, "off": "ok"}


def _panel_last(ctx: Any) -> Any:
    d = store.central_dir() / LEDGER_DIR
    if not fio.is_real_dir(d):
        return None
    for name in sorted(os.listdir(d))[:500]:
        if name.endswith(".json"):
            raw = fio.read_json(d / name, None)
            if isinstance(raw, dict) and raw.get("key") == ctx.pair_id and isinstance(raw.get("moved"), dict):
                m = raw["moved"]
                if parse_iso(m.get("at")) and m.get("way") in ("grok_to_hermes", "hermes_to_grok"):
                    return (m["at"], m["way"])
    return None


def _panel_note(ctx: Any) -> str:
    d = store.central_dir() / LEDGER_DIR
    if not fio.is_real_dir(d):
        return ""
    for name in sorted(os.listdir(d))[:500]:
        raw = fio.read_json(d / name, None) if name.endswith(".json") else None
        if isinstance(raw, dict) and raw.get("key") == ctx.pair_id:
            n = len(raw.get("conflicts") or [])
            if n:
                return f"{n} {'fact differs' if n == 1 else 'facts differ'} between Grok and Hermes: see them and keep one with hermes twin facts."
    return ""


def _panel_sentence(ctx: Any, direction: str) -> Optional[str]:
    return {
        "off": "No facts are copied between Grok and Hermes.",
        "grok_to_hermes": "Facts the Grok bot remembers are added to this twin's memory, each marked as from Grok. "
                          "A fact that looks like a password, key or instruction is left out.",
        "hermes_to_grok": "Facts this twin remembers are given to the Grok bot by the Mirror. A fact that looks like "
                          "a password, key or instruction stays in Hermes.",
        "both": "Facts are kept the same in Grok and Hermes; removing one removes it on both sides; if both changed "
                "it, you choose. Passwords, keys and instructions never cross.",
    }.get(direction)


def register_panel() -> bool:
    """Plug this layer into the "What syncs" panel, when this build has it."""
    panel = ml._panel()
    if panel is None or not callable(getattr(panel, "provide", None)):
        return False
    try:
        panel.provide("memory_facts", directions=_panel_dirs, last_synced=_panel_last, note=_panel_note,
                      default=lambda ctx: "off", sentence=_panel_sentence)
        return True
    except Exception:
        return False


register_panel()
