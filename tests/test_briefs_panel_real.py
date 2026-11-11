"""Session briefs in the panel's real "What syncs" panel (skipped in a build without twin/sync_panel.py)."""
from __future__ import annotations

import pytest

from twin_testlib import ROOT, home  # noqa: F401
from test_briefs_service import PID, setup  # noqa: F401

if not (ROOT / "twin" / "sync_panel.py").exists():
    pytest.skip("this build has no What syncs panel", allow_module_level=True)

from hermes_plugins.grok_bot_twin.twin import briefs as br, sync_panel as sp  # noqa: E402


def _row(svc):
    return next(i for t in sp.view(svc)["twins"] for i in t["items"] if i["key"] == "session_briefs")


def test_off_until_the_owner_chooses_then_the_panel_is_the_switch(setup, monkeypatch):
    svc, rec, _ = setup
    monkeypatch.setattr(br, "_panel", lambda: sp)  # the fixture hid the panel; here it is the real one
    assert br.register_panel()
    row = _row(svc)
    assert row["direction"] == "off" and br.effective(PID) == (False, False)
    assert {c["direction"]: c["state"] for c in row["choices"]}["both"] == "ok"
    sp.choose(svc, PID, "session_briefs", "hermes_to_grok")
    assert br.effective(PID) == (True, False) and br.settings(PID)["to_grok"]
    br.set_transport(None)
    assert {c["direction"]: c["state"] for c in _row(svc)["choices"]}["both"] == "needs_mirror"


def test_the_row_turns_on_by_itself_once_the_mirror_is_ready(setup, monkeypatch):
    """No set_transport: the default transport (the memory layers) is what the panel row asks, so the row can be
    turned on as soon as the Mirror is plugged in there, and the round hands the brief over through it."""
    from hermes_plugins.grok_bot_twin.twin import mem_layers as ml
    svc, rec, _ = setup
    br.set_transport(None)
    monkeypatch.setattr(br, "_panel", lambda: sp)
    assert br.register_panel()
    assert {c["direction"]: c["state"] for c in _row(svc)["choices"]}["both"] == "needs_mirror"
    sent = []

    class Mirror(ml.StubTransport):
        def write_brief(self, brief):
            sent.append((self.profile, brief["id"]))
            return True
    ml.set_transport_factory(Mirror)
    try:
        assert {c["direction"]: c["state"] for c in _row(svc)["choices"]}["both"] == "ok"
        with monkeypatch.context() as m:
            m.setattr(br.time, "time", lambda: 946717200.0 - 10)
            sp.choose(svc, PID, "session_briefs", "hermes_to_grok")
        assert _row(svc)["direction"] == "hermes_to_grok" and br.effective(PID) == (True, False)
        res = svc.briefs_now(PID)["results"][0]
        assert res["made"] == 1 and res["sent"] == 1 and sent and sent[0][0] == "test-scout"
    finally:
        ml.set_transport_factory(None)
    # the Mirror went: the saved choice is shown as Off, and Off it is
    assert _row(svc)["direction"] == "off" and br.effective(PID) == (False, False)
