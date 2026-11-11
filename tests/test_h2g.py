"""Hermes -> Grok (0.3.0 items 3, 4, 6): the planner in twin/h2g.py against the pretend Grok account (FakeGrokWriter).

Invented bots only. The Hermes side is a plain dict the test edits; the owner's direction, the Hermes cron pause and
resume are plain functions that log their calls, so the order of a routine handover can be checked.
"""
from __future__ import annotations

import json

import pytest

from twin_testlib import plugin  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import h2g

BOT = "bot-acme"
PID = "acme-helper"
ROUTINE = {"name": "Morning digest", "schedule": "0 9 * * *", "prompt": "Say good morning.", "enabled": True}


def fake_key() -> str:
    # key-shaped but meaningless: built at run time so the file itself holds no token-shaped text
    return "sk" + "-" + "Qz" * 20


class World:
    def __init__(self, tmp_path, writer=None, wanted=None, pause_ok=True, resume_ok=True, routines=None):
        self.calls: list = []
        self.writer = writer or h2g.FakeGrokWriter()
        self.writer.put_bot(BOT, name="Acme Helper", description="Helps with invented tasks.",
                            instructions="Be kind.\nAnswer briefly.", routines=routines or [])
        self.hermes = {"name": "Acme Helper", "description": "Helps with invented tasks.",
                       "instructions": "Be kind.\nAnswer briefly.", "routines": list(routines or [])}
        self.pause_ok, self.resume_ok = pause_ok, resume_ok
        self.marked: list = []
        orig_apply = self.writer.apply

        def logged_apply(bot, ops, cid):
            self.calls.append(("grok", [(o["op"], o.get("enabled")) for o in ops]))
            return orig_apply(bot, ops, cid)
        self.writer.apply = logged_apply
        self.x = h2g.H2G(tmp_path / "central", self.writer, hermes=lambda p: self.hermes, bot=lambda p: BOT,
                         wanted=wanted, pause_hermes=self._pause, resume_hermes=self._resume,
                         mark_synced=lambda p, item: self.marked.append(item))
        (tmp_path / "central").mkdir()

    def _pause(self, pid, key):
        self.calls.append(("hermes.pause", key))
        return self.pause_ok

    def _resume(self, pid, key):
        self.calls.append(("hermes.resume", key))
        return self.resume_ok

    def grok(self):
        return self.writer.read(BOT)


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    world.x.allow(PID, "all")
    return world


def apply_now(world):
    p = world.x.preview(PID)
    return p, world.x.apply(PID, p["change_id"])


# ---- rule 1: allowlist -----------------------------------------------------------------------------------------------

def test_nothing_is_allowed_until_the_owner_allows_it(tmp_path):
    world = World(tmp_path)
    assert world.x.allowed(PID) == []
    world.hermes["name"] = "Acme Helper Pro"
    with pytest.raises(h2g.H2GError) as e:
        world.x.preview(PID)
    assert e.value.code == "not_allowed"
    assert world.grok()["name"] == "Acme Helper"


def test_allowlist_is_per_item(tmp_path):
    world = World(tmp_path)
    world.x.allow(PID, ["description"])
    world.hermes["name"] = "Acme Helper Pro"
    world.hermes["description"] = "Helps with more invented tasks."
    p = world.x.preview(PID)
    assert [c["item"] for c in p["changes"]] == ["description"]
    world.x.apply(PID, p["change_id"])
    assert world.grok()["name"] == "Acme Helper" and world.grok()["description"] == "Helps with more invented tasks."
    world.x.disallow(PID, ["description"])
    assert world.x.allowed(PID) == []


def test_allow_refuses_unknown_items_and_unknown_bot(tmp_path):
    world = World(tmp_path)
    with pytest.raises(h2g.H2GError):
        world.x.allow(PID, ["memory"])
    x = h2g.H2G(tmp_path / "central", world.writer, hermes=lambda p: world.hermes, bot=lambda p: "")
    with pytest.raises(h2g.H2GError) as e:
        x.allow(PID, "all")
    assert e.value.code == "no_bot"


# ---- rule 2: the owner's direction -----------------------------------------------------------------------------------

@pytest.mark.parametrize("direction,sent", [("grok_to_hermes", False), ("off", False), (None, False),
                                            ("hermes_to_grok", True), ("both", True)])
def test_direction_gates_each_item(tmp_path, direction, sent):
    world = World(tmp_path, wanted=lambda pid, item: direction)
    world.x.allow(PID, "all")
    world.hermes["name"] = "Acme Helper Pro"
    p = world.x.preview(PID)
    assert bool(p["changes"]) is sent


# ---- rule 3: detection, preview and diff -----------------------------------------------------------------------------

def test_nothing_to_send_when_they_match(w):
    p = w.x.preview(PID)
    assert p["nothing"] is True and p["sentence"] == h2g.NOTHING
    assert w.x.apply(PID, p["change_id"])["state"] == "done"
    assert w.calls == []


def test_instructions_change_shows_a_line_diff_and_is_made_in_grok(w):
    w.hermes["instructions"] = "Be kind.\nAnswer in two sentences."
    p, r = apply_now(w)
    ch = next(c for c in p["changes"] if c["item"] == "instructions")
    assert "-Answer briefly." in ch["diff"] and "+Answer in two sentences." in ch["diff"]
    assert ch["before"] is None and ch["after"] is None  # the text itself only as a diff
    assert r["state"] == "done"
    assert w.grok()["instructions"] == "Be kind.\nAnswer in two sentences."
    assert "instructions" in w.marked
    assert w.x.preview(PID)["nothing"] is True


def test_name_change_shows_before_and_after(w):
    w.hermes["name"] = "Acme Helper Pro"
    p = w.x.preview(PID)
    assert p["changes"] == [{"item": "name", "before": "Acme Helper", "after": "Acme Helper Pro"}]


def test_change_made_in_grok_by_hand_already_is_not_sent_again(w):
    w.hermes["name"] = "Acme Helper Pro"
    w.writer.edit_in_grok(BOT, name="Acme Helper Pro")
    p = w.x.preview(PID)
    assert p["changes"] == [] and p["held"] == []


# ---- rule 4: the change id -------------------------------------------------------------------------------------------

def test_apply_refuses_a_plan_that_changed_since_the_preview(w):
    w.hermes["name"] = "Acme Helper Pro"
    p = w.x.preview(PID)
    w.hermes["name"] = "Acme Helper Max"
    with pytest.raises(h2g.H2GError) as e:
        w.x.apply(PID, p["change_id"])
    assert e.value.code == "stale"
    assert w.grok()["name"] == "Acme Helper"


def test_apply_refuses_a_malformed_change_id(w):
    with pytest.raises(h2g.H2GError) as e:
        w.x.apply(PID, "c-; echo nope")
    assert e.value.code == "bad_input"


def test_no_permission_blocks_apply(tmp_path):
    world = World(tmp_path)
    world.x.allow(PID, "all")
    world.writer._mem["allowed"].remove(BOT)
    world.hermes["name"] = "Acme Helper Pro"
    p = world.x.preview(PID)
    assert p["permission"] == "no_permission" and "permission" in p["blocked"]
    with pytest.raises(h2g.H2GError) as e:
        world.x.apply(PID, p["change_id"])
    assert e.value.code == "no_permission"
    assert world.grok()["name"] == "Acme Helper" and world.calls == []


# ---- rule 5: secrets -------------------------------------------------------------------------------------------------

def test_secret_looking_text_is_held(w):
    w.hermes["instructions"] = "Be kind.\nUse " + fake_key() + " for the invented service."
    p = w.x.preview(PID)
    assert p["changes"] == []
    assert p["held"][0]["item"] == "instructions" and p["held"][0]["code"] == "secret"
    w.x.apply(PID, p["change_id"])
    assert "Qz" not in w.grok()["instructions"]


def test_secret_in_a_routine_prompt_is_held(w):
    w.hermes["routines"] = [dict(ROUTINE, prompt="Call with " + fake_key())]
    p = w.x.preview(PID)
    assert p["changes"] == [] and p["held"][0]["code"] == "secret"


# ---- conflicts and resolve -------------------------------------------------------------------------------------------

def test_conflict_is_held_when_both_sides_changed(w):
    w.writer.edit_in_grok(BOT, name="Acme Grok Edit")
    w.hermes["name"] = "Acme Hermes Edit"
    p = w.x.preview(PID)
    assert p["changes"] == [] and p["held"] == [{"item": "name", "code": "conflict",
                                                 "why": h2g.CONFLICT.format(item="name")}]


def test_resolve_keep_hermes_then_sends(w):
    w.writer.edit_in_grok(BOT, name="Acme Grok Edit")
    w.hermes["name"] = "Acme Hermes Edit"
    p = w.x.resolve(PID, "name", "hermes")
    assert p["held"] == [] and p["changes"] == [{"item": "name", "before": "Acme Grok Edit",
                                                 "after": "Acme Hermes Edit"}]
    w.x.apply(PID, p["change_id"])
    assert w.grok()["name"] == "Acme Hermes Edit"


def test_resolve_keep_grok_stops_asking_until_hermes_changes_again(w):
    w.writer.edit_in_grok(BOT, name="Acme Grok Edit")
    w.hermes["name"] = "Acme Hermes Edit"
    p = w.x.resolve(PID, "name", "grok")
    assert p["held"] == [] and p["changes"] == []
    assert w.grok()["name"] == "Acme Grok Edit"
    w.hermes["name"] = "Acme Hermes Second Edit"
    p = w.x.preview(PID)
    assert [c["item"] for c in p["changes"]] == ["name"]


def test_resolve_refuses_bad_words(w):
    with pytest.raises(h2g.H2GError):
        w.x.resolve(PID, "routines", "hermes")
    with pytest.raises(h2g.H2GError):
        w.x.resolve(PID, "name", "both")


# ---- rule 6: undo ----------------------------------------------------------------------------------------------------

def test_undo_puts_back_what_was_replaced(w):
    w.hermes["name"] = "Acme Helper Pro"
    w.hermes["instructions"] = "Be brief."
    _, r = apply_now(w)
    u = w.x.undo(PID, r["change_id"])
    assert u["state"] == "done"
    g = w.grok()
    assert g["name"] == "Acme Helper" and g["instructions"] == "Be kind.\nAnswer briefly."
    with pytest.raises(h2g.H2GError) as e:
        w.x.undo(PID, r["change_id"])
    assert e.value.code == "undone"
    info = w.x.info(PID)
    assert any(c["undo_of"] == r["change_id"] for c in info["changes"])


def test_undo_is_refused_when_grok_changed_since(w):
    w.hermes["name"] = "Acme Helper Pro"
    _, r = apply_now(w)
    w.writer.edit_in_grok(BOT, name="Edited In Grok")
    with pytest.raises(h2g.H2GError) as e:
        w.x.undo(PID, r["change_id"])
    assert e.value.code == "changed_in_grok"
    assert w.grok()["name"] == "Edited In Grok"


def test_undo_unknown_change(w):
    with pytest.raises(h2g.H2GError) as e:
        w.x.undo(PID, "c-0000000000000000")
    assert e.value.code == "no_change"


def test_undo_of_a_routine_ignores_on_off_and_keeps_the_runner(w):
    w.hermes["routines"] = [ROUTINE]
    _, r = apply_now(w)
    assert w.x.move_runner(PID, "morning-digest", "grok")["runner"] == "grok"
    assert w.grok()["routines"][0]["enabled"] is True
    u = w.x.undo(PID, r["change_id"])  # the routine did not exist before: undo removes it
    assert u["state"] == "done"
    assert w.grok()["routines"] == []


# ---- rule 7: routines run in one place -------------------------------------------------------------------------------

def test_a_routine_arrives_paused_in_grok_while_hermes_runs_it(w):
    w.hermes["routines"] = [ROUTINE]
    p, r = apply_now(w)
    assert p["changes"][0]["item"] == "routine:morning-digest"
    g = w.grok()["routines"]
    assert len(g) == 1 and g[0]["enabled"] is False and g[0]["prompt"] == "Say good morning."
    assert w.x.runner(PID, "morning-digest") == "hermes"
    assert w.x.preview(PID)["nothing"] is True


def test_move_to_grok_pauses_hermes_first(w):
    w.hermes["routines"] = [ROUTINE]
    apply_now(w)
    w.calls.clear()
    out = w.x.move_runner(PID, "morning-digest", "grok")
    assert out["runner"] == "grok"
    assert w.calls == [("hermes.pause", "morning-digest"), ("grok", [("routine.enable", True)])]
    assert w.grok()["routines"][0]["enabled"] is True
    assert w.x.preview(PID)["nothing"] is True  # base and want agree: enabled in Grok because Grok runs it


def test_move_to_grok_refused_when_hermes_cannot_pause(tmp_path):
    world = World(tmp_path, pause_ok=False)
    world.x.allow(PID, "all")
    world.hermes["routines"] = [ROUTINE]
    apply_now(world)
    world.calls.clear()
    with pytest.raises(h2g.H2GError) as e:
        world.x.move_runner(PID, "morning-digest", "grok")
    assert e.value.code == "hermes_not_paused"
    assert world.calls == [("hermes.pause", "morning-digest")]  # nothing sent to Grok
    assert world.grok()["routines"][0]["enabled"] is False
    assert world.x.runner(PID, "morning-digest") == "hermes"


def test_failed_enable_leaves_it_running_nowhere_never_twice(tmp_path):
    world = World(tmp_path, writer=h2g.FakeGrokWriter(fail_ops=("routine.enable",)))
    world.x.allow(PID, "all")
    world.hermes["routines"] = [ROUTINE]
    apply_now(world)
    world.calls.clear()
    out = world.x.move_runner(PID, "morning-digest", "grok")
    assert out["state"] == "failed" and out["runner"] == "nowhere"
    assert world.x.runner(PID, "morning-digest") == "nowhere"
    assert ("hermes.resume", "morning-digest") not in world.calls
    assert world.grok()["routines"][0]["enabled"] is False


def test_move_back_to_hermes_pauses_grok_before_resuming(w):
    w.hermes["routines"] = [ROUTINE]
    apply_now(w)
    w.x.move_runner(PID, "morning-digest", "grok")
    w.calls.clear()
    out = w.x.move_runner(PID, "morning-digest", "hermes")
    assert out["runner"] == "hermes"
    assert w.calls == [("grok", [("routine.enable", False)]), ("hermes.resume", "morning-digest")]
    assert w.grok()["routines"][0]["enabled"] is False


def test_move_needs_the_routine_in_grok_and_the_routines_item(tmp_path):
    world = World(tmp_path)
    world.x.allow(PID, ["name"])
    with pytest.raises(h2g.H2GError) as e:
        world.x.move_runner(PID, "morning-digest", "grok")
    assert e.value.code == "not_allowed"
    world.x.allow(PID, "all")
    with pytest.raises(h2g.H2GError) as e:
        world.x.move_runner(PID, "morning-digest", "grok")
    assert e.value.code == "not_in_grok"
    with pytest.raises(h2g.H2GError):
        world.x.move_runner(PID, "Morning Digest!", "both")


def test_removed_routine_is_removed_in_grok(w):
    other = dict(ROUTINE, name="Evening digest", schedule="0 18 * * *")
    w.hermes["routines"] = [ROUTINE, other]
    apply_now(w)
    w.hermes["routines"] = [other]
    p, _ = apply_now(w)
    assert p["changes"][0]["after"] is None
    assert [r["name"] for r in w.grok()["routines"]] == ["Evening digest"]


def test_no_routines_in_hermes_never_empties_grok(w):
    w.hermes["routines"] = [ROUTINE]
    apply_now(w)
    w.hermes["routines"] = []
    p = w.x.preview(PID)
    assert p["nothing"] and p["held"][0]["code"] == "empty"
    assert len(w.grok()["routines"]) == 1


# ---- the Mirror answers later (delay) --------------------------------------------------------------------------------

def test_sent_then_poll_finishes(tmp_path):
    world = World(tmp_path, writer=h2g.FakeGrokWriter(path=tmp_path / "fake.json", delay=True))
    world.x.allow(PID, "all")
    world.hermes["description"] = "Now with invented recipes."
    p, r = apply_now(world)
    assert r["state"] == "sent"
    assert world.grok()["description"] == "Helps with invented tasks."
    assert world.x.preview(PID)["change_id"] == p["change_id"]  # base not moved yet: the same plan
    assert world.x.poll() == [{"change_id": r["change_id"], "state": "done"}]
    assert world.grok()["description"] == "Now with invented recipes."
    assert world.x.preview(PID)["nothing"] is True
    assert "description" in world.marked


def test_delayed_move_completes_on_finish_moves(tmp_path):
    world = World(tmp_path, writer=h2g.FakeGrokWriter(path=tmp_path / "fake.json", delay=True))
    world.x.allow(PID, "all")
    world.hermes["routines"] = [ROUTINE]
    _, r = apply_now(world)
    world.x.poll()
    out = world.x.move_runner(PID, "morning-digest", "grok")
    assert out["state"] == "sent" and out["runner"] == "nowhere"
    assert world.grok()["routines"][0]["enabled"] is False
    done = world.x.finish_moves()
    assert done and done[0]["runner"] == "grok"
    assert world.x.runner(PID, "morning-digest") == "grok"
    assert world.grok()["routines"][0]["enabled"] is True
    # and back: Grok pauses (on the Mirror's next run) before Hermes resumes
    world.calls.clear()
    out = world.x.move_runner(PID, "morning-digest", "hermes")
    assert out["state"] == "sent" and ("hermes.resume", "morning-digest") not in world.calls
    world.x.finish_moves()
    assert world.calls[-1] == ("hermes.resume", "morning-digest")
    assert world.x.runner(PID, "morning-digest") == "hermes"


# ---- partial failure: the base moves only by what was done -----------------------------------------------------------

def test_base_moves_only_by_ops_done(tmp_path):
    world = World(tmp_path, writer=h2g.FakeGrokWriter(fail_ops=("routine.put",)))
    world.x.allow(PID, "all")
    world.hermes["name"] = "Acme Helper Pro"
    world.hermes["routines"] = [ROUTINE]
    _, r = apply_now(world)
    assert r["state"] == "failed"
    assert world.grok()["name"] == "Acme Helper Pro" and world.grok()["routines"] == []
    p = world.x.preview(PID)
    assert [c["item"] for c in p["changes"]] == ["routine:morning-digest"]  # the name is not sent again


def test_file_backed_fake_and_state_survive_a_new_planner(tmp_path):
    world = World(tmp_path, writer=h2g.FakeGrokWriter(path=tmp_path / "fake.json"))
    world.x.allow(PID, "all")
    world.hermes["name"] = "Acme Helper Pro"
    _, r = apply_now(world)
    x2 = h2g.H2G(tmp_path / "central", h2g.FakeGrokWriter(path=tmp_path / "fake.json"),
                 hermes=lambda p: world.hermes, bot=lambda p: BOT)
    assert x2.allowed(PID) == list(h2g.ITEMS)
    assert x2.preview(PID)["nothing"] is True
    assert x2.undo(PID, r["change_id"])["state"] == "done"
    assert h2g.FakeGrokWriter(path=tmp_path / "fake.json").read(BOT)["name"] == "Acme Helper"


# ---- nothing ever switches a routine on or off by itself ------------------------------------------------------------

def test_paused_in_both_places_stays_paused(tmp_path):
    paused = dict(ROUTINE, enabled=False)
    world = World(tmp_path, routines=[dict(paused, key="morning-digest")])
    world.x.allow(PID, "all")
    assert world.x.info(PID)["runner"] == {"morning-digest": "nowhere"}
    world.hermes["routines"] = [dict(paused, prompt="Say good morning, briefly.")]
    p, r = apply_now(world)
    assert r["state"] == "done"
    assert world.grok()["routines"][0]["enabled"] is False
    assert world.grok()["routines"][0]["prompt"] == "Say good morning, briefly."


def test_running_in_grok_keeps_running_when_hermes_copy_is_paused(tmp_path):
    world = World(tmp_path, routines=[dict(ROUTINE, key="morning-digest", enabled=True)])
    world.hermes["routines"] = [dict(ROUTINE, enabled=False)]  # the importer's paused copy
    world.x.allow(PID, "all")
    assert world.x.info(PID)["runner"] == {"morning-digest": "grok"}
    assert world.x.preview(PID)["nothing"] is True
    world.hermes["routines"] = [dict(ROUTINE, enabled=False, schedule="0 10 * * *")]
    apply_now(world)
    assert world.grok()["routines"][0] == dict(ROUTINE, key="morning-digest", enabled=True, schedule="0 10 * * *")


class _NoRoutineRead(h2g.FakeGrokWriter):
    def read(self, bot):
        g = super().read(bot)
        return None if g is None else dict(g, routines=None, instructions=None)


def test_unknown_in_grok_keeps_its_switch(tmp_path):
    world = World(tmp_path, writer=_NoRoutineRead(), routines=[dict(ROUTINE, key="morning-digest", enabled=True)])
    world.hermes["routines"] = [dict(ROUTINE, enabled=False)]
    world.x.allow(PID, "all")
    assert world.x.info(PID)["runner"] == {"morning-digest": "keep"}
    p, r = apply_now(world)  # Grok's routines are unknown: the first apply puts each one, keeping its on/off
    assert [c["item"] for c in p["changes"]] == ["routine:morning-digest"]
    assert world.calls[-1] == ("grok", [("routine.put", None)])
    assert h2g.FakeGrokWriter.read(world.writer, BOT)["routines"][0]["enabled"] is True
    assert world.x.preview(PID)["nothing"] is True


def test_a_change_the_owner_did_not_choose_to_send_is_named_not_hidden(tmp_path):
    world = World(tmp_path, wanted=lambda p, item: "grok_to_hermes")
    world.x.allow(PID, "all")
    world.hermes["name"] = "Acme Helper Pro"
    p = world.x.preview(PID)
    assert p["nothing"] and p["off"] == [{"item": "name", "why": h2g.NOT_WANTED.format(item="name")}]
    assert p["sentence"].startswith("Nothing is sent: The name is set to stay")


def test_one_change_at_a_time_while_the_mirror_has_not_answered(tmp_path):
    world = World(tmp_path, writer=h2g.FakeGrokWriter(tmp_path / "fake.json", delay=True))
    world.x.allow(PID, "all")
    world.hermes["name"] = "Acme Helper Pro"
    p, r = apply_now(world)
    assert r["state"] == "sent"
    again = world.x.preview(PID)
    assert again["pending"] == r["change_id"] and again["blocked"] == h2g.PENDING
    with pytest.raises(h2g.H2GError) as e:
        world.x.apply(PID, again["change_id"])
    assert e.value.code == "pending"
    sent = [c for c in world.calls if c[0] == "grok"]
    assert len(sent) == 1
    world.x.poll()
    assert world.x.preview(PID)["nothing"] is True


def test_applying_nothing_never_settles_a_held_conflict(w):
    w.writer.edit_in_grok(BOT, name="Grok edit")
    w.hermes["name"] = "Hermes edit"
    p = w.x.preview(PID)
    assert p["held"][0]["code"] == "conflict" and p["nothing"]
    w.x.apply(PID, p["change_id"])
    assert w.x.preview(PID)["held"][0]["code"] == "conflict"  # still held: Grok was not told anything


def test_undo_never_empties_a_text_in_grok(tmp_path):
    world = World(tmp_path)
    world.writer.edit_in_grok(BOT, description="")
    world.hermes["description"] = ""
    world.x.allow(PID, "all")
    world.hermes["description"] = "Now with a description."
    p, r = apply_now(world)
    assert world.grok()["description"] == "Now with a description."
    with pytest.raises(h2g.H2GError) as e:
        world.x.undo(PID, r["change_id"])
    assert e.value.code == "empty" and world.grok()["description"] == "Now with a description."


# ---- after the independent review ------------------------------------------------------------------------------------

class _NoTextRead(h2g.FakeGrokWriter):
    """Like the real Mirror: name and description can be read back, instructions cannot."""
    def read(self, bot):
        g = super().read(bot)
        return None if g is None else dict(g, instructions=None)


def test_unreadable_instructions_are_said_to_be_replaced_whole_and_never_undone_to_a_guess(tmp_path):
    world = World(tmp_path, writer=_NoTextRead())
    world.writer.edit_in_grok(BOT, instructions="What Grok really holds.")
    world.x.allow(PID, "all")  # Hermes cannot read Grok's instructions: its base is Hermes' text then
    world.hermes["instructions"] = "Be kind.\nAnswer briefly.\nOne more line."
    p = world.x.preview(PID)
    ch = p["changes"][0]
    assert ch["grok_unknown"] and "REPLACES" in ch["note"] and ch["diff"][0] == "--- Hermes when you allowed it"
    r = world.x.apply(PID, p["change_id"])
    with pytest.raises(h2g.H2GError) as e:
        world.x.undo(PID, r["change_id"])  # Hermes never knew "What Grok really holds.": no undo to a guess
    assert e.value.code == "empty" and e.value.message == h2g.UNKNOWN_BEFORE
    world.hermes["instructions"] = "Second version."
    p2 = world.x.preview(PID)
    assert p2["changes"][0]["diff"][0] == "--- Last sent to Grok"
    r2 = world.x.apply(PID, p2["change_id"])
    world.x.undo(PID, r2["change_id"])  # the text before was one Hermes sent: undo puts it back
    assert h2g.FakeGrokWriter.read(world.writer, BOT)["instructions"] == "Be kind.\nAnswer briefly.\nOne more line."


def test_a_move_is_not_undone_by_undo(w):
    w.hermes["routines"] = [dict(ROUTINE, enabled=True)]
    apply_now(w)
    m = w.x.move_runner(PID, "morning-digest", "grok")
    with pytest.raises(h2g.H2GError) as e:
        w.x.undo(PID, m["change_id"])
    assert e.value.code == "is_move"


def test_give_up_stops_waiting_and_keeps_the_safe_side(tmp_path):
    fake = h2g.FakeGrokWriter(tmp_path / "fake.json")
    world = World(tmp_path, writer=fake, routines=[dict(ROUTINE, key="morning-digest", enabled=False)])
    world.hermes["routines"] = [dict(ROUTINE, enabled=True)]
    world.x.allow(PID, "all")
    world.x.move_runner(PID, "morning-digest", "hermes")  # runs in Hermes already: nothing sent
    fake.delay = True
    m = world.x.move_runner(PID, "morning-digest", "grok")
    assert m["state"] == "sent" and world.calls[-2] == ("hermes.pause", "morning-digest")
    g = world.x.give_up(PID)
    assert g["gave_up"] == [m["change_id"]]
    assert world.x.runner(PID, "morning-digest") == "nowhere"  # Hermes stays paused; Grok never told to start
    assert fake.poll(m["change_id"], 1) is None  # the request is gone: it can never be made
    assert world.x.preview(PID).get("pending") is None


def test_a_broken_state_file_does_not_crash(tmp_path):
    world = World(tmp_path)
    (tmp_path / "central" / h2g.STATE_FILE).write_text(json.dumps(
        {"v": 1, "allow": {PID: "x"}, "base": [], "runner": {PID: 3}, "changes": [None, {"id": 5}, {"id": "c-1"}]}))
    assert world.x.allowed(PID) == []
    world.x.allow(PID, "all")
    assert world.x.preview(PID)["nothing"] is True
    world.x.poll()


def test_undo_says_which_texts_it_cannot_put_back(tmp_path):
    world = World(tmp_path, writer=_NoTextRead())
    world.x.allow(PID, "all")
    world.hermes["instructions"] = "New instructions."
    world.hermes["routines"] = [ROUTINE]
    p, r = apply_now(world)
    u = world.x.undo(PID, r["change_id"])
    assert u["not_undone"] == ["instructions"] and "Not put back: instructions." in u["sentence"]
    assert h2g.FakeGrokWriter.read(world.writer, BOT)["routines"] == []


def test_the_to_grok_command_refuses_requests_and_chats(monkeypatch):
    """Allowing, applying, undoing, settling and moving routines need the owner: never from a request from another
    computer or a command a chat started (the preview must be seen by a person)."""
    from hermes_plugins.grok_bot_twin.twin import cli, h2g_service, tasks
    from hermes_plugins.grok_bot_twin.twin.engine import EngineError
    import types as _t
    monkeypatch.setattr(cli, "_svc", lambda: object())
    monkeypatch.setattr(cli, "_one", lambda svc, pair: "test-scout")
    for marks in ((True, []), (False, ["1"])):
        monkeypatch.setattr(tasks, "asked_turn", lambda *a, _m=marks[0], **k: _m)
        monkeypatch.setattr(tasks, "_chat_numbers", lambda *a, _m=marks[1], **k: _m)
        for action in ("allow", "apply", "undo", "resolve", "runner"):
            with pytest.raises(EngineError) as e:
                h2g_service.cmd_to_grok(_t.SimpleNamespace(args=["Test Scout", action, "x"], json=False))
            assert e.value.code == "refused", action
