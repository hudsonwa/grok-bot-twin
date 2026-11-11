"""Setting up again after a removal: after "Remove Grok Bot Twin" and a new setup,
twinning the same Grok bot made a second Hermes bot ("acme-helper-2") beside the old one. Against a real
botport build (skipped without BOTPORT_TEST_BIN), invented bots only.
"""
from __future__ import annotations

import os

import pytest

from test_service_real_botport import ACME, BIN, rig  # noqa: F401
from twin_testlib import home  # noqa: F401

# (the mark of the module the fixture comes from does not reach this one: without it these tests were not skipped
# but ended in an error when no botport build is given)
pytestmark = pytest.mark.skipif(not (BIN and os.path.isfile(BIN) and os.access(BIN, os.X_OK)),
                                reason="set BOTPORT_TEST_BIN to a botport build to run these")


def _profiles(home):  # noqa: F811
    root = home / "profiles"
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")) if root.is_dir() else []


def test_after_remove_and_a_new_setup_the_same_bot_gets_its_old_hermes_bot_back(rig):  # noqa: F811
    svc, home = rig["svc"], rig["home"]  # noqa: F811
    svc.trust_botport(True)
    first = svc.create([ACME])["pairs"][0]
    prof = first["hermes"]["profile"]
    soul = home / "profiles" / prof / "SOUL.md"
    svc.set_instructions(first["pair_id"], "You are Acme Helper. Keep answers short.")
    (home / "profiles" / prof / "sessions").mkdir(exist_ok=True)
    (home / "profiles" / prof / "sessions" / "a-chat.txt").write_text("the owner's chat", encoding="utf-8")
    assert _profiles(home) == [prof]
    # Remove Grok Bot Twin, then set up again
    svc.uninstall(False)
    assert _profiles(home) == [prof] and "Keep answers short." in soul.read_text(encoding="utf-8")
    svc.trust_botport(True)
    plan = svc.preview([ACME])
    assert [p["hermes"]["profile"] for p in plan["pairs"]] == [prof]  # the sheet shows the bot that is already here
    again = svc.create([ACME])["pairs"][0]
    assert again["hermes"]["profile"] == prof and _profiles(home) == [prof]  # no second Hermes bot
    assert "Keep answers short." in soul.read_text(encoding="utf-8")
    assert (home / "profiles" / prof / "sessions" / "a-chat.txt").read_text(encoding="utf-8") == "the owner's chat"
    assert svc.status(fresh=True)["pairs"][0]["state"] in ("in_step", "changes_waiting")


def test_a_hermes_bot_of_the_owners_with_that_name_is_never_taken_over(rig):  # noqa: F811
    svc, home = rig["svc"], rig["home"]  # noqa: F811
    svc.trust_botport(True)
    first = svc.create([ACME])["pairs"][0]
    prof = first["hermes"]["profile"]
    svc.uninstall(False)
    soul = home / "profiles" / prof / "SOUL.md"
    # the owner made the old bot their own: botport's name block is gone (or names something else)
    soul.write_text("My own helper. Nothing to do with Grok.\n", encoding="utf-8")
    before = soul.read_bytes()
    svc.trust_botport(True)
    again = svc.create([ACME])["pairs"][0]
    assert again["hermes"]["profile"] != prof and soul.read_bytes() == before
    assert sorted(_profiles(home)) == sorted([prof, again["hermes"]["profile"]])


def test_a_bot_renamed_in_grok_while_twinned_gets_its_old_hermes_bot_back_too(rig):  # noqa: F811
    # (after Remove and a new setup the renamed bot got a second Hermes bot under its new name, and the old one,
    # with its chats and pasted instructions, was left behind under the old name)
    svc, home, world = rig["svc"], rig["home"], rig["world"]  # noqa: F811
    svc.trust_botport(True)
    first = svc.create([ACME])["pairs"][0]
    prof = first["hermes"]["profile"]
    svc.set_instructions(first["pair_id"], "You are Acme Helper. Keep answers short.")
    world.step("rename")  # renamed in Grok; the twin follows, in the folder named after the old name
    svc.sync(first["pair_id"])
    soul = home / "profiles" / prof / "SOUL.md"
    new_name = svc.status(fresh=True)["pairs"][0]["grok"]["name"]
    assert new_name != "Acme Helper" and new_name in soul.read_text(encoding="utf-8")
    svc.uninstall(False)
    assert _profiles(home) == [prof]
    svc.trust_botport(True)
    assert [p["hermes"]["profile"] for p in svc.preview([ACME])["pairs"]] == [prof]
    again = svc.create([ACME])["pairs"][0]
    assert again["hermes"]["profile"] == prof and _profiles(home) == [prof]  # no second Hermes bot
    assert "Keep answers short." in soul.read_text(encoding="utf-8")


def test_a_bot_with_that_name_that_never_was_a_twin_is_left_alone_after_a_rename(rig):  # noqa: F811
    svc, home, world = rig["svc"], rig["home"], rig["world"]  # noqa: F811
    world.step("rename")
    svc.trust_botport(True)
    name = svc._grok_name(svc.botport(), ACME)
    assert name and name != "Acme Helper"
    own = home / "profiles" / "my-notes"
    own.mkdir(parents=True)
    # the owner's own bot happens to carry a block with that name, but botport never kept a twin in it
    text = f"<!-- botport:begin persona-name -->\n{name}\n<!-- botport:end persona-name -->\nMine.\n"
    (own / "SOUL.md").write_text(text, encoding="utf-8")
    made = svc.create([ACME])["pairs"][0]
    assert made["hermes"]["profile"] != "my-notes" and (own / "SOUL.md").read_text(encoding="utf-8") == text
