"""R17: the sample never sends a new user to a dead end. A first-time user's walk through the sample, on a computer without the
Grok app, is replayed command by command: every next step it prints is one the user can do now (a sample command,
or the real bots' way in), `finish --all` with only sample twins says so, and the tour ends with nothing left."""
from __future__ import annotations

import io
import re
from contextlib import redirect_stderr, redirect_stdout

from twin_testlib import cli, home, svc  # noqa: F401

# what a user with only the sample can't do: the Grok app is not here, and "no twins" sits next to listed twins
DEAD_ENDS = ("Grok app", "No twins yet")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue() + err.getvalue()


def dead_ends(text: str) -> list:
    return [ln for ln in text.splitlines() if any(d in ln for d in DEAD_ENDS)]


# a first-time user's sample tour, in order
TOUR = [["demo"], ["demo", "grok-edit"], ["check"], ["demo", "secret"], ["check"], ["demo", "conflict"], ["check"],
        ["conflicts"], ["resolve", "skill:greet-visitors", "--keep", "grok"], ["versions"], ["restore", "1"],
        ["status"], ["add", "--bot", "Recipe Planner", "--yes"], ["status"], ["finish", "--all"]]


def test_R17_the_sample_tour_has_no_dead_end(svc):
    found = []
    for step in TOUR:
        found += dead_ends(run(step)[1])
    assert found == []


def test_R17_a_held_password_in_the_sample_names_a_sample_command(svc):
    run(["demo"])
    run(["demo", "secret"])
    run(["check"])
    rc, out = run(["status"])
    nxt = out.splitlines()[-1]
    assert rc == 1 and nxt.startswith("Next (Acme Support): ") and "Grok app" not in nxt
    assert "hermes twin demo grok-edit" in nxt and "hermes twin demo remove" in nxt
    # and doing what it says clears it
    assert run(["demo", "grok-edit"])[0] == 0
    run(["check"])
    rc, out = run(["status"])
    assert rc == 0 and "nothing needs you" in out and "password" not in out


def test_R17_finish_all_with_only_the_sample_says_what_is_true(svc):
    run(["demo"])
    run(["add", "--bot", "Recipe Planner", "--yes"])
    rc, out = run(["finish", "--all"])
    assert rc == 0 and "No twins yet" not in out
    lines = out.splitlines()
    assert lines[0] == " Acme Support, Recipe Planner: the sample, with invented data; nothing to finish."
    assert lines[1].startswith("Next: to twin your real Grok bots, open the Twins page in Hermes Desktop")
    rc, out = run(["finish", "Recipe Planner"])  # named: one line for the sample, not "nothing to do" twice
    assert rc == 0 and out.splitlines()[0] == " Recipe Planner: the sample, with invented data; nothing to finish."
    assert "Nothing to do" not in out


def test_R17_the_tour_ends_with_nothing_left(svc):
    """Following each `Next` from where that tour stopped ("needs your attention", rc=1) reaches done: the
    held fake password, the skill put back (approve), then the send (`approve` alone left it "to send to Grok")."""
    for step in TOUR:
        run(step)
    for _ in range(6):
        rc, out = run(["status"])
        nxt = out.splitlines()[-1]
        if not nxt.startswith("Next (Acme Support): "):  # nothing left on the sample itself
            break
        cmds = re.findall(r"hermes twin (demo grok-edit|send-to-grok[^.]*|approve [^)]*?\"skill:[\w-]+\")", nxt)
        assert cmds, f"no command a user can run now in: {nxt}"
        argv = [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', cmds[0])]
        assert run(argv)[0] == 0
        run(["check"])
    assert rc == 0 and "nothing needs you" in out.splitlines()[0]
    assert out.splitlines()[-1].startswith("Next: to twin your real Grok bots")
