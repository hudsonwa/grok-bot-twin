"""The pinned botport release: which files are the genuine ones, by SHA-256, and where this computer's download is.

Why: verifying botport used to need GitHub's command-line tool, signed in. With this table the plugin verifies the
file the owner downloaded by itself. The chain: the catalog pins this plugin by commit -> this plugin carries the
table -> the table names the exact bytes of the release files. Nothing is fetched at run time; the table
(`botport_pins.json`, next to this file) is written once per plugin release by `tools/pin_botport.py` from the
release files, each checked there with `gh attestation verify`.

A release that has no entry here (an owner who set another `botport_release_tag`, or a table that was not filled)
is simply not pinned: the plugin then says botport is not verified yet, and the GitHub check stays available.
"""
from __future__ import annotations

import json
import platform
import re
from pathlib import Path
from typing import Any, Optional

# The four files a botport release holds a build for (release.yml in hudsonwa/botport).
TARGETS = ("aarch64-apple-darwin", "x86_64-apple-darwin", "x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl")
DOWNLOAD_BASE = "https://github.com/hudsonwa/botport/releases/download"
PINS_FILE = Path(__file__).resolve().parent / "botport_pins.json"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
_PINS_CAP = 256 * 1024

_table: Optional[dict] = None


def asset_name(target: str) -> str:
    """The release archive for one kind of computer, e.g. botport-aarch64-apple-darwin.tar.gz."""
    if target not in TARGETS:
        raise ValueError("not a botport build target")
    return f"botport-{target}.tar.gz"


def platform_target(system: Optional[str] = None, machine: Optional[str] = None) -> Optional[str]:
    """The build for this computer, or None when botport has none for it (the page then says so)."""
    system = platform.system() if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine)
    if arch is None:
        return None
    if system == "Darwin":
        return f"{arch}-apple-darwin"
    if system == "Linux":
        return f"{arch}-unknown-linux-musl"
    return None


def download_for(tag: Any, target: Optional[str] = None) -> Optional[dict]:
    """{"url", "file_name"}: the direct release file for this computer (the owner's browser downloads it; the
    plugin downloads nothing). The address is fixed text plus a release tag that looks like v0.2.1 and one of the
    four known file names: no other input reaches it. None when there is no build for this computer."""
    target = platform_target() if target is None else target
    if target not in TARGETS or not isinstance(tag, str) or not _TAG_RE.match(tag):
        return None
    name = asset_name(target)
    return {"url": f"{DOWNLOAD_BASE}/{tag}/{name}", "file_name": name}


def _clean(raw: Any) -> dict:
    """The table rebuilt from known shapes only: {tag: {"archives": {file name: sha}, "programs": {target: sha}}}."""
    out: dict = {}
    releases = raw.get("releases") if isinstance(raw, dict) else None
    for tag, entry in (releases.items() if isinstance(releases, dict) else []):
        if not (isinstance(tag, str) and _TAG_RE.match(tag) and isinstance(entry, dict)):
            continue
        archives = entry.get("archives") if isinstance(entry.get("archives"), dict) else {}
        programs = entry.get("programs") if isinstance(entry.get("programs"), dict) else {}
        out[tag] = {
            "archives": {asset_name(t): archives[asset_name(t)] for t in TARGETS
                         if isinstance(archives.get(asset_name(t)), str) and _SHA_RE.match(archives[asset_name(t)])},
            "programs": {t: programs[t] for t in TARGETS
                         if isinstance(programs.get(t), str) and _SHA_RE.match(programs[t])},
        }
    return out


def load() -> dict:
    global _table
    if _table is None:
        try:
            with open(PINS_FILE, "rb") as fh:
                _table = _clean(json.loads(fh.read(_PINS_CAP + 1).decode("utf-8")))
        except (OSError, ValueError):
            _table = {}
    return _table


def set_table(table: Optional[dict]) -> None:
    """Tests: use this table ({"releases": {...}} as in the file); None reads the file again."""
    global _table
    _table = None if table is None else _clean(table)


def known(tag: Any) -> bool:
    """True when this release is pinned for at least one kind of computer."""
    entry = load().get(tag) if isinstance(tag, str) else None
    return bool(entry and entry["programs"])


def program_target(sha256: Any, tag: Any) -> Optional[str]:
    """The kind of computer whose pinned `botport` program has this SHA-256 in release `tag`, else None."""
    entry = load().get(tag) if isinstance(tag, str) else None
    if not entry or not isinstance(sha256, str):
        return None
    return next((t for t, d in entry["programs"].items() if d == sha256), None)


def archive_target(sha256: Any, tag: Any) -> Optional[str]:
    """The kind of computer whose pinned release archive has this SHA-256 in release `tag`, else None."""
    entry = load().get(tag) if isinstance(tag, str) else None
    if not entry or not isinstance(sha256, str):
        return None
    return next((t for t in TARGETS if entry["archives"].get(asset_name(t)) == sha256), None)
