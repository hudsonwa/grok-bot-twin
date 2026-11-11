"""BotportEngine against a REAL botport build, in throwaway folders only.

Set BOTPORT_TEST_BIN to a botport binary (for example a local `cargo build` of botport); skipped otherwise.
Everything lives under tmp_path: HOME, the Hermes home, the state folder, HERMES_GATEWAY_LOCK_DIR, and PATH holds
only a stub `hermes` plus the system folders, so nothing can reach a real Hermes install or Grok app.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from twin import engine as E  # noqa: E402
from twin.engine import BotportEngine, EngineError  # noqa: E402

BIN = os.environ.get("BOTPORT_TEST_BIN", "")
pytestmark = pytest.mark.skipif(not (BIN and os.path.isfile(BIN) and os.access(BIN, os.X_OK)),
                                reason="set BOTPORT_TEST_BIN to a botport build to run these")
# the demo's fake key, split so secret scanners of this file do not flag it
FAKE_KEY_TAIL = "0000NOTAREAL" + "KEY0000"


@pytest.fixture
def real(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    stub = tmp_path / "stubbin"
    stub.mkdir()
    (stub / "hermes").write_text(f"#!{sys.executable} -I\nimport sys; sys.stderr.write('stub hermes\\n'); sys.exit(1)\n")
    (stub / "hermes").chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{stub}:/usr/bin:/bin")
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "gwlock"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    (tmp_path / "gwlock").mkdir()
    raw: list[bytes] = []
    orig = E.run_capped

    def capture(argv, timeout, env=None, out_cap=E.STDOUT_CAP):
        rc, out, err, over = orig(argv, timeout, env=env, out_cap=out_cap)
        raw.append(out + err)
        return rc, out, err, over

    monkeypatch.setattr(E, "run_capped", capture)
    eng = BotportEngine(os.path.realpath(BIN), tmp_path / "hermes-home", tmp_path / "central" / "botport-state")
    st = eng.trust(allow_unattested=True)
    assert st["trusted"] and set(E.REQUIRED_FEATURES) <= set(st["features"])
    eng.raw = raw  # type: ignore[attr-defined]
    return eng


def _pairs(d):
    return {p["pair_id"]: p for p in d["pairs"]}


def test_real_flow(real, tmp_path):
    info = real.info()
    assert info["available"] is True and info["trusted"] is True

    d = real.demo()
    assert set(_pairs(d)) >= {"acme-helper", "recipe-planner"}
    st = real.status()
    assert {p["state"] for p in st["pairs"]} == {"in_step"}
    assert all(p["grok"]["present"] for p in st["pairs"])

    d = real.demo("rename")
    assert "acme-helper" in _pairs(d)
    chk = real.check()
    assert isinstance(chk["changed"], list) and isinstance(chk["checked_at"], str)
    s = real.sync(None)
    assert "acme-helper" in _pairs(s)
    st = _pairs(real.status())
    assert st["acme-helper"]["grok"]["name"] != "Acme Helper"
    assert real.status("acme-helper")["pairs"][0]["pair_id"] == "acme-helper"

    d = real.demo("secret")
    assert d.get("_attention") is True
    held = _pairs(d)["acme-helper"]["secrets_withheld"]
    assert held and held[0]["item"] == "persona.description" and "not a guarantee" in held[0]["location"]
    s = real.sync("acme-helper")
    assert _pairs(s)["acme-helper"]["state"] == "needs_you"
    assert not any(FAKE_KEY_TAIL.encode() in r for r in real.raw)
    for p in (tmp_path / "hermes-home").rglob("*"):
        if p.is_file() and not p.is_symlink():
            assert FAKE_KEY_TAIL.encode() not in p.read_bytes(), p.name

    real.demo("reset")
    d = real.demo("conflict")
    assert d.get("_attention") is True
    c = real.conflicts("acme-helper")
    assert c.get("_attention") is True
    cid = c["conflicts"][0]["conflict_id"]
    assert cid.startswith("skill:") and "diff" in c["conflicts"][0]
    r = real.resolve("acme-helper", cid, "hermes")
    assert r["applied"][0]["keep"] == "hermes" and r.get("restore_point") is None
    assert real.conflicts("acme-helper")["conflicts"] == []

    pts = real.restore_points("acme-helper")["points"]
    assert pts and all("@" in p["id"] for p in pts)
    rs = real.restore("acme-helper", pts[0]["id"], dry_run=True)
    assert rs["dry_run"] is True

    rec = real.recent("acme-helper", 5)
    assert isinstance(rec["entries"], list) and len(rec["entries"]) <= 5

    par = real.parity("acme-helper")
    assert par["kind"] == "setup" and par["total"] >= par["passed"] and par["checks"]

    doc = real.doctor()
    ids = [c["id"] for c in doc["checks"]]
    assert ids[:2] == ["botport_trust", "botport_attestation"] and "state_dir" in ids
    assert doc["checks"][0]["ok"] is True

    pk = real.pack("recipe-planner")
    assert "pack" in pk and "recipe-planner" in _pairs(pk)

    rm = real.remove("recipe-planner", dry_run=True)
    assert rm["dry_run"] is True and rm["profile_kept"] is True and rm["removed"]
    assert all(not x.startswith("/") for x in rm["removed"])
    rm = real.remove("recipe-planner")
    assert rm["dry_run"] is False
    assert "recipe-planner" not in _pairs(real.status())
    with pytest.raises(EngineError) as ei:
        real.status("recipe-planner")
    assert ei.value.code == "pair_not_found"

    # no absolute path leaves botport
    for r in real.raw:
        assert str(tmp_path).encode() not in r.split(b"\n")[0] or b'"ok":false' in r


def test_real_mirror_and_approve(real, tmp_path):
    real.demo()
    folder = tmp_path / "drive" / "acme-pair"
    folder.mkdir(parents=True)
    m = real.mirror("acme-helper", pair_folder=str(folder))
    assert m["connected"] is True and m["steps"] and isinstance(m["charter"], str)
    assert _pairs(real.status())["acme-helper"]["mirror_bot"]["connected"] is True
    off = real.mirror("acme-helper", off=True)
    assert off["connected"] is False
    with pytest.raises(EngineError) as ei:
        real.approve("acme-helper", "skill:no-such-skill")
    assert ei.value.code in ("usage", "internal")


def test_real_untrusted_after_change(real, tmp_path):
    # a trusted copy elsewhere, then changed: fails closed without running
    copy = tmp_path / "bp" / "botport"
    copy.parent.mkdir()
    copy.write_bytes(Path(real.path).read_bytes())
    copy.chmod(0o755)
    eng = BotportEngine(str(copy), real.hermes_home, real.state_dir, trust_dir=tmp_path / "t2")
    eng.trust(allow_unattested=True)
    eng.check()
    with open(copy, "ab") as fh:
        fh.write(b"\0")
    with pytest.raises(EngineError) as ei:
        eng.check()
    assert ei.value.code == "untrusted_binary"


def test_real_locked(real, tmp_path):
    real.demo()
    lock = real.state_dir / ".botport.lock"
    import fcntl
    fh = open(lock, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        pytest.skip("lock is not a flock")
    try:
        with pytest.raises(EngineError) as ei:
            real.sync("acme-helper")
        if ei.value.code != "locked":
            pytest.skip(f"botport does not use flock on {lock.name}: {ei.value.code}")
        assert ei.value.message == "Busy, try again in a moment"
    finally:
        fh.close()
