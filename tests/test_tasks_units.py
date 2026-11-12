"""Requests between the owner's computers: the parts that need no second computer.

The switch file, the log, the cleaning of what travels, the labels, routing by name, the mailbox's own rules
(driven directly, without a socket), the local API's shapes and the REST routes.
"""
from __future__ import annotations

import importlib.util
import json
import os
import time

import pytest

import fleet_testlib
from fleet_testlib import shape_problems
from tasks_testlib import calls, install_fake_hermes
from twin_testlib import ROOT, config, home, service, slash, timer  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import (fleet, fleet_io as fio, fleet_server, store, tasks, tasks_hub,
                                               tasks_run)

fleet_testlib._SHAPES.update(json.loads((ROOT / "tests" / "contract" / "tasks_shapes.json").read_text("utf-8"))["shapes"])

ME, SRC, OTHER = "m-aaaaaaaaaaaa", "m-bbbbbbbbbbbb", "m-cccccccccccc"
SCOUT, QUILL = "t-1111111111111111", "t-2222222222222222"


@pytest.fixture
def linked(home, tmp_path):  # noqa: F811
    """This computer ("Surface") as one that follows "mini", with two mirrored twins, written straight to disk."""
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "machine_name": "Surface"})
    st = fleet._blank()
    st["machine"] = {"id": ME, "name": "Surface"}
    st["link"] = {"source": {"id": SRC, "name": "mini", "platform": "macos"}, "host": "127.0.0.1", "port": 9,
                  "pass": "p" * 43, "linked_at": "2000-01-01T09:00:00Z", "rev": "1-00000000", "state": "in_sync",
                  "last_sync_at": "2000-01-01T09:00:00Z", "last_contact_at": None, "offline_since": None,
                  "fleet": [{"id": SRC, "name": "mini", "platform": "macos", "role": "source"},
                            {"id": ME, "name": "Surface", "platform": "windows", "role": "follower"},
                            {"id": OTHER, "name": "Demo-Surface-Tablet", "platform": "windows", "role": "follower"},
                            {"id": "m-dddddddddddd", "name": "MacBook Pro", "platform": "macos", "role": "follower"},
                            {"id": "m-eeeeeeeeeeee", "name": "MacBook Air", "platform": "macos", "role": "follower"}],
                  "twins": {SCOUT: {"name": "Test Scout", "profile": "test-scout", "version": 1, "state": "in_sync"},
                            QUILL: {"name": "Test Quill", "profile": "test-quill", "version": 1, "state": "in_sync"}}}
    fleet.save(st)
    for prof in ("test-scout", "test-quill"):
        (home / "profiles" / prof).mkdir(parents=True)
    return service.get_service()


# ---- the switch ---------------------------------------------------------------------------------------------------------
def test_the_switch_is_off_unless_its_file_says_exactly_on(home):  # noqa: F811
    path = store.central_dir() / "tasks.json"
    assert tasks.switch() == {"on": False, "since": None} and not path.exists()
    for text in ("", "null", "[]", "true", '{"on": 1}', '{"on": "true"}', '{"On": true}', "{bad", '{"on": false}'):
        path.write_text(text, encoding="utf-8")
        assert tasks.accepting() is False, text
    assert tasks.set_switch(True)["on"] is True and tasks.accepting() is True
    since = tasks.switch()["since"]
    assert since and tasks.set_switch(True)["since"] == since  # switching on twice keeps the first time
    assert tasks.set_switch(False) == {"on": False, "since": None}
    with pytest.raises(ValueError):
        tasks.set_switch("yes")
    # a computer without a fleet runs no worker even with the switch on
    tasks.set_switch(True)
    assert tasks_run.ensure_started(service.get_service) is False and not tasks_run.running()


def test_no_worker_thread_until_the_switch_was_used(linked):
    assert tasks_run.ensure_started(service.get_service) is False and not tasks_run.running()
    view = linked.tasks()
    assert shape_problems(view, "Tasks") == []
    assert view["switch"]["on"] is False and view["working"] is False and view["linked"] is True
    assert [m["name"] for m in view["machines"]] == ["mini", "Demo-Surface-Tablet", "MacBook Pro", "MacBook Air"]
    assert view["limits"] == {"request_chars": 2000, "reply_chars": 6000, "per_minute": 6, "per_hour": 40}
    assert not tasks_run.running()  # looking at the page starts nothing either


# ---- what travels ---------------------------------------------------------------------------------------------------------
def test_requests_and_answers_are_cleaned_capped_and_stable():
    key = "sk-" + "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9J"
    assert key not in tasks.clean_request(f"use {key} please") and "please" in tasks.clean_request(f"use {key} please")
    assert tasks.clean_request("a\x00b\x1b[31mc\u202e") == "ab[31mc"
    assert tasks.clean_request("line one\nline two") == "line one\nline two"
    assert tasks.clean_request(None) == "" and tasks.clean_request(5) == "" and tasks.clean_request(" \n ") == ""
    assert len(tasks.clean_request("x" * 9000)) == tasks.REQUEST_CAP
    for raw in ("y" * 50000, "é" * 50000, "中" * 9000, "short answer", "a\nb\n" * 3000, key + " " + "z" * 7000):
        once = tasks.clean_reply(raw)
        assert tasks.clean_reply(once) == once  # cleaning again (the mailbox, then the asker) changes nothing
        assert len(once) <= tasks.REPLY_CAP and len(json.dumps(once)) <= tasks.REPLY_WIRE_CAP
        assert key not in once
        assert once.endswith("[cut: the answer was longer]") == (len(raw) > tasks.REPLY_CAP)
    assert tasks.clean_reply(None) == "" and tasks.clean_reply(["x"]) == ""


def test_labels():
    assert tasks.label("done", "Surface", 12.4) == "done on Surface - 12 s"
    assert tasks.label("done", "Surface", 0.2) == "done on Surface - 0 s"
    assert tasks.label("done", "Surface", 185) == "done on Surface - 3 min"
    assert tasks.label("done", "Surface") == "done on Surface"
    assert tasks.label("waiting_approval", "Surface") == "waiting for approval on Surface"
    assert tasks.label("running", "Surface") == "still working on Surface"
    assert tasks.label("queued", "Surface") == "waiting for Surface to pick it up"
    assert tasks.label("refused", "Surface", code="too_many") == "not sent: too many requests to Surface"
    assert tasks.label("refused", "Surface", code="busy") == "not sent: too many requests to Surface"
    assert tasks.in_label("cancelled", "switched_off") == "stopped: requests were switched off here"
    assert tasks.in_label("refused", "switched_off") == "refused: requests are switched off here"
    assert tasks.in_label("cancelled", "unknown_task") == "stopped: the asking computer no longer waits for it"
    assert tasks.label("refused", "Surface", code="tasks_off") == "not sent: requests are switched off on Surface"
    assert tasks.label("failed", "Surface", code="tasks_unreachable") == "not done: Surface could not be reached"
    assert tasks.label("failed", "Surface", code="approval_denied") == "not done on Surface: a step was not approved there"
    assert tasks.label("timeout", "Surface", code="task_timeout").startswith("not done on Surface: its twin there did not finish in time")
    # a name is only ever plain characters, whatever arrives
    assert tasks.label("done", "Sur<b>face\n[x]", 1) == "done on Surbfacex - 1 s"
    for code in tasks.CODES:
        m = tasks.message(code, name="Surface", asked="Laptop", names="mini")
        assert m["message"] and "{" not in m["message"] and "{" not in m.get("hint", "")


# ---- the log --------------------------------------------------------------------------------------------------------------
def test_the_log_is_capped_cleaned_and_never_reopens_an_ended_request(home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tasks, "LOG_CAP", 5)
    key = "sk-" + "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9J"
    for i in range(8):
        tasks.log_entry("out", f"k-{i:016x}", "Surface", "Test Scout", f"request {i} {key}")
    rows = tasks.log_view()
    assert len(rows) == 5 and rows[0]["text"].startswith("request 7") and key not in json.dumps(rows)
    assert all(shape_problems(r, "TaskLogRow") == [] for r in rows)
    tid = rows[0]["id"]
    tasks.log_update("out", tid, "waiting_approval")
    assert tasks.log_view()[0]["label"] == "waiting for approval on Surface"
    tasks.log_update("out", tid, "done", None, 12.4, "the answer " + "w" * 900)
    row = tasks.log_view()[0]
    assert row["state"] == "done" and row["seconds"] == 12 and row["label"] == "done on Surface - 12 s"
    assert len(row["reply"]) == tasks.LOG_TEXT_CAP and row["ended_at"]
    tasks.log_update("out", tid, "failed", "task_failed")  # a late second ending changes nothing
    assert tasks.log_view()[0]["state"] == "done"
    tasks.log_update("out", "k-ffffffffffffffff", "done")  # unknown: ignored
    # what another computer could put into this file is shown as plain, capped text only
    fio.write_json(store.central_dir() / "tasks-log.json", {"entries": [
        {"id": "../../x", "dir": "sideways", "peer": "<b>mini</b>\n", "twin": "T" * 500, "text": 5, "state": "odd",
         "code": "made_up", "asked_at": "yesterday", "seconds": "3"}, "junk", {"no": "id"}]})
    (row,) = tasks.log_view()
    assert row["id"] == "k-0000000000000000" and row["dir"] == "in" and row["peer"] == "bminib"
    assert row["state"] == "failed" and row["code"] is None and row["asked_at"] is None and row["seconds"] is None
    assert len(row["twin"]) <= 100 and row["text"] == "" and shape_problems(row, "TaskLogRow") == []


# ---- routing by name ----------------------------------------------------------------------------------------------------
def test_a_computer_is_found_by_the_name_the_owner_says(linked):
    st = fleet.load()
    for said, name in (("mini", "mini"), ("Mini", "mini"), (" MINI ", "mini"), (SRC, "mini"),
                       ("Demo-Surface-Tablet", "Demo-Surface-Tablet"), ("demo surface tablet", "Demo-Surface-Tablet"),
                       ("surface-tab", "Demo-Surface-Tablet"), ("macbook pro", "MacBook Pro"), ("MacBookAir", "MacBook Air")):
        assert tasks.find_machine(st, said)["name"] == name, said
    for said, code in (("Surface", "ask_self"), ("surface", "ask_self"), ("macbook", "unknown_machine"),
                       ("mac", "unknown_machine"), ("Laptop", "unknown_machine"), ("", "unknown_machine"),
                       (None, "unknown_machine"), ("m", "unknown_machine"), ("<script>", "unknown_machine")):
        with pytest.raises(tasks.TaskError) as exc:
            tasks.find_machine(st, said)
        assert exc.value.code == code, said
    assert "mini, Demo-Surface-Tablet, MacBook Pro, MacBook Air" in exc.value.hint  # the refusal lists the real names
    assert "<" not in exc.value.message
    # the twin: the one named, else the one whose Hermes bot the chat is with
    assert tasks.find_twin(st, None, "test-quill") == (QUILL, "Test Quill")
    assert tasks.find_twin(st, "test scout", "test-quill") == (SCOUT, "Test Scout")
    assert tasks.find_twin(st, SCOUT, None) == (SCOUT, "Test Scout")
    for asked, profile in ((None, "default"), ("Nobody", "test-scout"), (None, None)):
        with pytest.raises(tasks.TaskError) as exc:
            tasks.find_twin(st, asked, profile)
        assert exc.value.code == "no_twin_here" and "Test Quill, Test Scout" in exc.value.hint
    blank = fleet._blank()
    with pytest.raises(tasks.TaskError) as exc:
        tasks.find_machine(blank, "mini")
    assert exc.value.code == "not_linked"


# ---- the mailbox's own rules (no socket) ----------------------------------------------------------------------------------
def _sharing_state():
    st = fleet._blank()
    st["machine"] = {"id": SRC, "name": "mini"}
    st["share"] = fleet._blank_share()
    st["share"]["on"] = True
    for mid, name in ((ME, "Surface"), (OTHER, "Orchard")):
        st["share"]["followers"][mid] = {"id": mid, "name": name, "platform": "linux", "last_seen_at": None,
                                         "key": fleet.share_key("pass of " + name).hex()}
    return st


def test_the_mailbox_queues_only_for_a_listening_computer_and_answers_only_the_two_ends(home, monkeypatch):  # noqa: F811
    hub = tasks_hub.Hub()
    st = _sharing_state()
    surface, orchard, mini = ({"id": ME, "name": "Surface"}, {"id": OTHER, "name": "Orchard"},
                              {"id": SRC, "name": "mini"})
    ask = {"to": ME, "twin": SCOUT, "text": "hello there"}
    assert hub.ask(st, orchard, ask)[1]["error"]["code"] == "tasks_unreachable"   # never heard from
    st["share"]["followers"][ME]["last_seen_at"] = fleet.now_iso()
    assert hub.ask(st, orchard, ask)[1]["error"]["code"] == "tasks_off"           # follows, but fetches nothing
    assert hub.poll(surface, {"accept": True, "wait_s": 0})[1] == {"schema": fleet.SCHEMA, "proto": fleet.PROTO, "task": None}
    assert hub.ask(st, orchard, {**ask, "to": "m-ffffffffffff"})[1]["error"]["code"] == "unknown_machine"
    assert hub.ask(st, orchard, {**ask, "twin": "../x"})[1]["error"]["code"] == "bad_request"
    assert hub.ask(st, orchard, {**ask, "text": "   "})[1]["error"]["code"] == "empty"
    assert hub.ask(st, orchard, {**ask, "text": "x" * 2001})[1]["error"]["code"] == "too_long"
    assert hub.ask(st, orchard, {**ask, "text": ["x"]})[1]["error"]["code"] == "too_long"
    assert hub.ask(st, surface, ask)[1]["error"]["code"] == "ask_self"
    assert hub.tasks == {}
    status, out = hub.ask(st, orchard, ask)
    assert status == 200 and out["state"] == "queued" and tasks.TASK_ID_RE.match(out["id"])
    tid = out["id"]
    # only the asker may wait for it, only the asked computer may fetch and answer it
    assert hub.wait(surface, {"id": tid, "wait_s": 0})[0] == 404
    assert hub.wait(mini, {"id": tid, "wait_s": 0})[0] == 404
    assert hub.wait(orchard, {"id": tid, "wait_s": 0})[1]["state"] == "queued"
    assert hub.poll(orchard, {"accept": True, "wait_s": 0})[1]["task"] is None
    assert hub.update(orchard, {"id": tid, "state": "done", "reply": "forged"})[0] == 404
    got = hub.poll(surface, {"accept": True, "wait_s": 0})[1]["task"]
    assert got == {"id": tid, "from": {"id": OTHER, "name": "Orchard"}, "twin": SCOUT, "text": "hello there"}
    assert hub.poll(surface, {"accept": True, "wait_s": 0})[1]["task"] is None  # handed out once
    assert hub.update(surface, {"id": tid, "state": "made_up"})[0] == 400
    assert hub.update(surface, {"id": tid, "state": "waiting_approval"})[0] == 200
    # (the same call is the asked computer's look "do you still know this request?": yes, and nothing more)
    assert hub.update(surface, {"id": tid, "state": "waiting_approval"})[1] == {
        "schema": fleet.SCHEMA, "proto": fleet.PROTO, "ok": True}
    assert hub.wait(orchard, {"id": tid, "wait_s": 5})[1] == {"schema": fleet.SCHEMA, "proto": fleet.PROTO, "id": tid,
                                                              "state": "waiting_approval"}  # answered at once
    key = "sk-" + "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9J"
    assert hub.update(surface, {"id": tid, "state": "done", "seconds": 3.2, "reply": f"all good {key}",
                                "extra": {"path": "/etc/passwd"}})[0] == 200
    out = hub.wait(orchard, {"id": tid, "wait_s": 0})[1]
    assert out["state"] == "done" and out["seconds"] == 3.2 and key not in out["reply"] and "all good" in out["reply"]
    assert set(out) == {"schema", "proto", "id", "state", "seconds", "reply"}
    late = hub.update(surface, {"id": tid, "state": "failed", "code": "task_failed"})
    assert late[0] == 200 and late[1]["ended"] is True  # (a computer that still works on it learns that it ended)
    assert hub.update(surface, {"id": tid, "state": "running"})[1]["ended"] is True
    assert hub.wait(orchard, {"id": tid, "wait_s": 0})[1]["state"] == "done"  # the first ending stands
    # passed along between two linked computers: the sharing computer's log has who and how, not what
    (row,) = tasks.log_view()
    assert row["dir"] == "relay" and row["peer"] == "Orchard" and row["peer_to"] == "Surface" and row["text"] == ""
    assert row["state"] == "done" and row["reply"] == "" and row["label"] == "done on Surface - 3 s"
    # the queue for one computer is short, and "switched off" answers what waited
    monkeypatch.setattr(tasks, "RATE_PER_MIN", 100)
    ids = [hub.ask(st, orchard, ask)[1].get("id") for _ in range(tasks.QUEUE_CAP)]
    assert all(ids) and hub.ask(st, orchard, ask)[1]["error"]["code"] == "busy"
    hub.poll(surface, {"accept": False, "wait_s": 0})
    assert hub.wait(orchard, {"id": ids[0], "wait_s": 0})[1]["code"] == "tasks_off"
    assert hub.ask(st, orchard, ask)[1]["error"]["code"] == "tasks_off"
    # the sharing computer itself is asked only while its own switch file says on AND a Hermes process works
    to_mini = {"to": SRC, "twin": SCOUT, "text": "hello"}
    hub.poll(mini, {"accept": True, "wait_s": 0})
    assert hub.ask(st, orchard, to_mini)[1]["error"]["code"] == "tasks_off"
    tasks.set_switch(True)
    assert hub.ask(st, orchard, to_mini)[0] == 200


def test_a_computer_at_work_on_one_request_is_not_taken_for_switched_off(home, monkeypatch):  # noqa: F811
    """One request at a time: while a step of it waits for the owner (minutes), the computer fetches nothing. A
    second request then waits its turn; it is not answered "switched off" or "cannot be reached"."""
    hub = tasks_hub.Hub()
    st = _sharing_state()
    st["share"]["followers"][ME]["last_seen_at"] = fleet.now_iso()
    surface, orchard = {"id": ME, "name": "Surface"}, {"id": OTHER, "name": "Orchard"}
    hub.poll(surface, {"accept": True, "wait_s": 0})
    first = hub.ask(st, orchard, {"to": ME, "twin": SCOUT, "text": "one"})[1]["id"]
    assert hub.poll(surface, {"accept": True, "wait_s": 0})[1]["task"]["id"] == first
    hub.update(surface, {"id": first, "state": "waiting_approval"})
    monkeypatch.setattr(tasks_hub, "POLL_FRESH_S", 0.05)
    time.sleep(0.12)  # long after its last fetch
    assert hub.is_listening(ME) is None
    status, out = hub.ask(st, orchard, {"to": ME, "twin": SCOUT, "text": "two"})
    assert status == 200 and out["state"] == "queued"
    second = out["id"]
    assert hub.wait(orchard, {"id": second, "wait_s": 0})[1]["state"] == "queued"  # ... and it is not given up on
    hub.update(surface, {"id": first, "state": "done", "reply": "ok", "seconds": 1})
    assert hub.poll(surface, {"accept": True, "wait_s": 0})[1]["task"]["id"] == second
    # with nothing at work and no fetch for long, a waiting request does end as "could not be reached"
    hub.update(surface, {"id": second, "state": "done", "reply": "ok", "seconds": 1})
    monkeypatch.setattr(tasks_hub, "POLL_FRESH_S", 30)
    third = hub.ask(st, orchard, {"to": ME, "twin": SCOUT, "text": "three"})[1]["id"]
    monkeypatch.setattr(tasks_hub, "POLL_FRESH_S", 0.05)
    time.sleep(0.12)
    assert hub.wait(orchard, {"id": third, "wait_s": 0})[1]["code"] == "tasks_unreachable"


def test_the_mailbox_forgets_and_times_out(home, monkeypatch):  # noqa: F811
    hub = tasks_hub.Hub()
    st = _sharing_state()
    surface, orchard = {"id": ME, "name": "Surface"}, {"id": OTHER, "name": "Orchard"}
    hub.poll(surface, {"accept": True, "wait_s": 0})
    tid = hub.ask(st, orchard, {"to": ME, "twin": SCOUT, "text": "x"})[1]["id"]
    monkeypatch.setattr(tasks_hub, "POLL_FRESH_S", 0.05)
    time.sleep(0.1)
    out = hub.wait(orchard, {"id": tid, "wait_s": 0})[1]  # nobody fetched it: the asker is told, not left waiting
    assert out["state"] == "failed" and out["code"] == "tasks_unreachable"
    monkeypatch.setattr(tasks, "TASK_TTL_S", 0.01)
    time.sleep(0.05)
    hub.poll(surface, {"accept": True, "wait_s": 0})
    hub.ask(st, orchard, {"to": ME, "twin": SCOUT, "text": "x"})
    assert tid not in hub.tasks  # an ended request leaves the mailbox's memory
    hub.close()
    assert all(t["ended"] for t in hub.tasks.values())


def test_a_request_is_never_handed_to_a_computer_that_has_hung_up(home, monkeypatch):  # noqa: F811
    """The fault: the helper on a linked computer was stopped; its last fetch was still held open on the sharing
    computer; the next request was handed to that fetch, into a closed connection. The request was lost, and the
    asker heard "still working on ..." until the request ran out of time."""
    import socket
    import threading
    hub = tasks_hub.Hub()
    st = _sharing_state()
    surface, orchard = {"id": ME, "name": "Surface"}, {"id": OTHER, "name": "Orchard"}
    state = {"gone": False}
    got = {}
    t = threading.Thread(target=lambda: got.update(out=hub.poll({**surface, "gone": lambda: state["gone"]},
                                                                {"accept": True, "wait_s": 20})), daemon=True)
    t.start()
    time.sleep(0.3)
    state["gone"] = True   # Surface's program ends while its fetch is held
    asked = hub.ask(st, orchard, {"to": ME, "twin": SCOUT, "text": "x"})
    assert asked[0] == 200  # (it had looked a moment ago: the request is taken in and waits)
    tid = asked[1]["id"]
    t.join(8)
    assert not t.is_alive() and got["out"][1]["task"] is None  # the held fetch ends empty-handed
    waiting = hub.wait(orchard, {"id": tid, "wait_s": 0})[1]
    assert waiting["state"] == "queued" and hub.tasks[tid]["taken"] is False
    assert tasks.label(waiting["state"], "Surface") == "waiting for Surface to pick it up"
    # the computer comes back in time: it gets the request, once
    back = hub.poll(surface, {"accept": True, "wait_s": 0})[1]["task"]
    assert back["id"] == tid and hub.poll(surface, {"accept": True, "wait_s": 0})[1]["task"] is None
    hub.update(surface, {"id": tid, "state": "done", "reply": "ok", "seconds": 1})
    # it does not come back: the asker is told so after the usual wait, not after a quarter of an hour
    state["gone"] = False
    t = threading.Thread(target=lambda: hub.poll({**surface, "gone": lambda: state["gone"]},
                                                 {"accept": True, "wait_s": 20}), daemon=True)
    t.start()
    time.sleep(0.3)
    state["gone"] = True
    tid = hub.ask(st, orchard, {"to": ME, "twin": SCOUT, "text": "x"})[1]["id"]
    t.join(8)
    monkeypatch.setattr(tasks_hub, "POLL_FRESH_S", 0.05)
    time.sleep(0.1)
    out = hub.wait(orchard, {"id": tid, "wait_s": 0})[1]
    assert out["state"] == "failed" and out["code"] == "tasks_unreachable"
    assert tasks.label(out["state"], "Surface", code=out["code"]) == "not done: Surface could not be reached"
    # how the share server tells: the connection's other end is closed, seen without reading anything
    a, b = socket.socketpair()
    try:
        assert fleet_server.hung_up(a) is False
        b.sendall(b"x")
        time.sleep(0.05)
        assert fleet_server.hung_up(a) is False and a.recv(1) == b"x"  # something to read is not the end
        b.close()
        time.sleep(0.05)
        assert fleet_server.hung_up(a) is True
    finally:
        a.close()
    assert fleet_server.hung_up(a) is True  # (a connection that is no longer one counts as hung up)


def _signed(mid: str, pass_: str, path: str, body: dict, nonce: str = "") -> tuple:
    raw = json.dumps(body).encode()
    nonce = nonce or fleet.new_nonce()
    mac = fleet.request_mac(fleet.share_key(pass_), path, nonce, raw)
    return fleet_server.Asked(mid, nonce, mac, path, raw), body


def test_the_mailbox_route_refuses_strangers_replays_and_a_computer_that_does_not_share(home):  # noqa: F811
    path = "/v1/task/poll"
    body = {"proto": fleet.PROTO, "at": int(time.time()), "accept": True, "wait_s": 0}
    asked, _ = _signed(ME, "pass of Surface", path, body)
    assert tasks_hub.handle(path, asked, "", body, "secret")[1]["error"]["code"] == "not_sharing"
    fleet.save(_sharing_state())
    st = fleet.load()
    assert tasks_hub.handle(path, asked, "", {**body, "proto": fleet.PROTO + 1}, "secret")[0] == 426
    # who is who: the signature decides; a wrong key, another computer's id, or nothing at all is nobody
    for mid, pass_ in ((ME, "wrong pass"), (OTHER, "pass of Surface"), ("m-ffffffffffff", "pass of Surface"),
                       ("", "pass of Surface"), (SRC, "pass of Surface")):
        bad, _ = _signed(mid, pass_, path, body)
        status, obj, signed = tasks_hub.handle(path, bad, "", body, "secret")
        assert (status, obj["error"]["code"], signed) == (401, "unlinked", None), (mid, pass_)
    wrong_path, _ = _signed(ME, "pass of Surface", "/v1/task/ask", body)
    wrong_path.path = path
    assert tasks_hub.handle(path, wrong_path, "", body, "secret")[0] == 401
    for bearer in ("x" * 43, "secre", "secret "):
        nobody = fleet_server.Asked("", "", "", path, b"{}")
        assert tasks_hub.handle(path, nobody, bearer, body, "secret")[0] == 401
    assert tasks_hub.caller(st, fleet_server.Asked("", "", "", path, b"{}"), "", "")[0] is None  # no empty secret
    # the real thing: accepted once, its answer signed for that request; the same request again is refused
    status, obj, signed = tasks_hub.handle(path, asked, "", body, "secret")
    assert status == 200 and signed == (fleet.share_key("pass of Surface"), asked.nonce)
    status, obj, signed = tasks_hub.handle(path, asked, "", body, "secret")
    assert (status, obj["error"]["code"]) == (409, "replayed") and signed is not None
    for at in (int(time.time()) - 1000, int(time.time()) + 1000, None, True, "1"):  # (the window is 900 s)
        old = {k: v for k, v in body.items() if k != "at"} if at is None else {**body, "at": at}
        fresh, _ = _signed(ME, "pass of Surface", path, old)
        assert tasks_hub.handle(path, fresh, "", old, "secret")[1]["error"]["code"] == "clock_off", at
    # a request to do something is remembered across a restart of the sharing computer's Hermes: its memory is
    # empty then, the small file is not
    ask_path = "/v1/task/ask"
    ask_body = {"proto": fleet.PROTO, "at": int(time.time()), "to": OTHER, "twin": SCOUT, "text": "hello"}
    once, _ = _signed(ME, "pass of Surface", ask_path, ask_body)
    first = tasks_hub.handle(ask_path, once, "", ask_body, "secret")
    assert first[1].get("error", {}).get("code") != "replayed"
    tasks_hub._nonces.clear()  # (what a restart does)
    again = tasks_hub.handle(ask_path, once, "", ask_body, "secret")
    assert (again[0], again[1]["error"]["code"]) == (409, "replayed")
    seen = json.loads((store.central_dir() / "tasks-seen.json").read_text("utf-8"))["seen"]
    assert list(seen) == [f"{ME}:{once.nonce}"] and "hello" not in json.dumps(seen)
    assert tasks_hub.first_ask(ME, "f" * 32) is True and tasks_hub.first_ask(ME, "f" * 32) is False
    # a Hermes process on the sharing computer itself shows the instance secret instead (never a stranger's)
    who, signed = tasks_hub.caller(st, fleet_server.Asked("", "", "", path, b"{}"), "secret", "secret")
    assert who == {"id": SRC, "name": "mini", "local": True} and signed is None
    who, signed = tasks_hub.caller(st, asked, "", "secret")
    assert who == {"id": ME, "name": "Surface", "local": False}
    # a computer that was unlinked is told so with a signature it can check, and gets nothing else
    with fleet.locked():
        st = fleet.load()
        fleet.forget_follower(st, ME)
        fleet.save(st)
    gone, _ = _signed(ME, "pass of Surface", path, body)
    status, obj, signed = tasks_hub.handle(path, gone, "", body, "secret")
    assert (status, obj["error"]["code"]) == (401, "unlinked") and signed is not None
    tasks_hub.reset()


# ---- the chat turn: what is started, and with what ------------------------------------------------------------------------
def test_the_chat_turn_gets_no_approval_changing_word_or_variable(home, tmp_path, monkeypatch):  # noqa: F811
    import os
    bindir = install_fake_hermes(tmp_path)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_HERMES_LOG", str(log))
    for name in tasks_run.SCRUB_ENV:
        monkeypatch.setenv(name, "1")
    assert tasks_run.CHAT_WORDS == ("chat", "-Q", "--query-file", "-")
    argv = tasks_run.turn_command("test-scout")
    assert argv[1:] == ["-p", "test-scout", "chat", "-Q", "--query-file", "-"]
    assert tasks_run.turn_command("../evil") is None and tasks_run.turn_command("-p") is None
    env = tasks_run.turn_env()
    assert not any(name in env for name in tasks_run.SCRUB_ENV) and env["HERMES_HOME"] == str(home)
    singles = []
    real_single = tasks.mark_single
    monkeypatch.setattr(tasks, "mark_single", lambda on: (singles.append(on), real_single(on))[1])
    res = tasks_run.run_turn("test-scout", "--yolo\n-z please", lambda: None)
    assert res == {"ok": True, "reply": "test-scout heard: -z please"}
    assert singles == [True, False] and tasks._single_since() is None  # marked for exactly as long as it runs
    (call,) = calls(log)
    assert call["argv"] == ["-p", "test-scout", "chat", "-Q", "--query-file", "-"]  # the text is never an argument
    assert call["stdin"] == "--yolo\n-z please" and call["env"] == {}
    # `hermes chat` may start a Hermes that other chats then share: the variable does not live on in it
    assert call["asked"] is None
    # stopping: the turn is ended, and the reason is the one given
    t0 = time.monotonic()
    res = tasks_run.run_turn("test-scout", "nap 30", lambda: "switched_off" if time.monotonic() - t0 > 1 else None)
    assert res == {"ok": False, "code": "switched_off"} and time.monotonic() - t0 < 12
    assert tasks_run.run_turn("test-scout", "nap 30", lambda: None, limit_s=1.0) == {"ok": False, "code": "task_timeout"}
    assert tasks_run.run_turn("test-scout", "please fail", lambda: None) == {"ok": False, "code": "task_failed"}
    source = (ROOT / "twin" / "tasks_run.py").read_text(encoding="utf-8")
    assert "shell=True" not in source and "os.system" not in source


# ---- the page's routes ----------------------------------------------------------------------------------------------------
def test_asking_without_a_reachable_computer_is_a_plain_logged_refusal(linked):
    out = linked.tasks_ask("mini", "tell me this machine's OS and free disk", twin="Test Scout")
    assert shape_problems(out, "TaskResult") == []
    assert out["state"] == "refused" and out["error"]["code"] == "tasks_unreachable"
    assert out["label"] == "not done: mini could not be reached" and out["reply"] == ""
    view = linked.tasks()
    assert shape_problems(view, "Tasks") == []
    row = view["log"][0]
    assert row["dir"] == "out" and row["peer"] == "mini" and row["state"] == "refused" and row["twin"] == "Test Scout"
    again = linked.tasks_check(out["id"])
    assert shape_problems(again, "TaskResult") == [] and again["state"] == "refused"
    with pytest.raises(tasks.TaskError):
        linked.tasks_check("k-0000000000000000")
    # /twin ask and /twin requests say the same in a chat
    linked.ctx = type("Ctx", (), {"profile_name": "test-scout"})()
    text = slash.handle("ask mini: how much disk space is free?", platform="cli")
    assert text.splitlines()[0] == "not done: mini could not be reached" and "Hermes could not reach mini." in text
    assert "Say which computer" in slash.handle("ask", platform="cli")
    assert "Say which computer" in slash.handle("ask mini", platform="cli")
    assert slash.handle("ask laptop: hi", platform="cli").startswith("Grok Bot Twin: None of your linked computers is called laptop.")
    assert "only shows status" in slash.handle("ask mini: hi", platform="telegram")  # never from a messaging chat
    assert '"Let my other computers ask this one to do things" is off on Surface.' in slash.handle("requests", platform="cli")


def test_the_request_log_is_not_shown_in_a_chat_that_arrives_through_a_messaging_app(linked):
    """The log holds the first words of what was asked and answered. `/twin requests` shows it in a chat on this
    computer only, like `/twin ask`."""
    tasks.log_entry("out", "k-00000000000000e2", "mini", "Test Scout", "tidy up the notes", "waiting_approval")
    here = slash.handle("requests", platform="cli")
    assert "tidy up the notes" in here and "to mini" in here
    assert "tidy up the notes" in slash.handle("requests", platform="desktop")
    for platform in ("telegram", "discord", "webhook", "api_server"):
        said = slash.handle("requests", platform=platform)
        assert said.startswith("From a messaging chat, /twin only shows status and recent activity.")
        assert "tidy up" not in said and "mini" not in said and "Surface" not in said
        assert "only shows status" in slash.handle(" Requests ", platform=platform)


def test_a_look_that_cannot_reach_the_sharing_computer_writes_nothing_off(linked, monkeypatch):
    """A request asked from here is waiting for approval elsewhere; the sharing computer does not answer a status
    look just now. The line stays open (the request may well go on); only age closes it."""
    tid = "k-00000000000000e1"
    tasks.log_entry("out", tid, "mini", "Test Scout", "tidy up", "waiting_approval")
    out = linked.tasks_check(tid)  # (the link in this test points at a port where nothing listens)
    assert out["state"] == "waiting_approval" and out["label"] == "waiting for approval on mini" and out["error"] is None
    view = linked.tasks()
    assert view["log"][0]["state"] == "waiting_approval" and view["log"][0]["id"] == tid
    monkeypatch.setattr(tasks, "TASK_TTL_S", -10 ** 6)
    row = linked.tasks()["log"][0]
    assert row["state"] == "timeout" and row["code"] == "task_timeout" and row["label"].startswith("not done on mini: its twin there did not finish in time")


def test_rest_routes(linked):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_tasks_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/p")
    c = TestClient(app)
    view = c.get("/p/tasks").json()
    assert shape_problems(view, "Tasks") == [] and view["switch"]["on"] is False
    view = c.post("/p/tasks/switch", json={"on": True}).json()
    assert shape_problems(view, "Tasks") == [] and view["switch"]["on"] is True and tasks.accepting() is True
    view = c.post("/p/tasks/switch", json={"on": False}).json()
    assert view["switch"]["on"] is False and tasks.accepting() is False
    for bad in ({"on": "true"}, {"on": 1}, {}, {"off": True}):
        assert c.post("/p/tasks/switch", json=bad).status_code == 400
    assert tasks.accepting() is False
    out = c.post("/p/tasks/ask", json={"machine": "mini", "text": "hello", "twin": "Test Scout"}).json()
    assert shape_problems(out, "TaskResult") == [] and out["state"] == "refused"
    out = c.post("/p/tasks/ask", json={"machine": "Laptop", "text": "hello"}).json()
    assert out["error"]["code"] == "unknown_machine" and "message" in out["error"]
    out = c.post("/p/tasks/ask", json={"machine": "mini", "text": "x" * 2001, "twin": "Test Scout"}).json()
    assert out["error"]["code"] == "too_long"
    for bad in ({"machine": "mini"}, {"machine": 5, "text": "x"}, {"machine": "mini", "text": "x", "twin": 5}, []):
        assert c.post("/p/tasks/ask", json=bad).status_code == 400
    assert c.post("/p/tasks/check", json={"id": "../../etc"}).status_code == 400
    assert c.post("/p/tasks/check", json={"id": "k-0000000000000000"}).json()["error"]["code"] == "unknown_task"
    rows = c.get("/p/tasks").json()["log"]
    assert rows and all(shape_problems(r, "TaskLogRow") == [] for r in rows)
    # linked over a plain home network: the switch works, and the view says what carries the link
    with fleet.locked():
        st = fleet.load()
        st["link"]["host"] = "192.168.1.20"
        fleet.save(st)
    view = c.post("/p/tasks/switch", json={"on": True}).json()
    assert shape_problems(view, "Tasks") == [] and view["switch"]["on"] is True and view["switch"]["network"] == "lan"
    c.post("/p/tasks/switch", json={"on": False})


def test_removing_the_plugin_leaves_no_switch_and_no_log(linked):
    tasks.set_switch(True)
    tasks.log_entry("in", "k-0000000000000001", "mini", "Test Scout", "hello")
    data = store.central_dir()
    assert (data / "tasks.json").exists() and (data / "tasks-log.json").exists()
    linked.uninstall(False)
    assert not (data / "tasks.json").exists() and not (data / "tasks-log.json").exists()
    assert tasks.accepting() is False


# ---- a step that waits for approval: the question and the answer stay on this computer -----------------------------------
def test_the_approval_question_and_its_answer(home):  # noqa: F811
    tid, other = "k-00000000000000a1", "k-00000000000000a2"
    assert tasks.pending() == {} and tasks.decision(tid) is None
    with pytest.raises(tasks.TaskError):
        tasks.decide(tid, "once")  # nothing waits: nothing to answer
    key = "sk-" + "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9J"
    tasks.set_pending(tid, {"command": f"curl -H 'token: {key}' \x1b[31mhttp://x", "description": "d" * 900})
    q = tasks.pending()[tid]
    assert key not in q["command"] and "\x1b" not in q["command"] and len(q["description"]) <= 300 and q["asked_at"]
    for bad in ("always", "session", "", None, True, "Once"):
        with pytest.raises(ValueError):
            tasks.decide(tid, bad)
    with pytest.raises(tasks.TaskError):
        tasks.decide(other, "once")
    with pytest.raises(tasks.TaskError):
        tasks.decide("../../x", "once")
    assert tasks.decision(tid) is None
    tasks.decide(tid, "once")
    assert tasks.decision(tid) == "once" and tasks.decision(other) is None
    tasks.decide(tid, "deny")  # the owner may change their mind until Hermes has been told
    assert tasks.decision(tid) == "deny"
    assert tasks.pending() == {}  # answered: not shown as waiting any more (the answer is on its way to Hermes)
    # a new question for the same request starts without an answer; closing it forgets both
    tasks.set_pending(tid, {"command": "rm -rf ./x", "description": "recursive delete"})
    assert tasks.decision(tid) is None
    tasks.set_pending(tid, None)
    assert tasks.pending() == {}
    # a hand-written file is read as closed shapes only
    fio.write_json(store.central_dir() / "tasks-approvals.json", {
        "pending": {"../x": {"command": "x"}, tid: "not a dict"}, "decisions": {tid: {"choice": "once"}}})
    assert tasks.pending() == {} and tasks.decision(tid) is None  # an answer without its open question is nothing


def test_requests_left_open_by_an_ended_hermes_are_marked_stopped(home):  # noqa: F811
    tasks.log_entry("in", "k-00000000000000b1", "mini", "Test Scout", "one", "running")
    tasks.log_entry("in", "k-00000000000000b2", "mini", "Test Scout", "two", "waiting_approval")
    tasks.log_entry("in", "k-00000000000000b3", "mini", "Test Scout", "three", "done")
    tasks.log_entry("out", "k-00000000000000b4", "mini", "Test Scout", "four", "running")
    tasks.set_pending("k-00000000000000b2", {"command": "rm -rf ./x", "description": "recursive delete"})
    assert tasks.close_stale("in") == 2
    rows = {r["id"][-2:]: r for r in tasks.log_view()}
    assert rows["b1"]["state"] == rows["b2"]["state"] == "cancelled" and rows["b1"]["code"] == "stopped"
    assert rows["b3"]["state"] == "done" and rows["b4"]["state"] == "running"
    assert tasks.pending() == {} and tasks.close_stale("in") == 0


def test_rest_approve_route(linked):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_tasks_test2", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/p")
    c = TestClient(app)
    tid = "k-00000000000000c1"
    tasks.log_entry("in", tid, "mini", "Test Scout", "please tidy up", "waiting_approval")
    tasks.set_pending(tid, {"command": "rm -rf ./scratch", "description": "recursive delete"})
    view = c.get("/p/tasks").json()
    assert shape_problems(view, "Tasks") == []
    mark = view["waiting"][0]["q"]
    assert view["waiting"] == [{"id": tid, "peer": "mini", "twin": "Test Scout", "text": "please tidy up",
                                "command": "rm -rf ./scratch", "description": "recursive delete",
                                "asked_at": view["waiting"][0]["asked_at"], "q": mark}] and len(mark) == 8
    # an answer for another step than the one that is open (the mark the page was shown is not this one's)
    out = c.post(f"/p/tasks/{tid}/approve", json={"choice": "once", "q": "00000000" if mark != "00000000" else "11111111"}).json()
    assert out["error"]["code"] == "step_changed" and tasks.decision(tid) is None
    for bad in ({"choice": "always"}, {"choice": "session"}, {}, {"choice": True}, {"approve": "once"}):
        assert c.post(f"/p/tasks/{tid}/approve", json=bad).status_code == 400
    assert c.post("/p/tasks/not-an-id/approve", json={"choice": "once"}).status_code in (400, 404)
    assert tasks.decision(tid) is None
    out = c.post("/p/tasks/k-00000000000000ff/approve", json={"choice": "once"}).json()
    assert out["error"]["code"] == "unknown_task"
    view = c.post(f"/p/tasks/{tid}/approve", json={"choice": "once", "q": mark}).json()
    assert shape_problems(view, "Tasks") == [] and tasks.decision(tid) == "once"
    assert view["waiting"] == []  # answered: no longer shown as waiting


def test_the_owners_page_answers_a_waiting_step_while_the_requests_chat_works_in_the_same_hermes(linked, monkeypatch):
    """With Hermes Desktop on the asked computer, a request's turn runs attached to the
    running Hermes, and Hermes keeps that chat's number in its own process environment for the commands the chat
    starts. The Twins page's route runs in that same process and is no chat. Its "Do it once" and "Don't" were
    refused as if the request had answered its own step, and the request ran out after five minutes."""
    import sys
    import types
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    chat = "local-" + "ab12" * 16
    for name in (tasks.ASKED_ENV, *tasks.SESSION_VARS):
        monkeypatch.delenv(name, raising=False)
    # Hermes' own lookup: the value bound to this call when there is one, else the process environment
    bound = {}
    ctx = types.ModuleType("gateway.session_context")
    ctx.get_session_env = lambda name, default="": bound[name] if name in bound else os.environ.get(name, default)
    monkeypatch.setitem(sys.modules, "gateway.session_context", ctx)
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_tasks_test3", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/p")
    c = TestClient(app)

    def waiting(n):
        tid = "k-00000000000000d%d" % n
        tasks.log_entry("in", tid, "Orchard", "Test Scout", "please tidy up", "waiting_approval")
        tasks.set_pending(tid, {"command": "echo tidy", "description": "a step"})
        return tid

    # the request's chat is working in this Hermes: marked, and its number sits in the process environment
    tasks.mark_turn([chat], True)
    monkeypatch.setenv("HERMES_SESSION_ID", chat)
    for n, choice in ((1, "once"), (2, "deny")):
        tid = waiting(n)
        mark = c.get("/p/tasks").json()["waiting"][0]["q"]
        # a command that chat starts inherits the number: still refused (the terminal's `requests approve`)
        with pytest.raises(tasks.TaskError) as exc:
            linked.tasks_approve(tid, choice, mark)
        assert exc.value.code == "not_from_a_request" and tasks.decision(tid) is None
        # the page: answered
        view = c.post(f"/p/tasks/{tid}/approve", json={"choice": choice, "q": mark}).json()
        assert "error" not in view and tasks.decision(tid) == choice and view["waiting"] == []
        tasks.set_pending(tid, None)
    # the same while a single-question turn of a request runs here ("no chat may answer": the page is no chat)
    tasks.mark_single(True)
    tid = waiting(3)
    assert "error" not in c.post(f"/p/tasks/{tid}/approve", json={"choice": "once"}).json()
    assert tasks.decision(tid) == "once"
    tasks.set_pending(tid, None)
    tasks.mark_single(False)
    # what still refuses the route: a value Hermes bound to this very call that is the request's chat ...
    tid = waiting(4)
    bound["HERMES_SESSION_KEY"] = f"agent:test-scout:local:dm:{chat}"
    out = c.post(f"/p/tasks/{tid}/approve", json={"choice": "once"}).json()
    assert out["error"]["code"] == "not_from_a_request" and tasks.decision(tid) is None
    bound.clear()
    # ... and a process that a request's stand-alone turn started
    monkeypatch.setenv(tasks.ASKED_ENV, "1")
    out = c.post(f"/p/tasks/{tid}/approve", json={"choice": "once"}).json()
    assert out["error"]["code"] == "not_from_a_request" and tasks.decision(tid) is None
    monkeypatch.delenv(tasks.ASKED_ENV)
    assert tasks.asked_turn() is True and tasks.asked_turn(env=False) is False
    tasks.mark_turn([chat], False)


def test_hermes_is_recognised_by_what_is_loaded_never_by_importing_it(monkeypatch):
    import sys
    import types
    from hermes_plugins.grok_bot_twin.twin import tasks_turn
    assert tasks_run.in_hermes() is False and tasks_turn.transports() == []  # the test runner is not Hermes
    before = set(sys.modules)
    tasks_run.hermes_command()
    assert not [m for m in set(sys.modules) - before if m.startswith(("hermes_cli.main", "tui_gateway"))]
    for name in tasks_run.HERMES_MODULES:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        assert tasks_run.in_hermes() is True
        assert tasks_run.hermes_command() == [sys.executable, "-P", "-m", "hermes_cli.main"]
        ways = tasks_turn.transports()  # attach to a running Hermes first, the stand-alone interface second
        assert [d for _a, d in ways] == ["attached", "stdio"] and ways[1][0] == [sys.executable, "-P", "-m", "tui_gateway.entry"]
        assert ways[0][0] == [sys.executable, "-P", str(ROOT / "twin" / "turn_bridge.py")]
        env = tasks_run.turn_env()  # ... with the import path this process has, so the same Hermes code is found
        assert all(p in env["PYTHONPATH"].split(os.pathsep) for p in sys.path if p and os.path.isabs(p))
        monkeypatch.delitem(sys.modules, name)
    # started as `python -m hermes_cli.main ...` the module is called __main__: its spec still says what it is
    main = types.ModuleType("__main__")
    main.__spec__ = types.SimpleNamespace(name="hermes_cli.main")
    monkeypatch.setitem(sys.modules, "__main__", main)
    assert tasks_run.in_hermes() is True
    # A twin opened in Hermes Desktop gets its plugins in a process of its own, started as
    # `python -m hermes_cli.plugin_host_child`. The requests may be done from there: it is
    # Hermes, so a step that needs approval waits for the owner instead of being refused in the single-question mode.
    main.__spec__ = types.SimpleNamespace(name="hermes_cli.plugin_host_child")
    assert tasks_run.in_hermes() is True
    assert [d for _a, d in tasks_turn.transports()] == ["attached", "stdio"]
    assert tasks_run.hermes_command() == [sys.executable, "-P", "-m", "hermes_cli.main"]
    main.__spec__ = types.SimpleNamespace(name="some_other_program")
    assert tasks_run.in_hermes() is False and tasks_turn.transports() == []
    main.__spec__ = types.SimpleNamespace(name="hermes_cli.main")
    source = (ROOT / "twin" / "tasks_run.py").read_text("utf-8") + (ROOT / "twin" / "tasks_turn.py").read_text("utf-8")
    assert "import hermes_cli" not in source and "import tui_gateway" not in source
    # the bridge to a running Hermes only copies lines: it holds no choice and no call name of its own
    bridge = (ROOT / "twin" / "turn_bridge.py").read_text("utf-8")
    assert "from ." not in bridge and "hermes_plugins" not in bridge  # a program of its own
    from hermes_plugins.grok_bot_twin.twin import fleet_profiles as fp
    assert "twin" in fp.PLUGIN_PARTS and "tools" not in fp.PLUGIN_PARTS  # ... inside what a twin's own bot gets
    for word in ('"once"', '"always"', '"session"', "approval.respond", "yolo", "choice"):
        assert word not in bridge, word


def test_the_bridge_never_loads_the_plugins_modules_in_place_of_pythons(tmp_path):
    """twin/ holds a secrets.py of the plugin's own. Run as a program from that folder, the bridge must still see
    Python's `secrets` (Hermes' client needs it): with and without the -P switch."""
    import subprocess
    import sys
    bridge = ROOT / "twin" / "turn_bridge.py"
    probe = ("import runpy, sys\n"
             "sys.argv = [%r]\n"
             "try:\n"
             "    runpy.run_path(%r, run_name='not_main')\n"
             "finally:\n"
             "    import secrets\n"
             "    print('token_bytes' in dir(secrets), %r in [__import__('os').path.abspath(p or '.') for p in sys.path])\n"
             % (str(bridge), str(bridge), str(ROOT / "twin")))
    for flags in ([], ["-P"]):
        cp = subprocess.run([sys.executable, *flags, "-c", probe], capture_output=True, text=True, timeout=60,
                            cwd=str(ROOT / "twin"))
        assert cp.stdout.strip().endswith("True False"), (flags, cp.stdout, cp.stderr[-400:])


# ---- the chat turn of a request cannot answer its own step or pass the request on -----------------------------------------
def test_a_request_turn_is_known_by_its_chat_even_inside_a_hermes_that_was_already_running(linked, monkeypatch):
    """Attached to a running Hermes, the turn's tools run in that Hermes, which was started long before: the
    variable that marks a stand-alone turn is not there. The turn's chat number is, in everything the chat
    starts, so the plugin notes that number while the turn runs."""
    import os
    import sys
    import types
    chat = "local-" + "ab12" * 16
    tid = "k-00000000000000f1"
    tasks.log_entry("in", tid, "mini", "Test Scout", "tidy up", "waiting_approval")
    tasks.set_pending(tid, {"command": "rm -rf ./x", "description": "recursive delete"})
    for name in (tasks.ASKED_ENV, *tasks.SESSION_VARS):
        monkeypatch.delenv(name, raising=False)
    assert tasks.asked_turn() is False
    tasks.mark_turn([chat, None, "x", 5], True)  # (only a plausible chat number is kept)
    assert list(tasks._marked_chats()) == [chat]
    assert tasks.asked_turn() is False  # the owner's own terminal and page: no chat number, or another chat's
    monkeypatch.setenv("HERMES_SESSION_ID", "local-" + "cd34" * 16)
    assert tasks.asked_turn() is False
    tasks.decide(tid, "once")
    tasks.set_pending(tid, {"command": "rm -rf ./x", "description": "recursive delete"})
    # a command started by the request's own chat carries that chat's number, alone or inside a longer key
    for var, value in (("HERMES_SESSION_ID", chat), ("HERMES_SESSION_KEY", f"agent:test-scout:local:dm:{chat}")):
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        monkeypatch.setenv(var, value)
        assert tasks.asked_turn() is True
        with pytest.raises(tasks.TaskError) as exc:
            tasks.decide(tid, "once")
        assert exc.value.code == "not_from_a_request" and tasks.decision(tid) is None
        with pytest.raises(tasks.TaskError) as exc:
            linked.tasks_ask("mini", "pass it on", twin="Test Scout")
        assert exc.value.code == "passed_on"
        monkeypatch.delenv(var)
    # inside the running Hermes for that chat: Hermes' own per-chat value says which chat it is
    ctx = types.ModuleType("gateway.session_context")
    ctx.get_session_env = lambda name, default="": chat if name == "HERMES_SESSION_ID" else default
    monkeypatch.setitem(sys.modules, "gateway.session_context", ctx)
    linked.ctx = type("Ctx", (), {"profile_name": "test-scout"})()
    assert tasks.asked_turn() is True
    with pytest.raises(tasks.TaskError) as exc:
        linked.tasks_ask("mini", "pass it on", twin="Test Scout")
    assert exc.value.code == "passed_on"
    monkeypatch.delitem(sys.modules, "gateway.session_context")
    assert tasks.asked_turn([chat], in_chat=True) is True
    # when the turn ends the mark goes; a mark left by a Hermes that died is forgotten after an hour
    tasks.mark_turn([chat], False)
    monkeypatch.setenv("HERMES_SESSION_ID", chat)
    assert tasks._marked_chats() == {} and tasks.asked_turn() is False
    tasks.mark_turn([chat], True)
    monkeypatch.setattr(tasks, "TURN_MARK_S", -1)
    assert tasks.asked_turn() is False
    # the stand-alone turn's variable still marks it by itself
    monkeypatch.delenv("HERMES_SESSION_ID")
    monkeypatch.setenv(tasks.ASKED_ENV, "1")
    assert tasks.asked_turn() is True
    assert os.environ.get(tasks.ASKED_ENV) == "1" and tasks_run.turn_env()[tasks.ASKED_ENV] == "1"
    monkeypatch.delenv(tasks.ASKED_ENV)
    # a turn in Hermes' single-question mode has neither a chat number known beforehand nor a process of its
    # own for sure: while it runs, no chat here passes a request on or answers a question; the owner's page and
    # own terminal (no chat number) still answer
    monkeypatch.setattr(tasks, "TURN_MARK_S", 3600)
    tasks.mark_turn([chat], True)
    tasks.mark_single(True)
    assert list(tasks._marked_chats()) == [chat] and tasks._single_since()  # (one mark does not wipe the other)
    assert tasks.asked_turn() is False
    tasks.decide(tid, "once")
    tasks.set_pending(tid, {"command": "rm -rf ./x", "description": "recursive delete"})
    assert tasks.asked_turn(in_chat=True) is True
    monkeypatch.setenv("HERMES_SESSION_ID", "local-" + "ef56" * 16)  # any chat, not only a marked one
    assert tasks.asked_turn() is True
    with pytest.raises(tasks.TaskError) as exc:
        tasks.decide(tid, "once")
    assert exc.value.code == "not_from_a_request" and tasks.decision(tid) is None
    tasks.mark_turn([chat], False)
    assert tasks._single_since()
    tasks.mark_single(False)
    assert tasks._single_since() is None and tasks.asked_turn() is False
    assert tasks.asked_turn(in_chat=True) is False
    tasks.mark_single(True)
    monkeypatch.setattr(tasks, "RUN_LIMIT_S", -61)  # a mark left by a turn that died is forgotten by itself
    assert tasks._single_since() is None


def test_a_second_hermes_on_the_sharing_computer_never_opens_a_second_share_server(home, tmp_path):  # noqa: F811
    """The fault: a request's turn on the sharing computer started a Hermes there (no other was running beside
    `hermes twin fleet run`). That Hermes read the settings of the twin's own bot, which named another port than
    the main settings, so the port was free, and it opened a second share server and took the record over. The
    linked computers kept talking to the first server, this computer's own commands to the second: every request
    was answered "switched off". A process that finds a serving one that answers serves nothing itself."""
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "share_address": "127.0.0.1",
                         "share_port": 0, "machine_name": "mini"})
    store.central_dir().mkdir(parents=True, exist_ok=True)
    (store.central_dir() / "pairs.json").write_text(json.dumps({"pairs": [
        {"pair_id": "test-scout", "grok": {"id": "b-test-scout", "name": "Test Scout", "description": "Finds trails."},
         "hermes": {"profile": "test-scout"}, "created_at": "2000-01-01T09:00:00Z"}]}), encoding="utf-8")
    svc = service.get_service()
    svc.fleet_share(True)
    first = fleet_server.serving()
    assert first is not None
    inst = dict(fleet.load()["share"]["instance"])
    try:
        # the same computer as another Hermes process sees it: the record names a process that is not this one,
        # this process serves nothing, and its settings name another (free) port
        with fleet.locked():
            st = fleet.load()
            st["share"]["instance"]["pid"] = os.getpid() + 1
            fleet.save(st)
        fleet_server._server, fleet_server._other = None, (0.0, False)
        config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "share_address": "127.0.0.1",
                             "share_port": fleet_testlib.free_port(), "machine_name": "mini"})
        assert fleet_server.ensure(service.get_service) is None
        assert fleet_server._server is None  # no second server
        now = fleet.load()["share"]["instance"]
        assert (now["port"], now["secret"], now["pid"]) == (inst["port"], inst["secret"], os.getpid() + 1)
        # when the serving process is gone, this one takes over at its next look
        first.stop()
        fleet_server._other = (0.0, False)
        assert fleet_server.ensure(service.get_service) is None
        second = fleet_server._server
        assert second is not None and second is not first and second.alive()
        now = fleet.load()["share"]["instance"]
        assert now["pid"] == os.getpid() and now["secret"] != inst["secret"]
    finally:
        first.stop()
        svc.fleet_share(False)


def test_the_step_an_owner_is_asked_to_approve_shows_its_file_paths_and_hides_keys(linked):
    """The fault: the step "delete <a folder>" reached the owner as "rm -rf [not shown: looks
    like a password or key]", because a long path with digits looks random as one word. Nobody can approve
    that. A path is shown; what looks like a key is hidden, inside a path too."""
    hidden = "[not shown: looks like a password or key]"
    for cmd in ("rm -rf /home/example/sandbox/tmp/does-not-exist-5ff479df",
                "rm -rf C:/Users/example/sandbox/tmp/does-not-exist-032e481c",
                "del C:\\Users\\example\\sandbox\\tmp\\does-not-exist-032e481c",
                "cp ~/example/site-2000/build/output-v12.tar.gz ./backups/2000-01-01/output-v12.tar.gz",
                "tar -xf \"/srv/releases/2000-01-01/app-1.4.2-linux-x86_64.tar.gz\" --directory=/opt/app-1.4.2/current"):
        assert tasks.step_text(cmd) == cmd, cmd
    key = "sk-" + "a1B2c3D4e5" * 3
    blob = "9f8e7d6c5b4a3f2e1d0c" * 2  # 40 characters of hex
    assert tasks.step_text(f"curl -H 'Authorization: Bearer {key}' https://example.org/v1/things") == \
        f"curl -H 'Authorization: Bearer {hidden}' https://example.org/v1/things"
    assert tasks.step_text(f"cat /srv/data/{blob}/notes.txt") == f"cat /srv/data/{hidden}/notes.txt"
    for cmd in (f"export TOKEN=/{blob}", f"login --password /{blob}", f"deploy --api-key=/{blob}x/y"):
        out = tasks.step_text(cmd)
        assert blob not in out and hidden in out, out
    assert tasks.step_text("echo \u27e60\u27e7 /tmp/a") == "echo (0) /tmp/a"  # (the holder's own marks cannot be forged)
    assert tasks.step_text("x" * 5000).endswith("…") and len(tasks.step_text("/a" * 5000)) == 400
    assert tasks.step_text(None) == "" and tasks.step_text(7) == ""
    assert tasks.step_text("line one\nline\ttwo\x07") == "line one line two"
    # what the page and the terminal get is that text
    tid = "k-00000000000000f2"
    tasks.log_entry("in", tid, "mini", "Test Scout", "tidy up", "waiting_approval")
    tasks.set_pending(tid, {"command": "rm -rf /home/example/sandbox/tmp/does-not-exist-5ff479df",
                            "description": "recursive delete"})
    assert tasks.pending()[tid]["command"] == "rm -rf /home/example/sandbox/tmp/does-not-exist-5ff479df"
