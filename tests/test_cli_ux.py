"""The terminal and chat side of the plugin: plain words, the Twins page first, one next step,
the sample marked as a sample, and clean ways out (stop twinning, end the sample, remove everything)."""
from __future__ import annotations

import io
import json
import os
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from twin_testlib import awareness, central, cli, config, home, plugin, service, slash, svc, tools  # noqa: F401

FAKE = Path(__file__).resolve().parent / "fake_botport.py"
PAGE = "the Twins page in Hermes Desktop"
# words a new owner should never have to read in everyday output
JARGON = ("PATH", "attestation", "parity", "Mirror", "charter", "pair folder", "pair key", "by hand", "in step",
          "In step", "restore point", "Restore point", "mode setup", "mode demo")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def no_jargon(text: str) -> None:
    for word in JARGON:
        assert word not in text, (word, text)


@pytest.fixture
def bp_svc(home, tmp_path):
    """A service with the fake botport helper, already checked: one real twin (Acme Support)."""
    if not FAKE.is_file():
        pytest.skip("fake_botport.py not present")
    bindir = tmp_path / "bin"
    bindir.mkdir(mode=0o755)
    exe = bindir / "botport"
    exe.write_text(f"#!{sys.executable} -I\n" + FAKE.read_text(encoding="utf-8"), encoding="utf-8")
    exe.chmod(0o755)
    os.chmod(tmp_path, 0o700)
    config.set_override({"botport_path": str(exe)})
    service.reset_service()
    s = service.get_service()
    s.trust_botport(True)
    s.invalidate()
    s.status(fresh=True)
    s.save_status()  # (the timer's tick takes in a twin made outside the plugin)
    return s


# ---- plain `hermes twin`: the status plus one next step ----------------------------------------------------
def test_bare_command_first_run(svc):
    rc, out, err = run([])
    assert rc == 0 and err == ""
    lines = out.splitlines()
    assert lines[0] == (f"Grok Bot Twin: not connected to Grok yet. Next: open {PAGE}, or run hermes twin setup.")
    assert out.count("Next:") == 1  # one next step
    assert "hermes twin demo" in lines[1] and "sample" in lines[1]
    assert "Usage" not in out and "botport:" not in out
    no_jargon(out)
    assert run(["status"])[1] == out  # the same thing


def test_bare_command_with_the_sample(svc):
    run(["demo"])
    rc, out, _ = run([])
    lines = out.splitlines()
    assert rc == 0
    assert lines[0] == ("Grok Bot Twin: 1 sample twin, nothing needs you. Sample twin with invented data. Nothing "
                        "of yours is touched.")
    assert lines[1].startswith(" ● Acme Support (sample): Follows Grok · checked ")  # the page's sentence for the row
    assert "default" not in out and "profile" not in out and "⇄" not in out  # never names a real profile
    assert lines[-1].startswith("Next: to twin your real Grok bots, open the Twins page in Hermes Desktop")
    assert out.count("Next") == 1
    no_jargon(out)


def test_status_says_what_to_do_about_each_matter(svc):
    run(["demo"])
    run(["demo", "conflict"])
    run(["sync"])
    rc, out, _ = run(["status"])
    assert rc == 1 and "1 sample twin, 1 needs your attention." in out.splitlines()[0]
    assert "Acme Support (sample): Changed in both places" in out and "Needs your attention" not in out
    assert out.splitlines()[-1] == (f"Next (Acme Support): choose which version to keep on {PAGE}, or run "
                                    "hermes twin conflicts.")
    run(["resolve", "skill:greet-visitors", "--keep", "hermes"])
    run(["demo", "secret"])
    run(["sync"])
    rc, out, _ = run(["status"])
    # the row's sentence, as on the page: the change kept in Hermes still waits to go to Grok, and the password
    assert rc == 1 and "Acme Support (sample): 2 things need you" in out
    # the sample has no Grok app behind it: its own command is the next step, never "the Grok app"
    assert out.splitlines()[-1].startswith("Next (Acme Support): the sample holds its fake password back until "
                                           "the sample bot's text changes: hermes twin demo grok-edit")
    assert "Grok app" not in out
    no_jargon(out)


# ---- help: three groups, one form for the twin argument ------------------------------------------------------
def test_help_is_grouped_in_three(svc):
    text = cli.build_parser().format_help()
    a, b, c = text.index("\nEveryday\n"), text.index("\nFix things\n"), text.index("\nAdvanced\n")
    assert a < b < c
    everyday, fix, advanced = text[a:b], text[b:c], text[c:]
    for cmd in ("status", "add", "stop"):  # "stop", as the page says "Stop twinning" (remove still works)
        assert f"\n  {cmd}" in everyday, cmd
    for cmd in ("conflicts", "resolve", "versions", "restore", "doctor", "setup"):
        assert f"\n  {cmd}" in fix, cmd
    for cmd in ("check", "list-grok", "recent", "compare", "syncs", "checklist", "switch", "approve", "skip", "send-to-grok",
                "skill-sync", "digest", "verify", "demo", "uninstall"):
        assert f"\n  {cmd}" in advanced, cmd
    # the older names still work but are not in the overview (words nobody needs)
    for old in ("restore-points", "parity", "mirror", "attest"):
        assert old not in text, old
    assert "also:" not in text and "twin id" not in text and "short name" not in text
    assert 'TWIN is a twin\'s name, such as "Acme Support" (shown by status).' in text
    # the page's words: Check now is "check" (sync still works, unlisted); a version is put back by its number;
    # "link" is for computers, so Remove speaks of twins and their records
    # ("syncs", the What syncs panel, is a command of its own)
    assert not re.search(r"\n  sync\b", text) and "restore [TWIN] NUMBER" in text and " ID " not in text
    assert "every twin and its records" in text and "twin link" not in text
    assert "gh attestation verify" not in text and "helper" not in text and "key file" not in text
    assert "{status," not in text  # no flat list of 21 commands
    assert "PAIR" not in text and "[TWIN]" in text
    # every command in the overview exists, and every command is in the overview
    sub = next(x for x in cli.build_parser()._actions if getattr(x, "choices", None) and "status" in x.choices)
    aliases = {"sync", "move", "restore-points", "parity", "mirror", "attest", "remove"}
    for name in set(sub.choices) - aliases:
        assert name in text, name
    assert aliases <= set(sub.choices)


def test_twin_argument_has_one_form(svc):
    p = cli.build_parser()
    for cmd in ("sync", "conflicts", "versions", "restore-points", "recent", "compare", "parity", "checklist",
                "switch", "send-to-grok", "skill-sync", "mirror", "remove", "digest"):
        assert p.parse_args([cmd, "acme-support"]).pair == "acme-support", cmd
        assert p.parse_args([cmd]).pair is None, cmd
    assert p.parse_args(["sync", "--pair", "acme-support"]).pair_opt == "acme-support"  # the older spelling still works
    run(["demo"])
    assert run(["sync", "--pair", "acme-support"])[0] == 0 and run(["check", "acme-support"])[0] == 0
    # TWIN is the bot's name as status shows it, in any case; the short name works as before
    assert p.parse_args(["check", "Acme Support"]).pair == "Acme Support"
    for given in ("Acme Support", "acme support", "ACME SUPPORT"):
        rc, out, _ = run(["check", given])
        assert rc == 0 and out.startswith(" Acme Support: "), given
        assert run(["recent", given])[0] == 0, given
    # a name that is no twin's is one plain sentence with the names there are; nothing that is not a twin's
    # name or short name ever reaches the files
    for given in ("../etc", "Nobody Here"):
        rc, out, err = run(["remove", given, "--dry-run"])
        assert rc == 2 and out == "", given
        assert err.splitlines() == [f'✗ No twin is called "{given}".', '  Your twins: "Acme Support"'], given
    with pytest.raises(SystemExit):
        p.parse_args(["remove", ""])


# ---- adding: plain preview, real and sample bots never mixed -----------------------------------------------
def test_add_wording_with_the_sample(svc, monkeypatch):
    run(["demo"])
    rc, out, _ = run(["list-grok"])
    assert "Acme Support (sample) · has a twin" in out and "Recipe Planner (sample)" in out
    assert "[" not in out  # a bot is named by its name; Grok's own mark shows only for two bots with one name
    # --all means the owner's real Grok bots: never the sample's invented ones
    rc, out, err = run(["add", "--all", "--yes"])
    assert rc == 1 and "No Grok bots found. Run hermes twin doctor to see why." in err
    assert svc.pair_ids() == ["acme-support"]
    rc, _, err = run(["add", "--bot", "Nobody Bot", "--yes"])
    assert rc == 1 and 'No Grok bot called "Nobody Bot". See the names with hermes twin list-grok.' in err
    rc, _, err = run(["add"])
    assert rc == 2 and "hermes twin list-grok" in err
    asked = []
    monkeypatch.setattr(cli, "_confirm", lambda q: asked.append(q) or False)
    rc, out, _ = run(["add", "--bot", "Recipe Planner"])
    assert rc == 1 and asked == ["Twin 1 bot? Nothing changes in Grok."] and "Nothing changed." in out
    assert svc.pair_ids() == ["acme-support"]  # the preview wrote nothing
    lines = out.splitlines()
    assert lines[0] == "Ready to twin Recipe Planner (sample)"
    assert lines[1].startswith("  Copies now: ") and lines[2].startswith("  Later, with the page's help: ")
    assert lines[3] == '  Optional: 1 routine, from the twin\'s "…" menu.' and "routine" not in lines[2]
    assert lines[4] == "  2 skills: optional, set up later."
    no_jargon(out)
    assert " create" not in out and "schedule" not in out
    rc, out, _ = run(["add", "--bot", "Recipe Planner", "--yes"])
    assert rc == 0 and "Recipe Planner now has a twin." in out and PAGE in out
    assert len(svc.pair_ids()) == 2


# ---- leaving: stop twinning, end the sample, start it again ---------------------------------------------------
def test_remove_the_only_twin_needs_no_id_and_the_sample_can_start_again(svc):
    run(["demo"])
    rc, out, err = run(["remove"])  # not a terminal and no --yes: asks, changes nothing
    assert rc == 1 and "End the sample? Nothing of yours changes." in out and "Nothing changed." in out
    assert svc.pair_ids() == ["acme-support"]
    rc, out, _ = run(["remove", "--yes"])
    assert rc == 0 and out.splitlines()[-1] == "Sample removed. Nothing of yours was changed."
    assert "profile" not in out and "state:" not in out
    assert svc.pair_ids() == []
    # removing the last sample twin ended the sample: no dead end, it starts again
    rc, out, _ = run([])
    assert out.startswith("Grok Bot Twin: not connected to Grok yet.")
    rc, out, _ = run(["demo"])
    assert rc == 0 and out.startswith("Sample twin ready.") and "already running" not in out
    assert svc.pair_ids() == ["acme-support"] and "Acme Support (sample)" in out
    rc, out, _ = run(["demo", "remove"])
    assert rc == 0 and out.strip() == "Sample removed. Nothing of yours was changed."
    assert "hermes twin sync" not in out and "No demo" not in out
    assert svc.pair_ids() == []


def test_remove_a_real_twin_says_what_stays_and_what_goes(bp_svc):
    rc, out, _ = run(["remove", "--dry-run"])
    assert rc == 0
    assert out.splitlines()[0] == "Stop twinning Acme Support?"
    for line in ("Your bot in Grok, unchanged.", "Its Hermes bot and all its chats.", "Following Grok.",
                 "The earlier versions kept for undo.",
                 "You can twin it again later; it picks up this Hermes bot again.", "Nothing changed (dry run)."):
        assert line in out, line
    assert "key file" not in out and "pass" not in out.split("Files:")[0]  # the small internal file is not a line
    assert out.index("Stays:") < out.index("Removed:") < out.index("Files:")
    assert "Removed 2 item(s)" not in out and "The profile was kept" not in out
    rc, out, _ = run(["remove"])  # no --yes, not a terminal
    assert rc == 1 and "Nothing changed." in out
    rc, out, _ = run(["remove", "--yes"])
    assert rc == 0 and out.splitlines()[-1] == "Acme Support is no longer twinned. Its Hermes bot is still here."


def test_uninstall_dry_run_then_for_real(svc, home):
    run(["demo"])
    rc, out, _ = run(["uninstall", "--dry-run"])
    assert rc == 0 and out.splitlines()[0] == "Remove Grok Bot Twin?"
    for line in ("Your Grok bots.", "Your Hermes bots and their chats.", "The sample.", "Nothing changed (dry run)."):
        assert line in out, line
    for line in ("twin link", "Every twin", "Earlier versions", "botport"):  # only what exists now is listed
        assert line not in out, line
    assert out.index("Stays:") < out.index("Removed:") and "This ends the sample." in out
    assert "0 bots" not in out
    assert svc.pair_ids() == ["acme-support"] and (central(home) / "demo").is_dir()
    rc, out, _ = run(["uninstall"])  # asks first; not a terminal -> nothing happens
    assert rc == 1 and "Nothing changed." in out and svc.pair_ids() == ["acme-support"]
    rc, out, _ = run(["uninstall", "--yes"])
    assert rc == 0 and "Grok Bot Twin's data is removed." in out
    assert "Settings → Capabilities → Plugins" in out and "hermes plugins remove grok-bot-twin" in out
    assert service.get_service().pair_ids() == []
    assert not (central(home) / "demo").exists() and not (central(home) / "events.jsonl").exists()
    # and afterwards the plugin is back at the first-run state
    assert run([])[1].startswith("Grok Bot Twin: not connected to Grok yet.")


def test_uninstall_with_a_real_twin(bp_svc, home):
    rc, out, _ = run(["uninstall", "--dry-run"])
    assert rc == 0 and "This stops twinning 1 bot." in out
    rc, out, _ = run(["uninstall", "--yes"])
    assert rc == 0 and "Your Grok bots, your Hermes bots and their chats are untouched." in out
    assert not (central(home) / "pairs.json").exists()


# ---- everyday commands in the page's words -------------------------------------------------------------------
def test_send_keep_and_move_wording(svc):
    run(["demo"])
    run(["demo", "hermes-edit"])
    rc, out, _ = run(["status"])
    assert 'ready to send to Grok: fun-fact  ->  hermes twin approve "skill:fun-fact"' in out
    assert "waiting for your OK" not in out
    rc, out, _ = run(["skip", "skill:fun-fact"])
    assert rc == 0 and out.startswith("fun-fact stays in Hermes only.")
    assert "ready to send to Grok" not in run(["status"])[1]
    rc, out, _ = run(["skip", "skill:fun-fact", "--undo"])
    assert rc == 0 and "offered for Grok again" in out
    rc, out, _ = run(["approve", "skill:fun-fact"])
    assert rc == 0 and out.splitlines()[0] == ("Approved fun-fact. It goes to Grok at the next send: "
                                                "hermes twin send-to-grok.")
    assert "(to remote)" not in out
    rc, out, _ = run(["send-to-grok"])
    assert rc == 0 and out.startswith("Sent 1 skill")
    assert run(["send-to-grok"])[1].strip() == "Nothing to send to Grok right now."
    rc, out, _ = run(["compare"])
    assert out.splitlines()[0].startswith("Compare with Grok: ") and " match" in out.splitlines()[0]
    assert run(["parity"])[1] == out and "—" not in out  # the older name still works
    assert "Setup parity" not in out and "no model call" not in out
    rc, out, _ = run(["checklist"])
    assert out.startswith("You do these yourself:") and "Move routines to Hermes:" in out
    assert "Ready to switch" not in out
    rc, out, _ = run(["move", "--yes"])
    assert rc == 0 and "Turned on 2 routines in Hermes." in out and "hermes twin switch --undo" in out
    assert "Follows Grok · 2 routines run in Hermes" in run(["status"])[1]  # the page's sentence for the row
    rc, out, _ = run(["switch", "--undo"])
    assert rc == 0 and out.strip() == "Paused 2 routines in Hermes again."
    assert "runs in Hermes" not in run(["status"])[1]


def test_turning_skill_sync_off_asks_first(svc):
    run(["demo"])
    rc, out, _ = run(["mirror", "--off"])  # not a terminal, no --yes (the older name still works)
    assert rc == 1 and "pass is replaced" in out and "sync bot" in out and "Nothing changed." in out
    assert "key file" not in out and "helper bot" not in out
    assert svc.status(fresh=True)["pairs"][0]["mirror_bot"]["connected"] is True
    rc, out, _ = run(["skill-sync", "--off", "--yes"])
    assert rc == 0 and out.splitlines()[-1] == "Skill sync is off. Skills go back to copy-yourself."
    rc, out, _ = run(["skill-sync"])
    assert rc == 0 and "Skill sync is on." in out


# ---- setup: real values, no unsafe advice ----------------------------------------------------------------------
def test_setup_without_the_helper_gives_real_commands(svc):
    rc, out, err = run(["setup"])
    assert rc == 1
    assert "https://github.com/hudsonwa/botport/releases/tag/v0.2.1" in err
    assert "hermes twin setup --file ~/Downloads/botport-" in err and ".tar.gz" in err
    assert "Connect my Grok bots" in err  # the page first
    assert "botport, a small free app that reads your bot list from Grok" in err and "helper" not in err
    assert "<" not in err and "allow-unattested" not in err and "PATH" not in err
    assert "gh release" not in err and "GitHub" not in err  # the GitHub route is shown only when asked for
    rc, out, err = run(["setup", "--with-github"])
    assert rc == 1 and "gh release download v0.2.1 --repo hudsonwa/botport --pattern botport-" in err
    assert "hermes twin setup --file botport-" in err and "<" not in err and "allow-unattested" not in err
    # the older spelling of the option still works
    assert cli.build_parser().parse_args(["setup", "--helper", "x.tar.gz"]).helper == "x.tar.gz"
    assert cli.build_parser().parse_args(["setup", "--file", "x.tar.gz"]).helper == "x.tar.gz"


def test_setup_without_gh_never_suggests_the_unsafe_flag(bp_svc, monkeypatch):
    from hermes_plugins.grok_bot_twin.twin.engine import EngineError
    want = ("This check needs GitHub's command-line tool. Install it, run gh auth login, then run "
            "hermes twin setup --with-github again.")
    for err_ in (EngineError("gh_missing", "This check needs GitHub's free command-line tool, signed in once.",
                             "Install it, run gh auth login, then press Verify botport."),
                 EngineError("attestation_failed", "the GitHub CLI (gh) is not on PATH",
                             "For a local build only: hermes twin setup --allow-unattested")):
        def boom(self, *a, _e=err_, **k):
            raise _e
        monkeypatch.setattr(service.TwinService, "trust_botport", boom)
        rc, out, err = run(["setup"])
        assert rc == 1 and want in err
        assert "allow-unattested" not in out + err and "not on PATH" not in err
    def bad(self, *a, **k):
        raise EngineError("check_failed", "This download does not match the publisher's signed build record. "
                                          "Download it again from the download page.", "detail from gh")
    monkeypatch.setattr(service.TwinService, "trust_botport", bad)
    rc, out, err = run(["setup"])
    assert rc == 1 and "does not match the publisher's signed build record" in err
    assert "https://github.com/hudsonwa/botport/releases/tag/" in err and "allow-unattested" not in out + err


# ---- chat: the page first, the sample marked ------------------------------------------------------------------
def test_slash_help_and_empty_wording(svc):
    assert "[TWIN]" in slash.HELP and "PAIR" not in slash.HELP and "Twins page in Hermes Desktop" in slash.HELP
    empty = slash.handle("", platform="cli")
    assert empty.startswith("No twins yet. Open the Twins page in Hermes Desktop and press Connect my Grok bots.")
    assert "hermes twin add" not in empty
    assert slash.handle("conflicts", platform="cli").startswith("No twins yet. Open the Twins page")
    slash.handle("demo", platform="cli")
    assert "- Acme Support [sample]: Follows Grok, checked " in slash.handle("status", platform="cli")
    assert "profile" not in slash.handle("status", platform="cli")
    slash.handle("demo conflict", platform="cli")
    out = slash.handle("sync", platform="cli")
    assert "1 item changed in both places" in out and "Twins page" in out
    out = slash.handle("conflicts", platform="cli")
    assert out.startswith("1 item changed in both places:") and "- greet-visitors (skill)" in out
    assert out.index("Twins page in Hermes Desktop") < out.index("hermes twin resolve")  # page first, command second
    assert "--keep hermes|grok" not in out
    assert "Sample removed. Nothing of yours was changed." in slash.handle("demo remove", platform="cli")


def test_tools_mark_the_sample_and_point_to_the_page(svc):
    hint = json.loads(tools.twin_status({}))["hint"]
    assert hint.startswith("No twins yet. Open the Twins page in Hermes Desktop and press Connect my Grok bots.")
    assert "Try `hermes twin demo`" not in hint
    assert "Twins page" in json.loads(tools.twin_recent({}))["hint"]
    svc.demo_action("start")
    t = json.loads(tools.twin_status({}))["twins"][0]
    assert t["note_for_user"] == "This is the built-in sample twin with invented data." and t["sample"] is True
    svc.demo_action("conflict")
    svc.sync()
    t = json.loads(tools.twin_status({}))["twins"][0]
    assert t["state"] == "Needs your attention" and t["changed_in_both_places"] == 1
    assert ("Choose which version to keep for 1 item changed in both places (on the Twins page)"
            in t["to_do_in_hermes"])
    text = json.dumps(t)
    for old in ("hermes twin", "by hand", "Mirror", "In step", "Needs you\"", "suspected secret"):
        assert old not in text, old


def test_awareness_is_silent_for_the_default_sample_and_marks_it_when_asked_for(svc):
    svc.demo_action("start")
    awareness.invalidate()
    assert awareness.build_block(svc.profile(), svc) is None  # the sample is in no real profile's chats
    svc.demo_action("remove")
    # "Show the sample in this Hermes profile" (demo_profile) is the explicit opt-in
    config.set_override({"botport_path": "/nonexistent-botport", "demo_profile": "default"})
    service.reset_service()
    s = service.get_service()
    s.demo_action("start")
    awareness.invalidate()
    block = awareness.build_block("default", s)
    assert block is not None and "This is a sample twin with invented data." in block.splitlines()
    assert "up to date" in block and "in step" not in block and "by-hand" not in block
    assert len(block.splitlines()) <= awareness.MAX_LINES


# ---- the terminal tells the page's story: the same header counts, names instead of ids ----------------------
def _twin(name, pid, **over):
    p = {"pair_id": pid, "state": "in_step", "grok": {"name": name, "present": True}, "hermes": {"profile": pid},
         "setup_left": [], "waiting": [], "conflicts": 0, "pending": {}, "secrets_withheld": [], "secrets_held": 0,
         "last_sync_at": None}
    p.update(over)
    return p


def test_the_header_counts_every_open_thing_the_way_the_page_does():
    calm, quill = _twin("Test Scout", "test-scout"), _twin("Test Quill", "test-quill", setup_left=["instructions"])
    assert cli._header([calm]) == "1 twin, nothing needs you."
    # (the page: "2 twins · 1 to finish setting up"; the terminal said "nothing needs you" for the same two)
    assert cli._header([calm, quill]) == "2 twins, 1 to finish setting up."
    both = _twin("Trail Guide", "trail-guide", state="needs_you", conflicts=1, setup_left=["instructions", "model"])
    send = _twin("Garden Notes", "garden-notes", state="changes_waiting", pending={"to_grok": 2})
    asks = _twin("Book Club", "book-club", state="changes_waiting",
                 waiting=[{"item": "skill:greet-visitors", "to": "grok"}])
    assert cli._header([calm, quill, both, send, asks]) == (
        "5 twins, 1 needs your attention, 2 with a change to send to Grok, 2 to finish setting up.")
    # a change to send beside something that needs the owner is counted once, as needing them
    mixed = _twin("Trail Guide", "trail-guide", state="needs_you", pending={"to_grok": 1},
                  secrets_withheld=[{"item": "persona.description"}])
    gone = _twin("Book Club", "book-club", state="grok_missing", grok={"name": "Book Club", "present": False})
    failed = _twin("Garden Notes", "garden-notes", state="error")
    assert cli._header([mixed, gone, failed]) == "3 twins, 3 need your attention."
    assert cli._header([_twin("Test Scout", "test-scout", pending={"to_hermes": 1})]) == "1 twin, updating."
    # the sample that shows both of its sides is there to be watched: its chores are not counted
    sample = _twin("Acme Support", "acme-support", sample=True, setup_left=["instructions"],
                   grok={"name": "Acme Support", "present": True, "description": "a"},
                   hermes={"profile": "x", "description": "a"})
    assert cli._header([sample]) == "1 sample twin, nothing needs you."
    for line in (cli._header([calm, quill, both, send, asks]), cli._header([mixed, gone, failed])):
        assert len(line.split()) <= 22 and "·" not in line


def test_status_names_twins_by_name_and_never_prints_an_id(svc):
    run(["demo"])
    run(["add", "--bot", "Recipe Planner", "--yes"])
    rc, out, _ = run(["status"])
    lines = out.splitlines()
    assert rc == 0 and lines[0].startswith("Grok Bot Twin: 2 sample twins, ")
    assert " id " not in out and "acme-support" not in out and "recipe-planner" not in out
    assert json.loads(run(["status", "--json"])[1])["pairs"][0]["pair_id"] == "acme-support"  # scripts keep it
    run(["demo", "conflict"])
    run(["check"])
    rc, out, _ = run(["status"])
    assert rc == 1 and "1 needs your attention" in out.splitlines()[0]
    # the one next step names the twin as the row does, ready to paste
    assert out.splitlines()[-1].endswith('or run hermes twin conflicts "Acme Support".')
    assert " id " not in out and "acme-support" not in out
    rc, out, _ = run(["conflicts", "Acme Support"])
    assert rc == 1 and 'hermes twin resolve "Acme Support" "skill:greet-visitors" --keep grok' in out
    rc, out, _ = run(["resolve", "acme support", "skill:greet-visitors", "--keep", "grok"])
    assert rc == 0 and out.rstrip().endswith('Undo: hermes twin restore "Acme Support" 1')
    rc, out, _ = run(["restore", "Acme Support", "1"])
    assert rc == 0 and out.startswith("Put back the earlier version of greet-visitors.")
    assert "@" not in out and (out.count("\n") == 1 or "kept too (hermes twin versions)." in out)


def test_two_twins_with_one_name_are_told_apart_by_their_short_names(svc, monkeypatch):
    names = {"acme-support": "Acme Support", "acme-support-2": "Acme Support", "test-quill": "Test Quill"}
    monkeypatch.setattr(svc, "pair_ids", lambda: list(names))
    monkeypatch.setattr(cli, "_names", lambda _svc: dict(names))
    assert cli._resolve(svc, "test quill") == "test-quill" and cli._resolve(svc, "acme-support-2") == "acme-support-2"
    with pytest.raises(cli.EngineError) as exc:
        cli._resolve(svc, "Acme Support")
    assert exc.value.message == ('More than one twin is called "Acme Support". Use its short name instead: '
                                 "acme-support, acme-support-2")
    with pytest.raises(cli.EngineError) as exc:
        cli._one(svc, None)
    assert exc.value.message == 'You have 3 twins; say which one: acme-support, acme-support-2, "Test Quill"'
    with pytest.raises(cli.EngineError) as exc:
        cli._resolve(svc, "Nobody; here")
    assert exc.value.message == 'No twin is called "Nobody; here".'
    assert exc.value.hint == 'Your twins: "Acme Support", "Acme Support", "Test Quill"'


def test_removal_fallback_lines_keep_link_for_computers():
    for line in cli.REMOVE_GOES + cli.UNINSTALL_GOES:
        assert "link" not in line.lower(), line
    assert cli.REMOVE_GOES[0] == "Following Grok." and cli.UNINSTALL_GOES[0] == "Every twin."
