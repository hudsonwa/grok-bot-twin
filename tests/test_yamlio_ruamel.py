"""YAML with whatever the Hermes at hand ships (twin/yamlio.py): PyYAML on older releases, ruamel.yaml on Hermes
0.23 with Python 3.14, where `import yaml` fails. Without this path mirrored twins got no model
and no display name, with no error anywhere. These tests cover the plain ruamel.yaml path (PyYAML and Hermes' own
module both away); tests/test_yamlio.py covers Hermes' own module.

The ruamel tests need ruamel.yaml in the test environment (`--with ruamel.yaml`); they are skipped without it."""
from __future__ import annotations

import sys

import pytest

from twin_testlib import central, home  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import fleet_profiles, hermes_side, yamlio


@pytest.fixture
def no_pyyaml(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)  # `import yaml` now raises ImportError, as on Hermes 0.23
    monkeypatch.setitem(sys.modules, "hermes_yaml", None)  # and Hermes' own module is not importable either


def test_pyyaml_is_used_when_it_is_there():
    import yaml
    assert yamlio.module() is yaml


def test_without_any_yaml_reader_the_plugin_copes(home, no_pyyaml, monkeypatch):  # noqa: F811
    monkeypatch.setitem(sys.modules, "ruamel", None)
    monkeypatch.setitem(sys.modules, "ruamel.yaml", None)
    assert yamlio.module() is None
    (home / "config.yaml").write_bytes(b"model:\n  default: acme/test-model\n")
    assert hermes_side.profile_model(home) is None           # "cannot tell", never a guess
    prof = fleet_profiles.create("test-scout", "Test Scout", "Finds trails.", "note")
    assert (prof / "config.yaml").read_bytes() == b"plugins:\n  enabled:\n  - grok-bot-twin\n"
    assert fleet_profiles.plugin_enabled(prof) is True


def test_ruamel_stands_in_for_pyyaml(no_pyyaml):
    pytest.importorskip("ruamel.yaml")
    y = yamlio.module()
    assert y is yamlio._Ruamel
    doc = {"model": {"default": "acme/test-model", "provider": "auto"}, "plugins": {"enabled": ["grok-bot-twin"]},
           "note": "off", "name": "Test Scout: finds trails", "long": "x" * 300}
    text = y.safe_dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True)
    assert y.safe_load(text) == doc                           # "off" comes back as text, not as False
    assert text.index("model:") < text.index("plugins:") < text.index("note:")   # the order given
    assert "{" not in text and "\r" not in text and len(text.splitlines()) == 9  # block style, nothing folded
    assert y.safe_load("a: 1\nb: [x, z]\n") == {"a": 1, "b": ["x", "z"]}


def test_a_mirrored_twin_gets_the_model_and_its_name_on_a_hermes_without_pyyaml(home, no_pyyaml):  # noqa: F811
    pytest.importorskip("ruamel.yaml")
    (home / "config.yaml").write_bytes(b"model:\n  default: acme/test-model\n  provider: auto\n"
                                       b"  base_url: https://example.invalid/v1\n")
    prof = fleet_profiles.create("test-scout", "Test Scout", "Finds trails.", "note")
    y = yamlio.module()
    cfg = y.safe_load((prof / "config.yaml").read_text(encoding="utf-8"))
    assert cfg == {"model": {"default": "acme/test-model", "provider": "auto"},   # ids only: no address copied
                   "plugins": {"enabled": ["grok-bot-twin"]}}
    meta = y.safe_load((prof / "profile.yaml").read_text(encoding="utf-8"))
    assert meta["display_name"] == "Test Scout" and meta["description"] == "Finds trails."
    assert hermes_side.profile_model(prof) == "acme/test-model"
    assert fleet_profiles.plugin_enabled(prof) is True
    assert fleet_profiles.disable_plugin(prof) is True and fleet_profiles.plugin_enabled(prof) is False
    assert fleet_profiles.enable_plugin(prof) is True and fleet_profiles.plugin_enabled(prof) is True
