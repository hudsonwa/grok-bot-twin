"""A twin's skills on the owner's other computers.

The computer with the Grok app can send each twin's skills to the linked computers, over the same signed link
the twin list travels on. Off until the owner switches it on there. One direction: a linked computer takes
skills, and what is changed on a linked computer stays on that computer.

- A SKILL is a folder with a `SKILL.md` in the twin's own `skills` folder, directly or one folder down
  (`skills/<name>` or `skills/<group>/<name>`). What travels is its TEXT files only, after the plugin's
  password filter (`secrets.redact`): files that are not text, are large, start with a dot, are links or are
  named like key files never leave, and password-like text inside a file is replaced by a marker.
- The sharing computer keeps a version number per skill (it grows whenever the text that would travel changes)
  and the last ten versions as they were sent. A linked computer keeps the folder it had whenever one is
  replaced or taken away (ten per skill, 30 days), so every step can be put back.
- A linked computer never overwrites a skill that was changed there: it shows "changed here" or, when the
  sharing computer changed it too, a choice. A skill switched off on a linked computer is left alone for good
  ("only on this computer").

State: `fleet-skills.json` and `fleet-skills-kept/` in the plugin's data folder. Wire: fleet_items.py says how
the two computers find out that both know skills; the content travels on `/v1/xs/skills`.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets as _rand
import shutil
import stat
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from . import fleet, fleet_io as fio, fleet_items, fleet_profiles as fp, store
from . import osflags as _osflags
from . import secrets as _secrets
from .engine import EngineError
from .model import REF_RE, iso, now_dt, now_iso, parse_iso, plural

log = logging.getLogger(__name__)

V = 1
ROUTE = "/v1/xs/skills"
STATE_FILE = "fleet-skills.json"
KEPT_DIR = "fleet-skills-kept"
MARK = "[left out: looks like a password or key]"
MAX_SKILLS = 200          # per twin
MAX_FILES = 40            # text files of one skill
FILE_CAP = 200 * 1024     # one file
SKILL_CAP = 600 * 1024    # one skill, all its text
ANSWER_BUDGET = 1300 * 1024   # of one answer's skill texts, as they are sent (the receiving side reads 2 MB at most)
MAX_LISTED = 2000              # skills named in one answer, all twins together
MAX_WANT = 8
MAX_REPORT = 40
KEEP_VERSIONS = 10        # kept copies of what the other computer sent, per skill
KEEP_OWN = 40             # kept folders that held the owner's own text, per skill (they go by age, 30 days)
KEEP_TOTAL = 400          # kept folders in all on one computer
KEEP_DAYS = 30
REMOVED_DAYS = 90
WALK_CAP = 400
SEG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
VID_RE = re.compile(r"^(?:k-[0-9a-f]{12}|v[0-9]{1,9})\Z")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}\Z")
_SECRET_STEMS = {"token", "tokens", "secret", "secrets", "credential", "credentials", "auth", "password",
                 "passwords", "passwd", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "env", "netrc", "htpasswd"}
_SECRET_EXTS = {"pem", "key", "p12", "pfx", "jks", "keystore", "kdbx", "env"}

MESSAGES = {
    "unknown_skill": ("That skill is not here any more.", ""),
    "unknown_version": ("That earlier version is no longer kept.", ""),
    "no_choice": ("There is nothing to choose for that skill any more.", ""),
    "skills_off": ("Skills are switched off on this computer.", "Switch on Skills first."),
    "not_linked": ("This computer neither shares its twins nor follows another computer.",
                   "Share your twins or link this computer first."),
    "write_failed": ("Hermes could not save that skill on this computer.",
                     "Close any program that has the skill's files open, check that the disk is not full, then "
                     "try again."),
}


class SkillsError(EngineError):
    def __init__(self, code: str) -> None:
        msg, hint = MESSAGES[code]
        super().__init__(code, msg, hint)


# ---- names -----------------------------------------------------------------------------------------------------------
def _seg_ok(name: Any) -> bool:
    return (isinstance(name, str) and bool(SEG_RE.match(name)) and not name.endswith(".")
            and name.split(".")[0].lower() not in fp._WINDOWS_DEVICES)


def key_ok(key: Any) -> bool:
    """`name` or `group/name`, each a plain folder name."""
    if not isinstance(key, str) or len(key) > 130:
        return False
    parts = key.split("/")
    return 1 <= len(parts) <= 2 and all(_seg_ok(p) for p in parts)


def _secret_name(name: str) -> bool:
    low = name.lower()
    return low.split(".")[0] in _SECRET_STEMS or low.rsplit(".", 1)[-1] in _SECRET_EXTS


def _path_ok(rel: Any) -> bool:
    if not isinstance(rel, str) or len(rel) > 300:
        return False
    parts = rel.split("/")
    return 1 <= len(parts) <= 4 and all(_seg_ok(p) for p in parts) and not _secret_name(parts[-1])


def _when(value: Any) -> str:
    dt = parse_iso(value)
    if not dt:
        return "earlier"
    dt = dt.astimezone()
    return f"{dt.day} {dt.strftime('%b %H:%M')}"


# ---- reading a skill folder ------------------------------------------------------------------------------------------
def _walk(folder: Path) -> tuple:
    """([(relative path, Path or None, size, mtime)], too many). None marks something that never travels as it
    is: a link, a folder too deep or with a name that is not plain."""
    out: list = []
    seen = [0]

    def go(d: Path, rel: str, depth: int) -> None:
        try:
            with os.scandir(d) as it:
                ents = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        for e in ents:
            seen[0] += 1
            if seen[0] > WALK_CAP:
                return
            try:
                p = Path(e.path)
                if e.is_symlink():
                    out.append((rel + e.name, None, 0, 0))
                elif e.is_dir(follow_symlinks=False):
                    if depth < 3 and _seg_ok(e.name) and fio.is_real_dir(p):
                        go(p, rel + e.name + "/", depth + 1)
                    else:
                        out.append((rel + e.name + "/", None, 0, 0))
                elif e.is_file(follow_symlinks=False):
                    s = e.stat(follow_symlinks=False)
                    out.append((rel + e.name, p, s.st_size, s.st_mtime_ns))
                else:
                    out.append((rel + e.name, None, 0, 0))
            except OSError:
                continue

    go(folder, "", 0)
    return out, seen[0] > WALK_CAP


def _sig(entries: list) -> str:
    raw = json.dumps([(rel, size, mtime) for rel, _p, size, mtime in entries])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def quick_sig(folder: Path) -> Optional[str]:
    """A cheap fingerprint of a skill folder (names, sizes, dates; nothing is read). None: not a skill."""
    if not fio.is_real_dir(folder):
        return None
    entries, _ = _walk(folder)
    if not any(rel == "SKILL.md" and p is not None for rel, p, _s, _m in entries):
        return None
    return _sig(entries)


def digest_of(files: list) -> str:
    h = hashlib.sha256()
    for f in sorted(files, key=lambda f: f["path"]):
        h.update(f["path"].encode("utf-8") + b"\0" + hashlib.sha256(f["text"].encode("utf-8")).hexdigest().encode()
                 + b"\n")
    return h.hexdigest()



# Password-like text the plugin's general check (secrets.redact) lets through, because it looks for tokens of
# known makes and long values: a short password after a name that says what it is, in a settings line, a JSON
# object or a shell export; a pass inside an address; a bearer value. Only the value is replaced.
_WORDS = r"(?:pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|credentials?)"
_NAMED_QUOTED = re.compile(r"""(?i)(""" + _WORDS + r"""s?["']?\]?[ \t]{0,8}[:=][ \t]{0,8})(["'])([^"'\n]{4,400})\2""")
_NAMED_BARE = re.compile(r"""(?im)(""" + _WORDS + r"""s?["']?(?::[ \t]{0,8}|=))([^\s"'`,;(){}\[\]<>$]{6,400})(?=[\s,;]|$)""")
_IN_ADDRESS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,20}://[^/\s:@]{1,80}:)([^@\s/\[\]]{3,200})(?=@)")
_BEARER = re.compile(r"(?i)\b(bearer[ \t]{1,8})([A-Za-z0-9._~+/=-]{16,4000})")
_NOT_A_SECRET = {"string", "str", "required", "optional", "boolean", "bool", "number", "integer", "int", "null",
                 "none", "true", "false", "yes", "no", "changeme", "example", "your-password", "password",
                 "secret", "token", "undefined", "redacted", "hidden"}


def _hide_named(m: Any) -> str:
    value = m.group(m.lastindex)
    if value.lower().strip("*.x-_ ") in _NOT_A_SECRET or not value.strip("*.xX-_ ") or value.isdigit() \
            or value == MARK:
        return m.group(0)
    quote = m.group(2) if m.lastindex == 3 else ""
    return f"{m.group(1)}{quote}{MARK}{quote}"


def _clean_once(text: str) -> str:
    out = _secrets.redact(text, MARK)
    out = _NAMED_QUOTED.sub(_hide_named, out)
    out = _NAMED_BARE.sub(_hide_named, out)
    out = _IN_ADDRESS.sub(lambda m: m.group(1) + MARK, out)
    return _BEARER.sub(lambda m: m.group(1) + MARK, out)


def clean_text(text: str) -> str:
    """THE FILTER: what one file's text looks like when it leaves, and again when it arrives. Applied until it
    changes nothing more, so that the receiving computer's own pass over the same text leaves it as it is (the
    digest both sides compare is taken over this text)."""
    for _ in range(4):
        new = _clean_once(text)
        if new == text:
            break
        text = new
    return text


def scan(folder: Path) -> Optional[dict]:
    """What would travel of one skill folder: its text files after the filter, their digest, and what was left
    out. None when it is not a skill (no SKILL.md)."""
    if not fio.is_real_dir(folder):
        return None
    entries, overflow = _walk(folder)
    if not any(rel == "SKILL.md" and p is not None for rel, p, _s, _m in entries):
        return None
    files, left, masked, total, too_big = [], 0, 0, 0, overflow
    for rel, p, size, _m in entries:
        if p is None or not _path_ok(rel) or size > FILE_CAP:
            left += 1
            continue
        text = fio.read_text(p, FILE_CAP)
        if text is None or "\x00" in text:
            left += 1
            continue
        clean = clean_text(text)
        n = len(clean.encode("utf-8"))
        if n > FILE_CAP:  # (the marker is longer than a short password: a file at the limit can grow past it)
            left += 1
            continue
        if len(files) >= MAX_FILES or total + n > SKILL_CAP:
            too_big = True
            break
        masked += max(0, clean.count(MARK) - text.count(MARK))
        total += n
        files.append({"path": rel, "text": clean})
    if not any(f["path"] == "SKILL.md" for f in files):
        too_big = True
    if len(json.dumps(files, ensure_ascii=False).encode("utf-8")) > ANSWER_BUDGET - 4096:
        too_big = True  # (text that needs much quoting: it would never fit an answer)
    return {"sig": _sig(entries), "digest": digest_of(files), "files": files, "size": total, "left_out": left,
            "masked": masked, "too_big": too_big}


def skills_root(profile: Any) -> Optional[Path]:
    try:
        root = fp.profile_dir(profile) / "skills"
    except Exception:
        return None
    return root



def skill_dirs(profile: Any) -> Optional[dict]:
    """{key: folder} for the skills of one Hermes bot, never through a link. {} when the bot has no skills
    folder; None when the bot's folder or its skills folder cannot be read (which says nothing about its skills)."""
    root = skills_root(profile)
    out: dict = {}
    if root is None or not fio.is_real_dir(root.parent):
        return None
    if not os.path.lexists(root):
        return out
    if not fio.is_real_dir(root):
        return None
    low: set = set()

    def names(d: Path) -> Optional[list]:
        try:
            with os.scandir(d) as it:
                return sorted(e.name for e in it)[:WALK_CAP]
        except OSError:
            return None

    def is_skill(d: Path) -> bool:
        f = d / "SKILL.md"
        return os.path.isfile(f) and not os.path.islink(f)

    def add(key: str, d: Path) -> None:
        if key.lower() not in low and len(out) < MAX_SKILLS:
            low.add(key.lower())
            out[key] = d

    top = names(root)
    if top is None:
        return None
    for name in top:
        d = root / name
        if not _seg_ok(name) or not fio.is_real_dir(d):
            continue
        if is_skill(d):
            add(name, d)
            continue
        for sub in names(d) or []:
            d2 = d / sub
            if _seg_ok(sub) and fio.is_real_dir(d2) and is_skill(d2):
                add(f"{name}/{sub}", d2)
    return out


def _folder(profile: Any, key: str) -> Optional[Path]:
    root = skills_root(profile)
    if root is None or not key_ok(key):
        return None
    return root.joinpath(*key.split("/"))


# ---- state file ------------------------------------------------------------------------------------------------------
def _path() -> Path:
    return store.central_dir() / STATE_FILE


def _kept_root() -> Path:
    return store.central_dir() / KEPT_DIR


def _blank_src() -> dict:
    return {"counter": 0, "rev": "0-00000000", "digest": "", "twins": {}, "seen": {}}


def _blank_fol() -> dict:
    return {"source": None, "rev": None, "heard": None, "twins": {}, "reported": ""}


def load() -> dict:
    raw = fio.read_json(_path(), None)
    st = {"on": None, "src": _blank_src(), "fol": _blank_fol(), "kept": []}
    if not isinstance(raw, dict):
        return st
    if isinstance(raw.get("on"), bool):
        st["on"] = raw["on"]
    for part in ("src", "fol"):
        got = raw.get(part)
        if isinstance(got, dict):
            for k, blank in st[part].items():
                v = got.get(k)
                if blank is None:
                    if v is None or isinstance(v, (str, dict)):
                        st[part][k] = v
                elif isinstance(v, type(blank)) and not isinstance(v, bool):
                    st[part][k] = v
    for part in ("src", "fol"):
        st[part]["twins"] = {tid: t for tid, t in st[part]["twins"].items()
                             if isinstance(tid, str) and REF_RE.match(tid) and isinstance(t, dict)
                             and isinstance(t.get("skills"), dict)}
        for t in st[part]["twins"].values():
            t["skills"] = {k: r for k, r in t["skills"].items() if key_ok(k) and isinstance(r, dict)}
            if not isinstance(t.get("removed"), dict):
                t["removed"] = {}
    if isinstance(raw.get("kept"), list):
        st["kept"] = [k for k in raw["kept"] if isinstance(k, dict) and isinstance(k.get("vid"), str)
                      and VID_RE.match(k["vid"]) and key_ok(k.get("key")) and isinstance(k.get("twin"), str)]
    return st


def save(sk: dict) -> None:
    fio.write_json(_path(), sk)


def is_on(sk: dict, role: str) -> bool:
    """The owner's switch on this computer. Never set: off where skills would LEAVE, on where they arrive."""
    if isinstance(sk.get("on"), bool):
        return sk["on"]
    return role == "follower"


# ---- kept folders (both sides) and sent versions (sharing computer) -------------------------------------------------

def _set_aside(sk: dict, tid: str, key: str, folder: Path, why: str, own: bool = False,
               spare: Optional[str] = None) -> str:
    """Move a skill folder into the kept place (nothing is deleted). `own`: it held text of the owner's own
    (changed here, or set aside by the owner's own act), which is kept for its 30 days however many versions
    arrive after it. Raises OSError when it cannot be moved."""
    vid = "k-" + _rand.token_hex(6)
    fio.move_dir(folder, _kept_root() / vid)
    sk["kept"].insert(0, {"vid": vid, "twin": tid, "key": key, "at": now_iso(), "why": why, "own": bool(own)})
    prune_kept(sk, spare=spare)
    return vid



def prune_kept(sk: dict, spare: Optional[str] = None) -> None:
    """Drop kept folders past their 30 days, copies of what the other computer sent beyond ten per skill, the
    owner's own beyond forty per skill, and the oldest beyond the cap in all. `spare`: one that is in use now."""
    cut = iso(now_dt() - timedelta(days=KEEP_DAYS))
    count: dict = {}
    keep = []
    for i, k in enumerate(sk["kept"]):  # newest first
        pair = (k.get("twin"), k.get("key"), bool(k.get("own")))
        count[pair] = count.get(pair, 0) + 1
        over = count[pair] > (KEEP_OWN if k.get("own") else KEEP_VERSIONS) or len(keep) >= KEEP_TOTAL
        if k["vid"] != spare and (over or not isinstance(k.get("at"), str) or k["at"] < cut):
            with contextlib.suppress(OSError):
                fio.remove_dir(_kept_root() / k["vid"], _kept_root())
        else:
            keep.append(k)
    sk["kept"] = keep


def _bundle_dir(tid: str, key: str) -> Path:
    return _kept_root() / "sent" / tid / hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _write_bundle(tid: str, key: str, version: int, got: dict) -> None:
    d = _bundle_dir(tid, key)
    fio.write_json(d / f"{version}.json", {"key": key, "version": version, "digest": got["digest"],
                                           "at": now_iso(), "files": got["files"]})
    try:
        old = sorted(int(n[:-5]) for n in os.listdir(d) if re.match(r"^[0-9]{1,9}\.json\Z", n))
    except OSError:
        return
    for n in old[:-KEEP_VERSIONS]:
        with contextlib.suppress(OSError):
            os.unlink(d / f"{n}.json")  # one exact file of the plugin's own, below its own data folder


def _read_bundle(tid: str, key: str, version: int) -> Optional[dict]:
    raw = fio.read_json(_bundle_dir(tid, key) / f"{int(version)}.json", None)
    if not isinstance(raw, dict) or raw.get("key") != key:
        return None
    files = _clean_file_list(raw.get("files"))
    return {"version": version, "digest": raw.get("digest"), "at": raw.get("at"), "files": files} if files else None


def _bundle_versions(tid: str, key: str) -> list:
    try:
        return sorted((int(n[:-5]) for n in os.listdir(_bundle_dir(tid, key)) if re.match(r"^[0-9]{1,9}\.json\Z", n)),
                      reverse=True)
    except OSError:
        return []


def _clean_file_list(raw: Any) -> Optional[list]:
    """Files of one skill as they may be written: plain relative paths, text within the caps, the filter applied
    again. None when the list is not usable at all (no SKILL.md, too much)."""
    if not isinstance(raw, list) or len(raw) > MAX_FILES:
        return None
    out, total, seen = [], 0, set()
    for f in raw:
        if not isinstance(f, dict) or not _path_ok(f.get("path")) or not isinstance(f.get("text"), str):
            return None
        low = f["path"].lower()
        if low in seen or "\x00" in f["text"]:
            return None
        seen.add(low)
        try:
            text = clean_text(f["text"])
            n = len(text.encode("utf-8"))
        except (UnicodeError, ValueError):
            return None
        total += n
        if n > FILE_CAP or total > SKILL_CAP:
            return None
        out.append({"path": f["path"], "text": text})
    return out if any(f["path"] == "SKILL.md" for f in out) else None



def _dir_ok(path: Path) -> bool:
    """A folder of the bot's own skills tree that may be written in: a real folder (never a link), the user's
    own, not writable by others. Hermes makes these with the account's own settings, so a folder writable by
    the user's private group (the default on several Linux systems) is accepted, as for the bot's folder."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode) or _osflags.is_link(st):
        return False
    if os.name != "posix":
        return True
    if st.st_uid != os.getuid() or st.st_mode & 0o002:
        return False
    return not st.st_mode & 0o020 or store._private_group(st.st_gid)


def _parent_for(profile: Any, key: str) -> Path:
    """The folder a skill's own folder sits in, made if missing and checked part by part. Raises OSError."""
    root = skills_root(profile)
    if root is None or not key_ok(key):
        raise store.UnsafePath("not a skill's place")
    store.ensure_dir(root.parent)  # the bot's own folder, by the store's rules
    parent = root.joinpath(*key.split("/")[:-1])
    for d in ([root] if parent == root else [root, parent]):
        if not os.path.lexists(d):
            _osflags.mkdir_private(d)
        if not _dir_ok(d):
            raise store.UnsafePath(f"refusing to write below {d.name}")
    return parent


def blocked(profile: Any, key: str) -> bool:
    """True when a skill of that name cannot be put on this computer without touching something that is not
    that skill: something else already has its place (a folder of other skills, a file, a link), or its place
    would be inside another skill."""
    root = skills_root(profile)
    folder = _folder(profile, key)
    if root is None or folder is None:
        return True
    if os.path.lexists(folder) and quick_sig(folder) is None:
        return True
    if folder.parent != root:
        if os.path.lexists(folder.parent / "SKILL.md"):
            return True
        if os.path.lexists(folder.parent) and not fio.is_real_dir(folder.parent):
            return True
    return os.path.lexists(root) and not fio.is_real_dir(root)


def _write_tree(tmp: Path, files: list) -> None:
    """A new folder with exactly these text files, made by this call (so nothing in it can be a link)."""
    _osflags.mkdir_private(tmp)
    nofollow = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    for f in files:
        parts = f["path"].split("/")
        d = tmp
        for part in parts[:-1]:
            d = d / part
            if not os.path.lexists(d):
                _osflags.mkdir_private(d)
        fd = os.open(d / parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(f["text"].replace("\r\n", "\n").encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())


def _move_in(tmp: Path, folder: Path) -> None:
    for attempt in range(15):
        try:
            os.rename(tmp, folder)
            return
        except PermissionError:  # (Windows: a scanner had a new file open for a moment)
            if attempt == 14:
                raise
            time.sleep(0.1)


def _sweep(parent: Path) -> None:
    """Take away what an interrupted write of this plugin left beside the skills (its own `.gbt-` folders, an
    hour old at least)."""
    try:
        with os.scandir(parent) as it:
            old = [e.name for e in it if re.match(r"^\.gbt-[0-9a-f]{8}\Z", e.name)
                   and e.is_dir(follow_symlinks=False) and time.time() - e.stat(follow_symlinks=False).st_mtime > 3600]
    except OSError:
        return
    for name in old[:20]:
        with contextlib.suppress(OSError):
            fio.remove_dir(parent / name, parent)


def _put(sk: dict, tid: str, key: str, profile: Any, make: Any, why: str, own: bool = False,
         spare: Optional[str] = None) -> Optional[str]:
    """Make the skill's folder hold what `make(tmp)` builds. The new folder is built beside the place first;
    only then is what was there MOVED to the kept place (its id is returned) and the new one moved in. When
    that last step fails, what was there is moved back: the skill is never left missing."""
    if blocked(profile, key):
        raise store.UnsafePath("something else has that skill's place")
    parent = _parent_for(profile, key)
    folder = _folder(profile, key)
    _sweep(parent)
    tmp = parent / f".gbt-{_rand.token_hex(4)}"
    vid = None
    try:
        make(tmp)
        if os.path.lexists(folder):
            vid = _set_aside(sk, tid, key, folder, why, own=own, spare=spare)
        try:
            _move_in(tmp, folder)
        except BaseException:
            if vid is not None and not os.path.lexists(folder):
                fio.move_dir(_kept_root() / vid, folder)  # back where it was
                sk["kept"] = [k for k in sk["kept"] if k["vid"] != vid]
            raise
    except BaseException:
        with contextlib.suppress(OSError):
            fio.remove_dir(tmp, parent)
        raise
    return vid


# ---- the sharing computer --------------------------------------------------------------------------------------------
def _shared_twins(fst: dict) -> dict:
    lst = (fst.get("share") or {}).get("list") or {}
    twins = lst.get("twins") if isinstance(lst.get("twins"), dict) else {}
    return {tid: t for tid, t in twins.items() if isinstance(tid, str) and REF_RE.match(tid) and isinstance(t, dict)}


def manifest(sk: dict, fst: dict) -> list:
    """THE ALLOWLIST of what the sharing computer says about its skills: per twin id, per shared skill its
    name, version, digest, date, size and number of files; and the names that left."""
    out = []
    listed = _shared_twins(fst)
    room = MAX_LISTED
    for tid in sorted(sk["src"]["twins"]):
        if tid not in listed:
            continue
        tw = sk["src"]["twins"][tid]
        skills, removed = [], []
        for key in sorted(tw["skills"]):
            r = tw["skills"][key]
            if r.get("share") is False:
                if r.get("listed"):  # only a skill that was shared before is named as gone; another is not named
                    removed.append({"name": key, "removed_at": r.get("off_at") or r.get("updated_at")})
            elif not r.get("too_big") and room > 0:
                room -= 1
                skills.append({"name": key, "version": r.get("version"), "digest": r.get("digest"),
                               "updated_at": r.get("updated_at"), "size": r.get("size"), "files": r.get("nfiles")})
        for key in sorted(tw["removed"]):
            at = tw["removed"][key]
            if key not in tw["skills"] and isinstance(at, dict) and at.get("listed"):
                removed.append({"name": key, "removed_at": at.get("at")})
        out.append({"id": tid, "skills": skills, "removed": removed})
    return out


def source_scan(fst: Optional[dict] = None) -> bool:
    """Sharing computer: look at every shared twin's skills folder and note what changed. Cheap when nothing
    did (names, sizes and dates only). True when what the linked computers are told has changed."""
    fst = fst or fleet.load()
    if fleet.role(fst) != "source":
        return False
    listed = _shared_twins(fst)
    with fleet.locked():
        sk = load()
        src = sk["src"]
        dirty = False
        now = now_iso()
        for tid in [t for t in src["twins"] if t not in listed]:
            del src["twins"][tid]
            dirty = True
        for tid, t in listed.items():
            dirs = skill_dirs(t.get("profile")) if t.get("profile") else {}
            if dirs is None:
                continue  # its folder cannot be read just now: that is not "its skills are gone"
            tw = src["twins"].setdefault(tid, {"skills": {}, "removed": {}})
            present = set()
            for key, folder in dirs.items():
                sig = quick_sig(folder)
                if sig is None:
                    continue
                present.add(key)
                rec = tw["skills"].get(key)
                if rec and rec.get("sig") == sig:
                    continue
                got = scan(folder)
                if got is None:
                    present.discard(key)
                    continue
                dirty = True
                if rec and rec.get("digest") == got["digest"] and bool(rec.get("too_big")) == got["too_big"]:
                    rec.update(sig=got["sig"], left_out=got["left_out"], masked=got["masked"])
                    continue
                gone = tw["removed"].pop(key, None)
                # (also above every version still kept as sent: numbers are never used twice for one skill)
                before = max(int((rec or {}).get("version") or 0),
                             int(gone.get("version") or 0) if isinstance(gone, dict) else 0,
                             max(_bundle_versions(tid, key), default=0))
                new = {"version": before + 1, "digest": got["digest"], "sig": got["sig"], "updated_at": now,
                       "size": got["size"], "nfiles": len(got["files"]), "left_out": got["left_out"],
                       "masked": got["masked"], "too_big": got["too_big"],
                       "share": (rec or {}).get("share", True), "off_at": (rec or {}).get("off_at"),
                       "listed": bool((rec or {}).get("listed"))}
                if not got["too_big"]:
                    _write_bundle(tid, key, new["version"], got)
                tw["skills"][key] = new
            for key in [k for k in tw["skills"] if k not in present]:
                tw["removed"][key] = {"at": now, "version": tw["skills"][key].get("version"),
                                      "listed": bool(tw["skills"][key].get("listed"))}
                del tw["skills"][key]
                dirty = True
            cut = iso(now_dt() - timedelta(days=REMOVED_DAYS))
            for key in [k for k, at in tw["removed"].items() if not isinstance(at, dict) or str(at.get("at")) < cut]:
                del tw["removed"][key]
                dirty = True
        if is_on(sk, "source"):
            for tw in src["twins"].values():  # what a linked computer has been told of by name
                for rec in tw["skills"].values():
                    if rec.get("share") is not False and not rec.get("too_big") and not rec.get("listed"):
                        rec["listed"] = True
                        dirty = True
        body = json.dumps({"on": is_on(sk, "source"), "twins": manifest(sk, fst)}, sort_keys=True)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        changed = digest != src.get("digest")
        if changed:
            src["counter"] = int(src.get("counter") or 0) + 1
            src["digest"] = digest
            src["rev"] = f"{src['counter']}-{digest[:8]}"
        if dirty or changed:
            save(sk)
    return changed


def source_tick() -> None:
    """With every look of the sharing loop: notice changed skills and wake the waiting computers. Never raises."""
    try:
        if not os.path.lexists(_path()):
            return  # the switch was never touched on this computer: nothing to look at
        fst = fleet.load()
        if fleet.role(fst) == "source" and is_on(load(), "source") and source_scan(fst):
            fleet.announce()
    except Exception:
        log.debug("grok-bot-twin: could not look at the twins' skills", exc_info=True)


def _wire_rev() -> str:
    """What the sharing computer tells a linked one that named skills, in the "nothing changed" answer: the rev
    of its list of skills, or "off" while the owner has skills switched off here."""
    if not os.path.lexists(_path()):
        return "off"
    sk = load()
    return sk["src"]["rev"] if is_on(sk, "source") else "off"


def _clean_report(raw: Any) -> dict:
    out = {"conflict": [], "changed": [], "off": [], "failed": 0, "held": 0}
    if not isinstance(raw, dict):
        return out
    left = MAX_REPORT
    for part in ("conflict", "changed", "off"):
        for pair in (raw.get(part) if isinstance(raw.get(part), list) else [])[:left]:
            if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str) and REF_RE.match(pair[0]) \
                    and key_ok(pair[1]):
                out[part].append([pair[0], pair[1]])
        left = max(0, left - len(out[part]))
    for part in ("failed", "held"):
        n = raw.get(part)
        out[part] = n if isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 10 ** 6 else 0
    return out


def handle(asked: Any, body: dict) -> tuple:
    """`POST /v1/xs/skills` on the sharing computer: signed and counted like a sync request. Answers what skills
    there are and, for up to eight that the asking computer names, their text."""
    from . import fleet_server as server
    again = server._replayed(asked, body)
    if again is not None:
        return again
    fst = fleet.load()
    f, key = asked.by(fst)
    if f is None:
        return server._unlinked(key, asked)
    signed = (key, asked.nonce)
    v = body.get("xs")
    if body.get("proto") != fleet.PROTO or not isinstance(v, int) or isinstance(v, bool) or v < 1:
        return server._err(400, "bad_request") + (signed,)
    base = {"schema": fleet.SCHEMA, "xs": min(v, V)}
    if not os.path.lexists(_path()):
        return 200, {**base, "on": False, "rev": "off"}, signed
    report = _clean_report(body.get("report"))
    with fleet.locked():
        sk = load()
        if sk["src"]["seen"].get(f["id"]) != report:
            sk["src"]["seen"][f["id"]] = report
            for mid in [m for m in sk["src"]["seen"] if m not in (fst["share"].get("followers") or {})]:
                del sk["src"]["seen"][mid]
            save(sk)
    if not is_on(sk, "source") or not fst["share"].get("on"):
        return 200, {**base, "on": False, "rev": "off"}, signed
    files, budget = [], ANSWER_BUDGET
    want = body.get("want") if isinstance(body.get("want"), list) else []
    listed = _shared_twins(fst)
    for w in want[:MAX_WANT]:
        if not isinstance(w, dict) or w.get("twin") not in listed or not key_ok(w.get("skill")):
            continue
        rec = (sk["src"]["twins"].get(w["twin"]) or {}).get("skills", {}).get(w["skill"])
        if not rec or rec.get("share") is False or rec.get("too_big") or rec.get("version") != w.get("version"):
            continue
        b = _read_bundle(w["twin"], w["skill"], rec["version"])
        if b is None or b.get("digest") != rec.get("digest"):
            continue
        one = {"twin": w["twin"], "skill": w["skill"], "version": rec["version"], "digest": rec["digest"],
               "files": b["files"]}
        size = len(json.dumps(one, ensure_ascii=False).encode("utf-8"))  # as it travels, quoting included
        if size > budget:
            continue  # asked for again with the next request
        budget -= size
        files.append(one)
    return 200, {**base, "on": True, "rev": sk["src"]["rev"], "twins": manifest(sk, fst), "files": files}, signed


# ---- the linked computer ---------------------------------------------------------------------------------------------
def _linked_twins(fst: dict) -> dict:
    ln = fst.get("link") or {}
    return {tid: e for tid, e in (ln.get("twins") or {}).items()
            if isinstance(tid, str) and REF_RE.match(tid) and isinstance(e, dict) and not e.get("making")
            and isinstance(e.get("profile"), str)}


def _own_link(sk: dict, fst: dict) -> None:
    """What this computer notes about another computer's skills belongs to one link; a new link starts anew."""
    ln = fst.get("link")
    sid = (ln.get("source") or {}).get("id") if ln else None
    if sk["fol"].get("source") != sid:
        sk["fol"] = _blank_fol()
        sk["fol"]["source"] = sid


def clean_manifest(obj: dict) -> Optional[dict]:
    """The sharing computer's answer, reduced to what is believed: ids, plain names, numbers, digests, dates."""
    if obj.get("on") is not True:
        return {"on": False, "rev": "off", "twins": {}}
    rev = obj.get("rev")
    if not isinstance(rev, str) or not fleet.REV_RE.match(rev) or not isinstance(obj.get("twins"), list):
        return None
    twins: dict = {}
    for t in obj["twins"][:fleet.MAX_TWINS]:
        if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not REF_RE.match(t["id"]):
            continue
        skills, removed = {}, {}
        for s in (t.get("skills") if isinstance(t.get("skills"), list) else [])[:MAX_SKILLS]:
            if not isinstance(s, dict) or not key_ok(s.get("name")):
                continue
            ver, dig = s.get("version"), s.get("digest")
            if not isinstance(ver, int) or isinstance(ver, bool) or not 0 < ver <= 10 ** 9 \
                    or not isinstance(dig, str) or not DIGEST_RE.match(dig):
                continue
            skills[s["name"]] = {"version": ver, "digest": dig, "updated_at": fleet._iso_or_none(s.get("updated_at"))}
        for s in (t.get("removed") if isinstance(t.get("removed"), list) else [])[:MAX_SKILLS * 2]:
            if isinstance(s, dict) and key_ok(s.get("name")) and s["name"] not in skills:
                removed[s["name"]] = fleet._iso_or_none(s.get("removed_at")) or now_iso()
        twins[t["id"]] = {"skills": skills, "removed": removed}
    return {"on": True, "rev": rev, "twins": twins}


def clean_files(obj: dict) -> dict:
    out: dict = {}
    for b in (obj.get("files") if isinstance(obj.get("files"), list) else [])[:MAX_WANT]:
        if not isinstance(b, dict) or not isinstance(b.get("twin"), str) or not REF_RE.match(b["twin"]) \
                or not key_ok(b.get("skill")) or not isinstance(b.get("version"), int):
            continue
        files = _clean_file_list(b.get("files"))
        if files is not None:
            out[(b["twin"], b["skill"], b["version"])] = files
    return out


def _take_manifest(sk: dict, man: dict, linked: Any = None) -> None:
    """Note what the sharing computer has now. Only for twins this computer holds, and a bounded number of
    names per twin, so that no list can make this computer's notes grow without end."""
    fol = sk["fol"]
    fol["rev"] = man["rev"]
    fol["heard"] = {"v": V, "on": man["on"], "rev": man["rev"]}
    if not man["on"]:
        # switched off there: what is here stays as it is, nothing is fetched or taken away, and what was last
        # taken is remembered for when it is switched on again
        for tw in fol["twins"].values():
            for rec in tw["skills"].values():
                rec["their"] = None
        return
    for tid, tw in fol["twins"].items():
        if tid not in man["twins"]:
            for rec in tw["skills"].values():
                rec["their"] = None
    for tid, t in man["twins"].items():
        if linked is not None and tid not in linked:
            continue
        tw = fol["twins"].setdefault(tid, {"skills": {}, "removed": {}})
        for key, s in t["skills"].items():
            if key not in tw["skills"] and len(tw["skills"]) >= 2 * MAX_SKILLS:
                continue
            tw["skills"].setdefault(key, {"version": 0})["their"] = dict(s)
        for key, rec in tw["skills"].items():
            if key in t["removed"]:
                rec["their"] = {"removed": t["removed"][key]}
            elif key not in t["skills"]:
                rec["their"] = None


def judge(rec: dict) -> str:
    """What is to be done about one skill on a linked computer, from what is on disk here (`disk`), what was
    last taken (`version`, `digest`, `tdigest`) and what the sharing computer has now (`their`)."""
    their, disk = rec.get("their"), rec.get("disk")
    base = isinstance(rec.get("version"), int) and rec["version"] > 0
    if rec.get("off"):
        return "off"
    if not isinstance(their, dict):
        return "kept" if base else "only_here"
    if "removed" in their:
        if not base:
            return "only_here"
        if disk is None:
            return "drop"
        if rec.get("take") == "removed" or disk == rec.get("digest"):
            return "remove"
        return "conflict_removed"
    tv, td = their.get("version"), their.get("digest")
    if not isinstance(tv, int) or not isinstance(td, str):
        return "kept" if base else "only_here"
    if disk is not None and disk == td:
        return "adopt"
    if rec.get("take") == tv:
        return "apply"
    if not base:
        return "apply" if disk is None else "conflict_new"
    src_changed = td != rec.get("tdigest")
    if disk is not None and disk == rec.get("digest"):
        return "apply" if src_changed else "in_sync"
    if src_changed:
        return "conflict"
    return "gone_here" if disk is None else "changed_here"


def _refresh_disk(folder: Optional[Path], rec: dict) -> None:
    sig = quick_sig(folder) if folder is not None else None
    if sig is None:
        rec["disk"], rec["sig"] = None, None
    elif sig != rec.get("sig") or "disk" not in rec:
        got = scan(folder)
        rec["sig"] = sig if got else None
        rec["disk"] = got["digest"] if got else None


def _settle(fst: dict, sk: dict, files: dict) -> tuple:
    """Bring this computer's skills in line as far as it may go without asking the owner. Returns (what is
    still wanted from the sharing computer, how many skills changed on disk). Called with the lock held."""
    fol = sk["fol"]
    wants, done = [], 0
    linked = _linked_twins(fst)
    taking = is_on(sk, "follower")
    for tid, tw in fol["twins"].items():
        entry = linked.get(tid)
        if entry is None:
            continue
        for key in list(tw["skills"]):
            try:
                n = _settle_one(sk, tid, key, tw, entry["profile"], files, taking, wants)
            except Exception:  # one record that is not as expected never stops the others
                log.debug("grok-bot-twin: could not settle a skill", exc_info=True)
                n = 0
            done += n
    return wants, done


def _settle_one(sk: dict, tid: str, key: str, tw: dict, profile: str, files: dict, taking: bool, wants: list) -> int:
    rec = tw["skills"][key]
    folder = _folder(profile, key)
    _refresh_disk(folder, rec)
    what = judge(rec)
    their = rec.get("their") if isinstance(rec.get("their"), dict) else {}
    if rec.get("error") == "blocked" and what not in ("apply", "conflict_new"):
        rec.pop("error", None)
    if what == "adopt":
        rec.update(version=their["version"], tdigest=their["digest"], digest=rec["disk"])
        rec.pop("take", None)
        rec.pop("error", None)
    elif what == "drop" or (what == "only_here" and rec.get("disk") is None):
        del tw["skills"][key]
    elif not taking or folder is None:
        return 0
    elif what == "apply":
        if blocked(profile, key):
            rec["error"] = "blocked"  # something that is not this skill has its place: it is left alone
            return 0
        got = files.get((tid, key, their["version"]))
        if got is None or digest_of(got) != their["digest"]:
            wants.append({"twin": tid, "skill": key, "version": their["version"]})
            return 0
        own = rec.get("disk") is not None and rec.get("disk") != rec.get("digest")  # the owner's own text here
        try:
            _put(sk, tid, key, profile, lambda tmp: _write_tree(tmp, got), "replaced", own=own)
        except Exception:
            log.debug("grok-bot-twin: could not write a skill", exc_info=True)
            rec["error"] = "write_failed"
            return 0
        _refresh_disk(folder, rec)
        rec.update(version=their["version"], tdigest=their["digest"], digest=rec.get("disk"))
        rec.pop("take", None)
        rec.pop("error", None)
        return 1
    elif what == "remove":
        own = rec.get("disk") != rec.get("digest")
        try:
            _set_aside(sk, tid, key, folder, "removed", own=own)
        except Exception:
            log.debug("grok-bot-twin: could not set a skill aside", exc_info=True)
            rec["error"] = "write_failed"
            return 0
        del tw["skills"][key]
        return 1
    return 0


def _report(sk: dict) -> dict:
    out = {"conflict": [], "changed": [], "off": [], "failed": 0, "held": 0}
    for tid, tw in sk["fol"]["twins"].items():
        for key, rec in tw["skills"].items():
            what = judge(rec)
            if rec.get("error"):
                out["failed"] += 1
            if what in ("in_sync", "adopt"):
                out["held"] += 1
            part = {"conflict": "conflict", "conflict_new": "conflict", "conflict_removed": "conflict",
                    "changed_here": "changed", "gone_here": "changed", "off": "off"}.get(what)
            if part and sum(len(out[p]) for p in ("conflict", "changed", "off")) < MAX_REPORT:
                out[part].append([tid, key])
    return out


def _have() -> Any:
    """The rev this computer was last TOLD (not the one it has fetched: a fetch that fails must not make the
    other computer answer every waiting request at once; what is still to fetch is tried again with every
    answer, a minute apart at most). Not named at all while the owner has skills switched off here."""
    if not os.path.lexists(_path()):
        return ""
    sk = load()
    if not is_on(sk, "follower"):
        return fleet_items.SKIP
    said = sk["fol"].get("heard")
    return (said.get("rev") or "") if isinstance(said, dict) else ""


def _heard(svc: Any, rev: Optional[str]) -> None:
    if os.path.lexists(_path()) and not is_on(load(), "follower"):
        return
    follow(svc, heard={"v": V, "on": rev != "off", "rev": rev} if rev is not None else {"v": 0})


def follow(svc: Any, heard: Optional[dict] = None, force: bool = False) -> bool:
    """Linked computer: settle what can be settled here and, when the sharing computer has something this one
    does not hold (or was told to fetch), ask it. True when the two are in step afterwards. Never raises."""
    try:
        return _follow(svc, heard, force)
    except Exception:
        log.debug("grok-bot-twin: could not follow the other computer's skills", exc_info=True)
        return False


def _follow(svc: Any, heard: Optional[dict], force: bool) -> bool:
    from . import fleet_follow
    fst = fleet.load()
    ln = fst.get("link")
    if not ln:
        return False
    with fleet.locked():
        sk = load()
        before = json.dumps(sk, sort_keys=True)
        _own_link(sk, fst)
        fol = sk["fol"]
        if heard is not None:
            v = heard.get("v")
            fol["heard"] = {"v": v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= V else 0,
                            "on": heard.get("on") is True,
                            "rev": heard.get("rev") if isinstance(heard.get("rev"), str) else None}
        said = fol.get("heard") or {}
        wants, done = _settle(fst, sk, {})
        report = _report(sk)
        if json.dumps(sk, sort_keys=True) != before:
            save(sk)
    if not said.get("v") or not is_on(sk, "follower"):
        return False
    stale = json.dumps(report, sort_keys=True) != fol.get("reported")
    if not (force or wants or stale or said.get("rev") != fol.get("rev")):
        return True
    me = fleet.this_machine(fst)
    last = None
    for _ in range(12):
        try:
            status, obj, signed = fleet_follow._counted_request(
                ln, me["id"], ROUTE, {"proto": fleet.PROTO, "xs": V, "want": wants[:MAX_WANT], "report": report},
                timeout=20.0)
        except fleet_follow.Unreachable:
            return False
        if status != 200 or not signed:
            return False
        man = clean_manifest(obj)
        if man is None:
            return False
        files = clean_files(obj)
        with fleet.locked():
            fst = fleet.load()
            if not fst.get("link"):
                return False
            sk = load()
            _own_link(sk, fst)
            _take_manifest(sk, man, _linked_twins(fst))
            sk["fol"]["reported"] = json.dumps(report, sort_keys=True)
            wants, n = _settle(fst, sk, files)
            done += n
            report = _report(sk)
            save(sk)
        if not wants or wants == last:
            break
        last = wants
    if done:
        with contextlib.suppress(Exception):
            svc.emit("fleet", None, {"what": "synced", "count": done})
    return not wants


# ---- what the page and the terminal show ------------------------------------------------------------------------------
def _versions(sk: dict, tid: str, key: str, source: bool, current: int = 0) -> list:
    out = []
    for k in sk["kept"]:
        if k.get("twin") == tid and k.get("key") == key:
            why = {"replaced": "replaced", "removed": "taken away", "put_back": "set aside"}.get(k.get("why"), "set aside")
            out.append({"vid": k["vid"], "at": k.get("at"),
                        "label": f"This computer's version, {why} {_when(k.get('at'))}"})
    if source:
        for n in _bundle_versions(tid, key):
            if n != current:
                b = fio.read_json(_bundle_dir(tid, key) / f"{n}.json", {}) or {}
                out.append({"vid": f"v{n}", "at": b.get("at") if isinstance(b.get("at"), str) else None,
                            "label": f"Version {n}, from {_when(b.get('at'))}"})
    out.sort(key=lambda v: str(v.get("at")), reverse=True)
    return out[:KEEP_VERSIONS]


def _name_group(key: str) -> tuple:
    parts = key.split("/")
    return parts[-1], (parts[0] if len(parts) == 2 else None)


def _left_note(rec: dict) -> Optional[str]:
    bits = []
    if rec.get("left_out"):
        n = rec["left_out"]
        bits.append(f"{plural(n, 'file')} {'was' if n == 1 else 'were'} left out: not text, too large, or named like "
                    "a key file.")
    if rec.get("masked"):
        n = rec["masked"]
        bits.append(f"{plural(n, 'password-like text')} {'was' if n == 1 else 'were'} left out.")
    return " ".join(bits) or None


def _source_rows(sk: dict, fst: dict, on: bool) -> tuple:
    twins, counts = [], {"skills": 0, "conflicts": 0, "changed_here": 0, "only_here": 0, "off": 0}
    names = {f.get("id"): fp.machine_name(f.get("name")) for f in (fst["share"].get("followers") or {}).values()
             if isinstance(f, dict)}
    for tid, t in sorted(_shared_twins(fst).items(), key=lambda kv: str(kv[1].get("name")).lower()):
        rows = []
        tw = sk["src"]["twins"].get(tid) or {"skills": {}}
        for key in sorted(tw["skills"]):
            rec = tw["skills"][key]
            name, group = _name_group(key)
            shared = rec.get("share") is not False
            note = _left_note(rec)
            if rec.get("too_big"):
                state, label = "too_big", "not shared: over 40 text files or 600 KB"
            elif not on or not shared:
                state, label = "off", "not shared"
            else:
                state, label = "in_sync", f"shared, version {rec.get('version')}"
                own = sorted(names[m] for m, r in sk["src"]["seen"].items() if m in names and isinstance(r, dict)
                             and [tid, key] in (r.get("conflict") or []) + (r.get("changed") or []) + (r.get("off") or []))
                if own:
                    note = " ".join(x for x in (f"{fleet_and(own)} {'keeps' if len(own) == 1 else 'keep'} a version "
                                                "of its own." if len(own) == 1 else
                                                f"{fleet_and(own)} keep versions of their own.", note) if x)
            counts["skills"] += 1
            counts["off"] += state == "off"
            rows.append({"key": key, "name": name, "group": group, "version": int(rec.get("version") or 0),
                         "updated_at": rec.get("updated_at"), "state": state, "label": label,
                         "on": shared and not rec.get("too_big"), "can_switch": on and not rec.get("too_big"),
                         "switch_label": f"{name}: share it", "note": note, "choices": [],
                         "versions": _versions(sk, tid, key, True, int(rec.get("version") or 0))})
        twins.append({"id": tid, "name": str(t.get("name") or "Twin"), "skills": rows})
    return twins, counts


def fleet_and(names: list) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _follower_rows(sk: dict, fst: dict, other: str) -> tuple:
    twins, counts = [], {"skills": 0, "conflicts": 0, "changed_here": 0, "only_here": 0, "off": 0}
    for tid, e in sorted(_linked_twins(fst).items(), key=lambda kv: str(kv[1].get("name")).lower()):
        rows = []
        recs = (sk["fol"]["twins"].get(tid) or {"skills": {}})["skills"]
        keys = set(recs) | set(skill_dirs(e["profile"]) or {})
        for key in sorted(keys):
            rec = recs.get(key) or {"version": 0}
            name, group = _name_group(key)
            what = judge(rec)
            mine = {"keep": "mine", "label": "Keep this computer's"}
            theirs = {"keep": "theirs", "label": f"Keep {other}'s"}
            choices: list = []
            state, label = "in_sync", f"same as on {other}"
            if rec.get("error") == "blocked":
                state, label = "error", f"not taken: something else on this computer is called {name}"
            elif rec.get("error"):
                state, label = "error", "could not be saved on this computer"
            elif what == "off":
                state, label = "off", "only on this computer"
            elif what == "only_here":
                state, label = "only_here", f"only on this computer; {other} does not have it"
            elif what == "kept":
                state, label = "in_sync", f"as last sent by {other}"
            elif what in ("apply", "remove"):
                state, label = "waiting", f"on its way from {other}"
            elif what == "changed_here":
                state, label, choices = "changed_here", "changed on this computer; the change stays here", \
                    [{"keep": "theirs", "label": f"Put back {other}'s"}]
            elif what == "gone_here":
                state, label, choices = "changed_here", "removed on this computer", \
                    [{"keep": "theirs", "label": f"Put back {other}'s"}]
            elif what == "conflict":
                state, label, choices = "conflict", f"changed here and on {other}", [mine, theirs]
            elif what == "conflict_new":
                state, label, choices = "conflict", f"a different {name} was already on this computer", [mine, theirs]
            elif what == "conflict_removed":
                state, label, choices = "conflict", f"changed here, and no longer shared by {other}", \
                    [mine, {"keep": "theirs", "label": "Take it away here too"}]
            counts["skills"] += 1
            counts["conflicts"] += state == "conflict"
            counts["changed_here"] += state == "changed_here"
            counts["only_here"] += state == "only_here"
            counts["off"] += state == "off"
            their = rec.get("their") if isinstance(rec.get("their"), dict) else {}
            rows.append({"key": key, "name": name, "group": group, "version": int(rec.get("version") or 0),
                         "updated_at": their.get("updated_at"), "state": state, "label": label,
                         "on": state not in ("off", "only_here"), "can_switch": state != "only_here",
                         "switch_label": f"{name}: follow it from {other}", "note": None, "choices": choices,
                         "versions": _versions(sk, tid, key, False)})
        for key in sorted({k.get("key") for k in sk["kept"] if k.get("twin") == tid} - keys):
            # taken away here (the other computer stopped sharing it, or the owner chose so): its last text is
            # kept, and this row is where it can be put back from
            name, group = _name_group(key)
            rows.append({"key": key, "name": name, "group": group, "version": 0, "updated_at": None,
                         "state": "removed_there", "label": f"taken away here: {other} no longer shares it",
                         "on": False, "can_switch": False, "switch_label": f"{name}: follow it from {other}",
                         "note": None, "choices": [], "versions": _versions(sk, tid, key, False)})
        twins.append({"id": tid, "name": str(e.get("name") or "Twin"), "skills": rows})
    return twins, counts


WHAT_SOURCE = ("Sends each twin's skills (text files only) to your linked computers. Passwords and keys are left "
               "out. Changes made on a linked computer stay there.")
WHAT_FOLLOWER = "Takes each twin's skills (text files only) from {other}. Changes you make here stay on this computer."
WHAT_NONE = "A twin's skills can follow it to your other computers once this computer shares its twins or is linked."


def _plain_network(fst: dict) -> bool:
    try:
        from . import fleet_server
        return fleet_server.state_view(fst).get("network") == "lan"
    except Exception:
        return False


def view(svc: Any = None) -> dict:
    """`GET /fleet/skills`: the whole picture for this computer. No network call."""
    fst = fleet.load() if fleet.exists() else None
    role = fleet.role(fst) if fst else "none"
    out = {"role": role, "on": False, "state": "not_linked", "what": WHAT_NONE, "message": "", "other": None,
           "twins": [], "counts": {"skills": 0, "conflicts": 0, "changed_here": 0, "only_here": 0, "off": 0}}
    if role == "source":
        if os.path.lexists(_path()) and is_on(load(), "source"):
            source_scan(fst) and fleet.announce()
        sk = load() if os.path.lexists(_path()) else {"on": None, "src": _blank_src(), "fol": _blank_fol(), "kept": []}
        on = is_on(sk, "source")
        twins, counts = _source_rows(sk, fst, on)
        linked = sorted(fp.machine_name(f.get("name")) for f in (fst["share"].get("followers") or {}).values()
                        if isinstance(f, dict))
        shared = sum(1 for t in twins for s in t["skills"] if s["state"] == "in_sync")
        if not on:
            msg = ""
        elif not linked:
            msg = f"{plural(shared, 'skill')} ready for the first computer you link."
        else:
            msg = f"{plural(shared, 'skill')} shared with {fleet_and(linked)}."
        if on and _plain_network(fst):
            # said where the switch is: on a home network what travels can be read (not changed) by others on it
            msg += " On this home network they travel readable; on Tailscale they are encrypted."
        out.update(on=on, state="on" if on else "off", what=WHAT_SOURCE, message=msg, twins=twins, counts=counts)
    elif role == "follower":
        ln = fst["link"]
        other = fp.machine_name((ln.get("source") or {}).get("name"))
        out["other"] = {"id": (ln.get("source") or {}).get("id"), "name": other}
        with fleet.locked():
            sk = load()
            before = json.dumps(sk, sort_keys=True)
            _own_link(sk, fst)
            _settle(fst, sk, {})
            if json.dumps(sk, sort_keys=True) != before and (os.path.lexists(_path()) or sk["fol"]["twins"]):
                save(sk)
        on = is_on(sk, "follower")
        said = sk["fol"].get("heard")
        twins, counts = _follower_rows(sk, fst, other)
        held = sum(1 for t in twins for s in t["skills"] if s["state"] == "in_sync")
        if not on:
            state, msg = "off", (f"Skills from {other} are not taken on this computer. The ones already here stay "
                                 "as they are.")
        elif not isinstance(said, dict):
            state, msg = "waiting", f"Waiting to hear from {other}."
        elif not said.get("v"):
            state, msg = "older_there", (f"{other} has an older Grok Bot Twin that does not send skills yet. Update "
                                         "it there: Capabilities → Plugins → Update.")
        elif not said.get("on"):
            state, msg = "off_there", (f"Skills are not switched on on {other}. Switch on Skills there, on the "
                                       "Twins page." + (" The ones already here stay as they are." if held else ""))
        else:
            state = "on"
            msg = f"{plural(held, 'skill')} the same as on {other}."
            if counts["conflicts"]:
                msg += f" {plural(counts['conflicts'], 'skill')} {'needs' if counts['conflicts'] == 1 else 'need'} your choice."
        out.update(on=on, state=state, what=WHAT_FOLLOWER.format(other=other), message=msg, twins=twins, counts=counts)
    return out


def _find(sk: dict, fst: dict, role: str, tid: Any, key: Any) -> dict:
    if not isinstance(tid, str) or not REF_RE.match(tid) or not key_ok(key):
        raise SkillsError("unknown_skill")
    part = "src" if role == "source" else "fol"
    rec = (sk[part]["twins"].get(tid) or {"skills": {}})["skills"].get(key)
    if rec is None:
        raise SkillsError("unknown_skill")
    return rec


def _role() -> tuple:
    fst = fleet.load() if fleet.exists() else None
    role = fleet.role(fst) if fst else "none"
    if role == "none":
        raise SkillsError("not_linked")
    return fst, role


def switch(svc: Any, on: Any) -> dict:
    """`POST /fleet/skills/switch`: this computer's switch for skills."""
    fst, role = _role()
    with fleet.locked():
        sk = load()
        sk["on"] = on is True
        save(sk)
    if role == "source":
        source_scan(fst)
        fleet.announce()
    else:
        follow(svc, force=True)
    return view(svc)


def set_item(svc: Any, tid: Any, key: Any, on: Any) -> dict:
    """`POST /fleet/skills/item`: one skill's own switch. Sharing computer: share it or not. Linked computer:
    off means "only on this computer" (left alone from now on), on means it follows again."""
    fst, role = _role()
    on = on is True
    with fleet.locked():
        sk = load()
        if role == "follower":
            _own_link(sk, fst)
            if not isinstance(tid, str) or tid not in _linked_twins(fst):
                raise SkillsError("unknown_skill")
            rec = _find(sk, fst, role, tid, key)
            if on:
                rec.pop("off", None)
            else:
                rec["off"] = True
                rec.pop("take", None)
        else:
            rec = _find(sk, fst, role, tid, key)
            rec["share"] = on
            rec["off_at"] = None if on else now_iso()
        save(sk)
    if role == "source":
        source_scan(fst)
        fleet.announce()
    else:
        follow(svc, force=True)
    return view(svc)


def keep(svc: Any, tid: Any, key: Any, which: Any) -> dict:
    """`POST /fleet/skills/keep` on a linked computer: settle a skill that differs from the sharing computer's.
    "mine": this computer's text stays (and is not asked about again until the other one changes it again).
    "theirs": the other computer's version replaces it; this computer's is kept as an earlier version."""
    fst, role = _role()
    if role != "follower" or which not in ("mine", "theirs"):
        raise SkillsError("no_choice")
    before = None
    with fleet.locked():
        sk = load()
        _own_link(sk, fst)
        rec = _find(sk, fst, role, tid, key)
        entry = _linked_twins(fst).get(tid)
        _refresh_disk(_folder(entry["profile"], key) if entry else None, rec)
        what = judge(rec)
        their = rec.get("their") if isinstance(rec.get("their"), dict) else {}
        if what not in ("conflict", "conflict_new", "conflict_removed", "changed_here", "gone_here"):
            raise SkillsError("no_choice")
        before = [k["vid"] for k in sk["kept"]]
        if which == "mine":
            rec.pop("take", None)
            if what == "conflict_removed":
                rec.update(version=0, their=None)
                rec.pop("tdigest", None)
                rec.pop("digest", None)
            elif what in ("conflict", "conflict_new"):
                rec.update(version=their["version"], tdigest=their["digest"])
        else:
            if "removed" in their:
                rec["take"] = "removed"
            elif "version" in their:
                rec["take"] = their["version"]
            else:
                raise SkillsError("no_choice")
        save(sk)
    if which == "theirs":
        follow(svc, force=True)
    out = view(svc)
    sk = load()
    new = [k for k in sk["kept"] if k["vid"] not in before and k.get("twin") == tid and k.get("key") == key]
    out["undo"] = {"twin": tid, "skill": key, "vid": new[0]["vid"]} if new else None
    return out


def restore(svc: Any, tid: Any, key: Any, vid: Any) -> dict:
    """`POST /fleet/skills/restore`: put an earlier version of a skill back on this computer. What is there now
    is set aside first, so this can itself be undone."""
    fst, role = _role()
    if not isinstance(vid, str) or not VID_RE.match(vid) or not key_ok(key) or not isinstance(tid, str) \
            or not REF_RE.match(tid):
        raise SkillsError("unknown_version")
    twins = _shared_twins(fst) if role == "source" else _linked_twins(fst)
    if tid not in twins or not twins[tid].get("profile"):
        raise SkillsError("unknown_skill")
    profile = twins[tid]["profile"]
    if _folder(profile, key) is None:
        raise SkillsError("unknown_skill")
    aside = None
    with fleet.locked():
        sk = load()
        try:
            if vid.startswith("v"):
                b = _read_bundle(tid, key, int(vid[1:])) if role == "source" else None
                if b is None:
                    raise SkillsError("unknown_version")
                aside = _put(sk, tid, key, profile, lambda tmp: _write_tree(tmp, b["files"]), "put_back", own=True)
            else:
                k = next((k for k in sk["kept"] if k["vid"] == vid and k.get("twin") == tid and k.get("key") == key), None)
                src = _kept_root() / vid
                if k is None or not fio.is_real_dir(src):
                    raise SkillsError("unknown_version")
                aside = _put(sk, tid, key, profile, lambda tmp: shutil.copytree(src, tmp, symlinks=True),
                             "put_back", own=True, spare=vid)
        except SkillsError:
            raise
        except Exception:
            log.debug("grok-bot-twin: could not put a skill back", exc_info=True)
            save(sk)
            raise SkillsError("write_failed") from None
        if role == "follower":
            rec = (sk["fol"]["twins"].get(tid) or {"skills": {}})["skills"].get(key)
            if rec is not None:
                rec.pop("take", None)
                rec.pop("error", None)
        save(sk)
    if role == "source":
        source_scan(fst)
        fleet.announce()
    else:
        follow(svc)
    out = view(svc)
    out["undo"] = {"twin": tid, "skill": key, "vid": aside} if aside else None
    return out


# ---- the terminal: hermes twin fleet skills ... ----------------------------------------------------------------------
USAGE = ("hermes twin fleet skills [on|off] | set SKILL on|off | keep SKILL mine|theirs | versions SKILL | "
         "restore SKILL VERSION   (add --twin NAME when two twins have a skill of that name)")


def describe(v: dict) -> list:
    if v["role"] == "none":
        return ["Skills: " + v["what"]]
    lines = [f"Skills: {'on' if v['on'] else 'off'}. {v['what']}"]
    if v["message"]:
        lines.append(v["message"])
    if not v["on"]:
        lines.append("Switch on with: hermes twin fleet skills on")
    for t in v["twins"]:
        if t["skills"]:
            lines.append(f"{t['name']}:")
        for s in t["skills"]:
            line = f"  {s['key']}: {s['label']}"
            if s["choices"]:
                line += " (choose: hermes twin fleet skills keep " + s["key"] + " mine|theirs)" \
                    if len(s["choices"]) > 1 else f" (hermes twin fleet skills keep {s['key']} theirs puts it back)"
            lines.append(line)
            if s["note"]:
                lines.append(f"    {s['note']}")
    return lines


def fleet_lines() -> list:
    """The line `hermes twin fleet` adds about skills ([] on a computer that neither shares nor follows)."""
    try:
        v = view()
    except Exception:
        log.debug("grok-bot-twin: could not say what syncs", exc_info=True)
        return []
    if v["role"] == "none":
        return []
    if v["role"] == "source":
        if not v["on"]:
            return ["Skills: not shared. Only names and descriptions leave this computer "
                    "(share skills too: hermes twin fleet skills on)."]
        return [f"Skills: on. {v['message']} (hermes twin fleet skills)"]
    if not v["on"]:
        return [f"Skills: not taken from {v['other']['name']} (hermes twin fleet skills on)."]
    return [f"Skills: {v['message']} (hermes twin fleet skills)"]


def _pick(v: dict, key: Any, twin: Any) -> tuple:
    hits = [(t, s) for t in v["twins"] for s in t["skills"] if s["key"] == key
            and (not twin or twin in (t["id"], t["name"]) or str(twin).lower() == t["name"].lower())]
    if not hits:
        raise SkillsError("unknown_skill")
    if len(hits) > 1:
        raise EngineError("usage", "Two twins have a skill of that name.", "Add --twin NAME.")
    return hits[0][0]["id"], hits[0][1]


def cli(svc: Any, args: Any, out: Any, as_json: Any) -> int:
    word = getattr(args, "value", None)
    more = list(getattr(args, "more", None) or [])
    twin = getattr(args, "twin", None)
    if word in (None, "show"):
        v = view(svc)
    elif word in ("on", "off") and not more:
        v = switch(svc, word == "on")
    elif word == "set" and len(more) == 2 and more[1] in ("on", "off"):
        tid, _s = _pick(view(svc), more[0], twin)
        v = set_item(svc, tid, more[0], more[1] == "on")
    elif word == "keep" and len(more) == 2 and more[1] in ("mine", "theirs"):
        tid, _s = _pick(view(svc), more[0], twin)
        v = keep(svc, tid, more[0], more[1])
    elif word == "versions" and len(more) == 1:
        tid, s = _pick(view(svc), more[0], twin)
        if getattr(args, "json", False):
            return as_json(s["versions"])
        if not s["versions"]:
            out("No earlier version of that skill is kept on this computer.")
        for ver in s["versions"]:
            out(f"{ver['vid']}  {ver['label']}")
        return 0
    elif word == "restore" and len(more) == 2:
        tid, _s = _pick(view(svc), more[0], twin)
        v = restore(svc, tid, more[0], more[1])
    else:
        raise EngineError("usage", "Usage: " + USAGE)
    if getattr(args, "json", False):
        return as_json(v)
    for line in describe(v):
        out(line)
    if v.get("undo"):
        u = v["undo"]
        out(f"Undo: hermes twin fleet skills restore {u['skill']} {u['vid']}")
    return 0


fleet_items.register("skills", _wire_rev, _have, _heard, ROUTE, handle)
