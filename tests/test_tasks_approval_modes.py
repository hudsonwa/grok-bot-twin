"""What happens on a computer when a step of a request needs approval: the three modes, and every way around them.

The mode is chosen ON the computer that runs requests ("Let my other computers ask this one to do things"):

- "here" (default): the step waits for the owner of that computer, there, and nothing about it travels.
- "linked": the computer that asked is shown the step and may answer it. The answer names the question's mark,
  travels signed over the link, counts only while that very question is open, and is logged on both computers.
- "safe": such a step is refused at once, with a plain reason; requests that only read still work.

Real processes and sockets as in test_tasks_machines.py (mini shares, the test process is "Surface", "Orchard"
is a third computer), loopback only, invented twins only. The step in question is the stand-in's own (it is
never run by anything): see tasks_testlib.py. Other example steps here are `echo` lines.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

import fleet_testlib
from fleet_testlib import shape_problems, wait_until, wire
from tasks_testlib import approval_answers, rpc_record
from test_tasks_machines import (ASK, RISKY, _ask, _mid, _pass, _tok, _twin_id, approving, fake, orchard,  # noqa: F401
                                 source, surface, task)
from twin_testlib import config, home, service  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import fleet, fleet_follow as follow, slash, tasks, tasks_api, tasks_hub
from hermes_plugins.grok_bot_twin.twin import tasks_run
from hermes_plugins.grok_bot_twin.twin.connect import BadInput

ROOT = Path(__file__).resolve().parents[1]
fleet_testlib._SHAPES.update(json.loads((ROOT / "tests" / "contract" / "tasks_shapes.json").read_text("utf-8"))["shapes"])
STEP = "rm -rf ./scratch-folder"   # what the stand-in for Hermes asks about (tasks_testlib.FAKE_RPC); never run
MARK = "0a0b0c0d"
ECHO = {"q": MARK, "command": "echo hello", "description": "a step"}


def _other(mark: str) -> str:
    return "00000000" if mark != "00000000" else "11111111"


def _mailbox(sent: list, shown=None):
    """A stand-in for this computer's calls to the mailbox: a look at a request says "a step waits" (with the
    step, when `shown`), or cannot be made at all; everything else is written down and accepted."""
    def call(st, path, body, **kw):
        if path == "/v1/task/wait":
            if shown is None:
                raise follow.Unreachable()
            return 200, {"id": body["id"], "state": "waiting_approval", "approval": dict(shown)}, None
        if path == "/v1/task/answer":
            sent.append((path, {k: body.get(k) for k in ("id", "q", "choice")}))
        return 200, {"ok": True, "task": None}, None
    return call


def _check(source, tid: str, wait: int = 30) -> dict:  # noqa: F811
    return json.loads(source.run("ask", "--check", tid, "--wait", str(wait), "--json", check=False).stdout)


# ---- the setting itself -----------------------------------------------------------------------------------------------
def test_the_mode_is_this_computers_own_choice_and_asking_here_is_the_default(source, surface, home):  # noqa: F811
    data = home / "plugin-data" / "grok-bot-twin"
    view = surface.tasks()
    assert shape_problems(view, "Tasks") == []
    assert view["mode"]["value"] == "here" and view["mode"]["label"] == "When a step needs approval"
    assert [(c["value"], c["label"]) for c in view["mode"]["choices"]] == [
        ("here", "Ask on this computer"), ("linked", "Let my own linked computers approve"),
        ("safe", "Only safe requests")]
    # one sentence of warning, for the page and the terminal to show before "linked" is turned on
    warning = view["mode"]["warning"]
    assert warning.endswith(".") and warning.count(". ") == 0 and "linked computers" in warning
    assert view["asking"] == [] and not (data / "tasks.json").exists()
    # anything but a stored "linked" or "safe" is "here": a missing, damaged or odd file never loosens the rule
    for text in ("{not json", '{"on": true, "mode": "LINKED"}', '{"on": true, "mode": true}', '["linked"]',
                 '{"on": true, "mode": ["linked"]}', '{"mode": "always"}'):
        (data / "tasks.json").write_text(text, encoding="utf-8")
        assert tasks.mode() == "here", text
    (data / "tasks.json").unlink()
    for bad in ("always", "LINKED", "", None, True, 2, ["linked"]):
        with pytest.raises(BadInput):
            surface.tasks_mode(bad)
        with pytest.raises(ValueError):
            tasks.set_mode(bad)
    # choosing a mode does not switch requests on, and the switch does not change the mode
    assert surface.tasks_mode("safe")["mode"]["value"] == "safe" and tasks.accepting() is False
    assert surface.tasks_switch(True)["mode"]["value"] == "safe"
    assert surface.tasks_mode("linked")["mode"]["value"] == "linked" and tasks.accepting() is True
    assert surface.tasks_switch(False)["mode"]["value"] == "linked"
    assert json.loads((data / "tasks.json").read_text(encoding="utf-8")) == {"on": False, "since": None,
                                                                             "mode": "linked"}
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


def test_the_terminal_shows_the_mode_and_warns_once_when_linked_is_turned_on(source, surface, home):  # noqa: F811
    cp = source.run("requests", "mode")
    assert "When a step needs approval, on mini:" in cp.stdout and " * here " in cp.stdout
    assert "Let my own linked computers approve" in cp.stdout and "Only safe requests" in cp.stdout
    cp = source.run("requests", "mode", "linked")
    assert cp.stdout.splitlines()[0] == tasks.MODE_WARNING and " * linked " in cp.stdout
    assert tasks.MODE_WARNING not in source.run("requests", "mode", "linked").stdout  # (it was on already)
    assert source.json("requests")["mode"]["value"] == "linked"
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
            assert task(source.port, path, {"mode": "linked", "to": me}, token)[0] == 404, path
    assert tasks_hub.ROUTES == ("/v1/task/ask", "/v1/task/wait", "/v1/task/poll", "/v1/task/update",
                                "/v1/task/answer", "/v1/task/answered")
    # ... and a "mode" riding along in any real call changes nothing anywhere
    loose = {"mode": "linked", "approval_mode": "linked", "ask": dict(ECHO), "linked": True}
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
        for value in ("linked", "safe", "here"):
            with pytest.raises(tasks.TaskError) as exc:
                surface.tasks_mode(value)
            assert exc.value.code == "not_from_a_request"
    tasks.mark_turn(["chat-of-a-request-0001"], True)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HERMES_SESSION_ID", "chat-of-a-request-0001")
        with pytest.raises(tasks.TaskError) as exc:
            surface.tasks_mode("linked", typed=True)
        assert exc.value.code == "not_from_a_request"
    tasks.mark_turn(["chat-of-a-request-0001"], False)
    # ... and "linked" is not for any command a chat starts, the owner's own chats included: only the owner's
    # page, own terminal, or a line the owner typed turns it on
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HERMES_SESSION_ID", "an-ordinary-chat-0001")
        with pytest.raises(tasks.TaskError) as exc:
            surface.tasks_mode("linked")
        assert exc.value.code == "mode_owner_only" and tasks.mode() == "here"
        assert surface.tasks_mode("safe")["mode"]["value"] == "safe"  # (stricter is always allowed)
        assert surface.tasks_mode("linked", own_page=True)["mode"]["value"] == "linked"
    assert surface.tasks_mode("here")["mode"]["value"] == "here"


# ---- mode "here" (default): today's behaviour ---------------------------------------------------------------------------
def test_asking_here_nothing_about_the_step_travels_and_the_asking_computer_cannot_answer(approving, source, home,  # noqa: F811
                                                                                         fake):  # noqa: F811
    out = _ask(source, RISKY)
    assert out["state"] == "waiting_approval" and "approval" not in out and STEP not in json.dumps(out)
    assert "Approve or refuse it there" in out["hint"]
    assert source.json("requests")["asking"] == [] and not (source.data / "tasks-asking.json").exists()
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
    assert row["answered"] is None and row["label"] == "done here"


# ---- mode "linked" ---------------------------------------------------------------------------------------------------------
def test_linked_the_asking_computer_is_shown_the_exact_step_and_its_answer_counts_for_that_step_only(  # noqa: F811
        approving, source, home, fake):
    assert approving.tasks_mode("linked")["mode"]["value"] == "linked"
    out = _ask(source, RISKY)
    assert out["state"] == "waiting_approval" and out["label"] == "waiting for approval on Surface"
    (w,) = approving.tasks()["waiting"]
    # the step travels exactly as Surface's own page shows it, under the question's mark
    assert out["approval"] == {"command": STEP, "description": "recursive delete"} == {
        k: w[k] for k in ("command", "description")}
    # ... but not in the result a model reads: there the owner is pointed to the line they type themselves
    assert "/twin approve" in out["hint"] and "Only the user can answer" in out["hint"]
    assert w["q"] not in json.dumps(out)
    view = source.json("requests")
    assert shape_problems(view, "Tasks") == []
    (a,) = view["asking"]
    assert a == {"id": out["id"], "peer": "Surface", "twin": "Test Scout", "text": RISKY, "command": STEP,
                 "description": "recursive delete", "asked_at": a["asked_at"], "q": w["q"]}
    shown = source.run("requests").stdout
    assert f"WAITING FOR YOU ON Surface" in shown and STEP in shown
    assert f"hermes twin requests approve {out['id']} --step {w['q']}" in shown
    # attack: a wrong question id. Refused on the asking computer, and Hermes on Surface goes on waiting
    cp = source.run("requests", "approve", out["id"], "--step", _other(w["q"]), check=False)
    assert cp.returncode != 0 and "not the one you answered" in (cp.stdout + cp.stderr)
    cp = source.run("requests", "approve", out["id"], check=False)  # no mark at all: not an answer from afar
    assert cp.returncode != 0 and "Answer it with its mark" in (cp.stdout + cp.stderr)
    time.sleep(1.2)
    assert approval_answers(rpc_record(fake)) == [] and approving.tasks()["waiting"][0]["q"] == w["q"]
    # the owner answers on the asking computer, naming the step that was shown
    cp = source.run("requests", "approve", out["id"], "--step", w["q"], check=False)
    # (said as what it is on this side: an answer on its way; whether it counted is the asked computer's to say)
    assert cp.returncode == 0 and cp.stdout.startswith("Sent to Surface: do that one step once."), cp.stdout + cp.stderr
    done = _check(source, out["id"])
    assert done["state"] == "done" and done["reply"] == "did the risky thing" and "approval" not in done
    assert approval_answers(rpc_record(fake)) == ["once"]  # one answer, and only "once"
    # logged on both computers: what was answered, and on which computer
    row = approving.tasks()["log"][0]
    assert row["answered"] == {"choice": "once", "by": "mini"}
    assert row["label"] == "done here; a step was approved from mini" and row["approval"] is None
    mrow = source.json("requests")["log"][0]
    assert mrow["answered"] == {"choice": "once", "by": "mini"}
    assert mrow["label"].startswith("done on Surface - ") and mrow["label"].endswith(
        "; you answered a step from this computer: do it once")
    # it expired with its question: nothing is left to answer on either side, and the answer does not count again
    assert source.json("requests")["asking"] == [] and approving.tasks()["waiting"] == []
    assert tasks._approvals() == {"pending": {}, "decisions": {}}
    cp = source.run("requests", "approve", out["id"], "--step", w["q"], check=False)
    assert cp.returncode != 0 and "no longer known" in (cp.stdout + cp.stderr)
    assert approval_answers(rpc_record(fake)) == ["once"]


def test_linked_a_refusal_from_the_asking_computer_and_the_owner_there_still_comes_first(approving, source, home,  # noqa: F811
                                                                                        fake):  # noqa: F811
    approving.tasks_mode("linked")
    out = _ask(source, RISKY)
    q = source.json("requests")["asking"][0]["q"]
    cp = source.run("requests", "deny", out["id"], "--step", q, check=False)
    assert cp.returncode == 0 and cp.stdout.startswith("Sent to Surface: don't do that step.")
    done = _check(source, out["id"])
    assert done["state"] == "done" and done["reply"].startswith("BLOCKED: Command denied by user.")
    assert approval_answers(rpc_record(fake)) == ["deny"]
    assert approving.tasks()["log"][0]["label"] == "done here; a step was refused from mini"
    assert source.json("requests")["log"][0]["label"].endswith("; you answered a step from this computer: don't")
    # the owner of the asked computer can still answer there, and whoever answers first is the answer
    out = _ask(source, RISKY)
    q = source.json("requests")["asking"][0]["q"]
    approving.tasks_approve(out["id"], "deny", q)
    done = _check(source, out["id"])
    assert done["state"] == "done" and done["label"].endswith("; a step was not approved there")
    cp = source.run("requests", "approve", out["id"], "--step", q, check=False)
    assert cp.returncode != 0
    assert approval_answers(rpc_record(fake)) == ["deny"] and approving.tasks()["log"][0]["answered"] is None


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


def test_linked_in_the_chat_only_the_owner_answers_never_the_model_and_never_a_messaging_chat(approving, source,  # noqa: F811
                                                                                             home, fake):  # noqa: F811
    """The asking computer's own doors, tried on a made-up step that waits on "mini" for a request from here."""
    tid = "k-00000000000000d4"
    tasks.log_entry("out", tid, "mini", "Test Scout", "say hello", "waiting_approval")
    assert tasks.set_asking(tid, dict(ECHO)) is True
    (a,) = approving.tasks(refresh=False)["asking"]
    assert (a["id"], a["peer"], a["command"], a["q"]) == (tid, "mini", "echo hello", MARK)
    sent: list = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tasks_run, "_call", _mailbox(sent, ECHO))
        mp.setattr(type(approving), "tasks_check", lambda self, task_id, wait_s=0: {"label": "done on mini - 2 s",
                                                                                   "state": "done"})
        # the tool a model calls has no way to answer: its arguments are the four it always had ...
        assert set(tasks_api.TOOL_SCHEMA["parameters"]["properties"]) == {"machine", "request", "twin", "check"}
        assert "you cannot answer for them" in tasks_api.TOOL_SCHEMA["description"]
        # ... a command started by a chat is refused (Hermes hands the chat's number to what it starts) ...
        mp.setenv("HERMES_SESSION_ID", "an-ordinary-chat-0002")
        for call in (lambda: approving.tasks_answer(tid, "once", MARK),
                     lambda: approving.tasks_approve(tid, "once", MARK)):
            with pytest.raises(tasks.TaskError) as exc:
                call()
            assert exc.value.code == "answer_owner_only"
        # ... a messaging chat cannot type it ...
        for platform in ("telegram", "discord", "api_server", "webhook"):
            assert "From a messaging chat" in slash.handle(f"approve {MARK}", platform=platform)
        assert sent == []
        # ... without the mark nothing is answered: the step and the line to type are shown again ...
        said = slash.handle("approve", platform="desktop")
        assert "echo hello" in said and f"/twin approve {MARK}" in said and sent == []
        assert "Say which step" in slash.handle("approve ffffffff", platform="desktop") and sent == []
        # ... and the request's own turn is refused even when it "types"
        tasks.mark_turn(["an-ordinary-chat-0002"], True)
        assert "cannot approve steps" in slash.handle(f"approve {MARK}", platform="desktop") and sent == []
        tasks.mark_turn(["an-ordinary-chat-0002"], False)
        # the owner types the line the chat showed: one signed answer leaves, with the mark
        said = slash.handle(f"approve {MARK}", platform="desktop")
        assert said.splitlines()[0] == "Sent to mini: do that one step once." and "done on mini" in said
        assert sent == [("/v1/task/answer", {"id": tid, "q": MARK, "choice": "once"})]
    assert approving.tasks(refresh=False)["asking"] == []
    assert "No step on another computer waits" in slash.handle(f"approve {MARK}", platform="desktop")


def test_the_asked_computer_checks_every_answer_again_by_its_own_files(approving, source, home, fake,  # noqa: F811
                                                                       monkeypatch):
    """The mailbox is not trusted for it. An answer handed to the asked computer counts only when: the mode is
    "linked" there, it names the open question's mark, it comes from the computer that asked this very request,
    and that computer is linked now."""
    monkeypatch.setattr(tasks_run, "_rate", tasks.Rate(100, 1000))
    monkeypatch.setattr(tasks_run, "ANSWER_RETRY_S", 0.1)
    mini, twin = _mid(source, "mini"), _twin_id(home)
    n = {"i": 0}

    def turn(answer_for, during=None):
        n["i"] += 1
        tid = "k-%016x" % (0xe0 + n["i"])
        reports: list = []
        asked: list = []

        def report(tid_, state, code, seconds, reply, ask=None):
            reports.append((state, code, ask))

        def answered(tid_, q, wait_s):
            asked.append(q)
            return answer_for(q)

        th = threading.Thread(target=tasks_run.do_task, daemon=True, args=(
            approving, {"id": tid, "from": {"id": mini, "name": "whatever it calls itself"}, "twin": twin,
                        "text": RISKY}, report, lambda: False, None, answered))
        th.start()
        # (a good answer is taken within a moment, so the mark is read from what was reported, not from the file)
        wait_until(lambda: [r for r in reports if r[0] == "waiting_approval"], what="the step to wait")
        sent_q = [r[2]["q"] for r in reports if r[0] == "waiting_approval" and isinstance(r[2], dict)]
        q = sent_q[0] if sent_q else tasks.pending()[tid]["q"]
        if during:
            during()
        time.sleep(1.0)
        if tid in tasks.pending():
            approving.tasks_approve(tid, "deny", q)  # (nothing was taken: the owner here ends it)
        th.join(20)
        assert not th.is_alive()
        return {"tid": tid, "q": q, "reports": reports, "asked": asked,
                "answers": approval_answers(rpc_record(fake))}

    good = lambda q: {"q": q, "choice": "once", "by": {"id": mini, "name": "mini"}}  # noqa: E731
    # mode "here": the question is not sent, and nobody is asked for an answer
    r = turn(good)
    assert r["asked"] == [] and r["answers"] == ["deny"]
    # (said in so many words, "not shown to the asking computer": the mailbox then offers nothing, old or new)
    assert [x for x in r["reports"] if x[0] == "waiting_approval"] == [("waiting_approval", None, False)]
    approving.tasks_mode("linked")
    # the question is sent with its mark; a good answer counts once
    r = turn(good)
    assert r["answers"] == ["once"]
    assert ("waiting_approval", None, {"q": r["q"], "command": STEP, "description": "recursive delete"}) in r["reports"]
    assert tasks.log_view()[0]["answered"] == {"choice": "once", "by": "mini"}
    # attack: an answer for another mark, an answer that is not "once" or "deny", an answer without a sender
    for bad in (lambda q: {"q": _other(q), "choice": "once", "by": {"id": mini}},
                lambda q: {"q": q, "choice": "always", "by": {"id": mini}},
                lambda q: {"q": q, "choice": "once"}):
        r = turn(bad)
        assert r["answers"] == ["deny"], r
    # attack: an answer from a computer that did not ask this request (another linked one, or one made up)
    for who in (_mid(source, "Surface"), "m-0123456789ab"):
        r = turn(lambda q, who=who: {"q": q, "choice": "once", "by": {"id": who, "name": "mini"}})
        assert r["answers"] == ["deny"], who
    # attack: the computer that asked is not linked any more when its answer arrives
    real = tasks.machines
    state = {"linked": True}
    monkeypatch.setattr(tasks, "machines", lambda st: real(st) if state["linked"] else [
        m for m in real(st) if m["id"] != mini])
    hold = {"go": False}
    r = turn(lambda q: good(q) if hold["go"] else None,
             during=lambda: (state.update(linked=False), hold.update(go=True)))
    assert r["answers"] == ["deny"]
    state["linked"] = True
    # the owner changes the mode back while a step waits: the answer no longer counts, the step is taken back
    hold["go"] = False
    r = turn(lambda q: good(q) if hold["go"] else None,
             during=lambda: (approving.tasks_mode("here"), hold.update(go=True)))
    assert r["answers"] == ["deny"]
    assert ("waiting_approval", None, False) in r["reports"]
    # and decide_remote by itself: each condition, read from this computer's own files
    tid = "k-00000000000000f9"
    approving.tasks_mode("linked")
    tasks.set_pending(tid, {"command": "echo hello", "description": "a step"}, shared=True)
    q = tasks.pending()[tid]["q"]
    approving.tasks_mode("safe")
    assert tasks.decide_remote(tid, "once", q, "mini") is False            # the mode is not "linked" any more
    approving.tasks_mode("linked")
    assert tasks.decide_remote(tid, "once", q, "mini") is False            # (and that took the offer back for good)
    tasks.set_pending(tid, {"command": "echo hello", "description": "a step"}, shared=True)
    q = tasks.pending()[tid]["q"]
    for args in ((tid, "once", _other(q)), (tid, "always", q), (tid, "once", None), (tid, "once", q.upper()),
                 ("k-00000000000000aa", "once", q), (None, "once", q)):
        assert tasks.decide_remote(*args, "mini") is False, args
    tasks.set_pending(tid, {"command": "echo hello", "description": "a step"})  # a question that was not offered
    assert tasks.decide_remote(tid, "once", tasks.pending()[tid]["q"], "mini") is False
    tasks.set_pending(tid, {"command": "echo hello", "description": "a step"}, shared=True)
    q = tasks.pending()[tid]["q"]
    tasks.decide(tid, "deny", q)                                             # the owner here answered first
    assert tasks.decide_remote(tid, "once", q, "mini") is False and tasks.decision(tid) == "deny"
    tasks.set_pending(tid, {"command": "echo hello", "description": "a step"}, shared=True)
    q = tasks.pending()[tid]["q"]
    assert tasks.decide_remote(tid, "once", q, "mini") is True and tasks.decision(tid) == "once"
    # ... and the first answer counts in this direction too: one that came from the asking computer is not
    # replaced by a later one here, with or without the mark
    for late in (lambda: tasks.decide(tid, "deny", q), lambda: tasks.decide(tid, "deny")):
        with pytest.raises(tasks.TaskError) as exc:
            late()
        assert exc.value.code == "step_changed" and tasks.decision(tid) == "once"
    tasks.set_pending(tid, None)


# ---- attacks on the wire: replay, wrong question, unlinked computer, a request approving itself -----------------------------
def test_an_answer_on_the_wire_is_for_one_open_question_from_the_one_computer_that_asked(source, surface, orchard,  # noqa: F811
                                                                                        home, fake):  # noqa: F811
    """Orchard asks Surface. Surface's side is played by hand here (signed calls with its own key), so that
    every answer and every refusal of mini's mailbox can be seen."""
    orchard.run("link", source.code())
    wait_until(lambda: len(surface.fleet()["machines"]) == 3, what="Surface to know Orchard")
    mine, theirs = _pass(home), _tok(orchard.state())
    me = _mid(source, "Surface")
    port = source.port
    assert task(port, "poll", {"accept": True, "wait_s": 0}, mine)[0] == 200
    tid = task(port, "ask", {"to": me, "twin": _twin_id(home), "text": "say hello"}, theirs)[1]["id"]
    assert task(port, "poll", {"accept": True, "wait_s": 0}, mine)[1]["task"]["id"] == tid
    ans = {"id": tid, "q": MARK, "choice": "once"}
    # no step waits: nothing to answer
    assert task(port, "answer", ans, theirs)[1]["error"]["code"] == "step_changed"
    # a step waits and Surface did NOT offer it (mode "here"): it cannot be answered from anywhere else
    assert task(port, "update", {"id": tid, "state": "waiting_approval"}, mine)[0] == 200
    status, obj, _ = task(port, "answer", ans, theirs)
    assert status == 409 and obj["error"]["code"] == "approve_there"
    assert "approval" not in task(port, "wait", {"id": tid, "wait_s": 0}, theirs)[1]
    # Surface offers it (mode "linked"): only the computer that asked is shown it
    assert task(port, "update", {"id": tid, "state": "waiting_approval", "ask": dict(ECHO)}, mine)[0] == 200
    assert task(port, "wait", {"id": tid, "wait_s": 0}, theirs)[1]["approval"] == ECHO
    assert task(port, "wait", {"id": tid, "wait_s": 0}, mine)[0] == 404
    # attack: an unlinked computer (no signature, a wrong key, the right key under another id)
    mid, pass_ = theirs.split("|")
    for token in (None, "x" * 43, mid + "|" + pass_[:-1] + ("A" if pass_[-1] != "A" else "B"),
                  "m-0123456789ab|" + pass_):
        for path, body in (("answer", ans), ("answered", {"id": tid, "q": MARK, "wait_s": 0})):
            status, obj, _ = task(port, path, body, token)
            assert status == 401 and obj["error"] == {"code": "unlinked"}, (path, token)
    # attack: a wrong question id; a choice that is no choice; a mark that is no mark
    for bad, code in (({**ans, "q": "ffffffff"}, "step_changed"), ({**ans, "choice": "always"}, "bad_request"),
                      ({**ans, "choice": "session"}, "bad_request"), ({**ans, "q": "0A0B0C0D"}, "bad_request"),
                      ({**ans, "q": None}, "bad_request"), ({"id": tid, "choice": "once"}, "bad_request"),
                      ({**ans, "id": "k-ffffffffffffffff"}, "unknown_task")):
        assert task(port, "answer", bad, theirs)[1]["error"]["code"] == code, bad
    # attack: the request approving itself. The asked computer (where the request's turn runs, with that
    # computer's key within reach of whatever the turn may run) answers its own question: refused
    status, obj, _ = task(port, "answer", ans, mine)
    assert status == 404 and obj["error"]["code"] == "unknown_task"
    # ... and nothing was kept for it so far
    got: dict = {}
    status, obj, _ = task(port, "answered", {"id": tid, "q": MARK, "wait_s": 0}, mine, answer=got)
    assert status == 200 and obj["answer"] is None and got["signed"] is True
    # the answer of the computer that asked, signed by it: kept. Sent again as recorded: refused (replay)
    full = {"proto": fleet.PROTO, "at": int(time.time()), **ans}
    data = json.dumps(full).encode()
    nonce = fleet.new_nonce()
    hdrs = {"X-GBT-Machine": mid, "X-GBT-Nonce": nonce,
            "X-GBT-Mac": fleet.request_mac(fleet.share_key(pass_), "/v1/task/answer", nonce, data)}
    assert wire("POST", port, "/v1/task/update", raw_body=data, headers=hdrs)[0] == 401   # good for its path only
    assert wire("POST", port, "/v1/task/answer", raw_body=data + b" ", headers=hdrs)[0] == 401  # and its body
    assert wire("POST", port, "/v1/task/answer", raw_body=data, headers=hdrs)[0] == 200
    status, obj, _ = wire("POST", port, "/v1/task/answer", raw_body=data, headers=hdrs)
    assert status == 409 and obj["error"]["code"] == "replayed"
    # a second, fresh answer does not replace the first
    assert task(port, "answer", {**ans, "choice": "deny"}, theirs)[1]["error"]["code"] == "step_changed"
    assert "approval" not in task(port, "wait", {"id": tid, "wait_s": 0}, theirs)[1]  # (answered: not shown again)
    # only the asked computer can fetch it, and it says who gave it: whoever signed, never what a body says
    assert task(port, "answered", {"id": tid, "q": MARK, "wait_s": 0}, theirs)[0] == 404
    assert task(port, "answered", {"id": tid, "q": "ffffffff", "wait_s": 0}, mine)[1]["answer"] is None
    status, obj, _ = task(port, "answered", {"id": tid, "q": MARK, "wait_s": 0}, mine, answer=got)
    assert obj["answer"] == {"q": MARK, "choice": "once", "by": {"id": mid, "name": "Orchard"}} and got["signed"]
    # a look ("still waiting") keeps it, and so does the same question told a second time (its first telling
    # may have got no answer back); the step moving on drops the question and its answer for good
    assert task(port, "update", {"id": tid, "state": "waiting_approval", "ask": dict(ECHO)}, mine)[0] == 200
    assert task(port, "answered", {"id": tid, "q": MARK, "wait_s": "soon"}, mine)[1]["answer"]["choice"] == "once"
    assert task(port, "update", {"id": tid, "state": "waiting_approval"}, mine)[0] == 200
    assert task(port, "answered", {"id": tid, "q": MARK, "wait_s": 0}, mine)[1]["answer"]["choice"] == "once"
    assert task(port, "update", {"id": tid, "state": "running"}, mine)[0] == 200
    assert task(port, "answered", {"id": tid, "q": MARK, "wait_s": 0}, mine)[1]["answer"] is None
    assert task(port, "answer", ans, theirs)[1]["error"]["code"] == "step_changed"
    # a later step has a mark of its own: the old mark answers nothing, and an answer to the old step is gone
    later = {"q": "1b2c3d4e", "command": "echo again", "description": "a step"}
    assert task(port, "update", {"id": tid, "state": "waiting_approval", "ask": later}, mine)[0] == 200
    assert task(port, "answer", ans, theirs)[1]["error"]["code"] == "step_changed"
    assert task(port, "answered", {"id": tid, "q": "1b2c3d4e", "wait_s": 0}, mine)[1]["answer"] is None
    # "a step waits, and it is not shown to the asking computer" takes an earlier offer back (should the
    # "it goes on" in between have been lost on the way): nothing old is left to answer
    assert task(port, "update", {"id": tid, "state": "waiting_approval", "ask": None}, mine)[0] == 200
    assert "approval" not in task(port, "wait", {"id": tid, "wait_s": 0}, theirs)[1]
    assert task(port, "answer", {**ans, "q": "1b2c3d4e"}, theirs)[1]["error"]["code"] == "approve_there"
    # a question that cannot be shown whole or has no mark is not offered at all
    for bad in ({"q": "1b2c3d4e", "command": "echo " + "x" * 2000, "description": "a step"},
                {"q": "nope", "command": "echo hello"}, {"command": "echo hello"}, "echo hello", {"q": "1b2c3d4e"}):
        assert task(port, "update", {"id": tid, "state": "waiting_approval", "ask": bad}, mine)[0] == 200
        assert "approval" not in task(port, "wait", {"id": tid, "wait_s": 0}, theirs)[1], bad
    # the sharing computer wrote down that it passed an answer along: who, never what
    relay = [r for r in source.json("requests")["log"] if r["dir"] == "relay"][0]
    assert relay["answered"] == {"choice": "once", "by": "Orchard"} and relay["text"] == ""
    assert "echo" not in (source.data / "tasks-log.json").read_text(encoding="utf-8")
    # attack: the computer that asked is unlinked while its question is open. Its key opens nothing, and
    # nothing of its request is left to answer or to fetch
    assert task(port, "update", {"id": tid, "state": "waiting_approval", "ask": later}, mine)[0] == 200
    source.run("fleet", "remove", "Orchard", "--yes")
    assert task(port, "answer", {**ans, "q": "1b2c3d4e"}, theirs)[0] == 401
    assert task(port, "answered", {"id": tid, "q": "1b2c3d4e", "wait_s": 0}, mine)[1]["answer"] is None


def test_a_request_cannot_answer_its_own_step_from_either_computer(home, tmp_path, monkeypatch):  # noqa: F811
    """The text of a request says: answer the step yourself. On the asked computer its turn has the plugin's
    commands within reach; none of them answers, in any mode."""
    from test_security_twins import _linked
    svc = _linked(home, tmp_path)
    tasks.set_switch(True)
    tasks.set_mode("linked")
    tid = "k-00000000000000b7"
    tasks.log_entry("in", tid, "mini", "Test Scout", "tidy up", "waiting_approval")
    tasks.set_pending(tid, {"command": "echo hello", "description": "a step"}, shared=True)
    q = tasks.pending()[tid]["q"]
    # (and say the turn even planted a step "to answer from here" under its own request's number)
    tasks.log_entry("out", tid, "mini", "Test Scout", "tidy up", "waiting_approval")
    tasks.set_asking(tid, {"q": q, "command": "echo hello", "description": "a step"})
    sent: list = []
    monkeypatch.setattr(tasks_run, "_call", _mailbox(sent))
    # (no worker in this test: a starting worker tidies away requests left open, this made-up one included)
    monkeypatch.setattr(tasks_run, "ensure_started", lambda get_service: False)
    monkeypatch.setenv(tasks.ASKED_ENV, "1")
    for call in (lambda: svc.tasks_approve(tid, "once", q), lambda: svc.tasks_approve(tid, "once"),
                 lambda: svc.tasks_answer(tid, "once", q), lambda: svc.tasks_answer(tid, "once", q, typed=True),
                 lambda: tasks.decide(tid, "once", q), lambda: svc.tasks_mode("here")):
        with pytest.raises(tasks.TaskError) as exc:
            call()
        assert exc.value.code == "not_from_a_request"
    assert "cannot approve steps" in slash.handle(f"approve {q}", platform="desktop")
    assert tasks.decision(tid) is None and sent == [] and tasks.mode() == "linked"
    monkeypatch.delenv(tasks.ASKED_ENV)
    svc.tasks_approve(tid, "once", q)  # the owner of this computer, on its own page or terminal
    assert tasks.decision(tid) == "once" and sent == []


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
    assert source.json("requests")["asking"] == []
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
    not whole, so it is refused before anyone is asked: here, and (mode "linked") on the computer that asked."""
    tail = "; echo this-part-was-never-shown"
    for pad in (" " * 2000, "\n" * 1700, "\u200b" * 2000, "\t \t" * 700):
        step = "echo hello" + pad + tail
        assert tasks.step_whole(step) is False, repr(pad[:4])
        assert tasks.clean_question({"q": MARK, "command": step, "description": "a step"}) is None
    # padding that still fits is shown with everything behind it
    step = "echo hello" + " " * 1500 + tail
    assert tasks.step_whole(step) is True and tasks.step_text(step) == "echo hello " + tail.strip() == \
        tasks.clean_question({"q": MARK, "command": step, "description": "a step"})["command"]


# ---- the page's two routes ----------------------------------------------------------------------------------------------------
def test_rest_mode_and_answer_routes(home, tmp_path, monkeypatch):  # noqa: F811
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from test_security_twins import _linked
    _linked(home, tmp_path)
    sent: list = []
    monkeypatch.setattr(tasks_run, "_call", _mailbox(sent, ECHO))
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_modes_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/p")
    c = TestClient(app)
    view = c.get("/p/tasks").json()
    assert shape_problems(view, "Tasks") == [] and view["mode"]["value"] == "here"
    for bad in ({}, {"mode": "always"}, {"mode": True}, {"value": "linked"}, {"mode": ["linked"]}):
        assert c.post("/p/tasks/mode", json=bad).status_code == 400
    assert tasks.mode() == "here"
    # the page is the owner's: it may choose "linked" even while a chat works in that Hermes
    monkeypatch.setenv("HERMES_SESSION_ID", "an-ordinary-chat-0003")
    view = c.post("/p/tasks/mode", json={"mode": "linked"}).json()
    assert shape_problems(view, "Tasks") == [] and view["mode"]["value"] == "linked" and tasks.mode() == "linked"
    assert c.post("/p/tasks/mode", json={"mode": "safe"}).json()["mode"]["value"] == "safe"
    # answering a step that waits on another computer: the mark is required and must be the one shown
    tid = "k-00000000000000c7"
    tasks.log_entry("out", tid, "mini", "Test Scout", "say hello", "waiting_approval")
    tasks.set_asking(tid, dict(ECHO))
    view = c.get("/p/tasks").json()
    assert shape_problems(view, "Tasks") == [] and view["asking"][0]["q"] == MARK
    for bad in ({"choice": "once"}, {"choice": "always", "q": MARK}, {"choice": "once", "q": "nope"},
                {"choice": "once", "q": None}, {}):
        assert c.post(f"/p/tasks/{tid}/answer", json=bad).status_code == 400
    assert c.post("/p/tasks/not-an-id/answer", json={"choice": "once", "q": MARK}).status_code in (400, 404)
    out = c.post(f"/p/tasks/{tid}/answer", json={"choice": "once", "q": _other(MARK)}).json()
    assert out["error"]["code"] == "step_changed"
    out = c.post("/p/tasks/k-00000000000000ff/answer", json={"choice": "once", "q": MARK}).json()
    assert out["error"]["code"] == "unknown_task"
    view = c.post(f"/p/tasks/{tid}/answer", json={"choice": "deny", "q": MARK}).json()
    assert shape_problems(view, "Tasks") == [] and view["asking"] == []
    assert sent == [("/v1/task/answer", {"id": tid, "q": MARK, "choice": "deny"})]
    assert view["log"][0]["answered"] == {"choice": "deny", "by": "Surface"}
