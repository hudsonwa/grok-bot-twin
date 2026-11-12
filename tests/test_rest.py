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
    # the hint is plain words with no placeholder and no command to decode; the page gets three separate facts
    hint = body["engine"]["install_hint"]
    assert body["engine"]["available"] is False and "Connect my Grok bots" in hint
    assert "<path>" not in hint and "attestation" not in hint and "PATH" not in hint
    assert body["setup"] == {"ready": False, "grok_app_found": True, "helper_found": False, "helper_checked": False,
                             "problem": None, "cause": None}
    assert body["sample"] == {"on": False, "hide_real": False, "instructions_example": None}
    assert body["has_data"] is False
    r = client.post(P + "/demo", json={"action": "start"})
    assert r.status_code == 200 and r.json()["pairs"][0]["state"] == "in_step"
    assert r.json()["created"] is True and r.json()["did"] == "Sample twin ready"
    st = client.get(P + "/status").json()
    assert st["mode"] == "demo" and st["sample"]["on"] is True and st["sample"]["hide_real"] is False
    assert st["sample"]["instructions_example"].startswith("You are Acme Support.") and st["has_data"] is True
    assert st["pairs"][0]["sample"] is True and st["pairs"][0]["state_reason"] == "Up to date"


def test_full_demo_tour(client):
    client.post(P + "/demo", json={"action": "start"})
    client.post(P + "/demo", json={"action": "grok-edit"})
    r = client.post(P + "/sync", json={"pair_id": "acme-support"}).json()
    assert r["pairs"][0]["applied"][0]["kind"] == "description"
    client.post(P + "/demo", json={"action": "conflict"})
    client.post(P + "/sync", json={})
    cs = client.get(P + "/pairs/acme-support/conflicts").json()["conflicts"]
    assert cs[0]["conflict_id"] == "skill:greet-visitors"
    r = client.post(P + "/pairs/acme-support/resolve", json={"conflict_id": cs[0]["conflict_id"], "keep": "grok"})
    assert r.json()["applied"][0]["keep"] == "grok"
    pts = client.get(P + "/pairs/acme-support/restore-points").json()["points"]
    assert pts[0]["id"].startswith("skill:greet-visitors@") and pts[0]["scope"] == "item"
    r = client.post(P + "/pairs/acme-support/restore", json={"point_id": pts[0]["id"]})
    assert r.status_code == 200 and r.json()["restored"] == ["skill:greet-visitors"]
    assert client.get(P + "/pairs/acme-support/recent?limit=5").json()["entries"]
    assert client.get(P + "/pairs/acme-support/parity").json()["total"] > 0
    assert "switch" in client.get(P + "/pairs/acme-support/checklist").json()
    bots = client.get(P + "/grok/bots").json()
    # sample bots are listed apart and never pass as the owner's own; no helper here, so no real list
    assert bots["bots"] == [] and bots["ready"] is False and bots["real_hidden"] is False
    assert [b["name"] for b in bots["sample_bots"]] == ["Acme Support", "Recipe Planner"]
    pv = client.post(P + "/twins/preview", json={"grok_ids": ["b-recipe-0002"]}).json()
    assert pv["pairs"][0]["grok"]["name"] == "Recipe Planner" and pv["pairs"][0]["sample"] is True
    assert pv["pairs"][0]["lines"][0] == "Copies now: name and description."
    mk = client.post(P + "/twins/create", json={"grok_ids": ["b-recipe-0002"]}).json()
    assert mk["pairs"][0]["pair_id"] == "recipe-planner"
    client.post(P + "/demo", json={"action": "hermes-edit"})
    r = client.post(P + "/pairs/acme-support/approve", json={"item": "skill:fun-fact"}).json()
    assert r["approved"][0]["item"] == "skill:fun-fact"
    assert client.post(P + "/pairs/acme-support/send-to-grok").json()["skills"] == 1
    on = client.post(P + "/pairs/acme-support/mirror", json={}).json()  # demo: no pair folder needed
    assert on["connected"] is True and on["steps"] and "Sync bot instructions" in on["charter"]
    assert "helper bot" not in str(on).lower() and "key file" not in str(on["steps"]).lower()
    assert on["reveal"] == {"key_file": None, "guide_file": None}  # the sample has no pass
    st = client.get(P + "/status").json()
    assert next(p for p in st["pairs"] if p["pair_id"] == "acme-support")["mirror_bot"] == {"connected": True, "every_minutes": None}
    assert client.post(P + "/pairs/acme-support/mirror").json()["connected"] is True  # no body at all
    assert client.post(P + "/pairs/acme-support/mirror", json={"off": True}).json()["connected"] is False
    assert client.get(P + "/pairs/acme-support/digest").json()["enabled"] is False
    sw = client.post(P + "/pairs/acme-support/switch", json={"job_ids": ["demo-acme-support-job-1"]}).json()
    assert sw["resumed"] == ["demo-acme-support-job-1"]
    acme = next(p for p in client.get(P + "/status").json()["pairs"] if p["pair_id"] == "acme-support")
    assert acme["switched"]["routines_on"] == 1
    un = client.post(P + "/pairs/acme-support/switch", json={"undo": True}).json()
    assert un == {"paused": ["demo-acme-support-job-1"], "failed": []}
    plan = client.post(P + "/pairs/recipe-planner/remove", json={"dry_run": True}).json()
    # a sample twin never had a Hermes profile of its own: no "profile kept" promise
    assert plan["sample"] is True and plan["profile_kept"] is False and plan["stays"] and plan["goes"]
    assert client.post(P + "/pairs/recipe-planner/remove", json={}).json()["dry_run"] is False
    gone = client.get(P + "/pairs/recipe-planner/recent")
    assert gone.status_code == 200 and gone.json()["error"]["code"] == "pair_not_found"
    ev = client.get(P + "/events?since=2000-01-01T00:00:00Z").json()["events"]
    assert {"synced", "conflict", "changed"} & {e["type"] for e in ev} or ev
    checks = client.get(P + "/doctor").json()["checks"]
    assert any(c["id"] == "data_dir" and c["ok"] for c in checks)


def _error(r):
    """A problem the service reports: HTTP 200 with a plain {error} (one failure path for the page)."""
    assert r.status_code == 200, r.text
    err = r.json()["error"]
    # plain words: no field names, no class names, no transport text
    assert err["message"] and not any(w in err["message"] for w in ("_", "Error", "Exception", "{", "must match"))
    return err


def test_errors_are_plain_and_nothing_happens(client):
    client.post(P + "/demo", json={"action": "start"})
    before = client.get(P + "/status").json()["pairs"]
    # an unknown twin and bad input are answered in plain words (HTTP 200 {error}); nothing is done
    assert _error(client.get(P + "/pairs/nobody/conflicts"))["code"] == "pair_not_found"
    assert _error(client.post(P + "/demo", json={"action": "explode"}))["code"] == "bad_input"
    r = client.post(P + "/pairs/acme-support/resolve", json={"conflict_id": "x", "keep": "both"})
    assert _error(r) == {"code": "bad_input", "message": "Choose the Hermes version or the Grok version."}
    assert _error(client.post(P + "/pairs/acme-support/restore", json={"point_id": "../../x"}))["code"] == "bad_input"
    assert _error(client.post(P + "/twins/create", json={"grok_ids": "b-acme-0001"}))["message"] == \
        "Choose at least one bot."
    assert _error(client.get(P + "/events?since=yesterday"))["code"] == "bad_input"
    r = client.post(P + "/pairs/acme-support/digest", json={"enabled": True})
    assert _error(r)["message"] == 'Turn on "Allow an activity digest to Grok" in the plugin\'s settings first.'
    assert _error(client.post(P + "/pairs/acme-support/skip", json={"item": "../x"}))["code"] == "bad_input"
    assert _error(client.post(P + "/pairs/acme-support/switch", json={"job_ids": "all"}))["code"] == "bad_input"
    assert _error(client.post(P + "/engine/path", json={"path": "relative/botport"}))["code"] == "bad_input"
    # a body or an id that is not even well formed is still refused at the door
    assert client.get(P + "/pairs/bad..id%2F/conflicts").status_code in (400, 404)
    assert client.get(P + "/pairs/" + "x" * 80 + "/recent").status_code == 400
    assert client.post(P + "/demo", json=["start"]).status_code == 400
    assert client.post(P + "/pairs/acme-support/mirror", json={"off": "yes"}).status_code == 400
    assert client.post(P + "/pairs/acme-support/remove", json={"dry_run": 1}).status_code == 400
    assert client.post(P + "/uninstall", json={"dry_run": "yes"}).status_code == 400
    # removing everything is never the answer to an empty or half-made request
    for body in (None, {}, {"dry_run": False}, {"confirm": True}, {"confirm": "yes"}, {"confirm": "Remove-Everything"}):
        r = client.post(P + "/uninstall", json=body) if body is not None else client.post(P + "/uninstall")
        assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_input", body
    assert client.post(P + "/sync", json={"pair_id": "../x"}).status_code == 400
    # an engine-level problem is HTTP 200 with an error object too
    r = client.post(P + "/pairs/acme-support/resolve", json={"conflict_id": "skill:none", "keep": "hermes"})
    assert r.status_code == 200 and r.json()["error"]["code"] == "usage"
    r = client.post(P + "/pairs/acme-support/approve", json={"item": "skill:none"})
    assert r.status_code == 200 and "error" in r.json()
    assert client.get(P + "/status").json()["pairs"] == before  # none of the refused calls changed anything


def test_internal_failure_is_plain(client, monkeypatch):
    from twin_testlib import service
    monkeypatch.setattr(service.TwinService, "setup_state", lambda self: {}["boom"])
    r = client.get(P + "/setup")
    assert r.status_code == 200
    assert r.json() == {"error": {"code": "internal",
                                  "message": "Something went wrong inside Grok Bot Twin. Try again."}}


def test_secret_absent_from_rest(client):
    key = fake_key()
    client.post(P + "/demo", json={"action": "start"})
    client.post(P + "/demo", json={"action": "secret"})
    bodies = [client.post(P + "/sync", json={}).text, client.get(P + "/status").text,
              client.get(P + "/events").text, client.get(P + "/grok/bots").text,
              client.post(P + "/twins/preview", json={"grok_ids": ["b-acme-0001"]}).text,
              client.get(P + "/pairs/acme-support/conflicts").text, client.get(P + "/pairs/acme-support/recent").text,
              client.get(P + "/pairs/acme-support/restore-points").text]
    assert "looks like a password or key was not copied" in bodies[1]
    for b in bodies:
        assert key not in b


def test_to_grok_changes_refused_inside_a_request_from_another_computer(client, monkeypatch):
    """The page's own route is the owner's, but never the chat turn of a request from another computer."""
    from hermes_plugins.grok_bot_twin.twin import tasks
    client.post(P + "/demo", json={"action": "start"})
    monkeypatch.setattr(tasks, "asked_turn", lambda *a, **k: True)
    for action, body in (("allow", {"items": "all"}), ("apply", {"change_id": "c-0123456789abcdef"}),
                         ("undo", {"change_id": "c-0123456789abcdef"}), ("resolve", {"item": "name", "keep": "hermes"}),
                         ("runner", {"routine": "morning-digest", "to": "grok"}), ("give-up", {})):
        r = client.post(P + f"/pairs/acme-support/to-grok/{action}", json=body)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "refused", action
    # stopping only ever takes changes back: it is accepted, also inside a request
    r = client.post(P + "/pairs/acme-support/to-grok/disallow", json={"items": "all"})
    assert r.status_code == 200 and r.json()["allowed"] == []
    monkeypatch.setattr(tasks, "asked_turn", lambda *a, **k: False)
    assert client.post(P + "/pairs/acme-support/to-grok/allow", json={"items": "all"}).json()["allowed"]
    assert client.post(P + "/to-grok/beta", json={"on": True}).status_code in (404, 405)  # no plugin-wide switch


def test_to_grok_apply_and_undo_take_only_the_exact_preview(client):
    client.post(P + "/demo", json={"action": "start"})
    info = client.post(P + "/pairs/acme-support/to-grok/allow", json={"items": ["name", "description"]}).json()
    assert info["allowed"] == ["name", "description"] and "beta" not in info and info["grant"]["items"]
    r = client.post(P + "/pairs/acme-support/to-grok/apply", json={"change_id": "c-0123456789abcdef"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stale"
    r = client.post(P + "/pairs/acme-support/to-grok/undo", json={"change_id": "c-0123456789abcdef"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "no_change"
    assert client.post(P + "/pairs/acme-support/to-grok/apply", json={"change_id": "nope"}).status_code == 400
    out = client.post(P + "/pairs/acme-support/to-grok/disallow", json={"items": "all"}).json()
    assert out["allowed"] == [] and out["revoke"]["forgot"] == 0 and "where" in out["revoke"]
