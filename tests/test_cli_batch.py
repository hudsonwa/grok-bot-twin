"""`hermes twin finish` and `hermes twin import-exports`: several new twins at once from the terminal.

Invented bots only (Acme Helper, Recipe Planner, Weather Bot). Most tests stand the service's batch methods in with
a small fake (the command line is what is tested); the last ones run the real backend (twin/batch.py) through the
fake botport when it is there. Every test has its own HERMES_HOME (twin_testlib's `home`).
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout

import pytest

from twin_testlib import cli, home, service, svc  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import setup_export as sx, tasks

NAMES = {"acme-helper": "Acme Helper", "recipe-planner": "Recipe Planner"}
INSTRUCTIONS = {"Acme Helper": "You are Acme Helper.\nAnswer questions about invented widgets.",
                "Recipe Planner": "You are Recipe Planner.\nPlan invented meals."}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def export_text(bot: str, routines: int = 2, apps: int = 1) -> str:
    return json.dumps({"format": "grok-bot-export", "version": 1, "bots": [{
        "name": bot, "instructions": INSTRUCTIONS.get(bot, f"You are {bot}."),
        "routines": [{"name": f"Routine {i}", "instruction": "Summarise invented things.", "trigger": "schedule",
                      "schedule_cron": "0 8 * * *", "timezone": "UTC"} for i in range(routines)],
        "apps": [{"name": f"Invented App {i}"} for i in range(apps)]}]})


class FakeBatch:
    """Stands in for the BatchMixin methods (and the one-twin preview) on the running service."""

    def __init__(self, svc, monkeypatch, left=None, default_model="grok-4.7", models=("grok-4.7",), sample=False,
                 names=None):
        self.names = dict(NAMES if names is None else names)
        self.left = left if left is not None else {"acme-helper": ["instructions", "model"],
                                                   "recipe-planner": ["instructions"]}
        self.default_model, self.models, self.sample = default_model, list(models), sample
        self.info_calls, self.model_calls, self.match_calls, self.import_calls, self.previews = [], [], [], [], []
        monkeypatch.setattr(svc, "pair_ids", lambda: list(self.names) + (["acme-sample"] if sample else []))
        monkeypatch.setattr(cli, "_twin_names", lambda s: dict(self.names))
        for m in ("batch_info", "batch_models", "batch_match", "batch_import", "import_setup"):
            monkeypatch.setattr(svc, m, getattr(self, m), raising=False)

    def batch_info(self, pids=None):
        self.info_calls.append(pids)
        twins = [{"pair_id": p, "name": n, "profile": p, "left": list(self.left.get(p, [])),
                  "model": "" if "model" in self.left.get(p, []) else "grok-3", "sample": False}
                 for p, n in self.names.items() if pids is None or p in pids]
        if self.sample and pids is None:
            twins.append({"pair_id": "acme-sample", "name": "Acme Support", "profile": "", "left": ["model"],
                          "model": "", "sample": True})
        return {"request": sx.REQUEST, "privacy": sx.PRIVACY, "default_model": self.default_model,
                "models": self.models, "twins": twins, "downloads": {"available": True, "label": "Downloads"},
                "skill_sync": {"set_up": False, "pair_ids": []}}

    def batch_models(self, pids, model):
        self.model_calls.append((list(pids), model))
        return {"results": [{"pair_id": p, "name": self.names[p], "state": "set", "why": ""} for p in pids]}

    def _bots(self, text):
        try:
            doc = json.loads(text)
        except ValueError:
            return None
        if not isinstance(doc, dict) or doc.get("format") != "grok-bot-export":
            return None
        return [b.get("name") for b in doc.get("bots") or []]

    def batch_match(self, files, pids=None):
        self.match_calls.append(([f["name"] for f in files], pids))
        by_name = {n: p for p, n in self.names.items() if pids is None or p in pids}
        out = []
        for f in files:
            bots = self._bots(f["text"])
            if bots is None:
                out.append({"file": f["name"], "pair_id": None, "name": "", "bot": "", "state": "not_export",
                            "why": "not a setup export"})
                continue
            for b in bots:
                pid = by_name.get(b)
                out.append({"file": f["name"], "pair_id": pid, "name": b if pid else "", "bot": b,
                            "state": "matched" if pid else "no_twin", "why": ""})
        return {"matches": out}

    def import_setup(self, pid, text, apply=False, bot=None, skip=None):
        assert apply is False  # the command line only previews one twin; the import goes through batch_import
        self.previews.append((pid, bot))
        doc = json.loads(text)["bots"][0]
        return {"bot": {"name": doc["name"], "matches": True},
                "instructions": {"state": "new", "text": doc["instructions"],
                                 "lines": len(doc["instructions"].splitlines()), "left_out": [], "replaces": False,
                                 "saved": False},
                "routines": [{"name": r["name"], "state": "new", "prompt": r["instruction"],
                              "schedule": r["schedule_cron"], "timezone": r["timezone"], "why": ""}
                             for r in doc.get("routines") or []],
                "connectors": [{"name": a["name"]} for a in doc.get("apps") or []],
                "left_out": {"memories": 0, "user_facts": 0, "skills": 0, "routines": 0},
                "privacy": sx.PRIVACY, "added": 0}

    def batch_import(self, files, pids=None):
        self.import_calls.append(([f["name"] for f in files], pids))
        res = []
        for f in files:
            doc = json.loads(f["text"])["bots"][0]
            pid = next(p for p, n in self.names.items() if n == doc["name"])
            res.append({"pair_id": pid, "name": doc["name"], "file": f["name"], "state": "done",
                        "instructions": True, "routines": len(doc.get("routines") or []),
                        "connectors": len(doc.get("apps") or []), "lines_left_out": 0, "why": ""})
        return {"results": res}


@pytest.fixture
def exports(tmp_path):
    """A Downloads-like folder: two answer files (invented bots), one for a bot with no twin, one that is no setup
    export, a picture, a hidden file and a link to one of the answer files."""
    d = tmp_path / "Downloads"
    d.mkdir()
    (d / "acme-export.json").write_text(export_text("Acme Helper", routines=2, apps=1), encoding="utf-8")
    (d / "recipe-export.md").write_text(export_text("Recipe Planner", routines=0, apps=0), encoding="utf-8")
    (d / "weather.json").write_text(export_text("Weather Bot"), encoding="utf-8")
    (d / "shopping-list.txt").write_text("eggs\ninvented milk\n", encoding="utf-8")
    (d / "photo.png").write_bytes(b"\x89PNG invented")
    (d / ".hidden.json").write_text(export_text("Acme Helper"), encoding="utf-8")
    os.symlink(d / "acme-export.json", d / "link-to-acme.json")
    return d


# ---- parsing -------------------------------------------------------------------------------------------------------
def test_arguments_parse(svc):
    p = cli.build_parser()
    a = p.parse_args(["finish", "Acme Helper", "Recipe Planner", "--model", "grok-4.7", "--yes"])
    assert a.twins == ["Acme Helper", "Recipe Planner"] and a.model == "grok-4.7" and a.yes and not a.all
    a = p.parse_args(["finish", "--all"])
    assert a.all and a.twins == [] and a.model is None and not a.yes
    a = p.parse_args(["import-exports", "~/Downloads", "--twin", "Acme Helper", "--twin", "Recipe Planner",
                      "--show", "-y"])
    assert a.folder == "~/Downloads" and a.twin == ["Acme Helper", "Recipe Planner"] and a.show and a.yes
    a = p.parse_args(["import-exports", "/somewhere"])
    assert a.twin is None and not a.show and not a.yes
    with pytest.raises(SystemExit):
        p.parse_args(["import-exports"])
    text = p.format_help()
    assert "\n  finish [TWIN ...]" in text and "\n  import-exports DIR" in text
    for cmd in ("finish", "import-exports"):
        sub = p._subparsers._group_actions[0].choices[cmd].format_help()
        assert "started" not in sub.lower() and "pair" not in sub.lower(), cmd


def test_finish_needs_twins_or_all(svc, monkeypatch):
    fb = FakeBatch(svc, monkeypatch)
    rc, out, err = run(["finish"])
    assert rc == 2 and 'hermes twin finish "Acme Support" "Recipe Planner"' in err and "--all" in err
    rc, out, err = run(["finish", "Acme Helper", "--all"])
    assert rc == 2 and "not both" in err
    rc, out, err = run(["finish", "Nobody Here"])
    assert rc == 2 and 'No twin is called "Nobody Here".' in err
    assert fb.info_calls == [] and fb.model_calls == []


def test_all_with_no_twins(svc, monkeypatch):
    fb = FakeBatch(svc, monkeypatch, names={}, left={})
    rc, out, err = run(["finish", "--all", "--yes"])
    assert rc == 0 and out.startswith("No twins yet.") and fb.model_calls == []
    rc, out, err = run(["import-exports", "/does-not-matter", "--yes"])
    assert rc == 0 and out.startswith("No twins yet.") and fb.match_calls == []


def test_all_with_nothing_left_and_the_sample_left_alone(svc, monkeypatch):
    fb = FakeBatch(svc, monkeypatch, left={}, sample=True)
    rc, out, err = run(["finish", "--all", "--yes"])
    assert rc == 0 and "Nothing is left: every twin has its instructions and a model." in out
    assert fb.model_calls == [] and sx.REQUEST not in out and "Acme Support" not in out


# ---- finish: the plan, one question, the model, the request once ------------------------------------------------
def test_finish_all_sets_the_default_model_and_prints_the_request_once(svc, monkeypatch):
    fb = FakeBatch(svc, monkeypatch, sample=True)
    rc, out, err = run(["finish", "--all", "--yes"])
    assert rc == 0, err
    assert fb.info_calls == [None]
    assert fb.model_calls == [(["acme-helper"], "grok-4.7")]  # only the twin whose model is left
    lines = out.splitlines()
    assert lines[0] == "For 2 twins:"
    assert " Acme Helper: model: set to grok-4.7; instructions: still to come from Grok" in lines
    assert " Recipe Planner: model: stays grok-3; instructions: still to come from Grok" in lines
    assert " Acme Helper: model set to grok-4.7 ✓" in lines
    assert out.count(sx.REQUEST.strip()) == 1  # printed once for every bot
    assert ('Paste this into the chat of each of these bots in Grok: "Acme Helper", "Recipe Planner"' in out)
    assert lines[-2] == ("Each bot answers with a file. Save each answer file, then run: hermes twin import-exports "
                         "~/Downloads")
    assert "Acme Support" not in out  # the sample is never touched
    assert "started" not in out.replace(sx.REQUEST.strip(), "").lower()  # (the request itself is the bot's to read)


def test_finish_with_an_explicit_model_sets_it_on_every_named_twin(svc, monkeypatch):
    fb = FakeBatch(svc, monkeypatch, left={"acme-helper": ["instructions"], "recipe-planner": []})
    rc, out, err = run(["finish", "Recipe Planner", "acme helper", "--model", "grok-4.7-mini", "--yes"])
    assert rc == 0, err
    assert fb.info_calls == [["recipe-planner", "acme-helper"]]
    assert fb.model_calls == [(["acme-helper", "recipe-planner"], "grok-4.7-mini")]
    assert " Recipe Planner: model: set to grok-4.7-mini; instructions: in" in out
    assert out.count(sx.REQUEST.strip()) == 1
    assert 'bots in Grok: "Acme Helper"\n' in out  # only the one still without instructions


def test_finish_without_a_model_anywhere_says_how_to_pass_one(svc, monkeypatch):
    fb = FakeBatch(svc, monkeypatch, default_model="", models=["grok-3"])
    rc, out, err = run(["finish", "Acme Helper", "--yes"])
    assert rc == 2 and fb.model_calls == []
    assert 'hermes twin finish "Acme Helper" --model MODEL' in err
    assert "Models this Hermes already uses: grok-3" in err and "Nothing changed." in err
    rc, out, err = run(["finish", "--all"])
    assert rc == 2 and "hermes twin finish --all --model MODEL" in err


def test_finish_asks_once_and_a_no_changes_nothing(svc, monkeypatch):
    fb = FakeBatch(svc, monkeypatch)
    rc, out, err = run(["finish", "--all"])  # not a terminal, no --yes
    assert rc == 1 and "pass --yes" in err and "Nothing changed." in out and fb.model_calls == []
    asked = []
    monkeypatch.setattr(cli, "_confirm", lambda q: asked.append(q) or True)
    rc, out, err = run(["finish", "--all"])
    assert rc == 0 and len(asked) == 1 and "grok-4.7" in asked[0] and "1 twin" in asked[0]
    assert fb.model_calls == [(["acme-helper"], "grok-4.7")]


def test_finish_with_only_instructions_left_asks_nothing(svc, monkeypatch):
    fb = FakeBatch(svc, monkeypatch, left={"acme-helper": ["instructions"], "recipe-planner": ["instructions"]})
    monkeypatch.setattr(cli, "_confirm", lambda q: pytest.fail("asked although nothing changes"))
    rc, out, err = run(["finish", "--all"])
    assert rc == 0 and fb.model_calls == [] and out.count(sx.REQUEST.strip()) == 1


# ---- import-exports ------------------------------------------------------------------------------------------------
def test_import_exports_plan_then_yes(svc, monkeypatch, exports):
    fb = FakeBatch(svc, monkeypatch)
    rc, out, err = run(["import-exports", str(exports)])  # not a terminal, no --yes: the plan only
    assert rc == 1 and "pass --yes" in err and "Nothing changed." in out and fb.import_calls == []
    names, pids = fb.match_calls[0]
    assert sorted(names) == ["acme-export.json", "recipe-export.md", "shopping-list.txt", "weather.json"]
    assert pids is None  # every twin
    assert "Setup exports found: 4 files" in out
    assert (" acme-export.json -> Acme Helper: instructions (2 lines), 2 routines (arrive switched off), "
            "1 connector to set up here") in out
    assert " recipe-export.md -> Recipe Planner: instructions (2 lines)" in out
    assert ' weather.json: no twin for the bot "Weather Bot"' in out
    assert " shopping-list.txt: not a setup export" in out
    assert "link-to-acme" not in out and "photo.png" not in out and ".hidden" not in out
    assert " Read the text before you import it: add --show" in out
    for text in INSTRUCTIONS.values():
        assert text.splitlines()[1] not in out  # the bots' text is shown only with --show
    assert "invented milk" not in out + err

    rc, out, err = run(["import-exports", str(exports), "--show"])
    assert rc == 1 and "Read the text before" not in out
    assert "=== Acme Helper, from acme-export.json ===" in out and INSTRUCTIONS["Acme Helper"] in out
    assert INSTRUCTIONS["Recipe Planner"] in out and fb.import_calls == []

    rc, out, err = run(["import-exports", str(exports), "--yes"])
    assert rc == 0, err
    assert len(fb.import_calls) == 1  # one import for every file, newest first
    sent, pids = fb.import_calls[0]
    assert sorted(sent) == ["acme-export.json", "recipe-export.md"] and sorted(pids) == ["acme-helper", "recipe-planner"]
    assert " Acme Helper: instructions in ✓, 2 routines added (switched off), 1 connector to set up here" in out
    assert " Recipe Planner: instructions in ✓" in out.splitlines()
    assert " Read the text before you import it: add --show" in out  # --yes without --show still says it
    assert "Nothing was sent to Grok." in out and "hermes twin switch" in out
    assert "started" not in out.lower()


def test_import_exports_for_named_twins_only(svc, monkeypatch, exports):
    fb = FakeBatch(svc, monkeypatch)
    rc, out, err = run(["import-exports", str(exports), "--twin", "Recipe Planner", "--yes"])
    assert rc == 0, err
    assert fb.match_calls[0][1] == ["recipe-planner"]
    assert ' acme-export.json: no twin for the bot "Acme Helper"' in out
    assert fb.import_calls == [(["recipe-export.md"], ["recipe-planner"])]


def test_import_exports_skips_what_it_cannot_read(svc, monkeypatch, tmp_path):
    fb = FakeBatch(svc, monkeypatch)
    d = tmp_path / "answers"
    d.mkdir()
    (d / "big.json").write_text(export_text("Acme Helper") + " " * 400, encoding="utf-8")
    (d / "latin.txt").write_bytes(b"caf\xe9 invented")
    (d / "folder.json").mkdir()
    monkeypatch.setattr(sx, "TEXT_CAP", 300)
    rc, out, err = run(["import-exports", str(d), "--yes"])
    assert rc == 1 and " big.json: too large to be a setup export" in out and " latin.txt: not text" in out
    assert "No setup export files there" in out and fb.match_calls == [] and "folder.json" not in out
    rc, out, err = run(["import-exports", str(tmp_path / "not-there"), "--yes"])
    assert rc == 2 and "That folder could not be read." in err


def test_import_exports_with_nothing_matched(svc, monkeypatch, tmp_path):
    fb = FakeBatch(svc, monkeypatch)
    d = tmp_path / "answers"
    d.mkdir()
    (d / "notes.md").write_text("# invented notes\n", encoding="utf-8")
    rc, out, err = run(["import-exports", str(d), "--yes"])
    assert rc == 1 and " notes.md: not a setup export" in out and "Nothing to import." in out
    assert fb.import_calls == []


def test_finish_is_refused_in_a_requests_chat_turn(svc, monkeypatch):
    fb = FakeBatch(svc, monkeypatch)
    monkeypatch.setattr(tasks, "asked_turn", lambda *a, **k: True)
    rc, out, err = run(["finish", "--all", "--model", "local-model", "--yes"])
    assert rc == 1 and "A request from another computer cannot set twins' models." in err
    assert fb.model_calls == [] and fb.info_calls == [] and out == ""


def test_import_exports_is_refused_in_a_requests_chat_turn(svc, monkeypatch, exports):
    fb = FakeBatch(svc, monkeypatch)
    monkeypatch.setattr(tasks, "asked_turn", lambda *a, **k: True)
    rc, out, err = run(["import-exports", str(exports), "--yes"])
    assert rc == 1 and "A request from another computer cannot import a setup export." in err
    assert "Import it yourself, on the Twins page or in your own terminal." in err
    assert fb.match_calls == [] and fb.import_calls == [] and out == ""


# ---- the real backend (twin/batch.py), through the fake botport ----------------------------------------------------
def _real_backend() -> bool:
    return hasattr(service.TwinService, "batch_import") and hasattr(service.TwinService, "batch_info")


needs_backend = pytest.mark.skipif(not _real_backend(), reason="twin/batch.py is not in this tree yet")

try:  # the one-real-twin rig the setup-export tests use (the fake botport, accepted as a local build)
    from test_ux_flows import fake  # noqa: F401
    from test_setup_download_and_twin_facts import twin  # noqa: F401
    import test_setup_export as sx_tests
except Exception:  # pragma: no cover
    sx_tests = None


@needs_backend
def test_real_import_exports_into_a_real_twin(twin, home, tmp_path, monkeypatch):  # noqa: F811
    h = sx_tests.Hermes(home, monkeypatch)
    d = tmp_path / "Downloads"
    d.mkdir()
    (d / "acme.json").write_text(sx_tests.text_of(), encoding="utf-8")
    (d / "notes.txt").write_text("invented shopping list\n", encoding="utf-8")
    os.symlink(d / "acme.json", d / "acme-link.json")
    rc, out, err = run(["import-exports", str(d)])
    assert rc == 1 and "Nothing changed." in out and h.asked == []
    assert " acme.json -> Acme Support: " in out and " notes.txt: not a setup export" in out
    assert "acme-link" not in out
    rc, out, err = run(["import-exports", str(d), "--yes"])
    assert rc == 0, out + err
    assert " Acme Support: instructions in ✓, 1 routine added (switched off)" in out
    assert "You are Acme Support." in sx_tests.soul(home) and len(h.jobs()) == 1
    sx_tests.nothing_kept(home)


@needs_backend
def test_real_finish_prints_the_request_once(twin, home, monkeypatch):  # noqa: F811
    rc, out, err = run(["finish", "Acme Support", "--model", "grok-4.7", "--yes"])
    assert rc == 0, out + err
    assert "Acme Support" in out
    assert out.count(sx.REQUEST.strip()) <= 1
    cfg = home / "profiles" / "acme-support" / "config.yaml"
    assert cfg.is_file() and "grok-4.7" in cfg.read_text(encoding="utf-8")
