"""dashboard/plugin_api.py through FastAPI's TestClient."""
from __future__ import annotations

import importlib.util

import pytest

from twin_testlib import ROOT, dumps, fake_key, home, timer  # noqa: F401

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

P = "/api/plugins/grok-bot-twin"


def _api():
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_test", ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client(home, monkeypatch):
    api = _api()
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    assert api._package().__name__ == "hermes_plugins.grok_bot_twin.twin"  # reuses the loaded plugin
    app = FastAPI()
    app.include_router(api.router, prefix=P)
    return TestClient(app)


def test_status_empty_then_demo(client):
    r = client.get(P + "/status")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "setup" and body["pairs"] == [] and body["interval_s"] == 60
    assert body["engine"]["available"] is False and "gh attestation verify" in body["engine"]["install_hint"]
    r = client.post(P + "/demo", json={"action": "start"})
    assert r.status_code == 200 and r.json()["pairs"][0]["state"] == "in_step"
    assert client.get(P + "/status").json()["mode"] == "demo"


def test_full_demo_tour(client):
    client.post(P + "/demo", json={"action": "start"})
    client.post(P + "/demo", json={"action": "grok-edit"})
    r = client.post(P + "/sync", json={"pair_id": "acme-helper"}).json()
    assert r["pairs"][0]["applied"][0]["kind"] == "description"
    client.post(P + "/demo", json={"action": "conflict"})
    client.post(P + "/sync", json={})
    cs = client.get(P + "/pairs/acme-helper/conflicts").json()["conflicts"]
    assert cs[0]["conflict_id"] == "skill:greet-visitors"
    r = client.post(P + "/pairs/acme-helper/resolve", json={"conflict_id": cs[0]["conflict_id"], "keep": "grok"})
    assert r.json()["applied"][0]["keep"] == "grok"
    pts = client.get(P + "/pairs/acme-helper/restore-points").json()["points"]
    assert pts[0]["id"].startswith("skill:greet-visitors@") and pts[0]["scope"] == "item"
    r = client.post(P + "/pairs/acme-helper/restore", json={"point_id": pts[0]["id"]})
    assert r.status_code == 200 and r.json()["restored"] == ["skill:greet-visitors"]
    assert client.get(P + "/pairs/acme-helper/recent?limit=5").json()["entries"]
    assert client.get(P + "/pairs/acme-helper/parity").json()["total"] > 0
    assert "switch" in client.get(P + "/pairs/acme-helper/checklist").json()
    assert client.get(P + "/grok/bots").json()["app_found"] is True
    pv = client.post(P + "/twins/preview", json={"grok_ids": ["b-recipe-0002"]}).json()
    assert pv["pairs"][0]["grok"]["name"] == "Recipe Planner"
    mk = client.post(P + "/twins/create", json={"grok_ids": ["b-recipe-0002"]}).json()
    assert mk["pairs"][0]["pair_id"] == "recipe-planner"
    client.post(P + "/demo", json={"action": "hermes-edit"})
    r = client.post(P + "/pairs/acme-helper/approve", json={"item": "skill:fun-fact"}).json()
    assert r["approved"][0]["item"] == "skill:fun-fact"
    assert client.post(P + "/pairs/acme-helper/send-to-grok").json()["skills"] == 1
    on = client.post(P + "/pairs/acme-helper/mirror", json={}).json()  # demo: no pair folder needed
    assert on["connected"] is True and on["steps"] and "Mirror bot charter" in on["charter"]
    st = client.get(P + "/status").json()
    assert next(p for p in st["pairs"] if p["pair_id"] == "acme-helper")["mirror_bot"] == {"connected": True}
    assert client.post(P + "/pairs/acme-helper/mirror").json()["connected"] is True  # no body at all
    assert client.post(P + "/pairs/acme-helper/mirror", json={"off": True}).json()["connected"] is False
    assert client.get(P + "/pairs/acme-helper/digest").json()["enabled"] is False
    sw = client.post(P + "/pairs/acme-helper/switch", json={"job_ids": ["demo-acme-helper-job-1"]}).json()
    assert sw["resumed"] == ["demo-acme-helper-job-1"]
    assert client.post(P + "/pairs/recipe-planner/remove", json={"dry_run": True}).json()["profile_kept"] is True
    assert client.post(P + "/pairs/recipe-planner/remove", json={}).json()["dry_run"] is False
    assert client.get(P + "/pairs/recipe-planner/recent").status_code == 404
    ev = client.get(P + "/events?since=2000-01-01T00:00:00Z").json()["events"]
    assert {"synced", "conflict", "changed"} & {e["type"] for e in ev} or ev
    checks = client.get(P + "/doctor").json()["checks"]
    assert any(c["id"] == "data_dir" and c["ok"] for c in checks)


def test_errors_404_400_200(client):
    client.post(P + "/demo", json={"action": "start"})
    assert client.get(P + "/pairs/nobody/conflicts").status_code == 404
    assert client.get(P + "/pairs/bad..id%2F/conflicts").status_code in (400, 404)
    assert client.get(P + "/pairs/" + "x" * 80 + "/recent").status_code == 400
    assert client.post(P + "/demo", json={"action": "explode"}).status_code == 400
    assert client.post(P + "/demo", json=["start"]).status_code == 400
    assert client.post(P + "/pairs/acme-helper/resolve", json={"conflict_id": "x", "keep": "both"}).status_code == 400
    assert client.post(P + "/pairs/acme-helper/restore", json={"point_id": "../../x"}).status_code == 400
    assert client.post(P + "/pairs/acme-helper/mirror", json={"off": "yes"}).status_code == 400
    assert client.post(P + "/pairs/acme-helper/remove", json={"dry_run": 1}).status_code == 400
    assert client.post(P + "/twins/create", json={"grok_ids": "b-acme-0001"}).status_code == 400
    assert client.post(P + "/sync", json={"pair_id": "../x"}).status_code == 400
    assert client.get(P + "/events?since=yesterday").status_code == 400
    assert client.post(P + "/pairs/acme-helper/digest", json={"enabled": True}).status_code == 400
    # an engine-level problem is HTTP 200 with an error object
    r = client.post(P + "/pairs/acme-helper/resolve", json={"conflict_id": "skill:none", "keep": "hermes"})
    assert r.status_code == 200 and r.json()["error"]["code"] == "usage"
    r = client.post(P + "/pairs/acme-helper/approve", json={"item": "skill:none"})
    assert r.status_code == 200 and "error" in r.json()


def test_secret_absent_from_rest(client):
    key = fake_key()
    client.post(P + "/demo", json={"action": "start"})
    client.post(P + "/demo", json={"action": "secret"})
    bodies = [client.post(P + "/sync", json={}).text, client.get(P + "/status").text,
              client.get(P + "/events").text, client.get(P + "/grok/bots").text,
              client.post(P + "/twins/preview", json={"grok_ids": ["b-acme-0001"]}).text,
              client.get(P + "/pairs/acme-helper/conflicts").text, client.get(P + "/pairs/acme-helper/recent").text,
              client.get(P + "/pairs/acme-helper/restore-points").text]
    assert "suspected secret held back" in bodies[1]
    for b in bodies:
        assert key not in b
