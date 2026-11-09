"""TwinService with a (fake) botport: trust check, real-pair routing, demo + real side by side.

Uses tests/fake_botport.py (owned by the engine-adapter lane) as an executable in a private tmp folder."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from twin_testlib import config, dumps, home, service, tools  # noqa: F401

FAKE = Path(__file__).resolve().parent / "fake_botport.py"


@pytest.fixture
def bp_svc(home, tmp_path):
    if not FAKE.is_file():
        pytest.skip("fake_botport.py not present")
    bindir = tmp_path / "bin"
    bindir.mkdir(mode=0o755)
    exe = bindir / "botport"
    exe.write_text(f"#!{sys.executable} -I\n" + FAKE.read_text(encoding="utf-8"), encoding="utf-8")
    exe.chmod(0o755)
    os.chmod(tmp_path, 0o700)
    config.set_override({"botport_path": str(exe)})
    service.reset_service()
    return service.get_service()


def test_untrusted_botport_fails_closed(bp_svc):
    info = bp_svc.engine_info()
    assert info["available"] is False and info["trusted"] is False
    st = bp_svc.status(fresh=True)
    assert st["engine"]["available"] is False
    checks = {c["id"]: c for c in bp_svc.doctor(probe_importers=False)["checks"]}
    assert checks["botport_trust"]["ok"] is False and "trust" in checks["botport_trust"]["fix"].lower()


def test_trusted_botport_routes_real_pairs(bp_svc):
    res = bp_svc.trust_botport(True)
    assert res["trusted"] is True and res["sha256"]
    bp_svc.invalidate()
    st = bp_svc.status(fresh=True)
    assert st["engine"]["available"] is True and st["mode"] == "botport"
    real = [p for p in st["pairs"] if not p["demo"]]
    assert real and real[0]["pair_id"] == "acme-helper"
    out = bp_svc.sync("acme-helper")
    assert out["pairs"][0]["applied"][0]["kind"] == "name"
    # demo twins and real twins side by side: demo world uses its own ids
    bp_svc.demo_action("start", "demo-profile")
    assert bp_svc.demo.pair_ids() == ["demo-acme-helper"]  # never shadows the real twin's id
    assert bp_svc.engine_for("acme-helper") is not bp_svc.demo
    ids = [p["pair_id"] for p in bp_svc.status(fresh=True)["pairs"]]
    assert "acme-helper" in ids and "demo-acme-helper" in ids
    sent = bp_svc.send_to_grok("acme-helper")
    assert "pack" in sent
    view = json.loads(tools.twin_status({}))
    assert all(set(t) >= {"twin_id", "name", "state", "to_do_in_grok", "to_do_in_hermes"} for t in view["twins"])
    assert all("ref" not in t and "pair_id" not in t for t in view["twins"])
    with pytest.raises(service.BadInput):
        bp_svc.trust_botport("yes")
    with pytest.raises(service.BadInput):
        bp_svc.attest_botport("latest; rm -rf")


def test_preview_keeps_botports_masked_preview_text(bp_svc):
    # the Desktop's create dialog shows each item's preview text (botport masks suspected secrets first)
    bp_svc.trust_botport(True)
    prev = bp_svc.preview(["b-acme-0001"])
    item = prev["pairs"][0]["items"][0]
    assert item["item"] == "persona.name" and item["preview"] == "Acme Helper"
    assert prev["skipped"] == []
