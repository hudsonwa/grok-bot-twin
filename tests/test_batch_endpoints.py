"""The batch model picker lists model servers already set up in Hermes first (twin/endpoints.py). No real network:
the probe is replaced, except for one tiny server on 127.0.0.1."""
from __future__ import annotations

import http.server
import json
import threading

import pytest

from twin_testlib import central, config, home, service  # noqa: F401
from test_ux_flows import _client, _pair, fake  # noqa: F401
from test_setup_download_and_twin_facts import SOUL, profile, twin  # noqa: F401
from test_batch import world, TWINS, P  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import endpoints, yamlio
from hermes_plugins.grok_bot_twin.twin.connect import BadInput

CFG = """model:
  default: grok-4.7
  provider: openrouter
custom_providers:
- name: Homebox
  base_url: http://127.0.0.1:8000/v1
  model: local-model
  key_env: HOMEBOX_TOKEN_NAME_ONLY
providers:
  farbox:
    base_url: https://models.example.invalid:9443/v1?x=1
"""


@pytest.fixture
def probed(home, monkeypatch):
    asked = []

    def probe(url):
        asked.append(url)
        return ["local-model", "local-model-mini"] if "127.0.0.1" in url else None

    monkeypatch.setattr(endpoints, "PROBE", probe)
    endpoints._cache.clear()
    (home / "config.yaml").write_text(CFG, encoding="utf-8")
    return asked


def test_configured_endpoints_listed_with_host_port_only(probed, home):
    eps = endpoints.public(endpoints.endpoints())
    assert [e["name"] for e in eps] == ["Homebox", "farbox"]
    homebox, far = eps
    assert homebox == {"name": "Homebox", "base_url_label": "127.0.0.1:8000", "local": True, "reachable": True,
                     "models": ["local-model", "local-model-mini"], "provider": "custom:homebox"}
    assert far["base_url_label"] == "models.example.invalid:9443" and far["local"] is False
    assert far["reachable"] is False and far["models"] == []
    assert "HOMEBOX_TOKEN" not in json.dumps(eps) and "/v1" not in json.dumps(eps)


def test_is_local():
    for u in ("http://localhost:1/v1", "http://10.0.0.5/v1", "http://192.168.1.9:8080", "http://100.64.0.10:1",
              "http://homebox.local:8000", "http://[::1]:9/v1"):
        assert endpoints.is_local(u), u
    assert not endpoints.is_local("https://api.example.invalid/v1")
    assert endpoints.host_label("http://user:pw@h:1/v1") == ""


def test_unreachable_keeps_configured_model(home, monkeypatch):
    monkeypatch.setattr(endpoints, "PROBE", lambda url: None)
    endpoints._cache.clear()
    (home / "config.yaml").write_text(CFG, encoding="utf-8")
    homebox = endpoints.endpoints()[0]
    assert homebox["reachable"] is False and homebox["models"] == ["local-model"]


def test_batch_info_and_set_model_with_endpoint(world, home, probed):
    info = world.batch_info()
    assert info["endpoints"][0]["name"] == "Homebox" and "models" in info and "default_model" in info
    pid = TWINS[0][0]
    res = world.set_model(pid, "local-model-mini", endpoint="Homebox")
    assert res["state"] == "set"
    doc = yamlio.module().safe_load((home / "profiles" / pid / "config.yaml").read_text())
    assert doc["model"] == {"default": "local-model-mini", "provider": "custom", "base_url": "http://127.0.0.1:8000/v1"}
    assert world.set_model(pid, "local-model-mini", endpoint="Homebox")["state"] == "same"
    with pytest.raises(BadInput):
        world.set_model(pid, "local-model", endpoint="http://evil.invalid/v1")
    with pytest.raises(BadInput):
        world.set_model(pid, "not-listed", endpoint="Homebox")


def test_batch_route_takes_endpoint(world, home, probed, monkeypatch):
    c = _client(monkeypatch)
    pids = [t[0] for t in TWINS[:2]]
    r = c.post(f"{P}/batch/model", json={"pair_ids": pids, "model": "local-model", "endpoint": "Homebox"})
    assert r.status_code == 200, r.text
    assert {x["state"] for x in r.json()["results"]} == {"set"}
    doc = yamlio.module().safe_load((home / "profiles" / pids[1] / "config.yaml").read_text())
    assert doc["model"]["provider"] == "custom"
    r = c.post(f"{P}/batch/model", json={"pair_ids": pids, "model": "local-model", "endpoint": "Nope"})
    assert r.json()["error"]["code"] == "bad_input"
    r = c.post(f"{P}/pairs/{pids[0]}/model", json={"model": "local-model-mini", "endpoint": "Homebox"})
    assert r.json()["state"] == "set"


def test_real_probe_on_loopback_sends_no_key():
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"], seen["auth"] = self.path, self.headers.get("Authorization")
            body = json.dumps({"data": [{"id": "local-model"}, {"id": "bad id with spaces"}, {"id": "local-model-2"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        ids = endpoints.probe(f"http://127.0.0.1:{srv.server_address[1]}/v1")
    finally:
        srv.shutdown()
        srv.server_close()
    assert ids == ["local-model", "local-model-2"] and seen == {"path": "/v1/models", "auth": None}
    assert endpoints.probe("http://127.0.0.1:1/v1") is None
