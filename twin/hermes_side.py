"""What the plugin itself reads and writes inside a twin's own Hermes profile folder (never anywhere else).

Reads (small text files, opened without following any link, capped):
- `SOUL.md`: the name and description botport keeps there (between its own markers), and whether the Hermes bot has
  instructions at all.
- `config.yaml`: only whether a model is chosen (the same rule botport's own check uses).
- `skills/<category>/<name>/SKILL.md`: the text of one skill the owner asked to copy into Grok by hand.

Writes (atomic, 0600, only after the folder chain from the Hermes root down was checked):
- `SOUL.md`: the instructions the owner pasted on the Twins page, in the plugin's own marked block. botport's
  sections and any other text in the file are never touched. botport tracks its own `persona` section as "the
  instructions that follow Grok"; the owner's pasted text is deliberately NOT put there, so nothing claims it
  follows Grok and nothing asks to send it back.
- `config.yaml` of a NEW twin that has no model: the owner's default model settings (model ids and provider names
  only; anything that looks like a credential, an address or a header is dropped; `.env` is never read).

The earlier versions of pasted instructions live in the plugin's own data folder (`kept.json`), not in the profile.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import osflags as _osflags
from . import secrets as _secrets
from . import store
from . import yamlio as _yamlio
from .importer import _open_below_root
from .model import now_iso, valid_id, valid_item_id

BEGIN = "<!-- grok-bot-twin:begin instructions -->"
END = "<!-- grok-bot-twin:end instructions -->"
INSTRUCTIONS_ITEM = "persona.instructions"
INSTRUCTIONS_CAP = 200 * 1024
SOUL_CAP = 1024 * 1024
CONFIG_CAP = 1024 * 1024
SKILL_CAP = 200 * 1024
KEPT_MAX = 20
SHOWN_CAP = 20 * 1024
HIDDEN_LINE = "[not shown: looks like a password or key]"
_OWN_RE = re.compile(re.escape(BEGIN) + r"\n?(.*?)\n?" + re.escape(END) + r"\n?", re.S)
# the plugin's other marked blocks in the same file (a mirrored twin's name and description, the fleet note): they
# are facts the plugin keeps, not instructions the owner wrote
_FLEET_RE = re.compile(r"<!-- grok-bot-twin:begin (twin|fleet) -->\n?.*?\n?<!-- grok-bot-twin:end \1 -->\n?", re.S)
_BOTPORT_RE = re.compile(r"<!-- botport:begin ([a-z-]{1,40}) -->\n?(.*?)\n?<!-- botport:end \1 -->\n?", re.S)
_MARKER_RE = re.compile(r"<!--\s*(?:botport|grok-bot-twin)\s*:", re.I)
# (\ud800-\udfff: half a surrogate pair, which JSON can carry but no file or answer can be written with)
_CTRL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff\ud800-\udfff]")


def _read_below_root(path: Path, cap: int) -> Optional[str]:
    """A regular text file below the Hermes root, read through one descriptor opened without following a link.
    None when missing, not a regular file, not the user's, over the cap, or not UTF-8."""
    fd = _open_below_root(Path(path))
    if fd is None:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > cap or (_osflags.posix() and st.st_uid != os.getuid()):
            return None
        chunks, total = [], 0
        while total <= cap:
            chunk = os.read(fd, min(1 << 20, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > cap:
            return None
        # a byte-order mark and \r\n line ends (Windows editors and tools write both) never reach the parsing
        return b"".join(chunks).decode("utf-8-sig").replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)


def _file_style(path: Path) -> tuple[bool, bool]:
    """(starts with a byte-order mark, uses \\r\\n line ends) for an existing file; (False, False) for a new or
    unreadable one. Only the first 64 KB are looked at."""
    try:
        fd = _osflags.open_nofollow(path, os.O_RDONLY)
    except OSError:
        return False, False
    try:
        head = os.read(fd, 64 * 1024)
    except OSError:
        return False, False
    finally:
        os.close(fd)
    return head.startswith(b"\xef\xbb\xbf"), b"\r\n" in head


def _write_private(path: Path, text: str) -> None:
    """Atomic 0600 write of one file in a twin's profile folder. The folder chain is checked first (every folder
    from the Hermes root down a real folder of the user's, not writable by others); an existing target must be a
    regular file (never a link)."""
    store.ensure_dir(path.parent)
    with contextlib.suppress(FileNotFoundError):
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise store.UnsafePath(f"refusing to write {path.name}: it is not a regular file")
    data = text.replace("\r\n", "\n").encode("utf-8")
    bom, crlf = _file_style(path)
    if crlf:  # the file keeps the line ends (and the byte-order mark) it already has: only our text changes
        data = data.replace(b"\n", b"\r\n")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            _osflags.make_private(fh.fileno())
            fh.write((b"\xef\xbb\xbf" if bom else b"") + data)
            fh.flush()
            os.fsync(fh.fileno())
        _osflags.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---- SOUL.md ---------------------------------------------------------------------------------------------------
def shown(text: Any, cap: int = 2000) -> str:
    """Text from a profile file as the page shows it: password-like text hidden, control characters dropped
    (newlines kept), capped."""
    if not isinstance(text, str):
        return ""
    # (invisible characters are dropped BEFORE the check: one inside a key would otherwise hide it from the check
    # and be gone from what is shown)
    return _secrets.redact(_CTRL.sub("", text[: cap * 4]), HIDDEN_LINE)[:cap].strip()


def soul_facts(profile_home: Optional[Path]) -> dict:
    """{"name", "description", "own", "has_instructions"} from a twin's SOUL.md.
    name / description: what botport keeps between its markers (the Hermes side of what follows Grok).
    own: the owner's pasted instructions (the plugin's block), "" when there are none.
    has_instructions: the Hermes bot has instructions of any origin (the plugin's block, botport's own `persona`
    section, or text someone wrote into the file by hand)."""
    out = {"name": "", "description": "", "own": "", "has_instructions": False}
    text = _read_below_root(Path(profile_home) / "SOUL.md", SOUL_CAP) if profile_home is not None else None
    if not text:
        return out
    m = _OWN_RE.search(text)
    out["own"] = m.group(1) if m else ""
    rest = _FLEET_RE.sub("", _OWN_RE.sub("", text))
    sections = {name: body for name, body in _BOTPORT_RE.findall(rest)}
    rest = _BOTPORT_RE.sub("", rest)
    out["name"] = shown(sections.get("persona-name"), 100).replace("\n", " ")
    out["description"] = shown(sections.get("persona-description"), 2000)
    out["has_instructions"] = bool(out["own"].strip() or (sections.get("persona") or "").strip() or rest.strip())
    return out


def own_block_lines(profile_home: Optional[Path]) -> Optional[tuple]:
    """(first, last) line numbers, 1-based and as counted in SOUL.md, of the owner's pasted instructions; None
    when there is no such block. Used to say which line of the instructions another check pointed at."""
    text = _read_below_root(Path(profile_home) / "SOUL.md", SOUL_CAP) if profile_home is not None else None
    m = _OWN_RE.search(text) if text else None
    if not m or not m.group(1):
        return None
    first = text.count("\n", 0, m.start(1)) + 1
    return first, first + m.group(1).count("\n")


def has_markers(text: str) -> bool:
    """True when pasted text holds a line that looks like one of the markers this file is organised by."""
    return bool(_MARKER_RE.search(text))


def write_instructions(profile_home: Path, text: str) -> str:
    """Put `text` in the plugin's block of the twin's SOUL.md ("" takes the block out). Returns the text the block
    held before ("" when there was none). Everything else in the file stays byte for byte."""
    path = Path(profile_home) / "SOUL.md"
    store.ensure_dir(path.parent)
    current = _read_below_root(path, SOUL_CAP)
    if current is None:
        if os.path.lexists(path):
            raise store.UnsafePath("refusing to change SOUL.md: it could not be read safely")
        current = ""
    m = _OWN_RE.search(current)
    before = m.group(1) if m else ""
    rest = _OWN_RE.sub("", current)
    if text:
        if rest and not rest.endswith("\n"):
            rest += "\n"
        rest += f"{BEGIN}\n{text.rstrip(chr(10))}\n{END}\n"
    if rest != current:
        _write_private(path, rest)
    return before


# ---- config.yaml: is a model chosen? -----------------------------------------------------------------------------
def _yaml_module() -> Any:
    """PyYAML, Hermes' own YAML module or ruamel.yaml (twin/yamlio.py: a plain Hermes 0.23 has no PyYAML);
    ImportError when none is there, and the plugin then says "cannot tell" instead of guessing."""
    mod = _yamlio.module()
    if mod is None:
        raise ImportError("no YAML reader in this Python")
    return mod


def _yaml_load(text: str) -> Any:
    return _yaml_module().safe_load(text)


def _model_of(doc: Any) -> str:
    """botport's rule (its `twin parity` model check): `model` is a non-empty string, or a mapping whose
    `default`, `model` or `name` is one."""
    m = doc.get("model") if isinstance(doc, dict) else None
    if isinstance(m, str):
        return m.strip()
    if isinstance(m, dict):
        for key in ("default", "model", "name"):
            v = m.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def profile_model(profile_home: Optional[Path]) -> Optional[str]:
    """The model id chosen for this profile ("" when none). None when that cannot be told (no YAML reader, or a
    config file that cannot be read safely): the page then does not nag."""
    if profile_home is None:
        return None
    path = Path(profile_home) / "config.yaml"
    text = _read_below_root(path, CONFIG_CAP)
    if text is None:
        return "" if not os.path.lexists(path) else None
    try:
        return _model_of(_yaml_load(text))
    except Exception:
        return None


def _plain(value: Any) -> Optional[str]:
    """A model id or provider name that may be copied: one plain word, nothing that looks like a credential or
    an address."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if (not s or len(s) > 200 or any(c.isspace() or ord(c) < 32 or c == "@" for c in s) or "://" in s
            or _secrets.has_secret(s)):
        return None
    return s


def _allowed(value: Any, keys: tuple, as_list: bool = False) -> Any:
    if isinstance(value, str):
        return _plain(value)
    if isinstance(value, dict):
        kept = {k: _plain(value.get(k)) for k in keys}
        kept = {k: v for k, v in kept.items() if v}
        return kept or None
    if isinstance(value, list) and as_list:
        kept_list = [x for x in (_allowed(v, keys) for v in value[:20]) if x]
        return kept_list or None
    return None


def copy_default_model(profile_home: Path, root: Path) -> Optional[str]:
    """Give a new twin that has no model the owner's default model (the one the default Hermes profile uses).
    Copied: `model`, `fallback_providers`, `fallback_model`, each kept to model ids and provider names. Returns the
    model id now set, or None when nothing was written (the twin has a model, the owner has no default, the files
    cannot be read safely, or the result would not read back exactly as intended)."""
    profile_home, root = Path(profile_home), Path(root)
    if profile_home == root or profile_model(profile_home) != "":
        return None
    src_text = _read_below_root(root / "config.yaml", CONFIG_CAP)
    if src_text is None:
        return None
    try:
        yaml = _yaml_module()
        src = yaml.safe_load(src_text)
        if not _model_of(src):
            return None
        add: dict = {}
        for key, keys, as_list in (("model", ("default", "model", "name", "provider"), False),
                                   ("fallback_providers", ("provider", "model"), True),
                                   ("fallback_model", ("provider", "model"), False)):
            got = _allowed(src.get(key), keys, as_list)
            if got:
                add[key] = got
        if not _model_of(add):
            return None
        path = profile_home / "config.yaml"
        old_text = _read_below_root(path, CONFIG_CAP)
        if old_text is None and os.path.lexists(path):
            return None
        old = yaml.safe_load(old_text) if old_text else {}
        if old is None:
            old = {}
        if not isinstance(old, dict) or any(k in old for k in add):
            return None
        new_text = (old_text or "")
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += yaml.safe_dump(add, default_flow_style=False, sort_keys=True, allow_unicode=True)
        if yaml.safe_load(new_text) != {**old, **add}:  # the file must read back as: everything it had, plus these
            return None
        _write_private(path, new_text)
        return _model_of(add)
    except Exception:  # no YAML reader, a file that cannot be read or is not YAML: nothing is written
        return None


# ---- one skill's text ---------------------------------------------------------------------------------------------
def skill_text(profile_home: Optional[Path], name: Any) -> Optional[tuple]:
    """(text, truncated) of the Hermes version of one skill: `skills/<category>/<name>/SKILL.md` (or
    `skills/<name>/SKILL.md`) in the twin's own profile. None when there is no such skill. Only a plain name is
    looked for, folder names are listed (never followed through a link), and one file is read."""
    if profile_home is None or not valid_id(name) or name.startswith("-"):
        return None
    base = Path(profile_home) / "skills"
    candidates = [base / name / "SKILL.md"]
    try:
        with os.scandir(base) as it:
            cats = sorted(e.name for e in it if e.is_dir(follow_symlinks=False) and valid_id(e.name))[:200]
    except OSError:
        cats = []
    candidates += [base / c / name / "SKILL.md" for c in cats]
    for path in candidates:
        text = _read_below_root(path, 4 * SKILL_CAP)
        if text is not None:
            data = text.encode("utf-8")
            if len(data) <= SKILL_CAP:
                return text, False
            return data[:SKILL_CAP].decode("utf-8", errors="ignore"), True
    return None


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ---- earlier versions of pasted instructions (the plugin's own data folder) ---------------------------------------
def _kept_path(central: Path) -> Path:
    return Path(central) / "kept.json"


def kept_list(central: Path, pid: str) -> list:
    data = store.read_json(_kept_path(central), {}) or {}
    pts = data.get(pid) if isinstance(data, dict) else None
    return [p for p in pts if isinstance(p, dict) and valid_item_id(p.get("id")) and isinstance(p.get("text"), str)
            ][-KEPT_MAX:] if isinstance(pts, list) else []


def keep(central: Path, pid: str, text: str, reason: str, why: str, auto: bool = False) -> str:
    """Keep one earlier version of a twin's instructions (also the empty one from before the first paste).
    Returns its id, `persona.instructions@<digest>`. `auto`: this text was written by an automatic change from Grok
    (twin/instr_sync.py); past KEPT_MAX such versions go first, so a run of Grok's changes never pushes out one of
    the owner's own."""
    rid = f"{INSTRUCTIONS_ITEM}@{text_digest(text)}"
    with store.locked():
        data = store.read_json(_kept_path(central), {}) or {}
        data = data if isinstance(data, dict) else {}
        pts = [p for p in kept_list(central, pid) if p["id"] != rid]
        pts.append({"id": rid, "item": INSTRUCTIONS_ITEM, "at": now_iso(), "reason": reason, "why": why,
                    "text": text[:INSTRUCTIONS_CAP], **({"auto": True} if auto else {})})
        while len(pts) > KEPT_MAX:
            del pts[next((i for i, p in enumerate(pts[:-1]) if p.get("auto") is True), 0)]
        data[pid] = pts
        store.atomic_write_json(_kept_path(central), data)
    return rid


def kept_drop(central: Path, pid: str) -> None:
    with store.locked():
        data = store.read_json(_kept_path(central), {}) or {}
        if isinstance(data, dict) and data.pop(pid, None) is not None:
            store.atomic_write_json(_kept_path(central), data)


# ---- what botport's own kept copies hold ---------------------------------------------------------------------------
# botport keeps earlier states of a twin's Hermes bot as zip files in the bot's own folder
# (backups/botport/restore-<time>-NN.zip: the prompt file and botport's record of each part as they were). Its list
# of earlier versions names a part and the first 12 characters of a digest, and no text, so the page could offer
# "Put back" but never "Show". The text is found here, and only ever by proof: a text counts as "that version"
# when its own SHA-256 is the digest botport names (botport hashes a part with and without its last line end), or
# when one of botport's records ties the named digest to such a text. Nothing is guessed from times or order.
BOTPORT_KEPT_DIR = ("backups", "botport")
_KEPT_ZIP_RE = re.compile(r"restore-\d{8}T\d{6}Z-\d{2}\.zip")
KEPT_ZIPS_MAX = 60
KEPT_ZIP_CAP = 8 * 1024 * 1024
MIRROR_CAP = 1024 * 1024
BOTPORT_PARTS = {"persona.name": "persona-name", "persona.description": "persona-description"}


def _zip_member(zf: Any, name: str, cap: int) -> Optional[str]:
    try:
        info = zf.getinfo(name)
        if info.file_size > cap or info.flag_bits & 0x1:  # (too large, or encrypted: not botport's)
            return None
        with zf.open(info) as fh:
            raw = fh.read(cap + 1)
        return raw.decode("utf-8-sig").replace("\r\n", "\n") if len(raw) <= cap else None
    except Exception:  # a member that is missing, damaged or not text: nothing is known from it
        return None


def _kept_states(profile_home: Path) -> list:
    """[(prompt file text or None, botport's record or None)] for the live files and for every kept zip, newest
    zips first, each read through a descriptor opened without following a link."""
    import json
    import zipfile
    out = [(_read_below_root(Path(profile_home) / "SOUL.md", SOUL_CAP),
            _read_below_root(Path(profile_home) / "botport.mirror.json", MIRROR_CAP))]
    folder = Path(profile_home).joinpath(*BOTPORT_KEPT_DIR)
    # A bot without the folder of kept copies (Stop twinning takes it away, and botport does not make it again
    # for a bot it picks up) still has its live files: they are parsed below like any kept state. Returning here
    # handed botport's record on as raw text, the caller failed on it, and the twin's own current name and
    # description were listed as earlier versions.
    names: list = []
    try:
        if stat.S_ISDIR(os.lstat(folder).st_mode):
            names = sorted((n for n in os.listdir(folder) if _KEPT_ZIP_RE.fullmatch(n)),
                           reverse=True)[:KEPT_ZIPS_MAX]
    except OSError:
        names = []
    for name in names:
        fd = _open_below_root(folder / name)
        if fd is None:
            continue
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > KEPT_ZIP_CAP \
                    or (_osflags.posix() and st.st_uid != os.getuid()):
                os.close(fd)
                continue
            fh = os.fdopen(fd, "rb")
        except OSError:
            with contextlib.suppress(OSError):
                os.close(fd)
            continue
        try:
            with fh, zipfile.ZipFile(fh) as zf:
                out.append((_zip_member(zf, "SOUL.md", SOUL_CAP), _zip_member(zf, "botport.mirror.json", MIRROR_CAP)))
        except Exception:  # not a zip file, cut short, or of a kind this Python cannot read
            continue
    parsed = []
    for soul, mirror in out:
        try:
            rec = json.loads(mirror) if mirror else None
        except (ValueError, RecursionError):
            rec = None
        parsed.append((soul, rec if isinstance(rec, dict) else None))
    return parsed


def botport_kept_texts(profile_home: Optional[Path]) -> dict:
    """{"now": {part: text}, "versions": {(part, first 12 of a digest): text}} for the parts botport keeps in the
    prompt file (the name and the description). `now`: what the Hermes bot holds at this moment. `versions`: every
    earlier (or current) text that one of botport's digests is proven to stand for; a digest that two different
    texts could claim is left out. Empty when nothing can be read."""
    out: dict = {"now": {}, "versions": {}}
    if profile_home is None:
        return out
    states = _kept_states(Path(profile_home))
    if not any(soul is not None or rec is not None for soul, rec in states):
        return out  # nothing of this bot could be read: nothing is known, not even "it was empty"
    clash: set = set()

    def claim(part: str, digest: Any, text: str) -> None:
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{12,64}", digest):
            return
        key = (part, digest[:12])
        if out["versions"].setdefault(key, text) != text:
            clash.add(key)

    for part, block in BOTPORT_PARTS.items():
        texts = []
        for i, (soul, _rec) in enumerate(states):
            body = dict(_BOTPORT_RE.findall(soul)).get(block) if soul else None
            if i == 0 and body is not None:
                out["now"][part] = body
            # (a state without the prompt file, or without this part in it, is the empty text: the bot before
            # botport first wrote the part)
            texts.append(body if body is not None else "")
        texts.append("")  # (the digest of nothing is nothing's, whatever was kept)
        by_digest = {}
        for t in texts:
            for form in (t, t + "\n"):
                by_digest.setdefault(hashlib.sha256(form.encode("utf-8")).hexdigest(), t)
        for digest, t in by_digest.items():
            claim(part, digest, t)
        for _soul, rec in states:
            item = ((rec or {}).get("items") or {}).get(part) if isinstance((rec or {}).get("items"), dict) else None
            if not isinstance(item, dict):
                continue
            # botport's record of a part: the digest of the Hermes text, of the Grok text as Grok stores it, and
            # of that Grok text in plain form. The plain form is a text we hold; the stored form is tied to it.
            plain = by_digest.get(item.get("neutral_remote")) if isinstance(item.get("neutral_remote"), str) else None
            if plain is not None:
                claim(part, item.get("base_remote"), plain)
    for key in clash:
        out["versions"].pop(key, None)
    return out


def shown_version(text: Any) -> Optional[str]:
    """An earlier version as the page may show it: capped at 20 kB, every line that looks like a password or key
    replaced whole. None when it is not text."""
    if not isinstance(text, str):
        return None
    text = _CTRL.sub("", text)  # first, as in shown()
    flagged = {f["line"] for f in _secrets.detect_secrets(text)}
    lines = text.split("\n")
    return "\n".join(HIDDEN_LINE if i + 1 in flagged else ln for i, ln in enumerate(lines))[:SHOWN_CAP]
