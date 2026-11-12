"""`hermes twin ...` argparse wiring and `/twin`."""
from __future__ import annotations

import io
import json
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
    # no helper yet: the one next step is the Twins page (or setup), never `twin add --all` (it would fail)
    assert "add --all" not in out
    assert out.splitlines()[0] == ("Grok Bot Twin: not connected to Grok yet. Next: open the Twins page in Hermes "
                                   "Desktop, or run hermes twin setup.")
    rc, out, _ = run(["demo"])
    assert rc == 0 and out.startswith("Sample twin ready.") and "Acme Support (sample): Follows Grok" in out
    rc, out, _ = run(["demo", "grok-edit"])
    rc, out, _ = run(["sync"])
    assert rc == 0 and "Acme Support: updated description" in out
    rc, out, _ = run(["demo", "secret"])
    rc, out, _ = run(["sync"])
    assert rc == 1
    assert "1 thing that looks like a password or key was not copied (this check can miss things)" in out


def test_cli_usage_errors(svc):
    rc, out, err = run(["conflicts", "../etc"])  # not a twin's name: said plainly, and nothing is looked up
    assert rc == 2 and out == "" and err.splitlines() == ['✗ No twin is called "../etc".', "  You have no twins yet."]
    rc, _, err = run(["conflicts", "nobody"])
    assert rc == 1 and "No twin called nobody" in err
    # plain `hermes twin` is the status plus one next step, never a usage error
    rc, out, _ = run([])
    assert rc == 0 and "Next:" in out and "Usage" not in out


def test_cli_remove_and_digest(svc):
    run(["demo"])
    rc, out, _ = run(["remove", "acme-support", "--dry-run"])
    assert rc == 0 and "End the sample? Nothing of yours changes." in out and "Nothing changed" in out
    assert "acme-support" in svc.pair_ids()
    rc, out, _ = run(["digest", "acme-support"])
    assert "off" in out


def test_slash(svc):
    assert "No twins yet" in slash.handle("", platform="cli")
    assert "Sample twin ready" in slash.handle("demo", platform="cli")
    assert "Acme Support [sample]" in slash.handle("status", platform="cli")
    assert "up to date" in slash.handle("check", platform="cli") or "updated" in slash.handle("check", platform="cli")
    assert slash.handle("sync", platform="cli") == slash.handle("check", platform="cli")  # the older name still works
    assert "/twin check [TWIN]" in slash.HELP and "/twin sync" not in slash.HELP
    assert slash.handle("conflicts ../x", platform="cli") == 'Grok Bot Twin: No twin is called "../x". Your twins: "Acme Support"'
    # the twin is named as the page names it, quoted or not, in any case
    for given in ('"Acme Support"', "Acme Support", "acme support", "acme-support"):
        assert slash.handle(f"conflicts {given}", platform="cli") == "Nothing changed in both places.", given
        assert slash.handle(f"check {given}", platform="cli").startswith("- Acme Support: "), given
    assert slash.handle("unknown", platform="cli") == slash.HELP
    assert slash.handle('"unbalanced', platform="cli") == slash.HELP


def test_cli_defaults_to_the_only_twin_and_plurals(svc):
    rc, out, _ = run(["conflicts"])
    assert rc == 0 and "No twins yet" in out
    run(["demo"])
    rc, out, _ = run(["demo", "conflict"])
    assert "Needs your attention" not in out and "Up to date" not in out  # no state printed before it is computed
    assert "The twin notices at its next check (every 60 s), or now with `hermes twin check`." in out
    rc, out, _ = run(["sync"])
    assert rc == 1 and "1 item changed in both places" in out and "item(s)" not in out
    rc, out, _ = run(["conflicts"])
    assert rc == 1 and out.startswith("1 item changed in both places:") and "skill:greet-visitors" in out
    assert "Earlier versions" in out and '--keep grok' in out and '--keep hermes' in out
    assert "<conflict_id>" not in out  # real values in the commands, no placeholders
    rc, out, _ = run(["resolve", "skill:greet-visitors", "--keep", "grok"])
    # the way back is a number, never the long name of the version
    assert rc == 0 and "Kept the Grok version of greet-visitors" in out and "@" not in out
    assert out.rstrip().endswith("Undo: hermes twin restore 1")
    rc, out, _ = run(["versions"])
    lines = out.splitlines()
    assert lines[0].startswith("  1. greet-visitors (skill) · ") and "@" not in out and " id " not in out
    assert lines[-1] == "Put one back: hermes twin restore NUMBER"
    assert run(["restore-points"])[1] == out  # the older name still works
    point = json.loads(run(["versions", "--json"])[1])["points"][0]["id"]  # the long name is for scripts
    assert point.startswith("skill:greet-visitors@")
    rc, _, err = run(["restore", "9"])
    assert rc == 2 and err.splitlines() == ["✗ There is no earlier version 9.",
                                            "  hermes twin versions lists them, numbered."]
    rc, out, _ = run(["restore", "1"])
    assert rc == 0 and out.startswith("Put back the earlier version of greet-visitors.") and "@" not in out
    rc, out, _ = run(["restore", "Acme Support", point])  # by name, and the long form still works
    assert rc == 0 and "Nothing to" in out
    for cmd in (["recent"], ["compare"], ["parity"], ["checklist"], ["digest"]):
        assert "say which one" not in "".join(run(cmd)[1:]), cmd
    rc, out, _ = run(["recent"])
    assert "on 2000-" not in out and "min ago" in out  # fixture activity shown relative to the sample start
    rc, out, _ = run(["skill-sync"])  # sample: no shared folder needed
    assert rc == 0 and "Skill sync is on" in out and "sync bot" in out and "helper bot" not in out
    assert run(["mirror"])[1] == out  # the older name still works
    # with two twins, a command without a twin asks which one, by the names status shows
    run(["add", "--bot", "Recipe Planner", "--yes"])
    rc, _, err = run(["conflicts"])
    assert rc == 2 and err.strip() == '✗ You have 2 twins; say which one: "Acme Support", "Recipe Planner"'
    assert run(["conflicts", "recipe planner"])[0] == 0
    rc, out, _ = run(["versions", "Acme Support"])
    assert rc == 0 and out.splitlines()[-1] == 'Put one back: hermes twin restore "Acme Support" NUMBER'
    assert "say which one: /twin conflicts TWIN (\"Acme Support\", \"Recipe Planner\")" in slash.handle("conflicts", platform="cli")


def test_cli_demo_messages_doctor_status_line(svc):
    run(["demo"])
    for action in ("secret", "grok-edit", "hermes-edit"):
        rc, out, _ = run(["demo", action])
        assert rc == 0 and "or now with `hermes twin check`" in out
        assert "acme-support" not in out
    rc, out, _ = run(["status"])
    first = out.splitlines()[0]
    assert first.startswith("Grok Bot Twin: 1 sample twin") and "Nothing of yours is touched" in first
    assert "botport" not in first and "mode" not in first
    rc, out, _ = run(["doctor"])
    assert "Background check starts with the first chat or Twins page visit" in out
    assert "not running yet" not in out
    assert "/twin check" in slash.handle("demo grok-edit", platform="cli") and "sync" not in slash.handle("demo grok-edit", platform="cli")


def test_cli_restore_with_nothing_kept(svc):
    run(["demo"])
    rc, out, _ = run(["versions"])
    assert "No earlier versions yet." in out and "Hermes copy" not in out
    rc, _, err = run(["restore", "skill:nope@000000000000"])
    assert rc == 2 and err.startswith("✗ ") and "earlier version" in err and "restore point" not in err.lower()
