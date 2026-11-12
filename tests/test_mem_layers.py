"""The shared core of the memory layers (twin/mem_layers.py) and how facts plug into the rest of the plugin.

Switches (off by default, the "What syncs" panel's choice wins when the build has it), the 0.2 sealed box with one
key per layer between the owner's computers (attack tests: tamper, replay into another layer, wrong link, reading
the bytes), the transport hook for the Mirror, the background tick, and `hermes twin facts`. Invented data only.
"""
from __future__ import annotations

import base64
import json
import sys
import types

import pytest

from twin_testlib import fake_key, home  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import mem_facts as mf
from hermes_plugins.grok_bot_twin.twin import mem_layers as ml

P = "test-scout"


@pytest.fixture(autouse=True)
def _own_switches(monkeypatch):
    """These tests drive the layer's own switches (by profile); with the What syncs panel in the build the owner's
    choice comes from the panel instead (tests/test_sync_panel.py and the panel tests below cover that)."""
    monkeypatch.setattr(ml, "_panel", lambda: None)
LINK = bytes(range(32))
MID = "m-" + "a" * 16


# ---- switches ----------------------------------------------------------------------------------------------------------
def test_every_layer_starts_off(home):
    for layer in ml.LAYERS:
        assert ml.direction(P, layer) == "off"
        assert not ml.allowed(P, layer, "to_hermes") and not ml.allowed(P, layer, "to_grok")
    ml.set_direction(P, "facts", "grok_to_hermes")
    assert ml.allowed(P, "facts", "to_hermes") and not ml.allowed(P, "facts", "to_grok")
    assert ml.direction(P, "briefs") == "off" and ml.direction("test-quill", "facts") == "off"
    ml.set_direction(P, "facts", "off")
    assert not ml.any_on()


def test_panel_choice_wins_when_present(home, monkeypatch):
    calls = []
    fake = types.SimpleNamespace(
        wanted=lambda pid, key: {"memory_facts": "both"}.get(key) if pid == "test-scout" else None,
        mark_synced=lambda pid, key, way: calls.append((pid, key, way)),
        provide=lambda key, **kw: calls.append(("provide", key)))
    monkeypatch.setattr(ml, "_panel", lambda: fake)
    ml.set_direction(P, "briefs", "both")  # the fallback store is not believed while the panel is there
    assert ml.direction(P, "facts") == "off"  # saved "both" counts only while the Mirror can carry it
    ml.set_transport_factory(lambda prof: ml.StubTransport(prof))
    try:
        assert ml.direction(P, "facts") == "both"
    finally:
        ml.set_transport_factory(None)
    assert ml.direction(P, "briefs") == "off"
    assert ml.direction("test-quill", "facts") == "off"
    assert mf.register_panel()
    assert ("provide", "memory_facts") in calls
    ml.mark_synced(P, "facts", "grok_to_hermes")
    assert (P, "memory_facts", "grok_to_hermes") in calls


def test_panel_directions_need_the_mirror(home):
    assert mf._panel_dirs(None) == {"grok_to_hermes": "needs_mirror", "hermes_to_grok": "needs_mirror",
                                    "both": "needs_mirror", "off": "ok"}
    ml.set_transport_factory(lambda prof: ml.StubTransport(prof))
    try:
        assert mf._panel_dirs(None)["both"] == "ok"
        assert ml.mirror_ready()
    finally:
        ml.set_transport_factory(None)
    assert not ml.mirror_ready()
    for d in ml.DIRECTIONS:
        assert mf._panel_sentence(None, d)


# ---- the sealed box between computers ----------------------------------------------------------------------------------
NOTE = {"facts": ["Prefers short answers.", "Owner keeps a garden."], "secret_marker": "ZEBRA-41c7"}


def test_box_round_trip_and_unreadable_bytes():
    box = ml.seal(LINK, "briefs", "ask", MID, "bind-1", NOTE)
    assert ml.unseal(LINK, "briefs", "ask", MID, "bind-1", box) == NOTE
    raw = base64.b64decode(box["box"])
    assert b"ZEBRA" not in raw and b"garden" not in raw and b"ZEBRA" not in json.dumps(box).encode()
    assert len(raw) % 1024 == 0


@pytest.mark.parametrize("change", ["layer", "way", "machine", "bind", "link", "tamper", "nonce", "shape"])
def test_box_refuses_anything_not_made_for_this_use(change):
    box = ml.seal(LINK, "transcripts", "ask", MID, "bind-1", NOTE)
    args = dict(link=LINK, layer="transcripts", way="ask", machine=MID, bind="bind-1")
    if change == "layer":
        args["layer"] = "briefs"
    elif change == "way":
        args["way"] = "answer"
    elif change == "machine":
        args["machine"] = "m-" + "b" * 16
    elif change == "bind":
        args["bind"] = "bind-2"
    elif change == "link":
        args["link"] = bytes(32)
    elif change == "tamper":
        raw = bytearray(base64.b64decode(box["box"]))
        raw[10] ^= 1
        box = {**box, "box": base64.b64encode(bytes(raw)).decode()}
    elif change == "nonce":
        box = {**box, "nonce": "00" * 24}
    elif change == "shape":
        box = ["not", "a", "box"]
    assert ml.unseal(args["link"], args["layer"], args["way"], args["machine"], args["bind"], box) is None


def test_layer_keys_differ_and_differ_from_02_memory():
    from hermes_plugins.grok_bot_twin.twin import xs_memory
    keys = {ml.layer_key(LINK, layer) for layer in ml.LAYERS}
    assert len(keys) == 3
    box = ml.seal(LINK, "facts", "ask", MID, "b", NOTE)
    assert xs_memory.unseal(LINK, "ask", MID, "b", box) is None  # a layer box is not a 0.2 memory box
    with pytest.raises(ValueError):
        ml.layer_key(LINK, "everything")


# ---- privacy helpers ---------------------------------------------------------------------------------------------------
def test_mask_secrets_for_local_archives():
    text = "Hello.\nThe wifi password is " + "trail" + "4821x\nkey: " + fake_key() + "\nBye."
    out = ml.mask_secrets(text)
    assert "4821" not in out and fake_key() not in out
    assert out.startswith("Hello.\n") and out.endswith("\nBye.")
    assert ml.held("Prefers short answers.") is None
    assert ml.held_words({"secret": 1, "order": 2}) == [
        "2 items: reads like an instruction, not a note", "1 item: looks like it holds a password or key"]


# ---- background tick, followers ----------------------------------------------------------------------------------------
class _Svc:
    def status(self):
        return {"pairs": [{"pair_id": "test-scout", "hermes": {"profile": P}},
                          {"pair_id": "sample", "demo": True, "hermes": {"profile": "sample"}},
                          {"pair_id": "bad", "hermes": {"profile": "../x"}}]}

    def pair_ids(self):
        return ["test-scout"]


def test_tick_does_nothing_without_the_mirror(home, monkeypatch):
    (home / "profiles" / P / "memories").mkdir(parents=True)
    (home / "profiles" / P / "memories" / "MEMORY.md").write_text("Likes trail maps.")
    ml.set_direction("test-scout", "facts", "both")
    mf._last_tick[0] = 0.0
    mf.tick(lambda: _Svc())
    assert ml.StubTransport(P).read_facts() == []
    ml.set_transport_factory(lambda prof: ml.StubTransport(prof))
    try:
        mf._last_tick[0] = 0.0
        mf.tick(lambda: _Svc())
        assert [f["text"] for f in ml.StubTransport(P).read_facts()] == ["Likes trail maps."]
        before = ml.StubTransport(P).outbox()
        mf.tick(lambda: _Svc())  # within 15 minutes: nothing again
        assert ml.StubTransport(P).outbox() == before
    finally:
        ml.set_transport_factory(None)


def test_pairs_skip_sample_and_bad_profiles():
    assert mf._pairs(_Svc()) == {"test-scout": P}


def test_follower_never_merges_with_grok(home, monkeypatch):
    monkeypatch.setattr(mf, "runs_here", lambda: False)
    ml.set_direction("test-scout", "facts", "both")
    assert mf.sync_twin(_Svc(), "test-scout") == {"state": "follower"}
    with pytest.raises(ml.LayerError):
        mf.sync_twin(_Svc(), "nobody")


# ---- terminal ----------------------------------------------------------------------------------------------------------
def test_cli_facts(home, monkeypatch, capsys):
    from hermes_plugins.grok_bot_twin.twin import cli
    (home / "profiles" / P / "memories").mkdir(parents=True)
    (home / "profiles" / P / "memories" / "MEMORY.md").write_text("Likes trail maps.")
    monkeypatch.setattr(cli, "_svc", lambda: _Svc())
    monkeypatch.setattr(ml, "_panel", lambda: None)
    assert cli.main(["facts", "--json"]) == 0
    v = json.loads(capsys.readouterr().out)
    assert v["facts"]["direction"] == "off"
    assert cli.main(["facts", "way", "both"]) == 0
    ml.StubTransport(P)._put("facts.json", [{"id": "m1", "text": "Prefers short answers.", "kind": "memory"}])
    capsys.readouterr()
    assert cli.main(["facts", "test-scout", "sync"]) == 0
    out = capsys.readouterr().out
    assert "Sync: synced. Into Hermes: 1 added" in out and "To Grok: 1 added" in out
    assert "Prefers short answers." in (home / "profiles" / P / "memories" / "MEMORY.md").read_text()
    assert cli.main(["facts", "keep", "c0000000000", "grok"]) != 0
