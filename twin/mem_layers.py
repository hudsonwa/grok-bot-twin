"""Memory layers between a Grok bot and its Hermes twin: what they share, which way, and how it is kept private.

Three layers, each switched per twin and OFF until the owner switches it on:
- "facts" (`mem_facts.py`): the twin's MEMORY.md / USER.md notes and the Grok bot's memories, one fact per entry,
  each with an id, where it came from and when. Merged both ways; conflicts shown; deletions honoured both ways.
- "briefs" (`mem_briefs.py`): each conversation compacted into a short Markdown brief (who, what was decided, open
  threads), passed both ways so "carry on over there" works without the whole conversation.
- "transcripts" (`mem_archive.py`): whole conversations kept in a local SQLite archive with full-text search and a
  JSON export that Hermes can import as its own sessions. They are never sent to Grok unless the owner switches
  that direction on for that twin.

Directions: "off", "grok_to_hermes", "hermes_to_grok", "both". Everything here starts "off".

Privacy filter (`held`, `mask`): the 0.2 memory check (`xs_memory.why_held`): a note that looks like it holds a
password, key, code or card number, reads like an instruction to the model, looks like one of this plugin's own
notes, holds characters nobody can see, or is too long to check, never crosses: it stays on the side it was written
on and is counted, never quoted. Briefs and transcripts are filtered line by line.

Between the owner's computers: the sealed box of the 0.2 memory sync (`xs_memory.seal` / `unseal`: SHAKE-256 keyed
stream + HMAC-SHA256, encrypt then authenticate) with a key of its own per layer, made from the link's key.

The Grok side: a `GrokTransport`. Until the Mirror can write to Grok, the transport is
`StubTransport`: a pretend Grok bot kept in files in this plugin's folder (the plugin's own tests and the
owner's dry run use it), plus an outbox of what would be sent. `set_transport_factory` is the one place that
plugs the real Mirror in. Nothing in this module talks to Grok or to the network.

Standard library only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from . import fleet_io as fio
from . import secrets as _secrets
from . import store, xs_memory
from .engine import EngineError
from .model import now_iso

LAYERS = ("facts", "briefs", "transcripts")
DIRECTIONS = ("off", "grok_to_hermes", "hermes_to_grok", "both")
SETTINGS_FILE = "mem-layers.json"
STUB_DIR = "mem-grok-stub"
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")
OUTBOX_CAP = 500

SENTENCES = {
    "facts": "Facts: the twin's memory notes and the Grok bot's memories, one fact each, kept the same both ways. "
             "A note that looks like a password, key or instruction never crosses.",
    "briefs": "Briefs: each conversation summed up in a few lines (who, what was decided, what is still open), so "
              "you can carry on on the other side. Not the whole conversation.",
    "transcripts": "Transcripts: whole conversations kept on this computer in a searchable archive you can export "
                   "or open in Hermes. Sent to Grok only if you switch that direction on.",
}
WAY_WORDS = {"off": "off", "grok_to_hermes": "Grok → Hermes", "hermes_to_grok": "Hermes → Grok", "both": "both ways"}
HELD_WORDS = dict(xs_memory.HELD_WORDS)


class LayerError(EngineError):
    pass


def _bad(code: str, message: str, hint: str = "") -> LayerError:
    return LayerError(code, message, hint)


def check_profile(profile: Any) -> str:
    if not isinstance(profile, str) or not PROFILE_RE.match(profile) or profile == "default":
        raise _bad("bad_twin", "That is not a twin on this computer.")
    return profile


# ---- switches ----------------------------------------------------------------------------------------------------------
_guard = threading.RLock()


def _path() -> Path:
    return store.central_dir() / SETTINGS_FILE


def _load() -> dict:
    raw = fio.read_json(_path(), None)
    out: dict = {"v": 1, "twins": {}}
    if isinstance(raw, dict) and isinstance(raw.get("twins"), dict):
        for prof, rec in list(raw["twins"].items())[:500]:
            if isinstance(prof, str) and PROFILE_RE.match(prof) and isinstance(rec, dict):
                out["twins"][prof] = {
                    layer: (rec.get(layer) if rec.get(layer) in DIRECTIONS else "off") for layer in LAYERS}
                out["twins"][prof]["changed"] = rec.get("changed") if isinstance(rec.get("changed"), str) else None
    return out


def settings(profile: str) -> dict:
    """{"facts": direction, "briefs": ..., "transcripts": ..., "changed": iso or None}. Unknown twin: all off."""
    check_profile(profile)
    rec = _load()["twins"].get(profile)
    return dict(rec) if rec else {**{layer: "off" for layer in LAYERS}, "changed": None}


def set_direction(profile: str, layer: str, direction: str) -> dict:
    check_profile(profile)
    if layer not in LAYERS:
        raise _bad("bad_layer", "That is not a memory layer.", "Use facts, briefs or transcripts.")
    if direction not in DIRECTIONS:
        raise _bad("bad_direction", "That is not a direction.", "Use off, grok_to_hermes, hermes_to_grok or both.")
    with _guard:
        st = _load()
        rec = st["twins"].setdefault(profile, {**{x: "off" for x in LAYERS}, "changed": None})
        rec[layer] = direction
        rec["changed"] = now_iso()
        if all(rec[x] == "off" for x in LAYERS):
            st["twins"].pop(profile, None)
        fio.write_json(_path(), st)
    return settings(profile)


PANEL_KEYS = {"facts": "memory_facts", "briefs": "session_briefs", "transcripts": "transcripts"}


def _panel() -> Any:
    """the panel's `sync_panel` (the owner's switches) when this build has it, else None."""
    try:
        from . import sync_panel  # type: ignore[attr-defined]
    except Exception:
        return None
    return sync_panel if callable(getattr(sync_panel, "wanted", None)) else None


def direction(key: str, layer: str) -> str:
    """The direction in effect for a twin's layer. With the "What syncs" panel in this build, the owner's choice
    there (by pair id) and nothing else; without it, this module's own switches (by profile). Default: off."""
    if layer not in LAYERS:
        return "off"
    panel = _panel()
    if panel is not None:
        try:
            d = panel.wanted(key, PANEL_KEYS[layer])
        except Exception:
            d = None
        if d not in DIRECTIONS:
            return "off"
        if layer == "facts" and d != "off":
            # a choice saved while a direction was available counts only while it still is (the panel shows Off then)
            try:
                from . import mem_facts
                if mem_facts._panel_dirs(None).get(d) != "ok":
                    return "off"
            except Exception:
                return "off"
        return d
    try:
        return settings(key).get(layer, "off")
    except LayerError:
        return "off"


def mark_synced(key: str, layer: str, way: str) -> None:
    """Tell the panel something moved (its "last synced"); nothing without the panel."""
    panel = _panel()
    if panel is not None and way in ("grok_to_hermes", "hermes_to_grok"):
        try:
            panel.mark_synced(key, PANEL_KEYS[layer], way)
        except Exception:
            pass


def allowed(profile: str, layer: str, way: str) -> bool:
    """way: "to_hermes" (Grok -> Hermes) or "to_grok" (Hermes -> Grok). `profile` is the twin's key: its pair id
    when the panel is in this build, its Hermes profile otherwise."""
    d = direction(profile, layer)
    if way == "to_hermes":
        return d in ("grok_to_hermes", "both")
    if way == "to_grok":
        return d in ("hermes_to_grok", "both")
    return False


def any_on() -> bool:
    try:
        return any(any(r.get(x, "off") != "off" for x in LAYERS) for r in _load()["twins"].values())
    except Exception:
        return False


# ---- privacy filter ----------------------------------------------------------------------------------------------------
def held(text: Any) -> Optional[str]:
    """None when this text may cross; else the reason (a key of HELD_WORDS). Never says what the text is."""
    return xs_memory.why_held(text)


def held_words(counts: dict) -> list:
    return [f"{n} {'item' if n == 1 else 'items'}: {HELD_WORDS.get(k, k)}" for k, n in sorted(counts.items()) if n]


def mask_secrets(text: str) -> str:
    """For a whole transcript kept on this computer: suspected keys masked where they are; a line that looks like
    it states a password or code is replaced whole (its words are kept nowhere)."""
    return _secrets.redact(text, "[secret]", "[line held back: looks like it holds a password or key]")


# ---- between the owner's computers: the 0.2 sealed box, one key per layer ---------------------------------------------
def layer_key(link_key: bytes, layer: str) -> bytes:
    if layer not in LAYERS:
        raise ValueError("not a layer")
    return hmac.new(link_key, f"grok-bot-twin memory layer 1 {layer}".encode(), hashlib.sha256).digest()


def seal(link_key: bytes, layer: str, way: str, machine_id: str, bind: str, obj: dict) -> dict:
    return xs_memory.seal(layer_key(link_key, layer), way, machine_id, bind, obj)


def unseal(link_key: bytes, layer: str, way: str, machine_id: str, bind: str, sealed: Any) -> Optional[dict]:
    return xs_memory.unseal(layer_key(link_key, layer), way, machine_id, bind, sealed)


# ---- the Grok side -----------------------------------------------------------------------------------------------------
class GrokTransport(Protocol):
    """What the memory layers need from the Grok side of one twin. The Mirror implements it."""

    name: str

    def read_facts(self) -> Optional[list]:
        """[{"id", "text", "kind": "memory"|"user", "time"}] of the Grok bot's memories, or None when they cannot
        be read now (then nothing is changed on either side)."""

    def write_facts(self, adds: list, updates: list, deletes: list) -> bool:
        """Apply on the Grok bot: adds/updates are [{"id", "text", "kind", "time"}], deletes [id]. Truthy when done:
        True, or {"ok": True, "ids": {id sent: Grok's own id}} when Grok gives a new fact an id of its own."""

    def read_briefs(self) -> list:
        """[{"id", "title", "markdown", "time"}] briefs of the Grok bot's conversations."""

    def write_brief(self, brief: dict) -> bool:
        ...

    def write_transcript(self, conversation: dict) -> bool:
        ...

    def read_transcripts(self) -> list:
        """[{"id", "title", "started", "messages": [{"role", "content", "time"}]}] of the Grok bot's conversations."""


class StubTransport:
    """A pretend Grok bot in files (<plugin folder>/mem-grok-stub/<profile>/): `facts.json`, `briefs.json`,
    `transcripts.json` hold "what the Grok bot has"; every write is also put on `outbox.jsonl`, the list the
    Mirror will carry out once it can. Never a network call."""

    name = "stub"

    def __init__(self, profile: str) -> None:
        self.profile = check_profile(profile)
        self.dir = store.central_dir() / STUB_DIR / profile

    def _get(self, name: str) -> list:
        raw = fio.read_json(self.dir / name, None)
        return raw if isinstance(raw, list) else []

    def _put(self, name: str, value: list) -> None:
        store.ensure_dir(self.dir)
        fio.write_json(self.dir / name, value)

    def _out(self, op: str, payload: Any) -> None:
        store.ensure_dir(self.dir)
        store.append_jsonl(self.dir / "outbox.jsonl", {"at": now_iso(), "op": op, "payload": payload}, cap=OUTBOX_CAP)

    def outbox(self) -> list:
        return store.read_jsonl(self.dir / "outbox.jsonl")

    # facts
    def read_facts(self) -> Optional[list]:
        if (self.dir / "unavailable").exists():
            return None
        return [f for f in self._get("facts.json") if isinstance(f, dict)]

    def write_facts(self, adds: list, updates: list, deletes: list) -> bool:
        facts = {f.get("id"): f for f in self._get("facts.json") if isinstance(f, dict)}
        for f in list(adds) + list(updates):
            facts[f["id"]] = {k: f[k] for k in ("id", "text", "kind", "time") if k in f}
        for fid in deletes:
            facts.pop(fid, None)
        self._put("facts.json", list(facts.values()))
        self._out("facts", {"adds": len(adds), "updates": len(updates), "deletes": len(deletes)})
        return True

    # briefs
    def read_briefs(self) -> list:
        return [b for b in self._get("briefs.json") if isinstance(b, dict)]

    def write_brief(self, brief: dict) -> bool:
        items = [b for b in self._get("briefs.json") if isinstance(b, dict) and b.get("id") != brief.get("id")]
        items.append(brief)
        self._put("briefs.json", items[-200:])
        self._out("brief", {"id": brief.get("id")})
        return True

    # transcripts
    def read_transcripts(self) -> list:
        return [c for c in self._get("transcripts.json") if isinstance(c, dict)]

    def write_transcript(self, conversation: dict) -> bool:
        items = [c for c in self._get("transcripts.json")
                 if isinstance(c, dict) and c.get("id") != conversation.get("id")]
        items.append(conversation)
        self._put("transcripts.json", items[-200:])
        self._out("transcript", {"id": conversation.get("id")})
        return True


_factory: Callable[[str], Any] = StubTransport


def set_transport_factory(factory: Optional[Callable[[str], Any]]) -> None:
    """The Mirror calls this with its own transport; None goes back to the stub."""
    global _factory
    if factory is not None and not callable(factory):
        raise TypeError("transport factory must be callable")
    with _guard:
        _factory = factory or StubTransport


def mirror_ready() -> bool:
    """True once a real Grok transport (the Mirror) is plugged in: only then can anything go Hermes -> Grok."""
    return _factory is not StubTransport


def transport(profile: str) -> Any:
    return _factory(check_profile(profile))


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
