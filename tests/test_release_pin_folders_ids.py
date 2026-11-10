"""Hardening: the attestation pins the expected botport release tag, the plugin's folders and the cron
store fail closed on links / foreign or shared folders, and the opaque twin id is a salted HMAC."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from twin_testlib import central, cli, config, home, model, service, store, svc, tools  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import importer
from hermes_plugins.grok_bot_twin.twin import engine as E
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

FAKE = Path(__file__).resolve().parent / "fake_botport.py"


def _write_exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    path.write_text(f"#!{sys.executable} -I\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_gh(tmp_path: Path, rc: int = 0) -> Path:
    log = tmp_path / "gh_calls.jsonl"
    body = ("import json, sys\n"
            f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:]}}) + '\\n')\n"
            f"sys.exit({rc})\n")
    _write_exe(tmp_path / "ghbin" / "gh", body)
    return log


def _gh_calls(log: Path) -> list[list[str]]:
    return [json.loads(ln)["argv"] for ln in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def bp(home, tmp_path, monkeypatch):
    """The fake botport configured, and a fake gh (passing) first on PATH."""
    exe = _write_exe(tmp_path / "bin" / "botport", FAKE.read_text(encoding="utf-8"))
    log = _fake_gh(tmp_path, 0)
    monkeypatch.setenv("PATH", f"{tmp_path / 'ghbin'}:/usr/bin:/bin")

    class R:
        def settings(self, **extra):
            config.set_override({"botport_path": str(exe), **extra})
            service.reset_service()
            self.svc = service.get_service()
            return self.svc

    r = R()
    r.exe, r.log = exe, log
    r.settings()
    return r


def _record(home: Path) -> dict:
    return json.loads((central(home) / "botport-trust.json").read_text())


# ---- the attestation pins the release tag ------------------------------------------------------------------

def test_setup_pins_expected_tag_and_records_it(bp, home):
    res = bp.svc.trust_botport()
    argv = _gh_calls(bp.log)[-1]
    assert argv[:2] == ["attestation", "verify"]
    assert argv[3:] == ["--repo", "hudsonwa/botport", "--signer-workflow",
                        "hudsonwa/botport/.github/workflows/release.yml", "--source-ref", "refs/tags/v0.2.1"]
    assert E.EXPECTED_BOTPORT_TAG == "v0.2.1"
    assert res["attested"] and res["release_tag"] == "v0.2.1"
    rec = _record(home)
    assert rec["release_tag"] == "v0.2.1" and rec["attestation"]["tag"] == "v0.2.1"
    att = [c for c in bp.svc.doctor()["checks"] if c["id"] == "botport_attestation"][0]
    assert att["ok"] and "release v0.2.1" in att["message"]


def test_setup_tag_from_config_and_re_attests_on_a_new_tag(bp, home):
    svc = bp.settings(botport_release_tag="v0.3.1")
    assert svc.trust_botport()["release_tag"] == "v0.3.1"
    assert _gh_calls(bp.log)[-1][-2:] == ["--source-ref", "refs/tags/v0.3.1"]
    n = len(_gh_calls(bp.log))
    svc.trust_botport()  # same bytes, same tag: the earlier verification is reused
    assert len(_gh_calls(bp.log)) == n
    svc.trust_botport(False, "v0.4.0")  # an explicit tag wins over the setting, and is verified afresh
    assert len(_gh_calls(bp.log)) == n + 1 and _gh_calls(bp.log)[-1][-1] == "refs/tags/v0.4.0"
    assert _record(home)["release_tag"] == "v0.4.0"


@pytest.mark.parametrize("bad", ["latest", "0.2.0", "v0.2", "v0.2.0-rc1", "v0.2.0; rm -rf /", "--help", 7])
def test_bad_tags_are_refused(bp, home, bad):
    with pytest.raises(EngineError) as ei:
        bp.svc.trust_botport(False, bad)
    assert ei.value.code == "bad_input"
    svc = bp.settings(botport_release_tag=bad)
    with pytest.raises(EngineError) as ei:
        svc.trust_botport()  # a malformed setting is refused, never silently replaced
    assert ei.value.code == "bad_input"
    assert _gh_calls(bp.log) == [] and not (central(home) / "botport-trust.json").exists()
    if isinstance(bad, str):
        with pytest.raises(EngineError):
            E.attestation_command("/b", bad)


def test_cli_setup_tag(bp, home):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["setup", "--tag", "v0.5.2"])
    assert rc == 0, err.getvalue()
    assert "--source-ref refs/tags/v0.5.2" in out.getvalue() and "release v0.5.2" in out.getvalue()
    assert _gh_calls(bp.log)[-1][-2:] == ["--source-ref", "refs/tags/v0.5.2"]
    assert _record(home)["release_tag"] == "v0.5.2"
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        cli.build_parser().parse_args(["setup", "--tag", "latest"])


def test_unattested_records_no_tag(bp, home):
    res = bp.svc.trust_botport(True)
    assert res["allow_unattested"] and res["release_tag"] is None and _record(home)["release_tag"] is None
    assert _gh_calls(bp.log) == []


def test_plugin_yaml_declares_the_tag_setting():
    text = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8")
    block = text.split("\n  botport_release_tag:\n", 1)[1].split("\n  grok_app_dir:", 1)[0]
    assert "type: str" in block and "description:" in block and "v0.2.0" in block


# ---- folders and the cron store fail closed ----------------------------------------------------------------

def test_central_dir_created_0700_per_component(home):
    base = store.central_dir()
    for d in (home / "plugin-data", base):
        assert d.is_dir() and not d.is_symlink() and (d.stat().st_mode & 0o777) == 0o700
    sub = store.ensure_dir(base / "botport-state" / "xdg" / "config")
    for d in (base / "botport-state", base / "botport-state" / "xdg", sub):
        assert (d.stat().st_mode & 0o777) == 0o700


@pytest.mark.parametrize("where", ["plugin-data", "plugin-data/grok-bot-twin", "plugin-data/grok-bot-twin/botport-state"])
def test_symlinked_component_refused(home, tmp_path, where):
    store.central_dir()
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    link = home / where
    if link.exists():
        os.rename(link, tmp_path / "moved")
    os.symlink(target, link)
    with pytest.raises(store.UnsafePath):
        store.ensure_dir(central(home) / "botport-state")
    assert list(target.iterdir()) == []  # nothing was created through the link


@pytest.mark.parametrize("where", ["", "plugin-data", "plugin-data/grok-bot-twin"])
@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_group_or_world_writable_component_refused(home, where, mode):
    store.central_dir()
    d = home / where if where else home
    os.chmod(d, mode)
    try:
        with pytest.raises(store.UnsafePath):
            store.central_dir()
        with pytest.raises(OSError):
            store.atomic_write_json(central(home) / "x.json", {})
    finally:
        os.chmod(d, 0o700)
    assert store.central_dir() == central(home)


def test_ancestors_above_root_sticky_ok_shared_refused(tmp_path, monkeypatch):
    sticky = tmp_path / "sticky"
    sticky.mkdir()
    os.chmod(sticky, 0o1777)
    monkeypatch.setenv("HERMES_HOME", str(sticky / "hermes"))
    assert store.central_dir() == sticky / "hermes" / "plugin-data" / "grok-bot-twin"
    assert ((sticky / "hermes").stat().st_mode & 0o777) == 0o700
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)
    monkeypatch.setenv("HERMES_HOME", str(shared / "hermes"))
    try:
        with pytest.raises(store.UnsafePath):
            store.central_dir()
        assert not (shared / "hermes").exists()
    finally:
        os.chmod(shared, 0o700)


def test_engine_refuses_linked_xdg_and_bin_folders(bp, home, tmp_path):
    bp.svc.trust_botport()
    eng = bp.svc.botport()
    eng.run(["version"])
    xdg = central(home) / "botport-state" / "xdg"
    other = tmp_path / "other-xdg"
    other.mkdir(mode=0o700)
    os.rename(xdg, tmp_path / "old-xdg")
    os.symlink(other, xdg)
    with pytest.raises(EngineError) as ei:
        eng.run(["version"])
    assert ei.value.code == "unsafe_folder"
    os.unlink(xdg)
    eng.run(["version"])
    bindir = central(home) / "botport-bin"
    os.rename(bindir, tmp_path / "old-bin")
    os.symlink(tmp_path / "old-bin", bindir)
    with pytest.raises(EngineError) as ei:
        eng.run(["version"])
    assert ei.value.code == "untrusted_binary"


def _jobs_home(tmp_path: Path) -> tuple[Path, Path]:
    prof = tmp_path / "prof"
    (prof / "cron").mkdir(parents=True)
    real = tmp_path / "real-jobs.json"
    real.write_text(json.dumps({"jobs": [{"id": "j1", "paused_reason": "Imported from Grok Bot: resume once"}]}))
    return prof, real


def test_jobs_json_regular_file_read(tmp_path, monkeypatch):
    from hermes_plugins.grok_bot_twin.twin import store as _store
    monkeypatch.setattr(_store, "hermes_root", lambda: tmp_path)
    os.chmod(tmp_path, 0o700)
    prof, real = _jobs_home(tmp_path)
    os.rename(real, prof / "cron" / "jobs.json")
    assert [j["id"] for j in importer._load_jobs(prof)] == ["j1"]


def test_jobs_json_symlink_refused(tmp_path):
    prof, real = _jobs_home(tmp_path)
    os.symlink(real, prof / "cron" / "jobs.json")
    assert importer._load_jobs(prof) == []


def test_jobs_json_fifo_and_oversize_refused(tmp_path, monkeypatch):
    prof, real = _jobs_home(tmp_path)
    jobs = prof / "cron" / "jobs.json"
    os.mkfifo(jobs)
    t0 = time.monotonic()
    assert importer._load_jobs(prof) == []  # O_NONBLOCK + fstat: never blocks on a FIFO
    assert time.monotonic() - t0 < 5
    os.unlink(jobs)
    os.rename(real, jobs)
    monkeypatch.setattr(importer, "JOBS_FILE_CAP", 10)
    assert importer._load_jobs(prof) == []


# ---- the opaque twin id is a salted HMAC --------------------------------------------------------------------

def test_ref_is_salted_hmac_and_stable(home):
    ref = model.pair_ref("acme-helper")
    assert re.fullmatch(r"t-[0-9a-f]{16}", ref) and model.REF_RE.match(ref)
    assert model.pair_ref("acme-helper") == ref  # stable across calls
    salt_file = central(home) / store.SALT_FILE
    assert (salt_file.stat().st_mode & 0o777) == 0o600
    salt = bytes.fromhex(salt_file.read_text().strip())
    assert len(salt) >= 16
    assert ref == "t-" + hmac.new(salt, b"acme-helper", hashlib.sha256).hexdigest()[:16]
    assert ref != "t-" + hashlib.sha256(b"acme-helper").hexdigest()[:16]  # not an unsalted hash
    store._salts.clear()  # a new process reads the same salt back
    assert model.pair_ref("acme-helper") == ref and salt_file.read_text().strip() == salt.hex()


def test_ref_differs_across_installs_and_salts(tmp_path, monkeypatch):
    refs = []
    for name in ("install-a", "install-b"):
        h = tmp_path / name
        h.mkdir(mode=0o700)
        monkeypatch.setenv("HERMES_HOME", str(h))
        refs.append(model.pair_ref("acme-helper"))
    assert refs[0] != refs[1]
    assert model.pair_ref("x", b"\x01" * 16) != model.pair_ref("x", b"\x02" * 16)
    assert model.pair_ref("x", b"\x01" * 16) == model.pair_ref("x", b"\x01" * 16)


def test_salt_file_link_refused(home, tmp_path):
    base = store.central_dir()
    fake = tmp_path / "salt"
    fake.write_text("00" * 32)
    os.chmod(fake, 0o600)
    os.symlink(fake, base / store.SALT_FILE)
    with pytest.raises(store.UnsafePath):
        model.pair_ref("acme-helper")


def test_tools_map_hmac_id_back(svc):
    svc.demo_action("start", "acme-helper-profile")
    ref = model.pair_ref("acme-helper")
    out = json.loads(tools.twin_status({"pair": ref}))
    assert out["twins"][0]["twin_id"] == ref
    other = model.pair_ref("acme-helper", b"\x07" * 16)  # same pair, another install's salt: unknown here
    out = json.loads(tools.twin_status({"pair": other}))
    assert out["error"] == "No such twin." and out["twin_ids"] == [ref]
    old_style = "t-" + hashlib.sha256(b"acme-helper").hexdigest()[:8]
    assert "error" in json.loads(tools.twin_status({"pair": old_style}))


def test_jobs_file_parent_link_refused(home, tmp_path):
    """A link anywhere between the Hermes root and cron/jobs.json is never followed."""
    import json as _json
    from hermes_plugins.grok_bot_twin.twin import importer, store as _store
    root = _store.hermes_root()
    real = tmp_path / "elsewhere" / "cron"
    real.mkdir(parents=True, mode=0o700)
    (real / "jobs.json").write_text(_json.dumps({"jobs": [{"id": "j1"}]}))
    prof = root / "profiles" / "linked"
    prof.mkdir(parents=True, mode=0o700)
    (prof / "cron").symlink_to(real)
    assert importer._read_jobs_file(prof / "cron" / "jobs.json") is None
    good = root / "profiles" / "plain" / "cron"
    good.mkdir(parents=True, mode=0o700)
    (good / "jobs.json").write_text(_json.dumps({"jobs": [{"id": "j2"}]}))
    assert importer._read_jobs_file(good / "jobs.json") is not None
