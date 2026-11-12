"""What happens on a computer when a step of a request needs approval: the two modes, and every way around them.

The mode is chosen ON the computer that runs requests ("Let my other computers ask this one to do things"):

- "here" (default): the step waits for the owner of that computer, there, and nothing about it travels.
- "safe": such a step is refused at once, with a plain reason; requests that only read still work.

("linked", approving from the computer that asked, was removed in 0.3.4: test_c2_f5.py.)

Real processes and sockets as in test_tasks_machines.py (mini shares, the test process is "Surface", "Orchard"
is a third computer), loopback only, invented twins only. The step in question is the stand-in's own (it is
never run by anything): see tasks_testlib.py. Other example steps here are `echo` lines.
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

import fleet_testlib
from fleet_testlib import shape_problems, wait_until
from tasks_testlib import approval_answers, rpc_record
from test_tasks_machines import (ASK, RISKY, _ask, _mid, _pass, _tok, _twin_id, approving, fake, orchard,  # noqa: F401
                                 source, surface, task)
from twin_testlib import config, home, service  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import tasks, tasks_hub
from hermes_plugins.grok_bot_twin.twin.connect import BadInput

ROOT = Path(__file__).resolve().parents[1]
fleet_testlib._SHAPES.update(json.loads((ROOT / "tests" / "contract" / "tasks_shapes.json").read_text("utf-8"))["shapes"])
STEP = "rm -rf ./scratch-folder"   # what the stand-in for Hermes asks about (tasks_testlib.FAKE_RPC); never run
MARK = "0a0b0c0d"


def _check(source, tid: str, wait: int = 30) -> dict:  # noqa: F811
    return json.loads(source.run("ask", "--check", tid, "--wait", str(wait), "--json", check=False).stdout)


# ---- the setting itself -----------------------------------------------------------------------------------------------
def test_the_mode_is_this_computers_own_choice_and_asking_here_is_the_default(source, surface, home):  # noqa: F811
    data = home / "plugin-data" / "grok-bot-twin"
    view = surface.tasks()
    assert shape_problems(view, "Tasks") == []
    assert view["mode"]["value"] == "here" and view["mode"]["label"] == "When a step needs approval"
    assert [(c["value"], c["label"]) for c in view["mode"]["choices"]] == [
        ("here", "Ask on this computer"), ("safe", "Only safe requests")]
    assert "warning" not in view["mode"] and "asking" not in view and not (data / "tasks.json").exists()
    # anything but a stored "safe" is "here": a missing, damaged or odd file never loosens the rule
    for text in ("{not json", '{"on": true, "mode": "LINKED"}', '{"on": true, "mode": true}', '["linked"]',
                 '{"on": true, "mode": ["linked"]}', '{"mode": "always"}', '{"on": true, "mode": "linked"}'):
        (data / "tasks.json").write_text(text, encoding="utf-8")
        assert tasks.mode() == "here", text
    (data / "tasks.json").unlink()
    for bad in ("always", "LINKED", "linked", "", None, True, 2, ["linked"]):
        with pytest.raises(BadInput):
            surface.tasks_mode(bad)
        with pytest.raises(ValueError):
            tasks.set_mode(bad)
    # choosing a mode does not switch requests on, and the switch does not change the mode
    assert surface.tasks_mode("safe")["mode"]["value"] == "safe" and tasks.accepting() is False
    assert surface.tasks_switch(True)["mode"]["value"] == "safe"
    assert surface.tasks_switch(False)["mode"]["value"] == "safe"
    assert json.loads((data / "tasks.json").read_text(encoding="utf-8")) == {"on": False, "since": None,
                                                                             "mode": "safe"}
    surface.tasks_mode("here")
    # the mode says nothing to any other computer: mini's own view is its own
    assert source.json("requests")["mode"]["value"] == "here"
    # unlinking puts it back: a later link starts as a first one does
    surface.tasks_switch(True)
    surface.fleet_unlink()
    assert tasks.mode() == "here" and tasks.accepting() is False
    # ... but never looser than it was: the stricter choice stays through an unlink (which another computer can cause)
    tasks.set_mode("safe")
    tasks.set_switch(True)
    tasks.link_dropped()
    assert tasks.mode() == "safe" and tasks.accepting() is False


def test_the_terminal_shows_the_mode(source, surface, home):  # noqa: F811
    cp = source.run("requests", "mode")
    assert "When a step needs approval, on mini:" in cp.stdout and " * here " in cp.stdout
    assert "linked" not in cp.stdout and "Only safe requests" in cp.stdout
    assert json.loads(source.run("requests", "mode", "safe", "--json").stdout)["mode"]["value"] == "safe"
    cp = source.run("requests", "mode", "everything", check=False)
    assert cp.returncode != 0 and source.json("requests")["mode"]["value"] == "safe"
    source.run("requests", "on")
    assert "When a step needs approval: Only safe requests." in source.run("requests").stdout
    # ... and none of it reached the other computer
    assert surface.tasks()["mode"]["value"] == "here"


# ---- attack: another computer flips the mode -----------------------------------------------------------------------------
def test_no_other_computer_and_no_request_can_set_the_mode(source, surface, orchard, home, fake):  # noqa: F811
    orchard.run("link", source.code())
    surface.tasks_switch(True)
    wait_until(lambda: len(surface.fleet()["machines"]) == 3, what="Surface to know Orchard")
    data = home / "plugin-data" / "grok-bot-twin"
    before = (data / "tasks.json").read_text(encoding="utf-8")
    mine, theirs = _pass(home), _tok(orchard.state())
    me = _mid(source, "Surface")
    # there is no route for it on the share server, signed or not, whatever it is called
    for path in ("mode", "switch", "settings", "requests/mode"):
        for token in (theirs, mine, None):
            assert task(source.port, path, {"mode": "safe", "to": me}, token)[0] == 404, path
    assert tasks_hub.ROUTES == ("/v1/task/ask", "/v1/task/wait", "/v1/task/poll", "/v1/task/update")
    # ... and a "mode" riding along in any real call changes nothing anywhere
    loose = {"mode": "safe", "approval_mode": "linked", "ask": {"q": MARK, "command": "echo hello"}, "linked": True}
    task(source.port, "poll", {"accept": True, "wait_s": 0, **loose}, theirs)
    status, obj, _ = task(source.port, "ask", {"to": me, "twin": _twin_id(home), "text": "say hello", **loose}, theirs)
    assert status == 200
    wait_until(lambda: [r for r in surface.tasks()["log"] if r["dir"] == "in" and r["state"] == "done"],
               what="the request to be done on Surface")
    task(source.port, "wait", {"id": obj["id"], "wait_s": 0, **loose}, theirs)
    task(source.port, "update", {"id": obj["id"], "state": "running", **loose}, theirs)
    assert tasks.mode() == "here" and (data / "tasks.json").read_text(encoding="utf-8") == before
    assert source.json("requests")["mode"]["value"] == "here"
    # the text of a request cannot do it either: its own chat turn is refused by the plugin's command ...
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(tasks.ASKED_ENV, "1")
        for value in ("safe", "here"):
            with pytest.raises(tasks.TaskError) as exc:
                surface.tasks_mode(value)
            assert exc.value.code == "not_from_a_request"
    tasks.mark_turn(["chat-of-a-request-0001"], True)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HERMES_SESSION_ID", "chat-of-a-request-0001")
        with pytest.raises(tasks.TaskError) as exc:
            surface.tasks_mode("safe")
        assert exc.value.code == "not_from_a_request"
    tasks.mark_turn(["chat-of-a-request-0001"], False)
    assert surface.tasks_mode("here")["mode"]["value"] == "here"


# ---- mode "here" (default): today's behaviour ---------------------------------------------------------------------------
def test_asking_here_nothing_about_the_step_travels_and_the_asking_computer_cannot_answer(approving, source, home,  # noqa: F811
                                                                                         fake):  # noqa: F811
    out = _ask(source, RISKY)
    assert out["state"] == "waiting_approval" and "approval" not in out and STEP not in json.dumps(out)
    assert "Approve or refuse it there" in out["hint"]
    assert "asking" not in source.json("requests") and not (source.data / "tasks-asking.json").exists()
    (w,) = approving.tasks()["waiting"]
    # the asking computer knows the mark (say it was read off the other screen): still no way to answer from there
    for words in (("approve", out["id"], "--step", w["q"]), ("deny", out["id"], "--step", w["q"]),
                  ("approve", out["id"])):
        cp = source.run("requests", *words, check=False)
        assert cp.returncode != 0 and "no longer known" in (cp.stdout + cp.stderr), words
    time.sleep(1.2)
    assert approval_answers(rpc_record(fake)) == [] and approving.tasks()["waiting"][0]["q"] == w["q"]
    approving.tasks_approve(out["id"], "once", w["q"])  # the owner, on Surface
    done = _check(source, out["id"])
    assert done["state"] == "done" and done["reply"] == "did the risky thing"
    row = approving.tasks()["log"][0]
    assert "answered" not in row and row["label"] == "done here"


def test_a_step_that_waits_here_is_not_answered_by_a_command_that_a_chat_started(approving, home):  # noqa: F811
    """`hermes twin requests approve ID` from the owner's own terminal answers a step that waits on this computer.
    The same command started by a chat here (Hermes hands that chat's number to what it starts) is refused before
    anything is written; the Twins page's own route is no chat."""
    answered: list = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tasks, "decide", lambda *a, **k: answered.append((a, k)))
        mp.setenv("HERMES_SESSION_ID", "an-ordinary-chat-0003")
        with pytest.raises(tasks.TaskError) as exc:
            approving.tasks_approve("k-00000000000000d5", "once", None)
        assert exc.value.code == "answer_owner_only" and answered == []
        mp.delenv("HERMES_SESSION_ID")
        approving.tasks_approve("k-00000000000000d5", "once", None)
        assert len(answered) == 1


def test_a_request_cannot_answer_its_own_step_from_either_computer(home, tmp_path, monkeypatch):  # noqa: F811
    """The text of a request says: answer the step yourself. On the asked computer its turn has the plugin's
    commands within reach; none of them answers."""
    from test_security_twins import _linked
    from hermes_plugins.grok_bot_twin.twin import slash, tasks_run
    svc = _linked(home, tmp_path)
    tasks.set_switch(True)
    tid = "k-00000000000000b7"
    tasks.log_entry("in", tid, "mini", "Test Scout", "tidy up", "waiting_approval")
    tasks.set_pending(tid, {"command": "echo hello", "description": "a step"})
    q = tasks.pending()[tid]["q"]
    # (no worker in this test: a starting worker tidies away requests left open, this made-up one included)
    monkeypatch.setattr(tasks_run, "ensure_started", lambda get_service: False)
    monkeypatch.setenv(tasks.ASKED_ENV, "1")
    for call in (lambda: svc.tasks_approve(tid, "once", q), lambda: svc.tasks_approve(tid, "once"),
                 lambda: tasks.decide(tid, "once", q), lambda: svc.tasks_mode("here")):
        with pytest.raises(tasks.TaskError) as exc:
            call()
        assert exc.value.code == "not_from_a_request"
    assert slash.handle(f"approve {q}", platform="desktop") == slash.HELP  # (/twin approve is gone)
    assert tasks.decision(tid) is None
    monkeypatch.delenv(tasks.ASKED_ENV)
    svc.tasks_approve(tid, "once", q)  # the owner of this computer, on its own page or terminal
    assert tasks.decision(tid) == "once"


# ---- mode "safe" -------------------------------------------------------------------------------------------------------------
def test_only_safe_requests_a_step_that_needs_approval_is_refused_at_once_and_reading_still_works(  # noqa: F811
        approving, source, home, fake):
    assert approving.tasks_mode("safe")["mode"]["value"] == "safe"
    began = time.monotonic()
    out = _ask(source, RISKY, "--wait", "30")
    # refused at once: nothing waited for anybody, and the asker hears why in plain words
    assert out["state"] == "done" and time.monotonic() - began < 25, out
    assert out["label"].startswith("done on Surface - ") and out["label"].endswith(
        "; a step was refused: Surface takes only safe requests")
    assert out["hint"].startswith("Surface takes only safe requests, so a step that needed approval was refused "
                                  "at once.") and "approval" not in out
    assert out["reply"].startswith("BLOCKED: Command denied by user.")
    assert approval_answers(rpc_record(fake)) == ["deny"]
    view = approving.tasks()
    assert view["waiting"] == [] and tasks._approvals() == {"pending": {}, "decisions": {}}
    assert view["log"][0]["label"] == "done here; a step was refused: only safe requests are taken here"
    assert view["log"][0]["state"] == "done" and view["log"][0]["code"] == "only_safe"
    shown = source.run("ask", "--check", out["id"]).stdout
    assert "a step was refused: Surface takes only safe requests" in shown
    # nothing can be answered: there is no question, here or there
    for words in (("approve", out["id"]), ("approve", out["id"], "--step", MARK)):
        assert source.run("requests", *words, check=False).returncode != 0
    with pytest.raises(tasks.TaskError):
        approving.tasks_approve(out["id"], "once")
    # a request that needs no approval works as before
    out = _ask(source, ASK, "--wait", "30")
    assert out["state"] == "done" and out["reply"] == "test-scout heard: " + ASK
    assert out["label"].startswith("done on Surface - ") and "refused" not in out["label"]
    # back to "here": the next such step waits for the owner again
    approving.tasks_mode("here")
    out = _ask(source, RISKY)
    assert out["state"] == "waiting_approval" and "approval" not in out
    # ... and choosing "Only safe requests" while it waits ends the wait: refused, with the same reason
    approving.tasks_mode("safe")
    done = _check(source, out["id"])
    assert done["state"] == "done" and done["label"].endswith("; a step was refused: Surface takes only safe requests")
    assert approval_answers(rpc_record(fake)) == ["deny"] and approving.tasks()["waiting"] == []


def test_a_padded_step_is_never_shown_cut_to_either_computer():
    """A step is padded so that the part that matters lies beyond what a page shows: blanks, new lines or
    characters that take no room fold away, and the beginning alone looked like the whole step. Such a step is
    not whole, so it is refused before anyone is asked."""
    tail = "; echo this-part-was-never-shown"
    for pad in (" " * 2000, "\n" * 1700, "\u200b" * 2000, "\t \t" * 700):
        step = "echo hello" + pad + tail
        assert tasks.step_whole(step) is False, repr(pad[:4])
    # padding that still fits is shown with everything behind it
    step = "echo hello" + " " * 1500 + tail
    assert tasks.step_whole(step) is True and tasks.step_text(step) == "echo hello " + tail.strip()


# ---- the page's route ----------------------------------------------------------------------------------------------------
def test_rest_mode_route(home, tmp_path, monkeypatch):  # noqa: F811
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from test_security_twins import _linked
    _linked(home, tmp_path)
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_modes_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/p")
    c = TestClient(app)
    view = c.get("/p/tasks").json()
    assert shape_problems(view, "Tasks") == [] and view["mode"]["value"] == "here"
    for bad in ({}, {"mode": "always"}, {"mode": True}, {"value": "linked"}, {"mode": ["linked"]}, {"mode": "linked"}):
        assert c.post("/p/tasks/mode", json=bad).status_code == 400
    assert tasks.mode() == "here"
    # the page is the owner's: it may choose a mode even while a chat works in that Hermes
    monkeypatch.setenv("HERMES_SESSION_ID", "an-ordinary-chat-0003")
    view = c.post("/p/tasks/mode", json={"mode": "safe"}).json()
    assert shape_problems(view, "Tasks") == [] and view["mode"]["value"] == "safe" and tasks.mode() == "safe"
    assert c.post("/p/tasks/mode", json={"mode": "here"}).json()["mode"]["value"] == "here"
    # answering a step on another computer is gone with "linked"
    assert c.post("/p/tasks/k-00000000000000c7/answer", json={"choice": "once", "q": MARK}).status_code in (404, 405)
