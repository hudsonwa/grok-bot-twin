"""F6 (cycle 2): no twin is "Up to date" while a choice is open anywhere. botport's own conflicts were always counted;
an open conflict of the instructions sync (Grok -> Hermes) or of Hermes -> Grok was not, so the status said "Up to
date" over an open card. The status (CLI, slash, tool and the page all read it) now names them, and is back to "Up to
date" once the owner chose. Invented bots only; botport is tests/fake_botport.py, Grok is a pretend account."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_service_botport import bp_svc  # noqa: F401
from twin_testlib import cli, home, store  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import h2g, instr_sync

PID, BOT = "acme-support", "b-acme-0001"


def _soul(name: str) -> str:
    return "".join(f"<!-- botport:begin {k} -->\n{v}\n<!-- botport:end {k} -->\n" for k, v in (
        ("persona-name", name), ("persona-description", "Helps with invented widgets."), ("persona", "Be brief.")))


@pytest.fixture
def twin(bp_svc, home):  # noqa: F811
    bp_svc.trust_botport(True)
    prof = home / "profiles" / PID
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "SOUL.md").write_text(_soul("Acme Support"))
    pair = _pair(bp_svc)
    assert pair["state"] == "in_step" and "open_conflicts" not in pair  # (the starting point: nothing open)
    return bp_svc, prof


def _pair(svc) -> dict:
    return next(p for p in svc.status(fresh=True)["pairs"] if p["pair_id"] == PID)


def test_F6_open_instructions_sync_conflict_is_not_up_to_date(twin, capsys):
    svc, _ = twin
    svc._isync_update(PID, state="conflict", conflict={"at": "2026-11-12T00:00:00Z", "grok": "Be very brief."})
    p = _pair(svc)
    assert p["state"] == "needs_you" and p["state_reason"] == "Choose which instructions to keep"
    assert p["open_conflicts"] == [{"where": "instructions", "item": "instructions"}]
    shape = json.loads((Path(__file__).parent / "contract" / "api_shapes.json").read_text())["shapes"]
    assert set(p) <= {k for part in ("required", "backend", "optional") for k in shape["Pair"].get(part, {})}
    assert shape["Pair"]["optional"]["open_conflicts"] == "list[OpenConflict]"  # (the page's contract: optional)
    assert cli.main(["status"]) == 1  # (a twin needs you)
    out = capsys.readouterr().out
    assert "Changed in both places" in out and "hermes twin instructions" in out and "nothing needs you" not in out
    # the owner keeps Hermes' version: the card closes and the twin is up to date again
    svc.instructions_conflict_resolve(PID, "hermes")
    p = _pair(svc)
    assert p["state"] == "in_step" and p["state_reason"] == "Up to date" and "open_conflicts" not in p
    # switched off, an old card does not count either
    svc._isync_update(PID, state="conflict", conflict={"at": "2026-11-12T00:00:00Z", "grok": "Be very brief."},
                      on=False)
    assert _pair(svc)["state"] == "in_step"


def test_F6_open_hermes_to_grok_conflict_is_not_up_to_date(twin, monkeypatch):
    svc, prof = twin
    w = h2g.FakeGrokWriter()
    w.put_bot(BOT, name="Acme Support", description="Helps with invented widgets.", instructions="Be brief.")
    svc.h2g_test_writer = w
    svc.to_grok_allow(PID, ["name"])
    assert _pair(svc)["state"] == "in_step"
    # the name changed in Hermes and, differently, in Grok: Hermes -> Grok holds it as a conflict
    (prof / "SOUL.md").write_text(_soul("Acme Support Pro"))
    w.put_bot(BOT, name="Acme Support Plus", description="Helps with invented widgets.", instructions="Be brief.",
              allowed=True)
    assert [h["code"] for h in svc.to_grok_preview(PID)["held"]] == ["conflict"]
    p = _pair(svc)
    assert p["state"] == "needs_you" and p["state_reason"] == "Choose which name to keep"
    assert p["open_conflicts"] == [{"where": "to_grok", "item": "name"}]
    # the owner keeps Grok's: nothing is held any more, and the twin is up to date again
    svc.to_grok_resolve(PID, "name", "grok")
    p = _pair(svc)
    assert p["state"] == "in_step" and "open_conflicts" not in p


def test_F6_derive_state_never_in_step_with_an_open_conflict():
    from hermes_plugins.grok_bot_twin.twin.model import derive_state, status_sentence
    base = {"grok": {"present": True}, "conflicts": 0, "pending": {}}
    assert derive_state(base) == ("in_step", "Up to date")
    both = dict(base, open_conflicts=[{"where": "instructions", "item": "instructions"},
                                      {"where": "to_grok", "item": "instructions"},
                                      {"where": "to_grok", "item": "routine:morning"}])
    assert derive_state(both) == ("needs_you", "Choose which instructions and routine to keep")
    assert status_sentence(dict(both, state="needs_you")) == "3 things need you"
    assert instr_sync.STATES.count("conflict") == 1
