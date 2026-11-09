"""TwinService + the REST router end to end against a REAL botport build, the way the demo video runs in real mode.

Set BOTPORT_TEST_BIN to a botport binary (for example a local `cargo build` of botport); skipped otherwise.

The "Grok app" is botport's own fake one: `botport twin demo` in a separate fixture folder writes
`<fixture state>/demo-grok-app` (invented bots Acme Helper and Recipe Planner), and its `--step rename|secret|reset`
rewrite only that fake cache. The plugin is pointed at it with the `grok_app_dir` setting and twins the bots itself
(grok_list -> preview -> bulk create) in its own state folder. A conflict is made the way it happens on a real pair: the
same item (the description) changed in Hermes (SOUL.md) and in Grok (the rename step) before a sync.

Everything lives under tmp_path: HOME, XDG_*, HERMES_HOME, HERMES_GATEWAY_LOCK_DIR, the fixture world and a private
copy of the binary; PATH is the system folders only, so nothing can reach a real Hermes install or Grok app.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from twin_testlib import ROOT, central, config, dumps, home, service, timer, tools  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import engine as E  # noqa: E402

BIN = os.environ.get("BOTPORT_TEST_BIN", "")
pytestmark = pytest.mark.skipif(not (BIN and os.path.isfile(BIN) and os.access(BIN, os.X_OK)),
                                reason="set BOTPORT_TEST_BIN to a botport build to run these")
# botport's demo key, split so secret scanners of this file do not flag it
FAKE_KEY_TAIL = "0000NOTAREAL" + "KEY0000"
P = "/api/plugins/grok-bot-twin"
ACME, RECIPE = "b-acme-0001", "b-recipe-0002"


class World:
    """The fake Grok side: botport's demo world in its own folders (never the plugin's)."""

    def __init__(self, exe: Path, base: Path, env: dict) -> None:
        self.exe, self.base, self.env = exe, base, env
        self.app = base / "state" / "demo-grok-app"

    def run(self, *args: str) -> dict:
        argv = [str(self.exe), *args, "--json", f"--hermes-home={self.base / 'home'}",
                f"--state-dir={self.base / 'state'}"]
        cp = subprocess.run(argv, env=self.env, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        env = json.loads(cp.stdout.decode().strip().splitlines()[-1])
        assert env["ok"] is True, env
        return env["data"]

    def step(self, name: str) -> None:
        self.run("twin", "demo", f"--step={name}")


@pytest.fixture
def rig(home, tmp_path, monkeypatch):
    # full sandbox: HOME and every XDG folder under tmp_path as well (the `home` fixture set HERMES_HOME, the gateway
    # lock dir and PATH)
    os.chmod(tmp_path, 0o700)
    sand = tmp_path / "user-home"
    sand.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(sand))
    for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(k, str(tmp_path / "xdg" / k.lower()))
    (tmp_path / "gw-lock").mkdir(exist_ok=True)
    # a private copy of the binary with safe permissions: 0755, ours, no group/world-writable folder above it
    bindir = tmp_path / "bin"
    bindir.mkdir(mode=0o755)
    exe = bindir / "botport"
    shutil.copyfile(os.path.realpath(BIN), exe)
    exe.chmod(0o755)
    assert E.check_path_safety(str(exe.resolve())) is None, E.check_path_safety(str(exe.resolve()))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(sand), "LANG": "C.UTF-8", "NO_COLOR": "1",
           "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "gw-lock")}
    for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        env[k] = str(tmp_path / "fixture-xdg" / k.lower())
    world = World(exe, tmp_path / "grok-fixture", env)
    world.run("twin", "demo")
    assert world.app.is_dir()
    raw: list[bytes] = []
    orig = E.run_capped

    def capture(argv, timeout, env=None, out_cap=E.STDOUT_CAP):
        rc, out, err, over = orig(argv, timeout, env=env, out_cap=out_cap)
        raw.append(out + err)
        return rc, out, err, over

    monkeypatch.setattr(E, "run_capped", capture)
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    cfg = {"botport_path": str(exe), "grok_app_dir": str(world.app), "bots": ["acme*"]}
    config.set_override(cfg)
    service.reset_service()
    yield {"svc": service.get_service(), "world": world, "cfg": cfg, "raw": raw, "home": home, "tmp": tmp_path}


def _client():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_real", ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = fastapi.FastAPI()
    app.include_router(mod.router, prefix=P)
    return TestClient(app)


def _events(svc, etype):
    return [e for e in svc.events()["events"] if e["type"] == etype]


def _pair(svc, pid):
    return next(p for p in svc.status(fresh=True)["pairs"] if p["pair_id"] == pid)


def _soul(home: Path, profile: str) -> str:
    return (home / "profiles" / profile / "SOUL.md").read_text(encoding="utf-8")


def _files_with(root: Path, needle: str) -> list[str]:
    """Files under root holding needle, except the private copies of the botport binary itself (botport-bin/: the
    demo key's text is a constant inside the program)."""
    hits = []
    for f in sorted(root.rglob("*")):
        if f.is_file() and not f.is_symlink() and "botport-bin" not in f.relative_to(root).parts:
            if needle.encode() in f.read_bytes():
                hits.append(str(f.relative_to(root)))
    return hits


def _timed(label: str, fn, *a):
    t0 = time.monotonic()
    out = fn(*a)
    print(f"[wall] {label}: {(time.monotonic() - t0) * 1000:.0f} ms")
    return out


def test_real_mode_end_to_end(rig):
    svc, world, home, raw = rig["svc"], rig["world"], rig["home"], rig["raw"]
    api = _client()
    rest: list[str] = []

    def get(path):
        r = api.get(P + path)
        assert r.status_code == 200, (path, r.text)
        rest.append(r.text)
        return r.json()

    def post(path, body=None):
        r = api.post(P + path, json=body if body is not None else {})
        assert r.status_code == 200, (path, r.text)
        rest.append(r.text)
        return r.json()

    # 1. trust the private copy with the explicit local-build opt-out; status and doctor show the warning
    before = svc.engine_info()
    assert before["available"] is False and before["trusted"] is False
    t = post("/engine/trust", {"allow_unattested": True})
    assert t["trusted"] is True and t["attested"] is False and t["allow_unattested"] is True
    assert "WITHOUT attestation" in t["warning"] and len(t["sha256"]) == 64
    st = get("/status")
    assert st["engine"]["available"] is True and st["engine"]["trusted"] is True
    assert "WITHOUT attestation" in st["engine"]["trust_warning"]
    assert st["mode"] == "botport" and st["pairs"] == []
    assert "WITHOUT attestation" in get("/engine/trust")["warning"]

    # 2. the bots filter: only matching invented bots; then both, preview (masked), bulk create
    bots = get("/grok/bots")
    assert bots["app_found"] is True and [b["name"] for b in bots["bots"]] == ["Acme Helper"]
    assert bots["bots"][0]["routines"] == ["Morning summary"] and bots["bots"][0]["demo"] is False
    config.set_override({**rig["cfg"], "bots": ["acme*", "RECIPE*"]})
    bots = get("/grok/bots")
    assert sorted(b["grok_id"] for b in bots["bots"]) == [ACME, RECIPE]
    assert all(b["twinned_pair_id"] is None for b in bots["bots"])

    # a preview while the Grok description holds a (fake) key: the item is listed as held back, its text masked
    world.step("secret")
    masked = post("/twins/preview", {"grok_ids": [ACME]})["pairs"][0]
    assert [w["item"] for w in masked["secrets_withheld"]] == ["persona.description"]
    assert "not a guarantee" in masked["secrets_withheld"][0]["location"]
    # held back whole: the description is not among the items the twin would get at all
    assert [i["item"] for i in masked["items"] if i["kind"] in ("name", "description")] == ["persona.name"]
    assert FAKE_KEY_TAIL not in rest[-1]
    world.step("reset")

    prev = post("/twins/preview", {"grok_ids": [ACME, RECIPE]})
    assert [p["grok"]["id"] for p in prev["pairs"]] == [ACME, RECIPE]
    acme = prev["pairs"][0]
    assert acme["hermes"]["profile"] == "acme-helper" and acme["demo"] is False and acme["secrets_withheld"] == []
    kinds = {i["item"]: i for i in acme["items"]}
    assert kinds["persona.name"]["action"] == "create" and kinds["routine:Morning summary"]["action"] == "by_hand"
    # the Desktop shows the masked preview text of each item
    assert kinds["persona.description"]["preview"].startswith("Answers questions about Acme widgets.")
    assert prev.get("skipped") == []
    assert not (home / "profiles" / "acme-helper").exists()  # a preview writes nothing

    made = _timed("create x2", post, "/twins/create", {"grok_ids": [ACME, RECIPE]})
    assert sorted(p["pair_id"] for p in made["pairs"]) == ["acme-helper", "recipe-planner"]
    assert all(p["state"] == "in_step" and p["demo"] is False for p in made["pairs"])
    assert {e["pair_id"] for e in _events(svc, "created")} == {"acme-helper", "recipe-planner"}
    assert "Acme Helper" in _soul(home, "acme-helper")
    reg = json.loads((central(home) / "pairs.json").read_text())
    assert sorted(p["pair_id"] for p in reg["pairs"]) == ["acme-helper", "recipe-planner"]
    again = post("/twins/preview", {"grok_ids": [ACME]})
    assert again["pairs"] == [] and again["skipped"][0]["reason"] == "already_twinned"
    assert {b["grok_id"]: b["twinned_pair_id"] for b in get("/grok/bots")["bots"]} == \
        {ACME: "acme-helper", RECIPE: "recipe-planner"}
    svc.check_changed()  # settle: a first check counts every twin as changed

    # 3. a Grok-side rename + description edit -> the timer's tick syncs it
    assert svc.check_changed() == []
    world.step("rename")
    n = _timed("tick (rename)", svc.tick)
    assert n >= 1
    synced = [e for e in _events(svc, "synced") if e["pair_id"] == "acme-helper"]
    assert synced and synced[-1]["payload"]["message"] == "Acme Helper: name & description updated from Grok", synced
    p = _pair(svc, "acme-helper")
    assert p["state"] == "in_step" and p["grok"]["name"] == "Acme Support Desk"
    assert "spare parts" in _soul(home, "acme-helper")
    # ENGINE MISMATCH (reported, not hidden): TWIN.md says "a command that wrote settles the signatures, so its own
    # writes do not count", but `twin sync --pair` settles them before it writes the profile, so the next check lists
    # the synced twin again once. That extra sync must change nothing; the check after it is quiet.
    n_synced = len(_events(svc, "synced"))
    assert _timed("tick (after own write)", svc.tick) in (0, 1)
    assert len(_events(svc, "synced")) == n_synced
    assert _timed("tick (nothing changed)", svc.tick) == 0

    # 4. a fake key in the Grok description: held back whole, never anywhere on the plugin side
    world.step("secret")
    _timed("tick (secret)", svc.tick)
    held = [e for e in _events(svc, "secrets_withheld") if e["pair_id"] == "acme-helper"]
    assert held and held[-1]["payload"]["message"] == "1 suspected secret held back"
    assert held[-1]["payload"]["items"] == ["persona.description"]
    s = _timed("sync (secret held)", post, "/sync", {"pair_id": "acme-helper"})
    assert s["pairs"][0]["secrets_withheld"][0]["item"] == "persona.description"
    p = _pair(svc, "acme-helper")
    assert p["state"] == "needs_you"
    tool_out = tools.twin_status({}) + tools.twin_recent({"pair": "acme-helper"})
    assert "error" not in json.loads(tools.twin_status({}))
    assert _files_with(home, FAKE_KEY_TAIL) == []  # the plugin central dir, botport's state, every profile
    for where, text in (("events", dumps(svc.events())), ("rest", "\n".join(rest)), ("tools", tool_out)):
        assert FAKE_KEY_TAIL not in text, where
    assert not any(FAKE_KEY_TAIL.encode() in r for r in raw)
    assert "Answers questions about Acme widgets and spare parts." in _soul(home, "acme-helper")

    # back to the plain description
    world.step("reset")
    svc.tick()
    assert "Answers questions about Acme widgets." in _soul(home, "acme-helper")

    # 5. the same item changed on both sides: a conflict -> keep Grok -> restore points -> restore
    soul = home / "profiles" / "acme-helper" / "SOUL.md"
    soul.write_text(_soul(home, "acme-helper").replace("Answers questions about Acme widgets.",
                                                       "Answers questions about Acme widgets, politely."))
    world.step("rename")
    s = _timed("sync --all (conflict)", post, "/sync", {})
    acme_s = next(x for x in s["pairs"] if x["pair_id"] == "acme-helper")
    assert acme_s["conflicts"] == ["persona.description"] and acme_s["state"] == "needs_you"
    assert [e for e in _events(svc, "conflict") if e["pair_id"] == "acme-helper"]
    p = _pair(svc, "acme-helper")
    assert p["state"] == "needs_you" and p["conflicts"] == 1
    cs = get("/pairs/acme-helper/conflicts")["conflicts"]
    assert len(cs) == 1
    c = cs[0]
    assert set(c) == {"conflict_id", "kind", "name", "since", "hermes", "grok", "diff", "text_truncated"}
    assert c["conflict_id"] == "persona.description" and c["kind"] == "description"
    assert "politely" in c["hermes"]["text"] and "spare parts" in c["grok"]["text"]
    assert c["diff"].startswith("--- grok\n+++ hermes")
    r = post("/pairs/acme-helper/resolve", {"conflict_id": c["conflict_id"], "keep": "grok"})
    assert r["applied"] == [{"item": "persona.description", "keep": "grok"}]
    assert get("/pairs/acme-helper/conflicts")["conflicts"] == []
    assert "spare parts" in _soul(home, "acme-helper") and "politely" not in _soul(home, "acme-helper")
    assert _pair(svc, "acme-helper")["state"] == "in_step"

    pts = get("/pairs/acme-helper/restore-points")["points"]
    desc = [x for x in pts if x["item"] == "persona.description"]
    assert desc and all("@" in x["id"] and x["scope"] == "item" for x in pts)
    # botport v0.2.0 keeps the Hermes copy it overwrote (resolve --keep grok) as an item restore point, so a
    # restore brings the owner's Hermes edit back.
    back = None
    for pt in desc:
        rs = post("/pairs/acme-helper/restore", {"point_id": pt["id"]})
        assert rs["restored"] in (["persona.description"], []), rs
        if "politely" in _soul(home, "acme-helper"):
            back = pt
            break
    assert back is not None, desc
    after = _soul(home, "acme-helper")
    assert "politely" in after and "spare parts" not in after
    assert [e for e in _events(svc, "restored") if e["pair_id"] == "acme-helper"]

    # 6. parity, checklist, recent, doctor, remove (profile kept)
    par = get("/pairs/acme-helper/parity")
    assert par["total"] and par["total"] >= par["passed"] and par["checks"]
    assert all(set(x) == {"id", "name", "pass", "detail"} for x in par["checks"])
    assert any(x["id"] == "routine:Morning summary" and x["pass"] is False for x in par["checks"])
    cl = get("/pairs/acme-helper/checklist")
    assert {"name": "Morning summary"} in cl["switch"]["grok_routines_to_turn_off"]
    assert any(b["item"] == "routine:Morning summary" for b in cl["by_hand"])
    rec = get("/pairs/acme-helper/recent?limit=5")["entries"]
    assert 0 < len(rec) <= 5 and all(e["side"] in ("grok", "hermes", "both") for e in rec)
    recent_tool = json.loads(tools.twin_recent({"pair": "acme-helper"}))
    assert recent_tool["entries"] and "Acme" not in json.dumps(recent_tool)  # names stay out of the model's view
    doc = get("/doctor")
    ids = [x["id"] for x in doc["checks"]]
    assert {"botport", "botport_trust", "botport_attestation", "state_dir", "data_dir"} <= set(ids)
    chk = {x["id"]: x for x in doc["checks"]}
    assert chk["botport_trust"]["ok"] is True and "WITHOUT attestation" in chk["botport_attestation"]["message"]

    sent = post("/pairs/acme-helper/send-to-grok")
    assert sent["pairs"][0]["pair_id"] == "acme-helper" and sent["note"] and sent["skills"] == 0

    dry = post("/pairs/recipe-planner/remove", {"dry_run": True})
    assert dry["dry_run"] is True and dry["profile_kept"] is True and dry["removed"]
    assert all(not x.startswith("/") for x in dry["removed"])
    gone = post("/pairs/recipe-planner/remove", {})
    assert gone["dry_run"] is False and gone["profile_kept"] is True
    assert (home / "profiles" / "recipe-planner" / "SOUL.md").is_file()  # the profile stays
    assert [p["pair_id"] for p in get("/status")["pairs"]] == ["acme-helper"]
    assert api.get(P + "/pairs/recipe-planner/conflicts").status_code == 404
    reg = json.loads((central(home) / "pairs.json").read_text())
    assert [p["pair_id"] for p in reg["pairs"]] == ["acme-helper"]

    # nothing outgoing carries the fake key or an absolute path of this machine's temp folder
    everything = "\n".join(rest) + dumps(svc.events()) + tools.twin_status({})
    assert FAKE_KEY_TAIL not in everything
    assert str(rig["tmp"]) not in "\n".join(rest).replace("\\/", "/")
