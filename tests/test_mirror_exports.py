"""Setup exports that come through skill sync (twin/mirror_exports.py): the request's extra sentence, the sync bot's
script naming exports/ files on copy-up lines, and the batch watch picking them up from <shared folder>/exports/.

Invented bots only (Acme Support, Recipe Planner). Every test has its own HERMES_HOME and HOME (conftest.py); the
shared folder and the pretend box are under tmp_path. No network, no Grok app.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from twin_testlib import central, config, home, service  # noqa: F401
from test_batch import Hermes, bot, export, world, SAMPLE  # noqa: F401
from test_ux_flows import fake  # noqa: F401
from test_setup_download_and_twin_facts import profile, twin  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import batch, mirror_exports as M, setup_export as sx
from hermes_plugins.grok_bot_twin.twin import skill_sync as SS

START = 1_800_000_000.0
ACME_ID = "aaaaaaaa-0000-4000-8000-000000000001"


def share(home: Path, tmp: Path) -> Path:
    """Skill sync set up with a shared folder under tmp (only the remembered folder: no kit, no bot)."""
    shared = tmp / "shared"
    (shared / "exports").mkdir(parents=True)
    SS.save_state(central(home), {"root": str(shared)})
    return shared


def put(folder: Path, name: str, text: str, mtime: float = START + 5) -> Path:
    f = folder / name
    f.write_text(text, encoding="utf-8")
    os.utime(f, (mtime, mtime))
    return f


def test_request_and_pickup_without_and_with_skill_sync(world, home, tmp_path):
    info = world.batch_info(None)
    assert info["request"] == sx.REQUEST
    assert info["pickup"] == {"via": "downloads", "label": "Downloads"}
    share(home, tmp_path)
    info = world.batch_info(None)
    assert info["request"].startswith(sx.REQUEST.rstrip("\n")) and info["request"].endswith(M.SENTENCE + "\n")
    assert "/workspace/skill-sync/exports/grok-bot-export-<your bot id>.json" in info["request"]
    assert info["pickup"] == {"via": "mirror", "label": "the shared folder"}
    assert str(tmp_path) not in json.dumps(info)


def test_exports_in_the_shared_folder_are_imported_once_and_left_there(world, home, tmp_path, monkeypatch):
    h = Hermes(home, monkeypatch)
    ex = share(home, tmp_path) / "exports"
    ticked = ["acme-support", "recipe-planner", SAMPLE]
    put(ex, f"grok-bot-export-{ACME_ID}.json", export(bot("Acme Support")))
    put(ex, "acme.json", export(bot("Recipe Planner")))                                     # not an export's name
    put(ex, "grok-bot-export-big.json", '{"format": "grok-bot-export"}' + " " * (M.SIZE_MAX + 1))
    put(ex, "grok-bot-export-old.json", export(bot("Recipe Planner")), mtime=START - M.GRACE_S - 60)
    outside = tmp_path / "outside.json"
    outside.write_text(export(bot("Recipe Planner")))
    (ex / "grok-bot-export-link.json").symlink_to(outside)
    got = world.downloads_scan(START, ticked)
    assert got["files"] == 1 and got["waiting"] == ["recipe-planner"]
    assert [(r["pair_id"], r["state"]) for r in got["results"]] == [("acme-support", "done")]
    assert h.asked == ["acme-support"]
    # dedupe: not again, in this process or a new one
    assert world.downloads_scan(START, ticked)["results"] == []
    world._batch_files = {}
    assert world.downloads_scan(START, ticked)["results"] == [] and h.asked == ["acme-support"]
    assert (ex / f"grok-bot-export-{ACME_ID}.json").exists() and (ex / "acme.json").exists()
    # Downloads is still watched next to the shared folder
    dl = Path(os.path.expanduser("~")) / "Downloads"
    dl.mkdir(exist_ok=True)
    put(dl, "recipe.json", export(bot("Recipe Planner")), mtime=START + 9)
    got = world.downloads_scan(START, ticked)
    assert [(r["pair_id"], r["state"]) for r in got["results"]] == [("recipe-planner", "done")]
    assert got["files"] == 2 and got["waiting"] == []


def test_a_linked_exports_folder_is_not_read(world, home, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    put(elsewhere, f"grok-bot-export-{ACME_ID}.json", export(bot("Acme Support")))
    (shared / "exports").symlink_to(elsewhere)
    SS.save_state(central(home), {"root": str(shared)})
    assert M.exports_dir(central(home)) is None and M.candidates(central(home), START) == []
    assert world.downloads_scan(START, ["acme-support"])["files"] == 0


def test_without_skill_sync_the_shared_exports_are_not_looked_at(world, home, tmp_path):
    assert M.candidates(central(home), 0) == []
    assert world.downloads_scan(0, ["acme-support"]) == {"files": 0, "results": [], "waiting": ["acme-support"]}


def test_the_script_names_exports_on_copy_up_lines():
    s = SS.BOX_SCRIPT
    assert 'say "copy up: exports/$b"' in s and '-le 2097152' in s and 'chmod 700 "$ROOT/exports"' in s
    assert "files under exports/: copy up nothing else" in " ".join(SS.instructions("", "Skill sync").split())


@pytest.mark.skipif(shutil.which("sh") is None or not os.path.exists("/bin/sh"), reason="needs /bin/sh")
def test_a_rehearsal_run_prints_the_exports_copy_up_lines(tmp_path):
    root = tmp_path / "box" / "skill-sync"
    data = tmp_path / "box" / "agent-data"
    (root / "bin").mkdir(parents=True)
    data.mkdir(parents=True)
    script = root / "bin" / SS.SCRIPT_NAME
    script.write_text(SS.BOX_SCRIPT)
    bp = root / "bin" / "botport"
    bp.write_text('#!/bin/sh\ncase "$*" in --version) echo "botport 0.2.1" ;; esac\nexit 0\n')
    bp.chmod(0o755)
    ex = root / "exports"
    ex.mkdir(mode=0o700)
    (ex / f"grok-bot-export-{ACME_ID}.json").write_text(export(bot("Acme Support")))
    (ex / "grok-bot-export-recipe-planner.json").write_text(export(bot("Recipe Planner")))
    (ex / "grok-bot-export-empty.json").write_text("")
    (ex / "grok-bot-export-bad_name.json").write_text("{}")
    (ex / "notes.json").write_text("{}")
    (ex / "grok-bot-export-big.json").write_bytes(b" " * (2 * 1024 * 1024 + 1))
    (ex / "grok-bot-export-link.json").symlink_to(ex / "notes.json")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "box" / "home"), "SKILL_SYNC_ROOT": str(root),
           "SKILL_SYNC_AGENT_DATA": str(data), "LANG": "C"}
    cp = subprocess.run(["/bin/sh", str(script), "run"], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                        timeout=60)
    out = cp.stdout.decode() + cp.stderr.decode()
    ups = sorted(line for line in out.splitlines() if line.startswith("copy up: exports/"))
    assert ups == [f"copy up: exports/grok-bot-export-{ACME_ID}.json",
                   "copy up: exports/grok-bot-export-recipe-planner.json"], out
    assert "copy up: status/heartbeat.json" in out
    assert (ex.stat().st_mode & 0o777) == 0o700
