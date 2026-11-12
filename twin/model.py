"""Shared constants and small dict helpers (engine contract botport.twin.v1)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from . import safe

PLUGIN_ID = "grok-bot-twin"
SCHEMA = "botport.twin.v1"

STATES = ("in_step", "changes_waiting", "needs_you", "grok_missing", "error")
STATE_LABELS = {
    "in_step": "Up to date",
    "changes_waiting": "Updating…",
    "needs_you": "Needs your attention",
    "grok_missing": "Not in Grok any more",
    "error": "Can't check right now",
}
COVERAGE_VALUES = ("syncs", "syncs_via_mirror", "by_hand", "not_carried")
COVERAGE_LABELS = {
    "syncs": "Copies by itself",
    "syncs_via_mirror": "Copies when skill sync is on",
    "by_hand": "You do this yourself",
    "not_carried": "Does not copy",
}
# Honest default coverage (botport docs/TWIN.md "What syncs, honestly"). Skills Hermes->Grok become
# "syncs_via_mirror" only while the twin is connected to the owner's sync bot.
DEFAULT_COVERAGE = [
    {"item": "name", "g2h": "syncs", "h2g": "by_hand"},
    {"item": "description", "g2h": "syncs", "h2g": "by_hand"},
    {"item": "instructions", "g2h": "by_hand", "h2g": "by_hand"},
    {"item": "skills", "g2h": "syncs_via_mirror", "h2g": "by_hand"},
    {"item": "routines", "g2h": "by_hand", "h2g": "by_hand"},
    {"item": "connectors", "g2h": "by_hand", "h2g": "by_hand"},
]


def coverage(mirror_connected: bool = False) -> list[dict]:
    rows = [dict(c) for c in DEFAULT_COVERAGE]
    if mirror_connected:
        for c in rows:
            if c["item"] == "skills":
                c["h2g"] = "syncs_via_mirror"
    return rows

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Conflict / restore-point ids from the engine look like "skill:greet-visitors@3f2a9c1b7e40".
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9._:@ -]{1,128}$")
KEEP_SIDES = ("hermes", "grok")
DIRECTIONS = ("g2h", "h2g", "both")

MIRROR_NOTE = "It arrives when the sync bot's routine next runs in Grok"
# The reserved label a sample twin carries instead of a Hermes profile: the sample never names or touches a real one.
SAMPLE_PROFILE = "grok-bot-twin-sample"


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ID_RE.match(value)) and value not in (".", "..")


def valid_item_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ITEM_ID_RE.match(value)) and ".." not in value


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(now_dt())


def parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def ago(value: Any, now: Optional[datetime] = None) -> str:
    """Human 'N min ago' for an ISO timestamp; 'never' when missing."""
    dt = parse_iso(value)
    if dt is None:
        return "never"
    secs = int(((now or now_dt()) - dt).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 45:
        return "just now"
    if secs < 90:
        return "1 min ago"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 7200:
        return "1 hour ago"
    if secs < 86400 * 2:
        return f"{secs // 3600} hours ago"
    if secs < 86400 * 60:
        return f"{secs // 86400} days ago"
    return f"on {iso(dt)[:10]}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def item_kind(item: str) -> str:
    """'routine:Morning summary' -> 'routine'; 'persona.description' -> 'description'."""
    if not isinstance(item, str):
        return ""
    if ":" in item:
        return item.split(":", 1)[0]
    if item.startswith("persona."):
        return item.split(".", 1)[1]
    return item


def item_name(item: str) -> str:
    if isinstance(item, str) and ":" in item:
        return item.split(":", 1)[1]
    return item_kind(item)


FOLLOWS = "Follows Grok"


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


TONES = ("good", "busy", "todo", "send", "warn", "muted", "bad")


def view(p: dict) -> dict:
    """The one status sentence of a twin and the shape shown beside it (`tone`), from its counters. The Twins page
    shows these two fields as the server sends them, and the terminal and a chat say the same words:
    "Follows Grok · ready", "1 change to send to Grok · 2 things left", "Almost ready · 1 thing left",
    "Changed in both places". Tones: trouble "bad"/"warn", gone "muted", something to send "send", a to-do
    "todo", on its way "busy", calm "good". `needs`: what its open matters ask of the owner ("you", or only
    "send" to Grok; None: nothing). `ready`: no set-up chore is left (instructions, model)."""
    grok = p.get("grok") if isinstance(p.get("grok"), dict) else {}
    hermes = p.get("hermes") if isinstance(p.get("hermes"), dict) else {}
    sample = bool(p.get("sample") or p.get("demo"))
    # (the sample that shows both of its sides is there to be watched: its set-up chores are not counted)
    shown = sample and isinstance(grok.get("description"), str) and isinstance(hermes.get("description"), str)
    todo = p.get("setup_left") if isinstance(p.get("setup_left"), list) else []
    left = [] if shown else [k for k in SETUP_STEPS if k in todo]
    left_text = f"{plural(len(left), 'thing')} left" if left else ""

    def said(sentence: str, tone: str, needs: Optional[str] = None) -> dict:
        return {"sentence": sentence, "tone": tone, "needs": needs, "ready": not left}

    if p.get("state") == "error":
        return said(STATE_LABELS["error"], "bad", "you")
    if p.get("state") == "grok_missing" or grok.get("present") is False:
        return said(STATE_LABELS["grok_missing"], "muted", "you")
    pending = p.get("pending") if isinstance(p.get("pending"), dict) else {}
    matters: list = []  # (only asks "send this to Grok?", is a change waiting for a go-ahead, short sentence, how many)
    conflicts = _count(p.get("conflicts")) + len(open_conflicts(p))  # (one matter: one "changed in both places" box)
    if conflicts:
        matters.append((False, False, "Changed in both places" if conflicts == 1
                        else f"{conflicts} things changed in both places", 1))
    for w in (p.get("waiting") if isinstance(p.get("waiting"), list) else [])[:30]:
        if not isinstance(w, dict) or not w.get("item"):
            continue
        kind = item_kind(str(w["item"]).split("@")[0])
        kind = "connection" if kind == "connector" else kind
        if w.get("to") == "grok":
            matters.append((True, True, "1 change to send to Grok", 1))
        else:
            matters.append((False, True, f"A {kind} from Grok is ready" if w.get("to") == "hermes"
                            else "Something is waiting for your go-ahead", 1))
    to_grok = _count(pending.get("to_grok"))
    if to_grok:
        matters.append((True, False, f"{plural(to_grok, 'change')} to send to Grok", to_grok))
    held = p.get("secrets_withheld") if isinstance(p.get("secrets_withheld"), list) else []
    places = {str((w.get("item") or w.get("kind") or "description")) for w in held[:20] if isinstance(w, dict)}
    for _place in sorted(places) or ([""] if _count(p.get("secrets_held")) else []):
        matters.append((False, False, "A password or key was not copied", 1))
    if matters:
        sends = all(m[0] for m in matters)
        text = matters[0][2] if len(matters) == 1 else (
            f"{plural(sum(m[3] for m in matters), 'change')} to send to Grok" if sends
            else f"{len(matters)} things need you")
        return said(f"{text} · {left_text}" if left_text else text,
                    "send" if sends else "todo" if all(m[1] for m in matters) else "warn", "send" if sends else "you")
    if _count(pending.get("to_hermes")):
        return said(STATE_LABELS["changes_waiting"], "busy")
    if _count(pending.get("in_flight")):
        return said(f"{plural(_count(pending.get('in_flight')), 'change')} on the way to Grok", "busy")
    if left:
        return said(f"Almost ready · {left_text}", "todo")
    sw = p.get("switched") if isinstance(p.get("switched"), dict) else None
    n = _count(sw.get("routines_on")) if sw and sw.get("at") else 0
    sentence = (f"{FOLLOWS} · {plural(n, 'routine')} {'runs' if n == 1 else 'run'} in Hermes" if n
                else FOLLOWS if sample or (sw and sw.get("at")) else f"{FOLLOWS} · ready")
    return said(sentence, "good")


def status_sentence(p: dict) -> str:
    return view(p)["sentence"]


def open_conflicts(pair: dict) -> list:
    """The open choices of the instructions sync and of Hermes -> Grok (the service lists them; botport's own are
    `conflicts`)."""
    oc = pair.get("open_conflicts")
    return [c for c in oc if isinstance(c, dict) and isinstance(c.get("item"), str)] if isinstance(oc, list) else []


def derive_state(pair: dict) -> tuple[str, str]:
    """State + short reason from a pair's counters (the only source of a state other than "error": an engine's
    own state is not trusted over them). The reason names every open matter in plain words, joined with "; "
    (never one of several). Any open conflict means "needs_you". Every caller re-derives after it changes the
    counters, so the pair's `sentence` and `tone` (`view`) are refreshed here too, never computed anywhere else."""
    pair.update(view({**pair, "state": None}))
    if pair.get("grok", {}).get("present") is False:
        return "grok_missing", "This bot is not in the Grok app any more"
    conflicts = int(pair.get("conflicts") or 0)
    pending = pair.get("pending") or {}
    held = int(pair.get("secrets_held") or 0)
    waiting = int(pending.get("waiting_approval") or 0)
    needs = []
    if conflicts:
        needs.append(f"{plural(conflicts, 'item')} changed in both places")
    other = list(dict.fromkeys(item_kind(c["item"]) for c in open_conflicts(pair)))
    if other:
        needs.append(f"choose which {_word_list(other)} to keep")
    if held:
        needs.append(f"{plural(held, 'thing')} that {'looks' if held == 1 else 'look'} like a password "
                     f"or key {'was' if held == 1 else 'were'} not copied")
    if waiting:
        needs.append(f"{plural(waiting, 'change')} ready to send to Grok")
    to_g = int(pending.get("to_grok") or 0)
    to_h = int(pending.get("to_hermes") or 0)
    moving = []
    if to_h:
        moving.append(f"{plural(to_h, 'change')} on {'its' if to_h == 1 else 'their'} way to Hermes")
    if to_g:
        moving.append(f"{plural(to_g, 'change')} to send to Grok")
    if needs:
        reason = "; ".join(needs + moving)
        return "needs_you", reason[:1].upper() + reason[1:]
    if moving:
        return "changes_waiting", "; ".join(moving)
    return "in_step", "Up to date"


def _word_list(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


NOTE_ROUTINE = "arrives switched off, so nothing runs twice"
NOTE_ROUTINE_NO_COPY = "Grok keeps its schedule: make it again in Hermes yourself"
NOTE_CONNECTION = "logins are never copied"


def preview_note(kind: Any, action: Any, copies: bool = True) -> Optional[str]:
    """The reason behind a by-hand item, shown with it under "See every item" (not in the summary lines).
    `copies`: a switched-off copy of the bot's routines arrives with the twin (the sample; a bot that was imported
    before). A twin made from the Grok app's files alone gets none, and the note must not promise one."""
    if action != "by_hand":
        return None
    return {"routine": NOTE_ROUTINE if copies else NOTE_ROUTINE_NO_COPY, "connector": NOTE_CONNECTION}.get(kind)


def preview_lines(entry: Any) -> list[str]:
    """The plain lines that say what twinning one bot does (shown by the page and printed by `hermes twin add`),
    from a preview entry's items: what copies now, what comes later with the page's help, the optional skills,
    and a last line only when something was held back."""
    items = [i for i in (entry.get("items") if isinstance(entry, dict) and isinstance(entry.get("items"), list)
                         else []) if isinstance(i, dict)]
    def kinds(action: str) -> list[str]:
        return [str(i.get("kind") or "") for i in items if i.get("action") == action]
    order = ["name", "description", "instructions"]
    now = kinds("create")
    now_words = [k for k in order if k in now]
    n_sk = sum(1 for k in now if k == "skill")
    if n_sk:
        now_words.append(plural(n_sk, "skill"))
    other = sum(1 for k in now if k not in order and k != "skill")
    if other:
        now_words.append(plural(other, "other item"))
    lines = [f"Copies now: {_word_list(now_words)}." if now_words else "Copies now: nothing yet."]
    mine = kinds("by_hand")
    # Honest scope: the Grok app on this computer does not hold a bot's instructions, so unless this preview
    # says they copy (a twin started from a bot export, or through skill sync) they are always on the owner.
    if "instructions" not in now and "instructions" not in kinds("via_mirror") and "instructions" not in mine:
        mine = mine + ["instructions"]
    later = [k for k in order if k in mine]
    n_r = sum(1 for k in mine if k == "routine")
    n_c = sum(1 for k in mine if k == "connector")
    if n_c:
        later.append(plural(n_c, "connection"))
    other = sum(1 for k in mine if k not in order and k not in ("routine", "connector", "skill"))
    if other:
        later.append(plural(other, "other item"))
    lines.append(f"Later, with the page's help: {', '.join(later)}." if later else "Nothing is left for later.")
    if n_r:
        # routines are not part of "ready": the twin's row counts only what is left for it to be finished, so
        # they are not promised under "Later" (the sheet said "instructions, 1 routine" and the row "1 thing left")
        lines.append(f"Optional: {plural(n_r, 'routine')}, from the twin's \"…\" menu.")
    via = sum(1 for k in kinds("via_mirror") if k == "skill") + sum(1 for k in mine if k == "skill")
    if via:
        lines.append(f"{plural(via, 'skill')}: optional, set up later.")
    held = len({str(i.get("item")) for i in items if i.get("action") == "held_back"})
    sw = entry.get("secrets_withheld") if isinstance(entry, dict) else None
    held = held or (len({str(w.get("item")) for w in sw if isinstance(w, dict)}) if isinstance(sw, list) else 0)
    if held:
        lines.append(f"{plural(held, 'thing')} that {'looks' if held == 1 else 'look'} like a password or "
                     "key will not be copied.")
    return lines


COVERAGE_ITEMS = ("name", "description", "instructions", "skills", "routines", "connectors")


def _iso_or_none(v: Any) -> Optional[str]:
    return v[:40] if isinstance(v, str) and parse_iso(v) is not None else None


def clean_coverage(cov: Any) -> Optional[list[dict]]:
    """Coverage rows rebuilt from known values only (item, g2h, h2g); None when the engine gave no list."""
    if not isinstance(cov, list):
        return None
    out = []
    for c in cov[:20]:
        if isinstance(c, dict) and c.get("item") in COVERAGE_ITEMS:
            out.append({"item": c["item"],
                        "g2h": c.get("g2h") if c.get("g2h") in COVERAGE_VALUES else "by_hand",
                        "h2g": c.get("h2g") if c.get("h2g") in COVERAGE_VALUES else "by_hand"})
    for row in out:
        # Instructions are never said to copy from Grok, whatever the engine's list says (an older botport put
        # them under skill sync): the Grok app keeps none on this computer, and the sync bot reads none either
        # unless the owner hands it an export. The page, the terminal and the chat tool all read this list.
        if row["item"] == "instructions":
            row["g2h"] = "by_hand"
    return out


_WAITING_TO = {"grok": "grok", "remote": "grok", "h2g": "grok", "hermes": "hermes", "local": "hermes", "g2h": "hermes"}


def waiting_to(w: dict) -> Optional[str]:
    """Where a waiting change is headed ("grok" | "hermes"), only when the engine said so; never guessed."""
    for key in ("to", "direction"):
        v = w.get(key)
        if isinstance(v, str) and v in _WAITING_TO:
            return _WAITING_TO[v]
    return None


def clean_waiting(wt: Any) -> list[dict]:
    return [{"item": w["item"], "reason": safe.clean_name(w.get("reason") or "needs_approval", 40),
             "to": waiting_to(w),
             **({"detail": safe.clean_summary(w.get("detail"), 200)} if w.get("detail") else {})}
            for w in wt[:200] if isinstance(w, dict) and valid_item_id(w.get("item"))][:50] \
        if isinstance(wt, list) else []


def clean_withheld(sw: Any) -> list[dict]:
    return [{"item": safe.clean_name(s.get("item"), 140), "kind": safe.clean_name(s.get("kind"), 30),
             "location": safe.clean_summary(s.get("location"), 200),
             "direction": s.get("direction") if s.get("direction") in DIRECTIONS else "g2h"}
            for s in sw[:200] if isinstance(s, dict)][:50] if isinstance(sw, list) else []


def clean_error(err: Any) -> Optional[dict]:
    if not isinstance(err, dict):
        return None
    out = {"code": safe.clean_name(err.get("code"), 40) or "internal",
           "message": safe.clean_summary(err.get("message"), 300) or "Hermes could not check this twin"}
    hint = safe.clean_summary(err.get("hint"), 300)
    if hint:
        out["hint"] = hint
    return out


def normalize_pair(raw: Any, demo: bool = False) -> Optional[dict]:
    """Rebuild an engine pair dict from known fields only (strict allowlist: anything else the engine sends is
    dropped), every value type-checked, cleaned and capped, so it is safe for the UI, the tools and the cache."""
    if not isinstance(raw, dict) or not valid_id(raw.get("pair_id")):
        return None
    p: dict[str, Any] = {"pair_id": raw["pair_id"], "demo": raw.get("demo", demo) is True}
    p["sample"] = p["demo"]  # the user-facing word; `demo` stays for older callers
    grok = raw.get("grok") if isinstance(raw.get("grok"), dict) else {}
    gid = grok.get("id")
    # both sides of the twin, as last read: what the bot says in Grok, and what its Hermes bot has now
    p["grok"] = {"id": gid if isinstance(gid, str) and ITEM_ID_RE.match(gid) else "",
                 "name": safe.clean_name(grok.get("name"), 100), "present": grok.get("present", True) is not False,
                 "description": _side_text(grok.get("description"))}
    hermes = raw.get("hermes") if isinstance(raw.get("hermes"), dict) else {}
    p["hermes"] = {"profile": safe.clean_name(hermes.get("profile"), 64) or "default",
                   "name": safe.clean_name(hermes.get("name"), 100), "description": _side_text(hermes.get("description")),
                   "model_from": hermes.get("model_from") if hermes.get("model_from") in ("default", "own") else None}
    pending = raw.get("pending") if isinstance(raw.get("pending"), dict) else {}
    p["pending"] = {k: _int(pending.get(k)) for k in ("to_hermes", "to_grok", "in_flight", "waiting_approval")}
    tgi = pending.get("to_grok_items")
    p["pending"]["to_grok_items"] = [i for i in tgi[:200] if valid_item_id(i)][:50] if isinstance(tgi, list) else []
    p["conflicts"] = _int(raw.get("conflicts"))
    mb = raw.get("mirror_bot") if isinstance(raw.get("mirror_bot"), dict) else {}
    every = mb.get("every_minutes")
    p["mirror_bot"] = {"connected": mb.get("connected") is True,
                       "every_minutes": every if isinstance(every, int) and not isinstance(every, bool)
                       and 0 < every <= 100000 else None}
    left = raw.get("setup_left")
    p["setup_left"] = [k for k in SETUP_STEPS if k in left] if isinstance(left, list) else []
    rt = raw.get("routines") if isinstance(raw.get("routines"), dict) else {}
    p["routines"] = {k: _int(rt.get(k)) for k in ("in_grok", "paused_in_hermes", "on_in_hermes")}
    p["checked_at"] = _iso_or_none(raw.get("checked_at"))
    if raw.get("model_hint") and "model" in p["setup_left"]:
        p["model_hint"] = safe.cap(safe._strip(raw.get("model_hint")), 400)
    cov = clean_coverage(raw.get("coverage"))
    p["coverage"] = cov if cov is not None else coverage(p["mirror_bot"]["connected"])
    bh = raw.get("by_hand")
    p["by_hand"] = [{"item": safe.clean_name(b.get("item"), 140),
                     "side": b.get("side") if b.get("side") in ("grok", "hermes") else "hermes",
                     "name": safe.clean_name(b.get("name") or item_name(str(b.get("item") or "")), 100),
                     "what": safe.clean_summary(b.get("what"), 200)}
                    for b in bh[:200] if isinstance(b, dict)][:100] if isinstance(bh, list) else []
    p["restore_points"] = _int(raw.get("restore_points"))
    p["secrets_withheld_total"] = _int(raw.get("secrets_withheld_total"))
    p["last_sync_at"] = _iso_or_none(raw.get("last_sync_at"))
    p["last_change_at"] = _iso_or_none(raw.get("last_change_at"))
    p["waiting"] = clean_waiting(raw.get("waiting"))
    p["secrets_withheld"] = clean_withheld(raw.get("secrets_withheld"))
    p["secrets_held"] = _int(raw.get("secrets_held")) or len({s["item"] for s in p["secrets_withheld"]})
    par = raw.get("parity")
    p["parity"] = ({"passed": _int(par.get("passed")), "total": _int(par.get("total"))}
                   if isinstance(par, dict) and par.get("total") is not None else None)
    sw = raw.get("switched")
    p["switched"] = ({"at": sw["at"][:40], "routines_on": _int(sw.get("routines_on"))}
                     if isinstance(sw, dict) and isinstance(sw.get("at"), str) and parse_iso(sw["at"]) else None)
    p["skipped"] = []  # filled by the service (items the owner keeps in Hermes only)
    p["error"] = clean_error(raw.get("error"))
    # The state follows the counters (invariant: they never disagree). Only "error" (nothing could be read) and
    # "grok_missing" (said through `present`) come from the engine as they are.
    if raw.get("state") == "error":
        p["state"] = "error"
        p["state_reason"] = safe.clean_summary(raw.get("state_reason"), 120) or "Hermes could not check this twin"
        p.update(view(p))
        return p
    if raw.get("state") == "grok_missing":
        p["grok"]["present"] = False
    p["state"], p["state_reason"] = derive_state(p)
    return p


SETUP_STEPS = ("instructions", "model")
_SIDE_CTRL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]")


def _side_text(value: Any, limit: int = 2000) -> str:
    """A description as the page shows it: password-like text hidden, control characters dropped (newlines
    kept), capped."""
    if not isinstance(value, str):
        return ""
    from . import secrets as _secrets
    return _SIDE_CTRL.sub("", _secrets.redact(value[: limit * 4], "[not shown: looks like a password or key]"))[:limit].strip()


def _int(v: Any) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def pair_ref(pair_id: str, salt: Optional[bytes] = None) -> str:
    """Opaque, stable reference for a twin (what the model sees instead of any name): "t-" + the first 16 hex
    characters (64 bits) of HMAC-SHA256(per-install random salt, pair_id). The salt (store.ref_salt, 256 bits in
    the central data folder) means the id cannot be linked to a pair id by hashing guesses, nor across installs."""
    import hashlib
    import hmac
    if salt is None:
        from . import store
        salt = store.ref_salt()
    return "t-" + hmac.new(salt, str(pair_id).encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def ref_matches(ref: str, pair_id: str, salt: Optional[bytes] = None) -> bool:
    """True when `ref` is the opaque id of `pair_id` (constant-time compare)."""
    import hmac
    return isinstance(ref, str) and hmac.compare_digest(pair_ref(pair_id, salt), ref)


REF_RE = re.compile(r"^t-[0-9a-f]{16}\Z")
