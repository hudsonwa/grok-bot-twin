"""BotportEngine against tests/fake_botport.py: argv, envelope parsing, failure modes, trust and env scrubbing."""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from twin import engine as E  # noqa: E402
from twin.engine import BotportEngine, EngineError  # noqa: E402

FAKE_SRC = (Path(__file__).resolve().parent / "fake_botport.py").read_text(encoding="utf-8")
ALLOWED_ENV = {"PATH", "HOME", "LANG", "HERMES_GATEWAY_LOCK_DIR", "NO_COLOR", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
               "__CF_USER_TEXT_ENCODING", "LC_CTYPE"}  # the last two may be added by the OS/interpreter itself


def write_exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    path.write_text(f"#!{sys.executable} -I\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


class Rig:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.exe = write_exe(tmp / "bin" / "botport", FAKE_SRC)
        self.central = tmp / "central"
        self.state = self.central / "botport-state"
        self.home = tmp / "hermes-home"
        self.eng = BotportEngine(str(self.exe), self.home, self.state)

    def mode(self, **kw) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "fake_mode.json").write_text(json.dumps(kw))

    def calls(self) -> list[dict]:
        p = self.state / "fake_calls.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def last_args(self) -> list[str]:
        """argv of the last call without the global flags."""
        argv = self.calls()[-1]["argv"]
        return [a for a in argv if a != "--json" and not a.startswith(("--hermes-home=", "--state-dir=",
                                                                        "--grok-app-dir="))]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SECRET_TOKEN", "xai-FAKEFAKEFAKEFAKE0000")
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "gw"))
    r = Rig(tmp_path)
    r.eng.trust(allow_unattested=True)
    return r


# ---------------------------------------------------------------------------------------------- argv

CASES = [
    (lambda e: e.grok_list(), ["grok", "list"]),
    (lambda e: e.grok_list(["Acme*", "Recipe?"]), ["grok", "list", "--only=Acme*", "--only=Recipe?"]),
    (lambda e: e.create("b-acme-0001", None, False), ["twin", "create", "--grok-id=b-acme-0001"]),
    (lambda e: e.create("b-acme-0001", "acme", True, from_export="/x/export.json", approve="all"),
     ["twin", "create", "--grok-id=b-acme-0001", "--profile=acme", "--from-export=/x/export.json", "--approve=all",
      "--dry-run"]),
    (lambda e: e.create_all(["Acme*"], dry_run=True), ["twin", "create", "--all", "--only=Acme*", "--dry-run"]),
    (lambda e: e.check(), ["twin", "check"]),
    (lambda e: e.sync("acme-helper"), ["twin", "sync", "--pair=acme-helper"]),
    (lambda e: e.sync("acme-helper", "h2g", True), ["twin", "sync", "--pair=acme-helper", "--dry-run"]),
    (lambda e: e.sync(None), ["twin", "sync", "--all"]),
    (lambda e: e.status(), ["twin", "status", "--all"]),
    (lambda e: e.status("acme-helper"), ["twin", "status", "--pair=acme-helper"]),
    (lambda e: e.conflicts("acme-helper"), ["twin", "conflicts", "--pair=acme-helper"]),
    (lambda e: e.resolve("acme-helper", "skill:greet-visitors", "hermes"),
     ["twin", "resolve", "--pair=acme-helper", "--conflict=skill:greet-visitors", "--keep=hermes"]),
    (lambda e: e.approve("acme-helper", "skill:greet-visitors"),
     ["twin", "approve", "--pair=acme-helper", "--item=skill:greet-visitors"]),
    (lambda e: e.restore_points("acme-helper"), ["twin", "restore-points", "--pair=acme-helper"]),
    (lambda e: e.restore("acme-helper", "persona.name@8ef22c6d10ba"),
     ["twin", "restore", "--pair=acme-helper", "--point=persona.name@8ef22c6d10ba"]),
    (lambda e: e.restore("acme-helper", "persona.name@8ef22c6d10ba", dry_run=True),
     ["twin", "restore", "--pair=acme-helper", "--point=persona.name@8ef22c6d10ba", "--dry-run"]),
    (lambda e: e.recent("acme-helper"), ["twin", "recent", "--pair=acme-helper", "--limit=10"]),
    (lambda e: e.recent("acme-helper", 999), ["twin", "recent", "--pair=acme-helper", "--limit=50"]),
    (lambda e: e.remove("acme-helper"), ["twin", "remove", "--pair=acme-helper"]),
    (lambda e: e.remove("acme-helper", dry_run=True), ["twin", "remove", "--pair=acme-helper", "--dry-run"]),
    (lambda e: e.mirror("acme-helper", off=True), ["twin", "mirror", "--pair=acme-helper", "--off"]),
    (lambda e: e.parity("acme-helper"), ["twin", "parity", "--pair=acme-helper"]),
    (lambda e: e.pack("acme-helper"), ["twin", "sync", "--pair=acme-helper"]),
    (lambda e: e.demo(), ["twin", "demo"]),
    (lambda e: e.demo("rename"), ["twin", "demo", "--step=rename"]),
    (lambda e: e.doctor(), ["twin", "doctor"]),
]


@pytest.mark.parametrize("call,want", CASES)
def test_argv(rig, call, want):
    data = call(rig.eng)
    assert isinstance(data, dict)
    assert rig.last_args() == want
    argv = rig.calls()[-1]["argv"]
    assert "--json" in argv and f"--hermes-home={rig.home}" in argv and f"--state-dir={rig.state}" in argv
    assert not any(a.startswith("--direction") for a in argv)


def test_grok_app_dir_and_version(rig, tmp_path):
    eng = BotportEngine(str(rig.exe), rig.home, rig.state, grok_app_dir=str(tmp_path / "grokapp"))
    eng.trust(allow_unattested=True)
    eng.check()
    assert f"--grok-app-dir={tmp_path / 'grokapp'}" in rig.calls()[-1]["argv"]
    assert any(c["argv"][0] == "version" for c in rig.calls())


def test_mirror_folder_and_usage(rig, tmp_path):
    d = rig.eng.mirror("acme-helper", pair_folder=str(tmp_path / "drive" / "pair"))
    assert d["connected"] is True and d["steps"][0]["id"] == "mirror_bot"
    assert rig.last_args() == ["twin", "mirror", "--pair=acme-helper", f"--pair-folder={tmp_path / 'drive' / 'pair'}"]
    for kw in ({}, {"pair_folder": "/x", "off": True}):
        with pytest.raises(EngineError) as ei:
            rig.eng.mirror("acme-helper", **kw)
        assert ei.value.code == "usage"


def test_option_injection_is_inline(rig):
    rig.eng.status("--all")
    assert rig.last_args() == ["twin", "status", "--pair=--all"]
    rig.eng.resolve("acme-helper", "--keep=grok", "hermes")
    assert rig.last_args()[3:] == ["--conflict=--keep=grok", "--keep=hermes"]
    for bad in ("a\nb", "", "x\x00y", "y" * 2000):
        with pytest.raises(EngineError) as ei:
            rig.eng.conflicts(bad)
        assert ei.value.code == "usage"
    with pytest.raises(EngineError):
        rig.eng.resolve("acme-helper", "skill:x", "both")
    with pytest.raises(EngineError):
        rig.eng.demo("rm -rf")


# ---------------------------------------------------------------------------------------------- parsing

def test_parsing_shapes(rig):
    st = rig.eng.status()
    p = st["pairs"][0]
    assert p["grok"]["name"] == "Acme Helper"  # NUL/BEL dropped
    assert st["_warnings"] == [{"code": "no_bots", "message": "nothing new"}]
    assert "_attention" not in st
    s = rig.eng.sync("acme-helper")
    assert s["pairs"][0]["future_field"] == {"x": 1}  # unknown fields tolerated
    assert rig.eng.approve("acme-helper", "skill:a")["approved"] == [{"item": "skill:a", "to": "local"}]
    rm = rig.eng.remove("acme-helper", dry_run=True)
    assert rm["profile_kept"] is True and rm["dry_run"] is True and rm["removed"]
    pk = rig.eng.pack("acme-helper")
    assert pk["pack"] == {"gen": None, "items": 1, "written_to": None} and pk["pairs"]
    par = rig.eng.parity("acme-helper")
    assert par["passed"] == 1 and par["total"] == 2


def test_attention_exit_1_is_not_failure(rig):
    rig.mode(attention=True)
    d = rig.eng.parity("acme-helper")
    assert d["_attention"] is True and d["checks"]
    d = rig.eng.conflicts("acme-helper")
    assert d["_attention"] is True and d["conflicts"][0]["conflict_id"] == "skill:greet-visitors"


@pytest.mark.parametrize("mode,code", [
    ("badjson", "bad_output"), ("nonobject", "bad_output"), ("wrongschema", "schema_mismatch"),
    ("badtypes", "bad_output"), ("okbadexit", "bad_output"), ("empty", "exit_4"), ("huge", "output_too_large"),
    ("error", "pair_not_found"),
])
def test_bad_output(rig, mode, code):
    rig.mode(mode=mode)
    with pytest.raises(EngineError) as ei:
        rig.eng.status("acme-helper")
    assert ei.value.code == code


def test_locked_no_retry(rig):
    rig.mode(mode="locked")
    n = len(rig.calls())
    with pytest.raises(EngineError) as ei:
        rig.eng.sync("acme-helper")
    assert ei.value.code == "locked" and ei.value.message == "Busy, try again in a moment"
    assert len(rig.calls()) == n + 1  # exactly one attempt


def test_timeout_kills(rig):
    rig.mode(mode="sleep")
    t = time.monotonic()
    with pytest.raises(EngineError) as ei:
        rig.eng.run(["twin", "status", "--all"], timeout=1.0)
    assert ei.value.code == "timeout"
    assert time.monotonic() - t < 6


def test_parse_envelope_direct():
    env = {"schema": "botport.twin.v1", "ok": True, "command": "x", "data": {"a": "b\x00c", "n": [1, 2]},
           "warnings": [], "error": None, "extra": True}
    d = E.parse_envelope(0, json.dumps(env).encode(), b"", False, "x")
    assert d["a"] == "bc" and d["n"] == [1, 2]
    with pytest.raises(EngineError):
        E.parse_envelope(0, b"[" * 100000, b"", False, "x")
    env2 = dict(env, ok=False, data=None, error={"code": "secret_refused", "message": "held back", "hint": None})
    with pytest.raises(EngineError) as ei:
        E.parse_envelope(3, json.dumps(env2).encode(), b"", False, "x")
    assert ei.value.code == "secret_refused" and ei.value.hint == ""


# ---------------------------------------------------------------------------------------------- env

def test_env_scrubbed(rig):
    rig.eng.status()
    env = rig.calls()[-1]["env"]
    assert "FAKE_SECRET_TOKEN" not in env
    assert "xai-FAKEFAKEFAKEFAKE0000" not in json.dumps(env)
    assert set(env) <= ALLOWED_ENV
    assert env["HERMES_GATEWAY_LOCK_DIR"] == os.environ["HERMES_GATEWAY_LOCK_DIR"]
    assert env["NO_COLOR"] == "1"


# ---------------------------------------------------------------------------------------------- trust

def test_untrusted_until_trust(tmp_path):
    r = Rig(tmp_path)
    with pytest.raises(EngineError) as ei:
        r.eng.status()
    assert ei.value.code == "untrusted_binary"
    assert r.calls() == []
    info = r.eng.info()
    assert info["available"] is False and info["trusted"] is False
    st = r.eng.trust(allow_unattested=True)
    assert st["trusted"] is True and st["version"] == "0.1.2" and len(st["sha256"]) == 64
    rec = r.central / "botport-trust.json"
    assert stat.S_IMODE(rec.stat().st_mode) == 0o600
    saved = json.loads(rec.read_text())
    assert saved["path"] == os.path.realpath(r.exe) and saved["size"] == r.exe.stat().st_size
    assert saved["attestation"] == {"verified": False}
    assert r.eng.info()["available"] is True


def test_exec_is_private_copy(rig):
    rig.eng.status()
    exe = rig.calls()[-1]["exe"]
    assert exe != str(rig.exe) and Path(exe).parent == rig.central / "botport-bin"
    assert stat.S_IMODE(Path(exe).stat().st_mode) == 0o700
    assert stat.S_IMODE((rig.central / "botport-bin").stat().st_mode) == 0o700


def test_digest_change_fails_closed(rig):
    rig.exe.write_text(rig.exe.read_text() + "\n# changed\n")
    rig.exe.chmod(0o755)
    n = len(rig.calls())
    with pytest.raises(EngineError) as ei:
        rig.eng.status()
    assert ei.value.code == "untrusted_binary" and "changed" in ei.value.message
    assert len(rig.calls()) == n
    doc = rig.eng.doctor()
    assert doc["checks"][0]["id"] == "botport_trust" and doc["checks"][0]["ok"] is False
    assert doc["_attention"] is True


def test_group_writable_dir_fails_closed(rig):
    (rig.tmp / "bin").chmod(0o775)
    with pytest.raises(EngineError) as ei:
        rig.eng.status()
    assert ei.value.code == "untrusted_binary" and "writable" in ei.value.message
    (rig.tmp / "bin").chmod(0o755)
    rig.exe.chmod(0o775)
    with pytest.raises(EngineError):
        rig.eng.status()
    rig.exe.chmod(0o755)
    rig.eng.status()


def test_trust_refuses_unsafe(tmp_path):
    r = Rig(tmp_path)
    (tmp_path / "bin").chmod(0o777)
    with pytest.raises(EngineError) as ei:
        r.eng.trust(allow_unattested=True)
    assert ei.value.code == "untrusted_binary"
    (tmp_path / "bin").chmod(0o755)


def test_symlink_swap_after_trust(rig):
    other = rig.tmp / "other"
    write_exe(other / "botport", FAKE_SRC)
    (rig.tmp / "bin").rename(rig.tmp / "bin-old")
    (rig.tmp / "bin").symlink_to(other, target_is_directory=True)
    with pytest.raises(EngineError) as ei:
        rig.eng.status()
    assert ei.value.code == "untrusted_binary" and "link" in ei.value.message


def test_binary_replaced_by_symlink(rig):
    real = rig.tmp / "elsewhere" / "botport"
    write_exe(real, FAKE_SRC)
    rig.exe.unlink()
    rig.exe.symlink_to(real)
    with pytest.raises(EngineError) as ei:
        rig.eng.status()
    assert ei.value.code == "untrusted_binary"


def test_tampered_copy_is_replaced(rig):
    rig.eng.status()
    copy = Path(rig.calls()[-1]["exe"])
    copy.write_text(f"#!{sys.executable}\nimport sys; print('{{}}'); sys.exit(0)\n")
    d = rig.eng.status()  # the tampered copy fails its digest check and is re-made from the trusted binary
    assert d["pairs"][0]["pair_id"] == "acme-helper"
    assert E.sha256_file(str(copy)) == E.sha256_file(str(rig.exe))


def test_retrust_prunes_old_copy(rig):
    rig.eng.status()
    old = Path(rig.calls()[-1]["exe"])
    rig.exe.write_text(rig.exe.read_text() + "\n# new build\n")
    rig.exe.chmod(0o755)
    rig.eng.trust(allow_unattested=True)
    rig.eng.status()
    new = Path(rig.calls()[-1]["exe"])
    assert new != old and not old.exists() and new.exists()


def test_trust_record_permissions(rig):
    rec = rig.central / "botport-trust.json"
    rec.chmod(0o644)
    with pytest.raises(EngineError) as ei:
        rig.eng.status()
    assert ei.value.code == "untrusted_binary"


def test_trust_record_for_other_path(rig, tmp_path):
    other = write_exe(tmp_path / "bin2" / "botport", FAKE_SRC)
    eng2 = BotportEngine(str(other), rig.home, rig.state)
    with pytest.raises(EngineError) as ei:
        eng2.status()
    assert ei.value.code == "untrusted_binary"


def test_find_botport_resolves(tmp_path, monkeypatch):
    real = write_exe(tmp_path / "real" / "botport", "print(1)\n")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    (link_dir / "botport").symlink_to(real)
    monkeypatch.setenv("PATH", str(link_dir))
    assert E.find_botport() == os.path.realpath(real)
    assert E.find_botport(str(link_dir / "botport")) == os.path.realpath(real)
    assert E.find_botport("relative/botport") is None
    assert E.find_botport(str(tmp_path / "nope")) is None


# ---------------------------------------------------------------------------------------------- features

def test_required_features_check(tmp_path):
    r = Rig(tmp_path)
    r.mode(features=["grok-list", "twin-sync"])
    with pytest.raises(EngineError) as ei:
        r.eng.trust(allow_unattested=True)
    assert ei.value.code == "engine_too_old" and "twin-status" in ei.value.message


def test_optional_feature_missing(tmp_path):
    r = Rig(tmp_path)
    r.mode(features=list(E.REQUIRED_FEATURES))
    r.eng.trust(allow_unattested=True)
    for call in (lambda: r.eng.approve("acme-helper", "skill:a"), lambda: r.eng.remove("acme-helper"),
                 lambda: r.eng.mirror("acme-helper", off=True), lambda: r.eng.demo()):
        with pytest.raises(EngineError) as ei:
            call()
        assert ei.value.code == "unsupported"


def test_base_engine_stubs():
    eng = E.Engine()
    for name, args in (("approve", ("p", "i")), ("mirror", ("p",)), ("remove", ("p",))):
        with pytest.raises(NotImplementedError):
            getattr(eng, name)(*args)


# ---------------------------------------------------------------------------------------------- attestation

def test_attestation_command():
    assert E.attestation_command("/opt/bp/botport") == [
        "gh", "attestation", "verify", "/opt/bp/botport", "--repo", "hudsonwa/botport",
        "--signer-workflow", "hudsonwa/botport/.github/workflows/release.yml"]
    assert E.attestation_command("/b", tag="v0.2.0")[-2:] == ["--source-ref", "refs/tags/v0.2.0"]


def _fake_gh(tmp_path, rc: int) -> Path:
    log = tmp_path / "gh_calls.jsonl"
    body = ("import json, os, sys\n"
            f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'env': dict(os.environ)}}) + '\\n')\n"
            f"sys.stderr.write('verification result: fake\\n'); sys.exit({rc})\n")
    write_exe(tmp_path / "ghbin" / "gh", body)
    return log


@pytest.mark.parametrize("rc", [0, 1])
def test_verify_attestation(rig, tmp_path, monkeypatch, rc):
    log = _fake_gh(tmp_path, rc)
    monkeypatch.setenv("PATH", f"{tmp_path / 'ghbin'}:/usr/bin:/bin")
    res = rig.eng.verify_attestation(tag="v0.2.0")
    assert res["verified"] is (rc == 0)
    call = json.loads(log.read_text().splitlines()[-1])
    assert call["argv"][:2] == ["attestation", "verify"]
    assert Path(call["argv"][2]).parent == rig.central / "botport-bin"  # the exact bytes we execute
    assert call["argv"][3:] == ["--repo", "hudsonwa/botport", "--signer-workflow",
                                "hudsonwa/botport/.github/workflows/release.yml", "--source-ref", "refs/tags/v0.2.0"]
    assert "FAKE_SECRET_TOKEN" not in call["env"]
    assert rig.eng.trust_status()["attestation"]["verified"] is (rc == 0)
    doc = rig.eng.doctor()
    att = [c for c in doc["checks"] if c["id"] == "botport_attestation"][0]
    assert att["ok"] is (rc == 0)
    if rc:
        assert "gh attestation verify" in att["fix"]


def test_trust_require_attestation(tmp_path, monkeypatch):
    _fake_gh(tmp_path, 1)
    monkeypatch.setenv("PATH", f"{tmp_path / 'ghbin'}:/usr/bin:/bin")
    r = Rig(tmp_path)
    with pytest.raises(EngineError) as ei:
        r.eng.trust()
    assert ei.value.code == "attestation_failed"
    assert not (r.central / "botport-trust.json").exists()
    _fake_gh(tmp_path, 0)
    st = r.eng.trust()
    assert st["trusted"] and st["attestation"]["verified"] is True


def test_trust_never_runs_binary_before_attestation(tmp_path, monkeypatch):
    """With attestation required and gh failing, botport is never executed (not even `version`)."""
    marker = tmp_path / "botport-ran"
    log = _fake_gh(tmp_path, 1)
    monkeypatch.setenv("PATH", f"{tmp_path / 'ghbin'}:/usr/bin:/bin")
    r = Rig(tmp_path)
    write_exe(r.exe, f"open({str(marker)!r}, 'a').write('ran\\n')\n" + FAKE_SRC)
    with pytest.raises(EngineError) as ei:
        r.eng.trust()
    assert ei.value.code == "attestation_failed"
    assert not marker.exists() and r.calls() == []
    call = json.loads(log.read_text().splitlines()[-1])
    assert Path(call["argv"][2]).parent == r.central / "botport-bin"  # gh checked the private copy
    assert not (r.central / "botport-trust.json").exists()
    _fake_gh(tmp_path, 0)
    assert r.eng.trust()["trusted"]
    assert marker.read_text() == "ran\n"  # `version` ran exactly once, after the attestation passed


def test_doctor_merges_checks(rig):
    doc = rig.eng.doctor()
    ids = [c["id"] for c in doc["checks"]]
    assert ids[:2] == ["botport_trust", "botport_attestation"] and "state_dir" in ids


def test_env_points_xdg_inside_state_dir(rig):
    rig.eng.status()
    env = rig.calls()[-1]["env"]
    for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        assert env[k].startswith(str(rig.eng.state_dir)), k
