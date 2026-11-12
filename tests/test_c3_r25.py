"""R25: a routine runs on at most one computer. An unlink that cannot SEE every routine copy paused is held back,
on every path (the owner's unlink, and the sharing computer's "you are unlinked"), and nothing about routines is
forgotten meanwhile: the copy stays known and "routines may run on this computer" stays as the owner set it."""
from __future__ import annotations

import pytest

from twin_testlib import home, service  # noqa: F401
from test_fleet_units import T1, _apply, _linked, _list
from hermes_plugins.grok_bot_twin.twin import fleet, fleet_follow as follow, routines, routines_jobs as rj
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

RID = "j-00000000000000aa"
HELD = ("This computer stays linked for now: a routine it runs could not be paused, and unlinking would let it "
        "run on two computers.")


def _bot(home):  # noqa: F811
    return home / "profiles" / "test-scout"


def _copy(home):  # noqa: F811
    return next(j for j in rj.load(_bot(home)) if j.get(rj.COPY_KEY) == RID)


@pytest.fixture
def running_here(home, tmp_path, monkeypatch):  # noqa: F811
    """A linked computer ("Surface") that runs a copy of a routine for the others; Hermes then holds the job file,
    so no pause can be made (the cron lock times out)."""
    svc = _linked(home, tmp_path)
    _apply(_list([{"id": T1, "name": "Test Scout", "description": "Finds trails.", "version": 1, "updated_at": None}]))
    assert _bot(home).is_dir()
    job = {"id": "abc123", "name": "Morning trails", "prompt": "List three trails.", "enabled": True,
           "state": "scheduled", "schedule": {"kind": "cron", "expr": "0 7 * * *", "display": "0 7 * * *"},
           rj.COPY_KEY: RID}
    rj.edit(_bot(home), lambda jobs: (jobs.append(job) is None, None))
    R = routines.load()
    R["can_run"] = True
    R["dst"] = routines._blank_dst()
    R["dst"]["items"] = {RID: {"job": "abc123", "twin": T1}}
    routines.save(R)
    assert rj.runnable(_copy(home))

    edit, gate = rj.edit, {"held": True}

    def held(home, fn):  # noqa: F811
        if gate["held"]:
            raise OSError("Hermes holds the routines file")
        return edit(home, fn)

    def goodbye(ln):
        assert not gate["held"], "no goodbye while a copy may still run"
        return True
    monkeypatch.setattr(rj, "edit", held)
    monkeypatch.setattr(follow, "say_goodbye", goodbye)
    svc.gate = gate
    return svc


def _kept(home):  # noqa: F811
    """Nothing was let go: still linked (so the sharing computer still counts this one as running it), the bot in
    place, the copy known, the owner's switch as it was. The copy runs on THIS computer only."""
    assert fleet.load()["link"] is not None
    assert _bot(home).is_dir() and rj.runnable(_copy(home))
    R = routines.load()
    assert R["can_run"] is True and R["dst"] is not None and RID in R["dst"]["items"]


def _gone(home):  # noqa: F811
    """Let go only once every copy was seen paused: wherever the bot went, no copy can run here any more."""
    assert fleet.load()["link"] is None
    R = routines.load()
    assert R["can_run"] is False and R["dst"] is None
    found = [j for f in home.parent.rglob("jobs.json") for j in (rj.load(f.parent.parent) or [])
             if j.get(rj.COPY_KEY) == RID]
    assert found and not any(rj.runnable(j) for j in found)


def test_R25_owner_unlink_is_held_back_with_one_clear_sentence_and_forgets_nothing(running_here, home):  # noqa: F811
    with pytest.raises(EngineError) as e:
        running_here.fleet_unlink(False)
    _kept(home)
    assert e.value.code == "unlink_held" and str(e.value.message) == HELD
    with pytest.raises(EngineError):  # (asked again: the same, and still nothing let go)
        running_here.fleet_unlink(False)
    _kept(home)


def test_R25_source_says_unlinked_is_held_back_until_the_copy_is_seen_paused(running_here, home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(follow, "_counted_request",
                        lambda *a, **k: (401, {"error": {"code": "unlinked"}}, True))
    assert follow.sync_once(running_here) != "unlinked"
    _kept(home)
    # (and drop_link itself, the one place an unlink pauses routines, refuses the same way)
    with service.store.locked():
        st = fleet.load()
        with pytest.raises(routines.RoutinesError) as e:
            fleet.drop_link(st, "source")
    assert e.value.code == "unlink_held"
    _kept(home)
    # Hermes lets go of the file: the next "you are unlinked" pauses the copy first, then lets the link go
    running_here.gate["held"] = False
    assert follow.sync_once(running_here) == "unlinked"
    _gone(home)


def test_R25_owner_unlink_goes_through_once_the_copy_is_paused(running_here, home):  # noqa: F811
    with pytest.raises(EngineError):
        running_here.fleet_unlink(False)
    running_here.gate["held"] = False
    running_here.fleet_unlink(False)
    _gone(home)
