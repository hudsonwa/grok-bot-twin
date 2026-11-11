"""Undo after "Keep the Grok version" works for a real twin the way it does for the sample.

The sample's answer to a choice names the earlier version it kept (`restore_point`); the helper's answer does not.
The service now finds it (the earlier versions are listed before and after the choice), so the page's Undo button
is not a sample-only promise. With BOTPORT_TEST_BIN set this is proved against the real helper.
"""
from __future__ import annotations

import os

import pytest

from twin_testlib import config, home, service, svc, timer  # noqa: F401
from test_ux_flows import fake  # noqa: F401
from test_service_real_botport import BIN, _soul, rig  # noqa: F401


def _points(*ids):
    return {"points": [{"id": i, "item": i.split("@")[0], "kind": "skill", "name": "x", "at": "2000-01-01T09:00:00Z",
                        "reason": "kept copy", "scope": "item"} for i in ids]}


def _real_twin(fake):  # noqa: F811
    s = fake.svc
    s.trust_botport(True)
    s.create(["b-acme-0001"])
    return s, s.botport()


def _conflict(grok_digest="c354dae9eaf5"):
    return {"conflicts": [{"conflict_id": "skill:greet-visitors", "kind": "skill", "name": "greet-visitors",
                           "since": "2000-01-01T09:00:00Z", "grok": {"digest": grok_digest, "text": "Welcome.\n"},
                           "hermes": {"digest": "aa44e30d6551", "text": "Hello.\n"}, "diff": ""}]}


def _wire(monkeypatch, bp, before, after, conflict=None):
    answers = iter([before, after])
    monkeypatch.setattr(bp, "restore_points", lambda pid: next(answers))
    monkeypatch.setattr(bp, "conflicts", lambda pid: conflict or _conflict())


def test_keep_grok_names_the_earlier_version_it_made(fake, monkeypatch):  # noqa: F811
    """The helper keeps two new entries on "Keep Grok": the Grok version now in place (under its own digest) and
    the Hermes version it replaced. The answer names the replaced one."""
    s, bp = _real_twin(fake)
    _wire(monkeypatch, bp, _points("persona.name@8ef22c6d10ba"),
          _points("skill:greet-visitors@c354dae9eaf5", "persona.name@8ef22c6d10ba", "skill:greet-visitors@3f2a9c1b7e40"))
    r = s.resolve("acme-helper", "skill:greet-visitors", "grok")
    assert r == {"applied": [{"item": "skill:greet-visitors", "keep": "grok"}],
                 "restore_point": "skill:greet-visitors@3f2a9c1b7e40"}


def test_no_undo_is_offered_when_it_cannot_be_told_for_sure(fake, monkeypatch):  # noqa: F811
    s, bp = _real_twin(fake)
    # nothing new appeared (the fake helper always lists the same one entry)
    assert s.resolve("acme-helper", "skill:greet-visitors", "grok")["restore_point"] is None
    # a new entry, but of another item
    _wire(monkeypatch, bp, _points(), _points("persona.name@8ef22c6d10ba"))
    assert s.resolve("acme-helper", "skill:greet-visitors", "grok")["restore_point"] is None
    # only the Grok version itself appeared
    _wire(monkeypatch, bp, _points(), _points("skill:greet-visitors@c354dae9eaf5"))
    assert s.resolve("acme-helper", "skill:greet-visitors", "grok")["restore_point"] is None
    # two candidates: never guess
    _wire(monkeypatch, bp, _points(), _points("skill:greet-visitors@aaaaaaaaaaaa", "skill:greet-visitors@bbbbbbbbbbbb"))
    assert s.resolve("acme-helper", "skill:greet-visitors", "grok")["restore_point"] is None
    # a digest that is not a digest is never built into an id
    _wire(monkeypatch, bp, _points(), _points("skill:greet-visitors@aaaaaaaaaaaa", "skill:greet-visitors@bbbbbbbbbbbb"),
          _conflict("../../etc"))
    assert s.resolve("acme-helper", "skill:greet-visitors", "grok")["restore_point"] is None

    # the list could not be read before the choice: the choice is still made, without an Undo
    def boom(pid):
        raise service.EngineError("locked", "busy")
    monkeypatch.setattr(bp, "restore_points", boom)
    r = s.resolve("acme-helper", "skill:greet-visitors", "grok")
    assert r["applied"] and r["restore_point"] is None


def test_keep_hermes_and_the_sample_ask_nothing_extra(fake, monkeypatch):  # noqa: F811
    s, bp = _real_twin(fake)
    asked = []
    monkeypatch.setattr(bp, "restore_points", lambda pid: asked.append(pid) or _points())
    assert s.resolve("acme-helper", "skill:greet-visitors", "hermes")["restore_point"] is None
    assert asked == []  # keeping the Hermes version replaces nothing, so there is nothing to look up
    s.demo_action("start")
    sid = s.demo.pair_ids()[0]
    s.demo_action("conflict")
    s.sync(sid)
    c = s.conflicts(sid)["conflicts"][0]
    r = s.resolve(sid, c["conflict_id"], "grok")
    assert isinstance(r["restore_point"], str) and asked == []  # the sample names it itself


@pytest.mark.skipif(not (BIN and os.path.isfile(BIN) and os.access(BIN, os.X_OK)),
                    reason="set BOTPORT_TEST_BIN to a botport build to run this")
def test_undo_after_keep_grok_with_the_real_helper(rig):  # noqa: F811
    s, world, hermes_home = rig["svc"], rig["world"], rig["home"]
    s.trust_botport(True)
    s.create(["b-acme-0001"])
    s.check_changed()
    # the same item changed in both places before a sync: the description
    soul = hermes_home / "profiles" / "acme-helper" / "SOUL.md"
    soul.write_text(_soul(hermes_home, "acme-helper").replace("Answers questions about Acme widgets.",
                                                               "Answers questions about Acme widgets, politely."))
    world.step("rename")
    s.sync(None)
    c = s.conflicts("acme-helper")["conflicts"][0]
    r = s.resolve("acme-helper", c["conflict_id"], "grok")
    assert "spare parts" in _soul(hermes_home, "acme-helper")
    assert isinstance(r["restore_point"], str) and r["restore_point"].startswith("persona.description@"), r
    back = s.restore("acme-helper", r["restore_point"])  # what the page's Undo button sends
    assert back["restored"] == ["persona.description"]
    after = _soul(hermes_home, "acme-helper")
    assert "politely" in after and "spare parts" not in after
