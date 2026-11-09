"""`hermes twin ...` argparse wiring and `/twin`."""
from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from twin_testlib import cli, home, slash, svc  # noqa: F401


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_cli_demo_and_status(svc):
    rc, out, _ = run(["status"])
    assert rc == 0 and "No twins yet" in out
    # without botport the hint never suggests `twin add --all` (it would fail): demo, or setup first
    assert "add --all" not in out and "hermes twin setup, then hermes twin add" in out
    rc, out, _ = run(["demo"])
    assert rc == 0 and "Demo twin of Acme Helper is ready" in out and "In step" in out
    rc, out, _ = run(["demo", "grok-edit"])
    rc, out, _ = run(["sync"])
    assert rc == 0 and "updated description" in out
    rc, out, _ = run(["demo", "secret"])
    rc, out, _ = run(["sync"])
    assert rc == 1 and "1 suspected secret held back (a check, not a guarantee)" in out


def test_cli_usage_errors(svc):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["conflicts", "../etc"])
    rc, _, err = run(["conflicts", "nobody"])
    assert rc == 1 and "No twin called nobody" in err
    assert run([])[0] == 2


def test_cli_remove_and_digest(svc):
    run(["demo"])
    rc, out, _ = run(["remove", "acme-helper", "--dry-run"])
    assert rc == 0 and "profile stays" in out
    rc, out, _ = run(["digest", "acme-helper"])
    assert "off" in out


def test_slash(svc):
    assert "No twins yet" in slash.handle("")
    assert "Demo twin" in slash.handle("demo")
    assert "Acme Helper" in slash.handle("status")
    assert "in step" in slash.handle("sync") or "updated" in slash.handle("sync")
    assert "Twin ids look like" in slash.handle("conflicts ../x")
    assert slash.handle("unknown") == slash.HELP
    assert slash.handle('"unbalanced') == slash.HELP


def test_cli_defaults_to_the_only_twin_and_plurals(svc):
    rc, out, _ = run(["conflicts"])
    assert rc == 0 and "No twins yet" in out
    run(["demo"])
    rc, out, _ = run(["demo", "conflict"])
    assert "Needs you" not in out and "In step" not in out  # no state printed before it is computed
    assert "Run `hermes twin sync` or wait for the background check (every 60 s)." in out
    rc, out, _ = run(["sync"])
    assert "1 conflict:" in out and "conflict(s)" not in out
    rc, out, _ = run(["conflicts"])
    assert rc == 1 and out.startswith("1 conflict:") and "skill:greet-visitors" in out
    rc, out, _ = run(["resolve", "skill:greet-visitors", "--keep", "grok"])
    assert rc == 0 and "Kept Grok" in out and "skill:greet-visitors@" in out
    rc, out, _ = run(["restore-points"])
    point = out.split()[0]
    assert point.startswith("skill:greet-visitors@")
    rc, out, _ = run(["restore", point])
    assert rc == 0 and out.startswith("Restored skill:greet-visitors.")
    rc, out, _ = run(["restore", "acme-helper", point])
    assert rc == 0 and "Nothing to restore" in out
    for cmd in (["recent"], ["parity"], ["checklist"], ["digest"]):
        assert "say which one" not in "".join(run(cmd)[1:]), cmd
    rc, out, _ = run(["recent"])
    assert "on 2000-" not in out and "min ago" in out  # fixture activity shown relative to the demo start
    rc, out, _ = run(["mirror"])  # demo: no pair folder needed
    assert rc == 0 and "Connected" in out
    # with two twins, a command without a twin id asks which one
    run(["add", "--bot", "Recipe Planner", "--yes"])
    rc, _, err = run(["conflicts"])
    assert rc == 2 and "You have 2 twins; say which one" in err


def test_cli_demo_messages_doctor_status_line(svc):
    run(["demo"])
    for action in ("secret", "grok-edit", "hermes-edit"):
        rc, out, _ = run(["demo", action])
        assert rc == 0 and "Run `hermes twin sync` or wait for the background check" in out
        assert "acme-helper (demo)" not in out
    rc, out, _ = run(["status"])
    assert "botport: not set up (only needed for real twins)" in out.splitlines()[0]
    rc, out, _ = run(["doctor"])
    assert "Background check starts with the first chat or Twins page visit" in out
    assert "not running yet" not in out
    assert "/twin sync" in slash.handle("demo grok-edit")


def test_cli_restore_with_nothing_kept(svc):
    run(["demo"])
    rc, out, _ = run(["restore-points"])
    assert "No restore points yet." in out
    rc, _, err = run(["restore", "skill:nope@000000000000"])
    assert rc == 2 and "Unknown restore point" in err
