"""Setup exports that arrive through skill sync ("the Mirror"), for the sheet that finishes several twins.

When skill sync is set up, the request the owner pastes into each Grok bot's chat also asks the bot to save its export
as a file in the Grok box, at EXPORT_DIR/grok-bot-export-<its bot id>.json. The sync bot's script names those files on
"copy up: exports/<file>" lines, so the sync bot copies them up unchanged into the shared folder's exports/ folder,
where the batch sheet's watch (BatchMixin.downloads_scan) picks them up next to Downloads.

Purely files: no network call, no Grok app. Files in the shared folder are only read, never moved or deleted; what is
kept about one is the same sha256 digest of its name, size and time that Downloads uses.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Optional

from . import osflags as _osflags
from . import setup_export as sx
from . import skill_sync

EXPORT_DIR = skill_sync.BOX_ROOT + "/exports"
EXPORTS = "exports"                          # the same folder in the shared folder
NAME_RE = re.compile(r"grok-bot-export-[A-Za-z0-9-]{1,64}\.json")
SIZE_MAX = min(2 * 1024 * 1024, sx.TEXT_CAP)  # the script names files of at most 2 MB
FILES_MAX = 40
SCAN_ENTRIES_MAX = 5000
GRACE_S = 900  # the box's clock is not this computer's: a file saved a little "before" the sheet watched still counts

SENTENCE = (f"Also save that same JSON as a file in your box at {EXPORT_DIR}/grok-bot-export-<your bot id>.json "
            "(if you do not know your bot id, use your name in lower case with hyphens).")


def exports_dir(central: Path) -> Optional[Path]:
    """<shared folder>/exports while skill sync is set up and it is a real folder (never a link)."""
    root = skill_sync.shared_root(central)
    if root is None:
        return None
    d = root / EXPORTS
    try:
        return d if stat.S_ISDIR(os.lstat(d).st_mode) else None
    except OSError:
        return None


def request(central: Path) -> str:
    """The text to paste into each bot's chat: setup_export.REQUEST, plus one sentence when skill sync is set up."""
    if skill_sync.shared_root(central) is None:
        return sx.REQUEST
    return sx.REQUEST.rstrip("\n") + "\n\n" + SENTENCE + "\n"


def pickup(central: Path) -> dict:
    if skill_sync.shared_root(central) is None:
        return {"via": "downloads", "label": "Downloads"}
    return {"via": "mirror", "label": "the shared folder"}


def candidates(central: Path, since: float) -> list:
    """[(name, path, size, mtime_ns)] of the regular files in <shared folder>/exports named like an export, at most
    SIZE_MAX, modified at or after `since` (less GRACE_S), newest first, at most FILES_MAX. Names, types, sizes and
    times only; read them with BatchMixin._read_download."""
    folder = exports_dir(central)
    if folder is None:
        return []
    found = []
    try:
        with os.scandir(folder) as it:
            for n, entry in enumerate(it):
                if n >= SCAN_ENTRIES_MAX:
                    break
                name = entry.name
                if not NAME_RE.fullmatch(name):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or _osflags.is_link(st) or st.st_size > SIZE_MAX \
                        or st.st_size == 0 or st.st_mtime < since - GRACE_S:
                    continue
                found.append((name, Path(entry.path), st.st_size, st.st_mtime_ns))
    except OSError:
        return []
    found.sort(key=lambda f: f[3], reverse=True)
    return found[:FILES_MAX]
