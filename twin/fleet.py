"""Twins on the owner's other machines: the state, the shared list, and what each side does with it.

One machine (the one with the Grok app) is the SOURCE: it shares a list of its real twins. Every other machine
is a FOLLOWER: it keeps one Hermes bot per listed twin. This module holds everything that is not a socket:

- `fleet.json` in the plugin's data folder (owner-only): this machine's id and name, the sharing switch, the
  link code, the linked machines (source) or the link (follower), and the Hermes bots kept after a removal.
- The source's list: built from the plugin's registry of real twins (never the sample), each entry reduced to
  an opaque id, name, description, a version number and a date, after the plugin's existing text filter.
  `wire_list` is the one place that decides what leaves this machine; it copies named fields only.
- What a follower does with a list (`apply_list`): make, update or take away Hermes bots. A twin is taken away
  only when the source says so explicitly; a twin that is merely missing from a list is kept.
- The answers of the local API (`view`) and the plain sentences of its errors.

The HTTP server is in fleet_server.py, the follower's client and loop in fleet_follow.py, the files of a twin's
Hermes bot in fleet_profiles.py. Wire format: schema grok-bot-twin.share.v1.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, fleet_io as fio, fleet_net as net, fleet_profiles as fp, safe, store
from . import hermes_side as hs
from .engine import EngineError
from .model import REF_RE, iso, now_dt, now_iso, pair_ref, parse_iso, plural, valid_id

log = logging.getLogger(__name__)

SCHEMA = "grok-bot-twin.share.v1"
PROTO = 3  # 2: nothing secret is sent (a proof at linking, signed requests and answers afterwards)
#            3: linking also agrees a secret that someone who only listens cannot work out, and the link code's
#               key is slow to make (see "keys and signatures" below)
STATE_FILE = "fleet.json"
KEPT_DIR = "fleet-kept"
MAX_MACHINES = 16
MAX_TWINS = 500
MAX_REMOVED = 500
MAX_KEPT = 200  # Hermes bots kept after a removal; beyond that the oldest go before their 30 days are over
KEEP_DAYS = 30
MAX_LOOSE = 50  # folders in the kept place that no record names (set aside, left by a crash): the newest stay
REMOVED_DAYS = 90
OFFLINE_AFTER_S = 120
NAME_CAP = 100
DESCRIPTION_CAP = 2000
MACHINE_ID_RE = re.compile(r"^m-[0-9a-f]{12}\Z")  # (\Z, not $: a trailing newline is not part of an id)
RID_RE = re.compile(r"^r-[0-9a-f]{12}\Z")
REV_RE = re.compile(r"^[0-9]{1,12}-[0-9a-f]{8}\Z")
PLATFORMS = ("macos", "linux", "windows", "other")
FAIL_CODES = ("profile_blocked", "write_failed")

# ---- plain sentences (the page shows them as they are) -------------------------------------------------------------
THE_SOURCE = "the computer with the Grok app"
MESSAGES = {
    "bad_code": ("That is not a link code. A link code looks like ABCD-EFGH-JKMN.",
                 "Copy it from the Twins page on the computer with the Grok app."),
    "code_wrong": ("That link code is not the one showing on the other computer.",
                   "Check it, or make a new code there."),
    "code_expired": ("That link code has run out. A code lasts 10 minutes.",
                     "Make a new code on the computer with the Grok app."),
    "code_used": ("That link code was already used. Each code links one computer.",
                  "Make a new code on the computer with the Grok app."),
    "too_many_tries": ("Too many wrong codes. That code no longer works.",
                       "Make a new code on the computer with the Grok app."),
    "unreachable": ("Hermes could not reach the computer with the Grok app.",
                    "Check that it is on, that Hermes is open there with sharing switched on, and that both "
                    "computers are on the same network or on Tailscale."),
    "not_sharing": ("Sharing is switched off on {name}.",
                    "Switch on \"Share my twins with my other computers\" there, then try again."),
    "own_code": ("That code is from this computer.", "Type it on the computer you want to link."),
    "sharing_here": ("This computer shares its own twins, so it cannot follow another one.",
                     "Switch sharing off here first."),
    "already_linked": ("This computer already follows {name}.", "Unlink it first."),
    "is_follower": ("This computer follows {name}, so it cannot share twins itself.", "Unlink it first."),
    "no_twins": ("There are no twins to share yet.", "Twin a Grok bot first; then share."),
    "not_linked": ("This computer is not linked to another one.", ""),
    "version_mismatch": ("The two computers run versions of Grok Bot Twin that cannot work together.",
                         "Update Grok Bot Twin on the computer with the older version: Capabilities → Plugins → Update."),
    "update_here": ("This computer has an older Grok Bot Twin than {name}.",
                    "Update Grok Bot Twin on this computer: Capabilities → Plugins → Update. It then carries on by "
                    "itself."),
    "update_there": ("{name} has an older Grok Bot Twin than this computer.",
                     "Update Grok Bot Twin on {name}: Capabilities → Plugins → Update. This computer then carries "
                     "on by itself."),
    "fleet_full": ("{name} already shares with 16 computers.", "Unlink one there first."),
    "no_network": ("Hermes found no home network or Tailscale address to share on.",
                   "Connect this computer to your network or start Tailscale; sharing starts by itself."),
    "port_busy": ("Another program on this computer is in the way of sharing.",
                  # (never "restart this computer": sharing is tried again at every background check)
                  "Sharing tries again by itself every minute. If it stays this way, change \"Port for "
                  "sharing with your other computers\" from {port} to another number in the plugin's settings, on "
                  "every computer."),
    # (the same problem when the program could be named: its code on the wire stays port_busy)
    "port_busy_named": ("{program} on this computer is in the way of sharing.",
                        "Quit {program}; sharing then starts by itself."),
    "unknown_machine": ("That computer is not linked.", ""),
    "bad_name": ("A computer's name is 1 to 40 letters, digits, spaces, dots, dashes or underscores.", ""),
    "unknown_kept": ("That Hermes bot is no longer kept.", ""),
    "profile_blocked": ("Hermes could not make the Hermes bot for this twin on this computer.", ""),
    "write_failed": ("Hermes could not save this twin on this computer.",
                     "Check that the disk is not full, then press Try again."),
    "source_offline": ("{name} has not answered since {time}.",
                       # (said in so many words that they still work: "stay as they are" left a newcomer unsure)
                       "Your twins here still work, and catch up by themselves when {name} is back."),
    "sharing_off": ("Sharing was switched off on {name}.",
                    "Your twins here stay as they are. Switch sharing on there again, or unlink this computer."),
    # (worded as a condition: the answers behind it are unsigned, so anything at that address could send them)
    "source_forgot": ("{name} no longer answers as a computer linked to this one.",
                      "If Grok Bot Twin was set up again on {name}, unlink this computer and link it again with a "
                      "new link code. Your twins here stay as they are until then."),
    "source_removed": ("Grok Bot Twin was removed on {name}, so your twins here no longer follow it.",
                       "They stay as they are. If it is set up again on {name}, unlink this computer and link it "
                       "again with a new link code."),
}


def message(code: str, **fmt: Any) -> dict:
    """{"code", "message", "hint"?} for one of the fleet's own codes, in the contract's words."""
    msg, hint = MESSAGES.get(code, ("Something went wrong with sharing between your computers.", "Try again."))
    fmt.setdefault("name", THE_SOURCE)
    fmt.setdefault("port", net.DEFAULT_PORT)
    fmt.setdefault("time", "a while ago")
    fmt.setdefault("program", "Another program")
    if code == "port_busy" and fmt.get("program") not in (None, "", "Another program"):
        msg, hint = MESSAGES["port_busy_named"]
    out = {"code": code, "message": msg.format(**fmt)}
    if hint:
        out["hint"] = hint.format(**fmt)
    return out


class FleetError(EngineError):
    """A problem the local API answers as {"error": {...}} (HTTP 200), in plain words."""

    def __init__(self, code: str, hint: Optional[str] = None, **fmt: Any) -> None:
        m = message(code, **fmt)
        super().__init__(code, m["message"], hint if hint is not None else m.get("hint", ""))


# ---- this machine --------------------------------------------------------------------------------------------------
def platform_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform in ("win32", "cygwin"):
        return "windows"
    return "other"


def default_machine_name() -> str:
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    # the computer's own name, tidied: "example-pc_box.local" reads "example pc box" (the owner can give it another one)
    name = re.sub(r"[-_]+", " ", host.split(".")[0])
    # A Mac names itself after its owner ("Alexs-MacBook-Pro"). The name goes to the other computers and into
    # every twin's prompt file, so the owner's first name is left out: "MacBook Pro".
    name = _OWNERS_MAC.sub("", name)
    return fp.machine_name(name, "This computer")


_OWNERS_MAC = re.compile(r"^[A-Za-z]+s (?=(?:Mac|MacBook|iMac)(?: |$))")


def _clock(value: Any) -> str:
    """HH:MM in this machine's local time, for a sentence."""
    dt = parse_iso(value)
    return dt.astimezone().strftime("%H:%M") if dt else "a while ago"


# ---- state file ------------------------------------------------------------------------------------------------------
_lock_depth = 0


@contextlib.contextmanager
def locked():
    """Hold this while reading, changing and saving the fleet state. It is the store's lock (a thread lock, plus a
    file lock between processes) and, on Windows, a lock file of the fleet's own as well, so that two Hermes
    processes there (the app and a background follower, say) cannot overwrite each other's change. A lock that
    cannot be had in time raises OSError before anything is read or written; nothing goes on without it."""
    global _lock_depth
    with store.locked():
        fh = fio.lock_file(store.central_dir() / "fleet.lock") if _lock_depth == 0 else None
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
            fio.unlock_file(fh)


def state_path() -> Path:
    return store.central_dir() / STATE_FILE


def _blank() -> dict:
    return {"machine": {"id": "", "name": ""}, "share": None, "link": None, "kept": [], "last_unlink": None,
            "released": {}, "goodbye": None}


def _blank_share() -> dict:
    return {"on": False, "since": None, "address": None, "code": None, "old_codes": [], "followers": {}, "gone": {},
            "leaving": False,  # True from "Remove Grok Bot Twin" until the data is gone: the last answers say so
            "list": {"counter": 0, "rev": "0-00000000", "digest": "", "twins": {}, "removed": {}}, "instance": None}


def load() -> dict:
    """The fleet state, always with every top-level key. A file that cannot be read or is not an object counts
    as empty. Nothing is written here."""
    raw = fio.read_json(state_path(), None)
    st = _blank()
    if isinstance(raw, dict):
        m = raw.get("machine")
        if isinstance(m, dict):
            if isinstance(m.get("id"), str) and MACHINE_ID_RE.match(m["id"]):
                st["machine"]["id"] = m["id"]
            if fp.valid_machine_name(m.get("name")):
                st["machine"]["name"] = m["name"]
        if isinstance(raw.get("share"), dict):
            sh = _blank_share()
            sh.update({k: v for k, v in raw["share"].items() if k in sh})
            for key, kind in (("followers", dict), ("old_codes", list), ("list", dict), ("gone", dict)):
                if not isinstance(sh[key], kind):
                    sh[key] = _blank_share()[key]
            for key, kind in (("twins", dict), ("removed", dict)):
                if not isinstance(sh["list"].get(key), kind):
                    sh["list"][key] = {}
            st["share"] = sh
        if isinstance(raw.get("link"), dict) and isinstance(raw["link"].get("source"), dict):
            ln = dict(raw["link"])
            if not isinstance(ln.get("twins"), dict):
                ln["twins"] = {}
            if not isinstance(ln.get("fleet"), list):
                ln["fleet"] = []
            st["link"] = ln
        if isinstance(raw.get("kept"), list):
            st["kept"] = [k for k in raw["kept"] if isinstance(k, dict) and isinstance(k.get("rid"), str)
                          and RID_RE.match(k["rid"])]  # (never cut short: an unrecorded folder is an orphan)
        if isinstance(raw.get("last_unlink"), dict):
            st["last_unlink"] = raw["last_unlink"]
        if isinstance(raw.get("goodbye"), dict):  # an unlink the other computer has not heard of yet
            st["goodbye"] = raw["goodbye"]
        if isinstance(raw.get("released"), dict):
            st["released"] = {tid: r for tid, r in raw["released"].items()
                              if isinstance(tid, str) and REF_RE.match(tid) and isinstance(r, dict)}
    return st


def save(st: dict) -> None:
    fio.write_json(state_path(), st)


def exists() -> bool:
    return os.path.lexists(store.hermes_root() / "plugin-data" / "grok-bot-twin" / STATE_FILE)


def ensure_machine(st: dict) -> bool:
    """Give this install its machine id (once). True when the state changed."""
    if st["machine"]["id"]:
        return False
    st["machine"]["id"] = "m-" + secrets.token_hex(6)
    return True


def this_machine(st: dict) -> dict:
    name = st["machine"]["name"] or fp.machine_name(config.get("machine_name"), "") or default_machine_name()
    return {"id": st["machine"]["id"], "name": name, "platform": platform_name()}


def role(st: dict) -> str:
    if st.get("link"):
        return "follower"
    sh = st.get("share")
    if sh and (sh.get("on") or sh.get("followers")):
        return "source"
    return "none"


# ---- cleaning what arrives from another machine ----------------------------------------------------------------------
def clean_machine(raw: Any, role_: Optional[str] = None) -> Optional[dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not MACHINE_ID_RE.match(raw["id"]):
        return None
    out = {"id": raw["id"], "name": fp.machine_name(raw.get("name")),
           "platform": raw.get("platform") if raw.get("platform") in PLATFORMS else "other"}
    if role_ is not None:
        out["role"] = role_
    return out


def clean_text(value: Any, cap: int, one_line: bool) -> str:
    """Name or description as a machine keeps it: the plugin's text filter (control characters dropped,
    password-like text masked), capped."""
    if not isinstance(value, str):
        return ""
    return safe.clean_name(value, cap) if one_line else hs.shown(value, cap)


def _iso_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and len(value) <= 40 and parse_iso(value) else None


# ---- the source's list -----------------------------------------------------------------------------------------------
def _registry(svc: Any) -> Optional[dict]:
    """The real twins of this machine by pair id, or None when the registry file cannot be told (missing or
    damaged): then nothing is added to or removed from the list."""
    raw = fio.read_json(store.central_dir() / "pairs.json", None)
    pairs = raw.get("pairs") if isinstance(raw, dict) else None
    if not isinstance(pairs, list):
        return None
    demo_ids = set(svc.demo.pair_ids())
    return {p["pair_id"]: p for p in pairs
            if isinstance(p, dict) and valid_id(p.get("pair_id")) and p["pair_id"] not in demo_ids}


def real_twin_count(svc: Any) -> int:
    return len(_registry(svc) or {})


def fleet_members(st: dict) -> list:
    """The machines of the fleet as the source tells them: itself first, then the linked machines by name."""
    sh = st.get("share") or {}
    me = this_machine(st)
    out = [{"id": me["id"], "name": me["name"], "platform": me["platform"], "role": "source"}]
    for f in sorted((sh.get("followers") or {}).values(), key=lambda f: (str(f.get("name")).lower(), str(f.get("id")))):
        m = clean_machine(f, "follower")
        if m:
            out.append(m)
    return out[:MAX_MACHINES + 1]


def wire_list(st: dict) -> dict:
    """What a linked machine is sent. THE ALLOWLIST: every value is copied by name from the cleaned list; nothing
    else of this machine's state can ride along."""
    sh = st["share"]
    me = this_machine(st)
    lst = sh["list"]
    twins = [{"id": tid, "name": t["name"], "description": t["description"], "version": t["version"],
              "updated_at": t["updated_at"]} for tid, t in sorted(lst["twins"].items())][:MAX_TWINS]
    removed = [{"id": tid, "removed_at": at} for tid, at in sorted(lst["removed"].items())][:MAX_REMOVED]
    return {"schema": SCHEMA, "proto": PROTO, "changed": True, "sharing": True, "rev": lst["rev"],
            "generated_at": lst.get("generated_at") or now_iso(),
            "source": {"id": me["id"], "name": me["name"], "platform": me["platform"]},
            "twins": twins, "removed": removed, "fleet": fleet_members(st)}


def _digest_of(st: dict) -> str:
    w = wire_list(st)
    body = {k: w[k] for k in ("source", "twins", "removed", "fleet")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def rebuild_list(svc: Any, st: dict) -> bool:
    """Bring the source's list in line with the registry of real twins. True when the list (or the fleet's
    members) changed, in which case it has a new rev. Called with the store lock held."""
    sh = st["share"]
    lst = sh["list"]
    reg = _registry(svc)
    now = now_iso()
    if reg is not None:
        seen = set()
        for pid, entry in list(reg.items())[:MAX_TWINS]:
            g = entry.get("grok") if isinstance(entry.get("grok"), dict) else {}
            tid = pair_ref(pid)
            seen.add(tid)
            name = clean_text(g.get("name"), NAME_CAP, True) or "Twin"
            desc = clean_text(g.get("description"), DESCRIPTION_CAP, False)
            prof = (entry.get("hermes") or {}).get("profile") if isinstance(entry.get("hermes"), dict) else None
            old = lst["twins"].get(tid)
            if not isinstance(old, dict):
                lst["twins"][tid] = {"name": name, "description": desc, "version": 1, "updated_at": now,
                                     "pair_id": pid, "profile": prof if valid_id(prof) else None}
            else:
                if old.get("name") != name or old.get("description") != desc:
                    old.update(name=name, description=desc, version=int(old.get("version") or 1) + 1, updated_at=now)
                old["pair_id"] = pid
                old["profile"] = prof if valid_id(prof) else None
            lst["removed"].pop(tid, None)  # twinned again: it is back
        for tid in [t for t in lst["twins"] if t not in seen]:
            del lst["twins"][tid]
            lst["removed"][tid] = now
    cut = iso(now_dt() - timedelta(days=REMOVED_DAYS))
    for tid in [t for t, at in lst["removed"].items() if not isinstance(at, str) or at < cut]:
        del lst["removed"][tid]
    digest = _digest_of(st)
    if digest == lst.get("digest") and REV_RE.match(str(lst.get("rev"))):
        return False
    lst["counter"] = int(lst.get("counter") or 0) + 1
    lst["digest"] = digest
    lst["rev"] = f"{lst['counter']}-{digest[:8]}"
    lst["generated_at"] = now
    return True


# ---- keys and signatures (both sides; HMAC-SHA256, scrypt and whole-number powers from the standard library) ---------
# Nothing secret is ever sent, and a link code is short (24 random bits), so linking is built so that the code
# cannot be found by trying them all against what travelled. It is the SPEKE exchange (ISO/IEC 11770-4), which
# needs nothing but powers of whole numbers:
# - Both sides turn the code into a number g (a slow, memory-hungry scrypt key, stretched and squared into the
#   large subgroup of RFC 3526's 2048-bit group 14). Each picks a secret x and sends g^x. Both arrive at the same
#   g^(xy); nobody else can, and what travelled says nothing about the code: there is no value in it that a
#   guessed code could be checked against.
# - Then each proves to the other that it arrived at the same secret (a keyed digest over everything both sent).
#   Someone who does not know the code, whether it poses as the new machine, as the source, or sits in between,
#   gets exactly ONE guess per attempt, and the source counts the attempts: five, then the code is void.
# - The pass is made from that secret. A recording of the linking is worth nothing, also to someone who later
#   learns the code.
# Afterwards every request is signed with a key made from that pass, and so is every answer, bound to the
# request's nonce: a machine that merely sits at the source's address learns nothing and cannot say "you are
# unlinked" or hand over a list. (This authenticates; it does not hide. Names and descriptions travel readable
# unless Tailscale carries them.)
_DH_P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
_DH_G = 2
_DH_HEX = 512  # a public value is at most this many hex digits
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}  # 16 MB; the same on every machine, part of the wire format


def _hex_ok(value: Any, n: int) -> bool:
    return isinstance(value, str) and len(value) == n and all(c in "0123456789abcdef" for c in value)


def new_nonce() -> str:
    return secrets.token_hex(16)


MAX_COUNT = 2 ** 62  # the numbers a linked computer counts its calls with stay below this


def count_of(value: Any) -> Optional[int]:
    """The call number a linked computer sent, or None for anything that is not one."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < MAX_COUNT else None


@functools.lru_cache(maxsize=32)
def link_key(raw_code: str) -> bytes:
    """The key a link code stands for. Slow on purpose (see above); the source works it out once, when it makes
    the code, and keeps it; the new machine once, when the owner types the code."""
    return hashlib.scrypt(raw_code.encode("ascii"), salt=b"grok-bot-twin link 3", **_SCRYPT)


def sign(key: bytes, *parts: Any) -> str:
    return hmac.new(key, "\n".join(str(p) for p in parts).encode("utf-8"), hashlib.sha256).hexdigest()


def link_base(key: bytes) -> int:
    """The number both sides raise to their secret powers, made from the link code's key: only someone who knows
    the code starts from the same number. (Stretched to the size of the group and squared, which puts it in the
    large subgroup; nobody knows a power that leads from one code's number to another's.)"""
    h = int.from_bytes(hashlib.shake_256(b"grok-bot-twin base\n" + key).digest(320), "big") % _DH_P
    g = pow(h, 2, _DH_P)
    return g if g > 1 else 4


def dh_new(base: int = _DH_G) -> tuple:
    """(this side's secret number, its public value as hex) for one linking; the secret is never stored."""
    x = secrets.randbits(256) | (1 << 255)
    return x, format(pow(base, x, _DH_P), "x")


def dh_value(public: Any) -> Optional[int]:
    """The other side's public value as a number; None when it is not one this exchange may use (not hex, too
    long, or one of the values that would make the shared secret guessable: 0, 1, p-1 and above)."""
    if not isinstance(public, str) or not 1 <= len(public) <= _DH_HEX or any(c not in "0123456789abcdef" for c in public):
        return None
    value = int(public, 16)
    return value if 2 <= value <= _DH_P - 2 else None


def dh_shared(secret: int, public: Any) -> Optional[str]:
    """What both sides arrive at and nobody listening can: a digest of the shared secret. None when the other
    side's value is not usable."""
    value = dh_value(public)
    if value is None:
        return None
    return hashlib.sha256(b"grok-bot-twin dh\n" + pow(value, secret, _DH_P).to_bytes(256, "big")).hexdigest()


def link_keys(shared: str, nonce: str, source_nonce: str, machine_id: str, source_id: str, public: str,
              source_public: str) -> tuple:
    """(the source's proof, the new machine's proof, the pass), all made from the shared secret over everything
    the two sides sent. The proofs differ, so one cannot be sent back as the other."""
    key = bytes.fromhex(shared)
    said = (nonce, source_nonce, machine_id, source_id, public, source_public)
    return sign(key, "source", *said), sign(key, "machine", *said), sign(key, "pass", *said)


def share_key(pass_: str) -> bytes:
    """The key that signs one linked machine's requests and the answers to them. The source keeps this (as hex)
    and never the pass it was made from."""
    return hashlib.sha256(b"grok-bot-twin share\n" + pass_.encode("utf-8")).digest()


def body_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_mac(key: bytes, path: str, nonce: str, body: bytes) -> str:
    return sign(key, "req", path, nonce, body_digest(body))


def answer_mac(key: bytes, nonce: str, status: int, body: bytes) -> str:
    return sign(key, "ans", nonce, status, body_digest(body))


def same(a: Any, b: Any) -> bool:
    return isinstance(a, str) and isinstance(b, str) and hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---- link codes (source) -----------------------------------------------------------------------------------------------
def new_code(st: dict, address: str) -> dict:
    """Make the one valid link code (an unused older one stops working). Returns the stored record."""
    sh = st["share"]
    _retire_code(sh, "replaced")
    raw = net.make_code(address)
    sh["code"] = {"code": raw, "key": link_key(raw).hex(),
                  "expires_at": iso(now_dt() + timedelta(seconds=net.CODE_TTL_S)), "tries": 0}
    return sh["code"]


def _retire_code(sh: dict, why: str, by: Optional[str] = None) -> None:
    code = sh.get("code")
    if isinstance(code, dict) and _hex_ok(code.get("key"), 64):
        old = {"key": code["key"], "state": why, "at": now_iso()}
        if by:  # which computer used it, until when it would have lasted, its tries so far (_lost_answer_code)
            old.update(by=by, until=code.get("expires_at"), tries=int(code.get("tries") or 0))
        sh["old_codes"] = ([c for c in sh.get("old_codes", []) if isinstance(c, dict)] + [old])[-8:]
    sh["code"] = None


def _lost_answer_code(sh: dict, machine_id: Any) -> Optional[dict]:
    """The code a computer may use once more: it used the newest code itself, the code would still be good, no
    newer code was made, and that computer has never synced. That is a link whose last answer was lost on the way
    (the network dropped, or the computer was stopped before it could save the link): the owner was told "could
    not reach" and then, pressing Try again, "that code was already used".
    Every other computer still gets "already used", and the code's five tries are counted across both uses."""
    if sh.get("code") or not isinstance(machine_id, str):
        return None
    olds = [c for c in sh.get("old_codes", []) if isinstance(c, dict)]
    old = olds[-1] if olds else None
    if not old or old.get("state") != "used" or old.get("by") != machine_id or not _hex_ok(old.get("key"), 64):
        return None
    until = parse_iso(old.get("until"))
    f = (sh.get("followers") or {}).get(machine_id)
    if until is None or until <= now_dt() or not isinstance(f, dict) or (f.get("applied") or {}).get("rev"):
        return None
    return old


def _expire_code(sh: dict) -> None:
    code = sh.get("code")
    if isinstance(code, dict):
        exp = parse_iso(code.get("expires_at"))
        if exp is None or exp <= now_dt():
            _retire_code(sh, "expired")


def code_view(st: dict) -> Optional[dict]:
    sh = st.get("share") or {}
    code = sh.get("code")
    exp = parse_iso(code.get("expires_at")) if isinstance(code, dict) else None
    if exp is None or exp <= now_dt() or not net.normalize_code(code.get("code")):
        return None
    return {"code": net.pretty_code(code["code"]), "expires_at": code["expires_at"],
            "expires_in_s": max(0, int((exp - now_dt()).total_seconds()))}


def code_for_link(st: dict, machine_id: Any = None) -> tuple:
    """A machine starts the exchange: (None, the valid code's key), and one of the code's five tries is used; or
    (the wire error code, None) when there is no valid code (why: used, run out, or void after too many tries).
    Called with the store lock held; the caller saves the state."""
    sh = st["share"]
    _expire_code(sh)
    code = sh.get("code")
    if not isinstance(code, dict) or not _hex_ok(code.get("key"), 64):
        old = _lost_answer_code(sh, machine_id)
        if old is not None:
            old["tries"] = int(old.get("tries") or 0) + 1
            if old["tries"] > net.CODE_MAX_TRIES:
                old["state"] = "voided"
                return "too_many_tries", None
            return None, bytes.fromhex(old["key"])
        return no_code_reason(st), None
    code["tries"] = int(code.get("tries") or 0) + 1
    if code["tries"] > net.CODE_MAX_TRIES:
        _retire_code(sh, "voided")
        return "too_many_tries", None
    return None, bytes.fromhex(code["key"])


def no_code_reason(st: dict) -> str:
    """Why there is no valid link code right now, as a wire error code: the last one was used, ran out, or was
    made void by too many tries."""
    old = [c for c in st["share"].get("old_codes", []) if isinstance(c, dict)]
    return {"used": "code_used", "voided": "too_many_tries"}.get(old[-1].get("state") if old else None, "code_expired")


def code_failed(st: dict) -> str:
    """A machine's proof did not fit (it does not know the code). The wire error code; after the fifth try the
    code is void. Lock held by the caller, who saves."""
    sh = st["share"]
    code = sh.get("code")
    if isinstance(code, dict) and int(code.get("tries") or 0) >= net.CODE_MAX_TRIES:
        _retire_code(sh, "voided")
        return "too_many_tries"
    return "code_wrong"


def code_spent(st: dict, key_hex: str, machine_id: Any = None) -> bool:
    """A machine proved it knows the code whose key is `key_hex`: when that is still the valid code it is used
    up now (True); False when the owner made another code or it ran out in the meantime. Also True for the same
    computer finishing a link whose answer was lost (_lost_answer_code)."""
    sh = st["share"]
    _expire_code(sh)
    code = sh.get("code")
    if not isinstance(code, dict) or not same(code.get("key"), key_hex):
        old = _lost_answer_code(sh, machine_id)
        return old is not None and same(old.get("key"), key_hex)
    _retire_code(sh, "used", by=machine_id if isinstance(machine_id, str) else None)
    return True


def follower_key(st: dict, machine_id: Any) -> tuple:
    """(record, key) of a linked machine by its id; (None, key) for a machine that was unlinked here (so the
    source can still sign the answer that tells it so); (None, None) for any other id."""
    sh = st.get("share") or {}
    if not isinstance(machine_id, str) or not MACHINE_ID_RE.match(machine_id):
        return None, None
    f = (sh.get("followers") or {}).get(machine_id)
    if isinstance(f, dict) and _hex_ok(f.get("key"), 64):
        return f, bytes.fromhex(f["key"])
    g = (sh.get("gone") or {}).get(machine_id) if isinstance(sh.get("gone"), dict) else None
    if isinstance(g, dict) and _hex_ok(g.get("key"), 64):
        return None, bytes.fromhex(g["key"])
    return None, None


def forget_follower(st: dict, machine_id: str) -> Optional[dict]:
    """Take a linked machine off the source's list. Its key is remembered (90 days, at most 64 machines) only to
    sign "you are unlinked" when that machine next asks."""
    sh = st["share"]
    f = sh["followers"].pop(machine_id, None)
    if isinstance(f, dict) and _hex_ok(f.get("key"), 64):
        gone = sh.get("gone") if isinstance(sh.get("gone"), dict) else {}
        gone[machine_id] = {"key": f["key"], "at": now_iso()}
        cut = iso(now_dt() - timedelta(days=REMOVED_DAYS))
        gone = {m: g for m, g in gone.items() if isinstance(g, dict) and str(g.get("at")) >= cut}
        sh["gone"] = dict(sorted(gone.items(), key=lambda kv: str(kv[1].get("at")))[-64:])
    return f


# ---- what a follower does with a list ----------------------------------------------------------------------------------
def clean_list(raw: Any) -> Optional[dict]:
    """A list from the source reduced to the contract's fields, each checked, cleaned and capped again here.
    None when it is not a list answer at all."""
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or raw.get("changed") is not True:
        return None
    rev = raw.get("rev")
    src = clean_machine(raw.get("source"))
    if not isinstance(rev, str) or not REV_RE.match(rev) or src is None:
        return None
    twins, seen = [], set()
    for t in (raw.get("twins") if isinstance(raw.get("twins"), list) else [])[:MAX_TWINS]:
        if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not REF_RE.match(t["id"]) or t["id"] in seen:
            continue
        ver = t.get("version")
        if not isinstance(ver, int) or isinstance(ver, bool) or not 1 <= ver <= 10 ** 9:
            continue
        seen.add(t["id"])
        twins.append({"id": t["id"], "name": clean_text(t.get("name"), NAME_CAP, True) or "Twin",
                      "description": clean_text(t.get("description"), DESCRIPTION_CAP, False), "version": ver,
                      "updated_at": _iso_or_none(t.get("updated_at"))})
    removed = []
    for r in (raw.get("removed") if isinstance(raw.get("removed"), list) else [])[:MAX_REMOVED]:
        if isinstance(r, dict) and isinstance(r.get("id"), str) and REF_RE.match(r["id"]) and r["id"] not in seen:
            removed.append({"id": r["id"], "removed_at": _iso_or_none(r.get("removed_at")) or now_iso()})
    fleet = []
    for m in (raw.get("fleet") if isinstance(raw.get("fleet"), list) else [])[:MAX_MACHINES + 1]:
        cm = clean_machine(m, m.get("role") if isinstance(m, dict) and m.get("role") in ("source", "follower")
                           else "follower")
        if cm:
            fleet.append(cm)
    return {"rev": rev, "source": src, "twins": twins, "removed": removed, "fleet": fleet}


def _note_for(st: dict, twin_name: str) -> str:
    """The fleet note of one twin on this follower."""
    ln = st["link"]
    me = this_machine(st)
    src = ln["source"]
    others = [m["name"] for m in ln.get("fleet", []) if isinstance(m, dict) and m.get("id") != me["id"]]
    if not others:
        others = [src["name"]]
    return fp.fleet_note(me["name"], twin_name, others, src["name"], False)


def _kept_root() -> Path:
    return store.central_dir() / KEPT_DIR


def _keep(st: dict, tid: str, entry: dict, reason: str) -> bool:
    """Take one mirrored twin's Hermes bot away and remember where it is kept. True when it is gone from the
    profiles folder (also when it was not there any more)."""
    prof = entry.get("profile")
    if not isinstance(prof, str) or not fp.PROFILE_ID_RE.match(prof) or prof == "default":
        return True
    rid = "r-" + secrets.token_hex(6)
    home = fp.profile_dir(prof)
    if fio.is_real_dir(home) and not fp.is_mirrored(home):
        if entry.get("making") and fp.half_made(home):
            # this machine's own unfinished work (it was cut short while making this twin): nothing of the
            # owner's is in it, so it goes rather than stay behind as a folder nobody knows about
            with contextlib.suppress(OSError):
                fio.remove_dir(home, fp.profiles_root())
        return True  # a Hermes bot of that name that is not this twin's (the owner made it since): left alone
    try:
        moved = fp.take_away(prof, _kept_root() / rid)
    except (fp.ProfileBlocked, OSError):
        return False
    if moved:  # (only once it is out of the profiles folder is the plugin switched off in it)
        fp.option_a(_kept_root() / rid / "profile", False)
        fp.mark_moved(prof)  # Hermes' own note that this bot is gone: not listed, not made again meanwhile
    if moved:
        st["kept"].append({"rid": rid, "id": tid, "name": entry.get("name") or "Twin", "profile": prof,
                           "removed_at": now_iso(), "kept_until": iso(now_dt() + timedelta(days=KEEP_DAYS)),
                           "reason": reason})
        # never more than MAX_KEPT: a list that keeps adding and removing twins cannot fill this computer's disk
        # or make the state file too large to read (which would lose the link itself)
        while len(st["kept"]) > MAX_KEPT:
            oldest = st["kept"].pop(0)
            fio.remove_dir(_kept_root() / oldest["rid"], _kept_root())
            _forget_mark(st, oldest)
    return True


def _forget_mark(st: dict, gone: dict) -> None:
    """A kept bot is no longer kept (its 30 days are over, or it made room for a newer one): the plugin's note at
    its old name goes too, unless another kept bot still has that name."""
    prof = gone.get("profile")
    if isinstance(prof, str) and not any(k is not gone and k.get("profile") == prof for k in st["kept"]):
        fp.clear_moved(prof)


def forget_marks(st: dict) -> None:
    """Remove Grok Bot Twin: every note the plugin left at a kept bot's old name goes with its data."""
    for k in st.get("kept") or []:
        if isinstance(k, dict):
            fp.clear_moved(k.get("profile"))


def prune_kept(st: dict) -> bool:
    """Delete kept Hermes bots older than 30 days. True when the state changed."""
    now = now_dt()
    keep, changed = [], False
    st_was = list(st["kept"])
    for k in st["kept"]:
        until = parse_iso(k.get("kept_until"))
        if until is not None and until > now:
            keep.append(k)
            continue
        fio.remove_dir(_kept_root() / k["rid"], _kept_root())
        changed = True
    st["kept"] = keep
    for k in st_was:
        if k not in keep:
            _forget_mark(st, k)
    for k in keep:
        fp.heal_mark(k.get("profile"))
    # a folder in the kept place that no record names (a save that failed after the move, a crash) gets the same
    # 30 days, counted from when it was put there; it is never deleted sooner
    known = {k["rid"] for k in keep}
    loose = []
    try:
        for entry in os.scandir(_kept_root()):
            if RID_RE.match(entry.name) and entry.name not in known and entry.is_dir(follow_symlinks=False):
                put_there = entry.stat(follow_symlinks=False).st_mtime
                if put_there < time.time() - (KEEP_DAYS * 86400 + 3600):
                    fio.remove_dir(Path(entry.path), _kept_root())
                else:
                    loose.append((put_there, entry.name))
        # ...unless there are more of them than MAX_LOOSE. A leftover folder moved aside when a twin comes back
        # (fleet_profiles.set_aside) is such a folder, and a list that takes a twin away and brings it back over
        # and over makes one each time: like the kept bots themselves (MAX_KEPT), only the newest stay.
        for _put_there, name in sorted(loose)[:-MAX_LOOSE]:
            fio.remove_dir(_kept_root() / name, _kept_root())
    except OSError:
        pass
    return changed


def _bring_back_kept(st: dict, tid: str, name: Any = None, taken: Any = ()) -> Optional[str]:
    """A twin that was taken away comes back: its kept Hermes bot (chats included) is put back. Returns the
    profile name, or None when nothing is kept for it. `name`: the twin's name, for the folder it gets when its
    old one is taken by a bot of the owner's."""
    for k in reversed(st["kept"]):
        if k.get("id") != tid:
            continue
        prof = k.get("profile") if isinstance(k.get("profile"), str) else "twin"
        if _moved_back_already(st, k, prof):
            st["kept"] = [x for x in st["kept"] if x is not k]
            return prof
        # A folder at its old name that holds nothing of the owner's (a running Hermes makes one again right
        # after the bot was moved away) is moved aside into the kept place, so the bot is back under its own
        # name. Only a folder with something of the owner's in it sends the twin to a second name
        # (`taken`: names this pass has already given to other twins).
        if not fp.set_aside(prof, _kept_root()):
            fp.heal_mark(prof)  # (a bot of the owner's has that name now: nothing of the plugin's hides it)
            # the name it gets instead is freed the same way when a leftover folder sits there
            prof = fp.unique_profile(name if isinstance(name, str) and name.strip() else prof, taken, kept=True)
            fp.set_aside(prof, _kept_root())
        try:
            fp.bring_back(_kept_root() / k["rid"], prof)
        except fp.ProfileBlocked:
            return None
        st["kept"] = [x for x in st["kept"] if x is not k]
        return prof
    return None


def _moved_back_already(st: dict, k: dict, prof: str) -> bool:
    """A kept Hermes bot that was already moved back when this process was cut short (killed, the computer went
    off) before it could note so: its place in the kept folder is empty, and a twin's bot that no twin here is
    recorded with sits under its old name. It is that twin's; without this it was passed over and the twin was
    made a second time as "name-2"."""
    if not fp.PROFILE_ID_RE.match(prof) or prof == "default" or fio.is_real_dir(_kept_root() / k["rid"] / "profile"):
        return False
    home = fp.profiles_root() / prof
    held = (st.get("link") or {}).get("twins") or {}
    return fio.is_real_dir(home) and fp.is_mirrored(home) and not any(
        isinstance(e, dict) and e.get("profile") == prof for e in held.values())


def _adopt_released(st: dict, tid: str) -> Optional[str]:
    """A kept bot the owner brought back as an ordinary Hermes bot ("Keep this bot") is this twin's again when
    the twin returns: same folder, same chats. Returns the profile name, or None when there is no such bot (any
    more) or it has since become something else."""
    rec = st.get("released", {}).pop(tid, None)
    prof = rec.get("profile") if isinstance(rec, dict) else None
    if not isinstance(prof, str) or not fp.PROFILE_ID_RE.match(prof) or prof == "default":
        return None
    home = fp.profiles_root() / prof
    if not fio.is_real_dir(home) or fp.is_mirrored(home):
        return None
    # only the very bot that was released: its prompt file still holds, word for word, what it was left with. A
    # bot that merely has the same name (the owner deleted the released one and made another) is the owner's and
    # is not touched; the twin then gets a folder of its own
    if fp.released_at(fio.read_text(home / "SOUL.md", hs.SOUL_CAP), rec.get("text")) is None:
        return None
    try:
        fp.reclaim(home, rec.get("text") if isinstance(rec.get("text"), str) else "")
    except (fp.ProfileBlocked, OSError):
        return None
    return prof


def clash_note(profile: str, name: str) -> Optional[str]:
    """One sentence for a twin that had to take another folder name than its own, because a Hermes bot of that
    name was already on this computer. Said only while that is true: not when what stood in the way held
    nothing of the owner's (a folder Hermes Desktop had made again by itself) or is gone since."""
    shown = fp.shown_name(profile, name)
    plain = " ".join(name.split()) if isinstance(name, str) else ""
    if not plain or shown == plain or not fp.bot_in_the_way(plain):
        return None
    return f"A Hermes bot called {plain} was already here, so this twin is {shown}. The other one was left as it is."


def _own_entry(held: dict, tid: str, taken: Optional[set] = None) -> Optional[dict]:
    """The record of a listed twin when its Hermes bot here can be worked on: there and this twin's, or noted as
    "being made" (see _plan_new). None when the twin has to get a bot: it has none yet, its bot was deleted on
    this machine, or a bot of that name stands there that is not this twin's (made by the owner since)."""
    entry = held.get(tid) if isinstance(held.get(tid), dict) else None
    if entry and isinstance(entry.get("profile"), str) and fp.PROFILE_ID_RE.match(entry["profile"]):
        home = fp.profiles_root() / entry["profile"]
        if entry.get("making"):
            shared = len([e for e in held.values() if isinstance(e, dict) and e.get("profile") == entry["profile"]]) > 1
            if shared or (os.path.lexists(home) and not (fio.is_real_dir(home)
                                                         and (fp.is_mirrored(home) or fp.half_made(home)))):
                return None  # something else took the name before this machine got to it: left alone
        elif not os.path.lexists(home):
            if taken is not None:
                taken.discard(entry["profile"])
            return None  # its Hermes bot was deleted on this machine: it is made anew
        elif fio.is_real_dir(home) and not fp.is_mirrored(home):
            # Not this twin's any more. A bot of that name the owner made since is left alone (the twin gets a
            # name of its own). A folder with nothing of the owner's in it (the twin's bot was deleted here and
            # a running Hermes made the folder again) is moved aside, so the twin is made again where it was.
            if fp.set_aside(entry["profile"], _kept_root()) and taken is not None:
                taken.discard(entry["profile"])
            return None
    return entry


def _plan_new(st: dict, lst: dict, taken: set) -> bool:
    """Before the first folder is made: every listed twin that will get a newly made Hermes bot is recorded with
    the name its folder will have and the mark "making". The caller saves that at once. A process that is then
    cut short part-way (killed, the helper restarted, the computer went off) finds on its next sync which
    folders are its own unfinished work and finishes them. Without the record it took them for the owner's bots,
    left them where they were and made every twin again as "name-2" (a follower killed during its first
    sync left every folder twice). True when something was planned."""
    held: dict = st["link"]["twins"]
    listed = {t["id"] for t in lst["twins"]}
    back = [k for k in st["kept"] if k.get("id") in listed] + [r for tid, r in (st.get("released") or {}).items()
                                                                if tid in listed and isinstance(r, dict)]
    comes_back = {k.get("id") for k in st["kept"]} | set(st.get("released") or {})
    taken.update(b["profile"] for b in back if isinstance(b.get("profile"), str))  # their old names are theirs
    planned = False
    for t in lst["twins"]:
        tid = t["id"]
        entry = _own_entry(held, tid, taken)
        if entry is not None and entry.get("profile"):
            continue
        if tid in comes_back or (tid not in held and len(held) >= MAX_TWINS):
            continue  # a kept bot is put back instead (a rename, done in a moment); or the list is full
        prof = fp.unique_profile(t["name"], taken)
        taken.add(prof)
        held[tid] = {"name": t["name"], "description": t["description"], "version": t["version"],
                     "updated_at": t["updated_at"], "profile": prof, "synced_at": None, "state": "in_sync",
                     "error": None, "making": True}
        planned = True
    return planned


def apply_list(st: dict, lst: dict, checkpoint: Optional[Callable[[dict], None]] = None) -> dict:
    """Make this follower's Hermes bots match a cleaned list. Idempotent: applying the same list again writes
    no file. Returns counts {"added", "updated", "removed", "failed"}. Called with the store lock held; the
    caller saves the state. `checkpoint` (the caller's save) is called once, before any folder is made, when
    this pass will make new Hermes bots (see _plan_new)."""
    ln = st["link"]
    ln["source"] = {**ln["source"], **{k: lst["source"][k] for k in ("name", "platform")}}  # its id never changes
    ln["fleet"] = lst["fleet"]
    held: dict = ln["twins"]
    counts = {"added": 0, "updated": 0, "removed": 0, "failed": 0}
    now = now_iso()
    taken = {e.get("profile") for e in held.values() if isinstance(e, dict)}
    if _plan_new(st, lst, taken) and checkpoint is not None:
        checkpoint(st)
    for t in lst["twins"]:
        tid = t["id"]
        if isinstance(held.get(tid), dict):
            held[tid].pop("removing", None)  # it is listed (again): nothing about it is being taken away
        entry = _own_entry(held, tid, taken)
        if entry is None and isinstance(held.get(tid), dict) and held[tid].get("making"):
            taken.discard(held[tid].get("profile"))
            del held[tid]  # the name noted for it went to something else: it gets another one below
        if entry is None and tid not in held and len(held) >= MAX_TWINS:
            counts["failed"] += 1  # (never more Hermes bots than a list can hold, whatever arrives)
            continue
        note = _note_for(st, t["name"])
        prof = None
        try:
            if entry is not None and entry.get("making") and entry.get("profile"):
                prof = entry["profile"]
                fp.finish(prof, t["name"], t["description"], note)  # makes it, or finishes what was cut short
                held[tid] = {"name": t["name"], "description": t["description"], "version": t["version"],
                             "updated_at": t["updated_at"], "profile": prof, "synced_at": now, "state": "in_sync",
                             # (its first copy arrived now: the row says "arrived 14 sec ago", not "changed")
                             "error": None, "changed_at": now, "changed": "new"}
                said = clash_note(prof, t["name"])
                if said:
                    held[tid]["note"] = said
                counts["added"] += 1
            elif entry is None or not entry.get("profile"):
                prof = _bring_back_kept(st, tid, t["name"], taken) or _adopt_released(st, tid)
                if prof is None:
                    prof = fp.unique_profile(t["name"], taken)
                    fp.create(prof, t["name"], t["description"], note)
                else:
                    fp.update(prof, t["name"], t["description"], note)
                # said every time the twin sits under another name than its own, also when its kept bot came
                # back under one (the sentence used to go missing after linking again)
                said = clash_note(prof, t["name"])
                taken.add(prof)
                held[tid] = {"name": t["name"], "description": t["description"], "version": t["version"],
                             "updated_at": t["updated_at"], "profile": prof, "synced_at": now, "state": "in_sync",
                             # (its first copy arrived now: the row says "arrived 14 sec ago", not "changed")
                             "error": None, "changed_at": now, "changed": "new"}
                if said:
                    held[tid]["note"] = said
                counts["added"] += 1
            else:
                changed = fp.update(entry["profile"], t["name"], t["description"], note)
                newer = (entry.get("version") != t["version"] or entry.get("name") != t["name"]
                         or entry.get("description") != t["description"] or entry.get("state") != "in_sync")
                if changed or newer:
                    if entry.get("name") != t["name"] or entry.get("description") != t["description"]:
                        # when a new name or description last arrived here: the page's row can then say
                        # "description changed 20 sec ago" (nothing else on a row shows that it arrived)
                        entry["changed_at"] = now
                        entry["changed"] = "name" if entry.get("description") == t["description"] else "description"
                    entry.update(name=t["name"], description=t["description"], version=t["version"],
                                 updated_at=t["updated_at"], synced_at=now, state="in_sync", error=None)
                    counts["updated"] += 1
        except Exception as exc:  # ProfileBlocked says why; anything else (a full disk, a file that cannot be
            # written) is "could not save" for this one twin: the others are still made, and what was made is
            # recorded, so nothing is made twice at the next try
            if not isinstance(exc, fp.ProfileBlocked):
                log.debug("grok-bot-twin: one twin could not be made or updated here", exc_info=True)
                exc = fp.ProfileBlocked("", "write_failed")
            counts["failed"] += 1
            # a bot that was brought back from the kept place stays this twin's (the retry updates it); one that
            # could not be made at all left no folder, so the retry starts from the same free name
            base = entry or {"profile": prof if prof and fio.is_real_dir(fp.profiles_root() / prof) else None,
                             "synced_at": None}
            if base.get("making"):  # still to finish while its folder is there; else the name is simply free again
                there = fio.is_real_dir(fp.profiles_root() / str(base.get("profile")))
                base = {"profile": base["profile"] if there else None, "synced_at": None, **({"making": True} if there else {})}
            held[tid] = {**base, "name": t["name"], "description": t["description"], "version": t["version"],
                         "updated_at": t["updated_at"], "state": "error",
                         "error": {"code": exc.code, "hint": exc.hint}}
    listed = {t["id"] for t in lst["twins"]}
    for r in lst["removed"]:
        entry = held.get(r["id"])
        if isinstance(entry, dict):
            if _keep(st, r["id"], entry, "removed_on_source"):
                del held[r["id"]]
                counts["removed"] += 1
            else:
                counts["failed"] += 1
                entry.update(state="error", removing=True,
                             error={"code": "write_failed",
                                    "hint": "Close any chat with this twin, then press Try again."})
    for tid, entry in held.items():
        if tid not in listed and isinstance(entry, dict) and not entry.get("removing"):
            entry["state"] = "not_listed"
    ln["rev"] = lst["rev"]
    ln["last_sync_at"] = now
    return counts


def retry_failed(st: dict, checkpoint: Optional[Callable[[dict], None]] = None) -> bool:
    """Try again what could not be done last time (a twin that could not be made, a Hermes bot that could not be
    taken away), from what this machine already holds. True when something was tried. Lock held by the caller."""
    ln = st["link"]
    held = ln["twins"]
    if not ln.get("rev") or not any(isinstance(e, dict) and e.get("state") == "error" for e in held.values()):
        return False
    gone = [{"id": tid, "removed_at": now_iso()} for tid, e in held.items() if isinstance(e, dict) and e.get("removing")]
    twins = [{"id": tid, "name": e.get("name") or "Twin", "description": e.get("description") or "",
              "version": e.get("version") if isinstance(e.get("version"), int) and e["version"] > 0 else 1,
              "updated_at": e.get("updated_at")}
             for tid, e in held.items()
             if isinstance(e, dict) and not e.get("removing") and e.get("state") in ("in_sync", "error")]
    for e in held.values():
        if isinstance(e, dict):
            e.pop("removing", None)
    last = ln.get("last_sync_at")
    apply_list(st, {"rev": ln["rev"], "source": ln["source"], "fleet": ln.get("fleet", []), "removed": gone,
                    "twins": twins}, checkpoint)
    if any(isinstance(e, dict) and e.get("state") == "error" for e in held.values()):
        ln["last_sync_at"] = last  # still not whole: the time of the last full sync stands
    return True


def applied_report(st: dict) -> dict:
    """What this follower tells the source about itself: the rev it holds, when, and per twin id the version it
    made or the code of why it could not. Ids, numbers and fixed codes only."""
    ln = st["link"]
    twins, failed = {}, {}
    for tid, e in ln["twins"].items():
        if not isinstance(e, dict) or not REF_RE.match(tid):
            continue
        if e.get("state") == "error":
            code = (e.get("error") or {}).get("code")
            failed[tid] = code if code in FAIL_CODES else "write_failed"
        elif isinstance(e.get("version"), int) and not e.get("making"):  # (noted, not made yet: not reported)
            twins[tid] = e["version"]
    return {"rev": ln.get("rev"), "at": ln.get("last_sync_at"), "twins": twins, "failed": failed}


def drop_link(st: dict, by: str) -> int:
    """This machine stops following: every mirrored twin's Hermes bot is taken away (kept 30 days) and the link
    is forgotten. Returns how many twins left. Called with the store lock held.
    THE one place an unlink pauses routines first: a copy not SEEN paused keeps the link (RoutinesError
    "unlink_held", nothing changed), or it could run here and, after the goodbye or a forced move, elsewhere too."""
    from . import routines as _routines
    if not _routines.link_dropped(st):
        raise _routines.RoutinesError("unlink_held")
    ln = st["link"]
    n = 0
    reason = "unlinked" if by == "this" else "unlinked_by_source"
    for tid, entry in list(ln["twins"].items()):
        if isinstance(entry, dict):
            if not _keep(st, tid, entry, reason):
                # it could not be moved (Windows: a chat with it is open). It stays where it is as an ordinary
                # Hermes bot: nothing is lost, and nothing here follows the other computer any more
                untend_profile(entry.get("profile"), release=True)
            n += 1
    st["last_unlink"] = {"at": now_iso(), "by": by, "source_name": fp.machine_name(ln["source"].get("name"))}
    st["link"] = None
    try:  # "let my other computers ask this one" is off again: a later link starts with it off, as a first one does
        from . import tasks as _tasks
        _tasks.link_dropped()
    except Exception:
        log.debug("grok-bot-twin: could not switch requests off after the unlink", exc_info=True)
    return n


# ---- the fleet note and option A on the source ------------------------------------------------------------------------
def tend_source_twins(svc: Any, st: Optional[dict] = None, only: Optional[str] = None) -> int:
    """On the machine with the Grok app: every real twin's Hermes bot carries the fleet note and has the plugin
    switched on (option A), also with no other machine linked. Only existing Hermes bots are touched, never the
    default profile. Returns how many prompt files were written. Never raises."""
    try:
        st = st or load()
        reg = _registry(svc) or {}
        me = this_machine(st)
        others = [m["name"] for m in fleet_members(st)[1:]] if st.get("share") and st["machine"]["id"] else []
        written = 0
        for pid, entry in reg.items():
            if only is not None and pid != only:
                continue
            prof = (entry.get("hermes") or {}).get("profile") if isinstance(entry.get("hermes"), dict) else None
            if not isinstance(prof, str) or not fp.PROFILE_ID_RE.match(prof) or prof == "default":
                continue
            home = fp.profiles_root() / prof
            if not fio.is_real_dir(home):
                continue
            g = entry.get("grok") if isinstance(entry.get("grok"), dict) else {}
            note = fp.fleet_note(me["name"], clean_text(g.get("name"), NAME_CAP, True), others, me["name"], True)
            try:
                if fp.write_soul(home, fleet=note):
                    written += 1
                fp.option_a(home, True)
            except (fp.ProfileBlocked, OSError):
                continue
        return written
    except Exception:
        log.debug("grok-bot-twin: could not tend the twins' fleet notes", exc_info=True)
        return 0


def refresh_mirrored_copies(st: Optional[dict] = None) -> int:
    """On a computer that follows another: every mirrored twin's Hermes bot carries the plugin as it is installed
    now (after the plugin was updated the copies were left at the old version until the next change arrived, and
    Hermes could run its background work from an old one, two versions on one machine). Only a folder that carries
    the plugin's copy mark is replaced (fp.place_plugin leaves an install of the owner's alone), and only from the
    installed plugin, never from a twin's copy that happens to be the one running. Returns how many copies were
    looked at. Never raises."""
    try:
        src = fp.plugin_source()
        if src is None or fio.read_text(src / fp.COPY_MARKER, 200) is not None:
            return 0  # this process itself runs from a twin's copy: only the installed plugin hands its version on
        if st is None:
            if not exists():
                return 0
            st = load()
        ln = st.get("link") if isinstance(st.get("link"), dict) else None
        twins = ln.get("twins") if ln and isinstance(ln.get("twins"), dict) else {}
        looked = 0
        for t in twins.values():
            prof = t.get("profile") if isinstance(t, dict) else None
            if not isinstance(prof, str) or not fp.PROFILE_ID_RE.match(prof) or prof == "default":
                continue
            home = fp.profiles_root() / prof
            if not fio.is_real_dir(home):
                continue  # (a link where the twin's folder should be is never followed, here either)
            has_copy = fio.is_real_dir(home / "plugins" / fp.PLUGIN_ID)
            if not has_copy and not (fio.is_real_dir(home) and fp.is_mirrored(home) and fp.plugin_enabled(home)):
                continue
            try:
                fp.place_plugin(home)
                looked += 1
            except (fp.ProfileBlocked, OSError):
                continue
        return looked
    except Exception:
        log.debug("grok-bot-twin: could not refresh the twins' copies of the plugin", exc_info=True)
        return 0


def untend_profile(profile: Any, release: bool = False) -> None:
    """Twinning stopped for a bot on this machine: its Hermes bot stays, without the fleet note and without the
    plugin switched on in it. `release`: a mirrored twin becomes an ordinary Hermes bot (its name and description
    stay in the prompt file as plain text). Never raises."""
    try:
        if not isinstance(profile, str) or not fp.PROFILE_ID_RE.match(profile) or profile == "default":
            return
        home = fp.profiles_root() / profile
        if fio.is_real_dir(home):
            fp.write_soul(home, drop=not release, release=release)
            fp.option_a(home, False)
    except Exception:
        log.debug("grok-bot-twin: could not clear a twin's fleet note", exc_info=True)


# ---- waking whoever waits for a new list ------------------------------------------------------------------------------
class Hub:
    """Where the share server's waiting requests sleep. `bump` wakes them all; they then read the state again."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.seq = 0
        self.closing = False

    def bump(self) -> None:
        with self.cond:
            self.seq += 1
            self.cond.notify_all()

    def wait(self, seq: int, timeout: float) -> int:
        with self.cond:
            if self.seq == seq and not self.closing:
                self.cond.wait(timeout)
            return self.seq


hub = Hub()
_listeners: list = []  # callables run after the list or the fleet changed (the server pokes another process)


def on_change(fn: Callable[[], None]) -> None:
    if fn not in _listeners:
        _listeners.append(fn)


def announce() -> None:
    hub.bump()
    for fn in list(_listeners):
        try:
            fn()
        except Exception:
            log.debug("grok-bot-twin: fleet listener failed", exc_info=True)


def publish(svc: Any, force: bool = False) -> bool:
    """Source: rebuild the list from the registry; when it changed, save it, refresh the twins' own fleet notes
    and wake the waiting machines. Cheap when nothing changed (two small files read, nothing written)."""
    if not exists():
        return False
    with locked():
        st = load()
        if not st.get("share") or role(st) != "source":
            return False
        changed = rebuild_list(svc, st)
        if changed:
            save(st)
    if changed or force:
        tend_source_twins(svc, st)
        announce()
    return changed
