"""Suspected-secret detection. Reports kind + line + shape only; never returns or logs the value.

It is a check, not a guarantee: known key formats, private key blocks (multi-line), "key = value"
credentials, and (for prose fields that end up in a system prompt) long random-looking words.
"""
from __future__ import annotations

import math
import re

NOTE = "a check, not a guarantee"

# (kind, human shape, compiled pattern)
_PATTERNS = [
    ("private_key", "private key block",
     re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("api_key", "token-shaped value (sk-/xai-)",
     re.compile(r"(?<![A-Za-z0-9])(?:sk|xai|sk-proj|sk-ant)-[A-Za-z0-9_-]{20,}")),
    ("stripe_key", "token-shaped value (sk_live_/rk_live_)",
     re.compile(r"(?<![A-Za-z0-9])(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("github_token", "token-shaped value (gh*_)",
     re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("aws_key", "token-shaped value (AKIA...)", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])")),
    ("google_api_key", "token-shaped value (AIza...)", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}")),
    ("slack_token", "token-shaped value (xox*-)", re.compile(r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{6,}")),
    ("hf_token", "token-shaped value (hf_)", re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{20,}")),
    ("npm_token", "token-shaped value (npm_)", re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{20,}")),
    ("credential", "password or token assignment",
     re.compile(r"(?i)\b(?:api[_-]?key|key|token|secret|password|passwd)\s*[:=]\s*\S{12,}")),
]
_WORD = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_PRIVATE_BLOCK = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)",
                            re.S)

WHAT = {kind: what for kind, what, _ in _PATTERNS}
WHAT["random_word"] = "long random-looking word"


def _entropy(word: str) -> float:
    counts: dict[str, int] = {}
    for ch in word:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(word)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _random_word(word: str) -> bool:
    """32+ chars that look random: long hex, or letters and digits mixed with high entropy."""
    core = word.strip("=-_")
    if len(core) < 32:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{32,}", core):
        return True
    has_digit = any(c.isdigit() for c in core)
    has_alpha = any(c.isalpha() for c in core)
    return has_digit and has_alpha and _entropy(core) >= 4.0


def detect_secrets(text: str, prose: bool = True) -> list[dict]:
    """[{"kind", "what", "line"}] per suspected secret (1-based line), one finding per line.

    prose=True also flags long random-looking words (used for name/description/instructions)."""
    if not isinstance(text, str) or not text:
        return []
    findings: list[dict] = []
    lines_seen: set[int] = set()
    for kind, what, rx in _PATTERNS:
        for m in rx.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            if line in lines_seen:
                continue
            lines_seen.add(line)
            findings.append({"kind": kind, "what": what, "line": line})
    if prose:
        for m in _WORD.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            if line in lines_seen or not _random_word(m.group(0)):
                continue
            lines_seen.add(line)
            findings.append({"kind": "random_word", "what": WHAT["random_word"], "line": line})
    findings.sort(key=lambda f: (f["line"], f["kind"]))
    return findings


def has_secret(text: str) -> bool:
    return bool(detect_secrets(text))


def redact(text: str, marker: str = "[secret]") -> str:
    """Replace suspected secrets with a marker (display/LLM cleaning and previews; never used for syncing)."""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    out = _PRIVATE_BLOCK.sub(marker, text)
    for kind, _what, rx in _PATTERNS:
        if kind != "private_key":
            out = rx.sub(marker, out)
    return _WORD.sub(lambda m: marker if _random_word(m.group(0)) else m.group(0), out)


def location(kind: str, line: int, what: str) -> str:
    """One sentence, never the value (contract v1.2 `location`)."""
    return f"suspected secret in the {kind}, line {line}: {what} ({NOTE})"


def withhold(text: str, item: str, kind: str, direction: str = "g2h") -> tuple[bool, list[dict]]:
    """Held-back-whole policy: (hold, reports). When hold is True the item is not copied at all.
    Reports are {"item","kind","location","direction"} and never carry the value."""
    found = detect_secrets(text)
    reports = [{"item": item, "kind": kind, "direction": direction,
                "location": location(kind, f["line"], f["what"])} for f in found[:5]]
    return bool(found), reports


def held_back_text(n: int) -> str:
    """Fixed, calm wording (never "refused"): '1 suspected secret held back' / 'N suspected secrets held back'."""
    return f"{n} suspected secret{'s' if n != 1 else ''} held back"
