"""R15: the README's first install way (the Plugins button, or the catalog name) gives the older catalog version.

The README must say so before its first install instruction, and give the one command that installs the version it
describes (from `next`) right there, ready to paste, in one place only.
"""
import re
from pathlib import Path

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
NEXT_CMD = "hermes plugins install hudsonwa/twin --ref"


def _first(*words: str) -> int:
    found = [README.find(w) for w in words if w in README]
    return min(found) if found else len(README)


def test_R15_the_version_warning_and_the_next_command_come_before_any_other_install_instruction():
    assert NEXT_CMD in README
    cmd_at = README.find(NEXT_CMD)
    # the first `hermes plugins install` the reader meets is the one for `next`
    assert README.find("hermes plugins install") == cmd_at
    # the warning, naming the catalog and the version it gives, comes before the command and before the Desktop way
    warn = re.search(r"catalog[^\n]*(\n>?[^\n]*){0,3}0\.2\.1|0\.2\.1[^\n]*(\n>?[^\n]*){0,3}catalog", README)
    assert warn, "no warning that the catalog gives 0.2.1"
    assert warn.start() < cmd_at
    assert warn.start() < _first("Capabilities → Plugins", "hermes plugins install")
    # the command is copy-pasteable: it fetches the 40-character commit of `next` itself, no placeholder to fill in
    line = README[cmd_at:].split("\n", 1)[0]
    assert "<" not in line and "git ls-remote" in line and "refs/heads/next" in line and "--enable" in line
    assert "40-character" in README[:cmd_at + 2000]


def test_R15_the_next_install_is_written_once():
    assert README.count(NEXT_CMD) == 1
    assert "<that commit>" not in README
