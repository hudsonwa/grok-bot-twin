"""A twin's skills on the owner's other computers: what leaves, what arrives, and what is never overwritten.

Units run in the test's own process. The stories run three real processes on loopback, each with its own
Hermes folder: "mini" shares, "Surface" and "Orchard" follow (tools/fleet.py, as a background unit runs it).
Invented twins and invented skills only; the "key" in them is made up for the test.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

import fleet_testlib
from fleet_testlib import Machine, free_port, next_count, wait_until, wire, wire_link, wire_sync
from twin_testlib import ROOT, config, fake_key, home, service  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import fleet, fleet_follow as follow, fleet_items, fleet_skills as fs
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

SHAPES = json.loads((ROOT / "tests" / "contract" / "skills_shapes.json").read_text(encoding="utf-8"))["shapes"]
TRAIL = "outdoors/trail-notes"
SKILL_MD = "---\nname: trail-notes\ndescription: Notes on trails.\n---\n\n# Trail notes\n\nWalk the ridge first.\n"


def shape_problems(value, name):
    old = fleet_testlib._SHAPES
    fleet_testlib._SHAPES = {**old, **SHAPES}
    try:
        return fleet_testlib.shape_problems(value, name)
    finally:
        fleet_testlib._SHAPES = old


def write_skill(profile: Path, key: str, text: str = SKILL_MD, **more: str) -> Path:
    d = profile.joinpath("skills", *key.split("/"))
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    for rel, body in more.items():
        p = d.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


# ---- units -------------------------------------------------------------------------------------------------------------
def test_what_leaves_of_a_skill_is_its_text_after_the_password_filter(home):  # noqa: F811
    key = fake_key()
    d = write_skill(home / "profiles" / "test-scout", TRAIL, SKILL_MD + f'\napi_key = "{key}"\n',
                    **{"notes/routes.md": "North loop: 4 km.\n", "token.pem": "not a real key\n"})
    (d / ".env").write_text(f"PASSWORD={key}\n", encoding="utf-8")
    (d / "picture.bin").write_bytes(b"\xff\xfe\x00\x01 not text")
    (d / "big.md").write_text("x" * (fs.FILE_CAP + 1), encoding="utf-8")
    os.symlink(d / "SKILL.md", d / "link.md")
    got = fs.scan(d)
    assert [f["path"] for f in got["files"]] == ["SKILL.md", "notes/routes.md"]
    assert got["left_out"] == 5 and got["masked"] >= 1 and got["too_big"] is False
    assert key not in json.dumps(got) and fs.MARK in got["files"][0]["text"]
    # the filter changes nothing the second time, so the receiving computer's check of the same text agrees
    assert [fs.clean_text(f["text"]) for f in got["files"]] == [f["text"] for f in got["files"]]
    assert fs.scan(d)["digest"] == got["digest"] == fs.digest_of(fs._clean_file_list(got["files"]))
    assert fs.scan(home / "profiles" / "test-scout" / "skills") is None  # a folder without SKILL.md is no skill
    assert sorted(fs.skill_dirs("test-scout")) == [TRAIL]


def test_no_name_from_another_computer_reaches_outside_the_skills_folder():
    for bad in ("../x", "a/b/c", "con", "nul.txt/x", ".hidden", "a\\b", "x/..", "", "a//b", "/abs", "x/.y", "a b",
                "x" * 65, 7, None, "aux/skill", "skill."):
        assert fs.key_ok(bad) is False, bad
    for good in ("trail-notes", "outdoors/trail-notes", "A1/b_2.c"):
        assert fs.key_ok(good) is True, good
    ok = {"path": "SKILL.md", "text": "# ok\n"}
    for bad in ("../SKILL.md", "/etc/x", "a/../../b", "notes/../x.md", "con/x.md", ".env", "notes/.secret",
                "a/b/c/d/e.md", "token.pem", "auth.json", "id_rsa", "notes/credentials.txt", "x\x00.md", "C:\\x.md"):
        assert fs._clean_file_list([ok, {"path": bad, "text": "x"}]) is None, bad
    assert fs._clean_file_list([{"path": "notes.md", "text": "x"}]) is None  # no SKILL.md: not a skill
    assert fs._clean_file_list([ok, {"path": "skill.MD", "text": "x"}, {"path": "SKILL.MD", "text": "y"}]) is None
    assert fs._clean_file_list([ok] * 2) is None
    assert fs._clean_file_list([ok, {"path": "big.md", "text": "x" * (fs.FILE_CAP + 1)}]) is None
    assert fs._clean_file_list([ok, {"path": "n/a.md", "text": 5}]) is None
    key = fake_key()
    got = fs._clean_file_list([{"path": "SKILL.md", "text": f"token = {key}\n"}])
    assert key not in json.dumps(got)  # the receiving computer filters again, whatever was sent
    man = fs.clean_manifest({"on": True, "rev": "3-0123abcd", "twins": [
        {"id": "t-0123456789abcdef", "skills": [
            {"name": "../../x", "version": 1, "digest": "a" * 64},
            {"name": "ok", "version": 1, "digest": "a" * 64, "updated_at": "2000-01-01T09:00:00Z", "extra": "x"},
            {"name": "bad-digest", "version": 1, "digest": "zz"}, {"name": "bad-version", "version": True, "digest": "a" * 64}],
         "removed": [{"name": "gone", "removed_at": "2000-01-01T09:00:00Z"}, {"name": "..", "removed_at": "x"}]},
        {"id": "../../t", "skills": []}]})
    assert man["twins"] == {"t-0123456789abcdef": {
        "skills": {"ok": {"version": 1, "digest": "a" * 64, "updated_at": "2000-01-01T09:00:00Z"}},
        "removed": {"gone": "2000-01-01T09:00:00Z"}}}
    assert fs.clean_manifest({"on": True, "rev": "not a rev", "twins": []}) is None


def test_a_skill_changed_here_is_never_overwritten_without_a_choice():
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    held = {"version": 2, "tdigest": a, "digest": a}
    cases = [
        ({"version": 0, "disk": None, "their": {"version": 1, "digest": a}}, "apply"),          # new here
        ({**held, "disk": a, "their": {"version": 2, "digest": a}}, "adopt"),                    # the same text
        ({**held, "digest": b, "disk": b, "their": {"version": 2, "digest": a}}, "in_sync"),    # as last written
        ({**held, "disk": a, "their": {"version": 3, "digest": b}}, "adopt" if a == b else "apply"),
        ({**held, "disk": c, "their": {"version": 2, "digest": a}}, "changed_here"),             # edited here only
        ({**held, "disk": None, "their": {"version": 2, "digest": a}}, "gone_here"),             # deleted here only
        ({**held, "disk": c, "their": {"version": 3, "digest": b}}, "conflict"),                 # edited on both
        ({**held, "disk": None, "their": {"version": 3, "digest": b}}, "conflict"),              # deleted here, edited there
        ({**held, "disk": c, "their": {"version": 3, "digest": b}, "take": 3}, "apply"),         # the owner chose theirs
        ({**held, "disk": c, "their": {"version": 3, "digest": b}, "off": True}, "off"),         # only on this computer
        ({"version": 0, "disk": c, "their": {"version": 1, "digest": a}}, "conflict_new"),       # another one was here
        ({**held, "disk": a, "their": {"removed": "2000-01-01T09:00:00Z"}}, "remove"),           # untouched: it leaves
        ({**held, "disk": c, "their": {"removed": "2000-01-01T09:00:00Z"}}, "conflict_removed"),  # edited: it asks
        ({"version": 0, "disk": c, "their": {"removed": "2000-01-01T09:00:00Z"}}, "only_here"),  # never taken: not ours
        ({**held, "disk": c, "their": None}, "kept"),                                            # the other one is silent
        ({"version": 0, "disk": c, "their": None}, "only_here"),
    ]
    for rec, want in cases:
        assert fs.judge(rec) == want, rec
    # "apply" and "remove" are the only verdicts that write, and neither is reached with a text changed here
    # unless the owner said so
    for rec, want in cases:
        if want in ("apply", "remove") and not rec.get("take"):
            assert rec["disk"] is None or rec["disk"] == rec.get("digest"), rec


def test_a_computer_that_does_not_name_skills_is_told_nothing_about_them():
    assert fleet_items.answer({"proto": 3, "have": None}) == {}
    assert fleet_items.answer({"xs": "skills"}) == {} and fleet_items.answer({"xs": {"skills": 1}}) == {}
    assert fleet_items.answer({"xs": {"something-of-tomorrow": ""}}) == {}
    assert fleet_items.differs({}, {}) is False


def test_nothing_is_shown_or_written_on_a_computer_that_neither_shares_nor_follows(home):  # noqa: F811
    v = fs.view(service.get_service())
    assert shape_problems(v, "SkillsSync") == [] and v["role"] == "none" and v["twins"] == [] and v["on"] is False
    with pytest.raises(EngineError) as exc:
        fs.switch(service.get_service(), True)
    assert exc.value.code == "not_linked"
    assert not (home / "plugin-data" / "grok-bot-twin" / fs.STATE_FILE).exists()


# ---- stories on three computers ---------------------------------------------------------------------------------------
@pytest.fixture
def mini(tmp_path):
    m = Machine(tmp_path, "mini", free_port())
    m.fx("add", "Test Scout", "Finds trails.")
    m.fx("add", "Test Quill", "Drafts short notes.")
    yield m
    m.stop()


@pytest.fixture
def linked(mini, tmp_path):
    """mini shares; Surface and Orchard are linked and follow in the background."""
    out = []
    code = mini.share()
    for name in ("Surface", "Orchard"):
        m = Machine(tmp_path, name, mini.port, address=None)
        m.run("link", code, "--host", "127.0.0.1")
        m.start()
        out.append(m)
        code = mini.code()
    yield out
    for m in out:
        m.stop()


def skills(m: Machine) -> dict:
    v = m.json("fleet", "skills")
    assert shape_problems(v, "SkillsSync") == [], v
    return v


def row(m: Machine, key: str = TRAIL, twin: str = "Test Scout"):
    for t in skills(m)["twins"]:
        if t["name"] == twin:
            return next((s for s in t["skills"] if s["key"] == key), None)
    return None


def folder(m: Machine, key: str = TRAIL) -> Path:
    return m.profile("test-scout").joinpath("skills", *key.split("/"))


def text(m: Machine, key: str = TRAIL, rel: str = "SKILL.md"):
    p = folder(m, key).joinpath(*rel.split("/"))
    return p.read_text(encoding="utf-8") if p.is_file() else None


def arrives(m: Machine, want: str, key: str = TRAIL, timeout: float = 30.0) -> None:
    wait_until(lambda: text(m, key) == want, timeout=timeout, step=0.2, what=f"{key} on {m.name}")


def state(m: Machine, want: str, key: str = TRAIL, timeout: float = 30.0) -> dict:
    return wait_until(lambda: (lambda r: r if r and r["state"] == want else None)(row(m, key)), timeout=timeout,
                      step=0.5, what=f"{key} to be {want} on {m.name}")


def test_skills_are_not_sent_until_the_owner_switches_them_on_and_then_reach_every_linked_computer(mini, linked):
    key = fake_key()
    d = write_skill(mini.profile("test-scout"), TRAIL, SKILL_MD + f'\napi_key = "{key}"\n',
                    **{"notes/routes.md": "North loop: 4 km.\n", "token.pem": "not a real key\n"})
    (d / ".env").write_text(f"PASSWORD={key}\n", encoding="utf-8")
    write_skill(mini.profile("test-scout"), "packing-list", "# Packing list\n\nWater, map.\n")
    v = skills(mini)
    assert v["role"] == "source" and v["on"] is False and v["state"] == "off" and v["message"] == ""
    assert all(s["state"] == "off" and s["label"] == "not shared" for t in v["twins"] for s in t["skills"])
    surface, orchard = linked
    v = wait_until(lambda: (lambda x: x if x["state"] == "off_there" else None)(skills(surface)), what="Surface to hear")
    assert "not switched on on mini" in v["message"] and v["on"] is True and v["other"]["name"] == "mini"
    assert not folder(surface).exists() and not folder(orchard).exists()
    # nothing about skills has left: no name, no text
    assert "trail" not in json.dumps(v) and "packing" not in (surface.data / fs.STATE_FILE).read_text(encoding="utf-8")

    v = mini.json("fleet", "skills", "on")
    assert v["on"] is True and v["state"] == "on" and "2 skills shared with Orchard and Surface" in v["message"]
    assert row(mini)["label"] == "shared, version 1" and "left out" in row(mini)["note"]
    for m in linked:
        arrives(m, SKILL_MD + f'\napi_key = "{fs.MARK}"\n')
        assert text(m, TRAIL, "notes/routes.md") == "North loop: 4 km.\n"
        assert sorted(p.name for p in folder(m).iterdir()) == ["SKILL.md", "notes"]  # no .env, no key file
        arrives(m, "# Packing list\n\nWater, map.\n", "packing-list")
        for p in m.home.rglob("*"):  # the made-up key is nowhere on the linked computer
            if p.is_file():
                assert key.encode() not in p.read_bytes(), p
        r = state(m, "in_sync")
        assert r["label"] == "same as on mini" and r["version"] == 1 and r["on"] is True and r["versions"] == []
    assert "2 skills the same as on mini" in skills(surface)["message"]

    # a change on the sharing computer follows, and the version it replaced is kept on each linked computer
    (folder(mini) / "SKILL.md").write_text(SKILL_MD + "Then the creek.\n", encoding="utf-8")
    for m in linked:
        arrives(m, SKILL_MD + "Then the creek.\n")
        r = state(m, "in_sync")
        assert r["version"] == 2 and len(r["versions"]) == 1 and r["versions"][0]["vid"].startswith("k-")
    r = row(mini)
    assert r["label"] == "shared, version 2" and [v["vid"] for v in r["versions"]] == ["v1"]

    # switched off again: nothing more is sent, what arrived stays
    mini.json("fleet", "skills", "off")
    (folder(mini) / "SKILL.md").write_text(SKILL_MD + "Never sent.\n", encoding="utf-8")
    v = wait_until(lambda: (lambda x: x if x["state"] == "off_there" else None)(skills(surface)), what="Surface to hear")
    assert "stay as they are" in v["message"] and text(surface) == SKILL_MD + "Then the creek.\n"


def test_a_skill_changed_on_two_computers_asks_which_to_keep_and_each_answer_can_be_undone(mini, linked):
    surface, orchard = linked
    write_skill(mini.profile("test-scout"), TRAIL)
    mini.json("fleet", "skills", "on")
    for m in linked:
        arrives(m, SKILL_MD)
    # changed on a linked computer only: it stays there, nothing is sent back, nothing is overwritten
    for m in linked:
        (folder(m) / "SKILL.md").write_text(SKILL_MD + f"Mine, on {m.name}.\n", encoding="utf-8")
        r = state(m, "changed_here")
        assert [c["keep"] for c in r["choices"]] == ["theirs"] and "stays here" in r["label"]
    assert text(mini) == SKILL_MD
    # ... and then on the sharing computer too: a choice, and still nothing overwritten
    (folder(mini) / "SKILL.md").write_text(SKILL_MD + "Theirs.\n", encoding="utf-8")
    for m in linked:
        r = state(m, "conflict")
        assert [c["keep"] for c in r["choices"]] == ["mine", "theirs"] and r["label"] == "changed here and on mini"
        assert [c["label"] for c in r["choices"]] == ["Keep this computer's", "Keep mini's"]
        assert text(m) == SKILL_MD + f"Mine, on {m.name}.\n"
    assert "1 skill needs your choice" in skills(surface)["message"]
    wait_until(lambda: "keeps a version of its own" in (row(mini)["note"] or "") or
               "keep versions of their own" in (row(mini)["note"] or ""), what="mini to hear of the choice")

    # Surface keeps the other computer's: its own text is kept as an earlier version, and Undo brings it back
    v = surface.json("fleet", "skills", "keep", TRAIL, "theirs")
    assert text(surface) == SKILL_MD + "Theirs.\n" and v["undo"]["vid"].startswith("k-") and v["undo"]["skill"] == TRAIL
    assert row(surface)["state"] == "in_sync" and row(surface)["versions"][0]["vid"] == v["undo"]["vid"]
    v2 = surface.json("fleet", "skills", "restore", TRAIL, v["undo"]["vid"])
    assert text(surface) == SKILL_MD + "Mine, on Surface.\n" and v2["undo"]["vid"].startswith("k-")
    assert row(surface)["state"] == "changed_here"  # and it is not overwritten again behind the owner's back
    v3 = surface.json("fleet", "skills", "restore", TRAIL, v2["undo"]["vid"])  # undo of the undo
    assert text(surface) == SKILL_MD + "Theirs.\n" and row(surface)["state"] == "in_sync" and v3["undo"]

    # Orchard keeps its own: no more question, until the sharing computer changes the skill again
    v = orchard.json("fleet", "skills", "keep", TRAIL, "mine")
    assert v["undo"] is None and text(orchard) == SKILL_MD + "Mine, on Orchard.\n"
    assert row(orchard)["state"] == "changed_here"
    (folder(mini) / "SKILL.md").write_text(SKILL_MD + "Theirs, again.\n", encoding="utf-8")
    arrives(surface, SKILL_MD + "Theirs, again.\n")
    state(orchard, "conflict")
    assert text(orchard) == SKILL_MD + "Mine, on Orchard.\n"
    cp = orchard.run("fleet", "skills", "keep", "packing-list", "mine", check=False)
    assert cp.returncode != 0 and "not here any more" in cp.stdout + cp.stderr


def test_a_skill_marked_only_on_this_computer_is_left_alone_and_one_no_longer_shared_leaves_but_is_kept(mini, linked):
    surface, orchard = linked
    write_skill(mini.profile("test-scout"), TRAIL)
    write_skill(mini.profile("test-scout"), "packing-list", "# Packing list\n")
    mini.json("fleet", "skills", "on")
    for m in linked:
        arrives(m, SKILL_MD)
        arrives(m, "# Packing list\n", "packing-list")
    v = surface.json("fleet", "skills", "set", TRAIL, "off")
    r = next(s for t in v["twins"] for s in t["skills"] if s["key"] == TRAIL)
    assert r["state"] == "off" and r["on"] is False and r["label"] == "only on this computer"
    (folder(mini) / "SKILL.md").write_text(SKILL_MD + "Second.\n", encoding="utf-8")
    arrives(orchard, SKILL_MD + "Second.\n")
    assert text(surface) == SKILL_MD and row(surface)["state"] == "off"
    wait_until(lambda: "Surface keeps a version of its own" in (row(mini)["note"] or ""), what="mini to hear")
    surface.json("fleet", "skills", "set", TRAIL, "on")  # follows again: untouched here, so it catches up
    arrives(surface, SKILL_MD + "Second.\n")

    # one skill is no longer shared on the sharing computer: it leaves the linked ones, and is kept there
    v = mini.json("fleet", "skills", "set", "packing-list", "off")
    assert next(s for t in v["twins"] for s in t["skills"] if s["key"] == "packing-list")["label"] == "not shared"
    for m in linked:
        wait_until(lambda: not folder(m, "packing-list").exists(), what=f"packing-list to leave {m.name}")
        r = state(m, "removed_there", "packing-list")
        assert len(r["versions"]) == 1 and r["can_switch"] is False
    assert text(mini, "packing-list") == "# Packing list\n"  # nothing changes on the computer that shares
    # ... and can be put back by hand: it is then this computer's own
    surface.json("fleet", "skills", "restore", "packing-list", row(surface, "packing-list")["versions"][0]["vid"])
    assert text(surface, "packing-list") == "# Packing list\n" and row(surface, "packing-list")["state"] == "only_here"

    # a skill deleted on the sharing computer leaves where it was untouched, and asks where it was changed
    (folder(orchard) / "SKILL.md").write_text(SKILL_MD + "Orchard's own.\n", encoding="utf-8")
    state(orchard, "changed_here")
    shutil.move(str(folder(mini)), str(mini.home / "moved-away-trail-notes"))
    wait_until(lambda: not folder(surface).exists(), what="trail-notes to leave Surface")
    r = state(orchard, "conflict")
    assert r["choices"][1]["label"] == "Take it away here too" and text(orchard) == SKILL_MD + "Orchard's own.\n"
    orchard.json("fleet", "skills", "keep", TRAIL, "mine")
    assert row(orchard)["state"] == "only_here" and text(orchard) == SKILL_MD + "Orchard's own.\n"


def _probe(mini: Machine, mid: str = "m-aaaaaaaaaaaa") -> str:
    status, obj, _ = wire_link(mini.port, mini.code(), mid=mid)
    assert status == 200, obj
    return obj["pass"]


def _ask(mini: Machine, token, body=None, answer=None):
    body = {"proto": fleet.PROTO, "xs": 1, "want": [], "report": {}, "n": next_count(), **(body or {})}
    return wire("POST", mini.port, fs.ROUTE, body, token=token, answer=answer)


def test_only_a_linked_computer_is_told_about_skills_and_only_what_the_allowlist_names(mini):
    mini.share()
    key = fake_key()
    write_skill(mini.profile("test-scout"), TRAIL, SKILL_MD + f"password: {key}\n")
    token = _probe(mini)
    ans: dict = {}
    # off: a linked computer hears "off" and not one name
    status, obj, raw = _ask(mini, token, answer=ans)
    assert status == 200 and ans["signed"] and obj == {"schema": fleet.SCHEMA, "xs": 1, "on": False, "rev": "off"}
    mini.json("fleet", "skills", "on")
    # not linked, or not signed: nothing
    status, obj, raw = _ask(mini, "m-bbbbbbbbbbbb|" + "0" * 64, answer=ans)
    assert status == 401 and not ans["signed"] and b"trail" not in raw
    status, obj, raw = wire("POST", mini.port, fs.ROUTE, {"proto": fleet.PROTO, "xs": 1})
    assert status == 401 and b"trail" not in raw
    # linked: the list, and the text of what it names
    status, obj, raw = _ask(mini, token, answer=ans)
    assert status == 200 and ans["signed"] and set(obj) == {"schema", "xs", "on", "rev", "twins", "files"}
    scout = next(t for t in obj["twins"] if t["skills"])
    assert set(scout) == {"id", "skills", "removed"} and obj["files"] == []
    s = scout["skills"][0]
    assert set(s) == {"name", "version", "digest", "updated_at", "size", "files"} and s["name"] == TRAIL
    status, obj, raw = _ask(mini, token, {"want": [{"twin": scout["id"], "skill": TRAIL, "version": s["version"]},
                                                   {"twin": scout["id"], "skill": "../../x", "version": 1},
                                                   {"twin": scout["id"], "skill": TRAIL, "version": 99}]}, answer=ans)
    assert status == 200 and len(obj["files"]) == 1 and set(obj["files"][0]) == {"twin", "skill", "version", "digest", "files"}
    assert [set(f) for f in obj["files"][0]["files"]] == [{"path", "text"}]
    assert key.encode() not in raw and fs.MARK.encode() in raw
    assert fs.digest_of(obj["files"][0]["files"]) == s["digest"]
    for word in (b"profiles", b"hermes-home", str(mini.home).encode(), b"SOUL", b"test-scout", b"Finds trails"):
        assert word not in raw, word
    # a recording of a signed request, sent again, is refused
    body = {"proto": fleet.PROTO, "xs": 1, "want": [], "report": {}, "n": next_count()}
    mid, pass_ = token.split("|", 1)
    k, nonce = fleet.share_key(pass_), fleet.new_nonce()
    data = json.dumps(body).encode()
    hdrs = {"X-GBT-Machine": mid, "X-GBT-Nonce": nonce, "X-GBT-Mac": fleet.request_mac(k, fs.ROUTE, nonce, data)}
    assert wire("POST", mini.port, fs.ROUTE, raw_body=data, headers=hdrs)[0] == 200
    status, obj, raw = wire("POST", mini.port, fs.ROUTE, raw_body=data, headers=hdrs)
    assert status == 409 and obj["error"]["code"] == "replayed" and b"trail" not in raw
    # a computer the owner unlinked is told so (signed), and nothing else
    mini.run("fleet", "remove", "Probe", "--yes")
    status, obj, raw = _ask(mini, token, answer=ans)
    assert status == 401 and ans["signed"] and obj["error"]["code"] == "unlinked" and b"trail" not in raw


def test_an_older_linked_computer_gets_exactly_the_answers_it_always_got(mini):
    """A linked computer on a build from before skills names no item in its request: nothing is added for it."""
    mini.share()
    write_skill(mini.profile("test-scout"), TRAIL)
    mini.json("fleet", "skills", "on")
    token = _probe(mini)
    status, lst, _ = wire_sync(mini.port, token)
    assert status == 200 and lst["changed"] is True and fleet_testlib.key_problems(lst) == [] and lst["proto"] == 3
    status, obj, raw = wire_sync(mini.port, token, have=lst["rev"], wait_s=1)
    assert status == 200 and obj["changed"] is False and "xs" not in obj and b"skills" not in raw
    # the same computer on this build names the item, and is answered at once (it holds no rev of the skills yet)
    fl = fleet_testlib._fleet()[0]
    body = {"proto": fl.PROTO, "have": lst["rev"], "wait_s": 30, "machine": {"name": "Probe", "platform": "linux"},
            "applied": {}, "n": next_count(), "xs": {"skills": "", "something-of-tomorrow": ""}}
    import time
    t0 = time.monotonic()
    status, obj, raw = wire("POST", mini.port, "/v1/sync", body, token=token, timeout=40)
    assert status == 200 and time.monotonic() - t0 < 10 and obj["changed"] is False
    assert set(obj["xs"]) == {"skills"} and fl.REV_RE.match(obj["xs"]["skills"])
    # ... and waits like any other once it names the rev it was told
    t0 = time.monotonic()
    status, obj2, _ = wire("POST", mini.port, "/v1/sync", {**body, "xs": obj["xs"], "wait_s": 2, "n": next_count()},
                           token=token, timeout=40)
    assert status == 200 and time.monotonic() - t0 >= 1.5 and obj2["xs"] == obj["xs"]
    # the twin list itself is the list it always was
    status, again, _ = wire("POST", mini.port, "/v1/sync", {**body, "have": None, "n": next_count()}, token=token)
    assert status == 200 and again["changed"] is True and fleet_testlib.key_problems(again) == []


@pytest.fixture
def here(home, mini, tmp_path, monkeypatch):  # noqa: F811
    """The test process as a linked computer."""
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "share_port": mini.port,
                         "machine_name": "Studio"})
    monkeypatch.setattr(follow, "MIN_BACKOFF_S", 0.3)
    monkeypatch.setattr(follow, "MAX_BACKOFF_S", 1.0)
    return service.get_service()


def test_a_sharing_computer_on_an_older_build_is_named_as_the_one_to_update(mini, here, home):  # noqa: F811
    here.fleet_link(mini.share())
    follow.stop()  # (this test's sharing computer is on this build; what an older one says is played in by hand)
    with fleet.locked():
        sk = fs.load()
        sk["fol"] = fs._blank_fol()
        fs.save(sk)
    assert fs.view(here)["state"] == "waiting"
    fleet_items.heard(here, {"changed": False, "sharing": True})  # an older build's answer: no word about skills
    v = fs.view(here)
    assert shape_problems(v, "SkillsSync") == [] and v["role"] == "follower" and v["state"] == "older_there"
    assert "mini has an older Grok Bot Twin that does not send skills yet" in v["message"]
    assert fleet_items.ask() == {"skills": ""}
    fs.switch(here, False)  # switched off here: this computer does not even name skills to the other one
    assert "skills" not in fleet_items.ask() and fs.view(here)["state"] == "off"
    follow.stop()


def test_a_list_from_a_computer_that_was_taken_over_cannot_write_outside_the_skills_folder(mini, here, home):  # noqa: F811
    """What the linked computer does with an answer is decided by `clean_manifest`, `clean_files` and `_settle`:
    names that are not plain never become folders, text whose digest is not the one announced is not written,
    and a planted link where a skill would go is not followed."""
    here.fleet_link(mini.share())
    fst = fleet.load()
    tid, entry = next(iter(fs._linked_twins(fst).items()))
    prof = home / "profiles" / entry["profile"]
    good = [{"path": "SKILL.md", "text": "# ok\n"}]
    outside = home / "outside"
    outside.mkdir()
    (prof / "skills").mkdir(exist_ok=True)
    os.symlink(outside, prof / "skills" / "planted")
    man = fs.clean_manifest({"on": True, "rev": "1-0123abcd", "twins": [{"id": tid, "skills": [
        {"name": "fine", "version": 1, "digest": fs.digest_of(good)},
        {"name": "lied", "version": 1, "digest": "f" * 64},
        {"name": "planted/x", "version": 1, "digest": fs.digest_of(good)},
        {"name": "../escape", "version": 1, "digest": fs.digest_of(good)}], "removed": []}]})
    files = fs.clean_files({"files": [
        {"twin": tid, "skill": "fine", "version": 1, "files": good},
        {"twin": tid, "skill": "lied", "version": 1, "files": good},
        {"twin": tid, "skill": "planted/x", "version": 1, "files": good},
        {"twin": tid, "skill": "../escape", "version": 1, "files": good},
        {"twin": tid, "skill": "fine2", "version": 1, "files": [{"path": "../../SOUL.md", "text": "x"}, *good]}]})
    assert sorted(k[1] for k in files) == ["fine", "lied", "planted/x"]
    with fleet.locked():
        sk = fs.load()
        fs._own_link(sk, fst)
        fs._take_manifest(sk, man)
        wants, done = fs._settle(fst, sk, files)
        fs.save(sk)
    assert (prof / "skills" / "fine" / "SKILL.md").read_text(encoding="utf-8") == "# ok\n" and done == 1
    assert not (prof / "skills" / "lied").exists() and list(outside.iterdir()) == []
    assert not (home / "profiles" / "escape").exists() and not (prof / "escape").exists()
    assert {w["skill"] for w in wants} == {"lied"}  # still wanted (and never written while the digest is not met)
    follow.stop()


# ---- this process as the computer that shares: versions, putting one back, the local routes ---------------------------
@pytest.fixture
def sharing(home, tmp_path):  # noqa: F811
    """The test process shares one invented twin on loopback (what tools/fleet_fixture.py writes, in process)."""
    from hermes_plugins.grok_bot_twin.twin import fleet_io, fleet_server, store
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "share_port": free_port(),
                         "share_address": "127.0.0.1", "machine_name": "mini"})
    svc = service.get_service()
    fleet_io.write_json(store.central_dir() / "pairs.json", {"pairs": [
        {"pair_id": "test-scout", "grok": {"id": "fx-test-scout", "name": "Test Scout", "description": "Finds trails."},
         "hermes": {"profile": "test-scout"}, "created_at": "2000-01-01T09:00:00Z"}]})
    fleet_io.write_text(home / "profiles" / "test-scout" / "SOUL.md", "# Test Scout\n\nFinds trails.\n")
    svc.fleet_share(True)
    yield svc
    fleet_server.stop()


def test_the_sharing_computer_keeps_the_versions_it_sent_and_can_put_one_back(sharing, home):  # noqa: F811
    d = write_skill(home / "profiles" / "test-scout", TRAIL, **{"notes/routes.md": "North loop.\n"})
    (d / "picture.bin").write_bytes(b"\xff\xfe\x00 not text")
    v = fs.view(sharing)
    assert shape_problems(v, "SkillsSync") == [] and v["role"] == "source" and v["on"] is False and v["message"] == ""
    assert not (home / "plugin-data" / "grok-bot-twin" / fs.STATE_FILE).exists()  # looking writes nothing while off
    assert fs._wire_rev() == "off"
    v = fs.switch(sharing, True)
    assert v["on"] is True and "1 skill ready for the first computer you link" in v["message"]
    tid = v["twins"][0]["id"]
    s = v["twins"][0]["skills"][0]
    assert (s["key"], s["name"], s["group"], s["version"], s["state"]) == (TRAIL, "trail-notes", "outdoors", 1, "in_sync")
    assert s["switch_label"] == "trail-notes: share it" and "1 file was left out" in s["note"] and s["versions"] == []
    rev1 = fs._wire_rev()
    assert fleet.REV_RE.match(rev1)
    assert fs.view(sharing)["twins"][0]["skills"][0]["version"] == 1 and fs._wire_rev() == rev1  # looking changes nothing
    (d / "SKILL.md").write_text(SKILL_MD + "Second.\n", encoding="utf-8")
    s = fs.view(sharing)["twins"][0]["skills"][0]
    assert s["version"] == 2 and [x["vid"] for x in s["versions"]] == ["v1"] and fs._wire_rev() != rev1
    assert s["versions"][0]["label"].startswith("Version 1, from ")
    # version 1 back: it becomes version 3 (what the linked computers then get); what was there is set aside whole
    out = fs.restore(sharing, tid, TRAIL, "v1")
    assert (d / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD and out["undo"]["vid"].startswith("k-")
    s = out["twins"][0]["skills"][0]
    assert s["version"] == 3 and {x["vid"] for x in s["versions"]} == {"v1", "v2", out["undo"]["vid"]}
    kept = home / "plugin-data" / "grok-bot-twin" / fs.KEPT_DIR / out["undo"]["vid"]
    assert (kept / "picture.bin").read_bytes() == b"\xff\xfe\x00 not text"  # nothing of the owner's is lost
    assert (kept / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD + "Second.\n"
    out2 = fs.restore(sharing, tid, TRAIL, out["undo"]["vid"])  # Undo
    assert (d / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD + "Second.\n" and (d / "picture.bin").exists()
    assert out2["twins"][0]["skills"][0]["version"] == 4
    # one skill's own switch, and what the linked computers are then told
    out = fs.set_item(sharing, tid, TRAIL, False)
    s = out["twins"][0]["skills"][0]
    assert (s["state"], s["label"], s["on"]) == ("off", "not shared", False)
    man = fs.manifest(fs.load(), fleet.load())
    assert man[0]["skills"] == [] and [r["name"] for r in man[0]["removed"]] == [TRAIL]
    fs.set_item(sharing, tid, TRAIL, True)
    assert [x["name"] for x in fs.manifest(fs.load(), fleet.load())[0]["skills"]] == [TRAIL]
    # a skill over the limits is not shared, and says so
    big = write_skill(home / "profiles" / "test-scout", "too-much", "# Too much\n")
    for i in range(fs.MAX_FILES + 1):
        (big / f"note-{i}.md").write_text("x\n", encoding="utf-8")
    s = next(x for x in fs.view(sharing)["twins"][0]["skills"] if x["key"] == "too-much")
    assert s["state"] == "too_big" and s["on"] is False and s["can_switch"] is False
    assert "too-much" not in json.dumps(fs.manifest(fs.load(), fleet.load()))
    fs.set_item(sharing, tid, "too-much", False)  # a skill that was never sent is not named as "gone" either
    assert "too-much" not in json.dumps(fs.manifest(fs.load(), fleet.load()))
    # a version number is never used twice for one skill, also after the twin left the list and came back
    with fleet.locked():
        sk = fs.load()
        sk["src"]["twins"] = {}
        fs.save(sk)
    fs.source_scan()
    rec = fs.load()["src"]["twins"][tid]["skills"][TRAIL]
    assert rec["version"] == 5 and fs._read_bundle(tid, TRAIL, 5)["digest"] == rec["digest"]
    # a skills folder that cannot be read says nothing about the skills in it: nothing is told to leave
    os.rename(home / "profiles" / "test-scout", home / "profiles" / "test-scout-away")
    assert fs.skill_dirs("test-scout") is None
    fs.source_scan()
    assert fs.load()["src"]["twins"][tid]["skills"][TRAIL]["version"] == 5
    os.rename(home / "profiles" / "test-scout-away", home / "profiles" / "test-scout")
    for call, code in ((lambda: fs.keep(sharing, tid, TRAIL, "mine"), "no_choice"),
                       (lambda: fs.restore(sharing, tid, TRAIL, "v99"), "unknown_version"),
                       (lambda: fs.restore(sharing, tid, TRAIL, "k-000000000000"), "unknown_version"),
                       (lambda: fs.restore(sharing, tid, "../x", "v1"), "unknown_version"),
                       (lambda: fs.set_item(sharing, tid, "no-such", False), "unknown_skill"),
                       (lambda: fs.set_item(sharing, "t-0000000000000000", TRAIL, False), "unknown_skill")):
        with pytest.raises(EngineError) as exc:
            call()
        assert exc.value.code == code


def test_the_terminal_says_what_syncs_and_how_to_switch_it(sharing, home, capsys):  # noqa: F811
    from hermes_plugins.grok_bot_twin.twin import cli
    write_skill(home / "profiles" / "test-scout", TRAIL)
    assert cli.main(["fleet"]) == 0
    out = capsys.readouterr().out
    assert "Skills: not shared. Only names and descriptions leave this computer" in out
    assert "hermes twin fleet skills on" in out
    assert cli.main(["fleet", "skills", "on"]) == 0
    out = capsys.readouterr().out
    assert "Skills: on. Sends each twin's skills (text files only) to your linked computers." in out
    assert "Passwords and keys are left out." in out and "outdoors/trail-notes: shared, version 1" in out
    assert cli.main(["fleet", "skills", "set", TRAIL, "off"]) == 0 and "trail-notes: not shared" in capsys.readouterr().out
    assert cli.main(["fleet", "skills", "versions", TRAIL]) == 0
    assert "No earlier version" in capsys.readouterr().out
    assert cli.main(["fleet", "skills", "--json"]) == 0
    assert shape_problems(json.loads(capsys.readouterr().out), "SkillsSync") == []
    assert cli.main(["fleet"]) == 0 and "Skills: on." in capsys.readouterr().out
    assert cli.main(["fleet", "skills", "frobnicate"]) != 0
    assert "hermes twin fleet skills [on|off]" in "".join(capsys.readouterr())
    assert cli.main(["fleet", "skills", "off"]) == 0 and "Skills: off." in capsys.readouterr().out


def test_the_local_routes_answer_the_contracts_shapes_and_refuse_what_is_not_a_plain_name(sharing, home, monkeypatch):  # noqa: F811
    pytest.importorskip("fastapi")
    import importlib.util
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from twin_testlib import timer
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_skills", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    app = FastAPI()
    P = "/api/plugins/grok-bot-twin"
    app.include_router(api.router, prefix=P)
    client = TestClient(app)
    d = write_skill(home / "profiles" / "test-scout", TRAIL)
    v = client.get(P + "/fleet/skills").json()
    assert shape_problems(v, "SkillsSync") == [] and v["on"] is False
    v = client.post(P + "/fleet/skills/switch", json={"on": True}).json()
    assert shape_problems(v, "SkillsSync") == [] and v["on"] is True
    tid = v["twins"][0]["id"]
    v = client.post(P + "/fleet/skills/item", json={"twin": tid, "skill": TRAIL, "on": False}).json()
    assert v["twins"][0]["skills"][0]["label"] == "not shared"
    client.post(P + "/fleet/skills/item", json={"twin": tid, "skill": TRAIL, "on": True})
    (d / "SKILL.md").write_text(SKILL_MD + "Second.\n", encoding="utf-8")
    v = client.post(P + "/fleet/skills/restore", json={"twin": tid, "skill": TRAIL, "vid": "v1"}).json()
    assert shape_problems(v, "SkillsSync") == [] and v["undo"]["skill"] == TRAIL
    assert (d / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD
    r = client.post(P + "/fleet/skills/keep", json={"twin": tid, "skill": TRAIL, "keep": "mine"})
    assert r.status_code == 200 and r.json()["error"]["code"] == "no_choice" and r.json()["error"]["message"]
    for path, body in (("/fleet/skills/switch", {}), ("/fleet/skills/switch", {"on": "yes"}),
                       ("/fleet/skills/item", {"twin": tid, "skill": "../x", "on": True}),
                       ("/fleet/skills/item", {"twin": "../t", "skill": TRAIL, "on": True}),
                       ("/fleet/skills/item", {"twin": tid, "skill": TRAIL}),
                       ("/fleet/skills/item", {"twin": tid, "skill": TRAIL, "on": 1}),
                       ("/fleet/skills/keep", {"twin": tid, "skill": TRAIL, "keep": "both"}),
                       ("/fleet/skills/restore", {"twin": tid, "skill": TRAIL, "vid": "../../x"}),
                       ("/fleet/skills/restore", {"twin": tid, "skill": "a/b/c", "vid": "v1"})):
        assert client.post(P + path, json=body).status_code == 400, (path, body)


def test_password_like_text_of_many_shapes_is_left_out_and_code_is_left_alone():
    m = fs.MARK
    for text, want in {
        "export DB_PASSWORD=supersecretvalue1": f"export DB_PASSWORD={m}",
        '{"password": "correct-horse-battery-staple"}': '{"password": "%s"}' % m,
        '"api_key": "abcd1234efgh5678"': '"api_key": "%s"' % m,
        "password: hunter2": f"password: {m}",
        "postgres://admin:S3cretPassw0rd@host/db": f"postgres://admin:{m}@host/db",
        'os.environ["API_KEY"] = "abcd1234"': 'os.environ["API_KEY"] = "%s"' % m,
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyzABCDEF": f"Authorization: Bearer {m}",
        "client_secret: 'q9!x-r2'": f"client_secret: '{m}'",
        # ... and what is not a password stays as it is, so a skill's own scripts still run
        "max_tokens = 4096": None, "max_tokens: 100000": None, "password: required": None,
        'tokenizer = AutoTokenizer.from_pretrained("bert-base")': None,
        "total_tokens = prompt_tokens + completion_tokens": None,
        "token: ${{ secrets.DEPLOY_TOKEN }}": None,
        "Use the ridge. Passwords are never written here.": None,
        "See https://example.com/docs/page for the route.": None,
    }.items():
        got = fs.clean_text(text)
        assert got == (text if want is None else want), text
        assert fs.clean_text(got) == got, text  # the receiving computer's pass changes nothing
    key = fake_key()
    twice = fs.clean_text(f"a {key}{key} b\npassword: {key}\n")
    assert key not in twice and fs.clean_text(twice) == twice


def test_two_instant_answers_in_a_row_make_the_following_loop_wait(monkeypatch):
    naps = []
    monkeypatch.setattr(fleet_items.time, "sleep", naps.append)
    monkeypatch.setattr(fleet_items, "_items", {"skills": {"rev": lambda: "1-0123abcd", "have": lambda: "",
                                                           "heard": lambda svc, rev: None}})
    fleet_items._last[0] = 0.0
    fleet_items.heard(None, {"xs": {"skills": "1-0123abcd"}})
    assert naps == []
    fleet_items.heard(None, {"xs": {"skills": "2-0123abcd"}})
    assert len(naps) == 1 and 0 < naps[0] <= fleet_items.MIN_GAP_S


def test_answers_with_the_rev_that_was_asked_with_never_make_the_following_loop_wait(monkeypatch):
    # (a request that waits for nothing, as at linking, is answered at once with the rev it named: were the loop
    # to wait then, no request would be held open, and sharing switched off there would read "not answering")
    naps = []
    monkeypatch.setattr(fleet_items.time, "sleep", naps.append)
    monkeypatch.setattr(fleet_items, "_items", {"skills": {"rev": lambda: "off", "have": lambda: "off",
                                                           "heard": lambda svc, rev: None}})
    fleet_items._last[0] = 0.0
    fleet_items.heard(None, {"xs": {"skills": "off"}}, {"skills": ""})  # the first answer: another rev than asked
    fleet_items.heard(None, {"xs": {"skills": "off"}}, {"skills": "off"})
    fleet_items.heard(None, {"xs": {"skills": "off"}}, {"skills": "off"})
    fleet_items.heard(None, {"changed": False, "sharing": True}, {"skills": "off"})  # an older build: no word
    assert naps == []
    fleet_items.heard(None, {"xs": {"skills": "1-0123abcd"}}, {"skills": "off"})  # a second differing one, at once
    assert len(naps) == 1 and 0 < naps[0] <= fleet_items.MIN_GAP_S


def _arrive(fst, tid, skills, removed=(), version=1):
    """Play one signed answer of the sharing computer into this computer: {name: files}."""
    from hermes_plugins.grok_bot_twin.twin import store
    assert "pytest-" in str(store.hermes_root()), "this helper writes skills: only ever inside the test's own folder"
    man = fs.clean_manifest({"on": True, "rev": "1-0123abcd", "twins": [{"id": tid, "skills": [
        {"name": n, "version": version, "digest": fs.digest_of(f)} for n, f in skills.items()],
        "removed": [{"name": n, "removed_at": "2000-01-01T09:00:00Z"} for n in removed]}]})
    files = fs.clean_files({"files": [{"twin": tid, "skill": n, "version": version, "files": f}
                                      for n, f in skills.items()]})
    with fleet.locked():
        sk = fs.load()
        fs._own_link(sk, fst)
        fs._take_manifest(sk, man, fs._linked_twins(fst))
        out = fs._settle(fst, sk, files)
        fs.save(sk)
    return out


def _md(text):
    return [{"path": "SKILL.md", "text": text}]


def test_what_is_not_that_skill_is_never_moved_and_a_failed_write_loses_nothing(mini, here, home, monkeypatch):  # noqa: F811
    here.fleet_link(mini.share())
    follow.stop()
    fst = fleet.load()
    tid, entry = next(iter(fs._linked_twins(fst).items()))
    root = home / "profiles" / entry["profile"] / "skills"
    # the owner's own things on this computer: a folder of two skills, a skill with a subfolder, a plain file
    write_skill(root.parent, "research/a", "# a\n")
    write_skill(root.parent, "research/b", "# b\n")
    write_skill(root.parent, "deploy", "# deploy\n", **{"scripts/run.md": "mine\n"})
    (root / "plainfile").write_text("a note\n", encoding="utf-8")
    wants, done = _arrive(fst, tid, {"research": _md("# theirs\n"), "deploy/scripts": _md("# theirs\n"),
                                     "plainfile": _md("# theirs\n"), "fine": _md("# fine 1\n")})
    assert done == 1 and (root / "fine" / "SKILL.md").read_text(encoding="utf-8") == "# fine 1\n"
    assert sorted(p.name for p in (root / "research").iterdir()) == ["a", "b"]
    assert (root / "deploy" / "scripts" / "run.md").read_text(encoding="utf-8") == "mine\n"
    assert not (root / "deploy" / "scripts" / "SKILL.md").exists()
    assert (root / "plainfile").read_text(encoding="utf-8") == "a note\n" and fs.load()["kept"] == []
    rows = {s["key"]: s for t in fs.view(here)["twins"] for s in t["skills"]}
    assert rows["research"]["state"] == "error" and "something else on this computer is called research" in rows["research"]["label"]
    assert rows["deploy"]["state"] == "only_here" and rows["fine"]["state"] == "in_sync"

    # the last step of a write fails (a full disk, a file held open): the skill is exactly as it was
    def no(*_a):
        raise PermissionError("held open")
    really = fs._move_in
    monkeypatch.setattr(fs, "_move_in", no)
    wants, done = _arrive(fst, tid, {"fine": _md("# fine 2\n")}, version=2)
    assert done == 0 and (root / "fine" / "SKILL.md").read_text(encoding="utf-8") == "# fine 1\n"
    assert fs.load()["kept"] == [] and not [p for p in root.iterdir() if p.name.startswith(".gbt-")]
    assert next(s for t in fs.view(here)["twins"] for s in t["skills"] if s["key"] == "fine")["state"] == "error"
    monkeypatch.setattr(fs, "_move_in", really)  # (only this one setting goes back: the test keeps its own
    #                                               Hermes folder)
    wants, done = _arrive(fst, tid, {"fine": _md("# fine 2\n")}, version=2)
    assert done == 1 and (root / "fine" / "SKILL.md").read_text(encoding="utf-8") == "# fine 2\n"
    assert len(fs.load()["kept"]) == 1  # ... and tried again, it goes through, the old one kept

    # the owner's own text, set aside by the owner's choice, outlives any number of later versions
    (root / "fine" / "SKILL.md").write_text("# fine, mine\n", encoding="utf-8")
    with fleet.locked():
        sk = fs.load()
        sk["fol"]["twins"][tid]["skills"]["fine"]["take"] = 3  # what "Keep the other computer's" notes
        fs.save(sk)
    assert _arrive(fst, tid, {"fine": _md("# fine 3\n")}, version=3)[1] == 1
    own = fs.load()["kept"][0]
    assert own["own"] is True
    for n in range(4, 4 + fs.KEEP_VERSIONS + 3):
        assert _arrive(fst, tid, {"fine": _md(f"# fine {n}\n")}, version=n)[1] == 1
    kept = fs.load()["kept"]
    assert own["vid"] in [k["vid"] for k in kept] and sum(1 for k in kept if not k.get("own")) == fs.KEEP_VERSIONS
    oldest = [k for k in kept if not k.get("own")][-1]["vid"]
    fs.restore(here, tid, "fine", oldest)  # putting the oldest kept one back does not prune it away first
    assert (root / "fine" / "SKILL.md").read_text(encoding="utf-8").startswith("# fine ")
    out = fs.restore(here, tid, "fine", own["vid"])
    assert (root / "fine" / "SKILL.md").read_text(encoding="utf-8") == "# fine, mine\n" and out["undo"]
    follow.stop()
