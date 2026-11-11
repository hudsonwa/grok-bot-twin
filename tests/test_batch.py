"""Several twins at once (twin/batch.py): a model for all ticked twins, and their setup exports matched and imported,
dropped on the sheet or saved to Downloads.

Invented bots only (Acme Support, Recipe Planner, Test Scout). Every test has its own HERMES_HOME and HOME (see
conftest.py); "Hermes' command line" is a stand-in that writes the routine list the way Hermes does. Most tests
stand in for the twins' status (three real twins and one sample twin), so no botport runs; the ones that check what
the Twins page says afterwards use the fake botport (tests/fake_botport.py).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from twin_testlib import all_text_under, central, config, home, service  # noqa: F401
from test_ux_flows import _client, _pair, fake  # noqa: F401
from test_setup_download_and_twin_facts import SOUL, profile, twin  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import batch, importer, setup_export as sx, store, yamlio
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

P = "/api/plugins/grok-bot-twin"
TWINS = (("acme-support", "b-acme-0001", "Acme Support"), ("recipe-planner", "b-recipe-0002", "Recipe Planner"),
         ("test-scout", "b-scout-0003", "Test Scout"))
SAMPLE = "sample-trail"
MEMORY = "zebra-memory-canary likes long walks"
FACT = "quokka-fact-canary lives on an invented street"


def bot(name: str, **over) -> dict:
    b = {"name": name, "instructions": f"You are {name}, an invented helper.\nBe brief.",
         "memories": [MEMORY, "second " + MEMORY], "user_facts": [FACT],
         "routines": [{"name": f"{name} morning", "instruction": "Summarise the invented notes.",
                       "trigger": "schedule", "schedule_cron": "0 8 * * 1-5", "timezone": "UTC"}],
         "apps": [{"name": "Widget Desk"}]}
    b.update(over)
    return b


def export(*bots: dict) -> str:
    return json.dumps({"format": "grok-bot-export", "version": 1, "bots": list(bots)})


class Hermes:
    """Stands in for `hermes -p PROFILE cron create --paused` for every profile: writes that profile's routine list
    the way Hermes does."""

    def __init__(self, home: Path, monkeypatch):
        self.home = home
        self.asked: list = []
        monkeypatch.setattr(importer, "cron_create_flags", lambda prof: {"--paused", "--paused-reason", "--timezone"})
        monkeypatch.setattr(importer, "create_paused_cron", self.create)
        monkeypatch.setattr(importer, "pause_cron", lambda *a, **k: (True, ""))

    def file(self, prof: str) -> Path:
        return self.home / "profiles" / prof / "cron" / "jobs.json"

    def jobs(self, prof: str) -> list:
        f = self.file(prof)
        return json.loads(f.read_text())["jobs"] if f.exists() else []

    def create(self, prof, name, schedule, prompt, timezone="", reason=""):
        self.asked.append(prof)
        f = self.file(prof)
        f.parent.mkdir(parents=True, exist_ok=True)
        jobs = self.jobs(prof) + [{"id": f"j{len(self.asked):04d}{prof[:4]}", "name": name, "prompt": prompt,
                                   "enabled": False, "state": "paused", "paused_reason": reason,
                                   "schedule": {"kind": "cron", "expr": schedule, "display": schedule}}]
        f.write_text(json.dumps({"jobs": jobs}))
        return True, ""


@pytest.fixture
def world(home, monkeypatch):
    """Three real twins (their Hermes folders, no model chosen) and one sample twin, with the twins' status stood
    in for (no botport runs)."""
    svc = service.get_service()
    entries = []
    for pid, gid, name in TWINS:
        profile(home, pid)
        entries.append({"pair_id": pid, "grok": {"id": gid, "name": name}, "hermes": {"profile": pid},
                        "created_at": "2026-11-11T00:00:00Z"})
    store.atomic_write_json(central(home) / "pairs.json", {"pairs": entries})
    monkeypatch.setattr(svc.demo, "pair_ids", lambda: [SAMPLE])

    def status(fresh=False, allow_engine=True):
        pairs = [{"pair_id": SAMPLE, "demo": True, "grok": {"id": "b-sample-0009", "name": "Trail Guide"},
                  "hermes": {"profile": "trail-guide"}, "setup_left": ["instructions"]}]
        for e in svc._registry().values():
            pairs.append({"pair_id": e["pair_id"], "grok": dict(e["grok"]), "hermes": dict(e["hermes"]),
                          "setup_left": ["instructions", "model"]})
        return {"pairs": pairs}

    monkeypatch.setattr(svc, "status", status)
    return svc


def nothing_kept(home: Path) -> None:
    everything = all_text_under(home)
    for bad in ("canary", "zebra", "quokka"):
        assert bad not in everything, bad


def downloads() -> Path:
    d = Path(os.path.expanduser("~")) / "Downloads"
    d.mkdir(exist_ok=True)
    return d


def save(name: str, text: str, mtime: float | None = None) -> Path:
    f = downloads() / name
    f.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


# =====================================================================================================================
# matching files to twins
# =====================================================================================================================

def test_files_are_matched_by_name_and_by_id(world):
    files = [{"name": "acme.json", "text": export(bot("acme support"))},  # (the name in another case)
             {"name": "renamed.json", "text": export(bot("Recipe Planner 2", id="b-recipe-0002"))},
             {"name": "nobody.json", "text": export(bot("Someone Else"))},
             {"name": "notes.txt", "text": "hello, this is not an export"}]
    got = world.batch_match(files, ["acme-support", "recipe-planner"])["matches"]
    assert [(m["file"], m["pair_id"], m["state"]) for m in got] == [
        ("acme.json", "acme-support", "matched"), ("renamed.json", "recipe-planner", "matched"),
        ("nobody.json", None, "no_twin"), ("notes.txt", None, "not_export")]
    assert got[0]["name"] == "Acme Support" and got[0]["bot"] == "acme support"
    assert got[1]["bot"] == "Recipe Planner 2" and got[1]["name"] == "Recipe Planner"
    assert got[2]["bot"] == "Someone Else" and got[2]["why"] == batch.NO_TWIN_WHY
    assert got[3]["why"] == sx.NOT_AN_EXPORT[0]
    for m in got:
        assert set(m) == {"file", "pair_id", "name", "bot", "state", "why"}
    # a twin that is not ticked is not matched
    got = world.batch_match(files[:1], ["recipe-planner"])["matches"]
    assert got[0]["state"] == "no_twin"
    # the text of a file is never in an answer
    assert "invented helper" not in json.dumps(world.batch_match(files, None))


def test_a_file_with_several_bots_gives_one_match_per_ticked_twin(world):
    text = export(bot("Acme Support"), bot("Recipe Planner"), bot("Not Twinned"))
    got = world.batch_match([{"name": "all.json", "text": text}], ["acme-support", "recipe-planner"])["matches"]
    assert [(m["pair_id"], m["bot"], m["state"]) for m in got] == [
        ("acme-support", "Acme Support", "matched"), ("recipe-planner", "Recipe Planner", "matched")]
    got = world.batch_match([{"name": "all.json", "text": text}], ["test-scout"])["matches"]
    assert got == [{"file": "all.json", "pair_id": None, "name": "", "bot": "", "state": "no_twin",
                    "why": batch.NO_TWIN_MANY.format(n=3)}]


def test_two_files_for_one_twin_and_two_twins_of_one_name(world):
    files = [{"name": "new.json", "text": export(bot("Acme Support"))},
             {"name": "old.json", "text": export(bot("Acme Support"))}]
    got = world.batch_match(files, None)["matches"]
    assert [(m["state"], m["why"]) for m in got] == [("matched", ""), ("ambiguous", batch.DUP_WHY)]
    # a second twin of a bot with the same name: the file says which by name only, so neither is guessed
    reg = world._registry()
    reg["test-scout"]["grok"]["name"] = "Acme Support"
    world._save_registry(reg)
    got = world.batch_match(files[:1], None)["matches"]
    assert [(m["pair_id"], m["state"]) for m in got] == [(None, "ambiguous")]
    # an id in the file settles it
    got = world.batch_match([{"name": "id.json", "text": export(bot("Acme Support", id="b-scout-0003"))}],
                            None)["matches"]
    assert [(m["pair_id"], m["state"]) for m in got] == [("test-scout", "matched")]


def test_bad_requests_are_refused(world):
    for files in (None, [], "x", [{"name": "a"}], [{"name": 3, "text": "x"}], [{"name": "a", "text": "x"}] * 41):
        with pytest.raises(EngineError) as ei:
            world.batch_match(files, None)
        assert ei.value.code == "bad_input"
    with pytest.raises(EngineError) as ei:
        world.batch_match([{"name": "a", "text": "x"}], ["../etc"])
    assert ei.value.code == "bad_input"
    with pytest.raises(EngineError) as ei:
        world.batch_match([{"name": "a", "text": "x"}], ["no-such-twin"])
    assert ei.value.code == "pair_not_found"
    # an untrusted file name is made one plain line and capped
    got = world.batch_match([{"name": "a‮\nb" + "x" * 500, "text": "x"}], None)["matches"][0]
    assert "\n" not in got["file"] and "‮" not in got["file"] and len(got["file"]) <= batch.FILE_NAME_CAP


# =====================================================================================================================
# importing them
# =====================================================================================================================

def test_import_saves_instructions_and_adds_routines_switched_off_for_each_ticked_twin(world, home, monkeypatch):
    h = Hermes(home, monkeypatch)
    files = [{"name": "both.json", "text": export(bot("Acme Support"), bot("Recipe Planner"))}]
    got = world.batch_import(files, ["acme-support", "recipe-planner", "test-scout", SAMPLE])["results"]
    by = {r["pair_id"]: r for r in got}
    assert [r["pair_id"] for r in got] == ["acme-support", "recipe-planner", "test-scout", SAMPLE]
    for pid in ("acme-support", "recipe-planner"):
        assert by[pid]["state"] == "done" and by[pid]["file"] == "both.json"
        assert by[pid]["instructions"] is True and by[pid]["routines"] == 1 and by[pid]["connectors"] == 1
        assert by[pid]["lines_left_out"] == 0 and by[pid]["why"] == ""
        assert "an invented helper" in (home / "profiles" / pid / "SOUL.md").read_text(encoding="utf-8")
        jobs = h.jobs(pid)
        assert len(jobs) == 1 and jobs[0]["enabled"] is False and jobs[0]["state"] == "paused"
    assert by["test-scout"] == {"pair_id": "test-scout", "name": "Test Scout", "file": "", "state": "no_file",
                                "instructions": False, "routines": 0, "connectors": 0, "lines_left_out": 0, "why": ""}
    assert by[SAMPLE]["state"] == "sample" and by[SAMPLE]["instructions"] is False
    assert not (home / "profiles" / "trail-guide").exists() and not (home / "profiles" / "test-scout" / "cron").exists()
    assert sorted(h.asked) == ["acme-support", "recipe-planner"]
    # memories and facts about the owner are nowhere: not in a twin's folder, the plugin's notes or its events
    nothing_kept(home)
    ev = [json.loads(line) for line in (central(home) / "events.jsonl").read_text().splitlines()]
    assert {"type": "batch_import", "payload": {"files": 1, "twins": 2}}.items() <= ev[-1].items()


def test_a_held_line_and_a_failed_file_are_said_per_twin(world, home, monkeypatch):
    Hermes(home, monkeypatch)
    secret = "pass" + "word: " + "tr0ub4dor" + "-and-" + "3xample"
    files = [{"name": "acme.json", "text": export(bot("Acme Support", instructions=f"Be brief.\n{secret}\nBe kind."))},
             {"name": "recipe.json", "text": export(bot("Recipe Planner", version=2))}]
    files[1]["text"] = files[1]["text"].replace('"version": 1', '"version": 7')
    got = {r["pair_id"]: r for r in world.batch_import(files, ["acme-support", "recipe-planner"])["results"]}
    assert got["acme-support"]["state"] == "done" and got["acme-support"]["lines_left_out"] == 1
    assert got["recipe-planner"]["state"] == "no_file"  # (not an export this version reads: matched to nothing)
    assert "tr0ub4dor" not in all_text_under(home)


# =====================================================================================================================
# Downloads
# =====================================================================================================================

def test_downloads_imports_new_exports_once_and_says_who_waits(world, home, monkeypatch):
    h = Hermes(home, monkeypatch)
    ticked = ["acme-support", "recipe-planner", SAMPLE]
    # no Downloads folder yet: every ticked real twin waits
    assert world.downloads_scan(0, ticked) == {"files": 0, "results": [], "waiting": ["acme-support", "recipe-planner"]}
    start = 1_800_000_000.0
    save("old-acme.json", export(bot("Acme Support")), mtime=start - 60)            # before the sheet watched
    save("acme.json", export(bot("Acme Support")), mtime=start + 5)
    save("acme.txt.exe", export(bot("Acme Support")), mtime=start + 6)              # not a name an export has
    save("big.json", '{"format": "grok-bot-export"}' + " " * (sx.TEXT_CAP + 1), mtime=start + 7)
    save("plain.json", json.dumps({"bots": [bot("Recipe Planner")]}), mtime=start + 8)  # no "grok-bot-export"
    (downloads() / "link.json").symlink_to(save("elsewhere.md", "x", mtime=start - 100))
    target = home.parent / "outside.json"
    target.write_text(export(bot("Recipe Planner")))
    (downloads() / "recipe-link.json").symlink_to(target)
    got = world.downloads_scan(start, ticked)
    assert got["files"] == 1 and got["waiting"] == ["recipe-planner"]
    assert [(r["pair_id"], r["state"], r["file"]) for r in got["results"]] == [("acme-support", "done", "acme.json")]
    assert h.asked == ["acme-support"]
    # the next look imports nothing again, in this process or a new one (only digests are kept)
    assert world.downloads_scan(start, ticked)["results"] == []
    seen = json.loads((central(home) / batch.SEEN_FILE).read_text())
    assert list(seen) == ["seen"] and len(seen["seen"]) == 1 and len(seen["seen"][0]) == 64
    assert "acme" not in (central(home) / batch.SEEN_FILE).read_text()
    world._batch_files = {}
    again = world.downloads_scan(start, ticked)
    assert again["results"] == [] and again["files"] == 1 and again["waiting"] == ["recipe-planner"]
    assert h.asked == ["acme-support"]
    # Recipe Planner's file arrives: imported, nobody waits
    save("recipe.json", export(bot("Recipe Planner")), mtime=start + 20)
    got = world.downloads_scan(start, ticked)
    assert [(r["pair_id"], r["state"]) for r in got["results"]] == [("recipe-planner", "done")]
    assert got["waiting"] == [] and got["files"] == 2
    nothing_kept(home)


def test_downloads_look_without_import_and_bad_input(world, home, monkeypatch):
    h = Hermes(home, monkeypatch)
    save("acme.json", export(bot("Acme Support")))
    got = world.downloads_scan(0, ["acme-support"], apply=False)
    assert [(r["pair_id"], r["state"]) for r in got["results"]] == [("acme-support", "ready")]
    assert h.asked == [] and not (central(home) / batch.SEEN_FILE).exists()
    # a look without importing does not keep it from being imported afterwards
    got = world.downloads_scan(0, ["acme-support"])
    assert [(r["pair_id"], r["state"]) for r in got["results"]] == [("acme-support", "done")]
    for since in (-1, "0", True, float("nan"), float("inf"), None):
        with pytest.raises(EngineError):
            world.downloads_scan(since, ["acme-support"])
    with pytest.raises(EngineError):
        world.downloads_scan(0, ["acme-support"], apply="yes")


def test_the_seen_list_keeps_only_digests_and_at_most_500(world, home):
    junk = ["not-a-digest", "A" * 64, 7] + [f"{i:064x}" for i in range(600)]
    store.atomic_write_json(central(home) / batch.SEEN_FILE, {"seen": junk})
    seen = world._seen_digests()
    assert len(seen) == 500 and all(len(d) == 64 and int(d, 16) >= 0 for d in seen)
    world._note_seen(["f" * 64])
    kept = json.loads((central(home) / batch.SEEN_FILE).read_text())["seen"]
    assert len(kept) == 500 and kept[-1] == "f" * 64


# =====================================================================================================================
# the model
# =====================================================================================================================

def config_of(home: Path, prof: str) -> Path:
    return home / "profiles" / prof / "config.yaml"


def read_config(path: Path) -> dict:
    doc = yamlio.module().safe_load(path.read_text(encoding="utf-8"))
    doc.pop("plugins", None)  # (the plugin turns itself on in each twin's profile: not these tests' business)
    return doc


def test_set_model_keeps_the_other_keys_and_the_rest_of_the_file(world, home):
    cfg = config_of(home, "acme-support")
    cfg.write_text("# my own note\nmodel:\n  default: old-model\n  provider: invented-cloud\n  base_url: "
                   "http://127.0.0.1:9/v1\n\nagent:\n  max_turns: 7\n", encoding="utf-8")
    assert world.set_model("acme-support", "grok-4.7") == {"pair_id": "acme-support", "model": "grok-4.7",
                                                           "state": "set"}
    text = cfg.read_text(encoding="utf-8")
    doc = read_config(cfg)
    assert doc == {"model": {"default": "grok-4.7", "provider": "invented-cloud",
                                              "base_url": "http://127.0.0.1:9/v1"}, "agent": {"max_turns": 7}}
    assert text.startswith("# my own note\n")  # only the model entry was changed
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    assert world._registry()["acme-support"]["model"] == {"from": "owner", "value": "grok-4.7"}
    assert world.set_model("acme-support", "grok-4.7")["state"] == "same"
    # a text, or no entry at all: the id is set
    cfg2 = config_of(home, "recipe-planner")
    cfg2.write_text("model: older\n", encoding="utf-8")
    assert world.set_model("recipe-planner", "invented/model-2")["state"] == "set"
    assert read_config(cfg2) == {"model": "invented/model-2"}
    assert world.set_model("test-scout", "grok-4.7")["state"] == "set"
    assert read_config(config_of(home, "test-scout"))["model"] == {"default": "grok-4.7",
                                                                                   "provider": "invented-cloud"}


def test_set_model_refuses_what_is_not_a_plain_model_id_and_the_default_profile(world, home):
    key = "sk-" + "a1b2c3d4e5f6" * 4
    for bad in ("", " ", "two words", "a\nb", "http://x/y", "me@host", key, "x" * 201, None, 7, ["m"], "-flag",
                "quote'd", "{x}"):
        with pytest.raises(EngineError) as ei:
            world.set_model("acme-support", bad)
        assert ei.value.code == "bad_input", bad
    cfg = config_of(home, "acme-support")
    assert not cfg.exists() or "model" not in read_config(cfg)
    # a twin recorded on the default profile is never written
    reg = world._registry()
    reg["test-scout"]["hermes"]["profile"] = "default"
    world._save_registry(reg)
    with pytest.raises(EngineError) as ei:
        world.set_model("test-scout", "grok-4.7")
    assert ei.value.code == "bad_profile" and not (home / "config.yaml").exists()
    # a config that is a link is not followed
    outside = home.parent / "elsewhere.yaml"
    outside.write_text("model: x\n")
    if config_of(home, "recipe-planner").exists():
        config_of(home, "recipe-planner").replace(home.parent / "moved-aside.yaml")
    config_of(home, "recipe-planner").symlink_to(outside)
    with pytest.raises(EngineError) as ei:
        world.set_model("recipe-planner", "grok-4.7")
    assert ei.value.code == "unsafe_folder" and outside.read_text() == "model: x\n"
    # the sample twin is never written
    assert world.set_model(SAMPLE, "grok-4.7") == {"pair_id": SAMPLE, "model": "grok-4.7", "state": "sample"}


def test_batch_models_says_per_twin(world, home):
    config_of(home, "recipe-planner").write_text("model:\n  default: grok-4.7\n")
    reg = world._registry()
    reg["test-scout"]["hermes"]["profile"] = "default"
    world._save_registry(reg)
    got = world.batch_models(["acme-support", "recipe-planner", "test-scout", SAMPLE], "grok-4.7")["results"]
    assert [(r["pair_id"], r["state"]) for r in got] == [("acme-support", "set"), ("recipe-planner", "same"),
                                                          ("test-scout", "failed"), (SAMPLE, "sample")]
    assert got[2]["why"] and got[0]["name"] == "Acme Support"
    assert world._registry()["acme-support"]["model"] == {"from": "batch", "value": "grok-4.7"}
    with pytest.raises(EngineError):
        world.batch_models(["acme-support"], "two words")
    with pytest.raises(EngineError):
        world.batch_models([], "grok-4.7")


def test_set_model_takes_model_off_what_the_twin_needs(twin, home):
    assert "model" in _pair(twin)["setup_left"]
    assert twin.set_model("acme-support", "grok-4.7")["state"] == "set"
    p = _pair(twin)
    assert "model" not in p["setup_left"] and p["hermes"]["model_from"] == "own"


# =====================================================================================================================
# what the sheet shows first
# =====================================================================================================================

def test_batch_info(world, home):
    (home / "config.yaml").write_text("model:\n  default: grok-4.7\n  provider: invented-cloud\n")
    config_of(home, "recipe-planner").write_text("model: invented/model-2\n")
    config_of(home, "test-scout").write_text("model: not a plain id\n")
    info = world.batch_info(["recipe-planner", "acme-support", SAMPLE])
    assert set(info) == {"request", "pickup", "privacy", "default_model", "models", "twins", "downloads", "skill_sync",
                         "computer_use", "endpoints"}
    assert info["request"] == sx.REQUEST and info["privacy"] == sx.PRIVACY
    assert info["default_model"] == "grok-4.7" and info["models"] == ["grok-4.7", "invented/model-2"]
    assert info["twins"] == [
        {"pair_id": "recipe-planner", "name": "Recipe Planner", "profile": "recipe-planner",
         "left": ["instructions", "model"], "model": "invented/model-2", "sample": False},
        {"pair_id": "acme-support", "name": "Acme Support", "profile": "acme-support",
         "left": ["instructions", "model"], "model": "", "sample": False},
        {"pair_id": SAMPLE, "name": "Trail Guide", "profile": "trail-guide", "left": ["instructions"], "model": "",
         "sample": True}]
    assert info["downloads"] == {"available": False, "label": "Downloads"}
    downloads()
    info = world.batch_info(None)
    assert info["downloads"] == {"available": True, "label": "Downloads"}
    assert str(Path.home()) not in json.dumps(info)
    assert len(info["twins"]) == 4
    assert info["skill_sync"] == {"set_up": False, "pair_ids": []}
    cu = info["computer_use"]
    assert isinstance(cu["available"], bool) and isinstance(cu["why"], str)


# =====================================================================================================================
# the routes
# =====================================================================================================================

def test_the_routes(twin, home, monkeypatch):
    Hermes(home, monkeypatch)
    c = _client(monkeypatch)
    pid = "acme-support"
    info = c.get(f"{P}/batch?pair_ids={pid}").json()
    assert [t["pair_id"] for t in info["twins"]] == [pid]
    assert c.post(f"{P}/batch/model", json={"pair_ids": [pid], "model": "grok-4.7"}).json() == {
        "results": [{"pair_id": pid, "name": "Acme Support", "state": "set", "why": ""}]}
    assert c.post(f"{P}/pairs/{pid}/model", json={"model": "grok-4.7"}).json()["state"] == "same"
    assert c.post(f"{P}/pairs/{pid}/model", json={"model": "a b"}).json()["error"]["code"] == "bad_input"
    files = [{"name": "acme.json", "text": export(bot("Acme Support"))}]
    assert c.post(f"{P}/batch/match", json={"files": files, "pair_ids": [pid]}).json()["matches"][0]["state"] \
        == "matched"
    got = c.post(f"{P}/batch/import", json={"files": files, "pair_ids": [pid]}).json()["results"]
    assert got[0]["state"] == "done" and got[0]["instructions"] is True
    too = c.post(f"{P}/batch/import", json={"files": files * 41, "pair_ids": [pid]}).json()
    assert too["error"]["code"] == "too_large"
    assert c.post(f"{P}/batch/downloads", json={"since": 0, "pair_ids": [pid]}).json() == {
        "files": 0, "results": [], "waiting": [pid]}
    assert c.post(f"{P}/batch/downloads", json={"since": 0, "apply": "no"}).status_code == 400
    assert c.post(f"{P}/batch/match", json={"files": files, "pair_ids": ["../x"]}).status_code == 400
    assert isinstance(c.get(f"{P}/batch/computer-use").json()["available"], bool)
    assert c.post(f"{P}/batch/grok-part", json={"stop": True}).json() == {"ok": True}
    nothing_kept(home)
