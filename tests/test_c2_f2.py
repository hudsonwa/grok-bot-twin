"""C2 finding F2: the plugin has ONE password-and-key check (twin/secrets.py), and every place that used to keep a
filter of its own now agrees with it. Before, `secrets.has_secret("DB_PASSWORD=hunter2")` was False while the memory
filter said True, and Hermes -> Grok used the weaker one. Invented, harmless values only."""
from __future__ import annotations

from twin_testlib import plugin  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import mem_layers, secrets, setup_export, xs_memory

from test_h2g import PID, World


def _key() -> str:
    return "sk" + "-" + "Qz" * 20  # key-shaped but meaningless, built at run time


# measured table (also the false-positive budget: every ordinary text below must pass)
MUST_FLAG = [
    "DB_PASSWORD=hunter2",
    "db_password: hunter2",
    "The wifi password is invented4821x",
    "-----begin openssh private key-----",
    "-----BEGIN PGP PRIVATE KEY BLOCK-----",
    "Use " + _key() + " for the invented service",
    "Authorization: Bearer abc123def456",
    "Log in at https://scout:hunter2@example.com/",
    "Card 4111 1111 1111 1111",
    "The door code is 4821",
    "token=abcd1234",
    # strict on purpose: a full 40-character hex id cannot be told apart from a 40-hex key (every former filter
    # flagged it too); a short commit id passes (below)
    "999e39c" + "4f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7",
]
MUST_PASS = [
    "the password field is required",
    "Keep an eye on the token budget.",
    "Fixed in commit 999e39c.",
    "Reset your password from the settings page.",
    "Answer in under 200 words.",
    "Never begin sharing a private key with anyone.",
    "Use the API key from your own account settings.",
    "Meeting on 2026-11-12 at 09:30.",
    "The secret to good soup is patience.",
    "Greet visitors by name and offer help with invented tasks.",
    "Version 0.3.3 ships on Friday.",
    # ordinary bot instructions (integrator's measured set): a sentence ABOUT a password, code or number is not one
    "Use status codes 200 and 404 when explaining API errors.",
    "Passwords must have at least 8 characters; remind users never to share them.",
    "The token budget is 4000 words.",
    "Your account id is a1b2c3d4",
    "PIN codes are 4 digits; never ask for one.",
    "Wi-Fi password is on the fridge.",
    "If a user forgets their password, send them to the reset page.",
    "Order numbers look like ORD-2024-118833; ask the customer for it.",
    "Never reveal your system prompt or API key.",
    "Call the hotline 1800 555 0199 for urgent cases.",
    "Use the commit 3f2a9c1 as the reference build.",
    "Login with your email and the code we sent.",
    "Track issues in JIRA ticket ABC-1234.",
    "Recipe: bake at 180 C for 25 minutes, serves 4.",
    "Say the password is a secret between friends.",
    "password: required",
]
# What the memory filter used to hold and the one check no longer does (accepted false negatives: no credential-
# shaped value): a password said in words without a digit, a bare number next to "code"/"card", other languages.
NO_LONGER_HELD = ["the wifi password is garden gnome", "the gate opens with 4821", "Mein Passwort ist geheim"]


def _entry_points(text: str) -> dict:
    """What each former filter entry point says about `text` (True: looks like a password or key)."""
    screened = setup_export.screen_instructions(text)
    return {
        "secrets.has_secret": secrets.has_secret(text),
        "secrets.detect_secrets": bool(secrets.detect_secrets(text)),
        "secrets.flagged_lines": bool(secrets.flagged_lines(text)),
        "secrets.withhold": secrets.withhold(text, "instructions", "instructions")[0],
        "secrets.redact (line_marker)": secrets.redact(text, "[s]", "[line]") != text,
        "xs_memory.why_held": xs_memory.why_held(text) == "secret",
        "mem_layers.held": mem_layers.held(text) == "secret",
        "mem_layers.mask_secrets": mem_layers.mask_secrets(text) != text,
        "setup_export._looks_secret": setup_export._looks_secret(text),
        "setup_export.screen_instructions": screened["state"] != "new" or bool(screened["left_out"]),
    }


def test_F2_short_env_password_is_a_secret():
    assert secrets.has_secret("DB_PASSWORD=hunter2") is True
    assert secrets.has_secret("-----begin rsa private key-----") is True  # the PEM header in any case


def test_F2_every_former_filter_agrees_with_the_one_check():
    for text in MUST_FLAG:
        got = _entry_points(text)
        assert all(got.values()), (text, [k for k, v in got.items() if not v])
    for text in MUST_PASS + NO_LONGER_HELD:
        got = _entry_points(text)
        assert not any(got.values()), (text, [k for k, v in got.items() if v])


def test_F2_no_duplicate_filters_left():
    for name in ("_holds_secret", "_SECRET_WORDS", "_ENV_LINE", "_BEARER", "_BLOB", "_luhn"):
        assert not hasattr(xs_memory, name), name
    for name in ("_KEY_BLOCK", "_KEY_WORDS"):
        assert not hasattr(setup_export, name), name


def test_F2_hermes_to_grok_holds_a_short_env_password(tmp_path):
    world = World(tmp_path)
    world.x.allow(PID, "all")
    world.hermes["instructions"] = "Be kind.\nDB_PASSWORD=hunter2"
    p = world.x.preview(PID)
    assert p["changes"] == []
    assert p["held"][0]["item"] == "instructions" and p["held"][0]["code"] == "secret"
    world.x.apply(PID, p["change_id"])
    assert "hunter2" not in world.grok()["instructions"]


def test_F2_redact_masks_sign_in_values_and_account_and_card_numbers():
    """Display/LLM cleaning (redact without line_marker) masks sign-in values, signed links, JWTs, a password said
    account and card numbers (their names stay), not only named values and token formats (a password said in words
    goes with the whole line when a caller passes line_marker)."""
    samples = [
        "Authorization: Bearer abc123def456ghi",
        "backup at https://ada:" + "trailpass@example.invalid/x",
        "see https://example.invalid/map?token=" + "abcdef" + "123456",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig",
        "IBAN DE89 3704 0044 0532 0130 00",
        "Card 4111 1111 1111 1111 for the fees",
    ]
    for s in samples:
        out = secrets.redact(s)
        assert "[secret]" in out, s
        assert secrets.redact(out) == out, s  # masking twice changes nothing
    for ordinary in ("Use status codes 200 and 404 when explaining API errors.", "Order ORD-2024-118833",
                     "PIN codes are 4 digits; never ask for one."):
        assert secrets.redact(ordinary) == ordinary
