"""A twin is only "on" a computer that holds it; a request that was not done says why; an earlier version can be
seen before it is put back.

- A row said "also on Studio" for a twin made while Studio was off (or while sharing was off): every linked
  computer was listed for every twin. A computer that never reported the twin now has it as "pending".
- "Stop twinning" and "Unlink" said "when it is next on" of a computer that was on.
- The asking computer said only "not done on MacBook Pro" although both computers knew the reason.
- Earlier versions that botport keeps came without text, so the page offered "Put back" but never "Show"; versions
  that were empty or the same as the twin's current text were listed too.
Invented bots and computers only.
"""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from twin_testlib import config, home, service  # noqa: F401
from test_ux_flows import fake  # noqa: F401
from test_setup_download_and_twin_facts import profile, twin  # noqa: F401
from test_service_real_botport import ACME, BIN, rig  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import fleet, fleet_cli, tasks
from hermes_plugins.grok_bot_twin.twin import hermes_side as hs
from hermes_plugins.grok_bot_twin.twin import service as S

STUDIO = "m-77aa01bc9d10"


def _iso(seconds_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _link_studio(svc, holds: bool, seen_s_ago=5.0, rev_current: bool = True):
    """Studio as the sharing computer remembers it: linked, last heard `seen_s_ago` seconds ago (None: never),
    holding this computer's one twin or not."""
    with service.store.locked():
        st = fleet.load()
        lst = st["share"]["list"]
        tid, t = next(iter(lst["twins"].items()))
        st["share"]["followers"][STUDIO] = {
            "id": STUDIO, "name": "Studio", "platform": "macos", "key": "0" * 64, "linked_at": _iso(600),
            "last_seen_at": None if seen_s_ago is None else _iso(seen_s_ago),
            "applied": {"rev": lst["rev"] if rev_current else "r-older", "at": _iso(60),
                        "twins": {tid: t["version"]} if holds else {}, "failed": {}}}
        fleet.save(st)
    return tid


@pytest.fixture
def shares(twin, home):  # noqa: F811
    config.set_override({**config.settings(), "share_address": "127.0.0.1", "share_port": 0, "machine_name": "mini"})
    twin.fleet_share(True)
    return twin


def _studio(f):
    return next(m for m in f["twins"][0]["machines"] if m["id"] == STUDIO)


def test_a_twin_a_linked_computer_never_got_is_pending_there_and_the_terminal_does_not_say_on(shares):
    svc = shares
    _link_studio(svc, holds=False, seen_s_ago=None, rev_current=False)  # linked, off since before the twin was made
    f = svc.fleet()
    assert _studio(f)["state"] == "pending" and _studio(f)["last_sync_at"] is None and "error" not in _studio(f)
    line = next(ln for ln in fleet_cli.describe(f) if ln.startswith("  Acme Support:"))
    assert line == "  Acme Support: on mini; goes to Studio when it is next on"
    # the same computer answering right now, the twin not there yet: on its way, still not "on"
    _link_studio(svc, holds=False, seen_s_ago=3, rev_current=False)
    f = svc.fleet()
    assert _studio(f)["state"] == "pending"
    assert "  Acme Support: on mini; on its way to Studio" in fleet_cli.describe(f)
    # once Studio says it holds the twin, it is on Studio
    _link_studio(svc, holds=True, seen_s_ago=3)
    f = svc.fleet()
    assert _studio(f)["state"] == "in_sync"
    assert "  Acme Support: on mini, Studio" in fleet_cli.describe(f)


def test_a_twin_made_while_sharing_is_off_goes_there_when_sharing_is_on_again(shares):
    svc = shares
    _link_studio(svc, holds=False, seen_s_ago=3, rev_current=False)
    off = svc.fleet_share(False)
    assert _studio(off)["state"] == "pending"
    assert "  Acme Support: on mini; goes to Studio when sharing is on again" in fleet_cli.describe(off)


def test_stop_twinning_names_only_computers_that_hold_the_twin_and_next_on_only_for_one_that_is_off(shares):
    svc = shares
    _link_studio(svc, holds=False, seen_s_ago=3, rev_current=False)
    assert not any("copies on" in line for line in svc.remove("acme-support", True)["goes"])  # Studio has no copy
    _link_studio(svc, holds=True, seen_s_ago=3)
    assert svc.remove("acme-support", True)["goes"][-1] == "Its copies on Studio. Kept there 30 days, with chats."
    plan = svc.fleet_remove_machine(STUDIO, True)
    assert plan["goes"] == ["The 1 twin on Studio. It is kept there for 30 days with its chats."]
    _link_studio(svc, holds=True, seen_s_ago=3600)  # Studio is off
    assert svc.remove("acme-support", True)["goes"][-1] == ("Its copies on Studio, when it is next on. "
                                                            "Kept there 30 days, with chats.")
    plan = svc.fleet_remove_machine(STUDIO, True)
    assert plan["goes"] == ["The 1 twin on Studio, when it is next on. It is kept there for 30 days with its chats."]


def test_the_asking_computer_says_why_a_request_was_not_done():
    assert tasks.label("failed", "MacBook Pro", None, "task_failed") == \
        "not done on MacBook Pro: its twin there could not answer; its chat on MacBook Pro says why"
    assert tasks.label("failed", "Surface", None, "no_hermes") == \
        "not done on Surface: Hermes could not start a chat with the twin there; open Hermes on Surface once"
    assert tasks.label("timeout", "Surface", code="task_timeout") == \
        "not done on Surface: its twin there did not finish in time; its chat on Surface shows how far it got"
    assert tasks.label("failed", "Surface", None, "no_twin_there") == "not done on Surface: Surface holds no copy of this twin yet"
    # the reasons that were already said stay as they were; a code nobody knows still gives the plain line
    assert tasks.label("failed", "Surface", code="approval_denied") == "not done on Surface: a step was not approved there"
    assert tasks.label("failed", "Surface", None, "tasks_off") == "not sent: requests are switched off on Surface"
    assert tasks.label("failed", "Surface", None, "something_new") == "not done on Surface"
    for code in tasks.OUT_REASONS:
        line = tasks.label("failed", "Surface", None, code)
        assert code in tasks.CODES and "(" not in line and len(line.split()) <= 22, (code, line)


def test_an_activity_line_names_a_part_in_plain_words():
    assert S.plain_summary("persona.name restored from a kept copy") == "name put back from an earlier version"
    assert S.plain_summary("persona.description updated from Grok") == "description updated from Grok"
    assert S.plain_summary("skill:greet-visitors restored from a kept copy") == \
        'skill "greet-visitors" put back from an earlier version'
    assert S.plain_summary("name updated from Grok") == "name updated from Grok" and S.plain_summary("") == ""
    assert S.plain_summary(None) == ""


def test_the_page_is_told_when_the_grok_folder_goes_and_when_it_is_back(twin, home, monkeypatch):  # noqa: F811
    svc = twin
    there = {"is": True}
    monkeypatch.setattr(type(svc), "grok_app_found", lambda self: there["is"])

    def told():
        return [e["payload"] for e in svc.events()["events"] if e["type"] == "setup"]

    svc.note_grok_app()  # the first look tells nothing
    svc.note_grok_app()
    assert told() == []
    there["is"] = False
    svc.note_grok_app()
    svc.note_grok_app()  # said once, not at every look
    assert told() == [{"grok_app": False}]
    there["is"] = True
    svc.note_grok_app()
    assert told() == [{"grok_app": False}, {"grok_app": True}]


def test_a_default_computer_name_leaves_the_owners_first_name_out(monkeypatch):
    import socket
    for host, name in (("Alexs-MacBook-Pro.local", "MacBook Pro"), ("Alexs-Mac-mini", "Mac mini"),
                       ("Jamess-iMac.lan", "iMac"), ("Alexs-MacBook-Pro-2.local", "MacBook Pro 2"),
                       ("studio", "studio"), ("example-pc_box.local", "example pc box"),
                       ("Glass-Mac.local", "Mac"), ("Thomas-desk", "Thomas desk"), ("", "This computer")):
        monkeypatch.setattr(socket, "gethostname", lambda host=host: host)
        assert fleet.default_machine_name() == name, host


def test_fleet_hints_use_plain_words_and_never_ask_for_a_restart(home):  # noqa: F811
    from hermes_plugins.grok_bot_twin.twin import fleet_profiles as fp
    busy = fleet.message("port_busy", port=18777, program="")
    assert busy["hint"].startswith("Sharing tries again by itself every minute.") and "estart" not in busy["hint"]
    assert "both computers" not in fleet.message("version_mismatch")["hint"]
    (home / "profiles" / "test-quill").mkdir(parents=True)
    (home / "profiles" / "test-quill" / "SOUL.md").write_text("My own bot.\n", encoding="utf-8")
    with pytest.raises(fp.ProfileBlocked) as ei:
        fp.create("test-quill", "Test Quill", "Drafts short notes.", "")
    said = str(ei.value.hint)
    assert said == "Another Hermes bot on this computer already has the name Test Quill."
    assert "test-quill" not in said and "SOUL" not in said


# ---- earlier versions that botport keeps ----------------------------------------------------------------------------
def _soul(name, description):
    out = ""
    if description is not None:
        out += f"<!-- botport:begin persona-description -->\n{description}\n<!-- botport:end persona-description -->\n"
    if name is not None:
        out += f"<!-- botport:begin persona-name -->\n{name}\n<!-- botport:end persona-name -->\n"
    return out


def _d(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _record(name, grok_form_digest):
    return json.dumps({"schema": "botport-mirror/1", "items": {"persona.name": {
        "base_local": _d(name + "\n"), "base_remote": grok_form_digest, "neutral_remote": _d(name)}}})


def _kept_zip(folder, stamp, soul, record):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"restore-{stamp}-00.zip"
    with zipfile.ZipFile(path, "w") as zf:
        if soul is not None:
            zf.writestr("SOUL.md", soul)
        zf.writestr("botport.mirror.json", record)
    return path


def test_a_kept_version_has_its_text_only_when_a_digest_proves_it(home):  # noqa: F811
    prof = home / "profiles" / "test-scout"
    prof.mkdir(parents=True)
    (prof / "SOUL.md").write_text(_soul("Test Scout Pro", "Finds trails and checks the weather."), encoding="utf-8")
    (prof / "botport.mirror.json").write_text(_record("Test Scout Pro", "ab" * 32), encoding="utf-8")
    kept = prof / "backups" / "botport"
    _kept_zip(kept, "20000101T090000Z", None, json.dumps({"items": {}}))  # the moment the twin was made
    _kept_zip(kept, "20000102T090000Z", _soul("Test Scout", "Finds trails."), _record("Test Scout", "cd" * 32))
    known = hs.botport_kept_texts(prof)
    assert known["now"] == {"persona.name": "Test Scout Pro", "persona.description": "Finds trails and checks the weather."}
    v = known["versions"]
    assert v[("persona.name", _d("Test Scout\n")[:12])] == "Test Scout"       # the Hermes text's own digest
    assert v[("persona.name", ("cd" * 32)[:12])] == "Test Scout"               # Grok's form, tied by botport's record
    assert v[("persona.name", ("ab" * 32)[:12])] == "Test Scout Pro"
    assert v[("persona.description", _d("Finds trails.\n")[:12])] == "Finds trails."
    assert v[("persona.name", _d("")[:12])] == ""                              # before the part existed
    assert ("persona.name", "0123456789ab") not in v                           # a digest nothing proves
    assert hs.botport_kept_texts(None) == {"now": {}, "versions": {}}
    assert hs.botport_kept_texts(home / "profiles" / "nobody") == {"now": {}, "versions": {}}


def test_a_bot_without_the_folder_of_kept_copies_still_has_its_live_texts_proven(home):  # noqa: F811
    # Stop twinning takes backups/botport away and botport does not make it again for a bot it picks up: the
    # live prompt file and botport's live record are then all there is, and they must be read like a kept state.
    prof = home / "profiles" / "test-lark"
    prof.mkdir(parents=True)
    (prof / "SOUL.md").write_text(_soul("Test Lark", "Sings at dawn."), encoding="utf-8")
    (prof / "botport.mirror.json").write_text(_record("Test Lark", "ab" * 32), encoding="utf-8")
    assert not (prof / "backups").exists()
    known = hs.botport_kept_texts(prof)
    assert known["now"] == {"persona.name": "Test Lark", "persona.description": "Sings at dawn."}
    assert known["versions"][("persona.name", _d("Test Lark\n")[:12])] == "Test Lark"
    assert known["versions"][("persona.name", ("ab" * 32)[:12])] == "Test Lark"  # Grok's form, tied by the live record
    (prof / "backups").write_text("not a folder", encoding="utf-8")              # something else of that name
    assert hs.botport_kept_texts(prof)["now"]["persona.name"] == "Test Lark"
    (prof / "botport.mirror.json").write_text("{not json", encoding="utf-8")     # a damaged record: the texts stand
    assert hs.botport_kept_texts(prof)["versions"][("persona.name", _d("Test Lark\n")[:12])] == "Test Lark"


def test_a_twin_made_again_after_stop_twinning_lists_no_version_of_what_it_holds_now(twin, home, monkeypatch):  # noqa: F811
    svc = twin
    pair = svc.status()["pairs"][0]
    prof = home / "profiles" / pair["hermes"]["profile"]
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "SOUL.md").write_text(_soul("Acme Support", "Answers questions."), encoding="utf-8")
    (prof / "botport.mirror.json").write_text(_record("Acme Support", "cd" * 32), encoding="utf-8")
    assert not (prof / "backups" / "botport").exists()

    def point(item, digest):
        return {"id": f"{item}@{digest[:12]}", "item": item, "kind": item.split(".")[1], "name": item.split(".")[1],
                "at": "2000-01-02T09:00:00Z", "reason": "kept copy", "scope": "item"}

    raw = [point("persona.name", "cd" * 32),                          # the name it holds now, in Grok's form
           point("persona.description", _d("Answers questions.\n"))]  # the description it holds now
    eng = svc.engine_for(pair["pair_id"])
    monkeypatch.setattr(eng, "restore_points", lambda pid: {"points": raw})
    assert svc.restore_points(pair["pair_id"])["points"] == []
    assert svc.versions_listed(pair["pair_id"], prof, 2) == 0         # the number on the tab agrees


@pytest.mark.skipif(os.name == "nt", reason="links need rights on Windows")
def test_kept_copies_are_read_without_following_a_link_and_a_broken_one_is_skipped(home, tmp_path):  # noqa: F811
    prof = home / "profiles" / "test-scout"
    prof.mkdir(parents=True)
    (prof / "SOUL.md").write_text(_soul("Test Scout Pro", None), encoding="utf-8")
    kept = prof / "backups" / "botport"
    outside = _kept_zip(tmp_path / "elsewhere", "20000102T090000Z", _soul("Not Ours", None), _record("Not Ours", "ee" * 32))
    kept.mkdir(parents=True)
    os.symlink(outside, kept / "restore-20000102T090000Z-00.zip")          # a link is never followed
    (kept / "restore-20000103T090000Z-00.zip").write_bytes(b"not a zip")    # a damaged file is skipped
    (kept / "notes.zip").write_bytes(b"x")                                  # not one of botport's names
    v = hs.botport_kept_texts(prof)["versions"]
    assert "Not Ours" not in v.values() and v[("persona.name", _d("Test Scout Pro\n")[:12])] == "Test Scout Pro"


def test_the_list_shows_a_proven_version_and_leaves_out_empty_and_unchanged_ones(twin, home, monkeypatch):  # noqa: F811
    svc = twin
    pair = svc.status()["pairs"][0]
    prof = home / "profiles" / pair["hermes"]["profile"]
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "SOUL.md").write_text(_soul("Acme Support", "Answers questions."), encoding="utf-8")
    _kept_zip(prof / "backups" / "botport", "20000102T090000Z", _soul("Acme Helper", "Answers questions."),
              _record("Acme Helper", "cd" * 32))
    at = "2000-01-02T09:00:00Z"

    def point(item, digest):
        return {"id": f"{item}@{digest[:12]}", "item": item, "kind": item.split(".")[1], "name": item.split(".")[1],
                "at": at, "reason": "kept copy", "scope": "item"}

    raw = [point("persona.name", "cd" * 32),                          # the earlier name, in Grok's form
           point("persona.name", _d("Acme Helper\n")),                # the same text under a second digest
           point("persona.name", _d("Acme Support\n")),               # what the twin holds now
           point("persona.name", _d("")),                             # before the name existed
           point("persona.description", _d("Answers questions.\n")),  # unchanged
           point("persona.name", "0123456789ab" * 2)]                 # nothing proves this one: listed, no text
    eng = svc.engine_for(pair["pair_id"])
    monkeypatch.setattr(eng, "restore_points", lambda pid: {"points": raw})
    pts = svc.restore_points(pair["pair_id"])["points"]
    assert [(p["id"], p["text"]) for p in pts] == [("persona.name@cdcdcdcdcdcd", "Acme Helper"),
                                                   ("persona.name@0123456789ab", None)]


@pytest.mark.skipif(not (BIN and os.path.isfile(BIN) and os.access(BIN, os.X_OK)),
                    reason="set BOTPORT_TEST_BIN to a botport build to run these")
def test_with_a_real_botport_what_show_shows_is_what_put_back_puts_back(rig):  # noqa: F811
    svc, home, world = rig["svc"], rig["home"], rig["world"]  # noqa: F811
    svc.trust_botport(True)
    first = svc.create([ACME])["pairs"][0]
    pid, prof = first["pair_id"], home / "profiles" / first["hermes"]["profile"]
    assert svc.restore_points(pid)["points"] == []  # a twin made a moment ago has nothing to go back to
    assert svc.status(fresh=True)["pairs"][0]["restore_points"] == 0  # and the number on the tab agrees
    was = dict(hs.botport_kept_texts(prof)["now"])
    world.step("rename")  # the name and the description change in Grok, and the twin follows
    svc.sync(pid)
    now = hs.botport_kept_texts(prof)["now"]
    assert now != was and all(now[k] != was[k] for k in was)
    pts = svc.restore_points(pid)["points"]
    assert svc.status(fresh=True)["pairs"][0]["restore_points"] == len(pts)
    shown = {p["item"]: p for p in pts if p["text"]}
    assert {k: p["text"] for k, p in shown.items()} == was
    assert not any(p["text"] in (now["persona.name"], now["persona.description"], "") for p in pts)
    undo = {}
    for item, p in shown.items():
        res = svc.restore(pid, p["id"])
        assert hs.botport_kept_texts(prof)["now"][item] == p["text"]  # exactly the text that was shown
        undo[item] = res["restore_point"]  # botport names none; the plugin finds the version that was replaced
    # and each can be undone: the text that was replaced is listed now, with its text, and Undo is that version
    back = {p["item"]: p for p in svc.restore_points(pid)["points"] if p["text"]}
    assert {k: p["text"] for k, p in back.items()} == now
    assert undo == {k: p["id"] for k, p in back.items()}
    svc.restore(pid, undo["persona.name"])
    assert hs.botport_kept_texts(prof)["now"]["persona.name"] == now["persona.name"]
    # putting back what is already there replaces nothing: no Undo is named
    assert svc.restore(pid, undo["persona.name"])["restore_point"] is None
