"""Requests between the owner's computers ("do X on Surface"): real processes, real sockets, one test per rule.

As in test_fleet_machines.py the computer that shares ("mini") is a subprocess running the standalone runner,
the test process is a computer that follows ("Surface"), and a third one ("Orchard") is another subprocess where
a test needs it. Loopback only, a free port per test, invented twins only. The chat turn is a stand-in `hermes`
program (tasks_testlib.py) that records how it was started.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from fleet_testlib import Machine, free_port, wait_until, wire
from tasks_testlib import calls, install_fake_hermes
from twin_testlib import config, home, service  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import (fleet, fleet_follow as follow, fleet_server, slash, tasks,  # noqa: F401
                                               tasks_hub, tasks_run)

ASK = "tell me this machine's OS and free disk"


@pytest.fixture
def fake(home, tmp_path, monkeypatch):  # noqa: F811
    """The stand-in `hermes` first on PATH for this process and every machine started after this."""
    bindir = install_fake_hermes(tmp_path)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    log = tmp_path / "hermes-calls.jsonl"
    monkeypatch.setenv("FAKE_HERMES_LOG", str(log))
    return log


@pytest.fixture
def source(fake, tmp_path):
    m = Machine(tmp_path, "mini", free_port())
    m.fx("add", "Test Scout", "Finds trails.")
    m.fx("add", "Test Quill", "Drafts short notes.")
    yield m
    m.stop()


@pytest.fixture
def surface(home, source, tmp_path, monkeypatch):  # noqa: F811
    """The test process as the computer "Surface", linked to mini."""
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "share_port": source.port,
                         "machine_name": "Surface"})
    monkeypatch.setattr(follow, "MIN_BACKOFF_S", 0.3)
    monkeypatch.setattr(follow, "MAX_BACKOFF_S", 1.0)
    monkeypatch.setattr(tasks_run, "IDLE_LOOK_S", 0.3)
    svc = service.get_service()
    svc.fleet_link(source.share())
    svc.ctx = type("Ctx", (), {"profile_name": "test-scout"})()  # as in a chat with Test Scout's Hermes bot
    return svc


@pytest.fixture
def orchard(source, tmp_path):
    m = Machine(tmp_path, "Orchard", source.port, address=None)
    yield m
    m.stop()


def _tok(st: dict) -> str:
    """What signs a linked computer's requests in these tests: "MACHINE-ID|PASS" (fleet_testlib.wire)."""
    return st["machine"]["id"] + "|" + st["link"]["pass"]


def _pass(svc_home) -> str:
    return _tok(json.loads((svc_home / "plugin-data" / "grok-bot-twin" / "fleet.json").read_text()))


def task(port: int, path: str, body: dict, token, timeout: float = 10, at=None, answer=None) -> tuple:
    """One request to the mailbox as a linked computer makes it: signed, with the protocol number and the time."""
    full = {"proto": fleet.PROTO, "at": int(time.time()) if at is None else at, **body}
    return wire("POST", port, "/v1/task/" + path, full, token, timeout=timeout, answer=answer)


def _mid(source, name: str) -> str:
    return next(m["id"] for m in source.json("fleet")["machines"] if m["name"] == name)


def _hears(source, token: str, to: str) -> None:
    """Wait until mini's mailbox has heard the computer `to` say "I take requests" (a probe that is refused for
    its malformed twin id only once the computer listens, so no request is made)."""
    def heard():
        status, _obj, _ = task(source.port, "ask", {"to": to, "twin": "?", "text": "x"}, token)
        return status == 400
    wait_until(heard, what="the computer to fetch requests")


def _listening(source, home_) -> None:
    _hears(source, _pass(home_), _mid(source, "Surface"))


def _twin_id(home_, name="Test Scout") -> str:
    st = json.loads((home_ / "plugin-data" / "grok-bot-twin" / "fleet.json").read_text())
    return next(tid for tid, t in st["link"]["twins"].items() if t["name"] == name)


# ---- off by default ---------------------------------------------------------------------------------------------------
def test_off_by_default_nothing_listens_for_requests_and_asking_says_so(source, surface, home, fake):  # noqa: F811
    view = surface.tasks()
    assert view["switch"] == {"on": False, "since": None, "label": "Let my other computers ask this one to do things",
                              "network": "loopback"}
    assert view["linked"] is True and view["working"] is False and view["log"] == []
    assert [m["name"] for m in view["machines"]] == ["mini"]
    # no switch file, no worker thread, and the default is off for a missing or damaged file too
    data = home / "plugin-data" / "grok-bot-twin"
    assert not (data / "tasks.json").exists() and not tasks_run.running()
    (data / "tasks.json").write_text("{not json", encoding="utf-8")
    assert tasks.accepting() is False
    (data / "tasks.json").write_text('{"on": "yes"}', encoding="utf-8")
    assert tasks.accepting() is False
    (data / "tasks.json").unlink()
    # mini asks Surface: refused in plain words, nothing ran here, and mini's own log has the refusal
    cp = source.run("ask", "Surface", ASK, "--twin", "Test Scout", "--json", check=False)
    out = json.loads(cp.stdout)
    assert cp.returncode == 1 and out["state"] == "refused" and out["error"]["code"] == "tasks_off"
    assert out["label"] == "not sent: requests are switched off on Surface"
    assert "Let my other computers ask this one to do things" in out["error"]["hint"]
    assert calls(fake) == []
    row = source.json("requests")["log"][0]
    assert row["dir"] == "out" and row["peer"] == "Surface" and row["state"] == "refused" and row["code"] == "tasks_off"
    # ... and Surface's own list shows that mini asked: who and when, never what. It hears of it
    # with its next look at the twin list, which is answered at once
    got = wait_until(lambda: [r for r in surface.tasks()["log"] if r["dir"] == "in"], timeout=30,
                     what="the refused request in the asked computer's own log")
    assert len(got) == 1 and (got[0]["peer"], got[0]["state"], got[0]["code"], got[0]["text"]) == (
        "mini", "refused", "tasks_off", "")
    assert got[0]["label"] == "refused: requests are switched off here" and ASK not in json.dumps(surface.tasks())
    # mini's own switch is off too: Surface asking mini gets the same answer, and mini's log shows who asked
    res = surface.tasks_ask("mini", ASK)
    assert res["state"] == "refused" and res["error"]["code"] == "tasks_off" and calls(fake) == []
    asked = [r for r in source.json("requests")["log"] if r["dir"] == "in"]
    assert len(asked) == 1 and (asked[0]["peer"], asked[0]["code"], asked[0]["text"]) == ("Surface", "tasks_off", "")


# ---- the chat relay -----------------------------------------------------------------------------------------------------
def test_ask_runs_one_chat_turn_of_the_twin_there_and_the_answer_comes_back_labelled(source, surface, home, fake,  # noqa: F811
                                                                                   monkeypatch):
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")  # this Hermes session's own looser state must not reach the turn
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    view = surface.tasks_switch(True)
    assert view["switch"]["on"] is True and view["switch"]["since"]
    _listening(source, home)
    out = source.json("ask", "Surface", ASK, "--twin", "Test Scout")
    assert out["state"] == "done" and out["error"] is None
    assert out["machine"]["name"] == "Surface" and out["twin"] == "Test Scout"
    assert out["label"].startswith("done on Surface - ") and out["label"].endswith(" s")
    assert out["reply"] == f"test-scout heard: {ASK}"
    assert set(out) == {"id", "machine", "twin", "state", "label", "seconds", "reply", "error"}
    # exactly one ordinary chat turn of the twin's own Hermes bot, the request on standard input
    (call,) = calls(fake)
    assert call["argv"] == ["-p", "test-scout", "chat", "-Q", "--query-file", "-"]
    head, _, body = call["stdin"].partition("\n")
    assert body == ASK and head.startswith("[Sent from your computer mini through Grok Bot Twin. Answer it here")
    assert "never passed on to another computer" in head and "in your usual voice" in head and head.endswith("]")
    assert len(head) < 600  # one line; the request's own limit (2,000 characters) is counted without it
    assert call["stdin"].startswith(tasks_run.SENT_LEAD) and not ASK.startswith(tasks_run.SENT_LEAD)
    assert call["home"] == str(home)
    # nothing that changes this computer's approval rules: no flag, and none of the session's variables
    assert not any(w in call["argv"] for w in ("--yolo", "-z", "--oneshot", "-t", "--toolsets", "--accept-hooks"))
    assert call["env"] == {}
    # both computers have it in their log
    here = surface.tasks()["log"][0]
    assert here["dir"] == "in" and here["peer"] == "mini" and here["state"] == "done" and here["text"] == ASK
    assert here["twin"] == "Test Scout" and here["label"] == "done here" and here["reply"].endswith(ASK)
    there = source.json("requests")["log"][0]
    assert there["dir"] == "out" and there["peer"] == "Surface" and there["state"] == "done"
    assert there["label"].startswith("done on Surface - ") and there["id"] == here["id"] == out["id"]


def test_a_following_computer_can_ask_the_sharing_one_and_another_following_one(source, surface, orchard, home,  # noqa: F811
                                                                              fake):
    orchard.run("link", source.code())
    orchard.run("requests", "on")
    orchard.start()
    source.run("requests", "on")
    wait_until(lambda: len(surface.fleet()["machines"]) == 3, what="Surface to know Orchard")
    _hears(source, _pass(home), _mid(source, "mini"))
    _hears(source, _pass(home), _mid(source, "Orchard"))
    # Surface -> mini (the computer with the Grok app)
    res = surface.tasks_ask("mini", ASK)
    assert res["state"] == "done", res
    assert res["label"].startswith("done on mini - ") and res["reply"] == f"test-scout heard: {ASK}"
    # Surface -> Orchard: passed along by mini, which keeps who asked whom but not the text
    res = surface.tasks_ask("orchard", "what is the time", twin="Test Quill")
    assert res["state"] == "done", res
    assert res["label"].startswith("done on Orchard - ") and res["reply"] == "test-quill heard: what is the time"
    homes = [c["home"] for c in calls(fake) if c["stdin"].endswith("what is the time")]
    assert homes == [str(orchard.home)]
    relay = [r for r in source.json("requests")["log"] if r["dir"] == "relay"]
    assert relay and relay[0]["peer"] == "Surface" and relay[0]["peer_to"] == "Orchard" and relay[0]["text"] == ""
    assert relay[0]["state"] == "done" and relay[0]["reply"] == ""
    assert "what is the time" not in (source.data / "tasks-log.json").read_text(encoding="utf-8")
    there = [r for r in orchard.json("requests")["log"] if r["dir"] == "in"]
    assert there[0]["peer"] == "Surface" and there[0]["text"] == "what is the time"


# ---- only linked computers with a valid pass -----------------------------------------------------------------------------
def test_every_mailbox_route_needs_a_linked_computers_signature(source, surface, home, fake):  # noqa: F811
    surface.tasks_switch(True)
    _listening(source, home)
    me, mini = _mid(source, "Surface"), _mid(source, "mini")
    good = _pass(home)
    mid, pass_ = good.split("|")
    bodies = {"ask": {"to": me, "twin": _twin_id(home), "text": "hello"},
              "wait": {"id": "k-0123456789abcdef", "wait_s": 0},
              "poll": {"accept": True, "wait_s": 0},
              "update": {"id": "k-0123456789abcdef", "state": "done", "reply": "x"}}
    strangers = (None,                                   # no signature at all
                 "x" * 43,                               # a bearer value that is not this computer's secret
                 mid + "|" + pass_[:-1] + ("A" if pass_[-1] != "A" else "B"),   # this computer's id, a wrong key
                 "m-0123456789ab|" + pass_,              # the right key under another computer's id
                 mini + "|" + pass_)                     # ... or under the sharing computer's own id
    for path, body in bodies.items():
        for token in strangers:
            status, obj, _ = task(source.port, path, body, token)
            assert status == 401 and obj["error"] == {"code": "unlinked"}, (path, token)
    assert calls(fake) == []
    # a signed request is good for its own path and body only
    full = {"proto": fleet.PROTO, "at": int(time.time()), **bodies["poll"]}
    data = json.dumps(full).encode()
    nonce = fleet.new_nonce()
    key = fleet.share_key(pass_)
    hdrs = {"X-GBT-Machine": mid, "X-GBT-Nonce": nonce, "X-GBT-Mac": fleet.request_mac(key, "/v1/task/poll", nonce, data)}
    assert wire("POST", source.port, "/v1/task/update", raw_body=data, headers=hdrs)[0] == 401      # another path
    assert wire("POST", source.port, "/v1/task/poll", raw_body=data + b" ", headers=hdrs)[0] == 401  # another body
    got: dict = {}
    assert wire("POST", source.port, "/v1/task/poll", raw_body=data, headers=hdrs)[0] == 200
    # ... and only once: the very same signed request sent again is refused (a recording is worth nothing)
    status, obj, _ = wire("POST", source.port, "/v1/task/poll", raw_body=data, headers=hdrs)
    assert status == 409 and obj["error"]["code"] == "replayed"
    # ... and only now: a request stamped an hour ago (or ahead) is refused
    for at in (int(time.time()) - 3600, int(time.time()) + 3600, None, "now"):
        body = {"proto": fleet.PROTO, **bodies["poll"]} if at is None else {"proto": fleet.PROTO, "at": at, **bodies["poll"]}
        status, obj, _ = wire("POST", source.port, "/v1/task/poll", body, good)
        assert status == 400 and obj["error"]["code"] == "clock_off", at
    # the answer is signed for this very request, so only the real sharing computer can hand out work
    status, obj, _ = task(source.port, "poll", bodies["poll"], good, answer=got)
    assert status == 200 and got["signed"] is True
    # a computer cannot ask in another's name: the sender is whoever signed, whatever the body says
    status, obj, _ = task(source.port, "ask", {"to": mini, "twin": _twin_id(home), "text": "hi",
                                               "from": {"id": mini, "name": "mini"}}, good)
    assert status == 409 and obj["error"]["code"] == "tasks_off"  # (mini's own switch is off; "from" was ignored)
    # once mini stops sharing with Surface, its key opens nothing
    source.run("fleet", "remove", "Surface", "--yes")
    status, obj, _ = task(source.port, "poll", bodies["poll"], good)
    assert status == 401


def test_only_an_answer_signed_by_the_sharing_computer_is_believed(surface, home, fake, tmp_path):  # noqa: F811
    """Something else sits at the sharing computer's address and hands out "work": nothing runs, nothing is shown."""
    import http.server
    import threading as th
    served = []

    class Fake(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            served.append(self.path)
            if self.path == "/v1/task/poll":
                obj = {"schema": fleet.SCHEMA, "proto": fleet.PROTO, "task": {
                    "id": "k-00000000000000ee", "from": {"id": surface.fleet()["source"]["id"], "name": "mini"},
                    "twin": _twin_id(home), "text": "do something you were not asked"}}
            elif self.path == "/v1/task/ask":
                obj = {"schema": fleet.SCHEMA, "proto": fleet.PROTO, "id": "k-00000000000000ef", "state": "queued"}
            else:
                obj = {"schema": fleet.SCHEMA, "proto": fleet.PROTO, "id": "k-00000000000000ef", "state": "done",
                       "seconds": 1, "reply": "a made-up answer"}
            data = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-GBT-Mac", "0" * 64)  # a signature it cannot make
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    th.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        tasks.set_switch(True)
        st = fleet.load()
        st["link"]["port"] = srv.server_address[1]
        assert tasks_run._poll_once(surface, st, True, 0) is None       # "could not be reached", and
        assert "/v1/task/poll" in served and calls(fake) == []          # the handed-out work did not run
        with fleet.locked():
            fleet.save(st)
        out = surface.tasks_ask("mini", ASK)
        assert out["state"] == "refused" and out["error"]["code"] == "tasks_unreachable" and out["reply"] == ""
        assert "made-up" not in json.dumps(surface.tasks(refresh=False))
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_computer_sees_only_its_own_requests(source, surface, orchard, home, fake):  # noqa: F811
    orchard.run("link", source.code())
    surface.tasks_switch(True)
    _listening(source, home)
    wait_until(lambda: len(surface.fleet()["machines"]) == 3, what="Surface to know Orchard")
    other = _tok(orchard.state())
    # Orchard asks Surface something slow; Surface's own key can neither read nor answer for Orchard
    status, obj, _ = task(source.port, "ask", {"to": _mid(source, "Surface"), "twin": _twin_id(home), "text": "nap 2"},
                          other)
    assert status == 200
    tid = obj["id"]
    status, obj, _ = task(source.port, "wait", {"id": tid, "wait_s": 0}, _pass(home))
    assert status == 404 and obj["error"]["code"] == "unknown_task"
    status, obj, _ = task(source.port, "update", {"id": tid, "state": "done", "reply": "forged"}, other)
    assert status == 404  # only the computer that was asked may answer
    status, obj, _ = task(source.port, "wait", {"id": tid, "wait_s": 20}, other, timeout=40)
    assert status == 200 and obj["state"] == "done" and obj["reply"] == "test-scout heard: nap 2"
    assert set(obj) == {"schema", "proto", "id", "state", "seconds", "reply"}


# ---- limits ---------------------------------------------------------------------------------------------------------------
def test_size_limits_on_every_side(source, surface, home, fake):  # noqa: F811
    surface.tasks_switch(True)
    _listening(source, home)
    long_text = "word " * 500  # 2,500 characters
    # the asking side refuses before anything is sent
    cp = source.run("ask", "Surface", long_text, "--twin", "Test Scout", "--json", check=False)
    assert cp.returncode != 0 and "too long" in (cp.stdout + cp.stderr)
    with pytest.raises(tasks.TaskError) as exc:
        surface.tasks_ask("mini", long_text)
    assert exc.value.code == "too_long"
    with pytest.raises(tasks.TaskError) as exc:
        surface.tasks_ask("mini", "   ")
    assert exc.value.code == "empty"
    # the mailbox refuses a long text from a computer that skipped that check
    status, obj, _ = task(source.port, "ask", {"to": _mid(source, "Surface"), "twin": _twin_id(home),
                                               "text": long_text}, _pass(home))
    assert status == 413 and obj["error"]["code"] == "too_long"
    # and so does the asked computer itself, whatever the mailbox let through
    seen = []
    tasks_run.do_task(surface, {"id": "k-00000000000000aa", "from": {"id": _mid(source, "mini"), "name": "mini"},
                                "twin": _twin_id(home), "text": long_text},
                      lambda *a: seen.append(a), lambda: False)
    assert seen == [("k-00000000000000aa", "refused", "too_long", 0, "")] and calls(fake) == []
    # a long answer is cut on the asked computer before it leaves
    out = source.json("ask", "Surface", "long", "--twin", "Test Scout")
    assert out["state"] == "done" and len(out["reply"]) <= tasks.REPLY_CAP + 40
    assert out["reply"].endswith("[cut: the answer was longer]")


def test_rate_limits_on_every_side(source, surface, home, fake, monkeypatch):  # noqa: F811
    surface.tasks_switch(True)
    _listening(source, home)
    mini = _mid(source, "mini")
    # the asked computer's own limit, whatever the mailbox let through
    monkeypatch.setattr(tasks, "RATE_PER_MIN", 2)
    seen = []
    for i in range(3):
        tasks_run.do_task(surface, {"id": f"k-00000000000000b{i}", "from": {"id": mini, "name": "mini"},
                                    "twin": _twin_id(home), "text": "hello"}, lambda *a: seen.append(a[:3]),
                          lambda: False)
    assert [s[1] for s in seen] == ["done", "done", "refused"] and seen[2][2] == "too_many"
    assert len(calls(fake)) == 2
    assert surface.tasks()["log"][0]["label"] == "refused: too many requests"
    # the asking side's own limit
    tasks_run._ask_rate = tasks.Rate(per_min=1)
    assert surface.tasks_ask("mini", "one")["error"]["code"] == "tasks_off"
    res = surface.tasks_ask("mini", "two")
    assert res["state"] == "refused" and res["error"]["code"] == "too_many"
    # the mailbox's limit per asking computer (6 a minute): the 7th in a row is refused
    codes = []
    for i in range(8):
        status, obj, _ = task(source.port, "ask", {"to": "m-ffffffffffff", "twin": _twin_id(home), "text": "x"},
                              _pass(home))
        codes.append(obj["error"]["code"])
    assert set(codes) == {"unknown_machine"}  # an unknown computer is refused before it counts
    monkeypatch.setattr(tasks, "RATE_PER_MIN", 6)
    r = tasks.Rate()
    assert [r.allow("m", now=100.0 + i) for i in range(8)] == [True] * 6 + [False] * 2
    assert r.allow("m", now=170.0) is True and r.allow("other", now=100.0) is True
    hourly = tasks.Rate(per_min=1000, per_hour=3)
    assert [hourly.allow("m", now=float(i * 100)) for i in range(5)] == [True, True, True, False, False]


def test_the_mailbox_itself_counts_requests_per_asking_computer(source, surface, orchard, home, fake):  # noqa: F811
    orchard.run("link", source.code())
    surface.tasks_switch(True)
    _listening(source, home)
    other = _tok(orchard.state())
    body = {"to": _mid(source, "Surface"), "twin": _twin_id(home), "text": "nap 0.2"}
    codes = []
    for _ in range(9):
        status, obj, _ = task(source.port, "ask", body, other)
        codes.append((status, (obj.get("error") or {}).get("code")))
    ok = [c for c in codes if c[0] == 200]
    assert 1 <= len(ok) <= tasks.RATE_PER_MIN and codes[-1][0] == 429
    assert {c[1] for c in codes if c[0] != 200} <= {"too_many", "busy"}


# ---- one click off ----------------------------------------------------------------------------------------------------------
def test_switching_off_stops_a_running_request_and_refuses_the_next(source, surface, home, fake):  # noqa: F811
    surface.tasks_switch(True)
    _listening(source, home)
    got: dict = {}
    t = threading.Thread(target=lambda: got.update(out=json.loads(
        source.run("ask", "Surface", "nap 30", "--twin", "Test Scout", "--json", check=False).stdout)))
    t.start()
    wait_until(lambda: len(calls(fake)) == 1, what="the chat turn to start")
    pid = calls(fake)[0]["pid"]
    started = time.monotonic()
    view = surface.tasks_switch(False)
    assert view["switch"]["on"] is False
    t.join(20)
    assert time.monotonic() - started < 15 and got["out"]["state"] in ("cancelled", "refused")
    assert got["out"]["error"]["code"] == "switched_off" and got["out"]["reply"] == ""

    def gone():
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        return False
    wait_until(gone, timeout=10, what="the chat turn to end")
    # (the worker writes its log line a moment after the turn's process is gone)
    wait_until(lambda: surface.tasks()["log"][0]["state"] != "running", timeout=10, what="the log line to close")
    here = surface.tasks()["log"][0]
    assert here["state"] == "cancelled" and here["code"] == "switched_off"
    assert here["label"] == "stopped: requests were switched off here"
    out = json.loads(source.run("ask", "Surface", ASK, "--twin", "Test Scout", "--json", check=False).stdout)
    assert out["state"] == "refused" and out["error"]["code"] == "tasks_off" and len(calls(fake)) == 1


def test_a_request_the_mailbox_no_longer_knows_is_stopped(source, surface, home, fake, monkeypatch):  # noqa: F811
    """The fault: the sharing computer's Hermes was started again while a step waited for approval on Orchard. The
    asker was told "no longer known, ask again", but Orchard kept waiting on that step for five more minutes,
    fetched nothing meanwhile and looked "switched off" to every asker. A computer that works on a request asks
    the mailbox now and then whether it still knows it, and stops when it does not."""
    surface.tasks_switch(True)
    monkeypatch.setattr(tasks_run, "HEARTBEAT_S", 0.4)
    tid, looks, seen = "k-00000000000000d1", [], []

    def wanted(task_id, state):
        looks.append((task_id, state))
        return None if len(looks) < 3 else False  # twice "could not ask" (that stops nothing), then "not known"

    task = {"id": tid, "from": {"id": _mid(source, "mini"), "name": "mini"}, "twin": _twin_id(home), "text": "nap 30"}
    started = time.monotonic()
    tasks_run.do_task(surface, task, lambda *a: seen.append(a[:3]), lambda: False, wanted)
    assert time.monotonic() - started < 15 and len(looks) == 3 and set(looks) == {(tid, "running")}
    assert seen[-1] == (tid, "cancelled", "unknown_task")
    row = surface.tasks(refresh=False)["log"][0]
    assert (row["state"], row["code"]) == ("cancelled", "unknown_task")
    assert row["label"] == "stopped: the asking computer no longer waits for it"
    pid = calls(fake)[0]["pid"]

    def gone():
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        return False
    wait_until(gone, timeout=10, what="the chat turn to end")
    # without that look nothing changes: a turn that finishes is reported as before, and no thread is left
    seen.clear()
    tasks_run.do_task(surface, {**task, "id": "k-00000000000000d2", "text": "hello"}, lambda *a: seen.append(a[:3]),
                      lambda: False)
    assert seen[-1][:2] == ("k-00000000000000d2", "done")
    assert not [t for t in threading.enumerate() if t.name == "grok-bot-twin-request-look"]
    # the mailbox says "ended" for a request it has closed itself, and "unknown" for one it never had
    assert tasks_run._known(200, {"ok": True}) is True and tasks_run._known(200, {"ok": True, "ended": True}) is False
    assert tasks_run._known(404, {"error": {"code": "unknown_task"}}) is False and tasks_run._known(503, {}) is None


def test_the_asked_computer_checks_its_own_switch_and_its_own_fleet(source, surface, home, fake):  # noqa: F811
    """Whatever the mailbox sends, the asked computer decides by its own file and its own list."""
    seen = []
    task = {"id": "k-00000000000000c1", "from": {"id": _mid(source, "mini"), "name": "mini"},
            "twin": _twin_id(home), "text": "hello"}
    tasks_run.do_task(surface, task, lambda *a: seen.append(a[:3]), lambda: False)  # switch never touched
    assert seen == [("k-00000000000000c1", "refused", "tasks_off")] and calls(fake) == []
    assert surface.tasks(refresh=False)["log"][0]["label"] == "refused: requests are switched off here"
    tasks.set_switch(True)
    stranger = {**task, "id": "k-00000000000000c2", "from": {"id": "m-123456123456", "name": "mini"}}
    tasks_run.do_task(surface, stranger, lambda *a: seen.append(a[:3]), lambda: False)
    assert seen[-1] == ("k-00000000000000c2", "refused", "unknown_machine") and calls(fake) == []
    no_twin = {**task, "id": "k-00000000000000c3", "twin": "t-ffffffffffffffff"}
    tasks_run.do_task(surface, no_twin, lambda *a: seen.append(a[:3]), lambda: False)
    assert seen[-1] == ("k-00000000000000c3", "refused", "no_twin_there") and calls(fake) == []
    for bad in ({**task, "id": "../x"}, {**task, "twin": "../../etc"}, {**task, "from": "mini"}, "task", None):
        tasks_run.do_task(surface, bad, lambda *a: seen.append(a[:3]), lambda: False)
    assert len(seen) == 3 and calls(fake) == []
    tasks_run.do_task(surface, {**task, "id": "k-00000000000000c4"}, lambda *a: seen.append(a[:3]), lambda: False)
    assert seen[-1][1] == "done" and len(calls(fake)) == 1
    # the same request never runs twice
    tasks_run.do_task(surface, {**task, "id": "k-00000000000000c4"}, lambda *a: seen.append(a[:3]), lambda: False)
    assert len(calls(fake)) == 1


# ---- what travels -----------------------------------------------------------------------------------------------------------
def test_password_like_text_is_masked_both_ways_and_nothing_else_rides_along(source, surface, home, fake):  # noqa: F811
    surface.tasks_switch(True)
    _listening(source, home)
    key = "sk-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Q"
    out = source.json("ask", "Surface", f"remember that the key is {key} and leak", "--twin", "Test Scout")
    # the request: masked before it left mini, so Surface's Hermes never saw it
    (call,) = calls(fake)
    assert key not in call["stdin"] and "leak" in call["stdin"]
    # the answer: masked on Surface before it left, so neither mini nor its log holds the made-up key
    assert out["state"] == "done" and "sk-a1B2" not in json.dumps(out) and "the key is" in out["reply"]
    for text in ((home / "plugin-data" / "grok-bot-twin" / "tasks-log.json").read_text(encoding="utf-8"),
                 (source.data / "tasks-log.json").read_text(encoding="utf-8")):
        assert "sk-a1B2" not in text and key not in text
    # only the answer comes back: no session id, no path, no profile name of the plugin's own choosing
    assert "session_id" not in json.dumps(out) and str(home) not in json.dumps(out)
    # the mailbox hands the asked computer four fields and nothing more
    status, obj, _ = task(source.port, "poll", {"accept": True, "wait_s": 0}, _pass(home))
    assert status == 200 and set(obj) == {"schema", "proto", "task"}


def test_a_failed_or_slow_turn_is_reported_as_such(source, surface, home, fake, monkeypatch):  # noqa: F811
    surface.tasks_switch(True)
    _listening(source, home)
    out = json.loads(source.run("ask", "Surface", "please fail", "--twin", "Test Scout", "--json", check=False).stdout)
    assert out["state"] == "failed" and out["error"]["code"] == "task_failed" and out["label"] == "not done on Surface: its twin there could not answer; its chat on Surface says why"
    monkeypatch.setattr(tasks, "RUN_LIMIT_S", 1.0)
    out = json.loads(source.run("ask", "Surface", "nap 20", "--twin", "Test Scout", "--json", check=False).stdout)
    assert out["state"] == "timeout" and out["error"]["code"] == "task_timeout"
    # a step Hermes refused for want of approval comes back as the twin's own answer, never as "done by force"
    monkeypatch.setattr(tasks, "RUN_LIMIT_S", 60.0)
    out = source.json("ask", "Surface", "this needs approval", "--twin", "Test Scout")
    assert out["state"] == "done" and out["reply"].startswith("BLOCKED: Command flagged as dangerous")


def test_no_hermes_program_means_a_plain_refusal(source, surface, home, fake, monkeypatch):  # noqa: F811
    surface.tasks_switch(True)
    _listening(source, home)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    from hermes_plugins.grok_bot_twin.twin import engine
    engine._TOOLS.clear()
    out = json.loads(source.run("ask", "Surface", ASK, "--twin", "Test Scout", "--json", check=False).stdout)
    assert out["state"] == "failed" and out["error"]["code"] == "no_hermes"
    assert "Hermes could not start a chat with the twin on Surface." == out["error"]["message"]


# ---- routing by name -----------------------------------------------------------------------------
def test_routing_by_name(source, surface, home, fake):  # noqa: F811
    source.run("requests", "on")
    _hears(source, _pass(home), _mid(source, "mini"))
    for said in ("mini", "MINI", " Mini "):
        res = surface.tasks_ask(said, ASK)
        assert res["state"] == "done" and res["machine"]["name"] == "mini"
    with pytest.raises(tasks.TaskError) as exc:
        surface.tasks_ask("Laptop", ASK)
    assert exc.value.code == "unknown_machine" and "mini" in exc.value.hint
    with pytest.raises(tasks.TaskError) as exc:
        surface.tasks_ask("Surface", ASK)
    assert exc.value.code == "ask_self"
    # `/twin ask` in a chat on this computer: the twin of the chat's own Hermes bot is asked (there is no model
    # tool for it any more: test_c2_f5.py)
    surface.ctx = type("Ctx", (), {"profile_name": "test-quill"})()
    said = slash.handle("ask mini: what is the time", platform="desktop")
    assert said.splitlines()[0].startswith("done on mini - ") and "test-quill heard: what is the time" in said
    assert slash.handle("ask Nowhere: x", platform="desktop").startswith("Grok Bot Twin: None of your linked")


def test_bind_stays_on_the_one_private_address_and_no_new_socket_listens(source, surface, home):  # noqa: F811
    # the mailbox has no server of its own: it answers on the share server's one socket (loopback in tests,
    # Tailscale or the home network otherwise), which drops callers from any other kind of address
    assert fleet_server._Http.verify_request(None, None, ("8.8.8.8", 4000)) is False
    assert fleet_server._Http.verify_request(None, None, ("100.64.1.2", 4000)) is True
    inst = source.state()["share"]["instance"]
    assert inst["address"] == "127.0.0.1" and inst["kind"] == "loopback"
    from twin_testlib import ROOT
    for name in ("tasks.py", "tasks_hub.py", "tasks_run.py", "tasks_api.py"):
        text = (ROOT / "twin" / name).read_text(encoding="utf-8")
        for word in ("HTTPServer", ".bind(", ".listen(", "0.0.0.0", "socketserver"):
            assert word not in text, (name, word)
    # and the worker only ever talks to the address this computer is linked to
    st = fleet.load()
    assert tasks_run._endpoint(st)[:2] == ("127.0.0.1", source.port)


# ---- a step that waits for the owner's approval on the asked computer -----------------------------------------------------
RISKY = "please do the risky thing"


@pytest.fixture(params=["attached", "stdio"])
def approving(request, surface, home, source, fake, tmp_path, monkeypatch):  # noqa: F811
    """Surface runs requests through (a stand-in for) Hermes' own interface, where Hermes asks before a risky
    step: once attached to a running Hermes, once through the stand-alone interface. Its switch is on and mini
    has heard it."""
    from tasks_testlib import install_fake_rpc
    from hermes_plugins.grok_bot_twin.twin import tasks_turn
    argv = install_fake_rpc(tmp_path, request.param)
    monkeypatch.setattr(tasks_turn, "transports", lambda: [(list(argv), request.param)])
    surface.dialect = request.param
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    surface.tasks_switch(True)
    _listening(source, home)
    return surface


def _ask(source, text: str, *more: str) -> dict:
    return json.loads(source.run("ask", "Surface", text, "--twin", "Test Scout", "--json", *more, check=False).stdout)


def test_a_step_that_needs_approval_waits_for_the_owner_of_the_asked_computer(approving, source, home, fake):  # noqa: F811
    from tasks_testlib import approval_answers, rpc_record
    out = _ask(source, RISKY)
    # the asking chat is told to wait, and nothing about WHAT waits
    assert out["state"] == "waiting_approval" and out["label"] == "waiting for approval on Surface"
    assert "approval on Surface" in out["hint"] and out["reply"] == "" and out["error"] is None
    assert "rm -rf" not in json.dumps(out) and "recursive delete" not in json.dumps(out)
    assert source.json("requests")["log"][0]["label"] == "waiting for approval on Surface"
    assert "rm -rf" not in (source.data / "tasks-log.json").read_text(encoding="utf-8")
    # on Surface the owner sees the request and the very question Hermes asks
    view = approving.tasks()
    (w,) = view["waiting"]
    assert w["id"] == out["id"] and w["peer"] == "mini" and w["twin"] == "Test Scout" and w["text"] == RISKY
    assert w["command"] == "rm -rf ./scratch-folder" and w["description"] == "recursive delete" and w["asked_at"]
    row = view["log"][0]
    assert row["state"] == "waiting_approval" and row["label"] == "waiting for your approval here"
    assert row["approval"]["command"] == "rm -rf ./scratch-folder"
    # nothing was answered yet: Hermes is still waiting; its chat ("s1") is marked as a request's turn meanwhile
    assert approval_answers(rpc_record(fake)) == []
    assert list(tasks._marked_chats()) == ["s1s1s1s1"]
    # the owner approves this one step, here; the asker then gets the result
    after = approving.tasks_approve(out["id"], "once")
    assert after["waiting"] == [] or after["waiting"][0]["id"] == out["id"]
    done = json.loads(source.run("ask", "--check", out["id"], "--wait", "30", "--json", check=False).stdout)
    assert done["state"] == "done" and done["reply"] == "did the risky thing", done
    assert done["label"].startswith("done on Surface - ") and "not approved" not in done["label"]
    rec = rpc_record(fake)
    assert approval_answers(rec) == ["once"]  # one answer, the owner's, and nothing else was said to Hermes:
    said = [f["method"] for f in rec["got"] if "method" in f]
    submit = next(f for f in rec["got"] if f.get("method") == "prompt.submit")["params"]
    if approving.dialect == "attached":
        assert said == ["client.capabilities", "session.create", "prompt.submit", "approval.respond"]
        (respond,) = [f["params"] for f in rec["got"] if f.get("method") == "approval.respond"]
        assert respond == {"session_id": "s1s1s1s1", "execution_generation": 1, "prompt_id": "p1", "choice": "once"}
        assert set(submit) == {"session_id", "text", "submission_id"}
    else:
        assert said == ["client.capabilities", "session.create", "prompt.submit"]
        (answer,) = [f for f in rec["got"] if f.get("id") == "srq-1"]
        assert answer == {"jsonrpc": "2.0", "id": "srq-1", "result": {"choice": "once"}}
        assert set(submit) == {"session_id", "text"}
    assert submit["text"].endswith(RISKY) and "finite" not in submit and "unattended" not in submit
    # the turn was the twin's own Hermes bot, with no looser approval state handed down, and marked as a
    # request's turn (by the variable, and by its chat's number while it ran)
    assert rec["home"] == str(home / "profiles" / "test-scout")
    assert rec["env"] == {} and tasks._marked_chats() == {}
    # (the variable marks only a turn that has a Hermes process of its own; it never reaches a Hermes that
    # other chats share)
    assert rec.get("asked") == ("1" if approving.dialect == "stdio" else None)
    assert calls(fake) == []  # (the single-question mode was not used as well)
    view = approving.tasks()
    assert view["waiting"] == [] and view["log"][0]["label"] == "done here" and view["log"][0]["approval"] is None


def test_a_refused_or_unanswered_step_is_not_done_and_the_answer_says_so(approving, source, home, fake):  # noqa: F811
    from tasks_testlib import approval_answers, rpc_record
    out = _ask(source, RISKY)
    assert out["state"] == "waiting_approval"
    approving.tasks_approve(out["id"], "deny")
    done = json.loads(source.run("ask", "--check", out["id"], "--wait", "30", "--json", check=False).stdout)
    assert done["state"] == "done" and done["reply"].startswith("BLOCKED: Command denied by user.")
    assert done["label"].endswith("; a step was not approved there") and done["label"].startswith("done on Surface")
    assert approval_answers(rpc_record(fake)) == ["deny"]
    assert approving.tasks()["log"][0]["label"] == "done here; a step was not approved"
    # nobody answers: Hermes itself withdraws the question after its own time limit. The plugin never answers
    out = _ask(source, RISKY + " while nobody is there", "--wait", "1")
    done = json.loads(source.run("ask", "--check", out["id"], "--wait", "30", "--json", check=False).stdout)
    assert done["state"] == "done" and "Silence is not consent" in done["reply"]
    assert done["label"].endswith("; a step was not approved there")
    assert approval_answers(rpc_record(fake)) == []
    assert approving.tasks()["waiting"] == []


def test_only_the_asked_computers_owner_can_answer_and_only_once_or_deny(approving, source, home, fake):  # noqa: F811
    from tasks_testlib import approval_answers, rpc_record
    from hermes_plugins.grok_bot_twin.twin.connect import BadInput
    out = _ask(source, RISKY)
    tid = out["id"]
    # no standing permission: "for this session" and "always" are not answers the plugin passes on
    for choice in ("session", "always", "yes", True, None, "ONCE"):
        with pytest.raises(BadInput):
            approving.tasks_approve(tid, choice)
    with pytest.raises(ValueError):
        tasks.decide(tid, "always")
    for bad in ("k-ffffffffffffffff", "../x", 5):
        with pytest.raises((tasks.TaskError, BadInput)):
            approving.tasks_approve(bad, "once")
    # the asking computer cannot approve: not through the mailbox (no such route, and "update" is not its to send) ...
    good = _tok(source.state()) if source.state().get("link") else None
    assert good is None  # (mini shares; it signs nothing as a linked computer)
    mine = _pass(home)
    for path in ("approve", "decide", "answer", "answered"):
        assert task(source.port, path, {"id": tid, "choice": "once", "q": "0a0b0c0d"}, mine)[0] == 404, path
    assert task(source.port, "update", {"id": tid, "state": "running", "choice": "once", "approve": True},
                mine)[0] in (200, 404)
    # ... and not on its own side: there is no open question on mini to answer
    cp = source.run("requests", "approve", tid, check=False)
    assert cp.returncode != 0 and "no longer known" in (cp.stdout + cp.stderr)
    time.sleep(1.2)
    assert approval_answers(rpc_record(fake)) == []  # Hermes is still waiting
    assert approving.tasks()["waiting"][0]["id"] == tid
    # an answer written before its question exists does not count for a later question
    tasks.set_pending(tid, {"command": "rm -rf ./scratch-folder", "description": "recursive delete"})
    assert tasks.decision(tid) is None
    # switching off while a step waits: the question is refused, never left open, never approved
    approving.tasks_switch(False)
    done = json.loads(source.run("ask", "--check", tid, "--wait", "20", "--json", check=False).stdout)
    assert done["state"] in ("cancelled", "refused") and done["error"]["code"] == "switched_off"
    wait_until(lambda: approval_answers(rpc_record(fake)) == ["deny"], what="the open question to be refused")
    # ...and Hermes is told to stop that turn, as its own Stop does: in a Hermes that stays (the attached way) a
    # step that already runs would otherwise run on after the switch is off
    # (a running Hermes wants the run named, and answers "stale" without it)
    stop = {"session_id": "s1s1s1s1", "execution_generation": 1} if approving.dialect == "attached" \
        else {"session_id": "s1s1s1s1"}
    wait_until(lambda: [f["params"] for f in rpc_record(fake)["got"] if f.get("method") == "session.interrupt"]
               == [stop], what="Hermes to be told to stop the turn")
    assert approving.tasks(refresh=False)["waiting"] == []


def test_other_questions_hermes_asks_a_person_are_never_answered(approving, source, home, fake):  # noqa: F811
    from tasks_testlib import rpc_record
    out = _ask(source, "it wants a password", "--wait", "30")
    assert out["state"] == "done" and out["reply"] == "no password was given"
    if approving.dialect == "stdio":
        (frame,) = [f for f in rpc_record(fake)["got"] if f.get("id") == "srq-2"]
        assert "error" in frame and "result" not in frame
    else:  # nothing at all was said back about it
        assert [f["method"] for f in rpc_record(fake)["got"] if "method" in f][3:] == []
    out = _ask(source, "which one do you mean", "--wait", "30")
    assert out["state"] == "done" and out["reply"] == "no choice was made"
    assert approving.tasks()["waiting"] == []


def test_without_hermes_own_interface_the_single_question_mode_is_used(surface, source, home, fake, tmp_path,  # noqa: F811
                                                                      monkeypatch):
    from hermes_plugins.grok_bot_twin.twin import tasks_turn
    assert tasks_turn.transports() == []  # this test process is not Hermes
    dead = tmp_path / "dead.py"
    dead.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    import sys as _sys
    # neither way in starts (each ends before "ready"): nothing was sent, so the single-question mode runs it
    monkeypatch.setattr(tasks_turn, "transports", lambda: [([_sys.executable, str(dead)], "attached"),
                                                          ([_sys.executable, str(dead)], "stdio")])
    surface.tasks_switch(True)
    _listening(source, home)
    out = _ask(source, ASK, "--wait", "40")
    assert out["state"] == "done" and out["reply"] == f"test-scout heard: {ASK}"
    assert len(calls(fake)) == 1  # one turn, through `hermes chat -Q`
