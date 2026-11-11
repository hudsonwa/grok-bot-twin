"""What copies over: one table (twin/copies.py), and every place that repeats it says the same.

The README and the page hold written copies of the table; the manifest, the disclosure and a few README
sentences say parts of it in prose. These tests fail when one of them drifts, and when work that a row waits
for is merged they name the sentences that have to change.
"""
from __future__ import annotations

import re
import subprocess
import sys

import pytest

from twin_testlib import ROOT  # noqa: F401  (also puts the plugin on the import path)
from hermes_plugins.grok_bot_twin.twin import copies

README = (ROOT / "README.md").read_text(encoding="utf-8")
PAGE = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
DISCLOSURE = (ROOT / "docs" / "DISCLOSURE.md").read_text(encoding="utf-8")
MANIFEST = " ".join((ROOT / "plugin.yaml").read_text(encoding="utf-8").split())

LANES = ("xs-export", "xs-skills", "xs-routines", "xs-memory", "xs-config")
FLEET = ("xs-skills", "xs-routines", "xs-memory", "xs-config")

# Words the page never shows (the checklist's list of engine words), as whole words.
ENGINE_WORDS = ("pair", "pack", "attestation", "attested", "parity", "mirror", "restore point", "coverage", "helper",
                "key file", "slug", "id", "token", "tombstone", "env", "config", "yaml", "json", "daemon", "poll",
                "endpoint", "port", "host", "server", "client", "node", "peer", "primary", "secondary", "replica",
                "follower", "source", "payload", "machine", "connector", "connectors", "not carried", "by hand")


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_readme_and_the_page_hold_the_table_as_the_one_file_writes_it():
    for path, want in copies.written(ROOT).items():
        assert want is not None, f"{path.name}: the what-copies marks are missing"
        assert want == path.read_text(encoding="utf-8"), \
            f"{path.name} differs from twin/copies.py: run `python3 twin/copies.py --write`"
    done = subprocess.run([sys.executable, str(ROOT / "twin" / "copies.py"), "--check"], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_readme_has_one_table_of_what_copies_and_the_page_one_list():
    assert README.count(copies.README_BEGIN) == 1 and PAGE.count(copies.PAGE_BEGIN) == 1
    assert README.count("| What | How |") == 1
    # the older three-column table is gone: two tables could say two things
    assert "| Item | Grok → Hermes | Hermes → Grok |" not in README
    assert PAGE.count("const WHAT_COPIES = [") == 1
    for group in copies.table():
        assert f"| **{group['title']}** | |" in README
        for row in group["rows"]:
            assert f"| {row['what']} | {row['how']} |" in README
            assert row["what"] in PAGE and (row["how"] in PAGE or row["how"].replace("'", "\\'") in PAGE
                                            or row["how"].replace('"', '\\"') in PAGE)


def test_every_row_is_complete_and_named_once():
    keys = [r["key"] for r in copies.rows()]
    assert len(keys) == len(set(keys)) == len(copies._ROWS)
    assert [g for g, _ in copies.GROUPS] == ["grok", "back", "computers"]
    for group in copies.table():
        whats = [r["what"] for r in group["rows"]]
        assert whats and len(whats) == len(set(whats)), group["title"]
    for key, group, what, how, needs, until in copies._ROWS:
        assert group in dict(copies.GROUPS) and what and how.endswith(".")
        assert (needs is None) == (until is None), key
        assert needs is None or needs in LANES, key
    assert set(copies.PENDING) <= set(LANES)


def test_what_the_task_names_is_in_the_table():
    by = {r["key"]: r for r in copies.rows(())}  # as it reads once everything is merged
    assert by["name"]["what"] == "Name and description" and by["name"]["how"].startswith("By itself, in seconds")
    assert by["setup"]["what"] == "Instructions, routines and connections"
    assert by["setup"]["how"].startswith("With Import setup export") and "Logins are never copied." in by["setup"]["how"]
    assert by["skills"]["how"].startswith("With skill sync, which is optional")
    assert [by[k]["what"] for k in ("fleet-skills", "fleet-routines", "fleet-memory", "fleet-settings")] == \
        ["Skills", "Routines", "Memory", "Settings"]
    assert not any(r["pending"] for r in by.values())
    assert not any(copies.NOT_YET in r["how"] or "not in this version" in r["how"] for r in by.values())


def test_grok_memory_and_the_grok_model_setting_do_not_copy_and_say_why_in_one_sentence():
    for pending in (None, ()):
        by = {r["key"]: r for r in copies.rows(pending)}
        for key, what in (("grok-memory", "Grok memory"), ("grok-model", "Grok model setting")):
            row = by[key]
            assert row["what"] == what and row["pending"] is False
            assert row["how"].startswith("Does not copy: ") and ", so " in row["how"]
            assert row["how"].count(".") == 1 and row["how"].endswith("."), row["how"]  # one sentence
            assert len(row["how"].split()) <= 26, row["how"]
    # the same two facts where a reader meets them first
    for text, name in ((README, "README"), (DISCLOSURE, "disclosure")):
        flat = _flat(text)
        assert "Grok memory" in flat and "Grok model setting" in flat, name
        assert "memory is the most private thing a bot holds" in flat, name
        assert "nothing Hermes reads from Grok names the bot's model" in flat, name
    assert "a bot's Grok memory and Grok model setting are not copied" in MANIFEST


@pytest.mark.parametrize("lane", LANES)
def test_a_row_that_waits_says_so_and_stops_saying_so_when_its_work_is_merged(lane):
    waiting = [r for r in copies.rows((lane,)) if r["pending"]]
    assert waiting, lane
    for row in waiting:
        assert "ot in this version yet" in row["how"], row
    merged = {r["key"]: r for r in copies.rows(tuple(x for x in LANES if x != lane))}
    for row in waiting:
        after = merged[row["key"]]
        assert after["pending"] is False and "this version" not in after["how"]
        assert after["how"] != row["how"]
    # only the rows that name this work change
    assert sum(1 for r in copies.rows(()) if r["how"] != {x["key"]: x for x in copies.rows((lane,))}[r["key"]]["how"]) \
        == len(waiting)


def test_the_table_uses_the_pages_words_only():
    for pending in (None, (), LANES):
        for row in copies.rows(pending):
            for text in (row["what"], row["how"]):
                low = text.lower()
                for word in ENGINE_WORDS:
                    assert not re.search(rf"\b{re.escape(word)}\b", low), f"{word!r} in {text!r}"
                assert "(" not in text and "!" not in text
                for sentence in re.split(r"(?<=[.])\s+", text):
                    assert len(sentence.split()) <= 26, sentence
    for _, title in copies.GROUPS:
        assert not re.search(r"\b(machine|coverage|mirror)\b", title.lower())


# Sentences outside the table that are true only while the named work is not merged. While all of it waits, each
# must stand (the docs say what this version does); once any of it is merged, each must be gone or reworded.
ONLY_WHILE_WAITING = (
    ("README.md", "Skills, routines, memory and settings are not copied between your computers in this version",
     FLEET),
    ("README.md", "skills, routines, connections, memory and settings are not copied there in this version", FLEET),
    ("README.md", "| Chats, instructions, skills, routines, connections, memory, files, model keys, logins and "
                  "passwords.", FLEET),
    ("docs/DISCLOSURE.md", "Skills, routines, memory and settings are not copied between computers in this version",
     FLEET),
    ("docs/DISCLOSURE.md", "No instructions, skills, routines, connections, chats, memory, model settings,", FLEET),
    ("docs/DISCLOSURE.md", "Importing them from the bot's setup export is not in this version.", ("xs-export",)),
    ("README.md", 'Where a row says "Not in this version yet"', LANES),
    # the roadmap and the README's pointer to it call these "next": not once they are here
    ("README.md", "Skills a twin learned, its tools and connectors, your chats", ("xs-skills", "xs-config")),
    ("docs/ROADMAP.md", "Next: the skills it picked up in Hermes follow too", ("xs-skills",)),
    ("docs/ROADMAP.md", "**The same tools and connectors everywhere.**", ("xs-config",)),
    ("README.md", "Instructions are not copied to a linked computer", ("xs-config",)),
    ("docs/DISCLOSURE.md", "and neither are instructions (on a linked computer you add them to the Hermes bot "
                           "yourself)", ("xs-config",)),
)


@pytest.mark.parametrize("rel,phrase,lanes", ONLY_WHILE_WAITING)
def test_prose_about_what_is_not_copied_yet_follows_the_table(rel, phrase, lanes):
    flat = _flat((ROOT / rel).read_text(encoding="utf-8"))
    merged = [x for x in lanes if x not in copies.PENDING]
    if merged:
        assert phrase not in flat, (f"{rel} still says {phrase!r}, but {', '.join(merged)} is merged: "
                                    "reword it to what is true now")
    else:
        assert phrase in flat, f"{rel} no longer says {phrase!r}: move or reword the entry in this list with it"


def test_the_table_prints_in_a_terminal():
    out = copies.lines()
    assert out[0] == "From Grok to Hermes:" and "  Name and description: By itself, in seconds, while Hermes is open." in out
    assert sum(1 for line in out if not line.startswith("  ")) == 3
    done = subprocess.run([sys.executable, str(ROOT / "twin" / "copies.py")], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0 and done.stdout.splitlines() == out
    assert subprocess.run([sys.executable, str(ROOT / "twin" / "copies.py"), "--nope"], capture_output=True,
                          timeout=60).returncode == 2


def test_writing_is_refused_without_the_marks_and_changes_nothing_else(tmp_path):
    for rel in ("README.md", "desktop/plugin.js"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text((ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    # as written now: nothing to do
    assert all(want == path.read_text(encoding="utf-8") for path, want in copies.written(tmp_path).items())
    # the other table (everything merged while something waits; every lane waiting once all are merged, as now):
    # only what stands between the marks differs
    other = () if copies.PENDING else LANES
    for path, want in copies.written(tmp_path, other).items():
        now = path.read_text(encoding="utf-8")
        begin, end = ((copies.README_BEGIN, copies.README_END) if path.name == "README.md"
                      else (copies.PAGE_BEGIN, copies.PAGE_END))
        assert want is not None and want != now
        assert want[:want.index(begin)] == now[:now.index(begin)]
        assert want[want.index(end):] == now[now.index(end):]
        merged = want if other == () else now
        waiting = now if other == () else want
        assert "this version" not in merged[merged.index(begin):merged.index(end)]
        assert "this version" in waiting[waiting.index(begin):waiting.index(end)]
    # the writer writes bytes, so a README written on Windows has the same line ends as one written on a Mac
    src = (ROOT / "twin" / "copies.py").read_text(encoding="utf-8")
    assert "write_text(" not in src and 'path.write_bytes(want.encode("utf-8"))' in src
    # a file without its marks is left alone and reported
    (tmp_path / "README.md").write_text("no marks here\n", encoding="utf-8")
    assert copies.written(tmp_path)[tmp_path / "README.md"] is None


def test_the_terminal_and_the_chat_tool_say_what_the_table_says(capsys):
    import json

    from hermes_plugins.grok_bot_twin.twin import cli

    assert cli.main(["copies"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "What copies over, and how:"
    assert [line for line in out.splitlines()[1:] if line] == copies.lines()
    assert out.count("\n\nFrom ") + out.count("\n\nBetween ") == 3  # each part under its own heading
    assert cli.main(["copies", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"table": copies.table()}
    # the overview names it in the page's words
    assert "\n  copies                 what copies over and how: the whole table, as on the Twins page\n" in cli.__doc__ \
        or any("copies                 what copies over" in str(v) for v in vars(cli).values() if isinstance(v, str))
    # a chat can answer "does its memory come along?": fixed words, no name, the same for every twin
    assert copies.never() == ["Grok memory", "Grok model setting"]
    tools_src = (ROOT / "twin" / "tools.py").read_text(encoding="utf-8")
    assert '"never_copied_from_grok": copies.never()' in tools_src
    assert '"Grok memory", "Grok model setting"' in _flat(DISCLOSURE)
    assert "`hermes twin copies`" in README and "`hermes twin copies`" in DISCLOSURE and "`copies`, `checklist`" in README


def test_the_backend_never_says_instructions_copy_from_grok_or_that_a_routine_copy_arrives_when_none_does():
    from hermes_plugins.grok_bot_twin.twin import model

    # an older botport listed instructions under skill sync: the page, the terminal and the chat tool read this
    # list, and none of them may promise it
    rows = model.clean_coverage([{"item": "instructions", "g2h": "syncs_via_mirror", "h2g": "by_hand"},
                                 {"item": "instructions", "g2h": "syncs", "h2g": "syncs"},
                                 {"item": "skills", "g2h": "syncs_via_mirror", "h2g": "syncs_via_mirror"}])
    assert [r["g2h"] for r in rows] == ["by_hand", "by_hand", "syncs_via_mirror"]
    assert all(r["g2h"] == "by_hand" for r in model.coverage(True) + model.coverage(False) if r["item"] == "instructions")
    # the add-twin preview: a routine's note promises a switched-off copy only when one arrives
    assert model.preview_note("routine", "by_hand") == "arrives switched off, so nothing runs twice"
    assert model.preview_note("routine", "by_hand", copies=False) == \
        "Grok keeps its schedule: make it again in Hermes yourself"
    assert model.preview_note("connector", "by_hand", copies=False) == "logins are never copied"
    assert model.preview_note("routine", "create", copies=False) is None
    src = (ROOT / "twin" / "service.py").read_text(encoding="utf-8")
    assert 'copies = eng is self.demo or _n(p.get("schedules_paused")) > 0' in src
    assert 'preview_note(it.get("kind"), it.get("action"), copies)' in src
    # the page's own words for it, where no copy arrived
    assert "No routine arrived to switch on: Grok keeps their schedules. Make each routine again in Hermes." in PAGE
    assert "Grok gives no way to copy these" not in PAGE
