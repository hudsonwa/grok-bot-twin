"""R14: the manifest's description is a short summary that points to the one disclosure (docs/DISCLOSURE.md).

`hermes plugins list` prints the description; at 2,464 words it filled about a thousand terminal lines and was a
third copy of the disclosure. The disclosure itself lives only in docs/DISCLOSURE.md.
"""
from __future__ import annotations

import yaml

from twin_testlib import ROOT


def _description() -> str:
    doc = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert isinstance(doc, dict) and isinstance(doc.get("description"), str)
    return doc["description"]


def test_R14_the_manifest_description_is_short_and_points_to_the_disclosure():
    text = _description()
    assert 0 < len(text.split()) <= 60, len(text.split())
    assert "docs/DISCLOSURE.md" in text
    assert (ROOT / "docs" / "DISCLOSURE.md").is_file()
    assert "not affiliated with xAI" in text


def test_R14_the_fact_that_was_only_in_the_manifest_is_in_the_disclosure():
    disclosure = " ".join((ROOT / "docs" / "DISCLOSURE.md").read_text(encoding="utf-8").split())
    assert "Nothing is clicked or typed in the Grok app: you paste the request into each bot yourself." in disclosure
