"""No file of the plugin holds a raw invisible or text-direction character.

Hermes' install scan flags such characters (a zero-width space, a byte order mark, a right-to-left override...)
as possible hidden instructions and blocks a GitHub install. Code that needs one writes it as an escape
(``"\\u200b"``), which reads the same to Python and JavaScript and is visible to a person.
"""
from __future__ import annotations

import unicodedata

from twin_testlib import ROOT

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache", ".venv", "venv"}


def _hidden(ch: str) -> bool:
    # Cf: format characters (zero-width, joiners, BOM, soft hyphen, bidi embeddings/overrides/isolates, tags).
    # Zl, Zp: line and paragraph separators.
    return unicodedata.category(ch) in ("Cf", "Zl", "Zp")


def _files():
    for p in sorted(ROOT.rglob("*")):
        rel = p.relative_to(ROOT)
        if p.is_file() and not SKIP_DIRS.intersection(rel.parts):
            yield p, rel


def test_no_raw_hidden_characters():
    found, scanned = [], 0
    for p, rel in _files():
        try:
            text = p.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue  # not text (images); the install scan reads text files only
        scanned += 1
        for n, line in enumerate(text.splitlines(), 1):
            for ch in line:
                if _hidden(ch):
                    found.append(f"{rel}:{n} U+{ord(ch):04X} {unicodedata.name(ch, '?')}")
                    break
        if "\u2028" in text or "\u2029" in text:  # splitlines() splits on these, so look for them on their own
            found.append(f"{rel}: U+2028/U+2029 line or paragraph separator")
    assert scanned > 50, "the scan read too few files; is ROOT the plugin folder?"
    assert not found, "write these as escapes (\\uXXXX):\n" + "\n".join(found)


def test_the_check_sees_what_hermes_flags():
    for ch in ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u202e", "\u2066", "\u200f"):
        assert _hidden(ch), hex(ord(ch))
    for ch in ("a", " ", "\t", "é", "中"):
        assert not _hidden(ch)
