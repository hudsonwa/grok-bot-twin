"""A plain Hermes install carries no PyYAML, only Hermes' own YAML module (hermes_yaml, on ruamel). A mirrored
twin had no display name and no model on such an install, because the plugin asked
for PyYAML alone. twin/yamlio.py now returns whichever is present; these tests take PyYAML away."""
from __future__ import annotations

import sys
import types

import pytest

from twin_testlib import home, plugin  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import fleet_profiles as fp, hermes_side as hs, yamlio

OWNER_CONFIG = "model:\n  default: acme-large\n  provider: acme\nplugins:\n  enabled:\n  - grok-bot-twin\n"


@pytest.fixture
def plain_hermes(monkeypatch):
    """`import yaml` fails, `import hermes_yaml` works: a stand-in with Hermes' own signatures (built on PyYAML,
    which the test environment has), or the real module when this test runs inside a Hermes install."""
    real = pytest.importorskip("yaml")
    try:
        import hermes_yaml as mod
    except ImportError:
        mod = types.ModuleType("hermes_yaml")
        mod.safe_load = real.safe_load

        def safe_dump(data, stream=None, *, default_flow_style=False, sort_keys=True, allow_unicode=True, width=80):
            return real.safe_dump(data, default_flow_style=default_flow_style, sort_keys=sort_keys,
                                  allow_unicode=allow_unicode, width=width)
        mod.safe_dump = safe_dump
    monkeypatch.setitem(sys.modules, "hermes_yaml", mod)
    monkeypatch.setitem(sys.modules, "yaml", None)  # from here on `import yaml` raises ImportError
    return mod


def test_pyyaml_is_used_when_present_else_hermes_own_else_nothing(monkeypatch):
    real = pytest.importorskip("yaml")
    assert yamlio.module() is real
    monkeypatch.setitem(sys.modules, "yaml", None)
    fake = types.ModuleType("hermes_yaml")
    fake.safe_load = lambda text: {"read": text}
    fake.safe_dump = lambda data, **kw: f"dumped width={kw['width']} sort={kw['sort_keys']}"
    monkeypatch.setitem(sys.modules, "hermes_yaml", fake)
    got = yamlio.module()
    assert got.name == "hermes_yaml" and got.safe_load("a: 1") == {"read": "a: 1"}
    # long values stay on one line (ruamel can change a quoted value when it folds a line)
    assert got.safe_dump({"a": 1}, default_flow_style=False, sort_keys=False, allow_unicode=True) \
        == f"dumped width={2 ** 31 - 1} sort=False"
    monkeypatch.setitem(sys.modules, "hermes_yaml", None)
    monkeypatch.setitem(sys.modules, "ruamel", None)       # the third choice (twin/yamlio.py), where it is installed
    monkeypatch.setitem(sys.modules, "ruamel.yaml", None)
    assert yamlio.module() is None


def test_on_a_plain_hermes_a_mirrored_twin_gets_its_name_its_model_and_the_plugin(home, plain_hermes):
    (home / "config.yaml").write_text(OWNER_CONFIG, encoding="utf-8")
    prof = fp.create("test-scout", "Test Scout", "Finds trails.", "note")
    meta = plain_hermes.safe_load((prof / "profile.yaml").read_text(encoding="utf-8"))
    assert meta["display_name"] == "Test Scout" and meta["description"] == "Finds trails."
    cfg = plain_hermes.safe_load((prof / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["model"] == {"default": "acme-large", "provider": "acme"}
    assert cfg["plugins"]["enabled"] == ["grok-bot-twin"]
    assert hs.profile_model(prof) == "acme-large"  # the model check can tell
    assert fp.plugin_enabled(prof) is True
    # a rename reaches the name Hermes shows
    fp.update("test-scout", "Test Ranger", "Finds better trails.", "note")
    meta = plain_hermes.safe_load((prof / "profile.yaml").read_text(encoding="utf-8"))
    assert meta["display_name"] == "Test Ranger" and meta["description"] == "Finds better trails."


def test_on_a_plain_hermes_the_plugin_can_be_switched_on_and_off_in_a_bot_that_has_a_plugins_section(home, plain_hermes):
    prof = home / "profiles" / "test-quill"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text("model: acme-small\nplugins:\n  enabled:\n  - something-else\n", encoding="utf-8")
    assert fp.enable_plugin(prof) is True
    cfg = plain_hermes.safe_load((prof / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["plugins"]["enabled"] == ["something-else", "grok-bot-twin"] and cfg["model"] == "acme-small"
    assert fp.disable_plugin(prof) is True
    cfg = plain_hermes.safe_load((prof / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["plugins"]["enabled"] == ["something-else"] and cfg["model"] == "acme-small"


def test_on_a_plain_hermes_a_new_twin_on_the_source_gets_the_default_model(home, plain_hermes):
    (home / "config.yaml").write_text(OWNER_CONFIG, encoding="utf-8")
    prof = home / "profiles" / "acme-support"
    prof.mkdir(parents=True)
    assert hs.copy_default_model(prof, home) == "acme-large"
    assert hs.profile_model(prof) == "acme-large"


def test_with_no_yaml_module_at_all_the_twin_is_still_made(home, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    monkeypatch.setitem(sys.modules, "hermes_yaml", None)
    monkeypatch.setitem(sys.modules, "ruamel", None)
    monkeypatch.setitem(sys.modules, "ruamel.yaml", None)
    prof = fp.create("test-scout", "Test Scout", "Finds trails.", "note")
    assert "grok-bot-twin" in (prof / "config.yaml").read_text(encoding="utf-8")
    assert "# Test Scout" in (prof / "SOUL.md").read_text(encoding="utf-8")
    assert not (prof / "profile.yaml").exists() and hs.profile_model(prof) is None  # "cannot tell", as before


def test_hermes_own_module_reads_back_what_it_wrote_even_long_lines_with_backslashes():
    hermes_yaml = pytest.importorskip("hermes_yaml")  # runs where the tests run inside a Hermes install
    y = yamlio._HermesYaml(hermes_yaml)
    doc = {"display_name": "Test Scout", "description": ("C:\\Users\\example\\notes " * 40).strip(),
           "description_auto": False, "plugins": {"enabled": ["grok-bot-twin"]}}
    text = y.safe_dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True)
    assert y.safe_load(text) == doc
    assert isinstance(y.safe_load("a: [1, 2]\nb: off\n"), dict) and y.safe_load("b: off\n") == {"b": False}
