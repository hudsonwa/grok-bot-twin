"""R7: one state machine and one status sentence.

A twin's state follows its counters (an engine's own state that contradicts them is not trusted), and its sentence,
shape (`tone`), `needs` and `ready` are computed in one place, twin/model.py `view`. The terminal prints them and
the page shows the fields the server sends (tests/desktop/check.mjs serves every fixture through the same Python
`view`, so its ~40 sentence assertions are assertions on this one function). The page keeps no coverage table of
its own either."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")


def _pair(**kw):
    from hermes_plugins.grok_bot_twin.twin.model import normalize_pair
    return normalize_pair({"pair_id": "test-scout", "grok": {"name": "Test Scout"}, **kw})


def test_R7_an_engine_state_that_contradicts_the_counters_is_overridden():
    assert _pair(state="in_step", conflicts=2)["state"] == "needs_you"
    assert _pair(state="in_step", pending={"to_hermes": 1})["state"] == "changes_waiting"
    assert _pair(state="needs_you")["state"] == "in_step"
    assert _pair(state="changes_waiting")["state"] == "in_step"
    gone = _pair(state="grok_missing")
    assert gone["state"] == "grok_missing" and gone["grok"]["present"] is False and gone["tone"] == "muted"
    # what no counter can say is kept: nothing could be read
    err = _pair(state="error", conflicts=1, state_reason="botport did not answer")
    assert (err["state"], err["state_reason"], err["sentence"], err["tone"]) == (
        "error", "botport did not answer", "Can't check right now", "bad")


def test_R7_the_cli_and_the_page_show_the_one_sentence():
    from hermes_plugins.grok_bot_twin.twin import cli
    from hermes_plugins.grok_bot_twin.twin.model import derive_state, status_sentence, view
    p = _pair(state="in_step", waiting=[{"item": "skill:greet-visitors", "to": "grok"}],
              pending={"to_grok": 2}, setup_left=["model"])
    assert p["sentence"] == "3 changes to send to Grok · 1 thing left" == status_sentence(p)
    assert (p["tone"], p["needs"], p["ready"]) == ("send", "send", False)
    assert p["sentence"] in cli._pair_line(p)
    assert cli._header([p]) == "1 twin, 1 with a change to send to Grok, 1 to finish setting up."
    # a caller that changes the counters re-derives the state, and the sentence moves with it
    p["pending"]["to_grok"], p["waiting"] = 0, []
    p["state"], p["state_reason"] = derive_state(p)
    assert (p["state"], p["sentence"], p["tone"]) == ("in_step", "Almost ready · 1 thing left", "todo")
    assert view(p)["sentence"] == p["sentence"]
    # the page has no sentence logic left: it shows the server's field, with only its own two overlays
    body = PAGE[PAGE.index("function statusSentence("):PAGE.index("const TONES = ")]
    assert "pair.sentence" in body and "mattersOf" not in body
    for words in ("things need you", "Almost ready", "on the way to Grok", "to send to Grok', "):
        assert words not in PAGE, words
    assert "isToSend" not in PAGE and "choresLeft" not in PAGE
    # one word for "changes on their way to Hermes" everywhere
    from hermes_plugins.grok_bot_twin.twin.model import STATE_LABELS
    assert f"changes_waiting: {{ label: '{STATE_LABELS['changes_waiting']}'" in PAGE


def test_R7_the_page_uses_the_servers_coverage_table():
    from hermes_plugins.grok_bot_twin.twin.model import DEFAULT_COVERAGE, coverage
    assert "DEFAULT_COVERAGE" not in PAGE and "COVERAGE_ALIAS" not in PAGE
    assert not re.search(r"\{ g2h: '\w+', h2g: '\w+', item: '\w+' \}", PAGE)
    assert coverage(False) == DEFAULT_COVERAGE
    assert _pair()["coverage"] == DEFAULT_COVERAGE
