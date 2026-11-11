"""Routines on the owner's other computers: copied to each, switched on on exactly one.

Every "computer" is a real process with its own Hermes folder (tools/fleet.py), talking over real sockets on
loopback. The routines are jobs in a twin's own Hermes file of scheduled jobs (`cron/jobs.json`), written the
way Hermes writes them. Invented twins and routines only.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import fleet_testlib
from fleet_testlib import Machine, free_port, key_problems, next_count, shape_problems, wait_until, wire, wire_link, wire_sync
from twin_testlib import ROOT, config, home, service  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import fleet, routines, routines_jobs as rj

fleet_testlib._SHAPES.update(json.loads((ROOT / "tests" / "contract" / "routines_shapes.json").read_text())["shapes"])

ITEM_KEYS = {"id", "twin", "name", "prompt", "schedule", "skills", "timezone", "on", "version", "updated_at",
             "runs_on", "go", "epoch"}
ANSWER_KEYS = {"v", "gen", "rev", "on", "list", "removed", "can_run"}


def rfx(m: Machine, *words: str) -> None:
    cp = subprocess.run([sys.executable, str(ROOT / "tools" / "routines_fixture.py"), "--home", str(m.home), *words],
                        capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stdout + cp.stderr


def jobs(m: Machine, profile: str = "test-scout") -> list:
    path = m.home / "profiles" / profile / "cron" / "jobs.json"
    if not path.exists():
        return []
    for _ in range(20):  # (replaced atomically; a read can still land between two writes on a slow disk)
        try:
            return json.loads(path.read_text(encoding="utf-8"))["jobs"]
        except (ValueError, OSError):
            time.sleep(0.02)
    raise AssertionError("jobs.json could not be read")


def job(m: Machine, name: str = "Morning trails", profile: str = "test-scout"):
    return next((j for j in jobs(m, profile) if j.get("name") == name), None)


def on(m: Machine, name: str = "Morning trails") -> bool:
    return rj.runnable(job(m, name))


def row(m: Machine, name: str = "Morning trails") -> dict:
    return next(r for r in m.json("routines")["routines"] if r["name"] == name)


def mid(m: Machine) -> str:
    return m.state()["machine"]["id"]


# What the waits below look at is the computers' own files: starting a command for every look would make each
# look cost a second. The pictures the owner sees (`routines --json`, `fleet`) are asserted once things settled.
def rstate(m: Machine) -> dict:
    return json.loads((m.data / "routines.json").read_text(encoding="utf-8"))


def lets_run(mini: Machine) -> int:
    """How many linked computers the sharing one knows to let routines run."""
    return sum(1 for p in rstate(mini)["src"]["peers"].values() if p.get("on") and p.get("can_run"))


def settled(mini: Machine, target: Machine, *linked: Machine, name: str = "Morning trails") -> bool:
    """The routine runs on `target`, and every computer named knows it."""
    tid = mid(target)
    it = next(i for i in rstate(mini)["src"]["items"].values() if i["name"] == name)
    if it["runs_on"] != tid or it["holder"] != tid:
        return False
    for m in linked:
        d = next(i for i in rstate(m)["dst"]["items"].values() if i["name"] == name)
        if d["runs_on"] != tid or d["go"] is not True:
            return False
    return True


def _sharing(base) -> Machine:
    m = Machine(base, "mini", free_port())
    m.fx("add", "Test Scout", "Finds trails.")
    rfx(m, "add", "Test Scout", "Morning trails", "0 7 * * *", "List three trails for today.")
    return m


def _linked(tmp_path, mini: Machine, name: str) -> Machine:
    m = Machine(tmp_path, name, mini.port)
    m.run("link", mini.code())
    m.start()
    return m


# Starting a command costs seconds on a busy machine, so the computers are set up once per group of tests. The
# tests of a group run in the order they stand in, and each first puts back what it needs (`at_home`, `run_here`).
@pytest.fixture(scope="module")
def three(tmp_path_factory):
    """mini shares; Surface and Linux desktop are linked; Routines is switched on on mini."""
    base = tmp_path_factory.mktemp("three")
    mini = _sharing(base)
    mini.share()
    surface = _linked(base, mini, "Surface")
    linux = _linked(base, mini, "Linux desktop")
    mini.run("routines", "on")
    for m in (surface, linux):
        wait_until(lambda m=m: job(m) is not None, what=f"the routine to reach {m.name}")
    yield mini, surface, linux
    for m in (surface, linux, mini):
        m.stop()


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    """A sharing computer of its own for the tests that talk to it directly (as a linked computer would), and
    for the ones where this process is the linked computer."""
    m = _sharing(tmp_path_factory.mktemp("wire"))
    m.share()
    m.run("routines", "on")
    yield m
    m.stop()


def run_here(mini: Machine, *linked: Machine) -> None:
    """"Routines may run on this computer" is on on each of these linked computers, and mini knows."""
    ids = {mid(m) for m in linked}
    known = lambda: {k for k, p in rstate(mini)["src"]["peers"].items() if p.get("on") and p.get("can_run")}  # noqa: E731
    for m in linked:
        if mid(m) not in known():
            m.run("routines", "run-here", "on")
    wait_until(lambda: ids <= known(), what="mini to hear which computers let routines run")


def at_home(mini: Machine, *linked: Machine) -> None:
    """The routine runs on mini (moved back there if an earlier test left it elsewhere)."""
    if not settled(mini, mini, *linked):
        mini.run("routines", "move", "Morning trails", "here")
        wait_until(lambda: settled(mini, mini, *linked) and on(mini), what="the routine to run on mini")


def test_a_routine_copies_to_every_linked_computer_paused_and_runs_on_the_sharing_one(three):
    mini, surface, linux = three
    wait_until(lambda: settled(mini, mini, surface, linux), what="the first list to be applied")
    assert on(mini) and not on(surface) and not on(linux)
    for m in (surface, linux):
        j = job(m)
        assert j["prompt"] == "List three trails for today." and j["schedule"]["expr"] == "0 7 * * *"
        assert j["enabled"] is False and j["state"] == "paused"
        assert j["paused_reason"] == "Grok Bot Twin: runs on mini"
        assert j["id"] != job(mini)["id"]  # the job's own id never travels
        v = m.json("routines")
        assert shape_problems(v, "Routines") == []
        assert v["role"] == "follower" and v["on"] is True and v["can_run"] is False and v["state"] == "in_sync"
        assert [r["label"] for r in v["routines"]] == ["runs on mini"]
        assert v["routines"][0]["twin_name"] == "Test Scout"
    v = mini.json("routines")
    assert shape_problems(v, "Routines") == []
    assert v["role"] == "source" and [r["label"] for r in v["routines"]] == ["runs here"]
    assert v["routines"][0]["can_move_to"] == []  # no linked computer lets routines run yet
    words = mini.run("fleet").stdout
    assert "What syncs:" in words and "Routines: on." in words and "Morning trails (Test Scout, 0 7 * * *): runs here" in words
    assert "Routines may run on this computer: no" in surface.run("fleet").stdout


class Watch:
    """Looks at the same routine's Hermes job on several computers, over and over, and counts how often it was
    switched on on more than one. A look reads every file and then the first ones again: "on in two places"
    counts only when a computer was on both before and after another one was seen on."""

    def __init__(self, *machines: Machine) -> None:
        self.machines, self.twice, self.looks, self.seen_on = machines, [], 0, set()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            first = [on(m) for m in self.machines]
            again = [on(m) for m in self.machines]
            both = [m.name for m, a, b in zip(self.machines, first, again) if a and b]
            self.seen_on.update(both)
            if len(both) > 1:
                self.twice.append(both)
            self.looks += 1
            time.sleep(0.01)

    def __enter__(self) -> "Watch":
        self._t.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._t.join(10)


def test_moving_a_routine_is_one_step_and_it_is_never_switched_on_on_two_computers(three):
    mini, surface, linux = three
    run_here(mini, surface, linux)
    at_home(mini, surface, linux)
    with Watch(mini, surface, linux) as w:
        for target, others in ((surface, (mini, linux)), (linux, (mini, surface)), (surface, (mini, linux)),
                               (mini, (surface, linux))):
            mini.run("routines", "move", "Morning trails", target.name)
            wait_until(lambda t=target: on(t), what=f"the routine to run on {target.name}")
            assert not any(on(o) for o in others)
            wait_until(lambda t=target: settled(mini, t, surface, linux), what="every computer to know where it runs")
        assert row(mini)["label"] == "runs here" and row(surface)["label"] == row(linux)["label"] == "runs on mini"
        # asked for on a linked computer: "run it here" there, decided by the sharing computer all the same
        linux.run("routines", "move", "Morning trails", "here")
        wait_until(lambda: on(linux), what="the routine to run on Linux desktop after its own wish")
        assert not on(mini) and not on(surface)
        wait_until(lambda: settled(mini, linux, surface, linux), what="every computer to know where it runs")
    assert row(mini)["label"] == row(surface)["label"] == "runs on Linux desktop" and row(linux)["label"] == "runs here"
    assert w.looks > 50 and w.twice == []
    assert w.seen_on == {"mini", "Surface", "Linux desktop"}
    assert job(mini)["paused_reason"] == "Grok Bot Twin: runs on Linux desktop"
    assert job(surface)["paused_reason"] == "Grok Bot Twin: runs on Linux desktop"


def test_while_the_computer_that_runs_it_is_off_the_routine_starts_nowhere_else(three):
    mini, surface, linux = three
    run_here(mini, surface, linux)
    mini.run("routines", "move", "Morning trails", "Surface")
    wait_until(lambda: on(surface) and settled(mini, surface, surface, linux), what="the routine to run on Surface")
    surface.stop()  # Surface is closed with the routine switched on in its Hermes
    with Watch(mini, linux) as w:
        mini.run("routines", "move", "Morning trails", "Linux desktop")
        wait_until(lambda: rstate(linux)["dst"]["rev"] == rstate(mini)["src"]["rev"], what="Linux desktop to hear")
        time.sleep(2.0)
        r = row(mini)
        assert r["state"] == "moving" and r["label"] == "moving to Linux desktop: waiting for Surface to stop it"
        assert r["waiting_for"] == "Surface" and r["can_force"] is False
        assert on(surface) and not on(linux) and not on(mini)  # still Surface's, for when it is opened again
        # ... and it cannot be forced away from a computer that is still linked
        cp = mini.run("routines", "move", "Morning trails", "here", "--force", check=False)
        assert cp.returncode != 0 and "has not confirmed" in (cp.stdout + cp.stderr)
        assert not on(mini)
        surface.start()  # Surface is opened again: it stops the routine, says so, and only then Linux starts it
        wait_until(lambda: on(linux), what="the routine to run on Linux desktop")
        assert not on(surface) and not on(mini)
    assert w.twice == []


def test_switching_the_item_off_takes_the_copies_away_and_brings_every_routine_home(three):
    mini, surface, linux = three
    run_here(mini, surface)
    mini.run("routines", "move", "Morning trails", "Surface")
    wait_until(lambda: on(surface), what="the routine to run on Surface")
    with Watch(mini, surface, linux) as w:
        mini.run("routines", "off")
        wait_until(lambda: job(surface) is None and job(linux) is None, what="the copies to leave")
        wait_until(lambda: on(mini), what="the routine to run on mini again")
    assert w.twice == []
    v = surface.json("routines")
    assert v["routines"] == [] and v["state"] == "off_there" and v["restore"] == []
    assert "Routines: off." in mini.run("fleet").stdout
    # a linked computer's own switch: no copies here, the others keep theirs
    mini.run("routines", "on")
    wait_until(lambda: job(surface) is not None and job(linux) is not None, what="the copies to come back")
    linux.run("routines", "off")
    wait_until(lambda: job(linux) is None, what="the copy to leave Linux desktop")
    assert job(surface) is not None and on(mini)
    linux.run("routines", "on")
    wait_until(lambda: job(linux) is not None, what="the copy to come back to Linux desktop")


def test_a_computer_that_unlinks_itself_stops_its_routines_first_and_they_come_home(three):
    mini, surface, _linux = three
    run_here(mini, surface)
    mini.run("routines", "move", "Morning trails", "Surface")
    wait_until(lambda: on(surface), what="the routine to run on Surface")
    assert not on(mini)
    surface.run("unlink", "--yes")
    left = list(surface.home.rglob("jobs.json"))  # (the bot was moved to the kept place, its jobs with it)
    assert left and not (surface.home / "profiles" / "test-scout").exists()
    for path in left:
        assert not any(rj.runnable(j) for j in json.loads(path.read_text())["jobs"])
    wait_until(lambda: on(mini), what="the routine to run on mini again")
    assert row(mini)["label"] == "runs here"


def test_a_change_and_a_removal_follow_and_the_copy_as_it_was_can_be_put_back(three):
    mini, _surface, surface = three  # (the linked computer looked at here is Linux desktop)
    wait_until(lambda: job(surface) is not None, what="the copy on Linux desktop")
    rfx(mini, "edit", "Test Scout", "Morning trails", "List four trails for today.")
    mini.run("routines", "tend")
    wait_until(lambda: job(surface)["prompt"] == "List four trails for today.", what="the new prompt on Surface")
    assert not on(surface)
    v = surface.json("routines")
    assert [r["what"] for r in v["restore"]] == ["updated"]
    assert v["restore"][0]["label"] == "Morning trails (Test Scout), as it was before it changed"
    surface.run("routines", "restore", v["restore"][0]["id"])
    back = [j for j in jobs(surface) if j["prompt"] == "List three trails for today."]
    assert len(back) == 1 and back[0]["enabled"] is False and "Put back" in back[0]["paused_reason"]
    assert surface.json("routines")["restore"] == []
    rfx(mini, "remove", "Test Scout", "Morning trails")
    mini.run("routines", "tend")
    wait_until(lambda: job(surface, "Morning trails") is None or
               all(j["prompt"] != "List four trails for today." for j in jobs(surface)), what="the removal on Surface")
    assert [j["prompt"] for j in jobs(surface)] == ["List three trails for today."]  # the one put back is the owner's
    assert [r["what"] for r in surface.json("routines")["restore"]] == ["removed"]


def _key(have=None, gen=None, **over) -> dict:
    return {"v": 1, "gen": gen, "have": have, "on": True, "can_run": False, "applied": have, "running": [],
            "paused": [], **over}


def _sync(port: int, token: str, routines_key=None, have=None) -> tuple:
    body = {"proto": fleet.PROTO, "have": have, "wait_s": 0, "machine": {"name": "Probe", "platform": "linux"},
            "applied": {}, "n": next_count()}
    if routines_key is not None:
        body["routines"] = routines_key
    got: dict = {}
    status, obj, raw = wire("POST", port, "/v1/sync", body, token=token, answer=got)
    assert status == 200 and got["signed"]
    return obj, raw


def test_a_computer_on_an_older_build_is_answered_exactly_as_before(mini):
    code = mini.code()
    status, hello, _ = wire("GET", mini.port, "/v1/hello")
    assert status == 200 and set(hello) == {"schema", "app", "proto", "sharing"} and hello["proto"] == 3
    _s, linked, _r = wire_link(mini.port, code, mid="m-aaaaaaaaaa01")
    token = linked["pass"]
    lst, _raw = _sync(mini.port, token)  # an older build: no "routines" key in what it sends
    assert lst["changed"] is True and key_problems(lst) == [] and lst["proto"] == 3
    same, raw = _sync(mini.port, token, have=lst["rev"])
    assert same["changed"] is False and "routines" not in same and b"trails for today" not in raw
    assert set(same) <= {"schema", "proto", "changed", "sharing", "rev", "takes_requests"}
    # the twin list of a computer that does speak routines is the same list: nothing new rides in it
    lst2, _raw = _sync(mini.port, token, routines_key=_key())
    assert lst2["changed"] is True and key_problems(lst2) == []


def test_what_leaves_is_the_named_fields_of_prompt_routines_and_nothing_else(mini):
    rfx(mini, "add", "Test Scout", "Nightly script", "0 2 * * *", "Run the nightly script.", "--script", "nightly.py")
    rfx(mini, "add", "Test Scout", "Hourly check", "every 60m",
        "Check the trail report.\napi_key = sk-test-0123456789abcdef0123456789abcdef\nThen say what changed.")
    path = mini.home / "profiles" / "test-scout" / "cron" / "jobs.json"
    doc = json.loads(path.read_text())
    for j in doc["jobs"]:
        j.update(model="secret-model-name", provider="secret-provider", base_url="http://secret-host.invalid/v1",
                 deliver="telegram:secret-chat-7788", origin={"platform": "telegram", "chat_id": "secret-chat-7788"},
                 last_error="secret-last-error", last_status="ok", enabled_toolsets=["secret-toolset"])
    doc["jobs"].append({**doc["jobs"][0], "id": "aaaaaaaaaaaa", "name": "One-off", "schedule":
                        {"kind": "once", "run_at": "2000-01-01T09:00:00+00:00", "display": "once"}})
    doc["jobs"].append({**doc["jobs"][0], "id": "bbbbbbbbbbbb", "name": "In a folder", "workdir": "/secret/folder"})
    path.write_text(json.dumps(doc))
    mini.run("routines", "tend")
    _s, linked, _r = wire_link(mini.port, mini.code(), mid="m-aaaaaaaaaa02")
    token = linked["pass"]
    lst, _raw = _sync(mini.port, token)
    obj, raw = _sync(mini.port, token, routines_key=_key(), have=lst["rev"])
    rt = obj["routines"]
    assert set(rt) == ANSWER_KEYS and rt["on"] is True
    assert sorted(i["name"] for i in rt["list"]) == ["Hourly check", "Morning trails"]
    for item in rt["list"]:
        assert set(item) == ITEM_KEYS and set(item["schedule"]) <= {"kind", "expr", "minutes", "display"}
        assert item["runs_on"] == mid(mini) and item["go"] is True
        assert item["id"].startswith("j-") and all(j["id"] not in raw.decode() for j in doc["jobs"])
    text = raw.decode("utf-8")
    for secret in ("secret-", "sk-test-", "nightly", "telegram", "/secret/folder", "One-off", "In a folder"):
        assert secret not in text
    hourly = next(i for i in rt["list"] if i["name"] == "Hourly check")
    assert "Check the trail report." in hourly["prompt"] and "Then say what changed." in hourly["prompt"]
    assert mini.json("routines")["local_only"] == 3
    # once it holds that list, the next answer is the small one
    small, _raw = _sync(mini.port, token, routines_key=_key(rt["rev"], rt["gen"]), have=lst["rev"])
    assert set(small["routines"]) == {"v", "gen", "rev", "on"}
    # an unsigned or unknown computer gets nothing of it
    status, obj, raw = wire("POST", mini.port, "/v1/sync", {"proto": 3, "have": lst["rev"], "wait_s": 0,
                                                            "routines": _key(), "n": next_count()},
                            token="m-000000000000|" + "0" * 64)
    assert status == 401 and b"trails" not in raw


def test_a_linked_computer_cannot_report_its_way_into_running_a_routine(mini):
    """What a linked computer says about itself moves nothing to it: only the owner's move does, and only to a
    computer that lets routines run."""
    _s, linked, _r = wire_link(mini.port, mini.code(), mid="m-bbbbbbbbbbbb", name="Prober")
    token = linked["pass"]
    lst, _ = _sync(mini.port, token)
    obj, _ = _sync(mini.port, token, routines_key=_key(), have=lst["rev"])
    rid = next(i for i in obj["routines"]["list"] if i["name"] == "Morning trails")["id"]
    # it claims to run the routine, and asks for it without letting routines run
    obj, _ = _sync(mini.port, token, have=lst["rev"],
                   routines_key=_key(obj["routines"]["rev"], obj["routines"]["gen"], running=[rid],
                                     move={"id": rid, "to": "m-bbbbbbbbbbbb"}))
    obj, _ = _sync(mini.port, token, routines_key=_key(), have=lst["rev"])
    item = next(i for i in obj["routines"]["list"] if i["name"] == "Morning trails")
    assert item["runs_on"] == mid(mini) and item["go"] is True and on(mini)
    cp = mini.run("routines", "move", "Morning trails", "m-bbbbbbbbbbbb", check=False)
    assert cp.returncode != 0 and "does not let routines run" in (cp.stdout + cp.stderr)
    assert on(mini)


def test_a_quick_move_away_and_back_does_not_leave_the_routine_paused_everywhere(mini):
    """The computer that runs a routine pauses it for a move, and the owner moves it back before that computer
    has said so. Its report "not running" is the plugin's own pause, not the owner's: the routine stays "on"
    and that computer switches it on again."""
    _s, linked, _r = wire_link(mini.port, mini.code(), mid="m-dddddddddddd", name="Den")
    token = linked["pass"]
    have = [None]

    def ask(**kw) -> dict:
        for _ in range(3):  # (the twin list first, whenever it changed; routines ride in "nothing changed")
            body = {"proto": fleet.PROTO, "have": have[0], "wait_s": 0, "machine": {"name": "Den", "platform": "linux"},
                    "applied": {}, "n": next_count(), "routines": _key(can_run=True, **kw)}
            got: dict = {}
            status, obj, _raw = wire("POST", mini.port, "/v1/sync", body, token=token, answer=got)
            assert status == 200 and got["signed"]
            if obj["changed"] is not True:
                return obj["routines"]
            assert "routines" not in obj
            have[0] = obj["rev"]
        raise AssertionError("the twin list kept changing")
    rt = ask()
    rid = next(i for i in rt["list"] if i["name"] == "Morning trails")["id"]
    item = lambda rt: next(i for i in rt["list"] if i["id"] == rid)  # noqa: E731
    mini.run("routines", "move", "Morning trails", "m-dddddddddddd")
    rt = ask()
    assert item(rt)["runs_on"] == "m-dddddddddddd" and item(rt)["go"] is True and not on(mini)
    ask(have=rt["rev"], gen=rt["gen"], applied=rt["rev"], running=[rid])  # it runs there now
    mini.run("routines", "move", "Morning trails", "here")
    away = ask()
    assert item(away)["runs_on"] == mid(mini) and item(away)["go"] is False and not on(mini)
    mini.run("routines", "move", "Morning trails", "m-dddddddddddd")  # ... and back, before Den said it paused
    back = ask(applied=away["rev"], running=[])  # Den's late word: "I applied the move away; it does not run here"
    assert item(back)["runs_on"] == "m-dddddddddddd" and item(back)["go"] is True and item(back)["on"] is True
    assert not on(mini)
    # paused there by its OWNER is another thing, and is said as such
    ask(have=back["rev"], gen=back["gen"], applied=back["rev"], running=[], paused=[rid])
    rt = ask()
    assert item(rt)["on"] is False and row(mini)["label"] == "paused on Den"
    # home again for the tests that follow: Den says it stopped, and mini runs it
    ask(applied=rt["rev"], running=[rid])
    mini.run("routines", "move", "Morning trails", "here")
    away = ask()
    ask(have=away["rev"], gen=away["gen"], applied=away["rev"], running=[])
    wait_until(lambda: on(mini), what="the routine to run on mini again")


# ---- the linked computer's own rules, in this process ----------------------------------------------------------------
@pytest.fixture
def linked_here(home, mini, tmp_path, monkeypatch):  # noqa: F811
    """This process is "Surface", linked to mini, holding Test Scout."""
    from hermes_plugins.grok_bot_twin.twin import fleet_follow as follow
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "share_port": mini.port,
                         "machine_name": "Surface"})
    monkeypatch.setattr(follow, "MIN_BACKOFF_S", 0.3)
    monkeypatch.setattr(follow, "MAX_BACKOFF_S", 1.0)
    svc = service.get_service()
    svc.fleet_link(mini.code())
    return svc


@pytest.fixture
def alone_here(linked_here):
    """The same, with this computer's own asking stopped: the test hands it the answers itself."""
    from hermes_plugins.grok_bot_twin.twin import fleet_follow as follow
    follow.stop()
    wait_until(lambda: not follow.running() and not follow._state.get("holding"), what="the follower to stop")
    return linked_here


RID = "j-00000000000000aa"


def _here_jobs(home) -> list:  # noqa: F811
    return rj.load(home / "profiles" / "test-scout") or []


def _fed(home) -> list:  # noqa: F811
    """The copies of the routines a test fed in itself (the sharing computer of this group has routines of its
    own, which arrived before this computer's asking was stopped; they are not listed again and stay paused)."""
    return [j for j in _here_jobs(home) if str(j.get(rj.COPY_KEY, "")).startswith("j-00000000000000")]


def _fed_row(v: dict) -> dict:
    return next(r for r in v["routines"] if r["id"] == RID)


def _of(jobs: list) -> dict:
    return next(j for j in jobs if j.get(rj.COPY_KEY) == RID)


def _answer(me: str, tid: str, **over) -> dict:
    item = {"id": "j-00000000000000aa", "twin": tid, "name": "Morning trails", "prompt": "List three trails.",
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "display": "0 7 * * *"}, "skills": [], "timezone": None,
            "on": True, "version": 1, "updated_at": "2000-01-01T09:00:00Z", "runs_on": me, "go": True, "epoch": 1}
    item.update(over)
    return {"changed": False, "sharing": True,
            "routines": {"v": 1, "rev": f"{over.get('epoch', 1)}-0000000a", "on": True, "list": [item], "removed": [],
                         "can_run": {me: True}}}


def test_a_linked_computer_runs_a_routine_only_with_its_own_switch_on(alone_here, home):  # noqa: F811
    linked_here = alone_here
    st = fleet.load()
    me, tid = st["machine"]["id"], next(iter(st["link"]["twins"]))
    # even a signed list that says "it runs on you, go" switches nothing on while the owner's switch here is off
    routines.heard_answer(_answer(me, tid))
    j = _fed(home)[0]
    assert not rj.runnable(j) and j["paused_reason"] == 'Grok Bot Twin: waiting for "Routines may run on this computer"'
    assert routines.request_key(fleet.load())["running"] == []
    # the switch alone starts nothing either: it makes this computer ask again, and the list that arrives does
    linked_here.routines_switch(can_run=True)
    assert not rj.runnable(_fed(home)[0]) and routines.load()["dst"]["rev"] is None
    routines.heard_answer(_answer(me, tid))
    assert rj.runnable(_fed(home)[0]) and _fed_row(linked_here.routines())["label"] == "runs here"
    assert routines.request_key(fleet.load())["running"] == ["j-00000000000000aa"]
    # the owner pauses it in Hermes on this computer: it stays paused, and is reported as not running
    rj.edit(home / "profiles" / "test-scout", lambda jobs: (rj.set_paused(_of(jobs), "") or True, None))
    routines.tend_linked()
    key = routines.request_key(fleet.load())
    assert not rj.runnable(_fed(home)[0]) and key["running"] == [] and key["paused"] == ["j-00000000000000aa"]
    assert _fed_row(linked_here.routines())["label"] == "paused here"
    # the list moves it away while someone resumed the copy here by hand: paused again at the next look
    routines.heard_answer(_answer(me, tid, runs_on="m-cccccccccccc", epoch=2))
    rj.edit(home / "profiles" / "test-scout", lambda jobs: (rj.set_running(_of(jobs)), None))
    assert rj.runnable(_fed(home)[0])
    routines.tend_linked()
    assert not rj.runnable(_fed(home)[0]) and routines.request_key(fleet.load())["paused"] == []
    # an older list that arrives late (it still says "runs here") switches nothing on again
    routines.heard_answer(_answer(me, tid, epoch=1))
    assert not rj.runnable(_fed(home)[0]) and routines.load()["dst"]["rev"] == "2-0000000a"
    # switching the switch off pauses whatever runs here at once
    routines.heard_answer(_answer(me, tid, epoch=3))
    assert rj.runnable(_fed(home)[0])
    linked_here.routines_switch(can_run=False)
    assert not rj.runnable(_fed(home)[0])
    # the plugin's own notes are lost while a copy is switched on here: the copy is still known, by the mark in
    # its own record. It is reported as running, and the next list pauses that very copy (no second one is made)
    linked_here.routines_switch(can_run=True)
    routines.heard_answer(_answer(me, tid, epoch=4))
    assert rj.runnable(_fed(home)[0])
    # (a copy someone turned into a script job here is a prompt on a schedule again at the next look)
    rj.edit(home / "profiles" / "test-scout", lambda jobs: (_of(jobs).update(script="x.py", workdir="/tmp") or True, None))
    routines.tend_linked()
    assert _fed(home)[0]["script"] is None and _fed(home)[0]["workdir"] is None
    routines.state_path().write_text("{}", encoding="utf-8")
    assert routines.request_key(fleet.load())["running"] == ["j-00000000000000aa"]
    routines.heard_answer(_answer(me, tid, runs_on="m-cccccccccccc", epoch=5))
    assert [rj.runnable(j) for j in _fed(home)] == [False]
    assert not any(rj.runnable(j) for j in _here_jobs(home))
    assert routines.request_key(fleet.load())["running"] == []
    # unlinking pauses every copy and switches "routines may run on this computer" off again
    linked_here.routines_switch(can_run=True)
    assert routines.load()["can_run"] is True
    linked_here.fleet_unlink(False)
    assert routines.load()["can_run"] is False and routines.load()["dst"] is None


def test_what_arrives_is_checked_again_and_a_missing_routine_is_never_switched_on(alone_here, home):  # noqa: F811
    linked_here = alone_here
    st = fleet.load()
    me, tid = st["machine"]["id"], next(iter(st["link"]["twins"]))
    linked_here.routines_switch(can_run=True)
    ans = _answer(me, tid, prompt="Line one.\x00\x07\npassword = hunter2hunter2hunter2\nLine three.")
    ans["routines"]["list"] += [
        {**ans["routines"]["list"][0], "id": "../../x"},                                  # not an id
        {**ans["routines"]["list"][0], "id": "j-00000000000000bb", "twin": "t-ffffffffffffffff"},  # not a twin here
        {**ans["routines"]["list"][0], "id": "j-00000000000000cc",
         "schedule": {"kind": "once", "run_at": "2000-01-01T09:00:00Z"}},                 # not a repeating schedule
        {**ans["routines"]["list"][0], "id": "j-00000000000000dd", "script": "x.py", "workdir": "/", "model": "m",
         "name": "Extra keys"},
    ]
    routines.heard_answer(ans)
    got = _fed(home)
    assert sorted(j["name"] for j in got) == ["Extra keys", "Morning trails"]
    for j in got:
        assert j["script"] is None and j["workdir"] is None and j["model"] is None and j["deliver"] == "local"
        assert "\x00" not in j["prompt"] and "hunter2" not in j["prompt"]
    # the next list lacks one routine without saying it was removed: it is kept, paused, and never switched on
    ans2 = _answer(me, tid, epoch=1, prompt=ans["routines"]["list"][0]["prompt"])
    ans2["routines"]["rev"] = "2-0000000b"
    routines.heard_answer(ans2)
    extra = next(j for j in _fed(home) if j["name"] == "Extra keys")
    assert not rj.runnable(extra)
    assert rj.runnable(next(j for j in _fed(home) if j["name"] == "Morning trails"))
    # only a list that says "removed" removes it, and what was there is kept to put back
    ans3 = _answer(me, tid, epoch=1, prompt=ans["routines"]["list"][0]["prompt"])
    ans3["routines"].update(rev="3-0000000c", removed=["j-00000000000000dd"])
    routines.heard_answer(ans3)
    assert [j["name"] for j in _fed(home)] == ["Morning trails"]
    assert [r["what"] for r in linked_here.routines()["restore"]] == ["removed"]


def test_an_older_sharing_computer_is_said_in_plain_words_and_nothing_breaks(alone_here, home):  # noqa: F811
    linked_here = alone_here
    routines.heard_answer({"changed": False, "sharing": True, "rev": "1-00000000"})  # signed, without the key
    v = linked_here.routines()
    assert shape_problems(v, "Routines") == []
    assert v["supported"] is False and v["problem"]["code"] == "older_source"
    assert v["problem"]["message"] == "mini has an older Grok Bot Twin, so routines do not copy yet."
    assert linked_here.fleet()["source"]["state"] == "in_sync"  # the twins follow as before


def test_removing_the_plugin_on_a_linked_computer_pauses_what_ran_there_before_it_says_goodbye(linked_here, home, mini):  # noqa: F811
    me = fleet.load()["machine"]["id"]
    mine = lambda: next((j for j in _here_jobs(home) if j["name"] == "Morning trails"), None)  # noqa: E731
    wait_until(mine, what="the copy to arrive here")
    linked_here.routines_switch(can_run=True)
    wait_until(lambda: (rstate(mini)["src"]["peers"].get(me) or {}).get("can_run"), what="mini to hear it")
    mini.run("routines", "move", "Morning trails", me)
    wait_until(lambda: rj.runnable(mine()), what="the routine to run here")
    assert not on(mini)
    linked_here.fleet_shutdown_for_uninstall()  # ("Remove Grok Bot Twin": the bots stay as ordinary Hermes bots)
    assert not rj.runnable(mine()) and mine()["paused_reason"] == "Grok Bot Twin: this computer was unlinked"
    wait_until(lambda: on(mini), what="the routine to run on mini again")


# ---- Hermes' own file of jobs --------------------------------------------------------------------------------------------
def test_the_jobs_file_is_changed_only_under_hermes_own_lock_and_keeps_what_it_does_not_know(home, monkeypatch):  # noqa: F811
    prof = home / "profiles" / "test-scout"
    (prof / "cron").mkdir(parents=True)
    spec = {"name": "Morning trails", "prompt": "List three trails.", "skills": [],
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "display": "0 7 * * *"}}
    mine = rj.new_record(spec, "Grok Bot Twin: runs on mini")
    theirs = {"id": "feedfeedfeed", "name": "The owner's own", "prompt": "x", "enabled": True, "state": "scheduled",
              "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"}, "future_key": {"kept": True}}
    (prof / "cron" / "jobs.json").write_text(json.dumps({"jobs": [theirs, mine], "format": 7}))
    rj.edit(prof, lambda jobs: (rj.set_running(rj.find(jobs, mine["id"])), None))
    doc = json.loads((prof / "cron" / "jobs.json").read_text())
    assert doc["format"] == 7 and doc["jobs"][0] == theirs  # untouched, unknown keys included
    assert rj.runnable(doc["jobs"][1]) and doc["jobs"][1]["paused_reason"] is None and doc["jobs"][1]["paused_at"] is None
    # while Hermes (or anything else) holds its lock, nothing is written and the caller is told
    import fcntl
    monkeypatch.setattr(rj, "LOCK_WAIT_S", 0.3)
    with open(prof / "cron" / ".jobs.lock", "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(OSError):
            rj.edit(prof, lambda jobs: (rj.set_paused(rj.find(jobs, mine["id"]), "x"), None))
    assert rj.runnable(rj.find(rj.load(prof), mine["id"]))
    # a file that cannot be understood is never written over, and counts as "could be running"
    (prof / "cron" / "jobs.json").write_text("{not json")
    assert rj.load(prof) is None
    with pytest.raises(OSError):
        rj.edit(prof, lambda jobs: (True, None))
    assert (prof / "cron" / "jobs.json").read_text() == "{not json"


# ---- the page's routes ---------------------------------------------------------------------------------------------------
def test_the_routes_of_the_page_answer_in_the_shapes_and_say_no_in_plain_words(home, monkeypatch):  # noqa: F811
    pytest.importorskip("fastapi")
    import importlib.util
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from twin_testlib import timer
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_routines", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    app = FastAPI()
    P = "/api/plugins/grok-bot-twin"
    app.include_router(api.router, prefix=P)
    c = TestClient(app)
    v = c.get(P + "/fleet/routines").json()
    assert shape_problems(v, "Routines") == [] and v["role"] == "none" and v["on"] is False and v["routines"] == []
    assert c.post(P + "/fleet/routines", json={"on": True}).json()["error"]["code"] == "not_linked"
    assert c.post(P + "/fleet/routines", json={}).status_code == 400
    assert c.post(P + "/fleet/routines", json={"on": "yes"}).status_code == 400
    r = c.post(P + "/fleet/routines/j-0000000000000000/move", json={"to": "m-000000000000"}).json()
    assert r["error"]["code"] == "not_linked"
    assert c.post(P + "/fleet/routines/not-an-id/move", json={"to": "m-000000000000"}).json()["error"]["code"] == "unknown_routine"
    assert c.post(P + "/fleet/routines/j-0000000000000000/move", json={}).status_code == 400
    assert c.post(P + "/fleet/routines/restore/u-000000000000").json()["error"]["code"] == "unknown_restore"
    assert c.post(P + "/fleet/routines/restore/nope").json()["error"]["code"] == "unknown_restore"
