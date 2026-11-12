"""Suspected secrets: detection, and the demo's fake key never lands anywhere outside the pretend Grok app."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

from twin_testlib import (all_text_under, central, cli, dumps, fake_key, home, secrets, slash, svc,  # noqa: F401
                          tools)


def test_detects_key_formats_and_blocks():
    samples = {
        "api_key": "sk-" + "a1B2" * 6,
        "github_token": "gh" + "p_" + "A" * 36,
        "aws_key": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "hf_token": "hf" + "_" + "x1" * 15,
        "stripe_key": "sk" + "_live_" + "Z9" * 10,
    }
    for kind, value in samples.items():
        found = secrets.detect_secrets(f"line one\nuse {value} here")
        assert found and found[0]["kind"] == kind and found[0]["line"] == 2, kind
        assert value not in json.dumps(found)
    block = "-----BEGIN " + "PRIVATE KEY-----\nMIIBVQ\nabc\n-----END " + "PRIVATE KEY-----"
    assert secrets.detect_secrets("x\n" + block)[0]["kind"] == "private_key"
    assert "MIIBVQ" not in secrets.redact("x\n" + block)
    rnd = "Q7f" + "k2LmZ9pX4vR8sT1wY6bN3cH5jD0gA" + "e"
    assert secrets.detect_secrets(f"desc {rnd}")[0]["kind"] == "random_word"
    assert secrets.detect_secrets("deadbeef" * 5)[0]["kind"] == "random_word"


def test_no_false_positives_on_fixtures():
    from twin_testlib import demo
    for fx in demo._GROK_FIXTURES.values():
        for text in [fx["name"], fx["description"], fx["instructions"], *fx["skills"].values()]:
            assert secrets.detect_secrets(text) == [], text


def test_withhold_reports_never_carry_value():
    key = fake_key()
    hold, reps = secrets.withhold(f"Test key: {key}", "persona.description", "description")
    assert hold and reps[0]["item"] == "persona.description" and reps[0]["direction"] == "g2h"
    assert "looks like a password or key" in reps[0]["location"] and "this check can miss things" in reps[0]["location"]
    assert key not in dumps(reps)
    assert secrets.held_back_text(1) == "1 thing that looks like a password or key was not copied"
    assert secrets.held_back_text(2) == "2 things that look like a password or key were not copied"


def test_demo_secret_value_absent_everywhere(svc, home):
    from twin_testlib import load_plugin
    key = fake_key()
    svc.demo_action("start")
    svc.demo_action("secret")
    # the pretend Grok app does hold the key in memory (this is what the check must catch)
    assert key in svc.demo._grok("b-acme-0001")["description"]
    res = svc.sync()
    held = res["pairs"][0]["secrets_withheld"]
    assert held and held[0]["item"] == "persona.description"
    assert "looks like a password or key" in held[0]["location"]
    st = svc.status(fresh=True)
    p = st["pairs"][0]
    assert p["state"] == "needs_you"
    assert p["state_reason"] == "1 thing that looks like a password or key was not copied"
    ev = [e for e in svc.events()["events"] if e["type"] == "secrets_withheld"]
    assert ev and ev[-1]["payload"]["message"] == "1 thing that looks like a password or key was not copied"
    assert ev[-1]["payload"]["note"] == "this check can miss things"
    # every surface: sync result, status, events, conflicts, restore points, recent, parity, previews
    surfaces = [res, st, svc.events(), svc.conflicts("acme-support"), svc.restore_points("acme-support"),
                svc.recent("acme-support"), svc.parity("acme-support"), svc.checklist("acme-support"),
                svc.grok_list(), svc.preview(["b-acme-0001"]), svc.digest_preview("acme-support")]
    for s in surfaces:
        assert key not in dumps(s)
    assert key not in tools.twin_status({}) and key not in tools.twin_recent({})
    assert key not in slash.handle("", platform="cli") and key not in slash.handle("sync", platform="cli")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        for argv in (["status", "--json"], ["sync"], ["recent"], ["list-grok"], ["parity"], ["checklist"]):
            cli.main(argv)
    assert key not in out.getvalue() + err.getvalue()
    # nothing on disk under the plugin's data folder (incl. the pretend Grok app) holds the value
    text = all_text_under(central(home))
    assert text and key not in text
    assert key not in all_text_under(home)
    # the twin's description stayed the clean one
    assert key not in (svc.demo._twin("acme-support").get("description") or "")
    assert "acme-support" in svc.pair_ids()


def test_secret_in_conflict_is_hidden(svc):
    key = fake_key()
    svc.demo_action("start")
    svc.demo_action("conflict")
    t = svc.demo._twin("acme-support")
    t["skills"]["greet-visitors"]["text"] += f"\nkey {key}"
    svc.demo._write_twin("acme-support", t)
    svc.sync()
    c = dumps(svc.conflicts("acme-support"))
    assert key not in c and "not shown: looks like a password or key" in c


def test_names_and_labels_are_masked_too():
    from twin_testlib import safe
    key = fake_key()
    for raw in (f"Acme {key}", key, f"routine:{key}"):
        assert key not in safe.clean_name(raw, 200) and "[secret]" in safe.clean_name(raw, 200)
        assert key not in safe.clean_summary(raw, 200)
    assert safe.llm_name(key) == safe.TWIN_FALLBACK  # key-shaped yet inside the plain name charset
    assert safe.clean_name("Acme Support") == "Acme Support"
