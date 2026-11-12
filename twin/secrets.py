"""The plugin's one password-and-key check (every module asks here). Reports kind + line + shape only; never
returns or logs the value. The strictest policy the plugin had: key formats, private key blocks, short and long
"name = value" credentials, a password or code said in words, sign-in values, card and account numbers, random words;
read as written, in plain (NFKC) form without invisible characters, and without accents. A check, not a guarantee.
"""
from __future__ import annotations

import bisect
import math
import re
import unicodedata

NOTE = "this check can miss things"

_B, _E = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"
# Words that almost always name a secret: followed (maybe after "is", ":" or "=") by a value that holds a digit,
# they are one (a wifi password, a door code or a passcode written out with digits). A sentence ABOUT a password ("PIN codes are 4 digits",
# "the Wi-Fi password is on the fridge") is not: only credential-SHAPED text is held, so ordinary instructions pass.
_STRONG = (
    r"pass ?words?|passwd|pass ?phrases?|pass ?codes?|pwd?|pin(?: ?codes?| ?numbers?)?|api[ _-]?keys?|combinations?|"
    r"wi-?fi|passwort|kennwort|contrase[nñ]a|mot de passe|senha|wachtwoord|"
    r"access[ _-]?keys?|licen[cs]e[ _-]?keys?|recovery (?:codes?|keys?)|backup codes?|security codes?|"
    r"(?:door|gate|alarm|access|entry|lock|safe|unlock|verification|one[ -]?time)[ _-]?codes?|otp|cvv|cvc")
# "is required", "password: string": what a text (or a schema, or a script) says ABOUT a password, not one
_NOT_A_VALUE = (r"(?!\s*[\"']?(?:not|never|always|required|needed|optional|missing|wrong|incorrect|invalid|valid|"
                r"correct|case|empty|blank|hidden|masked|shown|stored|kept|sent|reset|changed|set|same|different|"
                r"strong|weak|secure|encrypted|hashed|given|provided|shared|asked|requested|unknown|the|a|an|"
                r"string|str|boolean|bool|number|integer|int|null|none|true|false|yes|no|changeme|example|your|"
                r"undefined|redacted|password|secret|token)\b)")

# (kind, human shape, pattern, whole: redact leaves it to `line_marker` (masking only the match would show the rest,
# or hide a file path that only looks like a code); a group, when there is one, is the value: only it is masked)
_PATTERNS = [
    ("private_key", "private key block", re.compile(r"(?i)-{2,}\s*BEGIN [A-Z0-9 ]*PRIVATE KEY"), False),
    ("api_key", "shaped like a token (sk-/xai-)",
     re.compile(r"(?<![A-Za-z0-9])(?:sk|xai|sk-proj|sk-ant)-[A-Za-z0-9_-]{20,}"), False),
    ("stripe_key", "shaped like a token (sk_live_/rk_live_)",
     re.compile(r"(?<![A-Za-z0-9])(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"), False),
    ("github_token", "shaped like a token (gh*_)",
     re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), False),
    ("aws_key", "shaped like a token (AKIA...)", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])"),
     False),
    ("google_api_key", "shaped like a token (AIza...)", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}"), False),
    ("slack_token", "shaped like a token (xox*-)", re.compile(r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{6,}"), False),
    ("hf_token", "shaped like a token (hf_)", re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{20,}"), False),
    ("npm_token", "shaped like a token (npm_)", re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{20,}"), False),
    ("credential", "password or token assignment", re.compile(
        r"(?i)(?<![A-Za-z0-9])key\s*[:=]\s*(\S{12,})"
        r"""|(?<![A-Za-z0-9])(?:api[_-]?key|token|secret|password|passwd|pwd|passphrase)["']?\s*[:=]""" + _NOT_A_VALUE
        + r"""\s*(?:"([^"\n]{4,400})"|'([^'\n]{4,400})'|(\S{4,}))"""
        r"|(?-i:(?<![A-Za-z0-9])[A-Z][A-Z0-9_]{0,60}(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|CREDENTIALS?)"
        r"\s*[:=]\s*(\S+))"), False),
    ("sign_in_value", "Authorization value, signed link or address with a password", re.compile(
        r"(?i)\b(?:authorization\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?|(?:bearer|basic)\s+)"
        r"(?=\S{0,200}[0-9=])([A-Za-z0-9._~+/=-]{8,})"
        r"|(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,20}://[^\s/:@]{1,80}:([^\s/@]{1,80})@"
        r"|[?&](?:access_token|api_key|apikey|auth|key|password|secret|sig|signature|token)=([^\s&]{6,})"
        r"|\b(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"), True),
    ("said_in_words", "a password, PIN or code stated in words", re.compile(
        rf"(?i){_B}(?:{_STRONG}){_E}(?:[^\n]{{0,24}}?(?:\b(?:is|are|was|were)\b|[:=]))?\s*[\"']?"
        r"(?=[^\s\"']{0,60}[0-9])[^\s\"']{4,}"), True),
    ("account_number", "bank account number",
     re.compile(r"\b[A-Z]{2}[0-9]{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b"), True),
    ("random_word", "long word that looks random", re.compile(
        r"\b[A-Fa-f0-9]{24,}\b|(?<![A-Za-z0-9+_=-])(?=[A-Za-z0-9+_=-]{0,200}[0-9])(?=[A-Za-z0-9+_=-]{0,200}[a-z])"
        r"(?=[A-Za-z0-9+_=-]{0,200}[A-Z])[A-Za-z0-9+_=-]{20,}"), False),
]
_WORD = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_DIGIT_RUN = re.compile(r"(?<![0-9])(?:[0-9][ .\-/]?){13,19}(?![0-9])")
_PRIVATE_BLOCK = re.compile(r"-{2,}\s*BEGIN [A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*-----.*?"
                            r"(?:-----\s*END [A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*-----|\Z)", re.I | re.S)

WHAT = {kind: what for kind, what, _rx, _whole in _PATTERNS}
WHAT["card_number"] = "payment card number"


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


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _matches(text: str):
    """(kind, match) for every suspected secret in `text`, as written."""
    for kind, _what, rx, _whole in _PATTERNS:
        for m in rx.finditer(text):
            yield kind, m
    for m in _PRIVATE_BLOCK.finditer(text):
        yield "private_key", m
    for m in _WORD.finditer(text):
        if _random_word(m.group(0)):
            yield "random_word", m
    for m in _DIGIT_RUN.finditer(text):
        if _luhn(re.sub(r"[^0-9]", "", m.group(0))):
            yield "card_number", m


def _readings(text: str) -> list:
    """The text as written; in its plain (NFKC) form without invisible (format) characters; and without accents
    or variation selectors ("pássword"). Line for line the same text (no reading adds or drops a line break)."""
    plain = "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf")
    bare = "".join(c for c in unicodedata.normalize("NFKD", plain) if unicodedata.category(c) != "Mn")
    return list(dict.fromkeys((text, plain, bare)))


def _line_starts(text: str) -> list:
    """Where each line of `text` starts, found once (counting line ends anew for every match took minutes on a
    large text with many matches)."""
    starts, i = [0], text.find("\n")
    while i != -1:
        starts.append(i + 1)
        i = text.find("\n", i + 1)
    return starts


def flagged_lines(text: str) -> set:
    """Every line (1-based) a suspected secret touches: all the lines a match covers, not only the one it starts
    on (a label on one line and its value on the next), and every line of a private key block."""
    if not isinstance(text, str) or not text:
        return set()
    out: set = set()
    for reading in _readings(text):
        starts = _line_starts(reading)
        for _kind, m in _matches(reading):
            a, b = m.start(), m.end()
            out.update(range(bisect.bisect_right(starts, a), bisect.bisect_right(starts, max(a, b - 1)) + 1))
    return out


def detect_secrets(text: str) -> list[dict]:
    """[{"kind", "what", "line"}] per suspected secret (1-based line), one finding per line."""
    if not isinstance(text, str) or not text:
        return []
    found: dict = {}
    for reading in _readings(text):
        starts = _line_starts(reading)
        for kind, m in _matches(reading):
            found.setdefault(bisect.bisect_right(starts, m.start()), kind)
    return sorted(({"kind": k, "what": WHAT[k], "line": n} for n, k in found.items()),
                  key=lambda f: (f["line"], f["kind"]))


def has_secret(text: str) -> bool:
    """THE check: does this text look like it holds a password or key."""
    return isinstance(text, str) and any(next(_matches(reading), None) for reading in _readings(text))


def redact(text: str, marker: str = "[secret]", line_marker: str = "") -> str:
    """Replace suspected secrets with a marker where they are (display/LLM cleaning and previews; never used for
    syncing): keys and token formats, the value of a named credential or sign-in value (its name stays), account and
    card numbers; with `line_marker`, also every line that still looks like it holds a password (said in words)."""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    def mask(m: "re.Match[str]") -> str:  # (a value that is already the marker stays: masking twice is a no-op)
        g = m.lastindex or 0
        if m.string.startswith(marker, m.start(g)):
            return m.group(0)
        return m.group(0)[:m.start(g) - m.start()] + marker + m.group(0)[m.end(g) - m.start():]

    out = _PRIVATE_BLOCK.sub(marker, text)
    for kind, _what, rx, whole in _PATTERNS:
        if not whole or (not line_marker and kind in ("sign_in_value", "account_number")):
            out = rx.sub(mask, out)
    def card(m: "re.Match[str]") -> str:  # a Luhn-valid card number goes; a separator after it stays
        run = m.group(0).rstrip(" .-/")
        return marker + m.group(0)[len(run):] if _luhn(re.sub(r"[^0-9]", "", run)) else m.group(0)
    if not line_marker:  # (with line_marker, such a line goes whole below)
        out = _DIGIT_RUN.sub(card, out)
    out = _WORD.sub(lambda m: marker if _random_word(m.group(0)) else m.group(0), out)
    if not line_marker:
        return out
    return "\n".join(line_marker if has_secret(ln) else ln for ln in out.split("\n"))


def location(kind: str, line: int, what: str) -> str:
    """One sentence, never the value (contract v1.2 `location`)."""
    return f"in the {kind}, line {line}: looks like a password or key ({what}; {NOTE})"


def withhold(text: str, item: str, kind: str, direction: str = "g2h") -> tuple[bool, list[dict]]:
    """Held-back-whole policy: (hold, reports). When hold is True the item is not copied at all.
    Reports are {"item","kind","location","direction"} and never carry the value."""
    found = detect_secrets(text)
    reports = [{"item": item, "kind": kind, "direction": direction,
                "location": location(kind, f["line"], f["what"])} for f in found[:5]]
    return bool(found), reports


def held_back_text(n: int) -> str:
    """Fixed, calm wording (never "refused"): '1 thing that looks like a password or key was not copied'."""
    if n == 1:
        return "1 thing that looks like a password or key was not copied"
    return f"{n} things that look like a password or key were not copied"
