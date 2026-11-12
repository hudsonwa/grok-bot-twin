"""Contract test: the sample world, a real twin and the page harness's mock all answer in the same shapes.

Three sources feed the Twins page with the same routes:
  1. the built-in sample (the DemoEngine: invented bots, no helper needed),
  2. a real twin through the botport helper (tests/fake_botport.py here; a real build when BOTPORT_TEST_BIN is set),
  3. the mock answers in tests/desktop/check.mjs that the page's own assertion suite renders against.

`tests/contract/api_shapes.json` says what each answer must look like (required keys and their types, optional
keys). This file checks 1 and 2 against it and against each other; `tests/desktop/contract.mjs` checks 3 against
it (run from here when node is installed).

Set TWIN_CONTRACT_DUMP to a folder to also write every collected answer there as JSON (for reading a mismatch).
Everything runs in a throwaway HERMES_HOME; no real helper, Grok app or Hermes state is touched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from twin_testlib import ROOT, config, home, service, timer  # noqa: F401
import test_service_real_botport as real_rig
from test_service_real_botport import rig  # noqa: F401  (the real-helper sandbox, skipped without BOTPORT_TEST_BIN)

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

P = "/api/plugins/grok-bot-twin"
FAKE = Path(__file__).resolve().parent / "fake_botport.py"
SPEC_PATH = ROOT / "tests" / "contract" / "api_shapes.json"
SPEC = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
SHAPES: dict = SPEC["shapes"]
ACME, RECIPE = "b-acme-0001", "b-recipe-0002"
# line 2 looks like a password (built here so that no key-shaped text sits in the source)
PASTE_WITH_KEY = "You are an invented bot.\npassword: " + "tr0ub4dor" + "-and-" + "3xample" + "\nBe brief."

# an invented bot's setup export: instructions with one password-like line, one routine of each kind, a connector,
# and the two lists an import must leave out
SETUP_EXPORT = json.dumps({"format": "grok-bot-export", "version": 1, "bots": [{
    "name": "Acme Support", "instructions": PASTE_WITH_KEY, "memories": ["an invented memory"],
    "user_facts": ["an invented fact"], "skills": [],
    "routines": [{"name": "Morning summary", "instruction": "Summarise the invented tickets.", "trigger": "schedule",
                  "schedule_cron": "0 8 * * 1-5", "timezone": "UTC"},
                 {"name": "Ticket arrives", "instruction": "Triage it.", "trigger": "event"}],
    "apps": [{"name": "Widget Desk"}]}]})
# an export of an invented bot no twin is for (the several-twins sheet: matched to nothing, imported nowhere)
SCOUT_EXPORT = json.dumps({"format": "grok-bot-export", "version": 1, "bots": [{
    "name": "Test Scout", "instructions": "You are an invented scout.", "memories": [], "user_facts": []}]})

# ---- the spec language (the same few rules are implemented again in tests/desktop/contract.mjs) -----------------
# A type is: str | int | number | bool | null | dict | list | any, a shape name, list[TYPE], or a union "A|B".
_BASIC = {
    "str": lambda v: isinstance(v, str),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "dict": lambda v: isinstance(v, dict),
    "list": lambda v: isinstance(v, list),
    "any": lambda v: True,
}


def _check_type(value: Any, typ: str, path: str) -> list[str]:
    """Problems (empty when `value` fits `typ`)."""
    options = [t.strip() for t in typ.split("|")]
    found: list[list[str]] = []
    for t in options:
        if t in _BASIC:
            probs = [] if _BASIC[t](value) else [f"{path}: expected {t}, got {_kind(value)}"]
        elif t.startswith("list[") and t.endswith("]"):
            if not isinstance(value, list):
                probs = [f"{path}: expected {t}, got {_kind(value)}"]
            else:
                probs = [p for i, v in enumerate(value) for p in _check_type(v, t[5:-1], f"{path}[{i}]")]
        elif t in SHAPES:
            probs = check_shape(value, t, path)
        else:
            raise AssertionError(f"api_shapes.json names an unknown type {t!r} at {path}")
        if not probs:
            return []
        found.append(probs)
    # the option that came closest explains the miss best (a shape that matched all but one key, say)
    return min(found, key=len) if len(options) > 1 else found[0]


def check_shape(value: Any, name: str, path: str = "") -> list[str]:
    spec = SHAPES[name]
    path = path or name
    if not isinstance(value, dict):
        return [f"{path}: expected {name} (an object), got {_kind(value)}"]
    out: list[str] = []
    # `backend` keys are required of every backend answer (only the page's test mock may leave them out)
    required, optional = {**spec.get("required", {}), **spec.get("backend", {})}, spec.get("optional", {})
    for key, typ in required.items():
        if key not in value:
            out.append(f"{path}: missing key {key!r} ({name})")
        else:
            out += _check_type(value[key], typ, f"{path}.{key}")
    for key, typ in optional.items():
        if key in value:
            out += _check_type(value[key], typ, f"{path}.{key}")
    if spec.get("closed"):
        extra = sorted(set(value) - set(required) - set(optional))
        if extra:
            out.append(f"{path}: keys not in the contract ({name}): {extra}")
    return out


def _kind(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "str"
    return "list" if isinstance(v, list) else "dict" if isinstance(v, dict) else type(v).__name__


def diff_shapes(a: Any, b: Any, path: str, depth: int = 6) -> list[str]:
    """Where two answers differ in SHAPE (keys and value kinds; lists by their first element). Names say which
    side is which: `sample` is `a`, `real` is `b`."""
    ka, kb = _kind(a), _kind(b)
    if ka != kb:
        # null on one side is how an absent optional value travels; the spec says where null is allowed
        return [] if "null" in (ka, kb) else [f"{path}: type sample={ka} real={kb}"]
    if depth <= 0:
        return []
    if ka == "dict":
        out = [f"{path}.{k}: only in the sample answer" for k in sorted(set(a) - set(b))]
        out += [f"{path}.{k}: only in the real answer" for k in sorted(set(b) - set(a))]
        for k in sorted(set(a) & set(b)):
            out += diff_shapes(a[k], b[k], f"{path}.{k}", depth - 1)
        return out
    if ka == "list" and a and b:
        return diff_shapes(a[0], b[0], f"{path}[0]", depth - 1)
    return []


# ---- the two worlds ---------------------------------------------------------------------------------------------
def _client(monkeypatch) -> TestClient:
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_contract", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    app = FastAPI()
    app.include_router(api.router, prefix=P)
    return TestClient(app)


class Api:
    """Every call must answer HTTP 200 with a JSON object: that is the contract's one transport rule."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def get(self, path: str) -> dict:
        r = self.client.get(P + path)
        assert r.status_code == 200, (path, r.status_code, r.text[:300])
        return r.json()

    def post(self, path: str, body: Any = None) -> dict:
        r = self.client.post(P + path, json=body if body is not None else {})
        assert r.status_code == 200, (path, r.status_code, r.text[:300])
        return r.json()


def _pair_of(status: dict, pid: str) -> dict:
    return next(p for p in status["pairs"] if p["pair_id"] == pid)


def _per_twin(api: Api, pid: str, folder: Path | None) -> dict:
    """The answers of every per-twin route (reads, dry runs and the undoable "keep in Hermes only")."""
    out = {
        "conflicts": api.get(f"/pairs/{pid}/conflicts"),
        "checklist": api.get(f"/pairs/{pid}/checklist"),
        "parity": api.get(f"/pairs/{pid}/parity"),
        "recent": api.get(f"/pairs/{pid}/recent"),
        "restore_points": api.get(f"/pairs/{pid}/restore-points"),
        "remove_plan": api.post(f"/pairs/{pid}/remove", {"dry_run": True}),
        "skip": api.post(f"/pairs/{pid}/skip", {"item": "skill:fun-fact"}),
        "skip_undo": api.post(f"/pairs/{pid}/skip", {"item": "skill:fun-fact", "undo": True}),
        "sync": api.post("/sync", {"pair_id": pid}),
        "error_bad_item": api.post(f"/pairs/{pid}/skip", {"item": 7}),
        "error_bad_conflict": api.post(f"/pairs/{pid}/resolve", {"conflict_id": "skill:greet-visitors", "keep": "both"}),
        # the owner brings the instructions over: a line that looks like a password refuses the save and names
        # the line; the same text with that line accepted, and a clean text, are saved
        "error_secret_paste": api.post(f"/pairs/{pid}/instructions", {"text": PASTE_WITH_KEY}),
        "instructions_accepted": api.post(f"/pairs/{pid}/instructions", {"text": PASTE_WITH_KEY, "accept_lines": [2]}),
        "instructions": api.post(f"/pairs/{pid}/instructions", {"text": "You are an invented bot.\nBe brief."}),
        "restore_points_after_paste": api.get(f"/pairs/{pid}/restore-points"),
        # the setup export a bot wrote about itself: what the sheet shows first, what an import would do (a
        # preview changes nothing, in both worlds), and a file that is not one
        "setup_export_info": api.get(f"/pairs/{pid}/setup-export"),
        "setup_export": api.post(f"/pairs/{pid}/setup-export", {"text": SETUP_EXPORT}),
        "error_not_an_export": api.post(f"/pairs/{pid}/setup-export", {"text": "hello"}),
        # several twins at once: the sheet, a model for the ticked twins, files matched and imported, Downloads
        "batch_info": api.get(f"/batch?pair_ids={pid}"),
        "batch_match": api.post("/batch/match", {"files": [{"name": "acme.json", "text": SETUP_EXPORT},
                                                           {"name": "notes.txt", "text": "hello"}],
                                                 "pair_ids": [pid]}),
        "batch_import": api.post("/batch/import", {"files": [{"name": "scout.json", "text": SCOUT_EXPORT}],
                                                   "pair_ids": [pid]}),
        "batch_downloads": api.post("/batch/downloads", {"since": 0, "pair_ids": [pid]}),
        "batch_models": api.post("/batch/model", {"pair_ids": [pid], "model": "test-model-1"}),
        "set_model": api.post(f"/pairs/{pid}/model", {"model": "test-model-2"}),
        "error_bad_model": api.post(f"/pairs/{pid}/model", {"model": "not a model"}),
    }
    # a skill changed in Hermes and waiting to go to Grok: its text, for the owner to paste there by hand
    waiting = [w["item"] for w in _pair_of(api.get("/status"), pid)["waiting"] if w["item"].startswith("skill:")]
    if waiting:
        out["skill_text"] = api.get(f"/pairs/{pid}/skill-text?item={waiting[0]}")
    out["error_no_such_skill"] = api.get(f"/pairs/{pid}/skill-text?item=skill:no-such-skill")
    return out


def _skill_sync(api: Api, pid: str, folder: Path | None) -> dict:
    """Skill sync on (a real twin needs a shared folder; the sample has no helper bot, so none) and off again.
    Asked last: a real helper reads the Grok side from that folder once it is connected."""
    return {"skill_sync": api.post(f"/pairs/{pid}/skill-sync", {"folder": str(folder)} if folder else {}),
            "skill_sync_status": api.get(f"/pairs/{pid}/skill-sync"),
            "mirror": api.post(f"/pairs/{pid}/mirror", {"pair_folder": str(folder)} if folder else {}),
            "mirror_off": api.post(f"/pairs/{pid}/mirror", {"off": True})}


def _writes(api: Api, pid: str, before: dict, calm_pid: str | None = None) -> dict:
    """The answers of the routes that change something, called last (each is undoable or harmless on invented
    data): choose a version, put an earlier version back, approve, send to Grok, move to Hermes and its undo."""
    out: dict[str, Any] = {}
    waiting = before["pair"]["waiting"]
    if waiting:
        out["approve"] = api.post(f"/pairs/{pid}/approve", {"item": waiting[0]["item"]})
    conflicts = before["conflicts"]["conflicts"]
    if conflicts:
        out["resolve"] = api.post(f"/pairs/{pid}/resolve", {"conflict_id": conflicts[0]["conflict_id"], "keep": "grok"})
    points = api.get(f"/pairs/{pid}/restore-points")["points"]
    if points:
        out["restore"] = api.post(f"/pairs/{pid}/restore", {"point_id": points[0]["id"]})
    out["send_to_grok"] = api.post(f"/pairs/{pid}/send-to-grok")
    # Move to Hermes is refused while a twin needs attention, so it is asked of a twin that is up to date
    calm = calm_pid or pid
    out["switch"] = api.post(f"/pairs/{calm}/switch", {"job_ids": []})
    out["switch_undo"] = api.post(f"/pairs/{calm}/switch", {"undo": True})
    return out


def collect_sample(api: Api) -> dict:
    """The sample world with its failure cases present: a possible password held back, an item changed in both
    places, and a change ready to send to Grok."""
    out: dict[str, Any] = {"demo_start": api.post("/demo", {"action": "start"})}
    out["status_calm"] = api.get("/status")
    pid = out["demo_start"]["pairs"][0]["pair_id"]
    out["grok_bots"] = api.get("/grok/bots")
    out["preview"] = api.post("/twins/preview", {"grok_ids": [RECIPE]})
    out["create"] = api.post("/twins/create", {"grok_ids": [RECIPE]})
    for action in ("grok-edit", "secret", "conflict", "hermes-edit"):
        out[f"demo_{action}"] = api.post("/demo", {"action": action})
        api.post("/sync", {"pair_id": pid})
    out["demo_hide_real"] = api.post("/demo", {"action": "hide-real"})
    out["demo_show_real"] = api.post("/demo", {"action": "show-real"})
    out["status"] = api.get("/status")
    out["pair"] = _pair_of(out["status"], pid)
    out["setup"] = api.get("/setup")
    out.update(_per_twin(api, pid, None))
    out["uninstall_plan"] = api.post("/uninstall", {"dry_run": True})
    out["error_unknown_twin"] = api.get("/pairs/no-such-twin/conflicts")
    out["error_bad_input"] = api.post("/twins/preview", {"grok_ids": "not-a-list"})
    out["error_bad_demo"] = api.post("/demo", {"action": "dance"})
    # this twin needs attention (a possible password, a choice to make): moving it to Hermes is refused in words
    out["error_blocked"] = api.post(f"/pairs/{pid}/switch", {"job_ids": []})
    out.update(_writes(api, pid, out, calm_pid=out["create"]["pairs"][0]["pair_id"]))
    out.update(_skill_sync(api, pid, None))
    out["events"] = api.get("/events")
    out["_pair_id"] = pid
    return out


def collect_real(api: Api, folder: Path, gid: str = ACME, steps: Any = None, unknown: Any = None) -> dict:
    """A real twin through the helper (already accepted for this throwaway home by the caller). `steps` (the real
    build only) makes the failure cases on the Grok side. `unknown` (the fake helper only) wraps the unknown-twin
    call: the fake answers for any id, where a real helper says it has no such twin."""
    out: dict[str, Any] = {"grok_bots": api.get("/grok/bots")}
    out["preview"] = api.post("/twins/preview", {"grok_ids": [gid]})
    out["create"] = api.post("/twins/create", {"grok_ids": [gid]})
    pid = out["create"]["pairs"][0]["pair_id"]
    if steps:
        steps(pid)
    out["status"] = api.get("/status")
    out["pair"] = _pair_of(out["status"], pid)
    out["setup"] = api.get("/setup")
    out.update(_per_twin(api, pid, folder))
    out["uninstall_plan"] = api.post("/uninstall", {"dry_run": True})
    ask = lambda: api.get("/pairs/no-such-twin/conflicts")  # noqa: E731
    out["error_unknown_twin"] = unknown(ask) if unknown else ask()
    out["error_bad_input"] = api.post("/twins/preview", {"grok_ids": "not-a-list"})
    out["error_bad_demo"] = api.post("/demo", {"action": "dance"})
    out.update(_writes(api, pid, out))
    out.update(_skill_sync(api, pid, folder))
    out["events"] = api.get("/events")
    out["_pair_id"] = pid
    return out


def _dump(world: str, answers: dict) -> None:
    target = os.environ.get("TWIN_CONTRACT_DUMP")
    if target:
        d = Path(target) / world
        d.mkdir(parents=True, exist_ok=True)
        for name, value in answers.items():
            (d / f"{name}.json").write_text(json.dumps(value, indent=1, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def sample(home, monkeypatch):
    answers = collect_sample(Api(_client(monkeypatch)))
    _dump("sample", answers)
    return answers


@pytest.fixture
def fake_api(home, tmp_path, monkeypatch):
    """The REST app over tests/fake_botport.py, named in the settings and not accepted yet."""
    exe = tmp_path / "bin" / "botport"
    exe.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    exe.write_text(f"#!{sys.executable} -I\n{FAKE.read_text(encoding='utf-8')}", encoding="utf-8")
    exe.chmod(0o755)
    os.chmod(tmp_path, 0o700)
    (tmp_path / "Grok Bot").mkdir()  # "the Grok desktop app is on this computer" (only its presence is looked at)
    config.set_override({"botport_path": str(exe), "grok_app_dir": str(tmp_path / "Grok Bot")})
    service.reset_service()
    api = Api(_client(monkeypatch))
    # a sample left in this home by an earlier fixture owns the invented bots' ids: end it first
    api.post("/demo", {"action": "remove"})
    return api


@pytest.fixture
def fake_real(fake_api, home, tmp_path):
    """A real twin made through tests/fake_botport.py (accepted with the explicit local-build opt-out)."""
    api = fake_api
    shared = tmp_path / "shared"
    shared.mkdir()
    first = {"status_unchecked": api.get("/status"), "setup_unchecked": api.get("/setup"),
             "grok_bots_unchecked": api.get("/grok/bots")}
    assert api.post("/engine/trust", {"allow_unattested": True})["trusted"] is True
    mode = home / "plugin-data" / "grok-bot-twin" / "botport-state" / "fake_mode.json"
    # what botport would have made: the twin's Hermes folder with the name and description, and one skill that
    # was changed in Hermes and waits to go to Grok
    skill = home / "profiles" / "acme-support" / "skills" / "imported" / "fun-fact"
    skill.mkdir(parents=True, mode=0o700)
    for d in (home / "profiles", home / "profiles" / "acme-support", skill.parent.parent, skill.parent):
        os.chmod(d, 0o700)
    (skill / "SKILL.md").write_text("# Fun fact\nShare one short, friendly fun fact when asked.\n", encoding="utf-8")
    (home / "profiles" / "acme-support" / "SOUL.md").write_text(
        "<!-- botport:begin persona-description -->\nAnswers questions about Acme widgets.\n"
        "<!-- botport:end persona-description -->\n<!-- botport:begin persona-name -->\nAcme Support\n"
        "<!-- botport:end persona-name -->\n", encoding="utf-8")
    normal = {"status": {"state": "needs_you", "pending": {"to_hermes": 0, "to_grok": 0, "in_flight": 0,
                                                           "waiting_approval": 1},
                         "waiting": [{"item": "skill:fun-fact", "reason": "needs_approval", "to": "grok",
                                      "detail": "You changed the skill fun-fact in Hermes."}]}}
    mode.parent.mkdir(parents=True, exist_ok=True)
    mode.write_text(json.dumps(normal), encoding="utf-8")

    def unknown(ask):
        mode.write_text(json.dumps({"mode": "error"}), encoding="utf-8")  # the helper: "no such twin"
        try:
            return ask()
        finally:
            mode.write_text(json.dumps(normal), encoding="utf-8")

    answers = {**first, **collect_real(api, shared, unknown=unknown)}
    _dump("fake", answers)
    return answers


# ---- which shape each collected answer must fit ----------------------------------------------------------------
ROUTES = {
    "status": "Status", "status_calm": "Status", "status_unchecked": "Status", "pair": "Pair",
    "setup": "Setup", "setup_unchecked": "Setup",
    "grok_bots": "GrokBots", "grok_bots_unchecked": "GrokBots",
    "preview": "Preview", "create": "Create",
    "conflicts": "Conflicts", "checklist": "Checklist", "parity": "Parity", "recent": "Recent",
    "restore_points": "RestorePoints", "remove_plan": "RemovePlan",
    "skip": "Skip", "skip_undo": "Skip", "sync": "Sync", "mirror": "Mirror", "mirror_off": "Mirror",
    "skill_sync": "SkillSyncSetup", "skill_sync_status": "SkillSyncStatus",
    "uninstall_plan": "UninstallPlan", "events": "Events",
    "demo_start": "Demo", "demo_grok-edit": "Demo", "demo_secret": "Demo", "demo_conflict": "Demo",
    "demo_hermes-edit": "Demo", "demo_hide_real": "Demo", "demo_show_real": "Demo",
    "error_unknown_twin": "ErrorEnvelope", "error_bad_input": "ErrorEnvelope", "error_bad_demo": "ErrorEnvelope",
    "error_bad_item": "ErrorEnvelope", "error_bad_conflict": "ErrorEnvelope", "error_blocked": "ErrorEnvelope",
    "approve": "Approve", "resolve": "Resolve", "restore": "Restore", "send_to_grok": "SendToGrok",
    "switch": "Switch", "switch_undo": "SwitchUndo",
    "error_secret_paste": "ErrorEnvelope", "instructions_accepted": "Instructions", "instructions": "Instructions",
    "restore_points_after_paste": "RestorePoints", "skill_text": "SkillText", "error_no_such_skill": "ErrorEnvelope",
    "setup_export_info": "SetupExportInfo", "setup_export": "SetupExport", "error_not_an_export": "ErrorEnvelope",
    "batch_info": "BatchInfo", "batch_match": "BatchMatch", "batch_import": "BatchImport",
    "batch_downloads": "BatchDownloads",
    "batch_models": "BatchModels",
    "set_model": "SetModel", "error_bad_model": "ErrorEnvelope",
    # twins on the owner's other computers (the sharing contract, local routes). The real answers of these routes are
    # checked against the same shapes in tests/test_fleet_units.py and tests/test_fleet_machines.py, which run
    # the sharing and the linked machine for real; the page's demo mock in tests/test_mock_contract.py.
    "fleet": "Fleet", "fleet_code": "CodeAnswer", "fleet_leaving": "Leaving",
}


def _validate_all(answers: dict) -> list[str]:
    problems: list[str] = []
    for name, value in answers.items():
        if name.startswith("_"):
            continue
        assert name in ROUTES, f"collected answer {name!r} has no shape named in ROUTES"
        problems += check_shape(value, ROUTES[name], name)
    return problems


# Differences between the sample answer and a real twin's answer that are BY DESIGN. Each entry is a path prefix
# as diff_shapes prints it; everything else that differs fails the test.
BY_DESIGN = (
    # /status.engine describes the helper file. The sample needs no helper, so in a home without one the facts
    # about the file (checked? genuine? accepted without the check?) are absent. Engine in api_shapes.json lists
    # them as optional, and the page reads `setup` (always complete) for its decisions.
    "status.engine.trusted", "status.engine.attested", "status.engine.allow_unattested",
    "status.engine.trust_warning",
    # An event's payload depends on what happened (a sample action carries its name, a sync its counts); the
    # envelope around it (at, type, pair_id, payload) is what must match.
    "events.events[0].payload",
    # `warnings` is there only when the helper reported some (GrokBots lists it as optional).
    "grok_bots.warnings",
    # where to choose a model, sent only while a twin has none; the sample never asks for a model
    "pair.model_hint", "create.pairs[0].model_hint",
    # `action` says what the owner can do about a check that does not pass (choose a model, bring the instructions
    # over); it is there only on such a check, and the two worlds list their checks in different orders
    "parity.checks[0].action",
)
# MISMATCH, parent to fix: differences that are NOT by design. Listed here only so the suite stays green while
# they are open; test_the_open_mismatch_list_is_still_true fails as soon as one is fixed, so the line is deleted.
MISMATCH: tuple = (
)


def _allowed(diff: str) -> bool:
    return any(diff.startswith(prefix) for prefix in (*BY_DESIGN, *MISMATCH))


COMPARED = ("pair", "setup", "grok_bots", "preview", "create", "conflicts", "checklist", "parity", "recent",
            "restore_points", "remove_plan", "skip", "skip_undo", "sync", "mirror", "mirror_off", "uninstall_plan",
            "events", "error_unknown_twin", "error_bad_input", "error_bad_demo", "error_bad_item",
            "error_bad_conflict", "approve", "resolve", "restore", "send_to_grok", "switch", "switch_undo",
            "error_secret_paste", "instructions_accepted", "instructions", "restore_points_after_paste",
            "skill_text", "error_no_such_skill", "setup_export_info", "setup_export", "error_not_an_export",
            "batch_info", "batch_match", "batch_import", "batch_downloads", "batch_models",
            "set_model", "error_bad_model")


WHEN_THERE = ("approve", "resolve", "restore", "skill_text")


def _all_diffs(sample: dict, real: dict) -> list[str]:
    diffs: list[str] = []
    for name in COMPARED:
        if name in WHEN_THERE and not (name in sample and name in real):
            continue  # nothing waiting, nothing to choose or nothing to put back in one of the worlds
        diffs += diff_shapes(sample[name], real[name], name)
    # /status as a whole: everything but the twins themselves (compared above as `pair`)
    diffs += diff_shapes(dict(sample["status"], pairs=[]), dict(real["status"], pairs=[]), "status")
    return diffs


def _same_shapes(sample: dict, real: dict) -> list[str]:
    return [d for d in _all_diffs(sample, real) if not _allowed(d)]


ERRORS = ("error_unknown_twin", "error_bad_input", "error_bad_demo", "error_bad_item", "error_bad_conflict")


def _check_errors(answers: dict) -> None:
    """d. One error envelope everywhere: HTTP 200 (Api asserts it), {"error": {code, message, hint?}}, plain words."""
    for name in ERRORS:
        body = answers[name]
        assert check_shape(body, "ErrorEnvelope", name) == []
        assert set(body) == {"error"} and set(body["error"]) <= {"code", "message", "hint"}
        msg = body["error"]["message"]
        assert msg and not any(w in msg for w in ("Traceback", "Error(", "PATH", "grok_ids", "conflict_id")), msg
    assert answers["error_unknown_twin"]["error"]["code"] == "pair_not_found"
    for name in ("error_bad_input", "error_bad_demo", "error_bad_item", "error_bad_conflict"):
        assert answers[name]["error"]["code"] == "bad_input", name


# ---- a. the sample world fits the spec ---------------------------------------------------------------------------
def test_sample_answers_fit_the_contract(sample):
    assert _validate_all(sample) == []
    _check_errors(sample)
    # the failure cases the page must be able to show are really in these answers
    pair = sample["pair"]
    assert pair["sample"] is True and pair["state"] == "needs_you"
    assert pair["secrets_withheld"] and pair["conflicts"] >= 1 and pair["waiting"]
    assert sample["conflicts"]["conflicts"] and sample["checklist"]["blocked_by"]
    assert sample["status"]["sample"]["on"] is True and sample["status"]["mode"] == "demo"
    # a twin that needs attention is not moved to Hermes: one plain sentence, in the same envelope
    assert sample["error_blocked"]["error"]["code"] == "blocked"
    assert sample["error_blocked"]["error"]["message"].startswith("First choose which version of ")
    # a refused paste names the line and never the text; accepted for that line, it is saved
    assert sample["error_secret_paste"]["error"]["code"] == "looks_like_secret"
    assert sample["error_secret_paste"]["error"]["lines"] == [2]
    assert "tr0ub4dor" not in json.dumps(sample)
    assert sample["instructions_accepted"]["saved"] is True and sample["instructions"]["setup_left"] == []
    assert sample["skill_text"]["text"].startswith("# Fun fact")


# ---- b. a real twin fits the same spec ---------------------------------------------------------------------------
def test_real_twin_answers_fit_the_contract(fake_real):
    assert _validate_all(fake_real) == []
    _check_errors(fake_real)
    assert fake_real["pair"]["sample"] is False and fake_real["status"]["mode"] == "botport"
    assert fake_real["status_unchecked"]["setup"] == {"ready": False, "grok_app_found": True, "helper_found": True,
                                                      "helper_checked": False, "problem": None, "cause": None}
    assert fake_real["error_secret_paste"]["error"]["lines"] == [2] and fake_real["instructions"]["saved"] is True
    assert fake_real["skill_text"]["name"] == "fun-fact" and "tr0ub4dor" not in json.dumps(fake_real)
    assert any(p["replaced_by"] == "paste" and p["text"] is not None
               for p in fake_real["restore_points_after_paste"]["points"])
    assert fake_real["setup_unchecked"]["helper"]["found"] is True
    assert fake_real["setup_unchecked"]["check"]["done"] is False


# ---- c. the two worlds answer in the same shapes (d: with the same error keys and codes) --------------------------
def test_sample_and_real_twin_answer_in_the_same_shapes(sample, request):
    real = request.getfixturevalue("fake_real")  # same throwaway home, after the sample answers were collected
    assert _same_shapes(sample, real) == []
    for name in ERRORS:
        assert sorted(sample[name]["error"]) == sorted(real[name]["error"]), name
        assert sample[name]["error"]["code"] == real[name]["error"]["code"], name
    # an entry of MISMATCH that no longer differs must be deleted: the list never hides drift
    diffs = _all_diffs(sample, real)
    stale = [prefix for prefix in MISMATCH if not any(d.startswith(prefix) for d in diffs)]
    assert stale == [], f"fixed, so take these out of MISMATCH: {stale}"


def test_status_keeps_the_helper_facts_right_after_a_sample_action(fake_api):
    api = fake_api
    assert api.post("/engine/trust", {"allow_unattested": True})["trusted"] is True
    before = api.get("/status")["engine"]
    assert before["allow_unattested"] is True and before["version"]
    api.post("/demo", {"action": "start"})
    after = api.get("/status")["engine"]
    assert sorted(after) == sorted(before) and after["version"] == before["version"]


def test_the_checker_notices_drift():
    """The comparison and the validator are themselves checked: a missing key, an extra key and a wrong type."""
    good = {"pair_id": "acme-support", "skipped": ["skill:fun-fact"]}
    assert check_shape(good, "Skip") == []
    assert check_shape({"pair_id": "acme-support"}, "Skip") == ["Skip: missing key 'skipped' (Skip)"]
    assert check_shape({**good, "extra": 1}, "Skip") == ["Skip: keys not in the contract (Skip): ['extra']"]
    assert check_shape({**good, "skipped": [1]}, "Skip") == ["Skip.skipped[0]: expected str, got int"]
    assert check_shape({"error": {"code": "x"}}, "ErrorEnvelope") == ["ErrorEnvelope.error: missing key 'message' (ErrorBody)"]
    a = {"pairs": [{"pair_id": "a", "waiting": [{"item": "x", "to": "grok"}]}], "n": 1}
    b = {"pairs": [{"pair_id": "a", "waiting": [{"item": "x", "to": None, "detail": "d"}]}], "n": "1", "more": True}
    assert diff_shapes(a, b, "r") == ["r.more: only in the real answer", "r.n: type sample=int real=str",
                                      "r.pairs[0].waiting[0].detail: only in the real answer"]
    assert diff_shapes(a, a, "r") == []


# ---- e. the very first run (no helper, no sample) ----------------------------------------------------------------
def test_first_run_answers_fit_the_contract(home, monkeypatch):
    api = Api(_client(monkeypatch))
    first = {"status": api.get("/status"), "setup": api.get("/setup"), "grok_bots": api.get("/grok/bots"),
             "events": api.get("/events"), "uninstall_plan": api.post("/uninstall", {"dry_run": True})}
    _dump("first-run", first)
    assert _validate_all(first) == []
    assert first["status"]["mode"] == "setup" and first["status"]["pairs"] == []
    assert first["status"]["sample"] == {"on": False, "hide_real": False, "instructions_example": None}
    assert first["status"]["has_data"] is False and first["status"]["setup"]["cause"] is None
    assert first["setup"]["ready"] is False and first["setup"]["helper"] == {"found": False, "version": None,
                                                                             "source": None}
    assert first["grok_bots"]["ready"] is False and first["grok_bots"]["bots"] == []
    assert first["grok_bots"]["sample_bots"] == []


# ---- b2/c2. the same against a real botport build (skipped without BOTPORT_TEST_BIN) -----------------------------
@pytest.mark.skipif(not (real_rig.BIN and os.path.isfile(real_rig.BIN) and os.access(real_rig.BIN, os.X_OK)),
                    reason="set BOTPORT_TEST_BIN to a botport build to run this")
def test_real_build_fits_the_contract_and_the_sample_shapes(rig, monkeypatch):  # noqa: F811
    world, home_dir, tmp = rig["world"], rig["home"], rig["tmp"]
    config.set_override({**rig["cfg"], "bots": ["acme*", "RECIPE*"]})
    api = Api(_client(monkeypatch))
    assert api.post("/engine/trust", {"allow_unattested": True})["trusted"] is True
    shared = tmp / "shared"
    shared.mkdir()

    def steps(pid: str) -> None:
        # changed in both places: the description in Hermes (SOUL.md) and in Grok (the rename step), then a sync
        soul = home_dir / "profiles" / "acme-helper" / "SOUL.md"
        soul.write_text(soul.read_text(encoding="utf-8").replace("Acme widgets", "Acme gadgets"), encoding="utf-8")
        world.step("rename")
        api.post("/sync", {"pair_id": pid})

    real = collect_real(api, shared, ACME, steps)
    _dump("real-build", real)
    assert _validate_all(real) == []
    assert real["pair"]["sample"] is False
    # the sample, collected in the same throwaway home afterwards (the sample never needs the helper)
    sample = collect_sample(api)
    _dump("sample-next-to-real-build", sample)
    assert _validate_all(sample) == []
    assert _same_shapes(sample, real) == []


# ---- the page harness's mock answers fit the same spec -----------------------------------------------------------
def test_page_harness_mock_fits_the_contract():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    cp = subprocess.run([node, str(ROOT / "tests" / "desktop" / "contract.mjs")], capture_output=True, text=True,
                        timeout=60, stdin=subprocess.DEVNULL)
    assert cp.returncode == 0, cp.stdout[-3000:] + cp.stderr[-3000:]
    assert "contract checks passed" in cp.stdout


# ---- the spec file itself ---------------------------------------------------------------------------------------
def test_spec_is_well_formed_and_every_shape_is_used():
    used = set(ROUTES.values())

    def walk(typ: str) -> None:
        for t in (x.strip() for x in typ.split("|")):
            while t.startswith("list[") and t.endswith("]"):
                t = t[5:-1]
            if t in _BASIC:
                continue
            assert t in SHAPES, f"unknown type {t!r} in api_shapes.json"
            if t not in used:
                used.add(t)
                for part in ("required", "backend", "optional"):
                    for sub in SHAPES[t].get(part, {}).values():
                        walk(sub)

    for name in list(used):
        used.discard(name)
        walk(name)
    for name, spec in SHAPES.items():
        assert set(spec) <= {"route", "note", "required", "backend", "optional", "closed"}, name
        keys = [k for part in ("required", "backend", "optional") for k in spec.get(part, {})]
        assert len(keys) == len(set(keys)), f"{name}: a key is listed twice"
    assert sorted(set(SHAPES) - used) == [], "shapes nothing refers to"
