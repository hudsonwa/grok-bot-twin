#!/usr/bin/env python3
"""Release-time tool (not part of the running plugin): fill twin/botport_pins.json for one botport release.

    1. Put the release archives in a folder yourself, for example:
         gh release download v0.2.1 --repo hudsonwa/botport --pattern 'botport-*.tar.gz' --pattern SHA256SUMS --dir DIR
    2. python3 tools/pin_botport.py DIR --tag v0.2.1 --write

For every archive found (botport-<target>.tar.gz, four targets) it takes the SHA-256 of the archive and of the
`botport` program inside it, and runs `gh attestation verify` on both (the same repo, signer workflow and release
tag the plugin's own GitHub check uses). Nothing is written unless every file passed. `--no-verify` skips the
attestation check and then needs `--write-unverified` to write: use that only for a throwaway test table.
When DIR holds a SHA256SUMS file, the archive digests are compared with it as well.

This script downloads nothing and the plugin never runs it. It only reads DIR and writes the one JSON file.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / "twin" / "botport_pins.json"
TARGETS = ("aarch64-apple-darwin", "x86_64-apple-darwin", "x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl")
REPO = "hudsonwa/botport"
WORKFLOW = "hudsonwa/botport/.github/workflows/release.yml"
CAP = 200 * 1024 * 1024


def program_from_archive(data: bytes) -> bytes:
    """The one regular member named `botport` (never a link, never a path with `..`)."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        for m in tar:
            parts = m.name.replace("\\", "/").split("/")
            if m.isreg() and parts[-1] == "botport" and not m.name.startswith("/") and ".." not in parts \
                    and 0 < m.size <= CAP:
                fh = tar.extractfile(m)
                if fh is not None:
                    return fh.read(CAP + 1)
    raise SystemExit("no member named botport in the archive")


def attested(gh: str, path: str, tag: str) -> bool:
    cp = subprocess.run([gh, "attestation", "verify", path, "--repo", REPO, "--signer-workflow", WORKFLOW,
                         "--source-ref", f"refs/tags/{tag}"], stdin=subprocess.DEVNULL, capture_output=True,
                        timeout=120)
    return cp.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder", help="folder that holds the downloaded release archives")
    ap.add_argument("--tag", required=True, help="the release tag, like v0.2.1")
    ap.add_argument("--write", action="store_true", help="write twin/botport_pins.json (every file must have passed)")
    ap.add_argument("--no-verify", action="store_true", help="skip gh attestation verify")
    ap.add_argument("--write-unverified", action="store_true", help="with --no-verify: write anyway (test tables only)")
    a = ap.parse_args()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", a.tag):
        raise SystemExit("the tag must look like v0.2.1")
    folder = Path(a.folder)
    gh = None
    if not a.no_verify:
        gh = shutil.which("gh")
        if not gh:
            raise SystemExit("gh is not installed; install it and run gh auth login, or pass --no-verify")
    sums: dict = {}
    sums_file = folder / "SHA256SUMS"
    if sums_file.is_file():
        for line in sums_file.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                sums[parts[1].lstrip("*./")] = parts[0]
    archives: dict = {}
    programs: dict = {}
    ok = True
    for target in TARGETS:
        name = f"botport-{target}.tar.gz"
        path = folder / name
        if not path.is_file():
            print(f"{name}: not in the folder (this target will not be pinned)")
            continue
        data = path.read_bytes()
        a_sha = hashlib.sha256(data).hexdigest()
        prog = program_from_archive(data)
        p_sha = hashlib.sha256(prog).hexdigest()
        notes = []
        if name in sums:
            same = sums[name] == a_sha
            notes.append("matches SHA256SUMS" if same else "DOES NOT MATCH SHA256SUMS")
            ok = ok and same
        if gh:
            with tempfile.TemporaryDirectory() as tmp:
                exe = os.path.join(tmp, "botport")
                with open(exe, "wb") as fh:
                    fh.write(prog)
                good = attested(gh, str(path), a.tag) and attested(gh, exe, a.tag)
            notes.append("attestation verified (archive and program)" if good else "ATTESTATION FAILED")
            ok = ok and good
        archives[name] = a_sha
        programs[target] = p_sha
        print(f"{name}: archive {a_sha}  program {p_sha}  [{'; '.join(notes) or 'not verified'}]")
    if not programs:
        raise SystemExit("no release archive found in the folder")
    if not ok:
        raise SystemExit("a check failed: nothing written")
    if not a.write:
        print("dry run: pass --write to update twin/botport_pins.json")
        return 0
    if a.no_verify and not a.write_unverified:
        raise SystemExit("not verified: pass --write-unverified to write a test table anyway")
    doc = json.loads(PINS.read_text(encoding="utf-8"))
    doc.setdefault("releases", {})[a.tag] = {"archives": archives, "programs": programs}
    tmp = PINS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, PINS)
    print(f"wrote {PINS.relative_to(ROOT)} for {a.tag}: {len(programs)} of {len(TARGETS)} targets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
