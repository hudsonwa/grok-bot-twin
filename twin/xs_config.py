"""Settings that follow a twin to the owner's other computers: tools, preferences, persona and (only when the
owner says so) the model.

Sharing between computers carried a twin's name and description. This module adds the twin's SETTINGS, between the
owner's own linked computers and over the same signed link:

- The computer that shares reads, from each twin's own Hermes bot, exactly the values named in the allowlist below
  (never "the settings file minus what looks secret") and hands them to a linked computer that asks on the signed
  route `/v1/xs/config`.
- A linked computer cleans every value again by the same allowlist, saves a restore point (what the twin's bot had
  before), and writes the items its own switches allow into that twin's Hermes bot.
- Each computer has one switch per item. On the computer that shares it means "send", on a linked one "take".
  The model is off unless switched on: each computer keeps its own.

Never read, sent or written, whatever the switches say: `.env`, `auth.json`, keys, tokens, passwords, headers,
addresses, provider set-ups, how a connector is set up (only its NAME travels, so the other computer can say "set
up on this computer"), approval and security settings, hooks, plugins, memory, skills, routines, chats.

On the wire this is additive: the version of the share protocol stays the same. A linked computer says which
settings rev it last heard in its ordinary look (`"xs": {"config": rev}`), the sharing computer answers its own in
the "nothing changed" answer, and a computer on an older build ignores the key on either side.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import secrets as _random
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import fleet, fleet_io as fio, fleet_profiles as fp
from . import hermes_side as hs
from . import secrets as _secrets
from . import store
from . import yamlio
from .engine import EngineError
from .model import now_iso, parse_iso

log = logging.getLogger(__name__)

NAME = "config"  # this module's key inside "xs"
ROUTE = "/v1/xs/config"
XS = 1
ITEMS = ("tools", "preferences", "persona", "model")
DEFAULT_ON = {"tools": True, "preferences": True, "persona": True, "model": False}
STATE_FILE = "xs-config.json"
POINTS_DIR = "xs-config-points"
MAX_POINTS = 10
PERSONA_CAP = 64000
PERSONA_FILES = ("SOUL.md",)  # the prompt files that may travel, by name
PERSONA_BEGIN = "<!-- grok-bot-twin:begin persona -->"
PERSONA_END = "<!-- grok-bot-twin:end persona -->"
PERSONA_HEADER = "[This twin's instructions, as kept on the computer with the Grok app]"
RETRY_S = 15.0  # a twin whose settings could not be fetched or written is tried again this often
OWED_WAIT_S = 15  # while a try is owed, this computer's look at the other one waits no longer than this
WATCH_S = 3.0
WATCH_FOR_S = 180.0
REV_RE = re.compile(r"^[0-9a-f]{16}\Z")
POINT_RE = re.compile(r"^p-[0-9a-f]{12}\Z")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_WORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:+-]{0,39}\Z")
_ZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]{0,31}(?:/[A-Za-z0-9_+-]{1,32}){0,2}\Z")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,119}\Z")
_PLUGIN_BLOCKS_RE = re.compile(r"<!-- grok-bot-twin:begin (twin|fleet|persona) -->\n?.*?\n?<!-- grok-bot-twin:end \1 -->\n?",
                               re.S)
MAX_NAMES = 100
MAX_PLATFORMS = 20
ABSENT = {"absent": True}  # in a restore point: the key was not there before

LABELS = {
    "tools": ("Tools", "Which tool sets and connectors each twin uses. A connector's set-up and sign-in stay on "
                       "each computer."),
    "preferences": ("Preferences", "Personality, reply style, turn limit, reasoning effort, time zone and the two memory switches."),
    "persona": ("Persona", "Each twin's instructions (its prompt file)."),
    "model": ("Model", "Which model and provider each twin uses."),
}
WORDS = {"tools": "tools", "preferences": "preferences", "persona": "persona", "model": "model"}
NEVER = ("Never copied: passwords, keys, tokens, sign-ins, the .env file, how a connector is set up, and approval "
         "or security settings.")
OWN_MODEL = "Each computer keeps its own model."
MESSAGES = {
    "bad_item": ("That is not one of the settings that can follow.", "Choose tools, preferences, persona or model."),
    "unknown_twin": ("That twin is not on this computer.", ""),
    "unknown_point": ("There is nothing earlier to put back for that twin.", ""),
    "not_linked": ("This computer is not linked to another one.", ""),
    "no_yaml": ("Hermes on this computer could not read that twin's settings file.", "Update Hermes, then try again."),
    "write_failed": ("Hermes could not save that twin's settings on this computer.",
                     "Check that the disk is not full, then try again."),
}


class SettingsError(EngineError):
    def __init__(self, code: str) -> None:
        msg, hint = MESSAGES.get(code, ("Something went wrong with the settings that follow.", "Try again."))
        super().__init__(code, msg, hint)


# ---- the allowlist -------------------------------------------------------------------------------------------------
def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _word(v: Any) -> bool:
    return isinstance(v, str) and bool(_WORD_RE.match(v)) and not _secrets.has_secret(v)


def _turns(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 10000


def _zone(v: Any) -> bool:
    return isinstance(v, str) and bool(_ZONE_RE.match(v)) and not _secrets.has_secret(v)


# preference -> the check its value must pass, here and again where it arrives. Nothing outside this table travels.
PREFS = {
    "display.personality": _word,
    "display.compact": _is_bool,
    "agent.max_turns": _turns,
    "agent.reasoning_effort": _word,
    "timezone": _zone,
    "memory.memory_enabled": _is_bool,
    "memory.user_profile_enabled": _is_bool,
}
# the settings-file keys of the "tools" item (names only)
TOOL_KEYS = ("toolsets", "platform_toolsets", "agent.disabled_toolsets")
MODEL_KEYS = ("model.default", "model.provider")
ALLOWED = {"tools": TOOL_KEYS + ("mcp_servers (names only)",), "preferences": tuple(PREFS),
           "persona": PERSONA_FILES, "model": MODEL_KEYS}


_ADDRESS_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}|:\d+\Z")


def _name(v: Any) -> bool:
    """A tool set's, platform's or connector's name: one plain word that is neither key-shaped nor an address."""
    return (isinstance(v, str) and bool(_NAME_RE.match(v)) and not _ADDRESS_RE.search(v)
            and not _secrets.has_secret(v))


def _names(value: Any) -> list:
    out: list = []
    for v in value if isinstance(value, list) else []:
        v = str(v) if isinstance(v, int) and not isinstance(v, bool) else v
        if _name(v) and v not in out:
            out.append(v)
        if len(out) >= MAX_NAMES:
            break
    return out


def _model_word(v: Any) -> Optional[str]:
    v = hs._plain(v)
    return v if v and _MODEL_RE.match(v) else None


# Instructions are free text, so no list of names can vouch for them. Besides the plugin's password-and-key check
# a LINE is not sent at all when it holds an address with a name and password in it, an Authorization or Bearer
# value, or a short "password: ..." of the kinds below. (A check, not a proof: the docs say not to keep
# passwords in instructions.)
_PERSONA_LINE = (
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"(?i)\bauthorization\s*[:=]"),
    re.compile(r"(?i)\bbearer\s+\S{8,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password|passwd|pwd|passphrase)\s*[:=]\s*\S{4,}"),
)


def clean_persona(text: Any) -> str:
    """Instructions as they may travel and be written: the plugin's privacy filter (control characters dropped,
    password-like text masked), lines that look like a sign-in left out, capped, and nothing in it can open or
    close one of the prompt file's blocks."""
    if not isinstance(text, str):
        return ""
    lines = []
    for line in hs.shown(text, PERSONA_CAP).split("\n"):
        hidden = any(rx.search(line) for rx in _PERSONA_LINE) or (len(line) <= 4000 and _secrets.has_secret(line))
        lines.append(hs.HIDDEN_LINE if hidden else line)
    return fp._inert("\n".join(lines))


def clean_config(raw: Any) -> dict:
    """One twin's settings reduced to the allowlist: used on what the sharing computer read from a bot, and again
    on what a linked computer received. Unknown keys, wrong types and anything that looks like a secret are
    dropped; an item that is absent stays absent."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    t = raw.get("tools")
    if isinstance(t, dict):
        tools: dict = {}
        if isinstance(t.get("toolsets"), list):
            tools["toolsets"] = _names(t["toolsets"])
        if isinstance(t.get("platform_toolsets"), dict):
            per: dict = {}
            for plat, names in list(t["platform_toolsets"].items())[:MAX_PLATFORMS]:
                if _name(plat) and isinstance(names, list):
                    per[plat] = _names(names)
            tools["platform_toolsets"] = per
        if isinstance(t.get("disabled_toolsets"), list):
            tools["disabled_toolsets"] = _names(t["disabled_toolsets"])
        tools["connectors"] = sorted(_names(t.get("connectors")))
        out["tools"] = tools
    p = raw.get("preferences")
    if isinstance(p, dict):
        out["preferences"] = {k: p[k] for k in PREFS if k in p and PREFS[k](p[k])}
    s = raw.get("persona")
    if isinstance(s, dict):
        out["persona"] = {name: clean_persona(s.get(name)) for name in PERSONA_FILES if isinstance(s.get(name), str)}
    m = raw.get("model")
    if isinstance(m, dict):
        model = {k: _model_word(m.get(k)) for k in ("default", "provider")}
        model = {k: v for k, v in model.items() if v}
        if model.get("default"):
            out["model"] = model
    return out


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8", "replace")).hexdigest()[:16]


# ---- this computer's own state ---------------------------------------------------------------------------------------
def _path() -> Path:
    return store.central_dir() / STATE_FILE


def _load() -> dict:
    raw = fio.read_json(_path(), None)
    st = {"items": {}, "twins": {}, "source": None}
    if isinstance(raw, dict):
        if isinstance(raw.get("items"), dict):
            st["items"] = {k: v for k, v in raw["items"].items() if k in ITEMS and isinstance(v, bool)}
        if isinstance(raw.get("twins"), dict):
            st["twins"] = {k: v for k, v in raw["twins"].items()
                           if isinstance(k, str) and fleet.REF_RE.match(k) and isinstance(v, dict)}
        if isinstance(raw.get("source"), dict):
            st["source"] = raw["source"]
    return st


def _save(st: dict) -> None:
    fio.write_json(_path(), st)


def switches(st: Optional[dict] = None) -> dict:
    """This computer's switch per item (the defaults where the owner has not chosen)."""
    st = _load() if st is None else st
    return {k: st["items"].get(k, DEFAULT_ON[k]) for k in ITEMS}


def _dig(doc: Any, dotted: str) -> tuple:
    """(found, value) of a dotted key in a settings document."""
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _put(doc: dict, dotted: str, value: Any, there: bool = True) -> None:
    """Set (or, with there=False, take out) a dotted key; a section left empty by a removal goes too."""
    parts = dotted.split(".")
    chain = [doc]
    for part in parts[:-1]:
        nxt = chain[-1].get(part)
        if not isinstance(nxt, dict):
            if not there:
                return
            nxt = {}
            chain[-1][part] = nxt
        chain.append(nxt)
    if there:
        chain[-1][parts[-1]] = value
        return
    chain[-1].pop(parts[-1], None)
    for i in range(len(parts) - 2, -1, -1):
        if chain[i + 1] == {}:
            chain[i].pop(parts[i], None)


def _read_doc(home: Path) -> Optional[dict]:
    """A bot's settings file as a mapping; {} when there is none; None when it cannot be told."""
    path = Path(home) / "config.yaml"
    text = fio.read_text(path, hs.CONFIG_CAP)
    if text is None:
        return None if os.path.lexists(path) else {}
    yaml = yamlio.module()
    if yaml is None:
        return None
    try:
        doc = yaml.safe_load(text) if text.strip() else {}
    except Exception:
        return None
    if doc is None:
        doc = {}
    return doc if isinstance(doc, dict) else None


# ---- the sharing computer: what a twin's bot holds, by the allowlist ----------------------------------------------
class Unreadable(Exception):
    """A twin's settings or prompt file cannot be read right now (half saved, damaged, too large)."""


MAX_MARKERS = 100  # block markers in one prompt file; a file with more is not taken apart (and not sent)


def persona_of(text: Any) -> str:
    """The instructions in a prompt file: the part that follows Grok (botport's own section), what the owner
    pasted on the Twins page, and what was written into the file by hand. The plugin's own blocks (the twin's name
    and description, the note about the owner's computers, a persona that arrived from another computer) and
    botport's name and description sections are not instructions and are left out."""
    if not isinstance(text, str) or not text:
        return ""
    if text.count("<!--") > MAX_MARKERS:
        raise Unreadable()
    rest = _PLUGIN_BLOCKS_RE.sub("", text)
    m = hs._OWN_RE.search(rest)
    own = m.group(1) if m else ""
    rest = hs._OWN_RE.sub("", rest)
    sections = {name: body for name, body in hs._BOTPORT_RE.findall(rest)}
    rest = hs._BOTPORT_RE.sub("", rest)
    parts = [p.strip() for p in (sections.get("persona") or "", own, rest) if isinstance(p, str) and p.strip()]
    return "\n\n".join(parts)


def collect(home: Path) -> dict:
    """Everything of one twin's Hermes bot that may travel (all four items), already cleaned. Raises Unreadable
    when its settings file cannot be read as it is right now (saved half-way, damaged): that must never be taken
    for "no settings", or every linked computer would empty the twin's tools and preferences."""
    doc = _read_doc(home)
    if doc is None:
        raise Unreadable()
    raw: dict = {"tools": {}, "preferences": {}, "persona": {}}
    for key, name in (("toolsets", "toolsets"), ("platform_toolsets", "platform_toolsets"),
                      ("agent.disabled_toolsets", "disabled_toolsets")):
        found, value = _dig(doc, key)
        if found:
            raw["tools"][name] = value
    servers = doc.get("mcp_servers")
    if isinstance(servers, dict):  # the NAMES only: how a connector is set up never leaves this computer
        raw["tools"]["connectors"] = [str(k) for k, v in servers.items()
                                      if not (isinstance(v, dict) and v.get("enabled") is False)]
    for key in PREFS:
        found, value = _dig(doc, key)
        if found:
            raw["preferences"][key] = value
    for name in PERSONA_FILES:
        text = fio.read_text(Path(home) / name, hs.SOUL_CAP)
        if isinstance(text, str):
            raw["persona"][name] = persona_of(text)
        elif os.path.lexists(Path(home) / name):
            raise Unreadable()
    m = doc.get("model")
    if isinstance(m, str):
        raw["model"] = {"default": m}
    elif isinstance(m, dict):
        raw["model"] = {"default": m.get("default") or m.get("model") or m.get("name"), "provider": m.get("provider")}
    return clean_config(raw)


_cache: dict = {}  # twin id -> (file dates, cleaned settings)
_cache_lock = threading.Lock()


def _file_sig(home: Path) -> tuple:
    out = []
    for name in ("config.yaml",) + PERSONA_FILES:
        try:
            s = os.stat(Path(home) / name)
            out.append((s.st_mtime_ns, s.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def source_index(st: dict) -> dict:
    """{"rev", "items", "twins": {id: digest}, "configs": {id: settings}} for the twins this computer shares, with
    the items its switches allow. A file is read again only when its date or size changed. A twin whose files
    cannot be read right now keeps the settings last read (or is left out when there are none): a linked computer
    then leaves that twin as it is."""
    on = switches()
    twins, configs = {}, {}
    lst = ((st.get("share") or {}).get("list") or {}).get("twins") or {}
    for tid, t in sorted(lst.items()):
        prof = t.get("profile") if isinstance(t, dict) else None
        if not isinstance(prof, str) or not fp.PROFILE_ID_RE.match(prof):
            continue
        try:
            home = fp.profile_dir(prof)
            if not fio.is_real_dir(home):
                continue
            mark = (prof, _file_sig(home))
            with _cache_lock:
                had = _cache.get(tid)
            if had is None or had[0] != mark:
                try:
                    got = collect(home)  # (outside the lock: a large prompt file must not hold up the others)
                except Unreadable:
                    got = had[1] if had is not None and had[0][0] == prof else None
                had = (mark, got)
                with _cache_lock:
                    _cache[tid] = had
        except Exception:
            log.debug("grok-bot-twin: could not read a twin's settings", exc_info=True)
            continue
        if had[1] is None:
            continue
        cfg = {k: v for k, v in had[1].items() if on.get(k)}
        configs[tid] = cfg
        twins[tid] = _digest(cfg)
    with _cache_lock:
        for tid in [t for t in _cache if t not in lst]:
            del _cache[tid]
    return {"rev": _digest({"items": on, "twins": twins}), "items": on, "twins": twins, "configs": configs}


def source_rev(st: dict) -> str:
    return source_index(st)["rev"]


# ---- the sharing computer: the wire ------------------------------------------------------------------------------------
_watch = {"until": 0.0, "thread": None, "rev": None}


def _watch_loop() -> None:
    """While a linked computer that takes settings has looked in lately: notice a changed settings file within a
    few seconds and wake the waiting looks (they then answer with the new rev)."""
    while time.monotonic() < _watch["until"] and not fleet.hub.closing:
        time.sleep(WATCH_S)
        try:
            from . import fleet_server as server
            if server.serving() is None or not fleet.exists():
                break  # this process no longer shares (or its Hermes folder is gone): nothing to watch
            st = fleet.load()
            if not (st.get("share") or {}).get("on"):
                break
            rev = source_rev(st)
            if rev != _watch["rev"]:
                _watch["rev"] = rev
                fleet.hub.bump()
        except Exception:
            log.debug("grok-bot-twin: settings watch failed", exc_info=True)
    _watch["thread"] = None


def _keep_watching(rev: str) -> None:
    _watch["until"] = time.monotonic() + WATCH_FOR_S
    if _watch["thread"] is None:
        _watch["rev"] = rev
        t = threading.Thread(target=_watch_loop, name="grok-bot-twin-settings-watch", daemon=True)
        _watch["thread"] = t
        t.start()


_told: dict = {}  # linked computer's id -> [rev, how often it was told at once, since when]
TOLD_AT_ONCE = 3   # at-once answers per computer and rev ...
TOLD_WINDOW_S = 60.0  # ... within this time; after that it hears it when its waiting look runs out


def sync_extra(body: dict, st: dict, machine_id: str = "") -> tuple:
    """For the share server's answer to a look that found the twin list unchanged: ({"xs": {"config": rev}}, True
    when that differs from what the asking computer last heard, and it has not been told so three times in the
    last minute). ({}, False) for a computer that did not say it takes settings (an older build): it is told
    nothing new. The limit is for a computer that cannot keep what it hears (its disk is full): it is not
    answered at once for ever. More than one, because a look that computer gave up on may still be waiting here
    and must not use up the answer meant for its live one."""
    heard = body.get("xs") if isinstance(body, dict) else None
    if not isinstance(heard, dict) or NAME not in heard:
        return {}, False
    try:
        rev = source_rev(st)
    except Exception:
        log.debug("grok-bot-twin: settings rev failed", exc_info=True)
        return {}, False
    _keep_watching(rev)
    if heard.get(NAME) == rev:
        return {"xs": {NAME: rev}}, False
    with _cache_lock:
        told = _told.get(machine_id)
        if told is None or told[0] != rev or time.monotonic() - told[2] > TOLD_WINDOW_S:
            if len(_told) > 4 * fleet.MAX_MACHINES:
                _told.clear()
            told = _told[machine_id] = [rev, 0, time.monotonic()]
        now = told[1] < TOLD_AT_ONCE
        if now:
            told[1] += 1
    return {"xs": {NAME: rev}}, now


def wire(asked: Any, body: dict) -> tuple:
    """`POST /v1/xs/config`, signed and counted like every look: the index (which twins have settings, and a digest
    of each), or one twin's settings. THE ALLOWLIST is `clean_config`: nothing else of a bot can ride along."""
    from . import fleet_server as server
    if body.get("proto") != fleet.PROTO:
        return server._err(426, "version_mismatch") + (None,)
    again = server._replayed(asked, body)
    if again is not None:
        return again
    st = fleet.load()
    f, key = asked.by(st)
    if f is None:
        return server._unlinked(key, asked)
    signed = (key, asked.nonce)
    idx = source_index(st)
    tid = body.get("twin")
    if tid is None:
        return 200, {"schema": fleet.SCHEMA, "xs": XS, "rev": idx["rev"], "items": idx["items"],
                     "twins": idx["twins"]}, signed
    if not isinstance(tid, str) or tid not in idx["twins"]:
        return server._err(404, "not_found") + (signed,)
    return 200, {"schema": fleet.SCHEMA, "xs": XS, "id": tid, "rev": idx["twins"][tid],
                 "config": idx["configs"][tid]}, signed


def announce() -> None:
    """A switch changed on this computer: waiting looks answer now (also those another Hermes process holds)."""
    fleet.hub.bump()
    try:
        from . import fleet_server as server
        server._tell_server()
    except Exception:
        log.debug("grok-bot-twin: could not tell the share server", exc_info=True)


# ---- a linked computer: hear, fetch, apply ---------------------------------------------------------------------------
def sync_body() -> dict:
    """What a linked computer adds to its look: the settings rev it last heard ("" when none)."""
    try:
        src = _load().get("source") or {}
        rev = src.get("rev")
        return {"xs": {NAME: rev if isinstance(rev, str) and REV_RE.match(rev) else ""}}
    except Exception:
        return {"xs": {NAME: ""}}


def wait_for(wait_s: Any) -> Any:
    """How long this computer's next look may wait: as asked, but short while settings are owed (a fetch or a
    write failed), so the next try comes in seconds and not after a full waiting look. Never raises."""
    try:
        if not isinstance(wait_s, (int, float)) or wait_s <= OWED_WAIT_S:
            return wait_s
        done = (_load().get("source") or {}).get("done")
        return OWED_WAIT_S if isinstance(done, dict) and done.get("ok") is False else wait_s
    except Exception:
        return wait_s


def _link_mark(ln: dict) -> str:
    return _digest([str((ln.get("source") or {}).get("id")), str(ln.get("linked_at"))])


def heard(obj: dict) -> None:
    """Note what a SIGNED answer of the sharing computer said about settings. An answer of "nothing changed" with
    no word about them comes from an older Grok Bot Twin there: this computer then says so and asks for nothing."""
    try:
        if obj.get("changed") is True or obj.get("sharing") is False:
            return  # (the twin list, or "sharing is off": neither carries the key on any build)
        xs = obj.get("xs")
        rev = xs.get(NAME) if isinstance(xs, dict) else None
        rev = rev if isinstance(rev, str) and REV_RE.match(rev) else None
        ln = fleet.load().get("link")
        if not ln:
            return
        mark = _link_mark(ln)
        st = _load()
        src = st.get("source") or {}
        new = {"link": mark, "state": "on" if rev else "older", "rev": rev,
               "items": src.get("items") if src.get("link") == mark else None,
               "done": src.get("done") if src.get("link") == mark else None}
        if {k: src.get(k) for k in new} == new:
            return
        with fleet.locked():
            st = _load()
            if (st.get("source") or {}).get("link") != mark:
                st["twins"] = {}  # another link: what was noted about the old one's twins is forgotten
                _forget_points()
            st["source"] = {**(st.get("source") or {}), **new, "at": now_iso()}
            _save(st)
    except Exception:
        log.debug("grok-bot-twin: could not note the settings rev", exc_info=True)


def _clean_index(obj: Any) -> Optional[dict]:
    if not isinstance(obj, dict) or obj.get("xs") != XS or not isinstance(obj.get("rev"), str) \
            or not REV_RE.match(obj["rev"]):
        return None
    items = obj.get("items") if isinstance(obj.get("items"), dict) else {}
    twins = {}
    for tid, d in list((obj.get("twins") if isinstance(obj.get("twins"), dict) else {}).items())[:fleet.MAX_TWINS]:
        if isinstance(tid, str) and fleet.REF_RE.match(tid) and isinstance(d, str) and REV_RE.match(d):
            twins[tid] = d
    return {"rev": obj["rev"], "items": {k: items.get(k) is True for k in ITEMS}, "twins": twins}


def _ask(ln: dict, machine_id: str, body: dict) -> tuple:
    from . import fleet_follow as follow
    return follow._counted_request(ln, machine_id, ROUTE, {"proto": fleet.PROTO, **body}, timeout=12.0)


def _known_toolsets() -> Optional[set]:
    """The tool sets this Hermes knows, or None when that cannot be told from here (the plugin's stand-alone tools
    run without Hermes)."""
    try:
        import toolsets as _ts  # Hermes' own module
        names = set(_ts.get_toolset_names())
        return names or None
    except Exception:
        return None


def needs_of(cfg: dict, doc: dict, took: dict, own_setup: Any = False) -> list:
    """What these settings ask for that this computer lacks: connectors that are not set up for this twin here,
    tool sets this Hermes does not know, and a provider this computer does not use."""
    out: list = []
    tools = cfg.get("tools") if took.get("tools") else None
    if isinstance(tools, dict):
        here = doc.get("mcp_servers") if isinstance(doc.get("mcp_servers"), dict) else {}
        connectors = [c for c in tools.get("connectors", [])]
        for name in connectors:
            if name not in here:
                out.append({"kind": "connector", "name": name,
                            "message": f"The connector {name} is not set up on this computer. Add it to this twin "
                                       "in Hermes here; its sign-in is never copied."})
        known = _known_toolsets()
        if known is not None:
            wanted: list = list(tools.get("toolsets", []))
            for names in (tools.get("platform_toolsets") or {}).values():
                wanted += names
            seen: set = set()
            for name in wanted:
                if name in seen or name in known or name in connectors or name in here or name == "no_mcp":
                    continue
                seen.add(name)
                out.append({"kind": "tools", "name": name,
                            "message": f"The tool set {name} is not part of Hermes on this computer. Update Hermes "
                                       "or add what provides it here."})
    model = cfg.get("model") if took.get("model") else None
    if isinstance(model, dict) and own_setup:
        out.append({"kind": "model", "name": model.get("provider") or model["default"],
                    "message": f"This twin has a model set-up of its own on this computer, so the model "
                               f"{model['default']} was not taken. Change it in Hermes here if you want the same one."})
    elif isinstance(model, dict) and model.get("provider"):
        root = _read_doc(store.hermes_root()) or {}
        mine = root.get("model").get("provider") if isinstance(root.get("model"), dict) else None
        if model["provider"] != mine:
            out.append({"kind": "model", "name": model["provider"],
                        "message": f"The model {model['default']} needs {model['provider']}. Sign in to it in Hermes "
                                   "on this computer; sign-ins are never copied."})
    return out[:40]


def _points_path(tid: str) -> Path:
    return store.central_dir() / POINTS_DIR / f"{tid}.json"


def _points(tid: str) -> list:
    raw = fio.read_json(_points_path(tid), None)
    return [p for p in raw if isinstance(p, dict) and isinstance(p.get("point"), str)
            and POINT_RE.match(p["point"])][-MAX_POINTS:] if isinstance(raw, list) else []


def _persona_block(text: str) -> Optional[str]:
    return fp.get_block(text, PERSONA_BEGIN, PERSONA_END)


_OWN_SETUP = ("base_url", "api_base", "api_key", "api_mode")


def apply(home: Path, cfg: dict, took: dict, source_name: str = "", note: Any = None) -> dict:
    """Write one twin's settings into its Hermes bot here: only the items in `took`, only allowlisted keys, and
    only after noting what was there before (`note(before, changed)` is called before the first file is written,
    so a write that fails half-way can still be put back). Returns {"changed": [items], "before": {...} | None,
    "doc": the settings document as it is now, "own_setup": True when the model was not taken because this
    computer's twin has a model set-up of its own}. Raises SettingsError when the bot's files cannot be read or
    written."""
    doc = _read_doc(home)
    if doc is None:
        raise SettingsError("no_yaml")
    new = copy.deepcopy(doc)
    own_setup = False
    before: dict = {"config": {}, "persona": None}
    changed: list = []

    def setkey(item: str, key: str, there: bool, value: Any = None) -> None:
        found, old = _dig(new, key)
        if (found, old) == (there, value if there else None):
            return
        before["config"][key] = old if found else ABSENT
        _put(new, key, value, there)
        if item not in changed:
            changed.append(item)

    tools = cfg.get("tools")
    if took.get("tools") and isinstance(tools, dict):
        for key, name in (("toolsets", "toolsets"), ("platform_toolsets", "platform_toolsets"),
                          ("agent.disabled_toolsets", "disabled_toolsets")):
            setkey("tools", key, name in tools, tools.get(name))
    prefs = cfg.get("preferences")
    if took.get("preferences") and isinstance(prefs, dict):
        for key in PREFS:
            setkey("preferences", key, key in prefs, prefs.get(key))
    model = cfg.get("model")
    here = new.get("model") if isinstance(new.get("model"), dict) else {}
    if took.get("model") and isinstance(model, dict) and model.get("default") \
            and model.get("provider") != here.get("provider") and any(k in here for k in _OWN_SETUP):
        # this twin's model here comes with an address or key of its own: another provider's name next to them
        # would send that key to the wrong place. The model is left alone and named as something to set up
        own_setup = True
    elif took.get("model") and isinstance(model, dict) and model.get("default"):
        if isinstance(new.get("model"), str):  # the short form: the same thing written as a section
            new["model"] = {"default": new["model"]}
        # only these two names are set, and only they are noted for "put back": whatever else the section holds
        # on this computer (an address, a key the owner typed there) is neither read, changed nor copied anywhere
        for key in MODEL_KEYS:
            name = key.split(".", 1)[1]
            setkey("model", key, bool(model.get(name)), model.get(name))
    soul_path = Path(home) / "SOUL.md"
    soul = None
    persona = cfg.get("persona")
    if took.get("persona") and isinstance(persona, dict) and "SOUL.md" in persona:
        current = fio.read_text(soul_path, hs.SOUL_CAP)
        if current is None and os.path.lexists(soul_path):
            raise SettingsError("write_failed")
        current = current or ""
        text = clean_persona(persona["SOUL.md"])
        soul = fp.set_block(current, PERSONA_BEGIN, PERSONA_END, f"{PERSONA_HEADER}\n{text}" if text else None)
        if soul != current:
            before["persona"] = {"block": _persona_block(current)}
            changed.append("persona")
        else:
            soul = None
    out = None
    if new != doc:
        yaml = yamlio.module()
        if yaml is None:
            raise SettingsError("no_yaml")
        try:
            out = yaml.safe_dump(new, default_flow_style=False, sort_keys=False, allow_unicode=True)
            if yaml.safe_load(out) != new:
                raise SettingsError("write_failed")
        except SettingsError:
            raise
        except Exception as exc:
            raise SettingsError("write_failed") from exc
    try:
        if changed and note is not None:
            note(before, changed)  # what was there, kept before anything is written
        if out is not None:
            fio.write_text(Path(home) / "config.yaml", out)
        if soul is not None:
            fio.write_text(soul_path, soul)
    except OSError as exc:
        raise SettingsError("write_failed") from exc
    return {"changed": changed, "before": before if changed else None, "doc": new, "own_setup": own_setup}


def _own_model_back(home: Path, own: dict) -> bool:
    """Put back the model this computer had for a twin before the other computer's model arrived (the two names
    only). True when the settings file changed."""
    doc = _read_doc(home)
    if doc is None:
        raise SettingsError("no_yaml")
    new = copy.deepcopy(doc)
    for key in MODEL_KEYS:
        if key in own:
            if isinstance(new.get("model"), str):
                new["model"] = {"default": new["model"]}
            old = own[key]
            _put(new, key, None if old == ABSENT else old, old != ABSENT)
    if new == doc:
        return False
    yaml = yamlio.module()
    out = yaml.safe_dump(new, default_flow_style=False, sort_keys=False, allow_unicode=True)
    if yaml.safe_load(out) != new:
        raise SettingsError("write_failed")
    try:
        fio.write_text(Path(home) / "config.yaml", out)
    except OSError as exc:
        raise SettingsError("write_failed") from exc
    return True


def _add_point(tid: str, before: dict, changed: list, source_name: str) -> None:
    pts = _points(tid)
    pts.append({"point": "p-" + _random.token_hex(6), "at": now_iso(), "items": [i for i in ITEMS if i in changed],
                "from": fp.machine_name(source_name), "before": before})
    while len(pts) > MAX_POINTS:
        # the oldest point goes, but not what it knew: it is folded into the next one, so putting back as far as
        # possible still ends at what this computer had before any settings arrived
        old, nxt = pts.pop(0), pts[0]
        ob = old.get("before") if isinstance(old.get("before"), dict) else {}
        nb = nxt.get("before") if isinstance(nxt.get("before"), dict) else {}
        nxt["before"] = {"config": {**(nb.get("config") or {}), **(ob.get("config") or {})},
                         "persona": ob.get("persona") if ob.get("persona") is not None else nb.get("persona")}
        nxt["items"] = [i for i in ITEMS if i in (old.get("items") or []) + (nxt.get("items") or [])]
    fio.write_json(_points_path(tid), pts)


def _forget_points() -> None:
    """A new link: the notes for "put back" belong to the old one and go (files named by a twin's id, nothing else)."""
    root = store.central_dir() / POINTS_DIR
    try:
        names = [e.name for e in os.scandir(root) if e.is_file(follow_symlinks=False)]
    except OSError:
        return
    for name in names:
        if name.endswith(".json") and fleet.REF_RE.match(name[:-5]):
            try:
                os.unlink(root / name)
            except OSError:
                pass


def follow(svc: Any = None, force: bool = False) -> int:
    """A linked computer: bring its twins' settings in line with what the sharing computer holds now. Asks only
    when there is something to do (a new rev was heard, a twin has no settings yet, a switch here changed, or an
    earlier try failed over a minute ago). Returns how many twins changed. Never raises."""
    try:
        from . import fleet_follow as ff
        fst = fleet.load()
        ln = fst.get("link")
        if not ln:
            return 0
        st = _load()
        src = st.get("source") or {}
        if src.get("state") != "on" or src.get("link") != _link_mark(ln) or not src.get("rev"):
            return 0
        mine = switches(st)
        here = {tid: e for tid, e in ln["twins"].items()
                if isinstance(e, dict) and isinstance(e.get("profile"), str) and e.get("state") != "error"
                and not e.get("making")}
        done = src.get("done") if isinstance(src.get("done"), dict) else {}
        want = {"rev": src["rev"], "took": mine, "twins": sorted(here)}
        tried = parse_iso(done.get("tried_at"))
        if not force and done.get("want") == want and (done.get("ok") or (
                tried is not None and (fleet.now_dt() - tried).total_seconds() < RETRY_S)):
            return 0
        me = fleet.this_machine(fst)
        n_changed = 0
        source_name = fp.machine_name((ln.get("source") or {}).get("name"))
        for attempt in (1, 2):  # (a second pass only when a twin's settings changed again while this one ran)
            ok, moved, idx = True, False, None
            try:
                status, obj, signed = _ask(ln, me["id"], {})
                idx = _clean_index(obj) if status == 200 and signed else None  # (nothing unsigned is acted on)
            except ff.Unreachable:
                idx = None
            if idx is None:
                _note_done(want, False)
                return n_changed
            n_changed += _follow_pass(svc, ln, me, st, here, mine, idx, source_name, force)
            ok, moved = _pass["ok"], _pass["moved"]
            if ok or not moved:
                break
            st = _load()
        _note_source(items=idx["items"])
        _note_done(want, ok)
        if n_changed and svc is not None:
            svc.emit("fleet", None, {"what": "settings", "count": n_changed})
        return n_changed
    except Exception:
        log.debug("grok-bot-twin: following settings failed", exc_info=True)
        return 0


_pass = {"ok": True, "moved": False}  # how the last pass went (one follower thread per process holds the lock)


def _follow_pass(svc: Any, ln: dict, me: dict, st: dict, here: dict, mine: dict, idx: dict, source_name: str,
                 force: bool) -> int:
    """One pass over this computer's twins against one index. Returns how many changed; `_pass` says whether
    every twin is done and whether a twin's settings moved on while the pass ran."""
    from . import fleet_follow as ff
    ok, moved, n_changed = True, False, 0
    try:
        for tid, entry in sorted(here.items()):
            digest = idx["twins"].get(tid)
            rec = st["twins"].get(tid) if isinstance(st["twins"].get(tid), dict) else {}
            took = {k: bool(mine[k] and idx["items"][k]) for k in ITEMS}
            if digest is None:
                _note_twin(tid, {"seen": None, "state": "off"})
                continue
            if rec.get("held") == digest:
                continue  # the owner put this twin's settings back: they stay until they next change over there
            if not force and rec.get("rev") == digest and rec.get("took") == took and not rec.get("error"):
                continue
            try:
                status, obj, signed = _ask(ln, me["id"], {"twin": tid})
            except ff.Unreachable:
                ok = False
                break  # it stopped answering: the rest waits for the next look instead of timing out one by one
            if status != 200 or not signed or obj.get("id") != tid or obj.get("rev") != digest:
                ok = False
                moved = moved or (status == 200 and signed and obj.get("id") == tid)  # changed again meanwhile
                continue
            cfg = clean_config(obj.get("config"))
            try:
                with fleet.locked():
                    # what is true NOW, not when this pass began: the owner may have put this twin's settings
                    # back, or unlinked and linked again, while the answer was on its way
                    now_ln = fleet.load().get("link")
                    now_rec = _load()["twins"].get(tid)
                    now_rec = now_rec if isinstance(now_rec, dict) else {}
                    if not now_ln or _link_mark(now_ln) != _link_mark(ln) or now_rec.get("held") == digest:
                        continue
                    rec = now_rec
                    home = fp.profile_dir(entry["profile"])
                    if not fp.is_mirrored(home):
                        continue  # not (or no longer) this twin's own bot: nothing is written into it
                    res = apply(home, cfg, took, source_name,
                                note=lambda before, changed, _t=tid: _add_point(_t, before, changed, source_name))
                    # the model: while it follows, the names this computer had for the twin are remembered; when
                    # it no longer follows (either computer switched it off) they are put back, so "each
                    # computer keeps its own model" is true again without the owner doing anything
                    own = rec.get("own_model") if isinstance(rec.get("own_model"), dict) else None
                    if took["model"]:
                        if own is None and res["before"] is not None and "model" in res["changed"]:
                            # both names as they were, also the one that did not change this time
                            own = {}
                            for k in MODEL_KEYS:
                                found, value = _dig(res["doc"], k)
                                own[k] = res["before"]["config"].get(k, value if found else ABSENT)
                    elif own is not None:
                        if _own_model_back(home, own) and "model" not in res["changed"]:
                            res["changed"].append("model")
                        res["doc"] = _read_doc(home) or res["doc"]
                        own = None
                    needs = needs_of(cfg, res["doc"], took, res.get("own_setup"))
                    _note_twin(tid, {"rev": digest, "seen": digest, "took": took, "at": now_iso(), "error": None,
                                     "held": None, "needs": needs, "own_model": own,
                                     **({"changed": res["changed"], "changed_at": now_iso()} if res["changed"] else {})})
                if res["changed"]:
                    n_changed += 1
            except SettingsError as exc:
                ok = False
                _note_twin(tid, {"seen": digest, "error": exc.code})
            except Exception:
                ok = False
                log.debug("grok-bot-twin: could not apply a twin's settings", exc_info=True)
                _note_twin(tid, {"seen": digest, "error": "write_failed"})
    finally:
        _pass.update(ok=ok, moved=moved)
    return n_changed


def _note_twin(tid: str, fields: dict) -> None:
    with fleet.locked():
        st = _load()
        rec = st["twins"].get(tid) if isinstance(st["twins"].get(tid), dict) else {}
        st["twins"][tid] = {**rec, **fields}
        _save(st)


def _note_source(**fields: Any) -> None:
    with fleet.locked():
        st = _load()
        src = st.get("source") or {}
        if {k: src.get(k) for k in fields} != fields:
            st["source"] = {**src, **fields}
            _save(st)


def _note_done(want: dict, ok: bool) -> None:
    _note_source(done={"want": want, "ok": ok, "tried_at": now_iso()})


# ---- what the Twins page and the terminal call -----------------------------------------------------------------------
def _joined(words: list) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def what_follows(on: dict) -> str:
    return _joined([WORDS[i] for i in ITEMS if on.get(i)])


def _sentence(role: str, on: dict, other: str) -> str:
    """What follows, in one sentence, for this computer's role and the switches as they are."""
    names = [WORDS[i] for i in ITEMS if on.get(i)]
    own = "" if on.get("model") else (" " + (OWN_MODEL if role == "source" else "This computer keeps its own model."))
    if not names:
        return ("No settings follow from this computer: your other computers keep their own." if role == "source"
                else f"No settings follow {other}: this computer keeps its own.")
    verb = "follow" if len(names) > 1 else "follows"
    if role == "source":
        return f"Each twin's {_joined(names)} {verb} from this computer to your other computers.{own}"
    return f"Each twin's {_joined(names)} {verb} {other}.{own}"


def _label(p: dict) -> str:
    return f"what this twin had before settings arrived from {fp.machine_name(p.get('from'))}"


def view(svc: Any = None) -> dict:
    """`GET /fleet/settings`: the switches, the one sentence of what follows, and (on a linked computer) how each
    twin's settings are doing here, with what can be put back."""
    fst = fleet.load()
    role = fleet.role(fst)
    st = _load()
    mine = switches(st)
    out: dict = {"role": role, "other": None, "state": "none", "sentence": "", "never": NEVER, "items": [],
                 "twins": []}
    theirs = None
    if role == "follower":
        ln = fst["link"]
        out["other"] = fp.machine_name((ln.get("source") or {}).get("name"))
        src = st.get("source") or {}
        same = src.get("link") == _link_mark(ln)
        out["state"] = (src.get("state") if same and src.get("state") in ("on", "older") else "waiting")
        if same and isinstance(src.get("items"), dict):
            theirs = {k: src["items"].get(k) is True for k in ITEMS}
    elif role == "source":
        out["state"] = "on"
    for item in ITEMS:
        label, what = LABELS[item]
        note = None
        if item == "model" and not mine[item]:
            note = OWN_MODEL
        elif theirs is not None and mine[item] and not theirs[item]:
            note = f"Switched off on {out['other']}."
        out["items"].append({"id": item, "label": label, "what": what, "on": mine[item], "note": note})
    effective = {k: mine[k] and (theirs[k] if theirs is not None else True) for k in ITEMS}
    out["sentence"] = "" if role == "none" else _sentence(role, effective, out["other"] or "")
    if role != "follower":
        return out
    ln = fst["link"]
    for tid, e in sorted(ln["twins"].items(), key=lambda kv: (str((kv[1] or {}).get("name")).lower(), kv[0])):
        if not isinstance(e, dict) or e.get("making"):
            continue
        rec = st["twins"].get(tid) if isinstance(st["twins"].get(tid), dict) else {}
        needs = [n for n in rec.get("needs") or [] if isinstance(n, dict)][:40]
        pts = _points(tid)
        if out["state"] != "on" or not any(effective.values()):
            state = "off" if out["state"] == "on" else "waiting"
        elif rec.get("held"):
            state = "held"
        elif rec.get("error"):
            state = "error"
        elif rec.get("state") == "off" and not rec.get("rev"):
            state = "off"
        elif not rec.get("rev") or rec.get("seen") != rec.get("rev"):
            state = "waiting"
        elif needs:
            state = "set_up"
        else:
            state = "in_sync"
        row = {"id": tid, "name": fleet.clean_text(e.get("name"), fleet.NAME_CAP, True) or "Twin",
               "profile": e.get("profile") if isinstance(e.get("profile"), str) else None, "state": state,
               "synced_at": rec.get("at") if parse_iso(rec.get("at")) else None,
               "changed": [i for i in ITEMS if i in (rec.get("changed") or [])],
               "needs": needs if state in ("set_up", "held") else [], "held": bool(rec.get("held")),
               "error": None,
               "restore": [{"point": p["point"], "at": p.get("at") if parse_iso(p.get("at")) else None,
                            "items": [i for i in ITEMS if i in (p.get("items") or [])], "label": _label(p)}
                           for p in reversed(pts)]}
        if state == "error":
            code = rec.get("error") if rec.get("error") in MESSAGES else "write_failed"
            row["error"] = {"code": code, "message": MESSAGES[code][0], "hint": MESSAGES[code][1]}
        out["twins"].append(row)
    return out


def switch(item: Any, on: Any, svc: Any = None) -> dict:
    """`POST /fleet/settings`: one item on or off on this computer. Off takes nothing away: a twin keeps what it
    has and stops following changes to that item. The model is the exception: switched off again, each twin gets
    back the model this computer had for it."""
    if item not in ITEMS:
        raise SettingsError("bad_item")
    if not isinstance(on, bool):
        raise EngineError("bad_input", "That request was not understood.")
    with fleet.locked():
        st = _load()
        if on == DEFAULT_ON[item]:
            st["items"].pop(item, None)
        else:
            st["items"][item] = on
        _save(st)
    role = fleet.role(fleet.load())
    if role == "source":
        announce()
    elif role == "follower":
        follow(svc, force=False)
    return view(svc)


def _twin_id(fst: dict, twin: Any) -> str:
    ln = fst.get("link")
    if not ln:
        raise SettingsError("not_linked")
    if isinstance(twin, str):
        if twin in ln["twins"] and isinstance(ln["twins"][twin], dict):
            return twin
        want = twin.strip().lower()
        hits = [tid for tid, e in ln["twins"].items() if isinstance(e, dict)
                and want in (str(e.get("name")).lower(), str(e.get("profile")).lower())]
        if len(hits) == 1:
            return hits[0]
    raise SettingsError("unknown_twin")


def restore(twin: Any, point: Any = None, svc: Any = None) -> dict:
    """`POST /fleet/settings/restore`: put back what a twin's bot had before settings arrived (the newest restore
    point, or the one named), and keep it that way until its settings next change on the sharing computer."""
    fst = fleet.load()
    tid = _twin_id(fst, twin)
    with fleet.locked():
        fst = fleet.load()
        ln = fst.get("link")
        entry = ln["twins"].get(tid) if ln else None
        if not isinstance(entry, dict) or not isinstance(entry.get("profile"), str):
            raise SettingsError("unknown_twin")
        pts = _points(tid)
        if point is None and pts:
            point = pts[-1]["point"]
        at = next((i for i, p in enumerate(pts) if p["point"] == point), None)
        if at is None:
            raise SettingsError("unknown_point")
        home = fp.profile_dir(entry["profile"])
        if not fp.is_mirrored(home):
            raise SettingsError("unknown_twin")
        # every point from the newest back to the one named is undone, newest first, so the bot ends up exactly
        # as it was before the named one was taken
        doc = _read_doc(home)
        if doc is None:
            raise SettingsError("no_yaml")
        new = copy.deepcopy(doc)
        soul_path = home / "SOUL.md"
        current = fio.read_text(soul_path, hs.SOUL_CAP)
        if current is None and os.path.lexists(soul_path):
            raise SettingsError("write_failed")
        current = current or ""
        soul = current
        for p in reversed(pts[at:]):
            before = p.get("before") if isinstance(p.get("before"), dict) else {}
            for key, old in (before.get("config") or {}).items():
                if key not in MODEL_KEYS and key not in TOOL_KEYS and key not in PREFS:
                    continue  # (a restore point is this computer's own file, and still only allowlisted keys)
                if key in MODEL_KEYS and isinstance(new.get("model"), str):
                    new["model"] = {"default": new["model"]}
                _put(new, key, None if old == ABSENT else old, old != ABSENT)
            if isinstance(before.get("persona"), dict):
                block = before["persona"].get("block")
                soul = fp.set_block(soul, PERSONA_BEGIN, PERSONA_END, block if isinstance(block, str) else None)
        try:
            if new != doc:
                yaml = yamlio.module()
                text = yaml.safe_dump(new, default_flow_style=False, sort_keys=False, allow_unicode=True)
                if yaml.safe_load(text) != new:
                    raise SettingsError("write_failed")
                fio.write_text(home / "config.yaml", text)
            if soul != current:
                fio.write_text(soul_path, soul)
        except OSError as exc:
            raise SettingsError("write_failed") from exc
        fio.write_json(_points_path(tid), pts[:at])
        st = _load()
        rec = st["twins"].get(tid) if isinstance(st["twins"].get(tid), dict) else {}
        st["twins"][tid] = {**rec, "held": rec.get("seen") or rec.get("rev") or "held", "changed": [],
                            "error": None}
        _save(st)
    if svc is not None:
        svc.emit("fleet", None, {"what": "settings", "count": 1})
    return view(svc)


def follow_again(twin: Any, svc: Any = None) -> dict:
    """`POST /fleet/settings/follow`: end the hold a "put back" left and take the current settings again."""
    fst = fleet.load()
    tid = _twin_id(fst, twin)
    with fleet.locked():
        st = _load()
        rec = st["twins"].get(tid) if isinstance(st["twins"].get(tid), dict) else {}
        st["twins"][tid] = {**rec, "held": None, "rev": None}
        _save(st)
    follow(svc, force=True)
    return view(svc)


def summary_line(v: dict) -> str:
    """One line for `hermes twin fleet`."""
    on = {i["id"]: i["on"] for i in v["items"]}
    what = what_follows(on)
    head = f"Settings that follow: {what}." if what else "Settings that follow: none."
    return head if on.get("model") else f"{head} {OWN_MODEL}"


def describe(v: dict) -> list:
    """`hermes twin fleet settings` in words."""
    out = [v["sentence"] or "This computer is not linked to another one and does not share: nothing follows yet."]
    if v["state"] == "older":
        out.append(f"{v['other']} needs a newer Grok Bot Twin before settings can follow.")
    elif v["state"] == "waiting":
        out.append(f"Waiting for {v['other']}.")
    for i in v["items"]:
        out.append(f"  {i['label'].lower():<12} {'on ' if i['on'] else 'off'}  {i['what']}" + (f" {i['note']}" if i["note"] else ""))
    words = {"in_sync": "in sync", "waiting": "waiting for its settings", "held": "kept as they are here",
             "set_up": "in sync; something to set up on this computer", "error": "could not be saved here",
             "off": "nothing follows"}
    for t in v["twins"]:
        out.append(f"  {t['name']}: {words.get(t['state'], t['state'])}"
                   + (f" (changed: {', '.join(t['changed'])})" if t["changed"] else ""))
        for n in t["needs"]:
            out.append(f"      Set up on this computer: {n['message']}")
        if t["error"]:
            out.append(f"      {t['error']['message']} {t['error']['hint']}".rstrip())
        if t["restore"]:
            out.append(f"      Can be put back: hermes twin fleet settings restore \"{t['name']}\"")
        if t["held"]:
            out.append(f"      Follow again: hermes twin fleet settings follow \"{t['name']}\"")
    out.append(v["never"])
    return out


def cli(args: Any, svc: Any, out: Any, to_json: Any) -> int:
    """`hermes twin fleet settings [ITEM on|off | restore TWIN [POINT] | follow TWIN]`."""
    words = [w for w in ([getattr(args, "value", None)] + list(getattr(args, "more", None) or [])) if w]
    if not words:
        v = view(svc)
    elif words[0] in ITEMS:
        if len(words) != 2 or words[1] not in ("on", "off"):
            raise EngineError("usage", f"Say on or off: hermes twin fleet settings {words[0]} on")
        v = switch(words[0], words[1] == "on", svc)
    elif words[0] == "restore":
        if len(words) < 2:
            raise EngineError("usage", "Say which twin: hermes twin fleet settings restore NAME")
        v = restore(words[1], words[2] if len(words) > 2 else None, svc)
    elif words[0] == "follow":
        if len(words) < 2:
            raise EngineError("usage", "Say which twin: hermes twin fleet settings follow NAME")
        v = follow_again(words[1], svc)
    else:
        raise SettingsError("bad_item")
    if getattr(args, "json", False):
        return to_json(v)
    for line in describe(v):
        out(line)
    return 0


def _reset_for_tests() -> None:
    _told.clear()
    with _cache_lock:
        _cache.clear()
    _watch.update(until=0.0, rev=None)
