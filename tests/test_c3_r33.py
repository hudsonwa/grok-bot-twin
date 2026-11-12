"""R33 (C3, the "status is a pure read" slice): status() reads files and botport and publishes one finished
snapshot. It writes nothing (no pairs.json, stuck.json, copied.json or status-cache.json), and the open conflicts
are folded in before the snapshot is published (R8: a lock-free reader never sees "up to date" while a choice is
open). The timer's tick keeps the files (save_status)."""
from __future__ import annotations

import copy
import json
import os

from twin_testlib import central, home  # noqa: F401
from test_ux_flows import _pair, fake  # noqa: F401
from test_setup_download_and_twin_facts import mode, profile, twin  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import instr_sync

PID = "acme-support"


def _files(root):
    """Every file the plugin keeps (botport's own folder aside: the fake botport logs its calls there), with its
    bytes and modification time."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel == "botport-state" or rel.startswith("botport-state/"):
            continue
        st = p.lstat()
        out[rel] = (p.read_bytes() if p.is_file() else b"", st.st_mtime_ns)
    return out


def _registry(home):  # noqa: F811
    return {p["pair_id"]: p for p in json.loads((central(home) / "pairs.json").read_text())["pairs"]}


def test_R33_status_writes_nothing_and_the_tick_keeps_the_files(twin, home):  # noqa: F811
    svc = twin
    svc.tick()  # (what create left behind is kept first)
    cache = central(home) / "status-cache.json"
    # botport now reads a new name in Grok, a refusal note no longer holds (nothing waits), a pasted note is stale
    mode(home, name="Acme Support Desk")
    (central(home) / "stuck.json").write_text(json.dumps({PID: {"file": "SOUL.md", "line": 3, "at": "2000-01-01T09:00:00Z"}}))
    (central(home) / "copied.json").write_text(json.dumps({PID: {"skill:gone": "0" * 64}}))
    before = _files(central(home))
    st = svc.status(fresh=True)
    p = next(x for x in st["pairs"] if x["pair_id"] == PID)
    assert p["grok"]["name"] == "Acme Support Desk" and p["state"] != "error"
    assert _files(central(home)) == before  # not one file written, created or touched
    # the tick keeps what the read learnt
    old_cache = cache.read_text() if cache.exists() else ""
    svc.tick()
    assert _registry(home)[PID]["grok"]["name"] == "Acme Support Desk"
    assert json.loads((central(home) / "stuck.json").read_text()) == {}
    cached = json.loads(cache.read_text())["status"]
    assert cache.read_text() != old_cache and cached["pairs"][0]["grok"]["name"] == "Acme Support Desk"
    # the same snapshot is not written twice
    at = cache.stat().st_mtime_ns
    svc.save_status()
    assert cache.stat().st_mtime_ns == at


def test_R33_the_published_snapshot_already_holds_the_open_conflicts(twin, home, monkeypatch):  # noqa: F811
    svc = twin
    (central(home) / instr_sync.STATE_FILE).write_text(json.dumps({PID: {"state": "conflict"}}))
    svc.status(fresh=True)  # (the choice is open from here on)
    seen = []
    orig = type(svc)._fold_open_conflicts

    def fold(self, pairs):  # a lock-free reader (the per-turn hook, Hermes -> Grok) in the middle of the fold
        seen.append(copy.deepcopy(self._status[1]) if self._status else None)  # (as it reads it then, not after)
        return orig(self, pairs)

    monkeypatch.setattr(type(svc), "_fold_open_conflicts", fold)
    for _ in range(2):
        st = svc.status(fresh=True)
        assert next(x for x in st["pairs"] if x["pair_id"] == PID)["open_conflicts"] == \
            [{"where": "instructions", "item": "instructions"}]
    assert seen
    for snap in seen:
        for x in (snap or {}).get("pairs") or []:
            if x["pair_id"] == PID:
                assert x.get("open_conflicts") and x["state"] == "needs_you", x["state"]
    assert svc.cached_status(compute=False)["pairs"] is st["pairs"]


def test_R33_a_fresh_read_of_a_new_install_writes_nothing(home):  # noqa: F811
    from twin_testlib import service
    svc = service.get_service()
    root = central(home)
    before = _files(root) if root.exists() else {}
    svc.status(fresh=True)
    assert (_files(root) if root.exists() else {}) == before
    assert not os.path.exists(root / "status-cache.json")


def test_R33_a_read_from_before_stop_twinning_never_brings_the_twin_back(twin, home):  # noqa: F811
    svc = twin
    mode(home, name="Acme Support Desk")
    assert any(p["pair_id"] == PID for p in svc.status(fresh=True)["pairs"])
    # another process (the terminal) stops twinning it after that read; the tick then keeps the read
    (central(home) / "pairs.json").write_text(json.dumps({"pairs": []}))
    svc.save_status()
    assert json.loads((central(home) / "pairs.json").read_text()) == {"pairs": []}


def test_R33_a_twin_made_outside_the_plugin_is_taken_in_by_the_tick(twin, home):  # noqa: F811
    svc = twin
    (central(home) / "pairs.json").write_text(json.dumps({"pairs": []}))  # botport knows it, the plugin does not
    before = _files(central(home))
    assert any(p["pair_id"] == PID for p in svc.status(fresh=True)["pairs"])
    assert _files(central(home)) == before
    svc.save_status()
    assert PID in _registry(home)
