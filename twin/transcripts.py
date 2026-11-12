"""Full transcripts (memory layer 3): an archive of a twin's conversations, on this computer, off until switched on.

What it keeps: every conversation of a twin, both sides (the Grok bot's, from a Grok export the owner or the Mirror
saved; the Hermes twin's, read from the twin's own Hermes session database), message by message (user and
assistant turns; tool output and hidden reasoning are not kept).

Where: one SQLite file in the plugin's data folder (`transcripts/archive.db`) with a full-text index (FTS5).

Encrypted at rest with the same construction as the 0.2 memory sync's sealed box (xs_memory.seal: a SHAKE-256
keystream XORed over the text, then HMAC-SHA256 over nonce + ciphertext, the tag checked in constant time before
anything is decrypted). That is the plugin's own encrypt-then-MAC construction from standard primitives, not a
packaged AEAD such as AES-GCM (the plugin adds no dependency to Hermes). The key is a random 32 bytes made when the
first twin is switched on and kept OUTSIDE the archive's folder (`keys/transcripts.key`, a folder and file only the
owner's user can open), so a copy of `transcripts/` alone cannot be read. Each row's tag also binds which twin,
conversation and position it belongs to, so rows cannot be moved between conversations unnoticed. Message text and
titles are encrypted; the search index holds no words: each word is replaced by a keyed hash (HMAC with a per-twin
key derived from the same key) before it is indexed, and conversation ids are keyed hashes too.

Honest limits (also in docs/DISCLOSURE.md): someone who can read the owner's files, or a copy of the whole plugin
data folder, has the key as well, so this protects copies of the archive, not the owner's account. A copy without
the key still shows the twin's name on each row, how many conversations and messages there are, their roles and
times, message lengths (rounded up to 64 bytes), and which messages share a word and how often (the keyed hashes are
the same for the same word), never the words themselves.

Opt-in per twin (`switch`). Off: nothing is read, nothing is kept, the search tool answers that it is off.
"Forget" deletes everything kept for a twin. Grok direction: what the Hermes twin said is never offered to the
Grok side (`for_grok`) unless the owner switches "send to Grok" on for that twin; it is off by default and its
switch is separate from the archive's.

Export: `export_json` writes the twin's archive, decrypted, as JSON (the owner's own copy). `import_to_hermes`
makes Grok-side conversations into ordinary Hermes sessions of the twin, through Hermes' own importer
(`hermes -p <twin> sessions import --from claude <file>`, Hermes' documented import of a JSONL chat log), then
renames each to "From Grok: <first line>". A Hermes session made that way is not archived again as a Hermes-side one.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets as _rand
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import fleet_io as fio
from . import osflags
from . import store
from .xs_memory import _xor

FOLDER = "transcripts"
DB_NAME = "archive.db"
KEY_DIR = "keys"  # beside, never inside, the archive's folder
KEY_NAME = "transcripts.key"
LEGACY_KEY = "key"  # where archives made before kept it (inside transcripts/): moved out on first use
SETTINGS = "settings.json"
OUTBOX = "outbox"
EXPORTS = "exports"
FORMAT = "grok-bot-twin-transcripts"
FORMAT_V = 1
SIDES = ("grok", "hermes")
ROLES = ("user", "assistant")
PAD = 64  # a sealed message is filled up to a multiple of this
MAX_CONVS = 2000  # conversations of one ingest
MAX_MSGS = 5000  # messages of one conversation
MSG_CHARS = 100_000
MAX_FILE_BYTES = 64 * 1024 * 1024  # a Grok export read from a file
STALE_OUTBOX_S = 600.0  # an import file older than this was left by a stopped run (an import takes at most 60 s each)
TITLE_CHARS = 200
WORD_CHARS = 64
MAX_QUERY_WORDS = 12
MAX_HITS = 20
SNIPPET = 240
IMPORT_TITLE = "From Grok: "
HERMES_DB = "state.db"
IMPORT_TIMEOUT = 60.0
PID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
CONV_RE = re.compile(r"^c-[0-9a-f]{16}\Z")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
# hidden reasoning some models write into the reply itself: never kept
_REASONING_RE = re.compile(r"<(think|thinking|reasoning)>.*?(</\1>|\Z)", re.S | re.I)
# the only platforms whose chats count as held on this computer (slash.LOCAL_PLATFORMS without the empty name):
# a chat whose source is missing or unknown is never offered to Grok
LOCAL_SOURCES = frozenset({"local", "cli", "tui", "desktop"})

SENTENCE = ("Full transcripts stay on this computer, encrypted, and are off until you switch them on for a twin. "
            "What your Hermes twin says is never sent to Grok unless you also switch that on.")

MESSAGES = {
    "off": "Full transcripts are off for this twin. Switch them on with `hermes twin transcripts on TWIN` or in its What syncs panel.",
    "to_grok_off": "Sending Hermes transcripts to Grok is off for this twin.",
    "bad_twin": "No twin by that name; `hermes twin` lists them.",
    "bad_input": "That transcript file could not be read: it is not a Grok export with conversations.",
    "no_key": "The transcript archive's key is missing, so what was kept cannot be read. Forget and start again.",
    "tampered": "A kept message failed its check (the archive was changed outside the plugin); it was skipped.",
    "no_hermes": "Hermes' own session import is not available here; the conversations were written to a file instead.",
    "nothing": "No Grok conversations kept for this twin yet.",
    "exists": "A file is already there; name a new file for the export.",
    "too_big": "That file is too big to be a Grok export this archive takes (64 MB at most).",
    "needs_mirror": "Giving this twin's Hermes conversations to Grok needs the Mirror; it is not connected here.",
}


class TranscriptsError(Exception):
    def __init__(self, code: str, message: Optional[str] = None):
        super().__init__(message or MESSAGES.get(code, code))
        self.code = code
        self.message = message or MESSAGES.get(code, code)


_LOCK = threading.RLock()


# ---- places ------------------------------------------------------------------------------------------------------------
def _dir() -> Path:
    """The archive's folder, not made: reading (is it on? how much is kept?) never creates `transcripts/`."""
    return store.central_dir() / FOLDER


def _private_dir(d: Path) -> Path:
    """Made if missing and owner-only; a failure to make it so is not swallowed (then nothing is written in it)."""
    store.ensure_dir(d)
    if os.name != "nt":
        os.chmod(d, 0o700)
    return d


def folder() -> Path:
    return _private_dir(_dir())


def _key_dir() -> Path:
    return store.central_dir() / KEY_DIR


def _check_pid(pid: Any) -> str:
    if not isinstance(pid, str) or not PID_RE.match(pid):
        raise TranscriptsError("bad_twin")
    return pid


# ---- settings (per twin) ---------------------------------------------------------------------------------------------
def _blank() -> dict:
    return {"on": False, "to_grok": False, "since": None, "hermes_seen": {}, "imported": {}}


def _settings() -> dict:
    raw = fio.read_json(_dir() / SETTINGS, None)
    out = {"v": 1, "twins": {}}
    if isinstance(raw, dict) and isinstance(raw.get("twins"), dict):
        for pid, t in list(raw["twins"].items())[:1000]:
            if isinstance(pid, str) and PID_RE.match(pid) and isinstance(t, dict):
                rec = _blank()
                rec["on"] = t.get("on") is True
                rec["to_grok"] = t.get("to_grok") is True and rec["on"]
                rec["since"] = t.get("since") if isinstance(t.get("since"), str) else None
                for k in ("hermes_seen", "imported"):
                    v = t.get(k)
                    rec[k] = {str(a)[:128]: str(b)[:128] for a, b in list(v.items())[:20000]} \
                        if isinstance(v, dict) else {}
                out["twins"][pid] = rec
    return out


def _save_settings(st: dict) -> None:
    fio.write_json(folder() / SETTINGS, st)


def imported_sessions() -> set:
    """The Hermes session ids this archive made from Grok conversations (import_to_hermes), every twin's: chats that
    are Grok's words, not the owner's in Hermes (session briefs pass them over). Reads only; makes nothing. An error
    is raised, never taken for "none imported" (that would let a Grok chat pass for the owner's own)."""
    return {sid for rec in _settings()["twins"].values() for sid in rec["imported"].values()}


def twin_settings(pid: str) -> dict:
    return _settings()["twins"].get(_check_pid(pid)) or _blank()


def is_on(pid: str) -> bool:
    try:
        return twin_settings(pid)["on"]
    except TranscriptsError:
        return False


# ---- the key and the sealed rows (the 0.2 memory box, at rest) --------------------------------------------------------
def _read_key(path: Path) -> Optional[bytes]:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    return data if len(data) == 32 else None


def _move_legacy_key() -> None:
    """An archive made before kept its key inside transcripts/: move it out, beside (never into) that folder."""
    old = _dir() / LEGACY_KEY
    if _read_key(old) is None:
        return
    new = _private_dir(_key_dir()) / KEY_NAME
    if _read_key(new) is None:
        osflags.replace(old, new)
        if os.name != "nt":
            os.chmod(new, 0o600)
    else:
        os.unlink(old)


def _key(create: bool = False) -> Optional[bytes]:
    _move_legacy_key()
    path = (_private_dir(_key_dir()) if create else _key_dir()) / KEY_NAME
    data = _read_key(path)
    if data is not None or not create:
        return data
    if path.exists() or path.is_symlink():  # a key file that is not a key: never replaced (its archive is unreadable)
        raise TranscriptsError("no_key")
    key = _rand.token_bytes(32)
    tmp = path.with_name(f".{KEY_NAME}.{_rand.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, path)  # never replaces a key another thread made a moment ago
    except FileExistsError:
        key = None
    finally:
        os.unlink(tmp)
    if key is None:
        key = _key(False)
    if key is None:  # never "on" without a key that opens what it keeps
        raise TranscriptsError("no_key")
    return key


def _subkeys(key: bytes) -> tuple:
    return (hmac.new(key, b"grok-bot-twin transcripts 1 enc", hashlib.sha256).digest(),
            hmac.new(key, b"grok-bot-twin transcripts 1 mac", hashlib.sha256).digest(),
            hmac.new(key, b"grok-bot-twin transcripts 1 words", hashlib.sha256).digest())


def _twin_words_key(key: bytes, pid: str) -> bytes:
    """The blind index's key for one twin: the same word gives different index entries in two twins."""
    return hmac.new(_subkeys(key)[2], b"twin\n" + pid.encode("utf-8"), hashlib.sha256).digest()


def _bind(pid: str, conv: str, seq: int, what: str) -> bytes:
    return f"grok-bot-twin transcript row 1\n{pid}\n{conv}\n{seq}\n{what}\n".encode("utf-8")


def seal_row(key: bytes, bind: bytes, text: str) -> bytes:
    """nonce(24) + ciphertext + tag(32). The text is length-prefixed and padded, then XORed with SHAKE-256 of
    (enc key + nonce), then authenticated with HMAC-SHA256 over (bind + nonce + ciphertext): xs_memory.seal's box."""
    enc, mac, _ = _subkeys(key)
    data = text.encode("utf-8", "replace")
    body = len(data).to_bytes(4, "big") + data
    body += b"\0" * (-len(body) % PAD)
    nonce = os.urandom(24)
    box = _xor(body, hashlib.shake_256(enc + nonce).digest(len(body)))
    return nonce + box + hmac.new(mac, bind + nonce + box, hashlib.sha256).digest()


def open_row(key: bytes, bind: bytes, blob: Any) -> Optional[str]:
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 24 + PAD + 32 or (len(blob) - 56) % PAD:
        return None
    enc, mac, _ = _subkeys(key)
    nonce, box, tag = bytes(blob[:24]), bytes(blob[24:-32]), bytes(blob[-32:])
    if not hmac.compare_digest(hmac.new(mac, bind + nonce + box, hashlib.sha256).digest(), tag):
        return None
    body = _xor(box, hashlib.shake_256(enc + nonce).digest(len(box)))
    size = int.from_bytes(body[:4], "big")
    if size > len(body) - 4:
        return None
    try:
        return body[4:4 + size].decode("utf-8")
    except UnicodeDecodeError:
        return None


# ---- the blind word index ---------------------------------------------------------------------------------------------
def words(text: str) -> list:
    norm = unicodedata.normalize("NFKC", text or "").casefold()
    return [w[:WORD_CHARS] for w in _WORD_RE.findall(norm)]


def _blind(words_key: bytes, word: str) -> str:
    return "w" + hmac.new(words_key, word.encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def _index_text(words_key: bytes, text: str) -> str:
    return " ".join(_blind(words_key, w) for w in words(text))


def _query(words_key: bytes, q: str) -> Optional[str]:
    """The owner's (or the twin's) query as an FTS5 expression over blind words: plain words must all appear,
    "quoted words" must appear together in that order, a word with a leading minus must not appear."""
    if not isinstance(q, str):
        return None
    q = q[:500]
    must, phrases, nots = [], [], []
    for m in re.finditer(r'"([^"]*)"|(-?)([^\s"]+)', q):
        if m.group(1) is not None:
            ws = words(m.group(1))
            if ws:
                phrases.append(ws)
        else:
            ws = words(m.group(3))
            (nots if m.group(2) else must).extend(ws)
    n = len(must) + sum(len(p) for p in phrases)
    if n == 0 or n + len(nots) > MAX_QUERY_WORDS:
        return None
    parts = [_blind(words_key, w) for w in must]
    parts += ['"' + " ".join(_blind(words_key, w) for w in p) + '"' for p in phrases]
    expr = " AND ".join(parts)
    for w in nots:
        expr += " NOT " + _blind(words_key, w)
    return expr


# ---- the database ------------------------------------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, pid TEXT NOT NULL, side TEXT NOT NULL, origin TEXT NOT NULL,
  started REAL, updated REAL, n INTEGER NOT NULL DEFAULT 0, title BLOB, digest TEXT,
  UNIQUE (pid, side, origin));
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, conv TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, role TEXT NOT NULL, at REAL, body BLOB NOT NULL, UNIQUE (conv, seq));
CREATE VIRTUAL TABLE IF NOT EXISTS message_words USING fts5(w, tokenize='ascii');
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


@contextlib.contextmanager
def _db():
    path = folder() / DB_NAME
    if path.is_symlink() or (path.exists() and not path.is_file()):  # never a link to some other file
        raise TranscriptsError("no_key", "The transcript archive is not a plain file; it was left alone.")
    new = not path.exists()
    if new:  # made owner-only from the start (as the key file), never left to the umask, never through a link
        os.close(osflags.open_nofollow(str(path), os.O_WRONLY | os.O_CREAT, 0o600))
    conn = sqlite3.connect(str(path), timeout=10.0)
    try:
        if new or (path.stat().st_mode & 0o077):  # owner-only, and a failure is not swallowed: no archive then
            os.chmod(path, 0o600)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA secure_delete = ON")  # forgotten rows are overwritten, not left in free pages
        conn.executescript(_SCHEMA)
        if "local" not in {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}:
            # 1 = a Hermes chat held on this computer (terminal, terminal window, Desktop); archives made before have
            # none until the next refresh, and only a 1 is ever offered to Grok (for_grok)
            conn.execute("ALTER TABLE conversations ADD COLUMN local INTEGER")
        conn.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('format', ?)", (f"{FORMAT} {FORMAT_V}",))
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _conv_id(pid: str, side: str, origin: str) -> str:
    return "c-" + hashlib.sha256(f"{pid}\n{side}\n{origin}".encode("utf-8")).hexdigest()[:16]


def _origin(key: bytes, raw_id: Any) -> str:
    """The conversation's own id (a Grok export's, a Hermes session's) as a keyed hash: a copy of the archive without
    the key cannot be checked against a guessed id."""
    return hmac.new(_subkeys(key)[2], b"origin\n" + str(raw_id).encode("utf-8", "surrogatepass"),
                    hashlib.sha256).hexdigest()[:32]


def _num_time(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and 0 <= v < 1e11:
        return float(v)
    if isinstance(v, (int, float)) and 1e11 <= v < 1e14:  # milliseconds
        return float(v) / 1000.0
    if isinstance(v, str) and v:
        from .model import parse_iso
        dt = parse_iso(v)
        if dt:
            return dt.timestamp()
    return None


def _clean_text(v: Any, cap: int) -> str:
    if not isinstance(v, str):
        return ""
    v = v.replace("\x00", "")[:cap]
    return v.encode("utf-8", "replace").decode("utf-8")  # a lone surrogate becomes "?" (it could not be kept)


def _store_conversation(conn: sqlite3.Connection, key: bytes, pid: str, side: str, origin: str, title: str,
                        msgs: list) -> bool:
    """Put one conversation in (whole: its old messages are replaced). True when anything changed."""
    wkey = _twin_words_key(key, pid)
    cid = _conv_id(pid, side, origin)
    # a keyed digest of the whole conversation (so it says nothing without the key): any edit is seen
    dig = hmac.new(wkey, json.dumps([title, msgs], ensure_ascii=False, sort_keys=True).encode("utf-8", "replace"),
                   hashlib.sha256).hexdigest()[:32]
    row = conn.execute("SELECT digest FROM conversations WHERE id = ?", (cid,)).fetchone()
    if row and row[0] == dig:
        return False
    last = max((m["at"] for m in msgs if m["at"] is not None), default=None)
    old = [r[0] for r in conn.execute("SELECT id FROM messages WHERE conv = ?", (cid,))]
    for mid in old:
        conn.execute("DELETE FROM message_words WHERE rowid = ?", (mid,))
    conn.execute("DELETE FROM messages WHERE conv = ?", (cid,))
    started = min((m["at"] for m in msgs if m["at"] is not None), default=None)
    conn.execute("INSERT INTO conversations (id, pid, side, origin, started, updated, n, title, digest) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET started=excluded.started, "
                 "updated=excluded.updated, n=excluded.n, title=excluded.title, digest=excluded.digest",
                 (cid, pid, side, origin, started, last, len(msgs),
                  seal_row(key, _bind(pid, cid, -1, "title"), title), dig))
    for seq, m in enumerate(msgs):
        cur = conn.execute("INSERT INTO messages (conv, seq, role, at, body) VALUES (?, ?, ?, ?, ?)",
                           (cid, seq, m["role"], m["at"],
                            seal_row(key, _bind(pid, cid, seq, m["role"]), m["text"])))
        conn.execute("INSERT INTO message_words (rowid, w) VALUES (?, ?)",
                     (cur.lastrowid, _index_text(wkey, m["text"])))
    return True


def _norm_messages(raw: Any) -> list:
    out = []
    if not isinstance(raw, list):
        return out
    for m in raw[:MAX_MSGS]:
        if not isinstance(m, dict):
            continue
        role = m.get("role") if m.get("role") in ROLES else {"human": "user", "bot": "assistant", "grok": "assistant",
                                                                 "model": "assistant"}.get(str(m.get("role")).lower())
        text = m.get("text") if isinstance(m.get("text"), str) else m.get("content")
        if isinstance(text, list):  # content blocks
            text = "\n\n".join(b.get("text", "") for b in text if isinstance(b, dict) and isinstance(b.get("text"), str))
        text = _clean_text(text, MSG_CHARS)
        if role == "assistant":
            text = _REASONING_RE.sub("", text)
        text = text.strip()
        if role and text:
            out.append({"role": role, "text": text,
                        "at": _num_time(next((m.get(k) for k in ("at", "timestamp", "time") if m.get(k) is not None), None))})
    return out


def _title_of(title: Any, msgs: list) -> str:
    t = _clean_text(title, TITLE_CHARS).strip()
    if t:
        return t
    first = next((m["text"] for m in msgs if m["role"] == "user"), "")
    return first.strip().partition("\n")[0][:TITLE_CHARS] or "Conversation"


# ---- switching ---------------------------------------------------------------------------------------------------------
def switch(pid: str, on: bool) -> dict:
    """Switch the archive on or off for one twin. Off keeps what was kept (forget() deletes it); "send to Grok"
    always goes off with it."""
    pid = _check_pid(pid)
    with _LOCK:
        st = _settings()
        rec = st["twins"].setdefault(pid, _blank())
        if on is True:
            _key(create=True)
            if not rec["on"]:
                rec["since"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            rec["on"] = True
        else:
            rec["on"] = False
            rec["to_grok"] = False
        _save_settings(st)
        return view(pid)


def switch_to_grok(pid: str, on: bool) -> dict:
    """The separate, default-off switch that lets what the Hermes twin said be offered to the Grok side."""
    pid = _check_pid(pid)
    with _LOCK:
        st = _settings()
        rec = st["twins"].setdefault(pid, _blank())
        if on is True and not rec["on"]:
            raise TranscriptsError("off")
        rec["to_grok"] = on is True
        _save_settings(st)
        return view(pid)


def forget(pid: str) -> dict:
    """Delete everything kept for this twin (its rows, their index entries, its bookkeeping, the decrypted exports
    and import files the plugin wrote for it in its own folder) and switch it off."""
    pid = _check_pid(pid)
    with _LOCK:
        removed = 0
        if (_dir() / DB_NAME).exists():
            with _db() as conn:
                _clear_outbox(_conv_ids(conn, pid))
                ids = [r[0] for r in conn.execute(
                    "SELECT m.id FROM messages m JOIN conversations c ON c.id = m.conv WHERE c.pid = ?", (pid,))]
                for mid in ids:
                    conn.execute("DELETE FROM message_words WHERE rowid = ?", (mid,))
                conn.execute("DELETE FROM messages WHERE conv IN (SELECT id FROM conversations WHERE pid = ?)", (pid,))
                removed = conn.execute("DELETE FROM conversations WHERE pid = ?", (pid,)).rowcount
            with contextlib.suppress(sqlite3.Error):
                c = sqlite3.connect(str(folder() / DB_NAME))
                try:
                    c.execute("INSERT INTO message_words (message_words) VALUES ('optimize')")
                    c.commit()
                    c.execute("VACUUM")
                finally:
                    c.close()
        _clear_stale_outbox()
        mine = re.compile(r"^transcripts-" + re.escape(pid) + r"-[0-9]{8}-[0-9]{6}\.json\Z")
        _own_files(_dir() / EXPORTS, mine.match)  # the decrypted exports it wrote (a --file one is the owner's own)
        st = _settings()
        if st["twins"].pop(pid, None) is not None:
            _save_settings(st)
        if not st["twins"]:
            _drop_empty_archive()
        return {"pid": pid, "forgotten": removed, "on": False}


def _drop_empty_archive() -> None:
    """No twin keeps anything any more: the archive file and its key go too (a later "on" makes new ones)."""
    db = _dir() / DB_NAME
    if db.exists():
        with _db() as conn:
            if conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]:
                return
    for p in (db, db.with_name(DB_NAME + "-journal"), db.with_name(DB_NAME + "-wal"), db.with_name(DB_NAME + "-shm"),
              _key_dir() / KEY_NAME, _dir() / LEGACY_KEY):
        if os.path.lexists(p) and not os.path.isdir(p):
            os.unlink(p)


def _conv_ids(conn: sqlite3.Connection, pid: str) -> set:
    return {r[0] for r in conn.execute("SELECT id FROM conversations WHERE pid = ?", (pid,))}


def _own_files(where: Path, wanted: Callable) -> int:
    """Delete the plain files directly in `where` whose names `wanted` accepts, each by its exact path (never a
    pattern handed to the system, never a link followed, nothing in a subfolder)."""
    gone = 0
    try:
        entries = list(os.scandir(where))
    except OSError:
        return 0
    for e in entries:
        with contextlib.suppress(OSError):
            if wanted(e.name) and e.is_file(follow_symlinks=False):
                os.unlink(os.path.join(where, e.name))
                gone += 1
    return gone


_OUTBOX_RE = re.compile(r"^(c-[0-9a-f]{16})-[0-9a-f]{8}\.jsonl\Z")


def _clear_outbox(cids: set) -> int:
    """Import files left in the outbox for these conversations (a run that was stopped half way)."""
    def wanted(name: str) -> bool:
        m = _OUTBOX_RE.match(name)
        return bool(m) and m.group(1) in cids
    return _own_files(_dir() / OUTBOX, wanted)


def _clear_stale_outbox() -> int:
    """Any twin's import file older than an import can take: left by a run that was killed half way."""
    now = time.time()

    def wanted(name: str) -> bool:
        if not _OUTBOX_RE.match(name):
            return False
        try:
            return now - os.lstat(_dir() / OUTBOX / name).st_mtime > STALE_OUTBOX_S
        except OSError:
            return False
    return _own_files(_dir() / OUTBOX, wanted)


def _need_on(pid: str) -> tuple:
    pid = _check_pid(pid)
    rec = twin_settings(pid)
    if not rec["on"]:
        raise TranscriptsError("off")
    key = _key(False)
    if key is None:
        raise TranscriptsError("no_key")
    return pid, rec, key


# ---- taking conversations in ------------------------------------------------------------------------------------------
def ingest_grok(pid: str, data: Any) -> dict:
    """Keep the Grok side's conversations from a Grok export: {"conversations": [{"id", "title", "messages":
    [{"role": "user"|"assistant", "text", "at"}]}]} (the bot's own export, saved by the owner or the Mirror)."""
    pid, _rec, key = _need_on(pid)
    convs = data.get("conversations") if isinstance(data, dict) else None
    if not isinstance(convs, list):
        raise TranscriptsError("bad_input")
    added = changed = 0
    with _LOCK, _db() as conn:
        for i, c in enumerate(convs[:MAX_CONVS]):
            if not isinstance(c, dict):
                continue
            msgs = _norm_messages(c.get("messages"))
            if not msgs:
                continue
            origin = _origin(key, c.get("id") if c.get("id") not in (None, "") else f"#{i}:{msgs[0]['text'][:200]}")
            existed = conn.execute("SELECT 1 FROM conversations WHERE id = ?",
                                   (_conv_id(pid, "grok", origin),)).fetchone()
            if _store_conversation(conn, key, pid, "grok", origin, _title_of(c.get("title"), msgs), msgs):
                changed += 1
                added += 0 if existed else 1
    return {"pid": pid, "side": "grok", "new": added, "updated": changed - added}


def ingest_grok_file(pid: str, path: Path) -> dict:
    """A Grok export the owner saved: a plain file of at most MAX_FILE_BYTES, read once."""
    import stat
    _need_on(pid)
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except OSError:
        raise TranscriptsError("bad_input")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise TranscriptsError("bad_input")
        if info.st_size > MAX_FILE_BYTES:
            raise TranscriptsError("too_big")
        chunks, got = [], 0
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            got += len(b)
            if got > MAX_FILE_BYTES:  # grew while being read
                raise TranscriptsError("too_big")
            chunks.append(b)
    finally:
        os.close(fd)
    try:
        data = json.loads(b"".join(chunks).decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise TranscriptsError("bad_input")
    return ingest_grok(pid, data)


_SESSIONS_SQL = {
    (True, True): "SELECT id, title, source FROM sessions ORDER BY rowid DESC LIMIT ?",
    (True, False): "SELECT id, title, NULL AS source FROM sessions ORDER BY rowid DESC LIMIT ?",
    (False, True): "SELECT id, NULL AS title, source FROM sessions ORDER BY rowid DESC LIMIT ?",
    (False, False): "SELECT id, NULL AS title, NULL AS source FROM sessions ORDER BY rowid DESC LIMIT ?",
}
_MESSAGES_SQL = {
    True: "SELECT role, content, timestamp FROM messages WHERE session_id = ? AND active = 1 "
          "AND role IN ('user', 'assistant') ORDER BY id LIMIT ?",
    False: "SELECT role, content, timestamp FROM messages WHERE session_id = ? "
           "AND role IN ('user', 'assistant') ORDER BY id LIMIT ?",
}


def _hermes_sessions(home: Path) -> Iterable[tuple]:
    """(session id, title, [messages]) from a twin's own Hermes session database, opened read-only."""
    db = home / HERMES_DB
    if not db.is_file():
        return []
    from urllib.parse import quote
    uri = "file:" + quote(str(db.resolve()), safe="/") + "?mode=ro"  # every special character (%, ?, #) encoded
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        mcols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        # only these fixed statements are ever run (no name read from the file goes into SQL)
        sessions = conn.execute(_SESSIONS_SQL[("title" in cols, "source" in cols)], (MAX_CONVS,)).fetchall()
        msql = _MESSAGES_SQL["active" in mcols]
        out = []
        for s in sessions:
            rows = conn.execute(msql, (s["id"], MAX_MSGS)).fetchall()
            out.append((str(s["id"]), s["title"], [{"role": r["role"], "text": r["content"], "at": r["timestamp"]}
                                                    for r in rows], s["source"]))
        return out
    finally:
        conn.close()


def _local_source(source: Any) -> bool:
    """A chat on this computer, by the source Hermes recorded for it: not one that came through a messaging app, a
    webhook or the API server, which holds other people's words or words the owner never typed here. Fails closed:
    a missing, empty or unknown source (or a session database without a source column) is not local."""
    return isinstance(source, str) and source.strip().lower() in LOCAL_SOURCES


def ingest_hermes(pid: str, home: Path) -> dict:
    """Keep the Hermes twin's own conversations (read-only from its session database). Sessions this archive made
    from Grok conversations (import_to_hermes) are skipped, so nothing goes round in a circle."""
    pid, rec, key = _need_on(pid)
    imported = set(rec["imported"].values())
    try:
        sessions = list(_hermes_sessions(Path(home)))
    except sqlite3.Error:
        return {"pid": pid, "side": "hermes", "new": 0, "updated": 0, "note": "The twin's sessions could not be read."}
    added = changed = 0
    with _LOCK, _db() as conn:
        for sid, title, raw, *src in sessions:
            if sid in imported or (isinstance(title, str) and title.startswith(IMPORT_TITLE)):
                continue
            msgs = _norm_messages(raw)
            if not msgs:
                continue
            origin = _origin(key, sid)
            existed = conn.execute("SELECT 1 FROM conversations WHERE id = ?",
                                   (_conv_id(pid, "hermes", origin),)).fetchone()
            if _store_conversation(conn, key, pid, "hermes", origin, _title_of(title, msgs), msgs):
                changed += 1
                added += 0 if existed else 1
            conn.execute("UPDATE conversations SET local = ? WHERE id = ?",
                         (1 if _local_source(src[0] if src else None) else 0, _conv_id(pid, "hermes", origin)))
    return {"pid": pid, "side": "hermes", "new": added, "updated": changed - added}


# ---- reading back ------------------------------------------------------------------------------------------------------
def _conversations(conn, key: bytes, pid: str, side: Optional[str] = None, ids: Optional[list] = None) -> list:
    q = "SELECT id, side, started, updated, n, title FROM conversations WHERE pid = ?"
    args: list = [pid]
    if side:
        q += " AND side = ?"
        args.append(side)
    q += " ORDER BY COALESCE(updated, started, 0) DESC"
    out = []
    for cid, sd, started, updated, n, tblob in conn.execute(q, args):
        if ids is not None and cid not in ids:
            continue
        title = open_row(key, _bind(pid, cid, -1, "title"), tblob)
        msgs = []
        for seq, role, at, body in conn.execute("SELECT seq, role, at, body FROM messages WHERE conv = ? ORDER BY seq",
                                                (cid,)):
            text = open_row(key, _bind(pid, cid, seq, role), body)
            if text is None:
                continue  # failed its check: never shown
            msgs.append({"role": role, "text": text, "at": at})
        out.append({"id": cid, "side": sd, "title": title or "Conversation", "started": started,
                    "updated": updated, "messages": msgs})
    return out


def _iso(t: Optional[float]) -> Optional[str]:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if isinstance(t, (int, float)) else None


def view(pid: str) -> dict:
    pid = _check_pid(pid)
    rec = twin_settings(pid)
    counts = {"grok": 0, "hermes": 0}
    if (_dir() / DB_NAME).exists():
        with _db() as conn:
            for side, n in conn.execute("SELECT side, COUNT(*) FROM conversations WHERE pid = ? GROUP BY side", (pid,)):
                counts[side] = n
    return {"pid": pid, "on": rec["on"], "to_grok": rec["to_grok"], "since": rec["since"],
            "conversations": counts, "sentence": SENTENCE}


def search(pid: str, query: str, limit: int = 10, side: Optional[str] = None, redact: bool = True) -> dict:
    """Messages of this twin's archive holding all the query's words, best first. `redact` (the default) masks
    anything that looks like a password or key and holds back lines guarded() flags; only the owner's own search
    in the terminal passes redact=False."""
    pid, _rec, key = _need_on(pid)
    expr = _query(_twin_words_key(key, pid), query)
    if expr is None:
        return {"pid": pid, "query": query if isinstance(query, str) else "", "hits": [],
                "note": f"Give 1 to {MAX_QUERY_WORDS} words to look for."}
    try:
        limit = max(1, min(MAX_HITS, int(limit or 10)))
    except (TypeError, ValueError):
        limit = 10
    qwords = set(words(query))
    hits = []
    if not (_dir() / DB_NAME).exists():
        return {"pid": pid, "query": query, "hits": []}
    with _db() as conn:
        sql = ("SELECT m.id, m.conv, m.seq, m.role, m.at, m.body, c.side, c.title FROM message_words f "
               "JOIN messages m ON m.id = f.rowid JOIN conversations c ON c.id = m.conv "
               "WHERE message_words MATCH ? AND c.pid = ?")
        args: list = [expr, pid]
        if side in SIDES:
            sql += " AND c.side = ?"
            args.append(side)
        sql += " ORDER BY bm25(message_words) LIMIT ?"
        args.append(limit)
        try:
            rows = conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for _mid, cid, seq, role, at, body, sd, tblob in rows:
            text = open_row(key, _bind(pid, cid, seq, role), body)
            if text is None:
                continue
            title = open_row(key, _bind(pid, cid, -1, "title"), tblob) or "Conversation"
            if redact:  # the whole message first: a cut could part a password from the word that names it
                text, title = guarded(text), guarded(title)
            snip = _snippet(text, qwords)
            hits.append({"conversation": cid, "side": sd, "title": title[:TITLE_CHARS], "role": role,
                         "when": _iso(at), "text": snip})
    return {"pid": pid, "query": query, "hits": hits}


HELD_LINE = "[line held back: {}]"


def guarded(text: str) -> str:
    """A kept message on its way out of the archive (to the twin's model, or to Grok): anything shaped like a key
    masked, and a line that looks like it states a password, PIN, code or card number, or reads like an instruction
    to a bot (or one of the plugin's own notes, or is too long or odd to check), held back whole: the same checks as
    the memory layers (mem_layers.mask_secrets, xs_memory.why_held)."""
    from . import mem_layers, xs_memory
    out = []
    for line in mem_layers.mask_secrets(text if isinstance(text, str) else "").split("\n"):
        bare = line.strip()
        why = xs_memory.why_held(bare) if bare and not bare.startswith("[line held back") else None
        out.append(HELD_LINE.format(xs_memory.HELD_WORDS.get(why, why)) if why else line)
    return "\n".join(out)


def _snippet(text: str, qwords: set) -> str:
    if len(text) <= SNIPPET:
        return text
    found = [m.start() for w in qwords if w for m in [re.search(re.escape(w), text, re.IGNORECASE)] if m]
    if not found and qwords:  # in the text only after normalising (ß / ss, ligatures): a letter at a time, keeping
        # just the tail that a word could end in (linear in the text, never the whole text re-read per letter)
        longest = max(len(w) for w in qwords)
        tail = ""
        for i, ch in enumerate(text):
            tail = (tail + unicodedata.normalize("NFKC", ch).casefold())[-(longest + 8):]
            if any(tail.endswith(w) for w in qwords if w):
                found.append(max(0, i - longest))
                break
    pos = min(found, default=0)
    start = max(0, pos - SNIPPET // 3)
    out = text[start:start + SNIPPET]
    return ("…" if start else "") + out + ("…" if start + SNIPPET < len(text) else "")


def conversation(pid: str, cid: str) -> dict:
    pid, _rec, key = _need_on(pid)
    if not isinstance(cid, str) or not CONV_RE.match(cid):
        raise TranscriptsError("bad_input", "That is not a conversation id.")
    with _db() as conn:
        convs = _conversations(conn, key, pid, ids=[cid])
    if not convs:
        raise TranscriptsError("bad_input", "No such conversation for this twin.")
    return convs[0]


# ---- out: JSON for the owner, Hermes sessions, and the Grok side ------------------------------------------------------
def export_data(pid: str, side: Optional[str] = None) -> dict:
    pid, _rec, key = _need_on(pid)
    with _db() as conn:
        convs = _conversations(conn, key, pid, side=side if side in SIDES else None)
    return {"format": FORMAT, "version": FORMAT_V, "twin": pid, "exported": _iso(time.time()),
            "conversations": [{**c, "started": _iso(c["started"]), "updated": _iso(c["updated"]),
                               "messages": [{**m, "at": _iso(m["at"])} for m in c["messages"]]} for c in convs]}


def export_json(pid: str, dest: Optional[Path] = None, side: Optional[str] = None) -> dict:
    """Write the twin's archive, decrypted, as JSON to a NEW file that only the owner's user can read from the
    start (made 0600, never an existing file or a link followed); a write that fails half way leaves no file."""
    data = export_data(pid, side)
    if dest is None:
        out_dir = _private_dir(folder() / EXPORTS)
        dest = out_dir / f"transcripts-{pid}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.json"
    dest = Path(dest)
    try:
        fd = osflags.open_nofollow(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        raise TranscriptsError("exists")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8") + b"\n")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(dest)  # exactly the file made above
        raise
    return {"path": str(dest), "conversations": len(data["conversations"])}


def for_grok(pid: str) -> list:
    """What the Grok side may be given: the Hermes twin's conversations, ONLY when "send to Grok" is on for this
    twin. Every code path that sends transcripts towards Grok goes through here, so the text it returns has already
    been through guarded() (keys masked, lines held back line by line, as briefs and memory notes)."""
    pid, rec, key = _need_on(pid)
    if not rec["to_grok"]:
        raise TranscriptsError("to_grok_off")
    imported = set(rec["imported"].values())
    with _db() as conn:
        local = {r[0] for r in conn.execute("SELECT id FROM conversations WHERE pid = ? AND side = 'hermes' "
                                            "AND local = 1", (pid,))}
        local -= {_conv_id(pid, "hermes", _origin(key, sid)) for sid in imported}
        convs = [c for c in _conversations(conn, key, pid, side="hermes") if c["id"] in local
                 and not c["title"].startswith(IMPORT_TITLE)]
    return [{**c, "title": guarded(c["title"]),
             "messages": [{**m, "text": guarded(m["text"])} for m in c["messages"]]} for c in convs]


def claude_jsonl(conv: dict) -> str:
    """One conversation as a Claude Code-style JSONL chat log, the shape `hermes sessions import --from claude`
    reads (summary line, then user / assistant lines)."""
    lines = [json.dumps({"type": "summary", "summary": _import_title(conv)}, ensure_ascii=False)]
    for m in conv["messages"]:
        lines.append(json.dumps({"type": m["role"], "message": {"role": m["role"], "content": m["text"]}},
                                ensure_ascii=False))
    return "\n".join(lines) + "\n"


def _import_title(conv: dict) -> str:
    """"From Grok: " and the conversation's title, as Hermes shows it: one line, no control characters, links,
    paths or key-like values (the title came from a Grok export), capped."""
    from .safe import clean_summary
    return clean_summary(IMPORT_TITLE + conv["title"], 100)


_IMPORTED_RE = re.compile(r"session as ([0-9]{8}_[0-9]{6}_[0-9a-f]{4,32})\b")


def _run_hermes(tail: list, profile: str) -> tuple:
    """Hermes' own CLI for this twin's bot, as an argument list (no shell), capped in time and output."""
    from . import importer
    from .engine import WHAT_HERMES, run_capped
    rc, out, _err, _ = run_capped(importer.hermes_argv(profile) + tail, IMPORT_TIMEOUT, env=importer._env(),
                                  out_cap=64 * 1024, what=WHAT_HERMES)
    return rc, out.decode("utf-8", "replace")


def import_to_hermes(pid: str, profile: str, ids: Optional[list] = None) -> dict:
    """Make the twin's Grok-side conversations into Hermes sessions of the twin, through Hermes' own importer.
    A conversation already imported is not imported again. Hermes-side conversations are already sessions."""
    pid, _rec, key = _need_on(pid)
    from .model import valid_id
    if not (isinstance(profile, str) and valid_id(profile) and not profile.startswith("-")):
        raise TranscriptsError("bad_twin", "That is not a Hermes bot name.")
    with _db() as conn:
        convs = _conversations(conn, key, pid, side="grok", ids=ids)
        mine = _conv_ids(conn, pid)
    if not convs:
        raise TranscriptsError("nothing")
    done, skipped, failed = [], [], []
    outbox = _private_dir(folder() / OUTBOX)
    _clear_outbox(mine)  # (decrypted text an earlier, stopped run left behind)
    _clear_stale_outbox()  # (and any other twin's, once older than an import can take)
    for c in convs:
        rec = twin_settings(pid)
        if c["id"] in rec["imported"] or not c["messages"]:
            skipped.append(c["id"])
            continue
        path, made = outbox / f"{c['id']}-{_rand.token_hex(4)}.jsonl", False
        try:  # (a write that fails half way still ends in the `finally` below: no decrypted file stays)
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            made = True
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(claude_jsonl(c))
            rc, out = _run_hermes(["sessions", "import", "--from", "claude", str(path)], profile)
            m = _IMPORTED_RE.search(out or "")
            if rc != 0 or not m:
                failed.append(c["id"])
                continue
            sid = m.group(1)
            title = _import_title(c)
            with contextlib.suppress(Exception):
                _run_hermes(["sessions", "rename", sid, title], profile)
            with _LOCK:
                st = _settings()
                st["twins"].setdefault(pid, _blank())["imported"][c["id"]] = sid
                _save_settings(st)
            done.append({"conversation": c["id"], "session": sid})
        except Exception:
            failed.append(c["id"])
        finally:
            if made:
                with contextlib.suppress(OSError):
                    os.unlink(path)  # exactly the file made above
    return {"pid": pid, "imported": done, "skipped": skipped, "failed": failed}


# ---- the Grok side's transport (the memory layers' GrokTransport: read_transcripts / write_transcript) ----------------------
def pull(pid: str, transport: Any) -> dict:
    """Keep what the Grok side's transport can read now (once the Mirror can read from Grok)."""
    _need_on(pid)
    got = transport.read_transcripts() if transport is not None else None
    if not isinstance(got, list):
        return {"pid": pid, "side": "grok", "new": 0, "updated": 0, "note": "Grok's conversations cannot be read now."}
    return ingest_grok(pid, {"conversations": got})


def push(pid: str, transport: Any) -> dict:
    """Give the Grok side the Hermes twin's conversations: only through for_grok(), so only with "send to Grok" on.
    Only chats held on this computer; a line that looks like it holds a password, PIN, code or card number, or reads
    like an instruction to a bot, is held back before it leaves (guarded)."""
    from . import mem_layers
    if transport is None or isinstance(transport, mem_layers.StubTransport) or not mem_layers.mirror_ready():
        # nothing to Grok without the Mirror; the stand-in would only keep the text in clear in the plugin folder
        return {"pid": pid, "sent": 0, "note": "needs_mirror"}
    convs = for_grok(pid)  # already guarded line by line
    sent = 0
    for c in convs:
        out = {"id": c["id"], "title": c["title"], "started": _iso(c["started"]),
               "messages": [{"role": m["role"], "content": m["text"], "time": _iso(m["at"])}
                            for m in c["messages"]]}
        if transport.write_transcript(out):
            sent += 1
    return {"pid": pid, "sent": sent}


# ---- the What syncs panel (the panel's sync_panel; a no-op on a branch without it) -----------------------------------
_PANEL_DIRECTIONS = {"off": (False, False), "grok_to_hermes": (True, False), "hermes_to_grok": (True, True),
                     "both": (True, True)}


def _mirror_ready() -> bool:
    try:
        from . import mem_layers
        return mem_layers.mirror_ready()
    except Exception:
        return False


def _panel_change(ctx: Any, direction: str) -> None:
    on, to_grok = _PANEL_DIRECTIONS.get(direction, (False, False))
    if to_grok and not _mirror_ready():  # (the panel refuses first; never "send to Grok" on without the Mirror)
        from .engine import EngineError
        raise EngineError("not_available", MESSAGES["needs_mirror"])
    switch(ctx.pair_id, on)
    if on:  # both ways: "send to Grok" goes off again when the row moves back to Grok -> Hermes only
        switch_to_grok(ctx.pair_id, to_grok)


def _panel_directions(ctx: Any) -> dict:
    # keeping (and searching) both sides here works now; giving Hermes' side to Grok needs the Mirror's transport
    grok = "ok" if _mirror_ready() else "needs_mirror"
    return {"off": "ok", "grok_to_hermes": "ok", "hermes_to_grok": grok, "both": grok}


def _panel_default(ctx: Any) -> str:
    rec = twin_settings(ctx.pair_id)
    return "both" if rec["to_grok"] else ("grok_to_hermes" if rec["on"] else "off")


PANEL_SENTENCES = {
    "grok_to_hermes": "This twin's conversations, from Grok and Hermes, are kept here, encrypted and searchable by the "
                      "twin; none go to Grok. Grok's come in from an export: hermes twin transcripts add-grok.",
    "hermes_to_grok": "Kept here as above, and this twin's Hermes conversations are also given to Grok by the Mirror.",
    "both": "Kept here as above, and this twin's Hermes conversations are also given to Grok by the Mirror.",
}


def plug_into_panel() -> bool:
    try:
        from . import sync_panel  # type: ignore[attr-defined]
    except ImportError:
        return False
    sync_panel.provide("transcripts", directions=_panel_directions, on_change=_panel_change, default=_panel_default,
                       sentence=lambda ctx, d: PANEL_SENTENCES.get(d), live=True)
    return True


def _reset_for_tests() -> None:
    """(Nothing is held between calls any more; kept so tests can call it.)"""


try:
    plug_into_panel()
except Exception:  # the panel row is missing then; everything else works (and says so in the log)
    import logging
    logging.getLogger(__name__).warning("grok-bot-twin: full transcripts could not join the What syncs panel",
                                        exc_info=True)
