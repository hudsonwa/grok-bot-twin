"""Import setup export: the file a Grok bot writes about itself, brought into its twin.

Invented bots and invented exports only (Acme Support, Recipe Planner; nothing here was ever a real bot's). Every
test has its own HERMES_HOME; "botport" is tests/fake_botport.py and "Hermes' command line" is a stand-in that
writes the routine list the way Hermes does. Key-shaped strings are built by joining pieces, so none sits in the
source.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from twin_testlib import all_text_under, central, cli, config, home, service  # noqa: F401
from test_ux_flows import P, _client, _pair, fake  # noqa: F401
from test_setup_download_and_twin_facts import SOUL, err, profile, twin  # noqa: F401
import test_service_real_botport as real_rig
from test_service_real_botport import rig  # noqa: F401  (a real botport build; skipped without BOTPORT_TEST_BIN)
from hermes_plugins.grok_bot_twin.twin import hermes_side as hs, importer, setup_export as sx, tasks

PID = "acme-support"
REAL_CREATE = importer.create_paused_cron  # (the stand-in below takes its place in most tests)
REAL_FLAGS = importer.cron_create_flags
# what must never be kept, shown or written anywhere: each is a word no other text in the plugin holds
MEMORY = "zebra-memory-canary likes long walks"
FACT = "quokka-fact-canary lives on an invented street"
SECRET = "pass" + "word: " + "tr0ub4dor" + "-and-" + "3xample"
ROUTINE_SECRET = "to" + "ken=" + "0f1e2d3c" + "4b5a6978" + "8796a5b4" + "c3d2e1f0"
APP_ADDRESS = "https://mcp.example.test/widgets?key=canary-address"
SKILL = "narwhal-skill-canary"
KEY_BLOCK = "-----BEGIN " + "PRIVATE KEY-----\nMIIcanaryblockline\n-----END " + "PRIVATE KEY-----"


def export(**over) -> dict:
    bot = {
        "name": "Acme Support",
        "title": "Invented helper",
        "instructions": f"You are Acme Support.\n{SECRET}\nBe friendly and brief.\nNever guess a price.",
        "memories": [MEMORY, "second " + MEMORY, "third " + MEMORY],
        "user_facts": [FACT, "second " + FACT],
        "skills": [{"name": SKILL, "instructions": "Say hello " + SKILL}],
        "routines": [
            {"name": "Morning summary", "instruction": "Summarise yesterday's invented tickets.",
             "trigger": "schedule", "schedule_cron": "0 8 * * 1-5", "schedule_text": "Weekdays at 08:00",
             "timezone": "UTC", "paused": False},
            {"name": "Ticket arrives", "instruction": "Triage the new invented ticket.", "trigger": "event",
             "schedule_text": "When a ticket arrives"},
            {"name": "Leaky", "instruction": f"Call the invented service with {ROUTINE_SECRET}",
             "trigger": "schedule", "schedule_cron": "5 5 * * *", "timezone": "UTC"},
        ],
        "apps": [{"name": "Widget Desk", "mcp_url": APP_ADDRESS, "id": "widget-desk"}, {"name": "Calendar"},
                 {"name": "widget desk"}],
    }
    bot.update(over)
    return {"format": "grok-bot-export", "version": 1, "bots": [bot]}


def text_of(**over) -> str:
    return json.dumps(export(**over))


class Hermes:
    """Stands in for `hermes -p PROFILE cron create --paused`: records what it was asked and writes the routine
    list the way Hermes does. `paused=False` plays a Hermes that ignored the flag."""

    def __init__(self, home: Path, monkeypatch, flags=("--paused", "--paused-reason", "--timezone"), paused=True,
                 fail_on=None, profile=PID, stored_as=None, says=(True, "")):
        self.file = home / "profiles" / profile / "cron" / "jobs.json"
        self.asked: list = []
        self.paused_now: list = []
        self.n = 0
        self.paused, self.fail_on, self.stored_as, self.says = paused, fail_on, stored_as, says
        monkeypatch.setattr(importer, "cron_create_flags", lambda prof: set(flags) if flags is not None else None)
        monkeypatch.setattr(importer, "create_paused_cron", self.create)
        monkeypatch.setattr(importer, "pause_cron", self.pause)

    def jobs(self) -> list:
        return json.loads(self.file.read_text())["jobs"] if self.file.exists() else []

    def _write(self, jobs: list) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps({"jobs": jobs}))

    def create(self, prof, name, schedule, prompt, timezone="", reason=""):
        self.asked.append({"profile": prof, "name": name, "schedule": schedule, "prompt": prompt,
                           "timezone": timezone, "reason": reason})
        if self.fail_on == name:
            return False, "Hermes could not add it (hermes cron create ended with 2)"
        self.n += 1
        self._write(self.jobs() + [{"id": f"a1b2c3d4e5{self.n:02d}", "name": self.stored_as or name, "prompt": prompt,
                                    "enabled": not self.paused, "state": "paused" if self.paused else "scheduled",
                                    "paused_reason": reason if self.paused else None,
                                    "schedule": {"kind": "cron", "expr": schedule, "display": schedule}}])
        return self.says

    def pause(self, job_id, prof, ctx=None):
        self.paused_now.append(job_id)
        jobs = self.jobs()
        for j in jobs:
            if j["id"] == job_id:
                j["enabled"], j["state"] = False, "paused"
        self._write(jobs)
        return True, ""


def soul(home: Path) -> str:
    return (home / "profiles" / PID / "SOUL.md").read_text(encoding="utf-8")


def nothing_kept(home: Path, *more: str) -> None:
    """None of the things an import must never keep is anywhere under the Hermes folder (every file, the event
    list included)."""
    everything = all_text_under(home)
    for bad in ("canary", "tr0ub4dor", "0f1e2d3c" + "4b5a", "mcp.example.test", *more):
        assert bad not in everything, bad


# =====================================================================================================================
# the request and the sheet's first answer
# =====================================================================================================================

def test_the_request_asks_for_setup_only_and_names_nobody(twin, home):
    info = twin.setup_export_info(PID)
    assert info == {"pair_id": PID, "request": sx.REQUEST, "privacy": sx.PRIVACY, "last": None}
    req = info["request"]
    assert '"format": "grok-bot-export"' in req and '"version": 1' in req
    assert '"memories": []' in req and '"user_facts": []' in req and "nothing you remember about me" in req
    assert "Never put a password, token, key or cookie" in req and "Change nothing about yourself" in req
    assert "Acme" not in req  # the same text for every bot: no name of the owner's is put into a prompt
    assert sx.PRIVACY.count(".") == 1 and "lists of memories and facts about you" in sx.PRIVACY  # one sentence
    # the request itself, filled in by a bot, is a file this import reads
    filled = json.loads(req[req.index("{"): req.rindex("}") + 1])
    assert set(filled["bots"][0]) == {"name", "instructions", "routines", "apps", "memories", "user_facts"}


# =====================================================================================================================
# a preview writes nothing
# =====================================================================================================================

def test_a_preview_says_what_would_happen_and_writes_nothing(twin, home, monkeypatch):
    h = Hermes(home, monkeypatch)
    before = all_text_under(home / "profiles")
    events = len(twin.events()["events"])
    r = twin.import_setup(PID, text_of(), False)
    assert r["applied"] is False and r["added"] == 0 and r["bot"] == {"name": "Acme Support", "matches": True}
    ins = r["instructions"]
    assert (ins["state"], ins["lines"], ins["left_out"], ins["replaces"], ins["saved"]) == ("new", 3, [2], False, False)
    assert ins["text"] == "You are Acme Support.\nBe friendly and brief.\nNever guess a price."
    by_name = {x["name"]: x for x in r["routines"]}
    assert by_name["Morning summary"] == {"name": "Morning summary", "schedule": "0 8 * * 1-5", "timezone": "UTC",
                                          "prompt": "Summarise yesterday's invented tickets.", "state": "new", "why": ""}
    assert by_name["Ticket arrives"]["state"] == "manual" and "event" in by_name["Ticket arrives"]["why"]
    assert by_name["Leaky"]["state"] == "held" and by_name["Leaky"]["prompt"] == ""
    assert r["connectors"] == [{"name": "Widget Desk"}, {"name": "Calendar"}]  # names only, once each
    assert r["left_out"] == {"memories": 3, "user_facts": 2, "skills": 1, "routines": 0}
    assert r["privacy"] == sx.PRIVACY
    # nothing of the memories, the facts, the password line, the held routine or the address is in the answer
    said = json.dumps(r)
    for bad in ("canary", "tr0ub4dor", "0f1e2d3c" + "4b5a", "mcp.example.test"):
        assert bad not in said, bad
    # and nothing at all was written: not the profile, not a note, not an event, no routine
    assert all_text_under(home / "profiles") == before and h.asked == []
    assert not (central(home) / sx.NOTES_FILE).exists() and len(twin.events()["events"]) == events
    assert twin.setup_export_info(PID)["last"] is None


# =====================================================================================================================
# an import
# =====================================================================================================================

def test_an_import_fills_the_twin_and_keeps_none_of_the_memories(twin, home, monkeypatch):
    h = Hermes(home, monkeypatch)
    r = twin.import_setup(PID, text_of(), True, "Acme Support")
    assert r["applied"] is True and r["added"] == 1 and r["instructions"]["saved"] is True
    # instructions: the plugin's own block, without the line that looked like a password; botport's part untouched
    s = soul(home)
    assert s.startswith(SOUL) and hs.soul_facts(home / "profiles" / PID)["own"] == \
        "You are Acme Support.\nBe friendly and brief.\nNever guess a price."
    assert _pair(twin)["setup_left"] == ["model"]
    # the version before is kept, and says an import replaced it
    kept = hs.kept_list(central(home), PID)
    assert [(k["why"], k["text"]) for k in kept] == [("import", "")]
    # routines: only the clean, timed one was made, and it was made switched off
    assert [a["name"] for a in h.asked] == ["Morning summary"]
    assert h.asked[0]["schedule"] == "0 8 * * 1-5" and h.asked[0]["timezone"] == "UTC"
    assert h.asked[0]["reason"].startswith(importer.SETUP_EXPORT_MARKER) and h.asked[0]["profile"] == PID
    states = {x["name"]: x["state"] for x in r["routines"]}
    assert states == {"Morning summary": "added", "Ticket arrives": "manual", "Leaky": "held"}
    job = h.jobs()[0]
    assert job["enabled"] is False and job["state"] == "paused" and h.paused_now == []
    # moving them on stays the existing flow: it lists the new routine as one to turn on, by its id
    plan = twin.checklist(PID)["switch"]
    assert [j["job_id"] for j in plan["hermes_jobs_paused"]] == [job["id"]] and plan["hermes_jobs_resumed"] == []
    assert _pair(twin)["routines"]["paused_in_hermes"] == 1 and _pair(twin)["routines"]["on_in_hermes"] == 0
    reg = json.loads((central(home) / "pairs.json").read_text())["pairs"][0]
    assert reg["job_ids"] == [job["id"]] and reg["instructions"]["from"] == "export"
    # the routine came from Grok: noted (a digest, no text), so its Hermes copy does not ask to go to Grok
    noted = json.loads((central(home) / "copied.json").read_text())[PID]
    assert noted == {"routine:Morning summary": importer.routine_digest(home / "profiles" / PID, "Morning summary")}
    assert len(noted["routine:Morning summary"]) == 12
    # connectors: names, to set up on this computer, found again later
    last = twin.setup_export_info(PID)["last"]
    assert last["connectors"] == ["Widget Desk", "Calendar"] and last["at"]
    # the event says how much, never what
    ev = [e for e in twin.events()["events"] if e["type"] == "setup_export"][-1]
    assert ev["payload"] == {"instructions": True, "routines": 1, "connectors": 2, "lines_left_out": 1}
    # memories, facts about the owner, the password line, the held routine's prompt, the address and the skill:
    # nowhere under the Hermes folder
    nothing_kept(home)
    # the same file again: nothing new to do, and nothing is made twice
    again = twin.import_setup(PID, text_of(), True, "Acme Support")
    assert again["instructions"]["state"] == "same" and again["instructions"]["saved"] is False and again["added"] == 0
    assert {x["name"]: x["state"] for x in again["routines"]}["Morning summary"] == "exists"
    assert len(h.jobs()) == 1 and len(hs.kept_list(central(home), PID)) == 1


def test_an_import_over_instructions_can_be_undone(twin, home, monkeypatch):
    Hermes(home, monkeypatch)
    twin.set_instructions(PID, "You are the old Acme Support.")
    plan = twin.import_setup(PID, text_of(routines=[], apps=[]), False)
    assert plan["instructions"]["replaces"] is True
    twin.import_setup(PID, text_of(routines=[], apps=[]), True)
    assert "old Acme Support" not in soul(home)
    pts = [p for p in twin.restore_points(PID)["points"] if p["item"] == "persona.instructions"]
    old = next(p for p in pts if p["text"] == "You are the old Acme Support.")
    assert old["replaced_by"] == "import" and old["reason"] == "Before you imported its setup export"
    assert twin.restore(PID, old["id"])["restored"] == ["persona.instructions"]
    assert "You are the old Acme Support." in soul(home)


def test_parts_can_be_left_out(twin, home, monkeypatch):
    h = Hermes(home, monkeypatch)
    r = twin.import_setup(PID, text_of(), True, None, ["instructions", "connectors"])
    assert r["instructions"]["saved"] is False and "grok-bot-twin:begin instructions" not in soul(home)
    assert r["added"] == 1 and len(h.jobs()) == 1 and not (central(home) / sx.NOTES_FILE).exists()
    assert err(twin.import_setup, PID, text_of(), True, None, ["everything"]).code == "bad_input"


# =====================================================================================================================
# routines are switched off, or they are not made
# =====================================================================================================================

def test_a_hermes_that_cannot_make_a_routine_switched_off_makes_none(twin, home, monkeypatch):
    for flags in ((), ("--timezone",), ("--paused",), None):
        h = Hermes(home, monkeypatch, flags=flags)
        r = twin.import_setup(PID, text_of(instructions=""), True)
        assert h.asked == [] and h.jobs() == [] and r["added"] == 0
        row = next(x for x in r["routines"] if x["name"] == "Morning summary")
        # (a Hermes that did not answer is not called too old)
        assert row["state"] == "failed" and row["why"] == (sx.NO_ANSWER if flags is None else sx.NO_PAUSED)


def test_a_long_import_stops_in_time_and_the_same_file_adds_the_rest(twin, home, monkeypatch):
    h = Hermes(home, monkeypatch)
    routines = [{"name": f"Routine {i}", "instruction": "Do an invented thing.", "trigger": "schedule",
                 "schedule_cron": "0 9 * * *"} for i in range(4)]
    clock = iter([0.0, 1.0, 2.0, 500.0, 501.0])  # the start, then one look before each of the four routines
    monkeypatch.setattr(sx, "_now", lambda: next(clock))
    r = twin.import_setup(PID, text_of(routines=routines, instructions=""), True)
    assert [x["state"] for x in r["routines"]] == ["added", "added", "failed", "failed"] and r["added"] == 2
    assert r["routines"][2]["why"] == sx.LATER
    monkeypatch.setattr(sx, "_now", lambda: 0.0)
    r = twin.import_setup(PID, text_of(routines=routines, instructions=""), True)
    assert [x["state"] for x in r["routines"]] == ["exists", "exists", "added", "added"] and len(h.jobs()) == 4


def test_a_routine_that_came_out_switched_on_is_switched_off_at_once(twin, home, monkeypatch):
    h = Hermes(home, monkeypatch, paused=False)
    r = twin.import_setup(PID, text_of(), True)
    job = h.jobs()[0]
    assert h.paused_now == [job["id"]] and job["enabled"] is False and r["added"] == 1
    # and when even that does not hold, it is not called added: the owner is told to switch it off
    monkeypatch.setattr(importer, "pause_cron", lambda *a, **k: (False, "no"))
    two = export()
    two["bots"][0]["routines"][0]["name"] = "Evening summary"
    r = twin.import_setup(PID, json.dumps(two), True)
    row = next(x for x in r["routines"] if x["name"] == "Evening summary")
    assert row["state"] == "failed" and "switch it off in Hermes now" in row["why"] and r["added"] == 0


def test_after_a_failure_the_rest_is_not_tried(twin, home, monkeypatch):
    h = Hermes(home, monkeypatch, fail_on="First")
    routines = [{"name": n, "instruction": "Do an invented thing.", "trigger": "schedule",
                 "schedule_cron": "0 9 * * *"} for n in ("First", "Second")]
    r = twin.import_setup(PID, text_of(routines=routines), True)
    assert [a["name"] for a in h.asked] == ["First"] and h.jobs() == []
    assert [(x["state"], x["why"]) for x in r["routines"]] == [
        ("failed", "Hermes could not add it (hermes cron create ended with 2)"), ("failed", sx.NOT_TRIED)]
    assert h.asked[0]["timezone"] == "" and "this computer's time zone" in h.asked[0]["reason"]


def test_what_hermes_cannot_take_is_left_to_the_owner(twin, home, monkeypatch):
    h = Hermes(home, monkeypatch, flags=("--paused", "--paused-reason"))  # a Hermes without --timezone
    base = {"instruction": "Do an invented thing.", "trigger": "schedule", "schedule_cron": "0 9 * * *"}
    routines = [
        {**base, "name": "Words only", "schedule_cron": "every monday 9am"},
        {**base, "name": "Six fields", "schedule_cron": "0 0 9 * * *"},
        {**base, "name": "Odd zone", "timezone": "Mars/Olympus Mons; rm"},
        {**base, "name": "No prompt", "instruction": "  "},
        {**base, "name": "Too long", "instruction": "word " * 4000},
        {**base, "name": ""},
        {**base, "name": "Twice"}, {**base, "name": "Twice"},
        {**base, "name": "In Paris", "timezone": "Europe/Paris"},
        {**base, "name": "In UTC", "timezone": "UTC"},
        "not a routine", 7, None,
    ]
    r = twin.import_setup(PID, text_of(routines=routines), True)
    got = {x["name"]: x for x in r["routines"]}
    for name in ("Words only", "Six fields", "Odd zone", "No prompt", "Too long", "(no name)"):
        assert got[name]["state"] == "manual" and got[name]["why"].endswith("add it in Hermes yourself"), name
    assert [x["state"] for x in r["routines"] if x["name"] == "Twice"] == ["added", "manual"]
    assert got["In Paris"]["state"] == "failed" and "time zone" in got["In Paris"]["why"]  # never at the wrong hour
    assert got["In UTC"]["state"] == "added"
    assert sorted(a["name"] for a in h.asked) == ["In UTC", "Twice"]
    assert all(a["timezone"] == "" for a in h.asked)
    # more than fifty: the rest is counted, not read
    many = [{**base, "name": f"Routine {i}"} for i in range(60)]
    r = twin.import_setup(PID, text_of(routines=many), False)
    assert len(r["routines"]) == 50 and r["left_out"]["routines"] == 10


def test_the_real_command_line_keeps_every_text_out_of_the_options(home, monkeypatch):
    seen: list = []

    def run(argv, timeout, env=None, out_cap=0, what=""):
        seen.append(argv)
        if argv[-1] == "--help":
            return 0, b"usage: cron create [--name NAME] [--paused] [--paused-reason R] [--timezone TZ]", b"", False
        return 0, b"", b"", False

    monkeypatch.setattr(importer, "hermes_argv", lambda prof=None: ["hermes", "-p", prof])
    monkeypatch.setattr(importer, "run_capped", run)
    importer._help_seen.clear()
    assert importer.cron_create_flags(PID) == {"--paused", "--paused-reason", "--timezone"}
    assert importer.cron_create_flags(PID) == {"--paused", "--paused-reason", "--timezone"} and len(seen) == 1
    ok, _ = importer.create_paused_cron(PID, "--help", "0 8 * * *", "--deliver=all then\n--paused=no", "UTC", "why")
    assert ok and seen[-1] == ["hermes", "-p", PID, "cron", "create", "--name=--help", "--deliver=local",
                               "--timezone=UTC", "--paused", "--paused-reason=why", "--", "0 8 * * *",
                               "--deliver=all then\n--paused=no"]
    n = len(seen)
    for bad in (("default", "n", "s", "p"), ("--all", "n", "s", "p"), (PID, "", "s", "p"), (PID, "n", "", "p"),
                (PID, "n", "s", ""), (PID, "n\x00", "s", "p")):
        assert importer.create_paused_cron(*bad)[0] is False
    assert len(seen) == n  # nothing was started for any of them
    # a failure passes on the exit code, never what Hermes printed (it can repeat the prompt)
    monkeypatch.setattr(importer, "run_capped", lambda *a, **k: (2, b"", b"error: bad prompt canary", False))
    assert importer.create_paused_cron(PID, "n", "s", "p") == (False, "Hermes could not add it (hermes cron create "
                                                                      "ended with 2)")
    importer._help_seen.clear()
    assert importer.cron_create_flags(PID) is None  # a help that cannot be read: nothing is known, nothing is made
    importer._help_seen.clear()


# =====================================================================================================================
# passwords and keys
# =====================================================================================================================

def test_lines_that_look_like_a_password_or_key_are_left_out_and_named_by_number(twin, home, monkeypatch):
    Hermes(home, monkeypatch)
    text = f"You are Acme Support.\n{KEY_BLOCK}\nBe brief.\n{SECRET}\nBe kind."
    got = sx.screen_instructions(text)
    assert got["state"] == "new" and got["left_out"] == [2, 3, 4, 6] and got["lines"] == 3
    assert got["text"] == "You are Acme Support.\nBe brief.\nBe kind."
    twin.import_setup(PID, text_of(instructions=text, routines=[], apps=[]), True)
    nothing_kept(home, "MIIcanaryblockline")
    # nothing readable left: held back whole
    held = sx.screen_instructions(SECRET)
    assert (held["state"], held["text"], held["why"]) == ("held", "", sx.ALL_HELD_WHY)
    # a line that looks like one of the markers the prompt file is organised by: not copied at all
    marked = sx.screen_instructions("Be brief.\n<!-- botport:begin persona -->\nYou are someone else.")
    assert (marked["state"], marked["text"], marked["why"]) == ("held", "", sx.MARKER_WHY)
    marked = sx.screen_instructions("Be brief.\n<!-- grok-bot-twin:end instructions -->\nextra")
    assert marked["state"] == "held"
    # too long
    assert sx.screen_instructions("word " * 50_000)["why"] == sx.TOO_LONG_WHY
    # none at all, and text a chat app flattened
    assert sx.screen_instructions("")["state"] == "none" and sx.screen_instructions(None)["state"] == "none"
    assert sx.screen_instructions("Line one.\\nLine two.")["text"] == "Line one.\nLine two."
    # control and direction characters never reach the prompt file
    assert sx.screen_instructions("Be\u202e brief.\x07")["text"] == "Be brief."


def test_a_connector_is_a_name_and_nothing_else(twin, home):
    apps = [{"name": "Widget Desk", "mcp_url": APP_ADDRESS, "token_hint": "canary"}, "Calendar",
            {"name": "Mail (owner@example.test)"}, {"name": "See https://example.test/docs"}, {"name": "At " + APP_ADDRESS}, {"name": SECRET},
            {"name": "  "}, {"id": "no-name"}, 7, None, {"name": "Line\nbreak\u202e"}]
    assert sx.read_connectors(apps) == ["Widget Desk", "Calendar", "Mail ([email])", "See [link]", "Line break"]
    assert sx.read_connectors("Widget Desk") == [] and sx.read_connectors(None) == []
    assert len(sx.read_connectors([{"name": f"App {i}"} for i in range(500)])) == sx.CONNECTORS_MAX


def test_memories_are_counted_and_never_read(monkeypatch):
    class Trap(list):
        """A list that fails the test the moment anything looks inside it."""
        def __iter__(self):
            raise AssertionError("the memories were walked")

        def __getitem__(self, i):
            raise AssertionError("a memory was read")

    doc = export()
    doc["bots"][0]["memories"] = Trap([MEMORY] * 4)
    doc["bots"][0]["user_facts"] = Trap([FACT] * 2)
    got = sx.read_setup(doc, "Acme Support")
    assert got["left_out"]["memories"] == 4 and got["left_out"]["user_facts"] == 2
    assert "canary" not in json.dumps(got)
    # keys this import does not name are never looked at either
    doc["bots"][0]["title"] = "canary title"
    doc["bots"][0]["conversations"] = Trap(["canary"])
    doc["owner"] = {"email": "canary@example.test"}
    assert "canary" not in json.dumps(sx.read_setup(doc, "Acme Support"))


# =====================================================================================================================
# which bot, and what is not an export
# =====================================================================================================================

def test_the_bot_in_the_file_is_checked_against_the_twin(twin, home, monkeypatch):
    Hermes(home, monkeypatch)
    other = text_of(name="Recipe Planner")
    r = twin.import_setup(PID, other, False)
    assert r["bot"] == {"name": "Recipe Planner", "matches": False}
    both = export()
    both["bots"].append({**export()["bots"][0], "name": "Recipe Planner", "instructions": "You plan recipes."})
    r = twin.import_setup(PID, json.dumps(both), False)
    assert r["bot"] == {"name": "Acme Support", "matches": True} and "Acme" in r["instructions"]["text"]
    r = twin.import_setup(PID, json.dumps(both), False, "recipe  planner")
    assert r["bot"] == {"name": "Recipe Planner", "matches": False} and r["instructions"]["text"] == "You plan recipes."
    assert err(twin.import_setup, PID, json.dumps(both), False, "Nobody").code == "bad_export"
    both["bots"][0]["name"] = "Trail Guide"
    e = err(twin.import_setup, PID, json.dumps(both), False)
    assert e.code == "several_bots" and "2 bots" in e.message and "own chat" in e.hint


def test_what_is_not_a_setup_export_is_refused_in_plain_words(twin, home):
    for bad in ("", "   ", "hello", "{", "[1, 2]", "{}", '{"format": "something-else"}',
                '{"format": "grok-bot-export", "bots": []}', '{"format": "grok-bot-export", "bots": [{"name": ""}]}',
                json.dumps({"format": "grok-bot-export", "version": 2, "bots": [{"name": "Acme Support"}]}),
                json.dumps({"format": "grok-bot-export", "version": True, "bots": [{"name": "Acme Support"}]}),
                "[" * 100_000):
        e = err(twin.import_setup, PID, bad, False)
        assert e.code in ("bad_export", "bad_input"), bad[:30]
        assert "Traceback" not in e.message and e.message
    assert err(twin.import_setup, PID, "x" * (sx.TEXT_CAP + 1), False).code == "too_large"
    assert err(twin.import_setup, PID, None, False).code == "bad_input"
    assert err(twin.import_setup, PID, text_of(), "yes").code == "bad_input"
    assert err(twin.import_setup, PID, text_of(), False, 7).code == "bad_input"
    # a bot that answered with a sentence and a code fence around the JSON is still read
    wrapped = "Here is my setup export:\n```json\n" + text_of() + "\n```\nAnything else?"
    assert twin.import_setup(PID, wrapped, False)["bot"]["matches"] is True
    # a file that is only a bot's name is an export with nothing in it
    r = twin.import_setup(PID, json.dumps({"format": "grok-bot-export", "bots": [{"name": "Acme Support"}]}), False)
    assert r["instructions"]["state"] == "none" and r["routines"] == [] and r["connectors"] == []
    assert r["left_out"] == {"memories": 0, "user_facts": 0, "skills": 0, "routines": 0}


def test_the_sample_can_be_looked_at_but_not_imported_into(home):
    svc = service.get_service()
    svc.demo_action("start")
    pid = svc.demo.pair_ids()[0]
    name = next(p["grok"]["name"] for p in svc.status()["pairs"] if p["pair_id"] == pid)
    r = svc.import_setup(pid, text_of(name=name), False)
    assert r["bot"]["matches"] is True and r["instructions"]["state"] == "new"
    e = err(svc.import_setup, pid, text_of(name=name), True)
    assert e.code == "sample" and "sample twin" in e.message
    nothing_kept(home)


# =====================================================================================================================
# the page's routes, the command line, leaving
# =====================================================================================================================

def test_the_routes(twin, home, monkeypatch):
    Hermes(home, monkeypatch)
    c = _client(monkeypatch)
    info = c.get(P + f"/pairs/{PID}/setup-export").json()
    assert set(info) == {"pair_id", "request", "privacy", "last"} and info["last"] is None
    plan = c.post(P + f"/pairs/{PID}/setup-export", json={"text": text_of()}).json()
    assert plan["applied"] is False and set(plan) == {"pair_id", "applied", "bot", "instructions", "routines",
                                                      "connectors", "left_out", "added", "privacy"}
    assert set(plan["instructions"]) == {"state", "lines", "left_out", "replaces", "text", "saved", "why"}
    done = c.post(P + f"/pairs/{PID}/setup-export", json={"text": text_of(), "apply": True,
                                                          "bot": plan["bot"]["name"]}).json()
    assert done["applied"] is True and done["added"] == 1 and done["instructions"]["saved"] is True
    assert c.get(P + f"/pairs/{PID}/setup-export").json()["last"]["connectors"] == ["Widget Desk", "Calendar"]
    # problems come back as the page's one failure shape, in plain words
    assert c.post(P + f"/pairs/{PID}/setup-export", json={"text": "hello"}).json()["error"]["code"] == "bad_export"
    big = c.post(P + f"/pairs/{PID}/setup-export", json={"text": "x" * (sx.TEXT_CAP + 1)}).json()
    assert big["error"]["code"] == "too_large"
    assert c.post(P + f"/pairs/{PID}/setup-export", json={"text": text_of(), "apply": "yes"}).status_code == 400
    assert c.post(P + "/pairs/nobody/setup-export", json={"text": text_of()}).json()["error"]["code"] == "pair_not_found"
    assert c.get(P + "/pairs/..%2Fx/setup-export").status_code in (400, 404)
    nothing_kept(home)


def run_cli(capsys, *argv) -> tuple:
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_the_command_line(twin, home, tmp_path, monkeypatch, capsys):
    h = Hermes(home, monkeypatch)
    rc, out, _ = run_cli(capsys, "import", "--request")
    assert rc == 0 and sx.REQUEST in out and "hermes twin import FILE" in out
    f = tmp_path / "acme-setup-export.json"
    f.write_text(text_of(), encoding="utf-8")
    # not a terminal and no --yes: it shows the plan and changes nothing
    rc, out, errs = run_cli(capsys, "import", str(f))
    assert rc == 1 and "Nothing changed." in out and "pass --yes" in errs and h.asked == []
    saved = "You are Acme Support.\nBe friendly and brief.\nNever guess a price."
    prompt = "Summarise yesterday's invented tickets."
    assert (f"Instructions: 3 lines, {len(saved)} characters (line 2 left out: it looks like a password or key)"
            in out)
    assert (f"+ Morning summary (0 8 * * 1-5 UTC): arrives switched off; what it runs is {len(prompt)} characters"
            in out)
    assert "Read the text before you import it: add --show" in out
    assert "! Leaky (5 5 * * *): not copied: looks like a password or key" in out
    assert "Set up on this computer: Widget Desk, Calendar" in out
    assert "Left out: 3 memories, 2 facts about you, 1 skill." in out and sx.PRIVACY in out
    for bad in ("canary", "tr0ub4dor", "0f1e2d3c" + "4b5a", "mcp.example.test", "Summarise yesterday"):
        assert bad not in out + errs, bad  # the terminal never shows a prompt, a memory or a held-back value
    # --show prints all of what would be saved and run, and nothing else of the file
    rc, out, errs = run_cli(capsys, "import", str(f), "--show")
    assert rc == 1 and saved in out and prompt in out and "Read the text before" not in out and h.asked == []
    for bad in ("canary", "tr0ub4dor", "0f1e2d3c" + "4b5a", "mcp.example.test", "Triage the new"):
        assert bad not in out + errs, bad
    rc, out, _ = run_cli(capsys, "import", "Acme Support", str(f), "--yes")
    assert rc == 0 and "Imported: instructions saved; 1 routine added, switched off; 2 connectors listed" in out
    assert "Nothing was sent to Grok." in out and "hermes twin switch" in out and len(h.jobs()) == 1
    rc, out, _ = run_cli(capsys, "import", str(f), "--yes")
    assert rc == 0 and "Instructions: already in Hermes." in out
    assert "= Morning summary (0 8 * * 1-5 UTC): a routine of this name is already in Hermes (not compared)" in out
    # a file that is not there, a folder, no file at all
    assert run_cli(capsys, "import", str(tmp_path / "missing.json"))[0] == 2
    assert run_cli(capsys, "import", str(tmp_path))[0] == 2
    assert run_cli(capsys, "import")[0] == 2
    # the chat turn of a request from another computer cannot import anything
    monkeypatch.setattr(tasks, "asked_turn", lambda *a, **k: True)
    n = len(h.asked)
    rc, _, errs = run_cli(capsys, "import", str(f), "--yes")
    assert rc == 1 and "request from another computer" in errs and len(h.asked) == n
    nothing_kept(home)


def test_stop_twinning_forgets_the_list_and_keeps_the_hermes_bot(twin, home, monkeypatch):
    Hermes(home, monkeypatch)
    twin.import_setup(PID, text_of(), True)
    assert PID in json.loads((central(home) / sx.NOTES_FILE).read_text())
    twin.remove(PID, False)
    assert json.loads((central(home) / sx.NOTES_FILE).read_text()) == {}
    assert "You are Acme Support." in soul(home)  # the Hermes bot keeps what was imported into it


# =====================================================================================================================
# a twin made by a real botport build (skipped without BOTPORT_TEST_BIN)
# =====================================================================================================================

@pytest.mark.skipif(not (real_rig.BIN and os.path.isfile(real_rig.BIN) and os.access(real_rig.BIN, os.X_OK)),
                    reason="set BOTPORT_TEST_BIN to a botport build to run this")
def test_an_import_into_a_twin_made_by_a_real_botport(rig, monkeypatch):  # noqa: F811
    """botport keeps its own parts of the prompt file and has its own, stricter password check before every
    update. An import must leave its parts alone, must not count as a change to send to Grok, and must not make
    botport stop updating the twin."""
    svc, home_dir, world = rig["svc"], rig["home"], rig["world"]
    svc.trust_botport(True)
    made = svc.create([real_rig.ACME])["pairs"][0]
    pid, prof = made["pair_id"], made["hermes"]["profile"]
    h = Hermes(home_dir, monkeypatch, profile=prof)
    soul_file = home_dir / "profiles" / prof / "SOUL.md"
    before_soul = soul_file.read_text(encoding="utf-8")
    before = real_rig._pair(svc, pid)
    text = text_of(name="Acme Helper")
    plan = svc.import_setup(pid, text, False)
    assert plan["bot"] == {"name": "Acme Helper", "matches": True} and soul_file.read_text(encoding="utf-8") == before_soul
    done = svc.import_setup(pid, text, True, plan["bot"]["name"])
    assert done["instructions"]["saved"] is True and done["added"] == 1 and len(h.jobs()) == 1
    after_soul = soul_file.read_text(encoding="utf-8")
    assert hs._OWN_RE.sub("", after_soul) == hs._OWN_RE.sub("", before_soul)  # every line that was there, as it was
    assert hs.soul_facts(home_dir / "profiles" / prof)["own"].startswith("You are Acme Support.")
    # botport checks and updates the twin as before: no refusal, nothing new to send to Grok, nothing waiting
    svc.sync(pid)
    after = real_rig._pair(svc, pid)
    # (botport lists any routine of the Hermes bot as "apply the Hermes copy in the Grok app". This one came from
    # Grok, so the plugin does not pass that on: the row stays as calm as it was.)
    assert after["state"] == before["state"] != "error" and after["pending"] == before["pending"]
    assert after["by_hand"] == before["by_hand"] and after["state_reason"] == before["state_reason"]
    assert "instructions" not in after["setup_left"]
    assert after["routines"] == {"in_grok": 1, "paused_in_hermes": 1, "on_in_hermes": 0}
    assert [r["name"] for r in svc.checklist(pid)["switch"]["grok_routines_to_turn_off"]] == ["Morning summary"]
    # changed in Hermes afterwards, it is a Hermes change like any other and asks again
    jobs = h.jobs()
    jobs[0]["prompt"] = "Summarise the invented tickets, shortest first."
    h._write(jobs)
    edited = real_rig._pair(svc, pid)
    assert edited["pending"]["to_grok"] == 1 and edited["pending"]["to_grok_items"] == ["routine:Morning summary"]
    assert edited["routines"]["in_grok"] == 1  # still one routine in Grok, though it is listed on both sides now
    jobs[0]["prompt"] = "Summarise yesterday's invented tickets."
    h._write(jobs)
    # and a change in Grok still arrives afterwards
    world.step("rename")
    svc.sync(pid)
    assert real_rig._pair(svc, pid)["state"] != "error"
    for bad in ("zebra-memory-canary", "quokka-fact-canary", "tr0ub4dor", "narwhal-skill-canary", "mcp.example.test"):
        assert real_rig._files_with(home_dir, bad) == [], bad


# =====================================================================================================================
# edge cases of the password-and-key check and of what an import keeps, one test each
# =====================================================================================================================
SOFT_HYPHEN, TAG_A, WIDE_S, HALF = chr(0xAD), chr(0xE0041), chr(0xFF53), chr(0xD800)
VALUE = "Tr0ub4dor" + "Example99"
LONG_KEY = "sk" + "-" + "abcdEFGH1234ijklMNOP5678qrstUVWX"


def test_a_password_on_the_line_after_its_label_is_left_out_too(twin, home, monkeypatch):
    Hermes(home, monkeypatch)
    for label in ("pass" + "word:", "api" + "_key =", "to" + "ken:"):
        got = sx.screen_instructions(f"You are Acme.\n{label}\n{VALUE}\nBe brief.")
        assert got["left_out"] == [2, 3] and got["text"] == "You are Acme.\nBe brief.", label
        got = sx.screen_instructions(f"You are Acme.\n{label}\n\n\n{VALUE}\nBe brief.")
        assert VALUE not in got["text"] and got["left_out"] == [2, 3, 4, 5], label
    twin.import_setup(PID, text_of(instructions=f"You are Acme.\npass" + f"word:\n{VALUE}\nBe brief.", routines=[],
                                   apps=[]), True)
    assert VALUE not in all_text_under(home)


def test_what_is_imported_is_what_was_shown_to_the_last_character(twin, home, monkeypatch):
    h = Hermes(home, monkeypatch)
    long_text = "\n".join(f"Rule {i}: an invented rule about widgets." for i in range(1500)) + "\nTHE LAST LINE."
    long_prompt = "Step. " * 2000 + "THE LAST STEP."
    assert len(long_text) > 60_000 and 10_000 < len(long_prompt) < sx.PROMPT_CAP
    routines = [{"name": "Long one", "instruction": long_prompt, "trigger": "schedule", "schedule_cron": "0 9 * * *"}]
    plan = twin.import_setup(PID, text_of(instructions=long_text, routines=routines, apps=[]), False)
    assert plan["instructions"]["text"] == long_text and plan["routines"][0]["prompt"] == long_prompt
    twin.import_setup(PID, text_of(instructions=long_text, routines=routines, apps=[]), True)
    assert hs.soul_facts(home / "profiles" / PID)["own"] == plan["instructions"]["text"]
    assert h.asked[0]["prompt"] == plan["routines"][0]["prompt"]
    # what cannot be shown whole is not imported: longer instructions and prompts are left to the owner
    over = twin.import_setup(PID, text_of(instructions="word " * 50_000, routines=[
        {**routines[0], "name": "Longer", "instruction": "x" * (sx.PROMPT_CAP + 1)}], apps=[]), False)
    assert over["instructions"]["state"] == "held" and over["instructions"]["text"] == ""
    assert over["routines"][0]["state"] == "manual" and over["routines"][0]["prompt"] == ""


def test_no_check_runs_over_more_than_a_field_may_hold(monkeypatch):
    """A file may be two million characters. The slow checks never see more than: the instructions' cap, a
    prompt's cap, or 2,000 characters of a field that is meant to be short."""
    seen: list = []
    real = sx._secrets.has_secret
    monkeypatch.setattr(sx._secrets, "has_secret", lambda t: (seen.append(len(t)), real(t))[1])
    real_flag = sx._secrets.flagged_lines
    monkeypatch.setattr(sx._secrets, "flagged_lines", lambda t, prose=True: (seen.append(len(t)), real_flag(t, prose))[1])
    huge = ("a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q " * 60_000)[:1_900_000]
    base = {"name": "R", "instruction": "Do an invented thing.", "trigger": "schedule", "schedule_cron": "0 9 * * *"}
    doc = export(instructions=huge, name="Acme Support", apps=[{"name": huge}],
                 routines=[{**base, "instruction": huge}, {**base, "name": huge}, {**base, "timezone": huge},
                           {**base, "schedule_cron": huge}, {**base, "schedule_text": chr(0xFDFA) * 100_000},
                           {**base, "trigger": huge}])
    doc["bots"][0]["name"] = "Acme Support " + huge
    got = sx.read_setup(doc, "Acme Support")
    assert got["instructions"]["why"] == sx.TOO_LONG_WHY and got["routines"][0]["state"] == "manual"
    assert seen and max(seen) <= sx.FIELD_CUT  # (also of a field whose plain form is longer than it looks)
    grown = chr(0xFDFA) * 60_000  # (eighteen plain characters each: not checked at all, and not copied)
    seen.clear()
    assert sx.screen_instructions(grown)["why"] == sx.TOO_LONG_WHY and seen == []
    rows, _ = sx.read_routines([{**base, "instruction": chr(0xFDFA) * 9000}, {**base, "name": chr(0xFDFA) * 2000}])
    assert rows[0]["state"] == "held" and max(seen, default=0) <= sx.FIELD_CUT
    # line numbers are found in one pass: a text with many matches is read in a time that grows with its length
    many = "\n".join("a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q" for _ in range(3000))
    found = sx._secrets.detect_secrets(many)
    assert [f["line"] for f in found] == list(range(1, 3001)) and sx._secrets.flagged_lines(many) == set(range(1, 3001))


def test_half_a_surrogate_pair_never_stops_an_import_half_way(twin, home, monkeypatch):
    h = Hermes(home, monkeypatch)
    # (apart, each is half a pair and no character; side by side they would be one real character)
    routines = [{"name": "Hi" + HALF, "instruction": "Say hi " + HALF + " there." + chr(0xDC80), "trigger": "schedule",
                 "schedule_cron": "0 9 * * *"}]
    r = twin.import_setup(PID, text_of(instructions="Be brief." + HALF, routines=routines,
                                       apps=[{"name": "Desk" + HALF}]), True)
    assert r["instructions"]["saved"] is True and r["added"] == 1 and r["connectors"] == [{"name": "Desk"}]
    assert h.asked[0]["name"] == "Hi" and h.asked[0]["prompt"] == "Say hi  there."
    (home / "profiles" / PID / "SOUL.md").read_text(encoding="utf-8")  # still a file that can be read
    assert [e for e in twin.events()["events"] if e["type"] == "setup_export"]
    # the paste path is repaired by the same change
    twin.set_instructions(PID, "Be kind." + HALF)
    assert hs.soul_facts(home / "profiles" / PID)["own"] == "Be kind."
    assert REAL_CREATE(PID, "n", "0 9 * * *", "p" + HALF) == (False, "invalid routine")  # (before any command)


def test_nothing_invisible_is_saved_and_a_key_cannot_hide_behind_it():
    hidden = ("Be brief." + TAG_A + chr(0xE0042) + SOFT_HYPHEN + chr(0x034F) + chr(0x3164) + chr(0x2800) + chr(0xFFFC)
              + " Be kind.")
    assert sx.screen_instructions(hidden)["text"] == "Be brief. Be kind."
    # what shows, or changes how its neighbour shows, stays as it was written: an emoji in colour, a keycap, an
    # ideograph's variant, a character this Python does not know yet, a private-use sign, the Arabic number sign
    kept = ("Heart " + chr(0x2764) + chr(0xFE0F) + " one " + "1" + chr(0xFE0F) + chr(0x20E3) + " variant " + chr(0x845B)
            + chr(0xE0100) + " new " + chr(0x1FAE9) + " own " + chr(0xF8FF) + " sign " + chr(0x0600) + ".")
    assert sx.screen_instructions(kept)["text"] == kept
    # ordinary words about keys are not a key
    prose = "You are Acme.\nNever begin sharing a private key with anyone.\nBe brief."
    assert sx.screen_instructions(prose) == {"state": "new", "text": prose, "left_out": [], "lines": 3, "why": ""}
    for breaker in (SOFT_HYPHEN, chr(0xFE0F), TAG_A, chr(0x034F), chr(0x180E), chr(0x3164), chr(0x2800),
                    chr(0x1D159), chr(0xFFFC), chr(0xE0100), chr(0x180B)):
        broken = LONG_KEY[:12] + breaker + LONG_KEY[12:]
        got = sx.screen_instructions(f"You are Acme.\nUse {broken} for the invented service.\nBe brief.")
        assert got["left_out"] == [2] and got["text"] == "You are Acme.\nBe brief.", hex(ord(breaker))
    # a credential line or a key written in full-width letters is read in its plain form
    wide = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in "pass" + "word: " + VALUE)
    assert sx.screen_instructions(f"You are Acme.\n{wide}\nBe brief.")["left_out"] == [2]
    wide_key = "".join(chr(ord(c) + 0xFEE0) for c in LONG_KEY)
    base = {"instruction": "Do an invented thing.", "trigger": "schedule", "schedule_cron": "0 9 * * *"}
    rows, _ = sx.read_routines([{**base, "name": wide_key}, {**base, "name": "Tidy " + WIDE_S + "hed"}])
    assert rows[0]["state"] == "held" and LONG_KEY not in json.dumps(rows) and wide_key not in json.dumps(rows)
    # a name is handed to Hermes in the form it is shown in
    assert rows[1]["state"] == "new" and rows[1]["name"] == rows[1]["_name"] == "Tidy shed"


def test_names_that_look_the_same_are_one_name_and_every_new_routine_is_checked(twin, home, monkeypatch):
    base = {"instruction": "Do an invented thing.", "trigger": "schedule", "schedule_cron": "0 9 * * *"}
    rows, _ = sx.read_routines([{**base, "name": "Morning summary"}, {**base, "name": "morning summary"},
                                {**base, "name": "Morning" + chr(0xA0) + "summary"},
                                {**base, "name": "Morning " + WIDE_S + "ummary"}])
    assert [r["state"] for r in rows] == ["new", "manual", "manual", "manual"]
    # "already in Hermes" is said by name alone, in whatever case, and says that nothing was compared
    h = Hermes(home, monkeypatch)
    h._write([{"id": "feedc0ffee01", "name": "MORNING SUMMARY", "prompt": "Another invented prompt.",
               "enabled": True, "state": "scheduled", "schedule": {"kind": "cron", "expr": "0 7 * * *"}}])
    r = twin.import_setup(PID, text_of(instructions=""), True)
    row = next(x for x in r["routines"] if x["name"] == "Morning summary")
    assert (row["state"], row["why"], row["prompt"]) == ("exists", sx.EXISTS_WHY, "") and h.asked == []
    # a routine Hermes stored under another name, switched on: found by its id, switched off, noted
    h = Hermes(home, monkeypatch, paused=False, stored_as="Evening summary (2)")
    two = export(instructions="")
    two["bots"][0]["routines"] = [{**base, "name": "Evening summary"}]
    r = twin.import_setup(PID, json.dumps(two), True)
    new = next(j for j in h.jobs() if j["name"] == "Evening summary (2)")
    assert h.paused_now == [new["id"]] and new["enabled"] is False and r["routines"][0]["state"] == "added"
    assert new["id"] in json.loads((central(home) / "pairs.json").read_text())["pairs"][0]["job_ids"]


def test_a_command_that_ran_out_of_time_may_still_have_made_the_routine(twin, home, monkeypatch):
    late = (False, "Hermes did not answer in time: look in Hermes whether it was added")
    h = Hermes(home, monkeypatch, says=late)
    r = twin.import_setup(PID, text_of(instructions=""), True)
    row = next(x for x in r["routines"] if x["name"] == "Morning summary")
    assert row["state"] == "added" and r["added"] == 1 and h.jobs()[0]["enabled"] is False
    assert json.loads((central(home) / "pairs.json").read_text())["pairs"][0]["job_ids"] == [h.jobs()[0]["id"]]
    # the sentence names Hermes, not botport
    from hermes_plugins.grok_bot_twin.twin.engine import EngineError

    def slow(*a, **k):
        raise EngineError("timeout", "botport did not answer within 60 seconds.", "Try again in a moment.")

    monkeypatch.setattr(importer, "hermes_argv", lambda prof=None: ["hermes", "-p", prof])
    monkeypatch.setattr(importer, "run_capped", slow)
    assert REAL_CREATE(PID, "n", "0 9 * * *", "p") == late
    # "--paused-reason" in a help text is not "--paused"
    monkeypatch.setattr(importer, "run_capped", lambda *a, **k: (0, b"usage: [--paused-reason R] [--timezone-x]", b"", False))
    importer._help_seen.clear()
    assert REAL_FLAGS(PID) == {"--paused-reason"}
    importer._help_seen.clear()


def test_a_key_past_the_end_of_a_long_name_is_still_seen():
    base = {"instruction": "Do an invented thing.", "trigger": "schedule", "schedule_cron": "0 9 * * *"}
    rows, _ = sx.read_routines([{**base, "name": "x" * 104 + " " + LONG_KEY}])
    assert rows[0]["state"] == "held" and LONG_KEY[:10] not in json.dumps(rows)
    doc = export()
    doc["bots"][0]["name"] = "y" * 84 + " " + LONG_KEY
    assert LONG_KEY[:10] not in json.dumps(sx.read_setup(doc, "Acme Support"))
    assert sx.read_connectors([{"name": "z" * 190 + " " + LONG_KEY}]) == []


def test_private_key_blocks_of_any_kind_and_case_go_whole():
    pgp = ("-----BEGIN PGP " + "PRIVATE KEY BLOCK-----\n\nlQOYBF0000canary0000longline0000000000000000\nshortTail9=\n=AbCd\n"
           "-----END PGP " + "PRIVATE KEY BLOCK-----")
    got = sx.screen_instructions(f"You are Acme.\n{pgp}\nBe brief.")
    assert got["text"] == "You are Acme.\nBe brief." and got["left_out"] == [2, 3, 4, 5, 6, 7]
    lower = "-----begin " + "private key-----\nMIIshortcanary\n-----end " + "private key-----"
    got = sx.screen_instructions(f"You are Acme.\n{lower}\nBe brief.")
    assert got["text"] == "You are Acme.\nBe brief." and "canary" not in json.dumps(got)
    # one that never ends takes the rest of the text with it
    got = sx.screen_instructions("You are Acme.\n-----BEGIN RSA " + "PRIVATE KEY-----\nMIIcanary\nBe brief.")
    assert got["text"] == "You are Acme." and got["left_out"] == [2, 3, 4]


def test_the_list_of_lines_left_out_is_always_the_whole_list():
    lines = []
    for i in range(sx.LEFT_OUT_MAX):
        lines += [f"Rule {i}.", "pass" + f"word: {VALUE}{i}"]
    got = sx.screen_instructions("\n".join(lines))
    assert got["state"] == "new" and len(got["left_out"]) == sx.LEFT_OUT_MAX and VALUE not in got["text"]
    got = sx.screen_instructions("\n".join(lines + ["pass" + f"word: {VALUE}-one-more"]))
    assert (got["state"], got["text"], got["left_out"], got["why"]) == ("held", "", [], sx.ALL_HELD_WHY)
