"""F3 (C2): Stop twinning forgets the twin's Hermes -> Grok allow.

remove() forgot the twin's panel choices, briefs, transcripts and memory-facts records, but never h2g.json: its
`allow` entry stayed, so h2g_mirror.owner_allowed (the signed list the Mirror checks) kept naming the bot after the
twin was gone. Now remove() forgets it (allow, base, runner, record of changes; its waiting requests leave). The
leftover scan guards every other twin-keyed file under the plugin's data folder. Invented bots only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from twin_testlib import home  # noqa: F401
from test_ux_flows import fake  # noqa: F401
from test_setup_download_and_twin_facts import twin  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import h2g, h2g_mirror, mem_layers

PID = "acme-support"
BOT = "b-acme-0001"

# Files that keep naming a removed twin ON PURPOSE (each named, so the scan still guards everything else):
KEPT = {
    "former.json",  # Grok bot -> its Hermes bot, so twinning the same bot again picks that Hermes bot up again
    "status-cache.json",  # the last status read, a working file the next read rewrites from botport (whose own
    #                       state is botport's to forget: the fake botport here keeps listing the twin)
}
# Not populated here (they need a linked computer; known leftovers): xs-memory.json (by design:
# a twin no longer listed is switched off on the next tick and forgotten after 30 days), xs-config.json and
# routines.json (keyed by the fleet's twin list, not traced).
SKIP_DIRS = {"botport-state"}  # botport's own state (it forgets the twin itself in `botport remove`)


@pytest.fixture(autouse=True)
def _no_panel_choice(monkeypatch):
    from hermes_plugins.grok_bot_twin.twin import sync_panel
    monkeypatch.setattr(sync_panel, "wanted", lambda pid, item: "both")


@pytest.fixture
def allowed(twin):  # noqa: F811
    """A real twin whose Grok bot Hermes -> Grok may change, with one change still waiting for the Mirror."""
    w = h2g.FakeGrokWriter(delay=True)
    w.put_bot(BOT, name="Acme Support", description="An older description in Grok.", instructions="Be brief.")
    twin.h2g_test_writer = w
    twin.to_grok_allow(PID, "all")
    p = twin.to_grok_preview(PID)
    assert twin.to_grok_apply(PID, p["change_id"])["state"] == "sent"
    assert BOT in h2g_mirror.owner_allowed(twin.central)
    return twin, w


def _names_twin(data, key: str) -> bool:
    if isinstance(data, dict):
        return any(k == key or _names_twin(v, key) for k, v in data.items())
    if isinstance(data, list):
        return any(_names_twin(v, key) for v in data)
    return data == key


def _leftovers(root: Path, key: str) -> list:
    out = []
    for f in sorted(root.rglob("*.json")):
        rel = f.relative_to(root)
        if rel.parts[0] in SKIP_DIRS or str(rel) in KEPT:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _names_twin(data, key):
            out.append(str(rel))
    return out


def test_F3_stop_twinning_forgets_the_hermes_to_grok_allow(allowed):
    svc, w = allowed
    svc.remove(PID, False)
    assert BOT not in h2g_mirror.owner_allowed(svc.central)
    d = json.loads((svc.central / h2g.STATE_FILE).read_text())
    assert all(PID not in d.get(k, {}) for k in ("allow", "base", "runner", "keep_grok"))
    assert not any(c.get("pid") == PID for c in d.get("changes") or [])
    assert w._load()["requests"] == {}  # the change still waiting left the Mirror's box: it is never made


def test_F3_dry_run_changes_nothing_and_says_so(allowed):
    svc, w = allowed
    before = (svc.central / h2g.STATE_FILE).read_bytes()
    plan = svc.remove(PID, True)
    assert h2g.STOP_GOES in plan["goes"]
    assert (svc.central / h2g.STATE_FILE).read_bytes() == before
    assert BOT in h2g_mirror.owner_allowed(svc.central) and w._load()["requests"]
    assert PID in svc.pair_ids()


def test_F3_no_twin_keyed_file_keeps_a_removed_twin(allowed):
    svc, _ = allowed  # (allowing also set its What syncs rows)
    mem_layers.set_direction(PID, "facts", "both")
    root = svc.central
    seen = set(_leftovers(root, PID))
    assert {"pairs.json", h2g.STATE_FILE, "sync-panel.json", mem_layers.SETTINGS_FILE} <= seen  # the scan sees it
    svc.remove(PID, False)
    assert _leftovers(root, PID) == []


def test_F3_forget_republishes_the_signed_list_and_writes_nothing_for_a_stranger(tmp_path):
    calls: list = []

    class Writer(h2g.FakeGrokWriter):
        def publish_allowed(self, strict=False):
            calls.append(("publish", h2g_mirror.owner_allowed(tmp_path)))

        def forget_bot(self, bot):
            calls.append(("forget_bot", bot))
    w = Writer()
    w.put_bot(BOT, name="Acme Support", description="Invented.", instructions="Be brief.")
    x = h2g.H2G(tmp_path, w, hermes=lambda p: {"name": "Acme Support", "description": "Invented.",
                                               "instructions": "Be brief.", "routines": []}, bot=lambda p: BOT)
    x.forget(PID)
    assert not (tmp_path / h2g.STATE_FILE).exists() and calls == []  # never allowed: nothing written, nothing told
    x.allow(PID, "all")
    calls.clear()
    x.forget(PID)
    assert calls == [("forget_bot", BOT), ("publish", [])]  # the Mirror's signed list no longer names the bot
