"""R16: the README must say which Hermes the plugin needs before the reader installs anything.

The minimum comes from plugin.yaml (`requires_hermes`), so the README stays true when it moves. It is stated once,
above the first install instruction of either way (the terminal line and the Desktop Plugins button).
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
MANIFEST = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))


def _min_hermes() -> str:
    m = re.fullmatch(r"\s*>=\s*(\d+(?:\.\d+)+)\s*", str(MANIFEST["requires_hermes"]))
    assert m, f"requires_hermes is not a plain '>=X.Y.Z': {MANIFEST['requires_hermes']!r}"
    return m.group(1)


def _first(*words: str) -> int:
    found = [README.find(w) for w in words if w in README]
    assert found, f"none of {words} in README"
    return min(found)


def test_R16_the_minimum_hermes_is_stated_once_before_the_first_install_instruction():
    ver = _min_hermes()
    assert README.count(ver) == 1, f"Hermes {ver} should be stated exactly once in README"
    at = README.find(ver)
    assert re.search(rf"Hermes {re.escape(ver)} or newer", README), f"no 'Hermes {ver} or newer'"
    # before both install ways: the terminal line and the Desktop Plugins button
    assert at < README.find("hermes plugins install")
    assert at < _first("Capabilities → Plugins", "**Plugins**")
    # and it tells how to check and how to update, right there
    near = README[at:at + 300]
    assert "hermes --version" in near and "hermes update" in near
