"""The Twins page's routes for twin memory, through FastAPI's TestClient (skipped where FastAPI is not installed)."""
from __future__ import annotations

import importlib.util
import json

import pytest

from twin_testlib import ROOT, central, config, home, service, timer  # noqa: F401

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

P = "/api/plugins/grok-bot-twin"
DELIM = "\n§\n"


@pytest.fixture
def client(home, tmp_path, monkeypatch):  # noqa: F811
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "share_address": "127.0.0.1",
                         "share_port": 0, "machine_name": "mini"})
    data = central(home)
    data.mkdir(parents=True, exist_ok=True)
    (data / "pairs.json").write_text(json.dumps({"pairs": [{
        "pair_id": "acme-scout", "grok": {"id": "b-1", "name": "Test Scout", "description": "Finds trails."},
        "hermes": {"profile": "test-scout"}, "created_at": "2000-01-01T09:00:00Z"}]}), encoding="utf-8")
    mem = home / "profiles" / "test-scout" / "memories"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text(DELIM.join(["Prefers the north trail.", "The door code is 4821"]), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_memory_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    app = FastAPI()
    app.include_router(api.router, prefix=P)
    return TestClient(app), mem


def test_the_page_can_switch_forget_and_put_back_and_never_gets_a_held_notes_text(client):
    c, mem = client
    v = c.get(P + "/fleet/memory").json()
    assert v["role"] == "none" and v["twins"] == [] and "stays on this computer" in v["sentence"]
    assert c.post(P + "/fleet/share", json={"on": True}).json()["role"] == "source"
    v = c.get(P + "/fleet/memory").json()
    twin = v["twins"][0]
    assert twin["name"] == "Test Scout" and twin["on"] is False and twin["state"] == "off" and len(v["what"]) == 5
    assert twin["count"] == {"memory": 0, "user": 0}  # off: the files were not opened
    v = c.post(P + "/fleet/memory", json={"twin": twin["id"], "on": True}).json()
    twin = v["twins"][0]
    assert twin["on"] is True and twin["count"] == {"memory": 2, "user": 0}
    assert twin["notes"] == ["1 note here stays on this computer only: it looks like it holds a password or key."]
    assert "4821" not in json.dumps(v) and "north trail" not in json.dumps(v)  # counts and sentences, never a note
    assert len(twin["restore"]) == 1
    # wrong shapes are refused before anything is looked at; an unknown twin is said in words
    for body in ({}, {"twin": 7, "on": True}, {"twin": twin["id"], "on": "yes"}, {"twin": "x" * 200, "on": True}, [1]):
        assert c.post(P + "/fleet/memory", json=body).status_code == 400
    assert c.post(P + "/fleet/memory/computer", json={"twin": twin["id"], "machine": "../x", "on": False}).status_code == 400
    assert c.post(P + "/fleet/memory/resolve", json={"twin": twin["id"], "conflict": 1, "keep": "both"}).status_code == 400
    r = c.post(P + "/fleet/memory", json={"twin": "Nobody", "on": True}).json()
    assert r["error"]["code"] == "unknown_twin" and r["error"]["message"] == "That twin is not on this computer."
    r = c.post(P + "/fleet/memory/computer", json={"twin": twin["id"], "machine": "m-0123456789ab", "on": False}).json()
    assert r["error"]["code"] == "unknown_machine"
    r = c.post(P + "/fleet/memory/restore", json={"twin": twin["id"], "point": "p-000000000000"}).json()
    assert r["error"]["code"] == "unknown_point"
    v = c.post(P + "/fleet/memory/restore", json={"twin": twin["id"]}).json()
    assert v["restored"]["sentence"].startswith("Put back.") and v["twins"][0]["on"] is False
    v = c.post(P + "/fleet/memory/forget", json={"twin": twin["id"]}).json()
    assert v["forgot"]["notes"] == 0 and v["twins"][0]["restore"] == []
    assert (mem / "MEMORY.md").read_text(encoding="utf-8").split(DELIM) == ["Prefers the north trail.",
                                                                           "The door code is 4821"]
