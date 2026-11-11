"""The Grok side: plain answers on the computer that has the Grok app.

- "Use it" on a release this plugin version has no table for: the page's plain "verify" answers in seconds, in the
  owner's words, with one next act and no developer tool's name
- a description changed in Hermes is handed out and noted as pasted like a skill is
- every "Compare with Grok" row carries one plain sentence (`why`); botport's own text stays in `detail`
- `hermes twin status` and /twin status say the sentence the twin's row on the page says
- the Stop twinning dialog loads without botport and names the button that is on the page
- Remove Grok Bot Twin says how many twin links, and which

Invented bots only. "botport" is tests/fake_botport.py; GitHub's tool is a script of the test's own.
"""
from __future__ import annotations

import json
import re
import time

import pytest

from twin_testlib import central, config, fake_key, home, service, slash  # noqa: F401
from test_ux_flows import P, _client, _pair, _write_exe, fake  # noqa: F401
from test_setup_download_and_twin_facts import SOUL, err, mode, pin, profile, twin  # noqa: F401
from test_cli_ux import run
from hermes_plugins.grok_bot_twin.twin import engine as E, hermes_side as hs, model
from hermes_plugins.grok_bot_twin.twin import service as S

DEV_WORDS = re.compile(r"(?i)\bgh\b|\bbrew\b|github|command-line|terminal|attest|allow-unattested|\bPATH\b")
L1_WORDS = re.compile(r"(?i)botport|mirror|parity|profile|sync bot|\bpair\b|helper|config|\bid\b|token|json|yaml")


def _plain(text: str, most: int = 22) -> None:
    """One sentence group for a list on the page: short, no brackets, no command, no engine word."""
    assert text and len(text.split()) <= most, text
    assert not re.search(r"[()\[\]`]", text) and not L1_WORDS.search(text), text


# ---- verifying a release that is not in the plugin's table ---------------------------------------------------------
def test_the_pages_plain_verify_of_an_unpinned_release_answers_in_the_owners_words(fake, home, monkeypatch):
    pin({}, {})  # this build's table: the release is named, no digests
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # GitHub's tool is not here
    svc = fake.svc
    t0 = time.monotonic()
    e = err(svc.trust_botport, False, None, None, True)
    assert time.monotonic() - t0 < 10
    assert e.code == "not_pinned"
    assert e.message == f"This version of Grok Bot Twin cannot verify botport {E.EXPECTED_BOTPORT_TAG[1:]} by itself."
    assert e.hint == ("Update Grok Bot Twin in Capabilities → Plugins, then press Verify again. Another way is "
                      "under For developers.")
    assert not DEV_WORDS.search(e.message) and not DEV_WORDS.search(e.hint)
    assert fake.calls() == [] and not (central(home) / "botport-trust.json").exists()  # nothing ran, nothing kept
    # the terminal and "For developers" keep their own, more exact answers
    assert err(svc.trust_botport).code == "gh_missing"
    assert err(svc.trust_botport, False, None, "gh", True).code == "gh_missing"
    # the owner's own opt-out for a local build is not turned into a quick ask
    assert svc.trust_botport(True, None, None, True)["trusted"] is True


def test_the_plain_verify_never_waits_long_for_githubs_tool(fake, home, tmp_path, monkeypatch):
    pin({}, {})
    monkeypatch.setattr(E, "QUICK_ATTEST_TIMEOUT_S", 1.0)
    # a tool that is there but does not answer (no network, not signed in and asking): seconds, then the plain answer
    _write_exe(tmp_path / "ghbin" / "gh", "import time\ntime.sleep(30)\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'ghbin'}:/usr/bin:/bin")
    t0 = time.monotonic()
    e = err(fake.svc.trust_botport, False, None, None, True)
    assert time.monotonic() - t0 < 10 and e.code == "not_pinned" and not DEV_WORDS.search(e.message + e.hint)
    # a tool that is not signed in: the same plain answer, never "run gh auth login"
    _write_exe(tmp_path / "ghbin" / "gh", "import sys\nsys.stderr.write('run gh auth login\\n'); sys.exit(4)\n")
    e = err(fake.svc.trust_botport, False, None, None, True)
    assert e.code == "not_pinned" and "auth" not in e.hint
    # a tool that looked at the file and said no: the download is the thing to replace
    _write_exe(tmp_path / "ghbin" / "gh", "import sys\nsys.stderr.write('verification: fake\\n'); sys.exit(1)\n")
    e = err(fake.svc.trust_botport, False, None, None, True)
    assert (e.code, e.hint) == ("check_failed", "Press Download botport and choose the new file.")
    assert not DEV_WORDS.search(e.message + e.hint)
    # and one that says yes verifies it
    _write_exe(tmp_path / "ghbin" / "gh", "import sys\nsys.exit(0)\n")
    assert fake.svc.trust_botport(False, None, None, True)["trusted"] is True


def test_the_rest_route_asks_the_plain_way(fake, home, monkeypatch):
    pin({}, {})
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    c = _client(monkeypatch)
    r = c.post(P + "/engine/trust", json={})
    body = r.json()["error"]
    assert r.status_code == 200 and body["code"] == "not_pinned" and not DEV_WORDS.search(json.dumps(body))
    assert c.post(P + "/engine/trust", json={"method": "gh"}).json()["error"]["code"] == "gh_missing"


# ---- the description is handed out and noted as pasted -------------------------------------------------------------
DESCRIPTION_WAITS = {"state": "changes_waiting",
                     "pending": {"to_hermes": 0, "to_grok": 1, "in_flight": 0, "waiting_approval": 0}, "waiting": [],
                     "by_hand": [{"item": "persona.description", "side": "grok",
                                  "what": "Copy the Hermes version into Grok"}]}


def test_a_description_changed_in_hermes_is_handed_out_and_noted_as_pasted(twin, home):
    svc = twin
    soul = home / "profiles" / "acme-support" / "SOUL.md"
    assert err(svc.skill_text, "acme-support", "description").message == ("The description is not waiting to be "
                                                                         "copied to Grok.")
    # "Done, I pasted it" when the plugin has already seen the text in Grok (the usual order: paste in Grok, come
    # back, press Done): answered as done, nothing written. Before, this was a red "Could not note that".
    for asked in ("description", "persona.description", "skill:no-such"):
        assert svc.skip("acme-support", asked, copied=True) == {"pair_id": "acme-support", "skipped": []}
    assert not (central(home) / "copied.json").exists() and not (central(home) / "skipped.json").exists()
    mode(home, status=DESCRIPTION_WAITS)
    p = _pair(svc)
    assert p["pending"]["to_grok_items"] == ["persona.description"] and p["pending"]["to_grok"] == 1
    for asked in ("description", "persona.description"):  # the page names it by its kind
        got = svc.skill_text("acme-support", asked)
        assert got == {"pair_id": "acme-support", "item": "persona.description", "name": "description",
                       "text": "Answers questions about Acme widgets.", "truncated": False}
    assert err(svc.skill_text, "acme-support", "name").code == "not_found"
    # the same check for passwords and keys as for a skill
    plain = soul.read_text(encoding="utf-8")
    soul.write_text(plain.replace("Acme widgets.", f"Acme widgets. Key {fake_key()}"), encoding="utf-8")
    e = err(svc.skill_text, "acme-support", "description")
    assert e.code == "looks_like_secret" and "description" in e.message and fake_key() not in e.message + e.hint
    soul.write_text(plain, encoding="utf-8")
    # "Done, I pasted it": it stops asking, and is not filed as kept in Hermes only
    out = svc.skip("acme-support", "description", copied=True)
    assert out["skipped"] == []
    p = _pair(svc)
    assert p["pending"]["to_grok"] == 0 and p["pending"]["to_grok_items"] == [] and p["skipped"] == []
    assert not [b for b in p["by_hand"] if b.get("item") == "persona.description"]
    assert json.loads((central(home) / "copied.json").read_text()) == {
        "acme-support": {"persona.description": hs.text_digest("Answers questions about Acme widgets.")}}
    # the description changes in Hermes later: it asks again and the note is forgotten
    soul.write_text(plain.replace("Acme widgets.", "Acme gadgets."), encoding="utf-8")
    p = _pair(svc)
    assert p["pending"]["to_grok_items"] == ["persona.description"]
    assert not json.loads((central(home) / "copied.json").read_text())
    assert svc.skill_text("acme-support", "description")["text"] == "Answers questions about Acme gadgets."


def test_the_description_route(twin, home, monkeypatch):
    mode(home, status=DESCRIPTION_WAITS)
    c = _client(monkeypatch)
    assert c.get(P + "/status", params={"fresh": "true"}).status_code == 200  # (the page has looked since)
    _pair(twin)
    r = c.get(P + "/pairs/acme-support/skill-text", params={"item": "description"}).json()
    assert r["name"] == "description" and r["text"] == "Answers questions about Acme widgets."
    r = c.post(P + "/pairs/acme-support/skip", json={"item": "description", "copied": True}).json()
    assert r == {"pair_id": "acme-support", "skipped": []}


# ---- one plain sentence per comparison row -------------------------------------------------------------------------
def test_every_comparison_row_has_a_plain_sentence_and_botports_text_stays_in_detail(twin, home):
    res = twin.parity("acme-support")
    assert res["checks"] and all(isinstance(c.get("why"), str) for c in res["checks"])
    for c in res["checks"]:
        _plain(c["why"])
    by = {c["id"]: c for c in res["checks"]}
    assert by["persona.instructions"]["why"] == ("Not brought over yet. The Grok app does not keep the instructions "
                                                "on this computer.")
    assert by["skill:greet-visitors"]["why"] == "The same in Grok and Hermes."
    assert by["skill:greet-visitors"]["detail"] == "same"  # botport's own text, unchanged
    # botport's raw text never reaches `why`
    raw = "not carried: Grok side: set in the Grok app (the bot's settings) (listed to apply: botport mirror status)"
    assert S.parity_why({"id": "persona.description", "name": "description", "pass": False, "detail": raw}) == (
        "Differs between Grok and Hermes. To change it in Grok, paste it in the Grok app yourself.")
    rows = [{"id": i, "pass": ok, "detail": raw} for ok in (True, False)
            for i in ("persona.name", "persona.description", "persona.instructions", "skill:pick-a-book",
                      "routine:Morning summary", "connector:Calendar", "something-new")]
    rows.append({"id": "model", "pass": False, "action": "choose_model", "detail": raw})
    for row in rows:
        _plain(S.parity_why(row))
    assert S.parity_why({"id": "routine:Morning summary", "pass": False}).startswith("Not in Hermes yet.")


def test_the_samples_comparison_rows_have_the_sentence_too(home):
    run(["demo"])
    svc = service.get_service()
    pid = svc.demo.pair_ids()[0]
    for c in svc.parity(pid)["checks"]:
        _plain(c["why"])


# ---- the terminal says the row's sentence --------------------------------------------------------------------------
def _p(**over) -> dict:
    p = {"state": "in_step", "grok": {"name": "Acme Support", "present": True}, "hermes": {"profile": "acme-support"},
         "pending": {"to_hermes": 0, "to_grok": 0, "in_flight": 0, "waiting_approval": 0}, "waiting": [],
         "conflicts": 0, "setup_left": [], "secrets_withheld": [], "secrets_held": 0}
    p.update(over)
    return p


def test_the_row_sentence_is_the_pages():
    s = model.status_sentence
    assert s(_p()) == "Follows Grok · ready"
    assert s(_p(sample=True)) == "Follows Grok"
    assert s(_p(setup_left=["model", "instructions"])) == "Almost ready · 2 things left"
    assert s(_p(state="changes_waiting", pending={"to_grok": 1}, setup_left=["instructions", "model"])) == (
        "1 change to send to Grok · 2 things left")  # a change waiting and set-up left
    assert s(_p(pending={"to_grok": 2}, waiting=[{"item": "skill:pick-a-book", "to": "grok"}])) == (
        "3 changes to send to Grok")
    assert s(_p(state="needs_you", conflicts=1)) == "Changed in both places"
    assert s(_p(state="needs_you", conflicts=2)) == "2 things changed in both places"
    assert s(_p(state="needs_you", conflicts=1, pending={"to_grok": 1}, setup_left=["model"])) == (
        "2 things need you · 1 thing left")
    assert s(_p(waiting=[{"item": "connector:Calendar", "to": "hermes"}])) == "A connection from Grok is ready"
    assert s(_p(secrets_held=1)) == "A password or key was not copied"
    assert s(_p(pending={"to_hermes": 1})) == "Updating…"
    assert s(_p(pending={"in_flight": 2})) == "2 changes on the way to Grok"
    assert s(_p(state="error")) == "Can't check right now"
    assert s(_p(state="grok_missing")) == "Not in Grok any more"
    assert s(_p(switched={"at": "2000-01-01T00:00:00Z", "routines_on": 1})) == "Follows Grok · 1 routine runs in Hermes"
    assert s({}) == "Follows Grok · ready"  # nothing known: never a crash


def test_hermes_twin_status_says_what_the_page_says(twin, home):
    rc, out, _ = run(["status"])
    # the fixture's new twin has two things from Grok waiting for a go-ahead: the row says so, and so does the head
    assert out.splitlines()[0] == "Grok Bot Twin: 1 twin, 1 needs your attention, 1 to finish setting up." and "Acme Support: 2 things need you · 2 things left" in out
    (central(home) / "waiting.json").unlink()  # both dealt with
    rc, out, _ = run(["status"])
    lines = out.splitlines()
    # a new twin: its instructions and its model are still the owner's to do
    assert rc == 0 and lines[0] == "Grok Bot Twin: 1 twin, 1 to finish setting up."
    assert lines[1].startswith(" ● Acme Support: Almost ready · 2 things left · checked "), out
    assert "Up to date" not in out and "Updating" not in out
    mode(home, status=DESCRIPTION_WAITS)
    rc, out, _ = run(["status"])
    assert out.splitlines()[0] == "Grok Bot Twin: 1 twin, 1 with a change to send to Grok, 1 to finish setting up."
    assert " Acme Support: 1 change to send to Grok · 2 things left · checked " in out, out  # the same sentence
    assert "Updating (" not in out
    assert "- Acme Support: 1 change to send to Grok · 2 things left, checked " in slash.handle("status")


# ---- Stop twinning without botport ---------------------------------------------------------------------------------
def test_the_stop_twinning_dialog_loads_without_botport_and_names_the_button_on_the_page(twin, home, monkeypatch):
    svc = twin
    with_botport = svc.remove("acme-support", True)
    assert "note" not in with_botport
    monkeypatch.setattr(svc, "botport", lambda: None)  # botport is gone from this computer
    plan = svc.remove("acme-support", True)
    assert plan["dry_run"] is True and plan["stays"] == with_botport["stays"] and plan["goes"] == with_botport["goes"]
    assert plan["removed"] == [] and plan["again"] == with_botport["again"]
    assert plan["note"] == ("botport is missing, so this cannot be done yet. On the Twins page, press Get botport "
                            "again first.")
    e = err(svc.remove, "acme-support")  # the act itself still needs it, and says the button that is there
    assert e.code == "botport_missing" and "Get botport again" in e.hint and "Connect my Grok bots" not in e.hint
    assert "acme-support" in svc.pair_ids()  # nothing was removed


# ---- Remove Grok Bot Twin says how many, and which -----------------------------------------------------------------
def test_remove_grok_bot_twin_counts_and_names_the_twin_links(twin, home):
    plan = twin.uninstall(True)
    assert plan["goes"][0] == "1 twin (Acme Support)." and "Every twin." not in plan["goes"]
    assert any(x.startswith("state:pairs/acme-support") for x in plan["removed"])  # the Technical details list
    assert S.goes_twins_line(["Trail Guide", "Garden Notes", "Book Club"], 3) == (
        "3 twins (Trail Guide, Garden Notes, Book Club).")
    names = ["Test Scout", "Test Quill", "Trail Guide", "Garden Notes", "Book Club", "Recipe Planner", "Acme Helper"]
    assert S.goes_twins_line(names, 7) == ("7 twins (Test Scout, Test Quill, Trail Guide, Garden Notes, "
                                           "Book Club and 2 more).")
    assert S.goes_twins_line([None, ""], 2) == "2 twins." and S.goes_twins_line([], 0) == "Every twin."
    assert S.goes_versions_line(1) == "Earlier versions of 1 twin."
    assert S.goes_versions_line(2) == "Earlier versions of 2 twins."
    # "link" is the word for two computers: the twins' line does not use it
    for line in (S.goes_twins_line(names, 7), S.goes_twins_line([], 0), S.goes_twins_line(["Acme Support"], 1)):
        assert "link" not in line and re.search(r"(?i)\btwins?\b", line)
    assert re.search(r"(?i)earlier versions", S.goes_versions_line(2))


# ---- what Stop twinning takes out of the Hermes bot's prompt file --------------------------------------------------
def test_stop_twinning_takes_only_the_plugins_own_note_out_of_the_prompt_file(twin, home):
    """Six lines go and no more. The six are the plugin's own note about the owner's computers (two
    markers and four lines); botport's name and description lines and the owner's pasted instructions stay,
    byte for byte."""
    soul = home / "profiles" / "acme-support" / "SOUL.md"
    made = soul.read_text(encoding="utf-8")  # botport's six lines, then the note the plugin wrote when twinning
    at = made.index("<!-- grok-bot-twin:begin fleet -->")
    botport_lines, note = made[:at], made[at:]
    assert botport_lines == SOUL and len(note.splitlines()) == 6
    assert note.splitlines()[0] == "<!-- grok-bot-twin:begin fleet -->"
    assert note.splitlines()[-1] == "<!-- grok-bot-twin:end fleet -->"
    own = ("<!-- grok-bot-twin:begin instructions -->\nYou are Acme Support. Answer questions about Acme widgets.\n"
           "Be brief and kind.\nAlways greet visitors.\n<!-- grok-bot-twin:end instructions -->\n")
    soul.write_text(made + own, encoding="utf-8")
    assert len(soul.read_text(encoding="utf-8").splitlines()) == 17
    twin.remove("acme-support")
    after = soul.read_text(encoding="utf-8")
    assert len(after.splitlines()) == 11 and after == botport_lines + own
