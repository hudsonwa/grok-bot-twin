"""C2 F11: no background thread before there is something to do; the default tick is check + sync only; one failing
step never skips the others nor backs the timer off; the routines look ends when routines are off."""
from __future__ import annotations

import json
import threading
import time

import pytest

from twin_testlib import central, config, home, plugin, service, svc, timer  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import fleet, h2g, mem_facts, routines
from hermes_plugins.grok_bot_twin.twin import mem_layers as ml
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

STEPS = ("fleet_tick", "note_grok_app", "briefs_tick", "instructions_sync_tick", "to_grok_tick", "check_changed",
         "sync")


def _ours() -> set:
    return {t.name for t in threading.enumerate() if t.name.startswith("grok-bot-twin")}


def _real_twin(home):  # noqa: F811
    central(home).mkdir(parents=True, exist_ok=True)
    (central(home) / "pairs.json").write_text(json.dumps({"pairs": [
        {"pair_id": "test-scout", "grok": {"id": "b-test-scout", "name": "Test Scout"},
         "hermes": {"profile": "test-scout"}, "created_at": "2000-01-01T09:00:00Z"}]}), encoding="utf-8")


def _spy(monkeypatch, s, calls: list, fail: dict = None, changed=()):
    fail = fail or {}

    def make(name):
        def fn(*a, **k):
            calls.append(name)
            if name in fail:
                raise fail[name]
            return list(changed) if name == "check_changed" else None
        return fn
    for name in STEPS:
        monkeypatch.setattr(s, name, make(name))
    monkeypatch.setattr(mem_facts, "tick", make("mem_facts"))


@pytest.fixture
def no_routines():
    routines._reset_for_tests()
    yield
    routines._reset_for_tests()


def test_F11_no_thread_on_a_fresh_install(svc, home):  # noqa: F811
    before = _ours()
    assert timer.ensure_started(service.get_service) is False
    plugin._on_pre_llm_call(user_message="hello")
    plugin._on_session_start()
    time.sleep(0.2)
    assert _ours() - before == set(), "nothing to do: no thread"
    assert not timer.running()
    svc.demo_action("start")  # the first twin (the sample): the next call starts the check, still no watcher
    assert timer.ensure_started(service.get_service) is True
    assert "grok-bot-twin-sync" in _ours() and "grok-bot-twin-watch" not in _ours() - before


def test_F11_default_tick_is_check_and_sync_only(svc, home, monkeypatch):  # noqa: F811
    svc.demo_action("start")
    calls: list = []
    _spy(monkeypatch, svc, calls, changed=["acme-support"])
    assert svc.tick() == 1
    assert calls == ["check_changed", "sync"]


def test_F11_real_twin_runs_no_switched_off_opt_in(home, monkeypatch):  # noqa: F811
    _real_twin(home)
    s = service.get_service()
    calls: list = []
    _spy(monkeypatch, s, calls)
    s.tick()
    assert "mem_facts" not in calls and "to_grok_tick" not in calls  # no Mirror, Hermes -> Grok never used
    assert calls[-1] == "check_changed"


def test_F11_one_failing_step_never_skips_the_others(home, monkeypatch):  # noqa: F811
    _real_twin(home)
    s = service.get_service()
    monkeypatch.setattr(ml, "mirror_ready", lambda: True)
    (central(home) / h2g.STATE_FILE).write_text("{}", encoding="utf-8")
    calls: list = []
    boom = RuntimeError("boom")
    fail = {n: RuntimeError(n) for n in ("note_grok_app", "briefs_tick", "instructions_sync_tick", "mem_facts",
                                         "to_grok_tick")}
    _spy(monkeypatch, s, calls, fail={"fleet_tick": boom, **fail})
    with pytest.raises(RuntimeError) as exc:
        s.tick()
    assert exc.value is boom  # the first failure, raised after every step ran
    assert calls == ["fleet_tick", "note_grok_app", "briefs_tick", "instructions_sync_tick", "mem_facts",
                     "to_grok_tick", "check_changed"]
    # the engine failing in the check: every other step still ran this round
    calls.clear()
    _spy(monkeypatch, s, calls, fail={"check_changed": EngineError("engine_failed", "x")})
    with pytest.raises(EngineError):
        s.tick()
    assert calls[:6] == ["fleet_tick", "note_grok_app", "briefs_tick", "instructions_sync_tick", "mem_facts",
                         "to_grok_tick"]
    # one pair's sync failing: the next pair is still synced
    calls.clear()
    _spy(monkeypatch, s, calls, changed=["test-scout", "test-quill"], fail={"sync": EngineError("engine_failed", "x")})
    with pytest.raises(EngineError):
        s.tick()
    assert calls.count("sync") == 2


def test_F11_an_engine_error_does_not_back_the_timer_off(svc, home, monkeypatch):  # noqa: F811
    svc.demo_action("start")
    config.set_override({**config.settings(), "sync_interval_seconds": 30})
    monkeypatch.setattr(svc, "tick", lambda: (_ for _ in ()).throw(EngineError("engine_failed", "x")))
    waits: list = []

    class Wake:  # records how long the loop waits before each round
        def wait(self, delay):
            waits.append(delay)
            if len(waits) >= 3:
                timer._stop.set()
            time.sleep(0.01)

        def clear(self):
            pass

        def set(self):
            pass
    monkeypatch.setattr(timer, "_wake", Wake())
    assert timer.ensure_started(lambda: svc) is True
    timer._thread.join(3)
    assert timer._state["last_error"] == "engine_failed"
    assert waits[1:] == [30.0, 30.0]  # each next round comes on time (it used to double, up to 10 min)


def test_F11_routines_thread_not_running_while_routines_are_off(home, no_routines, monkeypatch):  # noqa: F811
    fleet.save(fleet.load())  # a computer that once shared
    routines.state_path().parent.mkdir(parents=True, exist_ok=True)
    routines.state_path().write_text(json.dumps({"on": False}), encoding="utf-8")
    assert fleet.exists() and routines.exists()
    routines.ensure_started()
    assert "grok-bot-twin-routines" not in _ours()
    # a running look ends by itself once there is nothing left to look after
    work = [True]
    monkeypatch.setattr(routines, "_has_work", lambda: work[0])
    monkeypatch.setattr(routines, "TICK_S", 0.05)
    routines.ensure_started()
    t = routines._thread
    assert t is not None and t.is_alive()
    work[0] = False
    t.join(3)
    assert not t.is_alive()
