"""C2 F12: Hermes' own code is imported in ONE place (twin/config.py), its security checks fail closed, and the
plugin's one path to Hermes' cron store (twin/routines_jobs.py) refuses a jobs.json shape it does not know."""
from __future__ import annotations

import ast
import json
import os
import sys
import types
from pathlib import Path

import pytest

from hermes_plugins.grok_bot_twin.twin import config, fleet_profiles as fp, importer, routines_jobs as rj, tools

ROOT = Path(__file__).resolve().parent.parent
# Hermes' own top-level packages and modules the plugin may meet (not the standard library, not the plugin's own)
HERMES = {"agent", "hermes_constants", "hermes_cli", "hermes_state", "gateway", "tui_gateway", "plugins", "tools",
          "toolsets", "cron", "run_agent", "model_tools"}
ALLOWED = {
    "twin/config.py",  # the adapter
    "twin/turn_bridge.py",  # a program of its own (Hermes' client), it imports nothing of the plugin by design
}


def _hermes_imports(path: Path) -> list:
    hits = []
    for node in ast.walk(ast.parse(path.read_text("utf-8"), str(path))):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:  # (relative imports are the plugin's own)
            names = [node.module or ""]
        else:
            continue
        hits += [f"{path.relative_to(ROOT)}:{node.lineno} {n}" for n in names if n.split(".")[0] in HERMES]
    return hits


def test_F12_hermes_internals_are_imported_only_in_the_adapter():
    files = [ROOT / "__init__.py", *sorted((ROOT / "twin").glob("*.py")), *sorted((ROOT / "dashboard").glob("*.py"))]
    found = {f.relative_to(ROOT).as_posix(): _hermes_imports(f) for f in files}
    outside = [h for f, hits in found.items() if f not in ALLOWED for h in hits]
    assert outside == [], "import Hermes' own code through twin/config.py, never directly:\n" + "\n".join(outside)
    assert found["twin/config.py"]  # (the scan does see imports)


def _session(monkeypatch, values=None, **attrs):
    """Hermes' per-chat module as a test wants it: `values` for get_session_env, or other attributes."""
    ctx = types.ModuleType("gateway.session_context")
    if values is not None:
        ctx.get_session_env = lambda name, default="": values.get(name, default)
    for k, v in attrs.items():
        setattr(ctx, k, v)
    monkeypatch.setitem(sys.modules, "gateway.session_context", ctx)


def test_F12_is_local_refuses_without_hermes_or_with_another_shape(monkeypatch):
    local = {"HERMES_SESSION_SOURCE": "desktop"}
    _session(monkeypatch, local)
    assert config.chat_not_local() is False  # (an older Hermes without its own answer: the named origin counts)
    monkeypatch.setitem(sys.modules, "gateway", None)
    monkeypatch.setitem(sys.modules, "gateway.session_context", None)  # absent
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")  # (the environment is no way around it)
    assert config.chat_not_local() is True
    _session(monkeypatch)  # there, but without get_session_env
    assert config.chat_not_local() is True
    _session(monkeypatch, {"HERMES_SESSION_SOURCE": 1})  # answers something that is not text
    assert config.chat_not_local() is True

    def boom(*_a, **_k):
        raise RuntimeError("changed")
    _session(monkeypatch, get_session_env=boom)
    assert config.chat_not_local() is True
    _session(monkeypatch, local, session_is_messaging_surface=boom)  # Hermes' own answer fails
    assert config.chat_not_local() is True
    _session(monkeypatch, local, session_is_messaging_surface=lambda: None)  # ... or is not a yes/no
    assert config.chat_not_local() is True
    _session(monkeypatch, local, session_is_messaging_surface=lambda: False)
    assert config.chat_not_local() is False
    # the transcript search tool asks the same check (and, for its bot, Hermes' own home: none without Hermes)
    _session(monkeypatch, {"HERMES_SESSION_PLATFORM": "telegram"})
    assert "messaging app" in tools.twin_transcripts_search({"query": "x"})
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    assert tools._task_profile(None) is None


def test_F12_skill_scan_never_passes_without_hermes_scanner(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "tools", None)
    monkeypatch.setitem(sys.modules, "tools.skills_guard", None)  # absent
    assert config.skill_scan(tmp_path) is None
    guard = types.ModuleType("tools.skills_guard")
    monkeypatch.setitem(sys.modules, "tools.skills_guard", guard)  # there, without scan_skill
    assert config.skill_scan(tmp_path) is None
    for answer in ({"verdict": "safe"}, types.SimpleNamespace(verdict="SAFE"), types.SimpleNamespace(verdict=["safe"]),
                   types.SimpleNamespace()):
        guard.scan_skill = lambda path, source=None, a=answer: a  # an answer in another shape
        assert config.skill_scan(tmp_path) is None, answer
    guard.scan_skill = lambda path, source=None: (_ for _ in ()).throw(RuntimeError("changed"))
    assert config.skill_scan(tmp_path) is None
    guard.scan_skill = lambda path, source=None: types.SimpleNamespace(verdict="dangerous")
    assert config.skill_scan(tmp_path) == "dangerous"
    assert not hasattr(importer, "scan_skill_dir")  # (the one scanner call is config.skill_scan)


@pytest.mark.parametrize("doc", [
    {"version": 2, "entries": [{"id": "a1"}]},  # jobs kept under another name
    {"jobs": {"a1": {"id": "a1"}}},  # an id-keyed map
    {"jobs": [{"name": "no id"}]},
    {"jobs": [{"id": "a1", "enabled": "yes"}]},
    {"jobs": [{"id": "a1", "state": {"paused": True}}]},
    {"jobs": [{"id": "a1"}, "not a job"]},
    "jobs",
])
def test_F12_cron_store_refuses_an_unknown_shape(tmp_path, doc):
    home = Path(os.environ["HERMES_HOME"]) / "profiles" / "test-scout"
    (home / "cron").mkdir(parents=True)
    path = home / "cron" / "jobs.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    before = path.read_bytes()
    spec = {"name": "Test Scout digest", "prompt": "say hello", "schedule": {"kind": "cron", "expr": "0 9 * * *"}}
    with pytest.raises(OSError, match="not in the shape this plugin knows"):
        rj.edit(home, lambda jobs: (jobs.append(rj.new_record(spec, rj.MARK + "test")) or True, None))
    assert path.read_bytes() == before  # nothing written over it
    assert rj.load(home) is None  # nothing concluded from it ...
    assert importer.job_states(home) == {}  # ... by the importer either
    assert fp._has_jobs(home) is True  # (a folder whose routines cannot be read is never taken for an empty one)


def test_F12_cron_store_still_writes_hermes_shape(tmp_path):
    home = Path(os.environ["HERMES_HOME"]) / "profiles" / "test-scout"
    (home / "cron").mkdir(parents=True)
    path = home / "cron" / "jobs.json"
    path.write_text(json.dumps({"jobs": [{"id": "a1", "enabled": True, "state": "scheduled"}], "updated_at": None}))
    rj.edit(home, lambda jobs: (rj.set_paused(rj.find(jobs, "a1"), rj.MARK + "test"), None))
    doc = json.loads(path.read_text("utf-8"))
    assert doc["jobs"][0]["state"] == "paused" and doc["updated_at"] and rj.ours(doc["jobs"][0])
    assert importer.job_states(home) == {"a1": True}
