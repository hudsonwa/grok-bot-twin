"""TwinService against the fake botport: masked names everywhere, --account, per-twin sync errors, the Mirror
pair folder rule, and routines/connectors left waiting after create."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from twin_testlib import ROOT, all_text_under, central, cli, config, dumps, fake_key, home, service, timer, tools  # noqa: F401
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

FAKE = Path(__file__).resolve().parent / "fake_botport.py"
LABELS = ["a1b2c3d4", "0f0e0d0c"]


@pytest.fixture
def rig(home, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir(mode=0o755)
    exe = bindir / "botport"
    exe.write_text(f"#!{sys.executable} -I\n" + FAKE.read_text(encoding="utf-8"), encoding="utf-8")
    exe.chmod(0o755)
    os.chmod(tmp_path, 0o700)

    class R:
        def settings(self, **extra):
            config.set_override({"botport_path": str(exe), **extra})
            service.reset_service()
            self.svc = service.get_service()
            return self.svc

        def mode(self, **kw):
            d = central(home) / "botport-state"
            d.mkdir(parents=True, exist_ok=True)
            (d / "fake_mode.json").write_text(json.dumps(kw))

        def calls(self):
            p = central(home) / "botport-state" / "fake_calls.jsonl"
            return [json.loads(x)["argv"] for x in p.read_text().splitlines()] if p.exists() else []

    r = R()
    r.settings()
    r.svc.trust_botport(True)
    return r


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue() + err.getvalue()


# ---- a suspected secret in a bot name never leaves the plugin ---------------------------------------------

def test_secret_in_bot_name_is_masked_everywhere(rig, home, monkeypatch):
    key = fake_key()
    rig.mode(name=f"Acme {key} Support")
    svc = rig.svc
    texts = [dumps(svc.status(fresh=True)), dumps(svc.grok_list()), dumps(svc.sync(None)),
             dumps(svc.preview(["b-acme-0001"])), tools.twin_status({}), tools.twin_status({"pair": "acme-support"})]
    assert "[secret]" in texts[0]
    for argv in (["status"], ["list-grok"], ["sync"], ["status", "--json"]):
        texts.append(run_cli(argv)[1])
    texts.append(dumps(svc.events()))
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_names", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    app = fastapi.FastAPI()
    app.include_router(api.router, prefix="/p")
    client = TestClient(app)
    for path in ("/p/status", "/p/grok/bots", "/p/events", "/p/pairs/acme-support/checklist"):
        r = client.get(path)
        assert r.status_code == 200, path
        texts.append(r.text)
    texts.append(client.post("/p/sync", json={}).text)
    for t in texts:
        assert key not in t
    # nothing the plugin itself keeps holds it either (botport-state is botport's folder, with the fake's logs)
    for f in central(home).rglob("*"):
        if f.is_file() and "botport-state" not in f.relative_to(central(home)).parts:
            assert key not in f.read_text(errors="replace"), f.name


# ---- --account LABEL ----------------------------------------------------------------------------------------

def test_account_choice_message_and_setting(rig):
    rig.mode(accounts=LABELS)
    with pytest.raises(EngineError) as ei:
        rig.svc.grok_list()
    msg = ei.value.message
    assert ei.value.code == "account_choice"
    assert all(label in msg for label in LABELS) and "grok_account" in msg
    svc = rig.settings(grok_account="A1B2C3D4")  # labels are hex; case is forgiven
    assert svc.grok_list()["bots"]
    svc.status(fresh=True)
    svc.sync("acme-support")
    svc.conflicts("acme-support")
    by_cmd = {" ".join(a[:2]): a for a in rig.calls()}
    for cmd in ("grok list", "twin status", "twin sync"):
        assert "--account=a1b2c3d4" in by_cmd[cmd], cmd
    assert not any(a.startswith("--account") for a in by_cmd["twin conflicts"])
    svc = rig.settings(grok_account="not-a-label")
    assert svc.botport().account == ""


# ---- twin sync --all: a failing twin is reported in place ---------------------------------------------------

def test_sync_all_reports_a_failing_twin_in_place(rig, home):
    svc = rig.svc
    svc.status(fresh=True)  # registers acme-support
    rig.mode(sync_error_pair="recipe-planner")
    out = svc.sync(None)["pairs"]
    by = {p["pair_id"]: p for p in out}
    assert by["acme-support"]["error"] is None and by["acme-support"]["applied"]
    assert by["recipe-planner"]["state"] == "error"
    assert by["recipe-planner"]["error"] == {"code": "internal", "message": "this twin's state file could not be read"}
    assert any(e["type"] == "sync_error" and e["pair_id"] == "recipe-planner" for e in svc.events()["events"])
    rc, text = run_cli(["sync"])
    assert rc == 1 and "can't check right now: this twin's state file could not be read" in text
    assert "updated name" in text  # the other twin was still synced, and says so
    assert "could not sync" not in text and "Traceback" not in text


def test_sync_all_keeps_demo_results_when_botport_cannot_run(rig):
    svc = rig.svc
    svc.status(fresh=True)
    svc.demo_action("start", "demo-profile")
    rig.mode(accounts=LABELS)
    out = {p["pair_id"]: p for p in svc.sync(None)["pairs"]}
    assert out["acme-support"]["state"] == "error" and out["acme-support"]["error"]["code"] == "account_choice"
    assert "grok_account" in out["acme-support"]["error"]["message"]
    assert out["demo-acme-support"]["error"] is None


# ---- the Mirror pair folder ---------------------------------------------------------------------------------

def test_pair_folder_must_be_absolute_real_and_outside(rig, home, tmp_path):
    svc = rig.svc
    svc.status(fresh=True)
    good = tmp_path / "example-pair-folder"
    good.mkdir()
    linkdir = tmp_path / "via-link"
    linkdir.symlink_to(tmp_path)
    inside = home / "shared"
    inside.mkdir()
    for bad in ("example-pair-folder", str(linkdir / "example-pair-folder"), str(inside),
                str(central(home) / "botport-state"), str(tmp_path / "missing"), "", 3):
        with pytest.raises(service.BadInput):
            svc.mirror("acme-support", bad)
    assert not any(a[:2] == ["twin", "mirror"] for a in rig.calls())
    res = svc.mirror("acme-support", str(good))
    assert res["connected"] is True
    arg = [a for a in rig.calls()[-1] if a.startswith("--pair-folder=")][0]
    assert Path(arg.split("=", 1)[1]).is_absolute() and arg.endswith("example-pair-folder")


# ---- routines and connectors stay waiting after create ------------------------------------------------------

def test_create_leaves_routines_and_connectors_waiting(rig):
    svc = rig.svc
    created = svc.create(["b-acme-0001"])["pairs"]
    items = [w["item"] for w in created[0]["waiting"]]
    assert items == ["routine:Morning summary", "connector:example-files"]
    st = {p["pair_id"]: p for p in svc.status(fresh=True)["pairs"]}
    assert [w["item"] for w in st["acme-support"]["waiting"]] == items
    rc, text = run_cli(["status"])
    # each waiting item is listed with the command that approves it
    assert 'waiting for your go-ahead: Morning summary (routine)' in text
    assert 'hermes twin approve "routine:Morning summary"' in text  # one twin: no id needed
    assert "waiting for your OK" not in text
    assert "connector:example-files" in text
    out = svc.approve("acme-support", "connector:example-files")
    assert out["approved"][0]["item"] == "connector:example-files"
    # the approve's sync lists nothing waiting any more, so the Approve buttons go away
    assert svc.status(fresh=True)["pairs"][0]["waiting"] == []
