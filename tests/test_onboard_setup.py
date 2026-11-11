"""`hermes twin setup --check | --plan | --yes`: one setup command for every way in.

- --check and --plan change nothing, on any kind of computer, and say who does each step that is left;
- a run does only what is left and can be run again;
- anything beyond verifying botport waits for a yes;
- the command prints no bot name the caller did not type, and never prints a link code back.
"""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from fleet_testlib import Machine, free_port, wait_until
from twin_testlib import cli, config, home, service, svc  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import connect, fleet_follow as follow, onboard

FAKE = Path(__file__).resolve().parent / "fake_botport.py"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def files(home_dir) -> list:
    return sorted(str(p.relative_to(home_dir)) for p in home_dir.rglob("*"))


def steps(argv) -> dict:
    rc, out, err = run(argv + ["--json"])
    assert err == "", err
    plan = json.loads(out)
    return {**plan, "rc": rc, "by": {s["id"]: s for s in plan["steps"]}}


@pytest.fixture
def no_grok(home, tmp_path):
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"),
                         "grok_app_dir": str(tmp_path / "no-grok-app-here")})
    service.reset_service()
    return service.get_service()


@pytest.fixture
def with_botport(home, tmp_path):
    """The Grok app and a botport that is there but not verified yet; no twin."""
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
    return service.get_service()


# ---- a computer with the Grok app and nothing else ----------------------------------------------------------------
def test_check_and_plan_change_nothing_and_say_who_does_what(svc, home):
    before = files(home)
    rc, out, err = run(["setup", "--check"])
    assert rc == 1 and err == ""  # something is left
    lines = out.splitlines()
    assert lines[0].startswith("Grok Bot Twin setup on ") and lines[0].endswith("--check changes nothing.")
    assert " ✓ Grok Bot Twin is installed and switched on in Hermes." in lines
    assert " ✓ The Grok desktop app is on this computer." in lines
    assert any(l.startswith(" ! Yours to do: botport, a small free app") for l in lines)
    assert "this plugin never downloads it" in out and "hermes twin setup again" in out
    assert any(l.startswith(" · Waits: No twins yet.") for l in lines)
    assert lines[-1] == "Next: The first line marked ! is yours to do. Then run hermes twin setup --check again."
    rc, out2, err = run(["setup", "--plan", "--bot", "Acme Support", "--share"])
    assert rc == 0 and err == "" and "--plan changes nothing" in out2
    assert 'Waits: Twin "Acme Support". This waits until botport is verified.' in out2
    assert "This waits until there is a twin to share." in out2
    assert files(home) == before  # neither wrote a file
    for text in (out, out2):
        assert "<" not in text and "allow-unattested" not in text and "PATH" not in text


def test_json_is_the_same_plan(svc):
    plan = steps(["setup", "--check"])
    assert plan["rc"] == 1 and plan["kind"] == "grok" and plan["complete"] is False
    assert [s["id"] for s in plan["steps"]] == ["plugin", "grok_app", "botport", "twins", "share", "skill_sync"]
    assert plan["by"]["botport"]["state"] == "you" and plan["by"]["twins"]["state"] == "waits"
    assert plan["by"]["share"]["state"] == "optional" and plan["by"]["skill_sync"]["state"] == "optional"
    assert "spends Grok credit" in plan["by"]["skill_sync"]["title"]
    assert set(plan["steps"][0]) == {"id", "state", "title", "does", "how", "ask"}


def test_plain_setup_without_botport_is_what_it_always_was(svc):
    rc, out, err = run(["setup"])
    assert rc == 1 and "hermes twin setup --file ~/Downloads/botport-" in err


def test_options_that_do_not_go_together(svc):
    for argv in (["setup", "--check", "--plan"], ["setup", "--link", "ABCD-EFGH-JKMN", "--share"],
                 ["setup", "--bot", "Acme Support", "--all"], ["setup", "--link", "ABCD-EFGH-JKMN", "--bot", "X"]):
        rc, out, err = run(argv)
        assert rc == 2 and out == "" and err.startswith("✗ "), argv


# ---- botport is there: verify needs no yes; twinning and sharing do ------------------------------------------------
def test_a_botport_that_is_there_is_verified_without_a_question_and_twinning_asks(with_botport, home, monkeypatch):
    plan = steps(["setup", "--plan", "--bot", "Acme Support"])
    assert plan["by"]["botport"]["state"] == "will" and plan["by"]["botport"]["ask"] is False
    assert plan["by"]["twins"]["state"] == "will" and plan["by"]["twins"]["ask"] is True
    assert any("Nothing changes in Grok" in d for d in plan["by"]["twins"]["does"])
    assert with_botport.pair_ids() == []
    # not a terminal and no --yes: the plan is printed, nothing is done
    rc, out, err = run(["setup", "--bot", "Acme Support"])
    assert rc == 1 and "will do this:" in out and out.splitlines()[-1] == "Nothing was changed."
    assert "Not a terminal: pass --yes to go ahead." in err
    assert with_botport.pair_ids() == [] and with_botport.setup_state()["check"]["done"] is False
    # the bot list says this bot has no twin yet
    real = service.TwinService.grok_list

    def untwinned(self):
        data = real(self)
        for b in data["bots"]:
            b["twinned_pair_id"] = None if not self.pair_ids() else b.get("twinned_pair_id")
        return data
    monkeypatch.setattr(service.TwinService, "grok_list", untwinned)
    monkeypatch.setattr(service.TwinService, "trust_botport", lambda self, *a, **k: _trust(self))
    rc, out, err = run(["setup", "--bot", "Acme Support", "--yes"])
    assert rc == 0, out + err
    assert "1 bot now has a twin." in out and with_botport.pair_ids() == ["acme-support"]
    assert "Check any time: hermes twin setup --check" in out
    # again: nothing is left to do for that bot, and nothing is made twice
    rc, out, err = run(["setup", "--bot", "Acme Support", "--yes"])
    assert rc == 0 and with_botport.pair_ids() == ["acme-support"]
    plan = steps(["setup", "--plan", "--bot", "Acme Support"])
    assert plan["by"]["twins"]["state"] == "done" and plan["by"]["botport"]["state"] == "done"


def _trust(self):
    """Accept the stand-in botport the way a test may (it is in no table of release files)."""
    bp = self.botport()
    out = bp.trust(allow_unattested=True, tag=self.release_tag())
    self.invalidate()
    view = self._trust_view(out)
    view["setup"] = self.setup_state()
    return view


def test_setup_all_never_prints_a_bot_name(with_botport, monkeypatch):
    with_botport.trust_botport(True)
    real = service.TwinService.grok_list

    def untwinned(self):
        data = real(self)
        for b in data["bots"]:
            b["twinned_pair_id"] = None if not self.pair_ids() else b.get("twinned_pair_id")
        return data
    monkeypatch.setattr(service.TwinService, "grok_list", untwinned)
    rc, plan_out, _ = run(["setup", "--plan", "--all"])
    assert rc == 0 and "Twin every Grok bot that has no twin yet (names are not printed)." in plan_out
    rc, out, err = run(["setup", "--all", "--yes"])
    assert rc == 0, out + err
    assert "1 bot now has a twin." in out
    rc, check_out, _ = run(["setup", "--check"])
    assert "1 twin on this computer." in check_out
    for text in (plan_out, out, check_out):
        assert "Acme" not in text
    rc, out, _ = run(["setup", "--all", "--yes"])  # again
    assert rc == 0 and "Acme" not in out and with_botport.pair_ids() == ["acme-support"]


def test_what_is_left_says_whether_it_is_the_instructions_or_a_model(svc, monkeypatch):
    def facts(no_text, no_model):
        monkeypatch.setattr(onboard, "_twin_facts", lambda s: (["a", "b"], set(), no_text, no_model))
        return steps(["setup", "--check"])["by"]["finish"]
    st = facts(2, 0)
    assert st["state"] == "you" and st["title"] == "2 twins still wait for their instructions."
    assert "Paste its instructions" in st["how"] and "model" not in st["how"]
    st = facts(0, 1)
    assert st["title"] == "1 twin still waits for a model." and "Choose a model" in st["how"]
    assert "instructions" not in st["how"]
    st = facts(1, 2)
    assert st["title"] == "1 twin still waits for its instructions and 2 twins still wait for a model."
    assert st["how"].endswith("Nobody but you can do this.")
    assert facts(0, 0)["state"] == "done"


def test_a_bot_that_does_not_exist_stops_setup_and_changes_nothing(with_botport):
    with_botport.trust_botport(True)
    rc, out, err = run(["setup", "--bot", "Nobody Bot", "--yes"])
    assert rc == 1 and 'No Grok bot is called "Nobody Bot".' in err and "Nothing was changed." in err
    assert "Setup stopped at this step" in err and with_botport.pair_ids() == []


def test_a_download_found_in_downloads_is_used_only_after_a_yes(svc, monkeypatch):
    state = service.TwinService.setup_state

    def found(self):
        s = state(self)
        s["download"] = {"file_name": "botport-test.tar.gz", "found": {"name": "botport-test.tar.gz"}, "dir": None}
        return s
    monkeypatch.setattr(service.TwinService, "setup_state", found)
    took = []
    monkeypatch.setattr(service.TwinService, "choose_helper", lambda self, *a, **k: took.append(a) or {})
    plan = steps(["setup", "--plan"])
    assert plan["by"]["botport"]["state"] == "will" and plan["by"]["botport"]["ask"] is True
    assert "found in your Downloads folder (botport-test.tar.gz)" in plan["by"]["botport"]["title"]
    assert any("A file that does not match is not kept and never run." in d for d in plan["by"]["botport"]["does"])
    rc, out, err = run(["setup"])
    assert rc == 1 and took == [] and out.splitlines()[-1] == "Nothing was changed."


# ---- a computer without the Grok app --------------------------------------------------------------------------------
def test_without_the_grok_app_setup_is_about_the_link_never_about_botport(no_grok, home):
    before = files(home)
    for argv in (["setup"], ["setup", "--check"]):
        rc, out, err = run(argv)
        assert rc == 1 and err == "", argv
        assert out.splitlines()[0].endswith("--check changes nothing." if "--check" in argv
                                            else "(it gets its twins over a link). Nothing was changed.")
        assert "The Grok app is not on this computer" in out and "It needs no botport." in out
        assert "hermes twin setup --link CODE" in out and "hermes twin setup --share" in out
        assert "Download" not in out and "--file" not in out
    plan = steps(["setup", "--plan", "--link", "7KQ4-M2XD-P9RA"])
    assert plan["kind"] == "linked" and plan["by"]["link"]["state"] == "will" and plan["by"]["link"]["ask"] is True
    assert "7KQ4" not in json.dumps(plan)  # the code is never printed back
    assert files(home) == before
    for argv in (["setup", "--share"], ["setup", "--bot", "Acme Support"]):
        rc, out, err = run(argv + ["--plan"])
        assert rc == 0 and "hermes twin setup --link CODE" in out  # still the one thing this computer needs


@pytest.fixture
def source(tmp_path):
    m = Machine(tmp_path, "mini", free_port())
    m.fx("add", "Test Scout", "Finds trails.")
    m.fx("add", "Test Quill", "Drafts short notes.")
    yield m
    m.stop()


def test_two_computers_are_set_up_with_the_same_command(source, home, tmp_path, monkeypatch):
    # the computer with the twins: setup --share prints a code
    cp = source.run("setup", "--plan", "--share")
    assert "Will do: Share your twins with your other computers." in cp.stdout
    assert "ONE private address" in cp.stdout and "Test Scout" not in cp.stdout
    assert not (source.data / "fleet.json").exists() or source.state().get("share") in (None, {}) \
        or not source.state()["share"].get("on")
    cp = source.run("setup", "--share", "--yes")
    code = next(l.split()[2] for l in cp.stdout.splitlines() if l.startswith("Link code: "))
    assert "hermes twin setup --link CODE" in cp.stdout and "Test Scout" not in cp.stdout
    # again: sharing is done, so no new code is made (this invented computer has twins but no botport, so that
    # is what the run still names)
    cp = source.run("setup", "--share", "--yes", check=False)
    assert "Link code:" not in cp.stdout and code not in cp.stdout + cp.stderr
    cp = source.run("setup", "--share", "--plan")
    assert "✓ Sharing with your other computers is on" in cp.stdout and "Will do" not in cp.stdout
    source.start()
    wait_until(source.listening, what="mini to listen")
    # this process is the other computer
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "share_port": source.port,
                         "machine_name": "Surface", "grok_app_dir": str(tmp_path / "no-grok-app-here")})
    monkeypatch.setattr(follow, "MIN_BACKOFF_S", 0.3)
    monkeypatch.setattr(follow, "MAX_BACKOFF_S", 1.0)
    service.reset_service()
    rc, out, err = run(["setup", "--link", code])  # not a terminal, no --yes
    assert rc == 1 and code not in out and out.splitlines()[-1] == "Nothing was changed."
    rc, out, err = run(["setup", "--link", code, "--yes"])
    assert rc == 0, out + err
    assert "Linked to mini." in out and code not in out and "Test Scout" not in out
    assert "Setup is complete on this computer." in out
    assert (home / "profiles" / "test-scout" / "SOUL.md").is_file()
    rc, out, err = run(["setup", "--check"])
    assert rc == 0 and "Linked to mini" in out and "Setup is complete on this computer." in out
    rc, out, err = run(["setup", "--link", "ABCD-EFGH-JKMN", "--yes"])  # again, with any code: already linked
    assert rc == 0 and "The code you gave was not used." in out and "ABCD" not in out
    follow.stop()


# ---- the ways in: AGENTS.md, CLAUDE.md, the skill, the designs ------------------------------------------------------
import re  # noqa: E402
import shlex  # noqa: E402

import yaml  # noqa: E402

from twin_testlib import ROOT, plugin  # noqa: E402
from hermes_plugins.grok_bot_twin.twin import fleet_profiles as fp  # noqa: E402

SKILL = ROOT / "skills" / "set-up-grok-bot-twin" / "SKILL.md"
WAYS_IN = ("AGENTS.md", "CLAUDE.md", "skills/set-up-grok-bot-twin/SKILL.md", "docs/SETUP-ORIGINS.md",
           "docs/MIRROR-SELF-SETUP.md")
# what Hermes' own scanner of a plugin's files takes for an attack on the model: never in these texts, not even
# as an example of what not to do (the words are put together here so that this file does not hold them either)
SCANNER_WORDS = ("ignore " + "previous instructions", "ignore " + "all previous", "you are " + "now",
                 "disregard " + "your", "forget your " + "instructions", "new " + "instructions:",
                 "system " + "prompt:", "<" + "system>", "<" + "!--", "~/." + "ssh", "~/." + "aws", "sudo " + "rm")


def _twin_commands(text: str) -> list:
    """Every `hermes twin ...` command the text shows as code (in backticks or in a code block)."""
    found = re.findall(r"`(hermes twin [^`\n]*)`", text)
    for block in re.findall(r"```sh\n(.*?)```", text, flags=re.S):
        found += [line.split("#")[0].strip() for line in block.splitlines() if line.startswith("hermes twin ")]
    return sorted(set(found))


def test_every_command_the_guides_show_is_a_real_command():
    seen = 0
    for name in WAYS_IN + ("README.md",):
        for cmd in _twin_commands((ROOT / name).read_text(encoding="utf-8")):
            words = shlex.split(cmd.replace("[options]", "").replace("…", ""))[2:]
            if not words or words[-1] in ("...", "again") or words[0] == "--help":
                continue
            if words[0] in ("share", "requests", "background") and words[-1] in ("on|off|code", "on|off", "[on|off]"):
                continue
            if any(w in ("ID", "NUMBER", "ITEM", "[TWIN]", "TWIN", "DIR", "FILE", "approve|deny", "vX.Y.Z") for w in words) \
                    or words[0] == "requests" and "approve" in words:
                continue  # (shown with a placeholder that is not a value)
            try:
                cli.build_parser().parse_args(words)
            except SystemExit:
                raise AssertionError(f"{name}: `{cmd}` is not a command hermes twin takes")
            seen += 1
    assert seen > 40


def test_agents_md_has_the_steps_the_rules_and_the_check():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    for must in ("hermes twin setup --check", "hermes twin setup --plan", "hermes twin setup --yes",
                 "No secrets in chat.", "No changes to Grok outside the guided steps.",
                 "Do not list your owner's bots.", "Do not go around a refusal.", "Wait for a clear yes.",
                 "## Check your work", "## Undo", "Setup is complete on this computer.",
                 "This plugin never downloads botport, and neither do you."):
        assert must in flat, must
    assert "--allow-unattested" in flat and "never add `--allow-unattested`" in flat
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "[AGENTS.md](AGENTS.md)" in claude and "\n@AGENTS.md\n" in claude and len(claude) < 600
    # the sentences the guides quote are the ones the command prints
    assert "Setup is complete on this computer." in "\n".join(onboard.lines(
        {"computer": "mini", "kind": "grok", "complete": True, "steps": [], "next": ""}, "check"))


def test_the_ways_in_hold_nothing_the_plugin_scanner_takes_for_an_attack_and_no_private_detail():
    for name in WAYS_IN:
        text = (ROOT / name).read_text(encoding="utf-8")
        low = text.lower()
        for words in SCANNER_WORDS:
            assert words not in low, (name, words)
        assert not re.search(r"\|\s*(sh|bash)\b", low), name  # nothing is ever piped into a shell
        assert "—" not in text and "/Users/" not in text and "@" not in text.replace("@AGENTS.md", ""), name
        assert all(ord(ch) < 128 or ch in "…✓·–!→" for ch in text), name


def test_the_skill_is_one_hermes_can_load_and_the_plugin_registers_it(home):
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front = yaml.safe_load(text.split("---\n")[1])
    assert front["name"] == plugin.SKILL_NAME == "set-up-grok-bot-twin"
    assert front["description"] == plugin.SKILL_DESCRIPTION
    assert len(front["description"]) <= 60 and front["description"].endswith(".")  # the width Hermes' index shows
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", front["name"]) and len(text) < 100_000
    body = text.split("---\n", 2)[2]
    order = ["# Set Up Grok Bot Twin Skill", "## When to Use", "## Prerequisites", "## How to Run",
             "## Quick Reference", "## Procedure", "## Pitfalls", "## Verification"]
    places = [body.index(h) for h in order]
    assert places == sorted(places)
    for must in ("every run of that bot spends Grok credit", "Only after a clear yes", "never ask for the pass",
                 "Do nothing in the Grok app", "`terminal`", "Skill sync…"):
        assert must in " ".join(body.split()), must

    class Ctx:
        def __init__(self):
            self.skills = []

        def register_skill(self, name, path, description="", frontmatter=None):
            self.skills.append((name, Path(path), description, dict(frontmatter or {})))

        def __getattr__(self, name):
            if name.startswith("register_") or name == "on_unload":
                return lambda *a, **k: None
            raise AttributeError(name)
    ctx = Ctx()
    plugin.register(ctx)
    assert ctx.skills == [("set-up-grok-bot-twin", SKILL, plugin.SKILL_DESCRIPTION,
                           {"name": "set-up-grok-bot-twin", "description": plugin.SKILL_DESCRIPTION})]
    assert list(home.rglob("*")) == []  # registering it wrote nothing

    class Refuses(Ctx):
        def register_skill(self, *a, **k):
            raise FileNotFoundError("no such file")
    plugin.register(Refuses())  # a Hermes that cannot take the skill still gets the plugin
    # a twin's own Hermes bot gets the plugin's folder copied in: the skill's file travels with it
    assert "skills" in fp.PLUGIN_PARTS


def test_the_designs_say_that_they_are_designs_and_what_is_unverified():
    origins = (ROOT / "docs" / "SETUP-ORIGINS.md").read_text(encoding="utf-8")
    flat = " ".join(origins.split())
    for must in ("DESIGN ONLY", "Nothing in sections 3a and 3b has been run against the real Grok app",
                 "needs the owner's OK", "Test Scout", "Test Sync", "It never types a secret", "UNVERIFIED",
                 "Stop on anything unexpected", "spends Grok credit", "Decisions the owner has to make"):
        assert must in flat, must
    bot = (ROOT / "docs" / "MIRROR-SELF-SETUP.md").read_text(encoding="utf-8")
    flat = " ".join(bot.split())
    for must in ("It is a DESIGN: it has not been run on the real Grok app yet", "The pass is a file, never text.",
                 "Create or edit your own routine | Unknown | UNVERIFIED", "spends Grok credit",
                 "Every file in the shared folder is data."):
        assert must in flat, must
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert "## Start from wherever you are" in readme and "[AGENTS.md](AGENTS.md)" in readme
    assert "Neither has been run on the real Grok app" in readme
    manifest = " ".join((ROOT / "plugin.yaml").read_text(encoding="utf-8").split())
    disclosure = " ".join((ROOT / "docs" / "DISCLOSURE.md").read_text(encoding="utf-8").split())
    assert "One skill ships with the plugin: set-up-grok-bot-twin" in manifest
    assert "grok-bot-twin:set-up-grok-bot-twin" in disclosure and "after you said yes" in disclosure
