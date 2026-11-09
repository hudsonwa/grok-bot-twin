"""Cleaning for untrusted text (bot names, descriptions, anything from botport or Grok).

- clean_name / clean_summary: control chars stripped, whitespace collapsed to one line, suspected secrets masked
  (twin/secrets.py), capped.
- for_llm: the above plus links, e-mail addresses, file paths and key-like values replaced, and the
  awareness-block delimiters neutralised so a name can never close or fake the data block.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from . import secrets as _secrets

NAME_CAP = 60
SUMMARY_CAP = 120

_CTRL = re.compile("[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u206f\\ufeff]")
_WS = re.compile(r"\s+")
_URL = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PATH = re.compile(r"(?:(?<=\s)|^)(?:~|\.{1,2})?/[^\s]*|\b[A-Za-z]:\\[^\s]*")
_MARKER = re.compile(r"(?i)\b(?:end|begin)\s+grok\s+bot\s+twin\b")


def _strip(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = unicodedata.normalize("NFKC", text)
    text = _CTRL.sub(" ", text)
    return _WS.sub(" ", text).strip()


def cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def clean_name(value: Any, limit: int = NAME_CAP) -> str:
    """A bot/routine/skill name or short label: one line, capped, suspected secrets masked (a check, not a
    guarantee; the detector runs before the cap so a key cut in half is still caught)."""
    return cap(_secrets.redact(_strip(value), "[secret]"), limit)


def clean_summary(value: Any, limit: int = SUMMARY_CAP) -> str:
    """Descriptions and messages: as clean_name, plus links, e-mail addresses and paths replaced."""
    return cap(redact(_strip(value)), limit)


def redact(text: str) -> str:
    text = _secrets.redact(text, "[secret]")
    text = _URL.sub("[link]", text)
    text = _EMAIL.sub("[email]", text)
    text = _PATH.sub("[path]", text)
    return text


def for_llm(value: Any, limit: int = NAME_CAP) -> str:
    """One-line, delimiter-safe, capped text for the pre_llm_call data block and tool output."""
    text = redact(_strip(value))
    text = _MARKER.sub("(marker removed)", text)
    text = text.replace("[", "(").replace("]", ")").replace("«", "\"").replace("»", "\"")
    text = text.replace("`", "'").replace("<", "(").replace(">", ")")
    return cap(text, limit) or "(unnamed)"


def clean_obj(value: Any, limit: int = SUMMARY_CAP, depth: int = 0) -> Any:
    """Recursively clean every string in a JSON-like object for LLM-facing tool output."""
    if depth > 6:
        return None
    if isinstance(value, str):
        return clean_summary(value, limit)
    if isinstance(value, dict):
        return {str(k)[:40]: clean_obj(v, limit, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, list):
        return [clean_obj(v, limit, depth + 1) for v in value[:60]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return clean_summary(str(value), limit)


TWIN_FALLBACK = "your Grok twin"
_SAFE_NAME = re.compile(r"[A-Za-z0-9 ._-]{1,40}")


def llm_name(value: Any) -> str:
    """A bot name as shown to the model (tool results only): kept only when it is 1-40 characters of
    [A-Za-z0-9 ._-] and holds no suspected secret (an untrusted name never carries markup, instructions or a key
    to the model); anything else becomes the fixed words "your Grok twin"."""
    text = value.strip() if isinstance(value, str) else ""
    if text and _SAFE_NAME.fullmatch(text) and text.strip(" ._-") and not _secrets.has_secret(text):
        return text
    return TWIN_FALLBACK
