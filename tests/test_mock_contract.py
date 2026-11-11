"""Contract test: the demo mock the Twins page was designed against answers in the same shapes as the real API.

The page's design sandbox holds a mock REST backend (`mockcore.py`: a stateful, invented world with about forty
scenarios). The page was walked against it, so whatever it answers is what the page expects. This file loads that
mock in process, with a throwaway state file, and checks every answer of every scenario against
`tests/contract/api_shapes.json`: the same file the real backend's answers are checked against in
`tests/test_api_contract.py` (the built-in sample, a real twin through botport). It then compares the mock's
answers with the real backend's key by key.

The mock is not part of this repository. It is looked for at TWIN_PAGE_MOCK (the folder that holds mockcore.py);
without it these tests are skipped.
Nothing here starts a Desktop, a thread or a subprocess; the mock touches only its own state file (in tmp_path).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from twin_testlib import home  # noqa: F401
import test_api_contract as contract
from test_api_contract import Api, _client, check_shape, diff_shapes


def _mock_dir() -> Path | None:
    cands = [Path(os.environ["TWIN_PAGE_MOCK"])] if os.environ.get("TWIN_PAGE_MOCK") else []
    return next((c for c in cands if (c / "mockcore.py").is_file()), None)


MOCK_DIR = _mock_dir()
pytestmark = pytest.mark.skipif(MOCK_DIR is None, reason="the page mock is not on this computer "
                                                         "(set TWIN_PAGE_MOCK to the folder with mockcore.py)")


@pytest.fixture
def mock(tmp_path, monkeypatch):
    """The mock's core, loaded fresh with its whole world in tmp_path."""
    monkeypatch.setenv("GBT_MOCK_STATE", str(tmp_path / "mock" / "state.json"))
    spec = importlib.util.spec_from_file_location("_gbt_mockcore_contract", MOCK_DIR / "mockcore.py")
    core = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = core
    spec.loader.exec_module(core)
    assert str(core.STATE_FILE).startswith(str(tmp_path))
    yield core
    sys.modules.pop(spec.name, None)


def _set(core: Any, scenario: str) -> None:
    core.save(core.build_scenario(scenario))


# ---- mock-only behaviour that is not part of the contract (each line says why) --------------------------------
# A scenario that answers like an OLDER backend on purpose, so the page's fallbacks can be walked.
OLD_SHAPE_SCENARIOS = {"grok-gone"}
# Differences between a mock answer and the contract that are known and wanted. Path prefixes as check_shape
# prints them, per scenario ("*" = every scenario).
MOCK_ONLY: dict = {
    # the mock answers "no build for this computer" in two ways; the real backend keeps
    # `download_url` (the release page) as text in both
    "no-platform": ("setup.download_url", "error_path."),
    # `fast_check` (is a new name in Grok picked up right away?) belongs to the real background check; the mock
    # has no background check at all
    # `download.dir` (the Downloads folder, so the file picker opens there): the mock does not send it, and
    # the page shows it nowhere
    "*": ("status: missing key 'fast_check'", "setup: missing key 'fast_check'",
          "setup.download: missing key 'dir'"),
}


def _allowed(scenario: str, problem: str) -> bool:
    return any(problem.startswith(prefix) for prefix in (*MOCK_ONLY.get(scenario, ()), *MOCK_ONLY.get("*", ())))


def _answers(core: Any) -> dict:
    """Every read route (and every dry run) of the mock's current world, by the names test_api_contract uses."""
    h = core.handle
    out: dict = {"status": h("GET", "/status"), "setup": h("GET", "/setup"), "grok_bots": h("GET", "/grok/bots"),
                 "events": h("GET", "/events"), "uninstall_plan": h("POST", "/uninstall", {"dry_run": True})}
    for i, p in enumerate(out["status"].get("pairs", [])):
        pid = p["pair_id"]
        out[f"pair#{i}"] = p
        for name, path in (("conflicts", "conflicts"), ("checklist", "checklist"), ("parity", "parity"),
                           ("recent", "recent"), ("restore_points", "restore-points")):
            out[f"{name}#{i}"] = h("GET", f"/pairs/{pid}/{path}")
        out[f"remove_plan#{i}"] = h("POST", f"/pairs/{pid}/remove", {"dry_run": True})
    return out


SHAPE_OF = {**contract.ROUTES, "instructions": "Instructions", "skill_text": "SkillText"}


def _problems(scenario: str, answers: dict) -> list:
    probs = []
    for name, value in answers.items():
        base = name.split("#")[0]
        if isinstance(value, dict) and set(value) == {"error"}:
            shape = "ErrorEnvelope"  # e.g. conflicts of a twin that cannot be checked: the one error envelope
        else:
            shape = SHAPE_OF[base]
        probs += [f"[{scenario}] {x}" for x in check_shape(value, shape, name) if not _allowed(scenario, x)]
    return probs


def test_every_mock_scenario_answers_in_the_contracts_shapes(mock):
    problems: list = []
    seen = 0
    for scenario in mock.SCENARIOS:
        if scenario in OLD_SHAPE_SCENARIOS:
            continue
        _set(mock, scenario)
        answers = _answers(mock)
        seen += len(answers)
        problems += _problems(scenario, answers)
    assert problems == [], "\n".join(problems[:60])
    assert seen > 300, "the mock answered far fewer routes than expected"


def test_the_old_shape_scenario_is_really_the_old_shape(mock):
    """`grok-gone` imitates an older backend so the page's fallback can be walked. If it ever gains the
    new keys it stops testing the fallback, and belongs in the test above instead."""
    _set(mock, "grok-gone")
    st = mock.handle("GET", "/status")
    assert "cause" not in st["setup"] and st["setup"]["grok_app_found"] is False


def test_mock_writes_answer_in_the_contracts_shapes(mock):
    h = mock.handle
    problems: list = []

    def check(scenario: str, name: str, value: Any, shape: str) -> Any:
        problems.extend(f"[{scenario}] {x}" for x in check_shape(value, shape, name) if not _allowed(scenario, x))
        return value

    # connect: the download is found, used, and botport is verified by the pinned table
    _set(mock, "download-found")
    assert h("GET", "/setup")["download"]["found"]["name"]
    done = check("download-found", "setup", h("POST", "/engine/path", {"from_downloads": True}), "Setup")
    assert done["check"]["done"] is True and done["check"]["method"] == "pinned"
    _set(mock, "download-found-bad")
    bad = check("download-found-bad", "error", h("POST", "/engine/path", {"from_downloads": True}), "ErrorEnvelope")
    assert bad["error"]["code"] == "not_genuine"
    # twin a bot, then finish it: instructions with a line that looks like a password, then overruled for that line
    _set(mock, "ready-model")
    bots = h("GET", "/grok/bots")["bots"]
    gid = bots[0]["grok_id"]
    check("ready-model", "preview", h("POST", "/twins/preview", {"grok_ids": [gid]}), "Preview")
    made = check("ready-model", "create", h("POST", "/twins/create", {"grok_ids": [gid]}), "Create")
    pid = made["pairs"][0]["pair_id"]
    assert made["pairs"][0]["setup_left"] == ["instructions"] and made["pairs"][0]["hermes"]["model_from"] == "default"
    text = "You are a trail guide.\nBe brief.\npassword: " + "correct-horse-battery-staple-9"
    refused = check("ready-model", "error", h("POST", f"/pairs/{pid}/instructions", {"text": text}), "ErrorEnvelope")
    assert refused["error"]["code"] == "looks_like_secret" and refused["error"]["lines"] == [3]
    saved = check("ready-model", "instructions",
                  h("POST", f"/pairs/{pid}/instructions", {"text": text, "accept_lines": [3]}), "Instructions")
    assert saved["saved"] is True and saved["setup_left"] == []
    check("ready-model", "restore_points", h("GET", f"/pairs/{pid}/restore-points"), "RestorePoints")
    check("ready-model", "sync", h("POST", "/sync", {"pair_id": pid}), "Sync")
    # a skill the owner copies into Grok by hand
    _set(mock, "everyday")
    check("everyday", "skill_text", h("GET", "/pairs/book-club/skill-text", None, {"item": "skill:pick-a-book"}),
          "SkillText")
    check("everyday", "skip", h("POST", "/pairs/book-club/skip", {"item": "skill:pick-a-book", "copied": True}), "Skip")
    check("everyday", "error", h("GET", "/pairs/book-club/skill-text", None, {"item": "skill:no-such"}), "ErrorEnvelope")
    # the sample: start, the Try-it changes, both sides of the twin
    _set(mock, "first-run")
    for action in ("start", "grok-edit", "secret", "remove-secret", "conflict", "hermes-edit", "remove"):
        check("first-run", f"demo_{action}", h("POST", "/demo", {"action": action}), "Demo")
    # moving routines: refused while something changed in both places, in the one error envelope
    _set(mock, "sample-conflict")
    sample_pid = h("GET", "/status")["pairs"][0]["pair_id"]
    blocked = check("sample-conflict", "error", h("POST", f"/pairs/{sample_pid}/switch", {"job_ids": []}), "ErrorEnvelope")
    assert blocked["error"]["code"] == "blocked"
    assert problems == [], "\n".join(problems[:60])


def test_mock_and_real_backend_answer_with_the_same_keys(mock, home, monkeypatch):  # noqa: F811
    """Key by key: the mock's sample twin and the real backend's sample twin, and both setup answers. A key only
    one of them sends is drift between the page's world and the product."""
    api = Api(_client(monkeypatch))
    real_status = api.post("/demo", {"action": "start"}) and api.get("/status")
    real_pair = real_status["pairs"][0]
    real_setup = api.get("/setup")
    _set(mock, "sample-new")
    mock_status = mock.handle("GET", "/status")
    mock_pair = next(p for p in mock_status["pairs"] if p["sample"])
    diffs = diff_shapes(mock_pair, real_pair, "pair")
    diffs += diff_shapes(dict(mock_status, pairs=[]), dict(real_status, pairs=[]), "status")
    diffs += diff_shapes(mock.handle("GET", "/setup"), real_setup, "setup")
    for name, path, body in (("checklist", "checklist", None), ("parity", "parity", None),
                             ("restore_points", "restore-points", None), ("remove_plan", "remove", {"dry_run": True})):
        m = mock.handle("POST" if body else "GET", f"/pairs/{mock_pair['pair_id']}/{path}", body)
        r = api.post(f"/pairs/{real_pair['pair_id']}/{path}", body) if body else api.get(f"/pairs/{real_pair['pair_id']}/{path}")
        diffs += diff_shapes(m, r, name)
    diffs += diff_shapes(mock.handle("POST", "/uninstall", {"dry_run": True}), api.post("/uninstall", {"dry_run": True}),
                         "uninstall_plan")
    # "sample" in these lines means the mock, "real" the real backend (diff_shapes names its two sides that way)
    wanted = [d.replace("only in the sample answer", "only in the mock's answer")
               .replace("type sample=", "type mock=") for d in diffs if not any(d.startswith(p) for p in KEY_BY_DESIGN)]
    assert wanted == [], "\n".join(wanted)


# Keys that only one side sends, by design.
KEY_BY_DESIGN = (
    # facts about the botport file, there only while one is found (the sample needs none); see test_api_contract
    "status.engine.",
    # the real backend says whether a changed name is picked up right away (job 1); the mock has no background check
    "status.fast_check",
    # the real backend's opaque twin id for the model and the per-twin activity-digest settings: not page data
    "pair.ref", "pair.digest", "pair.parity", "pair.last_change_at", "pair.error",
    # only while a model is still to be chosen (never for the sample)
    "pair.model_hint",
    # the Downloads folder for the file picker: the mock does not send it
    "setup.download.dir",
    # the real backend's plain sentence for a comparison row; the page falls back to its own words, and
    # the page mock, which is older, does not send it
    "parity.checks[0].why",
)
