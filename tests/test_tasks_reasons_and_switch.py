"""Requests between the owner's computers: the reason a request failed, and the switch that allows requests.

- a request that failed left the ASKED computer's own list saying only "Not done here"
- the switch "Let my other computers ask this one to do things" stayed on through an unlink and a new link
- nothing told a computer which of the others take requests (`TasksMachine.takes_requests`)

Invented twins and computers only. The first tests need no second computer; the last one runs "mini" as a real
second process (tools/fleet.py) with the test process as "Surface", as tests/test_tasks_machines.py does.
"""
from __future__ import annotations

import json
import re

import pytest

from fleet_testlib import shape_problems, wait_until, wire_sync
from test_tasks_machines import (ASK, _listening, _mid, _pass, fake, orchard, source, surface)  # noqa: F401
from test_tasks_units import ME, OTHER, SRC, linked  # noqa: F401
from twin_testlib import config, home, service  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import fleet, fleet_follow as follow, store, tasks, tasks_hub


# ---- the asked computer's own list says why -------------------------------------------------------------------------
def test_every_way_a_request_ends_has_its_reason_in_the_asked_computers_list():
    said = {code: tasks.in_label("timeout" if code == "task_timeout" else "cancelled" if code == "stopped" else
                                 "refused" if tasks.IN_REASONS.get(code, "").startswith("refused") else "failed", code)
            for code in tasks.CODES}
    assert said["task_failed"] == "not done: the twin here could not answer; open its chat to see why"
    assert said["no_hermes"] == "not done: Hermes could not start a chat with the twin here"
    assert said["task_timeout"] == "not done: the twin here did not finish in time"
    assert said["stopped"] == "not done: Hermes here stopped before the request finished"
    assert said["no_twin_there"] == "refused: this computer holds no copy of that twin"
    assert said["tasks_off"] == said["switched_off"] == "refused: requests are switched off here"  # (as before)
    assert said["approval_denied"] == "not done: a step was not approved here"                      # (as before)
    # every code a request to this computer can end with (tasks_run.do_task, run_turn) gives more than "not done here"
    for code in ("task_failed", "no_hermes", "task_timeout", "stopped", "switched_off", "tasks_off", "too_many",
                 "too_long", "empty", "no_twin_there", "unknown_machine", "approval_denied", "approval_timeout"):
        assert said[code] != "not done here", code
    for code, line in said.items():
        # one short line in plain words: no bracket, no id, no code word, nothing of the engine
        assert len(line.split()) <= 14 and not re.search(r"[()\[\]{}_]|k-[0-9a-f]|m-[0-9a-f]|botport|\bcode\b", line), (code, line)
        assert line == line.strip() and not line.endswith(".")
    assert tasks.in_label("failed", None) == tasks.in_label("failed", "never-heard-of") == "not done here"
    # the states that are no failure are said as before
    assert tasks.in_label("done") == "done here" and tasks.in_label("running") == "working on it here"


def test_a_failed_request_in_the_log_carries_the_reason(linked):  # noqa: F811
    tasks.log_entry("in", "k-00000000000000a1", "mini", "Test Scout", "what os is this?", "running")
    tasks.log_update("in", "k-00000000000000a1", "failed", "task_failed", 2.0)
    tasks.log_entry("in", "k-00000000000000a2", "mini", "Test Scout", "what os is this?", "running")
    tasks.log_update("in", "k-00000000000000a2", "timeout", "task_timeout", 180.0)
    view = linked.tasks(refresh=False)
    assert shape_problems(view, "Tasks") == []
    rows = {r["id"]: r for r in view["log"]}
    assert rows["k-00000000000000a1"]["label"] == "not done: the twin here could not answer; open its chat to see why"
    assert rows["k-00000000000000a2"]["label"] == "not done: the twin here did not finish in time"
    # the asker's own line is unchanged
    assert tasks.label("failed", "Surface", None, "task_failed") == "not done on Surface: its twin there could not answer; its chat on Surface says why"


# ---- off again after an unlink ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("by", ["this", "source"])
def test_the_switch_is_off_again_once_this_computer_is_unlinked(linked, by):  # noqa: F811
    path = store.central_dir() / "tasks.json"
    assert tasks.set_switch(True)["on"] is True
    tasks.set_heard({SRC: True, OTHER: False})
    with fleet.locked():
        st = fleet.load()
        assert fleet.drop_link(st, by) == 2
        fleet.save(st)
    assert tasks.switch() == {"on": False, "since": None} and tasks.accepting() is False
    assert json.loads(path.read_text("utf-8")) == {"on": False, "since": None}
    assert tasks.heard() == {}  # ... and what was heard of the other computers' switches is forgotten
    view = linked.tasks(refresh=False)
    assert view["linked"] is False and view["switch"]["on"] is False and view["machines"] == []


def test_an_unlink_with_the_switch_off_writes_no_switch_file(linked):  # noqa: F811
    with fleet.locked():
        st = fleet.load()
        fleet.drop_link(st, "this")
        fleet.save(st)
    assert not (store.central_dir() / "tasks.json").exists() and not (store.central_dir() / "tasks-heard.json").exists()


# ---- which computers take requests ------------------------------------------------------------------------------------
def test_what_was_heard_of_the_other_computers_is_cleaned_and_believed_only_while_in_sync(linked):  # noqa: F811
    assert tasks.heard() == {} and tasks.set_heard({}) is False
    assert not (store.central_dir() / "tasks-heard.json").exists()  # nothing heard: nothing written
    # only fleet ids with a plain true or false are kept, whatever was sent
    sent = {SRC: True, OTHER: False, "m-dddddddddddd": "yes", "m-eeeeeeeeeeee": 1, "../x": True, "m-AAAAAAAAAAAA": True,
            7: True, "m-ffffffffffff\n": True}
    assert tasks.set_heard(sent) is True and tasks.heard() == {SRC: True, OTHER: False}
    assert tasks.set_heard(sent) is False  # unchanged: not written again
    for junk in (None, [], "x", 3, {"machines": {SRC: True}}):
        assert tasks.clean_heard(junk) == {}
    assert len(tasks.clean_heard({"m-%012x" % i: True for i in range(200)})) <= fleet.MAX_MACHINES + 1
    view = linked.tasks(refresh=False)
    assert shape_problems(view, "Tasks") == []
    got = {m["name"]: m["takes_requests"] for m in view["machines"]}
    assert got == {"mini": True, "Demo-Surface-Tablet": False, "MacBook Pro": None, "MacBook Air": None}
    # the sharing computer is not answering: nothing heard from it is passed off as current
    with fleet.locked():
        st = fleet.load()
        st["link"]["state"] = "offline"
        fleet.save(st)
    assert {m["takes_requests"] for m in linked.tasks(refresh=False)["machines"]} == {None}
    # a damaged file is "not known"
    (store.central_dir() / "tasks-heard.json").write_text("{not json", encoding="utf-8")
    assert tasks.heard() == {}


def test_the_mailbox_knows_each_linked_computers_switch_and_tells_the_others(home, monkeypatch):  # noqa: F811
    """The sharing computer's side, driven without a socket: who is known to take requests, what each linked
    computer is told, and that it is told again only when something changed."""
    config.set_override({"machine_name": "mini"})
    tasks_hub.reset()
    st = fleet._blank()
    st["machine"] = {"id": SRC, "name": "mini"}
    now = fleet.now_iso()
    st["share"] = {"on": True, "followers": {
        ME: {"id": ME, "name": "Surface", "platform": "windows", "key": "a" * 64, "last_seen_at": now},
        OTHER: {"id": OTHER, "name": "Orchard", "platform": "linux", "key": "b" * 64, "last_seen_at": now},
        "m-dddddddddddd": {"id": "m-dddddddddddd", "name": "MacBook Pro", "platform": "macos", "key": "c" * 64,
                           "last_seen_at": "2000-01-01T09:00:00Z"}}}
    monkeypatch.setattr(fleet, "fleet_members", lambda _st: [
        {"id": SRC, "name": "mini", "role": "source"}, {"id": ME, "name": "Surface", "role": "follower"},
        {"id": OTHER, "name": "Orchard", "role": "follower"},
        {"id": "m-dddddddddddd", "name": "MacBook Pro", "role": "follower"}])
    box = tasks_hub.hub
    # both keep their twins in step and fetch nothing: off (the default). The one not seen lately: not known
    assert box.picture(st) == {SRC: False, ME: False, OTHER: False}
    assert tasks.heard() == {SRC: False, ME: False, OTHER: False}  # kept for this computer's other Hermes processes
    assert box.news(ME, st) is None                      # told nothing yet: no early answer
    assert box.tell(ME, st) == {"takes_requests": {SRC: False, OTHER: False}}  # never its own switch
    assert box.news(ME, st) is None                      # nothing changed since
    seq = fleet.hub.seq
    box.poll({"id": OTHER, "name": "Orchard"}, {"accept": True, "wait_s": 0})   # Orchard switched on and fetches
    assert fleet.hub.seq == seq + 1                      # the waiting looks at the twin list are woken
    assert box.news(ME, st) == {"takes_requests": {SRC: False, OTHER: True}}
    assert box.news(ME, st) is None
    box.poll({"id": OTHER, "name": "Orchard"}, {"accept": True, "wait_s": 0})   # its next fetch: no news, no wake
    assert fleet.hub.seq == seq + 1
    tasks.set_switch(True)                               # this computer's own switch, from its own file
    assert box.news(ME, st) == {"takes_requests": {SRC: True, OTHER: True}}
    box.poll({"id": OTHER, "name": "Orchard"}, {"accept": False, "wait_s": 0})  # Orchard switched off
    assert box.news(ME, st) == {"takes_requests": {SRC: True, OTHER: False}} and fleet.hub.seq == seq + 2
    assert tasks.heard() == {SRC: True, ME: False, OTHER: False}
    box.forget(ME)                                       # unlinked: what it was told is forgotten with it
    assert ME not in box.told
    tasks_hub.reset()


def test_each_computer_sees_which_of_the_others_take_requests_and_a_new_link_starts_off(source, surface, home, fake,  # noqa: F811
                                                                                         monkeypatch):
    def takes(view, name):
        return next(m for m in view["machines"] if m["name"] == name)["takes_requests"]

    # (a linked computer is first told with the first "nothing changed" answer: at most one waiting look, 50 s;
    # shortened here)
    monkeypatch.setattr(follow, "LONG_WAIT_S", 2)
    # linked a moment ago, both switches off: each side says so about the other (never "not known" for long)
    wait_until(lambda: takes(surface.tasks(), "mini") is False, timeout=30, what="Surface to hear that mini's is off")
    wait_until(lambda: takes(source.json("requests"), "Surface") is False, timeout=30,
               what="mini to see that Surface fetches nothing")
    view = surface.tasks()
    assert shape_problems(view, "Tasks") == [] and shape_problems(source.json("requests"), "Tasks") == []
    # the signed "nothing changed" answer carries the key, and an answer to Surface never holds Surface's own
    # switch; the twin list itself carries nothing new
    mine = fleet.applied_report(fleet.load())
    status, obj, _ = wire_sync(source.port, _pass(home), have=None, applied=mine, name="Surface")
    assert status == 200 and obj["changed"] is True and "takes_requests" not in obj
    status, obj, _ = wire_sync(source.port, _pass(home), have=obj["rev"], applied=mine, name="Surface")
    assert status == 200 and obj["changed"] is False and obj["takes_requests"] == {_mid(source, "mini"): False}
    # Surface's owner switches it on: mini's page knows within the time of one fetch
    surface.tasks_switch(True)
    _listening(source, home)
    wait_until(lambda: takes(source.json("requests"), "Surface") is True, timeout=30,
               what="mini to see that Surface takes requests")
    # mini's owner switches mini's on in a terminal: Surface hears it without being asked anything
    source.run("requests", "on")
    wait_until(lambda: takes(surface.tasks(), "mini") is True, timeout=90, what="Surface to hear that mini's is on")
    # mini removes Surface. Surface's switch is off again, and stays off through a new link
    source.run("fleet", "remove", "Surface", "--yes")
    wait_until(lambda: surface.tasks()["linked"] is False, timeout=60, what="Surface to hear that it was unlinked")
    assert tasks.switch() == {"on": False, "since": None} and tasks.heard() == {}
    surface.fleet_link(source.code())
    view = wait_until(lambda: (lambda v: v if v["linked"] and v["machines"] else None)(surface.tasks()), timeout=30,
                      what="Surface linked again")
    assert view["switch"]["on"] is False and view["working"] is False
    wait_until(lambda: takes(source.json("requests"), "Surface") is False, timeout=60,
               what="mini to see that Surface takes none again")
    out = json.loads(source.run("ask", "Surface", ASK, "--twin", "Test Scout", "--json", check=False).stdout)
    assert out["state"] == "refused" and out["error"]["code"] == "tasks_off"  # off by default holds again


def test_a_request_that_fails_says_why_on_the_asked_computer_too(source, surface, home, fake):  # noqa: F811
    surface.tasks_switch(True)
    _listening(source, home)
    out = json.loads(source.run("ask", "Surface", "please fail", "--twin", "Test Scout", "--json", check=False).stdout)
    assert out["state"] == "failed" and out["error"]["message"] == "The twin on Surface could not answer."
    row = wait_until(lambda: next((r for r in surface.tasks()["log"] if r["dir"] == "in" and r["state"] == "failed"),
                                  None), timeout=30, what="the failed request in Surface's own list")
    assert row["label"] == "not done: the twin here could not answer; open its chat to see why"
    assert row["peer"] == "mini" and row["code"] == "task_failed"
