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
    "in_step": "In step",
    "changes_waiting": "Changes waiting",
    "needs_you": "Needs you",
    "grok_missing": "Grok bot missing",
    "error": "Error",
}
COVERAGE_VALUES = ("syncs", "syncs_via_mirror", "by_hand", "not_carried")
COVERAGE_LABELS = {
    "syncs": "Syncs",
    "syncs_via_mirror": "Via Mirror routine",
    "by_hand": "Set up by hand",
    "not_carried": "Not carried",
}
# Honest default coverage (botport docs/TWIN.md "What syncs, honestly"). Skills Hermes->Grok become
# "syncs_via_mirror" only while the twin is connected to the owner's Mirror bot.
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

MIRROR_NOTE = "Run the Mirror routine (Test) in Grok to apply"


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


def derive_state(pair: dict) -> tuple[str, str]:
    """State + short reason from a pair status dict (used when an engine omits them)."""
    if pair.get("grok", {}).get("present") is False:
        return "grok_missing", "Grok bot not found in the app"
    conflicts = int(pair.get("conflicts") or 0)
    pending = pair.get("pending") or {}
    held = int(pair.get("secrets_held") or 0)
    waiting = int(pending.get("waiting_approval") or 0)
    if conflicts:
        return "needs_you", plural(conflicts, "conflict")
    if held:
        return "needs_you", f"{plural(held, 'suspected secret')} held back"
    if waiting:
        return "needs_you", f"{plural(waiting, 'change')} waiting for approval"
    to_g = int(pending.get("to_grok") or 0)
    to_h = int(pending.get("to_hermes") or 0)
    if to_g or to_h:
        parts = []
        if to_h:
            parts.append(f"{plural(to_h, 'change')} for Hermes")
        if to_g:
            parts.append(f"{plural(to_g, 'change')} for Grok")
        return "changes_waiting", ", ".join(parts)
    return "in_step", "In step"


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
    return out


def clean_waiting(wt: Any) -> list[dict]:
    return [{"item": w["item"], "reason": safe.clean_name(w.get("reason") or "needs_approval", 40),
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
    return {"code": safe.clean_name(err.get("code"), 40) or "internal",
            "message": safe.clean_summary(err.get("message"), 300) or "Engine error"}


def normalize_pair(raw: Any, demo: bool = False) -> Optional[dict]:
    """Rebuild an engine pair dict from known fields only (strict allowlist: anything else the engine sends is
    dropped), every value type-checked, cleaned and capped, so it is safe for the UI, the tools and the cache."""
    if not isinstance(raw, dict) or not valid_id(raw.get("pair_id")):
        return None
    p: dict[str, Any] = {"pair_id": raw["pair_id"], "demo": raw.get("demo", demo) is True}
    grok = raw.get("grok") if isinstance(raw.get("grok"), dict) else {}
    gid = grok.get("id")
    p["grok"] = {"id": gid if isinstance(gid, str) and ITEM_ID_RE.match(gid) else "",
                 "name": safe.clean_name(grok.get("name"), 100), "present": grok.get("present", True) is not False}
    hermes = raw.get("hermes") if isinstance(raw.get("hermes"), dict) else {}
    p["hermes"] = {"profile": safe.clean_name(hermes.get("profile"), 64) or "default"}
    pending = raw.get("pending") if isinstance(raw.get("pending"), dict) else {}
    p["pending"] = {k: _int(pending.get(k)) for k in ("to_hermes", "to_grok", "in_flight", "waiting_approval")}
    p["conflicts"] = _int(raw.get("conflicts"))
    mb = raw.get("mirror_bot") if isinstance(raw.get("mirror_bot"), dict) else {}
    p["mirror_bot"] = {"connected": mb.get("connected") is True}
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
    p["error"] = clean_error(raw.get("error"))
    state = raw.get("state")
    if state not in STATES:
        p["state"], p["state_reason"] = derive_state(p)
    else:
        p["state"] = state
        p["state_reason"] = safe.clean_summary(raw.get("state_reason"), 120) or (
            derive_state(p)[1] if state != "error" else "Engine error")
    return p


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


REF_RE = re.compile(r"^t-[0-9a-f]{16}$")
