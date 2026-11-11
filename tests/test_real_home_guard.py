"""The safety net of tests/conftest.py itself: which HERMES_HOME it lets a test end with. Nothing is read or written
here; only the words of the setting are judged."""
from __future__ import annotations

import os
import tempfile

import conftest


def test_every_test_starts_with_a_hermes_home_of_its_own(tmp_path):
    assert os.environ["HERMES_HOME"] == str(tmp_path / "home-of-the-test" / ".hermes")
    assert conftest.unsafe_hermes_home((tmp_path,)) == ""


def test_unset_real_and_outside_are_all_refused(tmp_path, monkeypatch):
    real = conftest._REAL_HOME
    with monkeypatch.context() as m:
        m.delenv("HERMES_HOME")
        assert "not set" in conftest.unsafe_hermes_home((tmp_path,))
        for bad in (real / ".hermes", real / ".hermes" / "plugin-data" / "grok-bot-twin", real):
            m.setenv("HERMES_HOME", str(bad))
            assert "real Hermes folder" in conftest.unsafe_hermes_home((tmp_path, tempfile.gettempdir(), real))
        m.setenv("HERMES_HOME", str(tmp_path.parent / "not-this-tests-folder"))
        assert "outside" in conftest.unsafe_hermes_home((tmp_path,))
        m.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        assert conftest.unsafe_hermes_home((tmp_path,)) == ""
    assert conftest.unsafe_hermes_home((tmp_path,)) == ""


def test_no_test_calls_undo():
    here = os.path.dirname(os.path.abspath(__file__))
    word = "monkeypatch." + "undo("
    found = [n for n in sorted(os.listdir(here)) if n.startswith("test_") and n.endswith(".py")
             and word in open(os.path.join(here, n), encoding="utf-8").read()]
    assert found == []
