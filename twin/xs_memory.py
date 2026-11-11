"""A twin's memory, kept the same on the owner's own linked computers. Off for every twin until switched on.

What it is. A Hermes bot keeps two memory files in its own folder, `memories/MEMORY.md` (what it has learned) and
`memories/USER.md` (what it knows about its owner), each a list of notes. With the switch on for a twin on the
computer that shares AND on a linked computer, the notes of that twin are merged between the two: a note written
on either arrives on the other, a note removed on either leaves the other. Nothing else is read or sent.

How it travels. On the link the two computers already have, on one route of its own (`POST /v1/xs/memory`),
signed and counted like every other call. The body is a sealed box: encrypted and authenticated with keys made
from the link's key, which never travelled (see `fleet.py`, "keys and signatures"). Which twin, how many notes,
whether anything changed: all of it is inside the box. The only readable facts are that a memory call was made
and its size rounded up to a kilobyte. Nothing of a note is ever logged, put in an event or in an error.

How it merges. The memory file of the computer that shares is the meeting point. A linked computer sends what it
added and what it removed since it last agreed (it remembers that as digests, not text), and gets back the
whole list. So: a note added anywhere is kept; a note removed anywhere is removed; a note changed in two places
is kept in both versions and shown as a conflict for the owner to settle. Before this module rewrites a memory
file it saves a restore point (the file as it was), and "Forget on this computer" takes out again exactly the
notes that came from elsewhere.

What never travels: a note that looks like it holds a password, key, token or card number, a note that reads
like an instruction to the model or like one of this plugin's own notes, a note with invisible characters, and a
note too long to check. Such a note stays on the computer it is on and is counted, never quoted.

Standard library only. The cipher is SHAKE-256 as a keyed stream (key and a fresh 24-byte value in, as many
bytes out as the text is long) with HMAC-SHA256 over the result (encrypt, then authenticate): Python's own
library has no ready-made authenticated cipher, and the plugin installs nothing.
"""
from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets as _random
import threading
import time
import unicodedata
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from . import fleet, fleet_io as fio, fleet_profiles as fp, safe, secrets, store
from .engine import EngineError
from .model import iso, now_dt, now_iso, parse_iso

log = logging.getLogger(__name__)

ITEM = "memory"
ROUTE = "/v1/xs/memory"
XS = 1  # the version of this item's own format; both sides say the highest they speak, the lower one is used
FILES = {"memory": "MEMORY.md", "user": "USER.md"}  # Hermes' own names, in the bot's `memories` folder
DELIM = "\n§\n"  # Hermes' own separator between two notes
STATE_FILE = "xs-memory.json"
POINTS_DIR = "xs-memory-points"
TID_RE = re.compile(r"^t-[0-9a-f]{16}\Z")
POINT_RE = re.compile(r"^p-[0-9a-f]{12}\Z")
CONFLICT_RE = re.compile(r"^c-[0-9a-f]{12}\Z")
HASH_RE = re.compile(r"^[0-9a-f]{24}\Z")
EPOCH_RE = re.compile(r"^[0-9a-f]{16}\Z")
PAD = 1024  # a box is filled up to a multiple of this, so its length says little
ENTRY_CHARS = 4000  # a longer note is not shared
ENTRY_BYTES = 6000
FILE_BYTES = 1024 * 1024  # a larger memory file is left alone
MAX_ENTRIES = 400  # notes of one file that travel
BATCH_BYTES = 8000  # a request's box stays under the share server's 16 KiB for a whole request
ASK_BOX_CAP = 15 * 1024
ANSWER_BOX_CAP = 1536 * 1024
DEFAULT_LIMITS = {"memory": 2200, "user": 1375}  # Hermes' own defaults for the two files, in characters
MAX_POINTS = 30
POINT_DAYS = 30
MAX_FOREIGN = 4000  # digests remembered as "came from another computer", per file
BIG_REMOVAL = 5  # a linked computer may remove this many notes of a file in a day, or up to half of them
CHANGES_PER_DAY = 24  # questions from one linked computer that change a twin's memory here, in 24 hours
MAX_CONFLICTS = 20
TOMB_DAYS = 30
MAX_TOMBS = 400
POLL_S = 15.0  # a linked computer asks this often while a twin's memory is switched on (nothing is sent otherwise)
LOOK_S = 3.0  # and looks at its own memory files' dates this often, so a new note leaves within seconds
SHOWN_CHARS = 300


# ---- sentences ---------------------------------------------------------------------------------------------------------
SENTENCE = ("A twin's memory stays on the computer it was written on. Switch a twin on here and its memory is kept "
            "the same on your linked computers that switch it on too.")
WHAT = [
    "What follows: the notes in that twin's two memory files, MEMORY.md (what it has learned) and USER.md (what it "
    "knows about you). Nothing else.",
    "How: encrypted from one of your computers to the other with a key made when you linked them. It is not "
    "readable on your network, and it goes to no cloud.",
    "Never: a note that looks like it holds a password, key, code or card number, or that reads like an instruction "
    "rather than a note. It stays on the computer it is on. This check can miss things.",
    "Which computers: the linked computers you let take part on the computer with the Grok app, twin by twin, and "
    "only once the twin is switched on there too.",
    "A chat that is already open sees new notes when you start the next chat.",
]
HELD_WORDS = {"secret": "looks like it holds a password or key", "order": "reads like an instruction, not a note",
              "mark": "looks like one of Grok Bot Twin's own notes", "odd": "holds characters that cannot be seen",
              "long": "is too long to check"}
MESSAGES = {
    "unknown_twin": ("That twin is not on this computer.", ""),
    "not_linked": ("This computer is not linked to another one.", "Link it first; then a twin's memory can follow."),
    "no_bot": ("That twin has no Hermes bot on this computer yet.", "Wait until it is in sync, then switch memory on."),
    "unknown_point": ("That earlier version is no longer kept.", ""),
    "unknown_conflict": ("That is already settled.", ""),
    "memory_busy": ("Hermes is writing that twin's memory right now.", "Try again in a moment."),
    "not_sharing_here": ("Only the computer that shares its twins chooses which computers take part.", ""),
    "unknown_machine": ("That computer is not linked.", ""),
    "memory_unreadable": ("That twin's memory file could not be read, so it was left as it is.", ""),
}


class MemoryError_(EngineError):
    def __init__(self, code: str) -> None:
        m, hint = MESSAGES[code]
        super().__init__(code, m, hint)


def _plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"1 {one}" if n == 1 else f"{n} {many or one + 's'}"


# ---- the sealed box ----------------------------------------------------------------------------------------------------
def _keys(link_key: bytes) -> tuple:
    """The two keys for memory, made from the link's key (which itself was made at linking and never travelled)."""
    return (hmac.new(link_key, b"grok-bot-twin memory 1 enc", hashlib.sha256).digest(),
            hmac.new(link_key, b"grok-bot-twin memory 1 mac", hashlib.sha256).digest())


def _xor(a: bytes, b: bytes) -> bytes:
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")


def _tag(mac_key: bytes, way: str, machine_id: str, bind: str, nonce: bytes, box: bytes) -> str:
    head = f"grok-bot-twin memory box 1\n{way}\n{machine_id}\n{bind}\n".encode("utf-8")
    return hmac.new(mac_key, head + nonce + box, hashlib.sha256).hexdigest()


def seal(link_key: bytes, way: str, machine_id: str, bind: str, obj: dict) -> dict:
    """Encrypt and authenticate `obj` for the other end of this link. `way` is "ask" or "answer" (a box cannot be
    turned round), `machine_id` the linked computer's id, `bind` what an answer belongs to (the asking box's
    own random value), so an old answer cannot be given to a new question."""
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass")
    body = len(data).to_bytes(4, "big") + data
    body += b"\0" * (-len(body) % PAD)
    nonce = os.urandom(24)
    enc, mac = _keys(link_key)
    box = _xor(body, hashlib.shake_256(enc + nonce).digest(len(body)))
    return {"nonce": nonce.hex(), "box": base64.b64encode(box).decode("ascii"),
            "tag": _tag(mac, way, machine_id, bind, nonce, box)}


def unseal(link_key: bytes, way: str, machine_id: str, bind: str, sealed: Any, cap: int = ANSWER_BOX_CAP) -> Optional[dict]:
    """The object inside a box, or None for anything that is not a whole, untouched box made for exactly this
    use with this link's key. The check comes before anything is decrypted."""
    if not isinstance(sealed, dict):
        return None
    nonce, box, tag = sealed.get("nonce"), sealed.get("box"), sealed.get("tag")
    if not (fleet._hex_ok(nonce, 48) and fleet._hex_ok(tag, 64) and isinstance(box, str) and 0 < len(box) <= cap):
        return None
    try:
        raw = base64.b64decode(box.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None
    if not raw or len(raw) % PAD:
        return None
    enc, mac = _keys(link_key)
    n = bytes.fromhex(nonce)
    if not hmac.compare_digest(_tag(mac, way, machine_id, bind, n, raw), tag):
        return None
    body = _xor(raw, hashlib.shake_256(enc + n).digest(len(raw)))
    size = int.from_bytes(body[:4], "big")
    if size > len(body) - 4:
        return None
    try:
        obj = json.loads(body[4:4 + size].decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


# ---- which notes may travel --------------------------------------------------------------------------------------------
# Stricter than the plugin's general password-and-key check, because memory is prose: "the wifi password is ...".
# A note that trips any of these stays on the computer it is on (whole; it is never masked, a masked note would
# come back and replace the real one). Words that almost always name a secret hold the note as soon as a value
# follows; vaguer words (token, login) only when a value with a digit follows.
_B, _E = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"
_STRONG = (
    r"pass ?words?|passwd|pass ?phrases?|pass ?codes?|pass ?keys?|pwd?|pin(?: ?codes?| ?numbers?)?|api[ _-]?keys?|"
    r"access[ _-]?keys?|licen[cs]e[ _-]?keys?|private[ _-]?keys?|seed(?: ?phrases?| words)?|mnemonic|"
    r"recovery (?:codes?|phrases?|keys?|words)|backup codes?|security (?:codes?|answers?)|credentials?|"
    r"(?:door|gate|alarm|access|entry|lock|safe|unlock|verification|auth(?:entication)?|one[ -]?time)[ _-]?codes?|"
    r"security questions?|wallet (?:words|phrase)|otp|2fa|mfa|cvv|cvc|iban|ssn|social security")
_WEAK = r"secrets?|tokens?|log ?ins?|combinations?|(?:account|routing|card) (?:number|no\.?)|sort code|user ?names?"
# (every repeat is bounded and no two of them can match the same characters: a long hostile note costs
# milliseconds, not seconds; tested)
_SECRET_WORDS = re.compile(
    rf"(?i){_B}(?:{_STRONG}){_E}[^\n]{{0,24}}?(?:\b(?:is|are|was|were)\b|[:=])\s*\S"
    rf"|{_B}(?:{_STRONG}){_E}[^\n0-9]{{0,40}}[0-9]"
    rf"|{_B}(?:{_STRONG}){_E}[^\n]{{0,40}}?{_B}[A-Za-z0-9]{{4,8}}-[A-Za-z0-9]{{4,8}}{_E}"
    rf"|{_B}(?:{_WEAK}){_E}[^\n]{{0,24}}?(?:\b(?:is|are|was|were)\b|[:=])[^\n0-9]{{0,60}}"
    rf"(?:(?<![A-Za-z0-9])[0-9]{{4,}}|(?<=[A-Za-z])[0-9]|[0-9]{{1,3}}[A-Za-z])")
_SECRET_ELSEWHERE = re.compile(  # the same thing said in other languages: any note that names it stays
    r"(?i)passwort|kennwort|contrase[nñ]a|mot de passe|senha|wachtwoord|parola d'ordine|пароль|密码|密碼|パスワード|비밀번호")
# a place that has a code (a door, the wifi, a card) and a short number on the same line
_PLACE_AND_NUMBER = re.compile(
    r"(?im)^(?=[^\n]*\b(?:codes?|door|gate|alarm|lock(?:er|box)?|safe|garage|wi-?fi|router|visa|mastercard|amex|card)\b)"
    r"[^\n]*(?<![0-9A-Za-z.])[0-9]{3,8}[#*]?(?![0-9A-Za-z])")
_BEARER = re.compile(
    r"(?i)\b(?:bearer|basic)\s+(?=\S{0,200}[0-9=])[A-Za-z0-9._~+/=-]{8,}"
    r"|(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,20}://[^\s/:@]{1,80}:[^\s/@]{1,80}@"
    r"|[?&](?:access_token|api_key|apikey|auth|key|password|secret|sig|signature|token)=[^\s&]{6,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_ENV_LINE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9_]{0,60}(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|CREDENTIALS?)\s*[:=]\s*\S")
# a place one signs in to (the wifi, a portal, an account) and, on the same line, a word made of letters and digits
_PLACE_AND_WORD = re.compile(
    r"(?im)^(?=[^\n]{0,400}\b(?:wi-?fi|router|portal|sign[- ]?in|log[- ]?in|account|ssh)\b)[^\n]{0,400}?"
    r"(?<![A-Za-z0-9])(?=[A-Za-z]{0,30}[0-9])(?=[0-9]{0,30}[A-Za-z])[A-Za-z0-9!@#$%^&*._-]{6,}")
_DIGIT_RUN = re.compile(r"(?<![0-9])(?:[0-9][ .\-/]?){13,19}(?![0-9])")
_IBAN = re.compile(r"\b[A-Z]{2}[0-9]{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b")
_GROUPED = re.compile(r"(?<![A-Za-z0-9-])(?=[A-Za-z0-9-]*[0-9])[A-Za-z0-9]{4,6}(?:-[A-Za-z0-9]{4,6}){1,5}(?![A-Za-z0-9-])")
_BLOB = re.compile(r"\b[A-Fa-f0-9]{24,}\b|(?<![A-Za-z0-9+_=-])(?=[A-Za-z0-9+_=-]{0,200}[0-9])(?=[A-Za-z0-9+_=-]{0,200}[a-z])"
                   r"(?=[A-Za-z0-9+_=-]{0,200}[A-Z])[A-Za-z0-9+_=-]{20,}")
_OWN_MARKS = re.compile(
    r"(?i)<!--|\[\s*(?:end\s+)?grok[\s_-]*bot[\s_-]*twin|\[\s*sent\s+from\s+your\s+computer|"
    r"\[\s*description\s+of\s+this\s+twin|\[\s*blocked\s*:|[═━]{3,}|={6,}|^\s*§\s*$", re.M)
# The list below names phrases this check looks for in a NOTE, so that such a note stays on its computer. Three of
# them are written with a one-character class or in another order ("are[ ]now", "authorized[_]keys", ".env" last in
# its group): they match exactly the same text, and Hermes' own plugin scanner, which looks for the same phrases in
# source code, then does not take this list for the thing it describes.
_ORDERS = re.compile(
    r"(?i)\b(?:ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|any|your|"
    r"system|safety)\b[^.\n]{0,30}\b(?:instructions?|rules?|prompts?|guidelines?|guardrails?)\b"
    r"|\b(?:ignoriere|ignorez|ignora)\b[^.\n]{0,60}"
    r"|\b(?:do not|don'?t|never)\b[^.\n]{0,20}\b(?:tell|inform|mention|reveal|ask|let)\b[^.\n]{0,30}\b(?:user|owner|know)\b"
    r"|\byou are[ ]now (?:a|an|the|in|my|acting|operating|free|unrestricted|no longer)\b|\bfrom now on\b"
    r"|\bnew system prompt\b|\bsystem prompt\s*:|\bwithout (?:asking|telling|approval)\b|\bpre-?approved\b"
    r"|\bstanding (?:instructions?|orders?)\b|\bbefore (?:answering|replying|responding)\b"
    r"|\byour (?:real|true|new|actual) (?:instructions?|task|goal|purpose)\b"
    r"|</?\s*(?:system|assistant|user|developer|instructions?|tool|memory)[^>\n]{0,40}>"
    r"|^\s*(?:system|assistant|developer)\s*:|!\[[^\]\n]*\]\(\s*https?://"
    r"|\b(?:curl|wget)\b[^\n]{0,200}(?:https?://|\|)|\b(?:fetch|open|visit|load|call)\b[^.\n]{0,40}https?://"
    r"|\bauthorized[_]keys\b|\b(?:cat|type|read)\b[^\n]{0,80}(?:\.netrc|credentials|id_rsa|auth\.json|\.env)"
    r"|\|\s*(?:sh|bash|zsh|python[0-9.]*)\b|\bbase64\s+(?:-d|--decode)\b|\bsudo\s+\S"
    r"|\b(?:always|every time|whenever)\b[^.\n]{0,60}\b(?:send|upload|post|email|forward)\b[^.\n]{0,60}\bto\b"
    r"|\b(?:always|every time|whenever)\b[^.\n]{0,40}\bapprove\b"
    r"|\b(?:approve|allow|accept)\b[^.\n]{0,30}\b(?:every|all|any)\b[^.\n]{0,20}\b(?:steps?|requests?|commands?)\b", re.M)
# characters that cannot be seen and that Unicode does not class as "format" characters: joiners and fillers,
# variation selectors, the blank Braille cell
_UNSEEN = set("͏ᅟᅠ឴឵⠀ㅤﾠ") | {chr(c) for c in range(0xFE00, 0xFE10)}
_WORD_RE = re.compile(r"[^\W\d_]{2,}")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _odd_characters(entry: str) -> bool:
    """Control characters, characters that cannot be seen (Unicode's format, private and unassigned classes: zero
    width marks, direction marks, the "tag" block, soft hyphens), and words that mix Latin letters with Greek or
    Cyrillic look-alikes."""
    if safe._CTRL.search(entry) or "\r" in entry:
        return True
    for ch in entry:
        if ch in _UNSEEN or (ord(ch) > 0x7e and unicodedata.category(ch) in ("Cf", "Co", "Cn", "Cs")):
            return True
        if 0xE0000 <= ord(ch) <= 0xE0FFF:
            return True
    for word in _WORD_RE.findall(entry):
        if any("a" <= c.lower() <= "z" for c in word) and any(0x370 <= ord(c) <= 0x52f for c in word):
            return True
    return False


def _holds_secret(text: str) -> bool:
    if (secrets.detect_secrets(text) or _SECRET_WORDS.search(text) or _SECRET_ELSEWHERE.search(text)
            or _PLACE_AND_NUMBER.search(text) or _PLACE_AND_WORD.search(text) or _BEARER.search(text)
            or _ENV_LINE.search(text) or _IBAN.search(text)
            or _GROUPED.search(text) or _BLOB.search(text)):
        return True
    return any(_luhn(re.sub(r"[^0-9]", "", m.group(0))) for m in _DIGIT_RUN.finditer(text))  # a payment card number


def why_held(entry: Any) -> Optional[str]:
    """None when a note may travel; else why not (a key of HELD_WORDS). The same check runs on the computer that
    would send a note and again on the computer that receives it."""
    if not isinstance(entry, str):
        return "odd"
    return _why_held(entry)


@functools.lru_cache(maxsize=4096)  # (a note is judged once, however often the file is looked at)
def _why_held(entry: str) -> Optional[str]:
    if not entry or entry != entry.strip() or DELIM in entry:
        return "odd"
    if len(entry) > ENTRY_CHARS or len(entry.encode("utf-8", "replace")) > ENTRY_BYTES:
        return "long"
    if _odd_characters(entry):
        return "odd"
    # read three times: as written; with look-alike forms folded to the plain ones (full-width letters,
    # ligatures); and with accents and other marks taken off the letters ("pássword")
    bare = "".join(c for c in unicodedata.normalize("NFKD", entry) if unicodedata.category(c) != "Mn")
    for text in dict.fromkeys((entry, unicodedata.normalize("NFKC", entry), bare)):
        if _holds_secret(text):
            return "secret"
        if _OWN_MARKS.search(text):
            return "mark"
        if _ORDERS.search(text):
            return "order"
    return None


def digest(entry: str) -> str:
    return hashlib.sha256(b"grok-bot-twin memory note\n" + entry.encode("utf-8", "replace")).hexdigest()[:24]


def _hashes(value: Any, cap: int = MAX_ENTRIES) -> list:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(h for h in value[:cap * 2] if isinstance(h, str) and HASH_RE.match(h)))[:cap]


def _notes(value: Any) -> tuple:
    """(the notes of a received list that may be written here, how many were turned away)."""
    if not isinstance(value, list):
        return [], 0
    out, seen, refused = [], set(), 0
    for e in value[:MAX_ENTRIES]:
        if why_held(e) is not None:
            refused += 1
            continue
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out, refused + max(0, len(value) - MAX_ENTRIES)


# ---- a bot's memory files ----------------------------------------------------------------------------------------------
@contextlib.contextmanager
def _file_lock(lock: Path, wait: float = 5.0) -> Iterator[bool]:
    """Hold a lock file for the block; yields False when it could not be had in time (then nothing is written)."""
    fd, got = None, False
    try:
        store.ensure_dir(lock.parent)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        end = time.monotonic() + wait
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= end:
                    break
                time.sleep(0.05)
    except OSError:
        got = False
    try:
        yield got
    finally:
        if fd is not None:
            if got:
                with contextlib.suppress(Exception):
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, 0)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


def _mem_lock(path: Path) -> Any:
    """Hermes' own lock for a memory file (a `.lock` file beside it): while this is held Hermes does not write
    the file, and this module never writes it without."""
    return _file_lock(path.with_suffix(path.suffix + ".lock"))


def _bot_home(profile: Any) -> Optional[Path]:
    try:
        home = fp.profile_dir(profile)
    except Exception:
        return None
    return home if fio.is_real_dir(home) else None


def _mem_path(home: Path, name: str) -> Path:
    return home / "memories" / FILES[name]


def _read(path: Path) -> Optional[list]:
    """The notes of a memory file; [] when there is none; None when it is there but cannot be read safely (a
    link, not UTF-8, too large): then it is never written either."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return []
    except OSError:
        return None
    if os.path.lexists(path.parent) and not fio.is_real_dir(path.parent):
        return None
    text = fio.read_text(path, FILE_BYTES)
    if text is None:
        return None
    return list(dict.fromkeys(e for e in (x.strip() for x in text.split(DELIM)) if e))


def _write(path: Path, entries: list) -> None:
    if os.path.lexists(path.parent) and not fio.is_real_dir(path.parent):
        raise store.UnsafePath("refusing to write a memory file through a link")
    fio.write_text(path, DELIM.join(entries))


def _limits(home: Path) -> dict:
    """How many characters Hermes lets each memory file of this bot hold (its own settings, else its defaults)."""
    out = dict(DEFAULT_LIMITS)
    text = fio.read_text(home / "config.yaml", 512 * 1024) or ""
    for name, key in (("memory", "memory_char_limit"), ("user", "user_char_limit")):
        m = re.search(rf"(?m)^[ \t]+{key}[ \t]*:[ \t]*([0-9]{{2,7}})[ \t]*(?:#.*)?$", text)
        if m:
            out[name] = max(200, min(100_000, int(m.group(1))))
    return out


# ---- this computer's own notes about memory sharing (digests and switches; a note's text only in a restore point) -----
_guard = threading.RLock()


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    with _guard:
        with _file_lock(store.central_dir() / "xs-memory.lock", 10.0) as got:
            if not got:
                raise OSError("the memory state is in use")
            yield


def _blank_twin() -> dict:
    return {"on": False, "since": None, "epoch": None, "rev": None, "state": "off", "synced_at": None,
            "base": {n: [] for n in FILES}, "foreign": {n: [] for n in FILES}, "seen": {n: [] for n in FILES},
            "tombs": {n: {} for n in FILES}, "held": {}, "full": {n: [] for n in FILES}, "full_rev": None,
            "refused": 0, "over": 0, "kept": 0, "later": 0, "with": [], "chosen": False, "changes": {},
            "conflicts": [], "resolved": [], "last": None}


def _reset(rec: dict) -> None:
    """Back to "off, nothing agreed". Kept: which notes came from elsewhere (so they can still be forgotten here)
    and, on the computer that shares, which computers the owner lets take part and what each of them changed
    lately (so switching off and on is no way round a limit)."""
    keep = {"foreign": rec["foreign"], "with": rec["with"], "chosen": rec["chosen"], "changes": rec["changes"],
            "tombs": rec["tombs"]}
    rec.update(_blank_twin())
    rec.update(keep)


def _clean_twin(raw: Any) -> dict:
    t = _blank_twin()
    if not isinstance(raw, dict):
        return t
    t["on"] = raw.get("on") is True
    for key in ("since", "synced_at"):
        t[key] = raw.get(key) if parse_iso(raw.get(key)) else None
    for key in ("epoch", "rev", "full_rev"):
        t[key] = raw.get(key) if isinstance(raw.get(key), str) and EPOCH_RE.match(raw[key]) else None
    t["state"] = raw.get("state") if raw.get("state") in ("off", "on", "in_sync", "waiting", "off_there", "older_there",
                                                          "error") else "off"
    for key in ("base", "foreign", "seen", "full"):
        src = raw.get(key) if isinstance(raw.get(key), dict) else {}
        t[key] = {n: _hashes(src.get(n), MAX_FOREIGN) for n in FILES}
    tombs = raw.get("tombs") if isinstance(raw.get("tombs"), dict) else {}
    for n in FILES:
        src = tombs.get(n) if isinstance(tombs.get(n), dict) else {}
        t["tombs"][n] = {h: {"by": str(v.get("by"))[:20], "name": fp.machine_name(v.get("name")),
                             "at": v.get("at") if parse_iso(v.get("at")) else now_iso(),
                             "became": _hashes(v.get("became"), 3)}
                         for h, v in list(src.items())[:MAX_TOMBS] if HASH_RE.match(str(h)) and isinstance(v, dict)}
    held = raw.get("held") if isinstance(raw.get("held"), dict) else {}
    t["held"] = {k: int(v) for k, v in held.items() if k in HELD_WORDS and isinstance(v, int) and 0 < v < 10_000}
    for key in ("refused", "over", "kept", "later"):
        t[key] = raw[key] if isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool) and 0 <= raw[key] < 10_000 else 0
    t["with"] = [m for m in raw.get("with") if isinstance(m, str) and fleet.MACHINE_ID_RE.match(m)][:fleet.MAX_MACHINES + 1] \
        if isinstance(raw.get("with"), list) else []
    t["chosen"] = raw.get("chosen") is True
    changes = raw.get("changes") if isinstance(raw.get("changes"), dict) else {}
    t["changes"] = {m: [a for a in v if parse_iso(a)][-CHANGES_PER_DAY * 2:] for m, v in list(changes.items())[:64]
                    if isinstance(m, str) and fleet.MACHINE_ID_RE.match(m) and isinstance(v, list)}
    t["conflicts"] = _clean_conflicts(raw.get("conflicts"))
    t["resolved"] = [c for c in (raw.get("resolved") or []) if isinstance(c, str) and CONFLICT_RE.match(c)][:MAX_CONFLICTS] \
        if isinstance(raw.get("resolved"), list) else []
    last = raw.get("last")
    if isinstance(last, dict) and parse_iso(last.get("at")):
        t["last"] = {"at": last["at"], "added": int(last.get("added") or 0) if isinstance(last.get("added"), int) else 0,
                     "removed": int(last.get("removed") or 0) if isinstance(last.get("removed"), int) else 0}
    return t


def _clean_conflicts(raw: Any) -> list:
    out = []
    for c in raw[:MAX_CONFLICTS] if isinstance(raw, list) else []:
        if not isinstance(c, dict) or not CONFLICT_RE.match(str(c.get("id"))) or c.get("file") not in FILES:
            continue
        if c.get("kind") not in ("both_changed", "removed_and_changed", "changed_and_removed"):
            continue
        by = [fp.machine_name(b) for b in c.get("by")[:2]] if isinstance(c.get("by"), list) else []
        if len(by) != 2:
            continue
        out.append({"id": c["id"], "file": c["file"], "kind": c["kind"], "versions": _hashes(c.get("versions"), 6),
                    "by": by, "at": c.get("at") if parse_iso(c.get("at")) else now_iso()})
    return out


def _link_mark(fst: dict) -> Optional[str]:
    """What this computer's memory notes belong to: its link (a new link starts with everything off, as a first
    one does), or its own sharing."""
    ln = fst.get("link")
    if isinstance(ln, dict):
        src = ln.get("source") if isinstance(ln.get("source"), dict) else {}
        return f"follows {src.get('id')} since {ln.get('linked_at')}"
    return "shares" if fleet.role(fst) == "source" else None


def _load(fst: Optional[dict] = None) -> dict:
    raw = fio.read_json(store.central_dir() / STATE_FILE, None)
    st = {"v": 1, "link": None, "twins": {}, "gone": {}}
    if isinstance(raw, dict) and isinstance(raw.get("twins"), dict):
        st["link"] = raw.get("link") if isinstance(raw.get("link"), str) else None
        gone = raw.get("gone") if isinstance(raw.get("gone"), dict) else {}
        st["gone"] = {tid: at for tid, at in list(gone.items())[:fleet.MAX_TWINS]
                      if isinstance(tid, str) and TID_RE.match(tid) and parse_iso(at)}
        st["twins"] = {tid: _clean_twin(t) for tid, t in list(raw["twins"].items())[:fleet.MAX_TWINS]
                       if isinstance(tid, str) and TID_RE.match(tid)}
    if fst is not None:
        mark = _link_mark(fst)
        if st["link"] != mark:
            # another link (or none): every switch is off again and nothing of the old agreement is believed.
            # What came from elsewhere is still remembered, so that it can be forgotten here
            for t in st["twins"].values():
                _reset(t)
            st["link"] = mark
            st["_moved"] = True
    return st


def _save(st: dict) -> None:
    st = {k: v for k, v in st.items() if not k.startswith("_")}
    fio.write_json(store.central_dir() / STATE_FILE, st)


def _twin(st: dict, tid: str) -> dict:
    t = st["twins"].get(tid)
    if t is None:
        t = st["twins"][tid] = _blank_twin()
    return t


def any_on() -> bool:
    """Cheap: is memory switched on for any twin on this computer? (One small file.)"""
    try:
        raw = fio.read_json(store.central_dir() / STATE_FILE, None)
        return isinstance(raw, dict) and any(isinstance(t, dict) and t.get("on") is True
                                             for t in (raw.get("twins") or {}).values())
    except Exception:
        return False


# ---- restore points ----------------------------------------------------------------------------------------------------
def _points_dir(tid: str) -> Path:
    if not TID_RE.match(tid):
        raise ValueError("not a twin id")
    return store.central_dir() / POINTS_DIR / tid


def _point_files(tid: str) -> list:
    d = _points_dir(tid)
    if not fio.is_real_dir(d):
        return []
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    for name in names:
        if name.endswith(".json") and POINT_RE.match(name[:-5]):
            p = fio.read_json(d / name, None)
            if isinstance(p, dict) and parse_iso(p.get("at")) and isinstance(p.get("files"), dict):
                out.append((p["at"], name[:-5], p))
    out.sort(key=lambda x: (x[0], x[2].get("seq") if isinstance(x[2].get("seq"), int) else 0, x[1]))
    return out


def _drop_point(tid: str, pid: str) -> None:
    if not POINT_RE.match(pid):
        return
    d = _points_dir(tid)
    path = d / f"{pid}.json"
    if store.contained(store.central_dir() / POINTS_DIR, path) and fio.is_real_dir(d):
        with contextlib.suppress(OSError):
            os.unlink(path)


WHY_WORDS = {"first": "before memory was switched on here", "arrived": "before notes arrived from your other computers",
             "settled": "before a conflict was settled", "put_back": "before an earlier version was put back"}


def save_point(tid: str, home: Path, why: str) -> None:
    """Keep the bot's two memory files as they are now, before this module changes one of them. Always, unless
    the newest point already holds exactly this (so nothing written since the last point can be lost). Kept:
    the newest thirty, the first one of each day, both for thirty days, and the very first (the memory from
    before it was ever shared here) for as long as the twin is."""
    points = _point_files(tid)
    if why == "first" and any(p.get("why") == "first" for _, _, p in points):
        return
    files = {}
    for name in FILES:
        entries = _read(_mem_path(home, name))
        files[name] = None if entries is None else entries
    if points and points[-1][2].get("files") == files:
        return
    d = _points_dir(tid)
    store.ensure_dir(d)
    fio.write_json(d / f"p-{_random.token_hex(6)}.json", {"at": now_iso(), "seq": time.time_ns(), "why": why,
                                                          "files": files})
    points = _point_files(tid)
    cut = iso(now_dt() - timedelta(days=POINT_DAYS))
    keep = {pid for _at, pid, p in points if p.get("why") == "first"}
    keep |= {pid for at, pid, _p in points[-MAX_POINTS:] if at >= cut}
    days: dict = {}
    for at, pid, _p in points:
        if at >= cut:
            days.setdefault(at[:10], pid)  # a flood of changes cannot push out how the day began
    keep |= set(days.values())
    for _at, pid, _p in points:
        if pid not in keep:
            _drop_point(tid, pid)


_POINT_FILE_RE = re.compile(r"^\.?p-[0-9a-f]{12}\.json(?:\.[A-Za-z0-9_]{1,16}\.tmp)?\Z")


def _forget_points(tid: str) -> int:
    """Delete every earlier version kept for a twin (also one that cannot be read, and a half-written one).
    Each file by its exact name inside this twin's own folder under the plugin's data; returns how many."""
    d = _points_dir(tid)
    root = store.central_dir() / POINTS_DIR
    if not fio.is_real_dir(d) or not store.contained(root, d):
        return 0
    n = 0
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return 0
    for name in names:
        if _POINT_FILE_RE.match(name):
            with contextlib.suppress(OSError):
                os.unlink(d / name)
                n += 0 if name.startswith(".") else 1
    with contextlib.suppress(OSError):
        os.rmdir(d)  # (only when empty)
    return n


def _point_twins() -> list:
    root = store.central_dir() / POINTS_DIR
    if not fio.is_real_dir(root):
        return []
    try:
        return [n for n in sorted(os.listdir(root)) if TID_RE.match(n) and fio.is_real_dir(root / n)]
    except OSError:
        return []


# ---- the computer that shares: the meeting point -----------------------------------------------------------------------
def _own_changes(rec: dict, name: str, hs: list, me: dict, may_travel: set) -> None:
    """Notes that left this computer's own file since this module last looked were removed here (by the twin or
    the owner): remembered as such, with what appeared in the same breath (a changed note is one that left and
    one that came)."""
    seen = rec["seen"][name]
    if not seen:
        return
    now_set = set(hs)
    gone = [h for h in seen if h not in now_set]
    if gone:
        came = [h for h in hs if h not in set(seen) and h in may_travel][:3]  # (never a held note's digest)
        for h in gone:
            rec["tombs"][name][h] = {"by": me["id"], "name": me["name"], "at": now_iso(), "became": came}


def _prune_tombs(rec: dict) -> None:
    cut = iso(now_dt() - timedelta(days=TOMB_DAYS))
    for name in FILES:
        tombs = rec["tombs"][name]
        for h in [h for h, v in tombs.items() if v["at"] < cut]:
            del tombs[h]
        for h in sorted(tombs, key=lambda h: tombs[h]["at"])[:-MAX_TOMBS]:
            del tombs[h]


def _note_conflict(rec: dict, name: str, tomb: dict, who: str, added: list, present: set) -> None:
    theirs = [h for h in tomb["became"] if h in present]  # (`present`: the notes of the file that may travel)
    mine = [h for h in added if h in present and h not in theirs][:3]
    if theirs and mine:
        kind = "both_changed"
    elif mine:
        kind = "removed_and_changed"
    elif theirs:
        kind = "changed_and_removed"
    else:
        return
    versions = theirs + mine
    if any(c["file"] == name and set(c["versions"]) == set(versions) for c in rec["conflicts"]):
        return
    rec["conflicts"].append({"id": "c-" + _random.token_hex(6), "file": name, "kind": kind, "versions": versions,
                             "by": [tomb["name"], who], "at": now_iso()})
    del rec["conflicts"][:-MAX_CONFLICTS]


def _live_conflicts(rec: dict, present: dict) -> list:
    """Conflicts whose kept versions are still there (settled by hand otherwise: nothing left to choose)."""
    cut = iso(now_dt() - timedelta(days=TOMB_DAYS))
    out = []
    for c in rec["conflicts"]:
        left = [h for h in c["versions"] if h in present[c["file"]]]
        need = 2 if c["kind"] == "both_changed" else 1
        if len(left) >= need and c["at"] >= cut:
            out.append({**c, "versions": left})
    return out


def _rev(epoch: Optional[str], shared: dict, conflicts: list) -> str:
    text = json.dumps([epoch, [shared[n] for n in FILES], [c["id"] for c in conflicts]])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def hub_exchange(fst: dict, machine: dict, plain: dict) -> dict:
    """One linked computer's question about one twin, answered from this computer's own memory files. Returns
    what goes back inside the box. Never raises for anything the question carried."""
    tid = plain.get("twin")
    if not isinstance(tid, str) or not TID_RE.match(tid):
        return {"error": "bad"}
    listed = ((fst.get("share") or {}).get("list") or {}).get("twins") or {}
    t = listed.get(tid) if isinstance(listed.get(tid), dict) else None
    me = fleet.this_machine(fst)
    who = fp.machine_name(machine.get("name"))
    arrived = 0
    with _locked():
        st = _load(fst)
        rec = st["twins"].get(tid)
        if t is None or rec is None or not rec["on"] or not rec["epoch"] or machine["id"] not in rec["with"]:
            # off here, no such twin, or not one of the computers the owner let take part: one and the same answer
            if st.get("_moved"):
                _save(st)
            return {"twin": tid, "on": False}
        before = json.dumps(rec, sort_keys=True)
        home = _bot_home(t.get("profile"))
        if home is None:
            return {"twin": tid, "on": True, "busy": True}
        for name in FILES:
            if rec["seen"][name] and not os.path.lexists(_mem_path(home, name)):
                # this bot's memory file is gone altogether (the bot was made anew, the folder replaced): that
                # is not "the owner removed every note". Start anew, so the other computers' notes come back
                # here instead of leaving there. (Hermes leaves an empty file when the last note is removed.)
                rec["epoch"] = _random.token_hex(8)
                rec["seen"], rec["tombs"] = {n: [] for n in FILES}, {n: {} for n in FILES}
                break
        same_epoch = plain.get("epoch") == rec["epoch"]
        adds_in = plain.get("adds") if isinstance(plain.get("adds"), dict) else {}
        removes_in = plain.get("removes") if isinstance(plain.get("removes"), dict) else {}
        limits = _limits(home)
        full: dict = {}
        refused = kept = later = 0
        day = iso(now_dt() - timedelta(hours=24))
        mine_today = [a for a in rec["changes"].get(machine["id"], []) if a >= day]
        removed_today = sum(1 for n in FILES for v in rec["tombs"][n].values() if v["by"] == machine["id"] and v["at"] >= day)
        changed_now = False
        shared: dict = {}
        present: dict = {}
        texts: dict = {}
        for name in FILES:
            path = _mem_path(home, name)
            adds, bad = _notes(adds_in.get(name))
            refused += bad
            removes = _hashes(removes_in.get(name)) if same_epoch else []
            if len(mine_today) >= CHANGES_PER_DAY:
                # this computer changed this twin's memory often enough for one day: it still gets the list,
                # its own changes wait (they stay on it and are offered again). So no computer can churn a
                # memory, or push the earlier versions kept here out of reach, by asking again and again
                later += len(adds) + len(removes)
                adds, removes = [], []
            with _mem_lock(path) as got:
                current = _read(path) if got else None
                if current is None:
                    return {"twin": tid, "on": True, "busy": True}
                hs = [digest(e) for e in current]
                travels = {digest(e) for e in current if why_held(e) is None}
                _own_changes(rec, name, hs, me, travels)
                new, have = list(current), set(hs)
                add_hashes = [digest(e) for e in adds]
                late = []
                # only a note that travels can be removed from elsewhere (never one this computer holds back),
                # and only a few at a time: a linked computer that asks to remove most of a twin's memory in
                # one go is not believed, the notes stay and go back to it. (Clearing a memory is done here.)
                wanted = [h for h in removes if h in travels]
                if removed_today + len(wanted) > max(BIG_REMOVAL, (len(travels) + removed_today) // 2):
                    kept += len(wanted)
                    wanted = []
                removed_today += len(wanted)
                for h in removes:
                    if h in wanted:
                        new = [e for e in new if digest(e) != h]
                        have.discard(h)
                        rec["tombs"][name][h] = {"by": machine["id"], "name": who, "at": now_iso(),
                                                 "became": add_hashes[:3]}
                    elif h not in have:
                        late.append(h)
                for e, h in zip(adds, add_hashes):
                    if h in have:
                        continue
                    if len(DELIM.join(new + [e])) > limits[name]:
                        full.setdefault(name, []).append(h)
                        continue
                    new.append(e)
                    have.add(h)
                    rec["tombs"][name].pop(h, None)
                    if h not in rec["foreign"][name]:
                        rec["foreign"][name] = (rec["foreign"][name] + [h])[-MAX_FOREIGN:]
                    arrived += 1
                still = {digest(e) for e in new if why_held(e) is None}
                for h in late:  # it removed (or changed) a note that another computer had already removed or changed
                    tomb = rec["tombs"][name].get(h)
                    if tomb and tomb["by"] != machine["id"]:
                        _note_conflict(rec, name, tomb, who, add_hashes, still)
                if new != current:
                    save_point(tid, home, "arrived")
                    _write(path, new)
                    changed_now = True
                rec["seen"][name] = [digest(e) for e in new]
                texts[name] = [e for e in new if why_held(e) is None][:MAX_ENTRIES]
                shared[name] = [digest(e) for e in texts[name]]
                present[name] = set(shared[name])
        for cid in plain.get("resolved") if isinstance(plain.get("resolved"), list) else []:
            rec["conflicts"] = [c for c in rec["conflicts"] if c["id"] != cid]
        rec["conflicts"] = _live_conflicts(rec, present)
        _prune_tombs(rec)
        if changed_now:
            rec["changes"][machine["id"]] = mine_today + [now_iso()]
        elif machine["id"] in rec["changes"] and len(mine_today) != len(rec["changes"][machine["id"]]):
            rec["changes"][machine["id"]] = mine_today
        rev = _rev(rec["epoch"], shared, rec["conflicts"])
        rec["rev"], rec["state"] = rev, "on"
        if arrived:
            rec["last"] = {"at": now_iso(), "added": arrived, "removed": 0}
        if st.get("_moved") or json.dumps(rec, sort_keys=True) != before:
            _save(st)
        if plain.get("more") is True:
            out = {"twin": tid, "on": True, "epoch": rec["epoch"], "more": True}
        elif same_epoch and plain.get("rev") == rev and not full and not refused and not kept and not later:
            out = {"twin": tid, "on": True, "epoch": rec["epoch"], "rev": rev, "same": True}
        else:
            out = {"twin": tid, "on": True, "epoch": rec["epoch"], "rev": rev, "files": texts, "full": full,
                   "refused": refused, "kept": kept, "later": later, "conflicts": rec["conflicts"]}
    if arrived:
        _emit(arrived)
    return out


def wire(asked: Any, body: dict) -> tuple:
    """`POST /v1/xs/memory` on the computer that shares: (status, object, signed) like the other signed routes."""
    from . import fleet_server as server
    if body.get("proto") != fleet.PROTO:
        return server._err(426, "version_mismatch") + (None,)
    again = server._replayed(asked, body)
    if again is not None:
        return again
    fst = fleet.load()
    f, key = asked.by(fst)
    if f is None:
        return server._unlinked(key, asked)
    signed = (key, asked.nonce)
    speaks = body.get("xs")
    if not isinstance(speaks, int) or isinstance(speaks, bool) or speaks < 1:
        return server._err(400, "bad_request") + (signed,)
    plain = unseal(key, "ask", f["id"], "", body.get("seal"), ASK_BOX_CAP)
    if plain is None:  # not a box made with this link's key: nothing was opened, nothing is said about memory
        return server._err(400, "bad_box") + (signed,)
    try:
        out = hub_exchange(fst, f, plain)
    except Exception:
        log.debug("grok-bot-twin: a memory exchange failed")  # (never the exception's text: it could quote a note)
        out = {"twin": plain.get("twin") if isinstance(plain.get("twin"), str) else "", "on": True, "busy": True}
    bind = str((body.get("seal") or {}).get("nonce"))
    return 200, {"schema": fleet.SCHEMA, "xs": min(XS, speaks), "seal": seal(key, "answer", f["id"], bind, out)}, signed


# ---- a linked computer: ask, merge, write ------------------------------------------------------------------------------
def _batches(tid: str, rec: dict, adds: dict, removes: dict) -> list:
    """The questions of one exchange: what was added and removed here, cut so that each fits one request. Only
    the last one is answered with the list."""
    base = {"twin": tid, "epoch": rec["epoch"], "rev": rec["rev"]}
    out: list = []
    cur: dict = {"adds": {n: [] for n in FILES}, "removes": {n: [] for n in FILES}}
    size = 0
    for kind, src, cost in (("removes", removes, lambda h: 30), ("adds", adds, lambda e: len(e.encode("utf-8")) * 2 + 8)):
        for name in FILES:
            for item in src[name]:
                c = cost(item)
                if size and size + c > BATCH_BYTES:
                    out.append({**base, **cur, "more": True})
                    cur, size = {"adds": {n: [] for n in FILES}, "removes": {n: [] for n in FILES}}, 0
                cur[kind][name].append(item)
                size += c
    out.append({**base, **cur, "resolved": list(rec["resolved"])})
    return out


def _ask(ln: dict, mid: str, key: bytes, plain: dict) -> tuple:
    """(what happened, the answer's object). One sealed question, one sealed answer."""
    from . import fleet_follow as follow
    box = seal(key, "ask", mid, "", plain)
    try:
        status, obj, signed = follow._counted_request(ln, mid, ROUTE, {"proto": fleet.PROTO, "xs": XS, "seal": box})
    except follow.Unreachable:
        return "waiting", None
    if status == 404:
        return "older_there", None  # an older Grok Bot Twin on the other computer: it does not know this route
    if status != 200 or not signed:
        return "waiting", None
    ans = unseal(key, "answer", mid, box["nonce"], obj.get("seal"))
    if ans is None or ans.get("twin") != plain["twin"]:
        return "waiting", None
    return "ok", ans


def follow_once(tid: str, get_service: Optional[Callable] = None) -> str:
    """One exchange for one twin on a linked computer. Returns the twin's state afterwards."""
    fst = fleet.load()
    ln = fst.get("link")
    if not isinstance(ln, dict) or not isinstance(ln.get("pass"), str):
        return "off"
    entry = (ln.get("twins") or {}).get(tid)
    home = _bot_home(entry.get("profile")) if isinstance(entry, dict) and not entry.get("making") else None
    mid = fst["machine"]["id"]
    key = fleet.share_key(ln["pass"])
    with _locked():
        st = _load(fst)
        rec = st["twins"].get(tid)
        if st.get("_moved"):
            _save(st)
        if rec is None or not rec["on"]:
            return "off"
        rec = json.loads(json.dumps(rec))  # a copy to work from while nothing is locked
    if home is None:
        return _set_state(tid, "waiting")
    snap: dict = {}
    adds: dict = {}
    removes: dict = {}
    held: dict = {}
    for name in FILES:
        current = _read(_mem_path(home, name))
        if current is None:
            return _set_state(tid, "error")
        snap[name] = current
        base = set(rec["base"][name]) if rec["epoch"] else set()
        hs = {digest(e) for e in current}
        skip = set(rec["full"][name]) if rec["full_rev"] and rec["full_rev"] == rec["rev"] else set()
        adds[name] = []
        for e in current:
            h = digest(e)
            if h in base:
                continue
            why = why_held(e)
            if why is not None:
                held[why] = held.get(why, 0) + 1
            elif h not in skip:
                adds[name].append(e)
        removes[name] = [h for h in rec["base"][name] if h not in hs] if rec["epoch"] else []
    ans = None
    for plain in _batches(tid, rec, adds, removes):
        what, ans = _ask(ln, mid, key, plain)
        if what != "ok":
            return _set_state(tid, what, held=held)
        if ans.get("on") is not True:
            return _set_state(tid, "off_there", held=held)
        if ans.get("busy") is True or ans.get("error"):
            return _set_state(tid, "waiting", held=held)
    assert ans is not None
    if ans.get("same") is True:
        return _set_state(tid, "in_sync", held=held, synced=True)
    epoch = ans.get("epoch") if isinstance(ans.get("epoch"), str) and EPOCH_RE.match(ans["epoch"]) else None
    rev = ans.get("rev") if isinstance(ans.get("rev"), str) and EPOCH_RE.match(ans["rev"]) else None
    files = ans.get("files") if isinstance(ans.get("files"), dict) else None
    if epoch is None or rev is None or files is None:
        return _set_state(tid, "waiting", held=held)
    fresh = epoch != rec["epoch"]  # a first agreement (or the other computer started anew): nothing is removed here
    added = removed = refused = over = 0
    whole = True
    limits = _limits(home)
    kept = ans.get("kept") if isinstance(ans.get("kept"), int) and not isinstance(ans.get("kept"), bool) else 0
    later = ans.get("later") if isinstance(ans.get("later"), int) and not isinstance(ans.get("later"), bool) else 0
    with _locked():
        st = _load(fst)
        now = st["twins"].get(tid)
        if now is None or not now["on"]:
            return "off"
        for name in FILES:
            theirs, bad = _notes(files.get(name))
            refused += bad
            their_set = {digest(e) for e in theirs}
            base = set() if fresh else set(now["base"][name])
            path = _mem_path(home, name)
            with _mem_lock(path) as got:
                current = _read(path) if got else None
                if current is None or current != snap[name]:
                    whole = False  # Hermes wrote the file meanwhile: this round changes nothing in it
                    continue
                mine = {digest(e) for e in current}
                keep = [e for e in current if digest(e) not in their_set and digest(e) not in base]
                # a note that arrives must fit: Hermes lets this bot's file hold only so many characters, and a
                # file over that limit can no longer be added to. What does not fit is left out and counted
                size = len(DELIM.join([e for e in theirs if digest(e) in mine] + keep))
                fits = set()
                for e in theirs:
                    if digest(e) not in mine:
                        if size + len(DELIM) + len(e) <= limits[name]:
                            fits.add(e)
                            size += len(DELIM) + len(e)
                        else:
                            over += 1
                taken = [e for e in theirs if digest(e) in mine or e in fits]
                new = taken + keep
                if new != current:
                    if set(new) != set(current):
                        save_point(tid, home, "first" if not _point_files(tid) else "arrived")
                    _write(path, new)
                    added += len(fits)
                    removed += len([e for e in current if e not in set(new)])
                came = [digest(e) for e in fits]
                now["foreign"][name] = list(dict.fromkeys(now["foreign"][name] + came))[-MAX_FOREIGN:]
                now["base"][name] = [digest(e) for e in taken]
        if whole:
            full = ans.get("full") if isinstance(ans.get("full"), dict) else {}
            now.update(epoch=epoch, rev=rev, state="in_sync", synced_at=now_iso(), held=held, refused=refused,
                       over=over, kept=max(0, min(9999, kept)), later=max(0, min(9999, later)), conflicts=_clean_conflicts(ans.get("conflicts")), resolved=[],
                       full={n: _hashes(full.get(n)) for n in FILES}, full_rev=rev)
            if added or removed:
                now["last"] = {"at": now_iso(), "added": added, "removed": removed}
        _save(st)
    if (added or removed) and get_service is not None:
        _emit(added + removed, get_service)
    return "in_sync" if whole else "waiting"


def _set_state(tid: str, state: str, held: Optional[dict] = None, synced: bool = False) -> str:
    with contextlib.suppress(OSError):
        with _locked():
            st = _load()
            rec = st["twins"].get(tid)
            if rec is not None and rec["on"]:
                before = (rec["state"], rec["held"])
                rec["state"] = state
                if held is not None:
                    rec["held"] = held
                if synced:
                    when = parse_iso(rec["synced_at"])
                    if when is None or (now_dt() - when).total_seconds() >= 60:
                        rec["synced_at"] = now_iso()  # (written at most once a minute while nothing changes)
                        before = None
                if before != (rec["state"], rec["held"]):
                    _save(st)
    return state


def _emit(count: int, get_service: Optional[Callable] = None) -> None:
    try:
        if get_service is None:
            from . import service as _service
            get_service = _service.get_service
        get_service().emit("fleet", None, {"what": "memory", "count": int(count)})  # a count; never a note
    except Exception:
        log.debug("grok-bot-twin: could not announce a memory change")


def follow_all(get_service: Optional[Callable] = None) -> dict:
    """One exchange for every twin whose memory is switched on here. {twin id: state}."""
    out = {}
    try:
        st = _load()
    except OSError:
        return out
    for tid, rec in st["twins"].items():
        if rec["on"]:
            try:
                out[tid] = follow_once(tid, get_service)
            except Exception:
                log.debug("grok-bot-twin: a memory exchange failed here")
                out[tid] = "error"
    return out


# ---- the background look (a linked computer only; nothing runs while every switch is off) ------------------------------
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_wake = threading.Event()
_start_lock = threading.Lock()


def _dates() -> tuple:
    out = []
    try:
        fst = fleet.load()
        ln = fst.get("link") or {}
        for tid, rec in _load()["twins"].items():
            entry = (ln.get("twins") or {}).get(tid)
            home = _bot_home(entry.get("profile")) if rec["on"] and isinstance(entry, dict) else None
            for name in FILES if home is not None else ():
                try:
                    s = os.stat(_mem_path(home, name))
                    out.append((tid, name, s.st_mtime_ns, s.st_size))
                except OSError:
                    out.append((tid, name, 0, 0))
    except Exception:
        return ()
    return tuple(out)


def _loop(get_service: Callable) -> None:
    while not _stop.is_set():
        try:
            if not any_on() or not fleet.exists() or not fleet.load().get("link"):
                _wake.wait(30)
                _wake.clear()
                continue
            with fio.exclusive(store.central_dir() / "xs-memory-follow.lock") as got:
                if not got:  # another Hermes process on this computer does it
                    _wake.wait(30)
                    _wake.clear()
                    continue
                seen, last = None, 0.0
                while not _stop.is_set() and any_on():
                    cur = _dates()
                    if cur != seen or time.monotonic() - last >= POLL_S or _wake.is_set():
                        _wake.clear()
                        follow_all(get_service)
                        seen, last = _dates(), time.monotonic()
                    _wake.wait(LOOK_S)
        except Exception:
            log.debug("grok-bot-twin: the memory look stumbled")
            _wake.wait(30)
            _wake.clear()


def _listed(fst: Optional[dict]) -> set:
    """Every twin id this computer's fleet state names (also one whose Hermes bot is still being made)."""
    if not fst:
        return set()
    ln = fst.get("link")
    if isinstance(ln, dict):
        return {t for t in (ln.get("twins") or {}) if TID_RE.match(str(t))}
    return {t for t in (((fst.get("share") or {}).get("list") or {}).get("twins") or {}) if TID_RE.match(str(t))}


def tidy() -> None:
    """With the background check: memory is switched off for a twin that is no longer on this computer, and the
    earlier versions kept for such a twin are deleted thirty days after it was first seen missing (the same
    thirty days its Hermes bot is kept); a twin that comes back in time keeps them. Never raises."""
    try:
        fst = fleet.load() if fleet.exists() else None
        listed = _listed(fst)
        with _locked():
            st = _load(fst)
            before = json.dumps(st, sort_keys=True)
            for tid, rec in st["twins"].items():
                if rec["on"] and tid not in listed:
                    _reset(rec)
            away = {tid for tid in list(st["twins"]) + _point_twins() if tid not in listed}
            for tid in list(st["gone"]):
                if tid not in away:
                    del st["gone"][tid]
            cut = iso(now_dt() - timedelta(days=POINT_DAYS))
            for tid in sorted(away):
                since = st["gone"].setdefault(tid, now_iso())
                if since < cut:
                    _forget_points(tid)
                    st["twins"].pop(tid, None)
                    del st["gone"][tid]
            if json.dumps(st, sort_keys=True) != before:
                _save(st)
    except Exception:
        log.debug("grok-bot-twin: could not tidy the memory notes")


def tick(get_service: Callable) -> None:
    """Called with the fleet's own background check: start the look once memory is switched on for a twin on a
    linked computer. One small file read otherwise; never raises."""
    global _thread
    try:
        if os.path.lexists(store.central_dir() / STATE_FILE) or os.path.lexists(store.central_dir() / POINTS_DIR):
            tidy()  # (only on a computer where memory was ever switched on)
        with _start_lock:
            if _stop.is_set() or (_thread is not None and _thread.is_alive()) or not any_on():
                return
            if not fleet.exists() or not fleet.load().get("link"):
                return  # the computer that shares only answers; it has nothing to ask
            _thread = threading.Thread(target=_loop, name="grok-bot-twin-memory", daemon=True, args=(get_service,))
            _thread.start()
    except Exception:
        log.debug("grok-bot-twin: the memory look could not start")


def stop() -> None:
    _stop.set()
    _wake.set()


def _reset_for_tests() -> None:
    global _thread
    stop()
    if _thread is not None:
        _thread.join(5)
    _thread = None
    _stop.clear()
    _wake.clear()


# ---- what the page and the terminal ask ---------------------------------------------------------------------------------
def _twins_here(fst: dict) -> dict:
    """{twin id: {"name", "profile"}} of the twins this computer holds as Hermes bots."""
    role = fleet.role(fst)
    out = {}
    if role == "follower":
        for tid, e in ((fst["link"].get("twins") or {}).items()):
            if isinstance(e, dict) and TID_RE.match(str(tid)) and not e.get("making") and e.get("profile"):
                out[tid] = {"name": fleet.clean_text(e.get("name"), fleet.NAME_CAP, True) or "Twin",
                            "profile": e.get("profile")}
    elif role == "source":
        for tid, e in (((fst.get("share") or {}).get("list") or {}).get("twins") or {}).items():
            if isinstance(e, dict) and TID_RE.match(str(tid)):
                out[tid] = {"name": fleet.clean_text(e.get("name"), fleet.NAME_CAP, True) or "Twin",
                            "profile": e.get("profile")}
    return out


def _other_name(fst: dict) -> Optional[str]:
    ln = fst.get("link")
    if isinstance(ln, dict):
        return fp.machine_name((ln.get("source") or {}).get("name"))
    return None


def _shown(text: str) -> str:
    text = safe._CTRL.sub("", text)
    return text if len(text) <= SHOWN_CHARS else text[:SHOWN_CHARS - 1].rstrip() + "…"


def _twin_view(tid: str, info: dict, rec: dict, role: str, other: Optional[str], computers: Optional[list] = None) -> dict:
    home = _bot_home(info.get("profile"))
    counts, by_hash = {n: 0 for n in FILES}, {}
    for name in FILES if rec["on"] else ():  # (a twin whose memory is off: its files are not even opened)
        entries = (_read(_mem_path(home, name)) if home is not None else None) or []
        counts[name] = len(entries)
        for e in entries:
            by_hash[digest(e)] = e
    foreign = sum(len([h for h in rec["foreign"][n] if h in by_hash]) for n in FILES) if rec["on"] \
        else sum(len(rec["foreign"][n]) for n in FILES)
    held: dict = {}
    for e in by_hash.values() if rec["on"] else ():
        why = why_held(e)
        if why is not None:
            held[why] = held.get(why, 0) + 1
    full = sum(len(rec["full"][n]) for n in FILES) if rec["full_rev"] and rec["full_rev"] == rec["rev"] else 0
    state = rec["state"] if rec["on"] else "off"
    there = other or "the computer with the Grok app"
    total = counts["memory"] + counts["user"]
    if not rec["on"]:
        message = "Off. This twin's memory stays on this computer."
    elif role == "source":
        state = "on"
        message = (f"On. {_plural(total, 'note')} here. The computers below that switch this twin on too get them; "
                   "a computer you link later is out until you let it in.")
    elif state == "in_sync":
        message = f"In sync with {there}. {_plural(total, 'note')} here, {foreign} from your other computers."
    elif state == "off_there":
        message = f"Waiting: memory for this twin is switched off on {there}. Switch it on there too."
    elif state == "older_there":
        message = f"Waiting: {there} needs a newer Grok Bot Twin before memory can follow."
    elif state == "error":
        message = MESSAGES["memory_unreadable"][0]
    else:
        state = "waiting"
        message = f"Waiting for {there} to answer. This twin's memory here stays as it is."
    notes = []
    for why, n in sorted(held.items()):
        notes.append(f"{_plural(n, 'note')} here {'stays' if n == 1 else 'stay'} on this computer only: "
                     f"{'it' if n == 1 else 'each'} {HELD_WORDS[why]}.")
    if full:
        notes.append(f"{_plural(full, 'note')} from this computer {'does' if full == 1 else 'do'} not fit: this twin's "
                     f"memory on {there} is full. {'It stays' if full == 1 else 'They stay'} here.")
    if rec["over"]:
        notes.append(f"{_plural(rec['over'], 'note')} from {there} {'does' if rec['over'] == 1 else 'do'} not fit in "
                     f"this twin's memory here (it is full), so {'it was' if rec['over'] == 1 else 'they were'} left out.")
    if rec["kept"]:
        notes.append(f"{_plural(rec['kept'], 'note')} removed here came back: clearing most of a twin's memory "
                     f"within a day is done on {there}.")
    if rec["later"]:
        notes.append(f"{_plural(rec['later'], 'change')} from this computer {'waits' if rec['later'] == 1 else 'wait'}: "
                     f"{there} takes {CHANGES_PER_DAY} rounds of changes a day from one computer.")
    if rec["refused"]:
        notes.append(f"{_plural(rec['refused'], 'note')} from {there} {'was' if rec['refused'] == 1 else 'were'} not "
                     "taken in here: the privacy check on this computer held "
                     f"{'it' if rec['refused'] == 1 else 'them'} back.")
    conflicts = []
    for c in rec["conflicts"]:
        versions = [{"key": h, "text": _shown(by_hash[h])} for h in c["versions"] if h in by_hash]
        if len(versions) < (2 if c["kind"] == "both_changed" else 1):
            continue
        a, b = c["by"]
        message_c = {"both_changed": f"The same note was changed on {a} and on {b}. Both versions are kept.",
                     "removed_and_changed": f"A note was removed on {a} and changed on {b}. The changed note is kept.",
                     "changed_and_removed": f"A note was changed on {a} and removed on {b}. The changed note is kept.",
                     }[c["kind"]]
        conflicts.append({"id": c["id"], "file": FILES[c["file"]], "kind": c["kind"], "message": message_c,
                          "versions": versions, "at": c["at"]})
    restore = [{"point": pid, "at": at, "label": "The memory " + WHY_WORDS.get(p.get("why"), "as it was then")}
               for at, pid, p in reversed(_point_files(tid))]
    return {"id": tid, "name": info["name"], "profile": info.get("profile"), "on": rec["on"], "state": state,
            "message": message, "notes": notes, "count": counts, "from_others": foreign,
            "synced_at": rec["synced_at"], "last": rec["last"], "conflicts": conflicts, "restore": restore,
            "computers": computers or [], "can_forget": bool(foreign or restore or rec["on"])}


def _calm(fn: Callable) -> Callable:
    """A file that is locked, a link where a folder should be, a full disk: said in the plugin's own words, never
    as the system's message (which names paths)."""
    import functools

    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except EngineError:
            raise
        except OSError:
            raise MemoryError_("memory_busy") from None
    return inner


@_calm
def view() -> dict:
    """`GET /fleet/memory`: every twin on this computer with its memory switch, in words ready to show. Reads
    this computer's own files; no network call."""
    fst = fleet.load() if fleet.exists() else None
    role = fleet.role(fst) if fst else "none"
    out = {"role": role, "other": _other_name(fst) if fst else None, "sentence": SENTENCE, "what": list(WHAT),
           "twins": []}
    if fst is None or role == "none":
        out["sentence"] = ("A twin's memory stays on this computer. Once this computer is linked to another one, "
                           "you can switch a twin's memory on to keep it the same on both.")
        out["left"] = [{"id": tid, "points": len(_point_files(tid))} for tid in _point_twins()]
        return out
    with _locked():
        st = _load(fst)
        if st.get("_moved"):
            _save(st)
    here = _twins_here(fst)
    linked = []
    if role == "source":  # the computers linked to this one: the owner can keep one out, twin by twin
        linked = sorted((m for m in (fleet.clean_machine(f) for f in ((fst.get("share") or {}).get("followers") or {}).values())
                         if m), key=lambda m: (m["name"].lower(), m["id"]))
    for tid, info in sorted(here.items(), key=lambda kv: (kv[1]["name"].lower(), kv[0])):
        rec = st["twins"].get(tid) or _blank_twin()
        computers = [{"id": m["id"], "name": m["name"], "on": m["id"] in rec["with"]} for m in linked] \
            if rec["on"] else []
        out["twins"].append(_twin_view(tid, info, rec, role, out["other"], computers))
    # what this module still keeps for a twin that is no longer on this computer (earlier versions, for 30 days)
    out["left"] = [{"id": tid, "points": len(_point_files(tid))} for tid in _point_twins() if tid not in here]
    return out


def _find(fst: dict, word: Any) -> tuple:
    """(twin id, info) for an id or a name."""
    here = _twins_here(fst)
    if isinstance(word, str):
        if word in here:
            return word, here[word]
        hits = [(tid, i) for tid, i in here.items() if i["name"].lower() == word.strip().lower()]
        if len(hits) == 1:
            return hits[0]
    raise MemoryError_("unknown_twin")


@_calm
def switch(twin: Any, on: Any, get_service: Optional[Callable] = None) -> dict:
    """`POST /fleet/memory` {"twin", "on"}: this twin's memory switch on this computer. Switching on keeps the
    file as it is now as the first restore point; switching off stops the exchange and changes no note."""
    if not isinstance(on, bool):
        raise EngineError("bad_input", "That request was not understood.")
    if not fleet.exists():
        raise MemoryError_("not_linked")
    fst = fleet.load()
    if fleet.role(fst) == "none":
        raise MemoryError_("not_linked")
    tid, info = _find(fst, twin)
    home = _bot_home(info.get("profile"))
    if on and home is None:
        raise MemoryError_("no_bot")
    with _locked():
        st = _load(fst)
        rec = _twin(st, tid)
        if on and not rec["on"]:
            if any(_read(_mem_path(home, n)) is None for n in FILES):
                raise MemoryError_("memory_unreadable")
            save_point(tid, home, "first")
            _reset(rec)
            rec.update(on=True, since=now_iso(), state="on" if fleet.role(fst) == "source" else "waiting",
                       epoch=_random.token_hex(8) if fleet.role(fst) == "source" else None)
            if fleet.role(fst) == "source" and not rec["chosen"]:
                # the first time: the computers linked right now take part (the page lists them, each with its
                # switch). One linked later, or linked anew, is out until the owner lets it in
                rec["with"] = sorted(m for m in ((fst.get("share") or {}).get("followers") or {})
                                     if fleet.MACHINE_ID_RE.match(str(m)))
                rec["chosen"] = True
        elif not on and rec["on"]:
            _reset(rec)
        _save(st)
    if on and fleet.role(fst) == "follower":
        with contextlib.suppress(Exception):
            follow_once(tid, get_service)
        _wake.set()
        if get_service is not None:
            tick(get_service)
    return view()


@_calm
def forget(twin: Any) -> dict:
    """`POST /fleet/memory/forget` {"twin"}: one click. The notes that came from the owner's other computers
    leave this twin's memory on this computer, the restore points go, the switch is off. Notes written on this
    computer stay, and nothing changes anywhere else."""
    fst = fleet.load() if fleet.exists() else None
    try:
        tid, info = _find(fst, twin) if fst is not None else (None, None)
    except MemoryError_:
        tid, info = None, None
    if tid is None:
        # a twin that is no longer on this computer (removed, or this computer was unlinked): what this module
        # still keeps for it goes, by its id
        if not (isinstance(twin, str) and TID_RE.match(twin) and (twin in _point_twins() or twin in _load()["twins"])):
            raise MemoryError_("unknown_twin")
        tid, info = twin, {}
    home = _bot_home(info.get("profile"))
    gone = 0
    with _locked():
        st = _load(fst)
        rec = _twin(st, tid)
        for name in FILES if home is not None else ():
            path = _mem_path(home, name)
            drop = set(rec["foreign"][name])
            if not drop:
                continue
            with _mem_lock(path) as got:
                current = _read(path) if got else None
                if current is None:
                    raise MemoryError_("memory_busy" if not got else "memory_unreadable")
                new = [e for e in current if digest(e) not in drop]
                if new != current:
                    _write(path, new)
                    gone += len(current) - len(new)
        points = _forget_points(tid)
        st["twins"].pop(tid, None)
        _save(st)
    out = view()
    out["forgot"] = {"twin": tid, "notes": gone, "points": points,
                     "sentence": f"Forgotten on this computer: {_plural(gone, 'note')} from your other computers and "
                                 f"{_plural(points, 'earlier version')} kept here. Your own notes here stay, and nothing changed "
                                 "on your other computers."}
    return out


@_calm
def computer(twin: Any, machine: Any, on: Any) -> dict:
    """`POST /fleet/memory/computer` {"twin", "machine", "on"} on the computer that shares: whether one linked
    computer may take part in this twin's memory. Off: that computer is answered exactly as if memory were
    switched off here (what it already holds stays there until it is forgotten there)."""
    if not isinstance(on, bool):
        raise EngineError("bad_input", "That request was not understood.")
    if not fleet.exists():
        raise MemoryError_("not_linked")
    fst = fleet.load()
    if fleet.role(fst) != "source":
        raise MemoryError_("not_sharing_here")
    tid, _info = _find(fst, twin)
    known = {m["id"]: m for m in (fleet.clean_machine(f) for f in ((fst.get("share") or {}).get("followers") or {}).values()) if m}
    hit = [m for m in known.values() if machine == m["id"] or (isinstance(machine, str) and m["name"].lower() == machine.strip().lower())]
    if len(hit) != 1:
        raise MemoryError_("unknown_machine")
    with _locked():
        st = _load(fst)
        rec = _twin(st, tid)
        rec["with"] = [m for m in rec["with"] if m != hit[0]["id"] and m in known]
        if on:
            rec["with"].append(hit[0]["id"])
        rec["chosen"] = True
        _save(st)
    return view()


@_calm
def restore(twin: Any, point: Any = None) -> dict:
    """`POST /fleet/memory/restore` {"twin", "point"?}: put this twin's memory files back as a restore point has
    them (the newest when none is named). The switch goes off here first, so the other computers keep theirs."""
    if not fleet.exists():
        raise MemoryError_("not_linked")
    fst = fleet.load()
    tid, info = _find(fst, twin)
    home = _bot_home(info.get("profile"))
    points = _point_files(tid)
    if point is None and points:
        point = points[-1][1]
    hit = next((p for _at, pid, p in points if pid == point), None)
    if hit is None or home is None:
        raise MemoryError_("unknown_point")
    with _locked():
        st = _load(fst)
        rec = _twin(st, tid)
        _reset(rec)
        _save(st)
        save_point(tid, home, "put_back")
        for name in FILES:
            entries = hit["files"].get(name)
            if not isinstance(entries, list):
                continue
            entries = [e for e in entries if isinstance(e, str) and e.strip() and DELIM not in e]
            path = _mem_path(home, name)
            with _mem_lock(path) as got:
                current = _read(path) if got else None
                if current is None:
                    raise MemoryError_("memory_busy" if not got else "memory_unreadable")
                if entries != current:
                    _write(path, entries)
    out = view()
    out["restored"] = {"twin": tid, "point": point,
                       "sentence": "Put back. Memory for this twin is now switched off on this computer, so your "
                                   "other computers keep theirs. Switch it on again to bring their notes back."}
    return out


@_calm
def resolve(twin: Any, conflict: Any, keep: Any) -> dict:
    """`POST /fleet/memory/resolve` {"twin", "conflict", "keep"}: settle a conflict. `keep` is "both" or the key
    of the version to keep; the other versions are removed here (after a restore point) and, through the
    ordinary exchange, on the other computers."""
    if not fleet.exists():
        raise MemoryError_("not_linked")
    fst = fleet.load()
    tid, info = _find(fst, twin)
    home = _bot_home(info.get("profile"))
    with _locked():
        st = _load(fst)
        rec = _twin(st, tid)
        c = next((c for c in rec["conflicts"] if c["id"] == conflict), None)
        if c is None or home is None:
            raise MemoryError_("unknown_conflict")
        if keep != "both":
            if keep not in c["versions"]:
                raise MemoryError_("unknown_conflict")
            drop = {h for h in c["versions"] if h != keep}
            path = _mem_path(home, c["file"])
            with _mem_lock(path) as got:
                current = _read(path) if got else None
                if current is None:
                    raise MemoryError_("memory_busy" if not got else "memory_unreadable")
                new = [e for e in current if digest(e) not in drop]
                if new != current:
                    save_point(tid, home, "settled")
                    _write(path, new)
        rec["conflicts"] = [x for x in rec["conflicts"] if x["id"] != c["id"]]
        if fleet.role(fst) == "follower":
            rec["resolved"] = (rec["resolved"] + [c["id"]])[-MAX_CONFLICTS:]
        _save(st)
    _wake.set()
    return view()


# ---- terminal: `hermes twin fleet memory ...` ----------------------------------------------------------------------------
USAGE = ("hermes twin fleet memory                      which twins share their memory, in words\n"
         "hermes twin fleet memory TWIN on|off          this twin's memory switch on this computer\n"
         "hermes twin fleet memory TWIN forget          take out what came from your other computers; switch off\n"
         "hermes twin fleet memory TWIN restore [POINT] put the memory back as it was; switch off\n"
         "hermes twin fleet memory TWIN keep ID both|KEY  settle a conflict\n"
         "hermes twin fleet memory TWIN with COMPUTER   on the Mac: let a linked computer take part (without: keep it out)\n"
         "hermes twin fleet memory once                 one exchange now (a linked computer)")


def describe(v: dict) -> list:
    lines = [v["sentence"]]
    for t in v["twins"]:
        lines.append(f"  {t['name']}: {t['message']}")
        for n in t["notes"]:
            lines.append(f"      {n}")
        for c in t["conflicts"]:
            lines.append(f"      Conflict {c['id']} in {c['file']}: {c['message']}")
            for ver in c["versions"]:
                lines.append(f"        {ver['key']}  {ver['text']}")
        if t["restore"]:
            lines.append(f"      Earlier versions kept here: {len(t['restore'])} (newest {t['restore'][0]['point']}, "
                         f"{t['restore'][0]['at']}).")
        if t.get("computers"):
            lines.append("      Takes part: " + (", ".join(c["name"] for c in t["computers"] if c["on"]) or "no computer yet")
                         + ". Kept out: " + (", ".join(c["name"] for c in t["computers"] if not c["on"]) or "none") + ".")
    for left in v.get("left", []):
        lines.append(f"  Earlier versions of a twin that is no longer here: {left['points']} "
                     f"(forget them with: hermes twin fleet memory {left['id']} forget)")
    if v["twins"]:
        lines += [""] + [f"  {w}" for w in v["what"]]
    return lines


def summary_line() -> str:
    """One line for `hermes twin fleet`."""
    try:
        v = view()
    except Exception:
        return ""
    if v["role"] == "none" or not v["twins"]:
        return ""
    on = [t["name"] for t in v["twins"] if t["on"]]
    if not on:
        return "Memory: stays on each computer (switch a twin on with: hermes twin fleet memory TWIN on)."
    return f"Memory that follows: {', '.join(on)}. Encrypted between your computers; passwords and keys never."


def cli(words: list, as_json: bool, out: Callable[[str], None], get_service: Optional[Callable] = None) -> int:
    def show(v: dict) -> int:
        if as_json:
            out(json.dumps(v, indent=1, ensure_ascii=False))
            return 0
        for key in ("forgot", "restored"):
            if key in v:
                out(v[key]["sentence"])
        for line in describe(v):
            out(line)
        return 0

    if not words:
        return show(view())
    if words == ["once"]:
        states = follow_all(get_service)
        show(view())
        return 0 if states and all(s == "in_sync" for s in states.values()) else 1
    if len(words) >= 2 and words[1] in ("on", "off"):
        return show(switch(words[0], words[1] == "on", get_service))
    if len(words) == 2 and words[1] == "forget":
        return show(forget(words[0]))
    if len(words) in (2, 3) and words[1] == "restore":
        return show(restore(words[0], words[2] if len(words) == 3 else None))
    if len(words) == 4 and words[1] == "keep":
        return show(resolve(words[0], words[2], words[3]))
    if len(words) == 3 and words[1] in ("with", "without"):  # the Mac: let a linked computer take part, or keep it out
        return show(computer(words[0], words[2], words[1] == "with"))
    raise EngineError("usage", "Use it like this:", USAGE)
