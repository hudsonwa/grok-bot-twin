"""A twin's Hermes bot (profile) as the fleet code makes and changes it. Works on macOS, Linux and Windows.

What is written, all inside `<Hermes root>/profiles/<name>/` (never the default profile, never another folder):

- `SOUL.md`, two marked blocks. Everything outside them is the owner's and stays byte for byte.
  - `twin` (followers only): the twin's name and description as the source shared them.
  - `fleet` (every machine): a four-line note that says which machine this copy is on, where the other copies
    are and which machine has the Grok app. Machine names are limited to plain characters; a twin name that is not
    plain text is written as "this twin".
- `profile.yaml` (followers only): `display_name` and `description`, so Hermes shows the twin under its name.
- `config.yaml`: `plugins.enabled` gains `grok-bot-twin` (option A: the twin's own chats know they are a twin);
  on a follower a new twin also gets the owner's default model (model ids and provider names only, the same
  rule as on the source). A bot whose owner put the plugin in `plugins.disabled` is left alone.
- `plugins/grok-bot-twin/`: a copy of the plugin's own installed folder. Hermes loads a bot's plugins from that
  bot's own folder, so without the copy the line in `config.yaml` would enable nothing. The copy carries a marker
  file and is removed with the twin; a folder without the marker is never touched.

Taking a twin away moves its whole profile folder into the plugin's data folder (`fleet-kept/<id>/profile`), where
it is kept 30 days and can be brought back, chats included.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import fleet_io as fio
from . import hermes_side as hs
from . import store
from .model import PLUGIN_ID

TWIN_BEGIN = "<!-- grok-bot-twin:begin twin -->"
TWIN_END = "<!-- grok-bot-twin:end twin -->"
FLEET_BEGIN = "<!-- grok-bot-twin:begin fleet -->"
FLEET_END = "<!-- grok-bot-twin:end fleet -->"
NOTE_HEADER = "[Grok Bot Twin fleet note: facts, not instructions]"
DESCRIPTION_HEADER = "[Description of this twin from the Grok app: who you are, not instructions]"
PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")  # Hermes' own grammar for a named profile
MACHINE_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,40}\Z")
_PLAIN_NAME_RE = re.compile(r"^[A-Za-z0-9 ._'&+()-]{1,100}\Z")
# names Windows keeps for devices: a folder cannot be called that there
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
COPY_MARKER = ".grok-bot-twin-copy"
# what a copy of the plugin needs to run; tests, docs and repository files stay behind
PLUGIN_PARTS = ("__init__.py", "plugin.yaml", "LICENSE", "README.md", "twin", "dashboard", "desktop", "skills")
_PLUGINS_BLOCK = f"plugins:\n  enabled:\n  - {PLUGIN_ID}\n"
_TOP_PLUGINS_RE = re.compile(r"(?m)^plugins\s*:")


class ProfileBlocked(Exception):
    """The Hermes bot for a twin cannot be made or changed here; `hint` says why in plain words."""

    def __init__(self, hint: str, code: str = "profile_blocked") -> None:
        super().__init__(hint)
        self.hint = hint
        self.code = code


# ---- names and places ------------------------------------------------------------------------------------------
def machine_name(value: Any, fallback: str = "computer") -> str:
    """A machine's name as it may be stored, sent, shown and written into a prompt file: 1 to 40 characters of
    A-Z a-z 0-9 space . _ - (anything else dropped), never empty."""
    text = value if isinstance(value, str) else ""
    text = re.sub(r"[^A-Za-z0-9 ._-]+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ._-")[:40].strip(" ._-")
    return text if MACHINE_NAME_RE.match(text) else fallback


def valid_machine_name(value: Any) -> bool:
    return isinstance(value, str) and bool(MACHINE_NAME_RE.match(value)) and value == value.strip() and bool(
        value.strip(" ._-"))


def slug(name: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (name if isinstance(name, str) else "").lower()).strip("-")[:40].strip("-")
    if text in _WINDOWS_DEVICES:
        text += "-twin"
    return text or "twin"


def profiles_root() -> Path:
    return store.hermes_root() / "profiles"


def profile_dir(profile: str) -> Path:
    if not isinstance(profile, str) or not PROFILE_ID_RE.match(profile) or profile == "default":
        raise ProfileBlocked("That is not a name a Hermes bot can have.")
    return profiles_root() / profile


# What a running Hermes makes again by itself in a bot's folder right after the bot was moved away (with
# Hermes Desktop open while a computer is unlinked, for one): its own working folders, logs, caches, the
# cron scheduler's heartbeat files and a fresh session database with no chat in it. None of it is the owner's.
_SHELL_DIRS = ("logs", "cron", "sessions", "memories", "skills", "skins", "plans", "workspace", "home", "cache",
               "plugins", "plugin-update-checks", "audio_cache", "image_cache", "hooks", "runtime", "platforms",
               "pairing")
_SHELL_FILE_DIRS = ("logs", "cache", "plugin-update-checks")  # any file below these is Hermes' own bookkeeping
_SHELL_CRON_FILES = ("ticker_last_success", "ticker_heartbeat", ".tick.lock", ".jobs.lock",
                     "executions.db", "executions.db-wal", "executions.db-shm")  # (no job file is allowed there)
_SHELL_DB = "state.db"
_SHELL_TOP_FILES = (_SHELL_DB, _SHELL_DB + "-wal", _SHELL_DB + "-shm")
_SHELL_DB_CAP = 8 * 1024 * 1024
# tables of Hermes' session database that say nothing about the owner (every other table must be empty)
_SHELL_DB_BOOKKEEPING = ("schema_version", "state_meta", "sqlite_sequence", "runtime_epoch", "gateway_heartbeats",
                         "gateway_hygiene_state")


def _db_state(db: Path) -> str:
    """What a session database in a leftover folder holds: "empty" only when it is a Hermes session database (it
    has the `sessions` and `messages` tables) and every table but Hermes' own bookkeeping is empty; "used" when a
    table holds a row; "unknown" when that cannot be told right now (it is still being set up, cannot be opened,
    or is not that database). Opened read-only."""
    import sqlite3
    try:
        if os.path.islink(db) or os.path.getsize(db) > _SHELL_DB_CAP:
            return "used"
        wal = str(db) + "-wal"
        busy = os.path.lexists(wal) and os.path.getsize(wal) > 0  # then the newest rows are in that file
        con = sqlite3.connect(Path(db).resolve().as_uri() + ("?mode=ro" if busy else "?mode=ro&immutable=1"),
                              uri=True, timeout=1.0)
        try:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            if "sessions" not in tables or "messages" not in tables:
                return "unknown"
            for name in tables:
                if name in _SHELL_DB_BOOKKEEPING or "_fts" in name:
                    continue
                quoted = '"' + name.replace('"', '""') + '"'
                if con.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone() is not None:
                    return "used"
            return "empty"
        finally:
            con.close()
    except Exception:
        return "unknown"


def _no_chats(db: Path) -> bool:
    return _db_state(db) == "empty"


def _shell_layout(home: Path, also: Optional[Callable[[str], bool]] = None) -> bool:
    """The folder holds none of the files that make a Hermes bot, nothing but Hermes' own working folders, and
    files only where Hermes keeps its bookkeeping (see is_shell). Says nothing about the session database.
    `also`: top-level file names that are let through as well (half_made)."""
    home = Path(home)
    if not fio.is_real_dir(home):
        return False
    try:
        seen = 0
        for dirpath, dirnames, filenames in os.walk(home, followlinks=False):
            rel = Path(dirpath).relative_to(home).parts
            if not rel and any(d not in _SHELL_DIRS for d in dirnames):
                return False
            if any(os.path.islink(os.path.join(dirpath, n)) for n in dirnames + filenames):
                return False
            for name in filenames:
                if not rel:
                    if name not in _SHELL_TOP_FILES and not (name.startswith(_SHELL_DB + ".") and name.endswith(".lock")) \
                            and not (also is not None and also(name)):
                        return False
                elif rel[0] in _SHELL_FILE_DIRS or (rel == ("cron",) and name in _SHELL_CRON_FILES):
                    continue
                else:
                    return False
            seen += len(dirnames) + len(filenames)
            if seen > 500:
                return False
        return True
    except OSError:
        return False


def is_shell(home: Path) -> bool:
    """An empty shell where a Hermes bot used to be: a real folder with none of the files that make a Hermes bot
    (no SOUL.md, config.yaml, profile.yaml, .env, auth.json), nothing but Hermes' own working folders, files only
    where Hermes keeps its bookkeeping (logs, caches, the cron scheduler's heartbeat, lock files of its session
    database), and at most a session database with no chat in it (_db_state). A running Hermes leaves one behind
    right after a twin's bot was moved to the kept place and keeps making it again; it is not the owner's bot and
    counts as free."""
    home = Path(home)
    if not _shell_layout(home):
        return False
    try:
        if os.path.lexists(home / _SHELL_DB):
            return _db_state(home / _SHELL_DB) == "empty"
        return not any(os.path.lexists(home / n) for n in _SHELL_TOP_FILES)
    except OSError:
        return False


_OWN_TMP_RE = re.compile(r"^\.(?:config\.yaml|SOUL\.md|profile\.yaml)\.[A-Za-z0-9_]{1,16}\.tmp\Z")  # fleet_io.write_text


def half_made(home: Path) -> bool:
    """A folder this plugin began to make a twin's Hermes bot in and was cut short (the process was killed, the
    computer went off): no prompt file yet, and nothing in it but the settings file that is written first,
    leftovers of the plugin's own unfinished writes and what a running Hermes adds to any bot's folder by itself
    (see is_shell). Only ever asked about a folder this machine noted it was about to make."""
    home = Path(home)
    if not _shell_layout(home, lambda n: n == "config.yaml" or bool(_OWN_TMP_RE.match(n))):
        return False
    try:
        if os.path.lexists(home / _SHELL_DB):
            return _db_state(home / _SHELL_DB) == "empty"
        return not any(os.path.lexists(home / n) for n in _SHELL_TOP_FILES)
    except OSError:
        return False


def clear_shell(profile: str) -> bool:
    """Remove such an empty shell so the name can be used. True when the name is free afterwards."""
    home = profiles_root() / profile
    if not os.path.lexists(home):
        return True
    if not is_shell(home):
        return False
    fio.remove_dir(home, profiles_root())
    return not os.path.lexists(home)


# ---- a leftover folder where a kept twin's bot belongs ------------------------------------------------------------
# With Hermes Desktop open, the running Hermes makes a moved bot's folder again within seconds, and what it puts
# there differs from run to run (a session database, `projects.db`, a `gateway.lock`, once its own default prompt
# file). A list of file names never caught up with it. So when a KEPT twin comes back and such a folder sits at its
# old name, the question is not "is it empty" but "is anything in it the owner's": if not, the folder is moved
# aside into the kept place (nothing is deleted; it goes after the same 30 days) and the twin's bot gets its own
# name back instead of `name-2`.
_OWNER_FILES = ("config.yaml", "profile.yaml", ".env", "auth.json", "MEMORY.md", "USER.md", "todo.json")
# Any file below these is the owner's (or makes the folder a bot the owner set up: `hermes profile create` puts
# its skills there, a chat its session files). What Hermes makes again by itself leaves them empty.
_OWNER_DIRS = ("memories", "workspace", "home", "plans", "skins", "skills", "sessions", "hooks", "plugins")
# ...but for Hermes' own bookkeeping there: a Hermes that goes on serving the folder it made again starts its
# skills curator after some minutes, which notes its own state (no skill, nothing the owner wrote).
# Without this a link made later finds the name taken again.
_OWNER_DIR_BOOKKEEPING = {"skills": (".curator_state",)}


def _hermes_default_soul(text: str) -> bool:
    """Is this prompt file Hermes' own starting text, never edited? Asked of Hermes itself; "no" when this
    Hermes cannot say."""
    try:
        from hermes_cli.default_soul import DEFAULT_SOUL_MD, is_legacy_template_soul
    except Exception:
        return False
    try:
        return text.strip() == DEFAULT_SOUL_MD.strip() or bool(is_legacy_template_soul(text))
    except Exception:
        return False


def _has_jobs(home: Path) -> bool:
    path = home / "cron" / "jobs.json"
    if not os.path.lexists(path):
        return False
    raw = fio.read_json(path, None)
    jobs = raw.get("jobs") if isinstance(raw, dict) else raw
    return not (isinstance(jobs, (list, dict)) and not jobs)  # unreadable counts as "has jobs"


def is_leftover(home: Path, patient: bool = True) -> bool:
    """A folder that holds nothing of the owner's: a real folder with none of the files that make a Hermes bot
    or carry what the owner told it (config.yaml, profile.yaml, .env, auth.json, memory files), a prompt file
    only if it is Hermes' own untouched starting text, no scheduled job, no file where the owner's work would be
    (memories, skills, sessions, workspace, plans...), no link, and no chat in its session database (one that
    holds a chat, cannot be read or is too large to look at always counts as the owner's). An empty shell
    (is_shell) is one. `patient` False: a session database that cannot be read right now is not waited for (the
    page's own look, which must answer at once); the folder then counts as the owner's."""
    home = Path(home)
    if not fio.is_real_dir(home):
        return False
    try:
        if is_shell(home):
            return True
        names = os.listdir(home)
        if len(names) > 200 or any(os.path.islink(home / n) for n in names):
            return False
        if any(os.path.lexists(home / n) for n in _OWNER_FILES):
            return False
        if os.path.lexists(home / "SOUL.md"):
            text = fio.read_text(home / "SOUL.md", hs.SOUL_CAP)
            if text is None or (text.strip() and not _hermes_default_soul(text)):
                return False
        for sub in _OWNER_DIRS:
            if os.path.lexists(home / sub):
                if not fio.is_real_dir(home / sub):
                    return False
                for dirpath, _dirnames, filenames in os.walk(home / sub, followlinks=False):
                    own = _OWNER_DIR_BOOKKEEPING.get(sub, ()) if Path(dirpath) == home / sub else ()
                    if any(n not in own for n in filenames):
                        return False
        if _has_jobs(home):
            return False
        if not os.path.lexists(home / _SHELL_DB):
            return True
        return _chatless(home / _SHELL_DB) if patient else _has_chat(home / _SHELL_DB) is False
    except OSError:
        return False


_CHAT_WORDS = ("session", "message", "chat", "conversation", "turn")


def _has_chat(db: Path) -> Optional[bool]:
    """Does this session database hold a chat: a row in `messages` or any other table named after chats? False
    also for a database with no table yet, or with none of those tables yet; None when that cannot be told right
    now. Opened read-only. (Asked this way, and not "is every table empty": a fresh Hermes database already
    holds rows of its own, a different handful in each Hermes version, and none of them is the owner's.)
    A row in `sessions` alone is not a chat: Hermes Desktop, with a twin's chat still open in a tab when the
    computer is unlinked, notes that open tab again in the database it makes anew, with nothing said in it.
    Linking again in that moment took the folder for the owner's and gave the twin back as `name-2`."""
    import sqlite3
    try:
        if os.path.islink(db) or os.path.getsize(db) > _SHELL_DB_CAP:
            return True
        wal = str(db) + "-wal"
        busy = os.path.lexists(wal) and os.path.getsize(wal) > 0  # then the newest rows are in that file
        con = sqlite3.connect(Path(db).resolve().as_uri() + ("?mode=ro" if busy else "?mode=ro&immutable=1"),
                              uri=True, timeout=1.0)
        try:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            for name in tables:
                if "_fts" in name or name == "sessions" or not any(w in name.lower() for w in _CHAT_WORDS):
                    continue
                quoted = '"' + name.replace('"', '""') + '"'
                if con.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone() is not None:
                    return True
            return False
        finally:
            con.close()
    except Exception:
        return None


# How long a session database that Hermes is just making is given to become readable. On a busy
# computer that can take well over ten seconds, and a twin judged too early comes back under a numbered name.
SETTLE_S = 30.0


def _being_made(db: Path) -> bool:
    """A session database that Hermes is setting up right now: changed in the last half minute, and a database
    file by its first bytes (or still empty). Anything else that cannot be read is not waited for."""
    import time
    try:
        changed = max(os.path.getmtime(p) for p in (str(db), str(db) + "-wal") if os.path.lexists(p))
        if time.time() - changed > 30:
            return False
        with open(db, "rb") as f:
            head = f.read(16)
        return head in (b"", b"SQLite format 3\x00")
    except OSError:
        return False


def _chatless(db: Path) -> bool:
    """No chat in this session database, for sure. One that cannot be read right now is looked at again for
    about a second, and for up to SETTLE_S while Hermes is just setting it up (for some
    seconds after Unlink the new database cannot be read, and a link made in that time would give the twins
    back as `name-2`); if it still cannot be read it counts as holding a chat, and the folder is left alone."""
    import time
    started = time.monotonic()
    while True:
        has = _has_chat(db)
        if has is not None:
            return not has
        waited = time.monotonic() - started
        if waited >= SETTLE_S or (waited >= 1.0 and not _being_made(db)):
            return False
        time.sleep(0.35)


# ---- Hermes' own note that a bot is gone -------------------------------------------------------------------------------
# Hermes remembers a bot it deleted or renamed with a small file `profiles/.deleted/<name>`: a bot of that name is
# then not listed, not served and not made again by a part of a running Hermes that still has it open (its own
# rename does exactly this before it moves the folder). The plugin leaves the same note when it moves a twin's bot
# into the kept place. Without it a running Hermes made the folder again within seconds and kept writing into it,
# and its list of bots (and Settings, Model, "Applies to") went on showing a bot that could not be opened.
# The plugin's note says so in its text, which is how it is told from one
# of Hermes' own; only the plugin's is ever taken away again here.
MOVED_MARK = "moved aside by grok-bot-twin\n"


def _gone_mark(profile: str) -> Path:
    return profiles_root() / ".deleted" / profile


def _own_mark(profile: str) -> bool:
    try:
        mark = _gone_mark(profile)
        return stat.S_ISREG(os.lstat(mark).st_mode) and fio.read_text(mark, 200) == MOVED_MARK
    except OSError:
        return False


def _hermes_deleted(profile: str) -> bool:
    """Hermes itself remembers deleting a bot of this name (the plugin's own note does not count)."""
    return os.path.lexists(_gone_mark(profile)) and not _own_mark(profile)


def mark_moved(profile: Any) -> bool:
    """Leave the note for a twin's bot that was just moved into the kept place. Only for a plain bot name with no
    note yet and no folder at it (or one a running Hermes has made again in that moment, with nothing of the
    owner's in it: is_leftover); never raises."""
    try:
        if not isinstance(profile, str) or not PROFILE_ID_RE.match(profile) or profile == "default":
            return False
        home = profiles_root() / profile
        if os.path.lexists(_gone_mark(profile)) or (os.path.lexists(home) and not is_leftover(home)):
            return False
        fio.write_text(_gone_mark(profile), MOVED_MARK)
        return True
    except Exception:
        return False


def clear_moved(profile: Any) -> bool:
    """Take the plugin's own note away again (the twin's bot comes back, is kept as an ordinary bot, or its 30
    days are over). One of Hermes' own is never touched. Never raises."""
    try:
        if not isinstance(profile, str) or not PROFILE_ID_RE.match(profile) or not _own_mark(profile):
            return False
        os.unlink(_gone_mark(profile))
        with contextlib.suppress(OSError):
            os.rmdir(_gone_mark(profile).parent)  # (only when nothing else is in it; Hermes makes it when it needs it)
        return True
    except Exception:
        return False


def heal_mark(profile: Any) -> bool:
    """The plugin's note must never hide a bot of the owner's: when a folder with anything of the owner's sits at
    the name now (put there by hand, say; Hermes' own "new bot" takes the note away itself), the note goes."""
    try:
        if not isinstance(profile, str) or not PROFILE_ID_RE.match(profile) or not _own_mark(profile):
            return False
        home = profiles_root() / profile
        return os.path.lexists(home) and not is_leftover(home) and clear_moved(profile)
    except Exception:
        return False


def set_aside(profile: str, kept_root: Path) -> bool:
    """Free a kept twin's old name: a leftover folder there (is_leftover) is moved into the kept place under an
    id of its own. True when the name is free afterwards. Never touches a folder that holds anything of the
    owner's, and never deletes."""
    import secrets
    root = profiles_root()
    if not isinstance(profile, str) or not PROFILE_ID_RE.match(profile) or profile == "default":
        return False
    if _hermes_deleted(profile):
        return False
    home = root / profile
    if not os.path.lexists(home):
        return True
    if not is_leftover(home):
        return False
    try:
        fio.move_dir(home, Path(kept_root) / ("r-" + secrets.token_hex(6)) / "profile")
    except OSError:
        return False  # (Windows: Hermes still has a file in it open) the twin then comes back beside it
    return not os.path.lexists(home)


def name_taken(profile: str) -> bool:
    """A Hermes bot (or anything else that is not an empty shell, see is_shell) already sits where this profile
    would go, or Hermes remembers deleting one of that name."""
    root = profiles_root()
    if _hermes_deleted(profile):
        return True
    return os.path.lexists(root / profile) and not is_shell(root / profile)


def bot_in_the_way(name: Any) -> bool:
    """Is a Hermes bot of the owner's sitting under this twin's own name right now (or does Hermes remember
    deleting one)? Asked before the page says so. Never waits."""
    base = slug(" ".join(name.split())[:100]) if isinstance(name, str) else ""
    if not base or not PROFILE_ID_RE.match(base):
        return False
    home = profiles_root() / base
    return _hermes_deleted(base) or (os.path.lexists(home) and not is_leftover(home, patient=False))


def unique_profile(name: Any, taken: Any = (), kept: bool = False) -> str:
    """`kept`: the name is for a kept twin's bot that comes back. A folder with nothing of the owner's in it
    (is_leftover) does not take a name from it; the caller moves that folder aside (set_aside)."""
    base = cand = slug(name)
    n = 2
    while cand == "default" or cand in taken or (name_taken(cand) and not (
            kept and not _hermes_deleted(cand) and is_leftover(profiles_root() / cand))):
        tail = f"-{n}"
        cand = base[:64 - len(tail)].rstrip("-_") + tail
        n += 1
    return cand


# ---- SOUL.md blocks ----------------------------------------------------------------------------------------------
def _block_re(begin: str, end: str) -> "re.Pattern[str]":
    return re.compile(re.escape(begin) + r"\n?(.*?)\n?" + re.escape(end) + r"\n?", re.S)


def _inert(text: str) -> str:
    """Shared text as it may sit inside a marked block: nothing in it can open or close a block."""
    return text.replace("<!--", "<!- -").replace("-->", "- ->")


def set_block(text: str, begin: str, end: str, body: Optional[str]) -> str:
    """`text` with the marked block replaced in place (or added at the end); None takes the block out."""
    pat = _block_re(begin, end)
    block = None if body is None else f"{begin}\n{body.rstrip(chr(10))}\n{end}\n"
    m = pat.search(text)
    if m:
        out = text[:m.start()] + (block or "") + pat.sub("", text[m.end():])
        return out
    if block is None:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + block


def get_block(text: str, begin: str, end: str) -> Optional[str]:
    m = _block_re(begin, end).search(text or "")
    return m.group(1) if m else None


def twin_body(name: str, description: str) -> str:
    """The twin block: the name as a heading, then the description as the other computer shared it. The
    description is text from outside this computer, so it is marked as a description and every line of it is
    quoted: whatever it says, it cannot start a section of its own, pass for the fleet note, or read as the
    owner's own instructions."""
    name = _inert(" ".join(name.split()))[:100] or "Twin"
    body = f"# {name}\n"
    description = _inert(description.replace("\r\n", "\n").replace("\r", "\n").strip())[:2000]
    if description:
        quoted = "\n".join((">" + (" " + line if line.strip() else "")) for line in description.split("\n"))
        body += f"\n{DESCRIPTION_HEADER}\n{quoted}\n"
    return body


_SENTENCE_END_RE = re.compile(r"[.](?=\s|\Z)")


def _no_sentences(text: str) -> str:
    """A name as the fleet note carries it: a dot that would end a sentence is dropped (one inside a word, as in
    "example-pc.local", stays), so a name reads as a name and never as sentences of its own."""
    return " ".join(_SENTENCE_END_RE.sub("", text).split())


def note_machine(name: Any) -> str:
    """A computer's name as a note for a chat may carry it (the fleet note in the prompt file, the per-turn note
    about the other computers, the line that heads a request): plain characters, and no full stop that would
    make sentences of it. A computer names itself, so its name is text from outside this computer."""
    return _no_sentences(machine_name(name)) or "computer"


def note_name(name: Any) -> str:
    """A twin's name as the fleet note may carry it; "this twin" for anything that is not plain text or is
    longer than a name (60 characters)."""
    text = " ".join(name.split()) if isinstance(name, str) else ""
    text = _no_sentences(text) if _PLAIN_NAME_RE.match(text) else ""
    return text if text and len(text) <= 60 else "this twin"


def fleet_note(this: str, twin_name: Any, others: list, source: str, is_source: bool) -> str:
    """The fleet note: fixed wording, machine names in plain characters, the twin's name only when it is plain."""
    this, source = (note_machine(m) for m in (this, source))
    names = [note_machine(o) for o in others if isinstance(o, str)][:16]
    lines = [NOTE_HEADER, f"You are the {this} copy of {note_name(twin_name)}, the Hermes twin of a Grok bot."]
    lines.append(f"Copies of this twin also run on: {', '.join(names)}." if names
                 else "No other computer holds a copy yet.")
    if is_source:
        lines.append(f"This computer, {this}, has the Grok app; the copies follow it.")
    else:
        # (fixed words first, the name after them: a computer's name never opens a line of the note)
        lines.append(f"The computer with the Grok app is {source}; your name and description follow it.")
    return "\n".join(lines)


def write_soul(home: Path, twin: Optional[tuple] = None, fleet: Optional[str] = None, drop: bool = False,
               release: bool = False) -> bool:
    """Bring the marked blocks of `home`/SOUL.md up to date. `twin` = (name, description) writes the twin block
    (None leaves it alone); `fleet` = the note (None leaves it alone); `drop` takes both blocks out; `release`
    takes the fleet note out and leaves the twin block's text in place as plain text (the bot becomes an ordinary
    Hermes bot that keeps its name and description). Returns True when the file was written. Nothing is written
    when nothing would change."""
    path = Path(home) / "SOUL.md"
    current = fio.read_text(path, hs.SOUL_CAP)
    if current is None:
        if os.path.lexists(path):
            raise ProfileBlocked("Its prompt file could not be read safely.", "write_failed")
        if drop or release:
            return False
        current = ""
    text = current
    if release:
        body = get_block(text, TWIN_BEGIN, TWIN_END)
        if body is not None:
            m = _block_re(TWIN_BEGIN, TWIN_END).search(text)
            text = text[:m.start()] + body.rstrip("\n") + "\n" + text[m.end():]
        text = set_block(text, FLEET_BEGIN, FLEET_END, None)
    elif drop:
        text = set_block(set_block(text, TWIN_BEGIN, TWIN_END, None), FLEET_BEGIN, FLEET_END, None)
    else:
        if twin is not None:
            text = set_block(text, TWIN_BEGIN, TWIN_END, twin_body(twin[0], twin[1]))
        if fleet is not None:
            text = set_block(text, FLEET_BEGIN, FLEET_END, fleet)
    if text == current:
        return False
    fio.write_text(path, text)
    return True


def twin_text(home: Path) -> str:
    """What the twin block of this bot's prompt file holds ("" when there is none)."""
    text = fio.read_text(Path(home) / "SOUL.md", hs.SOUL_CAP)
    return (get_block(text, TWIN_BEGIN, TWIN_END) or "") if text else ""


def reclaim(home: Path, released_text: str) -> None:
    """A bot that was released (its twin block left in place as plain text) becomes a twin's bot again: that
    plain text is taken out when it still stands there exactly as it was left, so the twin block that follows
    does not say everything twice. Anything the owner wrote is kept."""
    path = Path(home) / "SOUL.md"
    text = fio.read_text(path, hs.SOUL_CAP)
    if text is None:
        if os.path.lexists(path):
            raise ProfileBlocked("Its prompt file could not be read safely.", "write_failed")
        return
    at = released_at(text, released_text)
    if at is not None:
        plain = released_text.rstrip("\n") + "\n"
        fio.write_text(path, text[:at] + text[at + len(plain):])


def released_at(text: Any, released_text: Any) -> Optional[int]:
    """Where the text a released bot was left with still stands in its prompt file, whole and from the start of a
    line; None when it does not (the owner rewrote it, or this is another bot that only has the same name)."""
    if not isinstance(text, str) or not isinstance(released_text, str) or not released_text.strip():
        return None
    plain = released_text.rstrip("\n") + "\n"
    at = text.find(plain)
    while at > 0 and text[at - 1] != "\n":
        at = text.find(plain, at + 1)
    return at if at >= 0 else None


def soul_fleet_note(home: Path) -> Optional[str]:
    text = fio.read_text(Path(home) / "SOUL.md", hs.SOUL_CAP)
    return get_block(text, FLEET_BEGIN, FLEET_END) if text else None


# ---- config.yaml: option A (the plugin switched on in the twin's own Hermes bot) ---------------------------------
def _yaml():
    """PyYAML when this Hermes has it, else Hermes' own YAML module or ruamel.yaml (a plain Hermes install has
    no PyYAML); None when none is there."""
    from . import yamlio
    return yamlio.module()


def _names(value: Any) -> list:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def plugin_enabled(home: Path) -> Optional[bool]:
    """Is the plugin in this bot's `plugins.enabled`? None when that cannot be told."""
    text = fio.read_text(Path(home) / "config.yaml", hs.CONFIG_CAP)
    if text is None:
        return False if not os.path.lexists(Path(home) / "config.yaml") else None
    yaml = _yaml()
    if yaml is None:
        return _PLUGINS_BLOCK in text
    try:
        doc = yaml.safe_load(text) or {}
    except Exception:
        return None
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    return PLUGIN_ID in _names(plugins.get("enabled")) if isinstance(plugins, dict) else False


def enable_plugin(home: Path) -> bool:
    """Add the plugin to `plugins.enabled` of this bot's config.yaml. True when the file now enables it. The rest
    of the file is kept: when it has no `plugins` section the three lines are added at its end; only a file that
    already has one is rewritten as a whole (and only when it reads back as intended). A bot whose owner listed
    the plugin under `plugins.disabled` is left alone."""
    path = Path(home) / "config.yaml"
    text = fio.read_text(path, hs.CONFIG_CAP)
    if text is None and os.path.lexists(path):
        return False
    text = text or ""
    yaml = _yaml()
    if yaml is None:
        if _PLUGINS_BLOCK in text:
            return True
        if _TOP_PLUGINS_RE.search(text):
            return False
        fio.write_text(path, text + ("" if not text or text.endswith("\n") else "\n") + _PLUGINS_BLOCK)
        return True
    try:
        doc = yaml.safe_load(text) if text.strip() else {}
    except Exception:
        return False
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        return False
    plugins = doc.get("plugins")
    if plugins is None and "plugins" not in doc:
        new_text = text + ("" if not text or text.endswith("\n") else "\n") + _PLUGINS_BLOCK
        want = {**doc, "plugins": {"enabled": [PLUGIN_ID]}}
    else:
        if not isinstance(plugins, dict):
            plugins = {}
        if PLUGIN_ID in _names(plugins.get("disabled")):
            return False
        enabled = _names(plugins.get("enabled"))
        if PLUGIN_ID in enabled:
            return True
        want = {**doc, "plugins": {**plugins, "enabled": enabled + [PLUGIN_ID]}}
        new_text = yaml.safe_dump(want, default_flow_style=False, sort_keys=False, allow_unicode=True)
    try:
        if yaml.safe_load(new_text) != want:
            return False
    except Exception:
        return False
    fio.write_text(path, new_text)
    return True


def disable_plugin(home: Path) -> bool:
    """Take the plugin out of `plugins.enabled` again (twin removed, or twinning stopped). True when changed."""
    path = Path(home) / "config.yaml"
    text = fio.read_text(path, hs.CONFIG_CAP)
    if not text:
        return False
    if text.endswith(_PLUGINS_BLOCK) and not _TOP_PLUGINS_RE.search(text[:-len(_PLUGINS_BLOCK)]):
        fio.write_text(path, text[:-len(_PLUGINS_BLOCK)])  # exactly what enable_plugin added: the rest is untouched
        return True
    yaml = _yaml()
    if yaml is None:
        return False
    try:
        doc = yaml.safe_load(text)
    except Exception:
        return False
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    if not isinstance(plugins, dict) or PLUGIN_ID not in _names(plugins.get("enabled")):
        return False
    plugins = {**plugins, "enabled": [n for n in _names(plugins.get("enabled")) if n != PLUGIN_ID]}
    want = {**doc, "plugins": plugins}
    new_text = yaml.safe_dump(want, default_flow_style=False, sort_keys=False, allow_unicode=True)
    try:
        if yaml.safe_load(new_text) != want:
            return False
    except Exception:
        return False
    fio.write_text(path, new_text)
    return True


def default_model_doc() -> dict:
    """The owner's default model settings as a new twin may copy them (ids and provider names only)."""
    yaml = _yaml()
    text = fio.read_text(store.hermes_root() / "config.yaml", hs.CONFIG_CAP)
    if yaml is None or not text:
        return {}
    try:
        src = yaml.safe_load(text)
    except Exception:
        return {}
    if not isinstance(src, dict) or not hs._model_of(src):
        return {}
    add: dict = {}
    for key, keys, as_list in (("model", ("default", "model", "name", "provider"), False),
                               ("fallback_providers", ("provider", "model"), True),
                               ("fallback_model", ("provider", "model"), False)):
        got = hs._allowed(src.get(key), keys, as_list)
        if got:
            add[key] = got
    return add if hs._model_of(add) else {}


# ---- the plugin's own code inside the twin's Hermes bot ---------------------------------------------------------
def _is_plugin(root: Path) -> bool:
    return (root / "plugin.yaml").is_file() and (root / "__init__.py").is_file()


def plugin_source() -> Optional[Path]:
    """The folder copies of the plugin are made from: the one this code runs from, when it looks like an installed
    plugin. When this code itself runs from a COPY inside a twin's Hermes bot (it carries the copy marker), the
    owner's installed plugin in the main Hermes folder is the source instead: after an update the copies follow
    the install, and an old copy that happens to be the one running never overwrites newer ones."""
    root = Path(__file__).resolve().parent.parent
    if not _is_plugin(root):
        return None
    try:
        if os.path.lexists(root / COPY_MARKER):
            main = store.hermes_root() / "plugins" / PLUGIN_ID
            if fio.is_real_dir(main) and _is_plugin(main) and not os.path.lexists(main / COPY_MARKER):
                return main
    except OSError:
        pass
    return root


_stamps: dict = {}


def _source_files(src: Path) -> list:
    out = []
    for part in PLUGIN_PARTS:
        p = src / part
        if p.is_file() and not p.is_symlink():
            out.append(p)
        elif p.is_dir() and not p.is_symlink():
            out += [f for f in sorted(p.rglob("*")) if f.is_file() and not f.is_symlink()
                    and "__pycache__" not in f.parts and f.suffix != ".pyc" and f.name != ".DS_Store"]
    return out


STAMP_AGE_S = 30.0  # how old a remembered stamp may be when it is written next to a new copy


def _source_stamp(src: Path, max_age: Optional[float] = None) -> str:
    """A digest of what the plugin's folder holds (names and contents), so a copy made from a copy carries the
    same stamp as one made from the install. Worked out once per process and remembered; `max_age` (seconds)
    has it worked out again when what is remembered is older than that: the installed plugin can be updated
    while this process runs."""
    key = str(src)
    got = _stamps.get(key)
    if got is None or (max_age is not None and time.monotonic() - got[1] > max_age):
        import hashlib
        h = hashlib.sha256()
        for f in _source_files(src):
            h.update(str(f.relative_to(src).as_posix()).encode("utf-8") + b"\0")
            with contextlib.suppress(OSError):
                h.update(hashlib.sha256(f.read_bytes()).digest())
        got = _stamps[key] = (h.hexdigest()[:16], time.monotonic())
    return got[0]


def _copy_state(dst: Path, src: Path) -> Optional[bool]:
    """How the copy at `dst` stands: True when nothing is to do (a current copy, or an install of the owner's own:
    not ours to change), False when the place cannot be used, None when a copy has to be made or replaced."""
    if not os.path.lexists(dst):
        return None
    if not fio.is_real_dir(dst):
        return False
    marker = fio.read_text(dst / COPY_MARKER, 200)
    if marker is None:
        return True  # the owner's own install: not ours to change
    if marker.strip() == _source_stamp(src):
        return True
    # Before a copy is replaced the install is looked at afresh: when the plugin was updated while this process
    # runs, a newer process has already brought the copy up to date, and what this one remembers is the stamp of
    # the version before. Without the second look the two took turns replacing every twin's copy on every
    # change, each writing its own stamp (every copy written again per change, and each
    # change many times slower).
    if marker.strip() == _source_stamp(src, max_age=0):
        return True
    return None


_install_sig: Optional[tuple] = None


def install_changed() -> bool:
    """Has the installed plugin's folder changed on disk since this was last asked: was the plugin updated while
    Hermes runs? File names, dates and sizes only (nothing is read). The first call only remembers and says no.
    When it says yes the remembered digest of the folder is dropped, so the copies made next carry the new one."""
    global _install_sig
    try:
        src = plugin_source()
        if src is None:
            return False
        sig = []
        for f in _source_files(src):
            st = f.stat()
            sig.append((f.relative_to(src).as_posix(), st.st_mtime_ns, st.st_size))
        was, _install_sig = _install_sig, tuple(sig)
        if was is None or was == _install_sig:
            return False
        _stamps.pop(str(src), None)
        return True
    except OSError:
        return False


def install_look(src: Optional[Path] = None) -> Optional[str]:
    """A cheap look at the installed plugin's files: names, sizes and dates, nothing is read. It changes when the
    owner updates the plugin under a process that keeps running (the background helper). None when the plugin does
    not run from an installed folder, or while that folder is being replaced."""
    src = src or plugin_source()
    if src is None:
        return None
    import hashlib
    h = hashlib.sha256()
    try:
        for f in _source_files(src):
            st = f.stat()
            h.update(f"{f.relative_to(src).as_posix()}\0{st.st_size}\0{st.st_mtime_ns}\n".encode("utf-8"))
    except OSError:
        return None
    return h.hexdigest()[:16]


class UpdateWatch:
    """Notices that the installed plugin changed under this running process. `changed()` is True once the same
    new set of files was seen at two looks in a row (an update that is still being written is not one yet)."""

    def __init__(self, look: Any = None) -> None:
        self._look = look or install_look
        self.first = self._look()
        self.pending: Optional[str] = None

    def changed(self) -> bool:
        now = self._look()
        if self.first is None:  # nothing to compare with yet (started in the middle of an install)
            self.first = now
            return False
        if now is None or now == self.first:
            self.pending = None
            return False
        if now == self.pending:
            return True
        self.pending = now
        return False


def place_plugin(home: Path, src: Optional[Path] = None) -> bool:
    """Put a copy of the plugin into `home`/plugins/grok-bot-twin (refreshed when the installed plugin changed).
    True when a current copy is there afterwards. A folder of that name that is not such a copy (the owner
    installed the plugin there) is left alone and counts as present."""
    src = src or plugin_source()
    dst = Path(home) / "plugins" / PLUGIN_ID
    if src is None:
        return dst.is_dir()
    state = _copy_state(dst, src)
    if state is not None:
        return state
    try:
        if Path(src).resolve() == dst.resolve() or store.contained(Path(src).resolve(), dst.resolve()):
            return True
    except OSError:
        return False
    # One process at a time replaces a copy (the store's lock, which the caller may hold already): two doing it
    # to the same bot at once took each other's half-made copy away, and one could be left empty under a stamp
    # that says it is whole (the sharing process and a terminal command after a plugin update, for one).
    with store.locked():
        state = _copy_state(dst, src)  # another process may have done it while this one waited
        if state is not None:
            return state
        if os.path.lexists(dst):
            fio.remove_dir(dst, Path(home) / "plugins")
        store.ensure_dir(dst)
        # marked as unfinished first: a copy cut short part-way (the process was killed) is then replaced next
        # time instead of being taken for an install of the owner's own and left broken for good
        fio.write_text(dst / COPY_MARKER, "unfinished\n")
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".DS_Store", "tests", ".pytest_cache")
        for part in PLUGIN_PARTS:
            p = src / part
            if p.is_dir() and not p.is_symlink():
                shutil.copytree(p, dst / part, ignore=ignore, dirs_exist_ok=True)
            elif p.is_file() and not p.is_symlink():
                shutil.copy2(p, dst / part)
        fio.write_text(dst / COPY_MARKER, _source_stamp(src, max_age=STAMP_AGE_S) + "\n")  # of what was copied just now
    return True


def remove_plugin_copy(home: Path) -> bool:
    dst = Path(home) / "plugins" / PLUGIN_ID
    if not fio.is_real_dir(dst) or fio.read_text(dst / COPY_MARKER, 200) is None:
        return False
    fio.remove_dir(dst, Path(home) / "plugins")
    with contextlib.suppress(OSError):
        os.rmdir(Path(home) / "plugins")  # only when it is empty now
    return True


def option_a(home: Path, on: bool = True) -> dict:
    """Switch the plugin on (or off again) in one twin's Hermes bot. Never raises: what worked is reported."""
    out = {"enabled": False, "code": False}
    try:
        if on:
            out["enabled"] = enable_plugin(home)
            out["code"] = place_plugin(home) if out["enabled"] else False
        else:
            disable_plugin(home)
            remove_plugin_copy(home)
    except (OSError, ProfileBlocked):
        pass
    return out


# ---- profile.yaml (how Hermes shows the bot) ---------------------------------------------------------------------
def shown_name(profile: Any, name: Any) -> str:
    """The name Hermes shows for a twin's bot. Its own name; with a number behind it ("Test Scout 2") when the
    bot had to take a numbered folder because a Hermes bot of that name was already on this computer, so the two
    can be told apart in Hermes' list of bots."""
    text = " ".join(name.split())[:100] if isinstance(name, str) else ""
    base = slug(text)
    m = re.search(r"-(\d{1,3})\Z", profile) if isinstance(profile, str) and profile != base else None
    if m and text and profile == base[:64 - len(m.group(0))].rstrip("-_") + m.group(0):  # as unique_profile makes it
        return f"{text[:96]} {m.group(1)}"
    return text


def write_meta(home: Path, name: str, description: str) -> None:
    name = shown_name(Path(home).name, name)
    yaml = _yaml()
    if yaml is None:
        return
    path = Path(home) / "profile.yaml"
    text = fio.read_text(path, hs.CONFIG_CAP)
    if text is None and os.path.lexists(path):
        return
    try:
        doc = yaml.safe_load(text) if text else {}
    except Exception:
        return
    if not isinstance(doc, dict):
        doc = {}
    want = {**doc, "display_name": " ".join(name.split())[:100], "description": description.strip()[:2000],
            "description_auto": False}
    if want != doc:
        fio.write_text(path, yaml.safe_dump(want, default_flow_style=False, sort_keys=False, allow_unicode=True))


# ---- a mirrored twin's Hermes bot: make, update, take away, bring back --------------------------------------------
def is_mirrored(home: Path) -> bool:
    text = fio.read_text(Path(home) / "SOUL.md", hs.SOUL_CAP)
    return bool(text) and get_block(text, TWIN_BEGIN, TWIN_END) is not None


def create(profile: str, name: str, description: str, note: str) -> Path:
    """Make the Hermes bot of a mirrored twin. The name must be free (see unique_profile)."""
    home = profile_dir(profile)
    if name_taken(profile) or not clear_shell(profile):
        # (said with the twin's name, never with the folder name the plugin made from it)
        raise ProfileBlocked(f"Another Hermes bot on this computer already has the name {name}.")
    clear_moved(profile)  # (a bot of this name is here again: Hermes may list and serve it)
    try:
        _make(home, name, description, note)
    except BaseException:
        fio.remove_dir(home, profiles_root())  # nothing half made stays behind (the name stays free for the retry)
        raise
    return home


def finish(profile: str, name: str, description: str, note: str) -> Path:
    """Make the Hermes bot of a mirrored twin under a name this machine noted for it beforehand, or finish one
    whose making was cut short (the process was killed part-way): what is already there is kept, what is missing
    is written, leftovers of unfinished writes are cleared. The caller has checked that the folder, when there,
    is this twin's own (fleet._own_entry)."""
    home = profile_dir(profile)
    if not os.path.lexists(home):
        return create(profile, name, description, note)
    if not fio.is_real_dir(home):
        raise ProfileBlocked(f"The folder of the Hermes bot {name} is not a normal folder.")
    try:
        for entry in list(os.scandir(home)):
            if _OWN_TMP_RE.match(entry.name) and entry.is_file(follow_symlinks=False):
                os.unlink(entry.path)  # (a write that never finished; the file it was for is written below)
    except OSError:
        pass
    _make(home, name, description, note, fresh=not os.path.lexists(home / "config.yaml"))
    return home


def _make(home: Path, name: str, description: str, note: str, fresh: bool = True) -> None:
    """`fresh` False: a settings file is already there (finish); it is kept and only gets the plugin's switch."""
    try:
        store.ensure_dir(home)
        config: dict = dict(default_model_doc())
        yaml = _yaml()
        config["plugins"] = {"enabled": [PLUGIN_ID]}
        if not fresh:
            enable_plugin(home)
        elif yaml is not None:
            fio.write_text(home / "config.yaml",
                           yaml.safe_dump(config, default_flow_style=False, sort_keys=True, allow_unicode=True))
        else:
            fio.write_text(home / "config.yaml", _PLUGINS_BLOCK)
        write_soul(home, twin=(name, description), fleet=note)
        write_meta(home, name, description)
        with contextlib.suppress(OSError):
            place_plugin(home)
    except OSError as exc:
        raise ProfileBlocked("Check that the disk is not full, then press Try again.", "write_failed") from exc


def update(profile: str, name: str, description: str, note: str) -> bool:
    """Bring an existing mirrored twin's Hermes bot up to date. True when a file changed. A bot that went missing
    is made again."""
    home = profile_dir(profile)
    if not fio.is_real_dir(home):
        if os.path.lexists(home):
            raise ProfileBlocked(f"The folder of the Hermes bot {name} is not a normal folder.")
        create(profile, name, description, note)
        return True
    try:
        changed = write_soul(home, twin=(name, description), fleet=note)
        write_meta(home, name, description)
        option_a(home, True)
        return changed
    except OSError as exc:
        raise ProfileBlocked("Check that the disk is not full, then press Try again.", "write_failed") from exc


def take_away(profile: str, kept_dir: Path) -> bool:
    """Move a mirrored twin's Hermes bot into the kept folder. False when there was no such bot."""
    home = profile_dir(profile)
    if not fio.is_real_dir(home):
        return False
    try:
        fio.move_dir(home, Path(kept_dir) / "profile")
    except OSError as exc:
        raise ProfileBlocked("Close any chat with this twin, then press Try again.", "write_failed") from exc
    return True


def bring_back(kept_dir: Path, profile: str) -> Path:
    """Move a kept Hermes bot back under `profile` (which must be free)."""
    home = profile_dir(profile)
    src = Path(kept_dir) / "profile"
    if not fio.is_real_dir(src):
        raise ProfileBlocked("That Hermes bot is no longer kept.", "unknown_kept")
    if name_taken(profile) or not clear_shell(profile):
        raise ProfileBlocked("A Hermes bot of that name exists on this computer now.")
    clear_moved(profile)  # (the bot is back under this name: Hermes may list and serve it again)
    try:
        fio.move_dir(src, home)
    except OSError as exc:
        raise ProfileBlocked("Check that the disk is not full, then try again.", "write_failed") from exc
    with contextlib.suppress(OSError):
        os.rmdir(kept_dir)
    return home
