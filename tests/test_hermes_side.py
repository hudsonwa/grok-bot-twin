"""The plugin's own reads and writes inside a twin's Hermes profile folder, and the pinned-release table.

Everything runs in a throwaway HERMES_HOME with invented text.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest

from twin_testlib import ROOT, assert_private, central, fake_key, home  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import hermes_side as hs
from hermes_plugins.grok_bot_twin.twin import pins, store

BOTPORT_SOUL = ("<!-- botport:begin persona-description -->\nAnswers questions about Acme widgets.\n"
                "<!-- botport:end persona-description -->\n<!-- botport:begin persona-name -->\nAcme Support\n"
                "<!-- botport:end persona-name -->\n")


def _profile(home, name="acme-support"):
    d = home / "profiles" / name
    d.mkdir(parents=True, mode=0o700)
    return d


# ---- SOUL.md ---------------------------------------------------------------------------------------------------
def test_soul_facts_reads_what_botport_keeps_and_whether_there_are_instructions(home):
    prof = _profile(home)
    assert hs.soul_facts(prof) == {"name": "", "description": "", "own": "", "has_instructions": False}
    (prof / "SOUL.md").write_text(BOTPORT_SOUL, encoding="utf-8")
    assert hs.soul_facts(prof) == {"name": "Acme Support", "description": "Answers questions about Acme widgets.",
                                   "own": "", "has_instructions": False}
    # botport's own instructions section (a twin started from an export) counts, and so does hand-written text
    (prof / "SOUL.md").write_text(BOTPORT_SOUL + "<!-- botport:begin persona -->\nBe brief.\n<!-- botport:end persona -->\n")
    assert hs.soul_facts(prof)["has_instructions"] is True
    (prof / "SOUL.md").write_text("Be kind.\n" + BOTPORT_SOUL)
    assert hs.soul_facts(prof)["has_instructions"] is True


def test_pasted_instructions_go_into_their_own_block_and_nothing_else_changes(home):
    prof = _profile(home)
    (prof / "SOUL.md").write_text(BOTPORT_SOUL, encoding="utf-8")
    assert hs.write_instructions(prof, "You are Acme Support.\nBe friendly and brief.") == ""
    text = (prof / "SOUL.md").read_text(encoding="utf-8")
    assert text.startswith(BOTPORT_SOUL) and text.endswith(hs.END + "\n")
    assert_private(prof / "SOUL.md")
    facts = hs.soul_facts(prof)
    assert facts["own"] == "You are Acme Support.\nBe friendly and brief." and facts["has_instructions"] is True
    assert facts["name"] == "Acme Support"  # botport's sections still read the same
    # a second paste replaces the block and hands back what was there
    assert hs.write_instructions(prof, "Second version.") == "You are Acme Support.\nBe friendly and brief."
    assert (prof / "SOUL.md").read_text().count(hs.BEGIN) == 1
    # an empty text takes the block out again: the file is exactly what botport wrote
    assert hs.write_instructions(prof, "") == "Second version."
    assert (prof / "SOUL.md").read_text(encoding="utf-8") == BOTPORT_SOUL
    assert hs.has_markers("fine\n<!-- botport:begin persona -->\nx") and hs.has_markers("<!--grok-bot-twin:end x")
    assert not hs.has_markers("An ordinary <!-- comment --> is fine")


def test_a_linked_profile_or_soul_file_is_never_written(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "profiles").mkdir(mode=0o700)
    (home / "profiles" / "linked").symlink_to(outside)
    with pytest.raises(OSError):
        hs.write_instructions(home / "profiles" / "linked", "text")
    assert list(outside.iterdir()) == []
    prof = _profile(home, "acme-two")
    target = tmp_path / "elsewhere.md"
    target.write_text("keep me", encoding="utf-8")
    (prof / "SOUL.md").symlink_to(target)
    with pytest.raises(OSError):
        hs.write_instructions(prof, "text")
    assert target.read_text() == "keep me"
    assert hs.soul_facts(prof)["has_instructions"] is False  # a link is not read either


# ---- the model ---------------------------------------------------------------------------------------------------
def test_profile_model_follows_botports_rule(home):
    prof = _profile(home)
    assert hs.profile_model(prof) == ""  # no config file: no model
    for text, want in (("model: example-model\n", "example-model"), ("model:\n  default: m1\n  provider: p\n", "m1"),
                       ("model:\n  name: m2\n", "m2"), ("model:\n  provider: p\n", ""), ("other: 1\n", ""),
                       ("model: ''\n", "")):
        (prof / "config.yaml").write_text(text, encoding="utf-8")
        assert hs.profile_model(prof) == want, text
    (prof / "config.yaml").write_text("model: [unclosed\n", encoding="utf-8")
    assert hs.profile_model(prof) is None  # cannot tell: the page does not nag
    assert hs.profile_model(None) is None


def test_a_new_twin_gets_the_owners_default_model_and_nothing_secret(home):
    prof = _profile(home)
    assert hs.copy_default_model(prof, home) is None  # the owner has no default model
    (home / "config.yaml").write_text(
        "model:\n  default: example-model\n  provider: example\n  api_key: " + fake_key() + "\n"
        "  base_url: https://models.example.invalid/v1\n"
        "fallback_providers:\n- provider: other\n  model: small-model\n  token: " + fake_key() + "\n"
        "mcp_servers:\n  files:\n    url: https://files.example.invalid\n", encoding="utf-8")
    assert hs.copy_default_model(prof, home) == "example-model"
    import yaml
    got = yaml.safe_load((prof / "config.yaml").read_text(encoding="utf-8"))
    assert got == {"model": {"default": "example-model", "provider": "example"},
                   "fallback_providers": [{"provider": "other", "model": "small-model"}]}
    assert fake_key() not in (prof / "config.yaml").read_text() and "invalid" not in (prof / "config.yaml").read_text()
    assert_private(prof / "config.yaml")
    assert hs.profile_model(prof) == "example-model"
    assert hs.copy_default_model(prof, home) is None  # it has a model now: never overwritten
    assert hs.copy_default_model(home, home) is None  # the default profile is never its own twin


def test_default_model_is_added_to_an_existing_config_without_losing_a_line(home):
    prof = _profile(home)
    (home / "config.yaml").write_text("model: example-model\n", encoding="utf-8")
    before = "# my notes\nplugins:\n  enabled:\n  - something\n"
    (prof / "config.yaml").write_text(before, encoding="utf-8")
    assert hs.copy_default_model(prof, home) == "example-model"
    after = (prof / "config.yaml").read_text(encoding="utf-8")
    assert after.startswith(before) and after.endswith("model: example-model\n")


# ---- one skill's text ---------------------------------------------------------------------------------------------
def test_skill_text_reads_one_plain_named_skill_only(home, tmp_path):
    prof = _profile(home)
    d = prof / "skills" / "imported" / "pick-a-book"
    d.mkdir(parents=True, mode=0o700)
    os.chmod(prof / "skills", 0o700)
    os.chmod(prof / "skills" / "imported", 0o700)
    (d / "SKILL.md").write_text("# Pick a book\nSuggest one book.\n", encoding="utf-8")
    assert hs.skill_text(prof, "pick-a-book") == ("# Pick a book\nSuggest one book.\n", False)
    assert hs.skill_text(prof, "no-such-skill") is None
    for bad in ("../pick-a-book", "imported/pick-a-book", "", None, "-x", "a b"):
        assert hs.skill_text(prof, bad) is None
    (d / "SKILL.md").write_text("x" * (hs.SKILL_CAP + 10), encoding="utf-8")
    text, truncated = hs.skill_text(prof, "pick-a-book")
    assert truncated is True and len(text) == hs.SKILL_CAP
    # a skill folder that is a link out of the profile is not followed
    secret = tmp_path / "private"
    secret.mkdir()
    (secret / "SKILL.md").write_text("not yours to read", encoding="utf-8")
    (prof / "skills" / "imported" / "linked").symlink_to(secret)
    assert hs.skill_text(prof, "linked") is None


# ---- earlier versions of pasted instructions ------------------------------------------------------------------------
def test_kept_versions_are_capped_named_and_shown_without_password_lines(home):
    c = central(home)
    store.ensure_dir(c)
    first = hs.keep(c, "acme-support", "", "Before you added the instructions", "paste")
    assert first.startswith("persona.instructions@")
    text = "Line one.\npassword: " + fake_key() + "\nLine three."
    rid = hs.keep(c, "acme-support", text, "Before you changed the instructions", "paste")
    pts = hs.kept_list(c, "acme-support")
    assert [p["id"] for p in pts] == [first, rid] and pts[1]["why"] == "paste"
    assert hs.shown_version(text) == "Line one.\n" + hs.HIDDEN_LINE + "\nLine three."
    assert hs.shown_version(None) is None and hs.shown_version("") == ""
    for i in range(hs.KEPT_MAX + 5):
        hs.keep(c, "acme-support", f"version {i}", "Before you changed the instructions", "paste")
    assert len(hs.kept_list(c, "acme-support")) == hs.KEPT_MAX
    hs.kept_drop(c, "acme-support")
    assert hs.kept_list(c, "acme-support") == []
    assert_private(c / "kept.json")


# ---- the pinned release table -----------------------------------------------------------------------------------
def test_download_is_the_right_file_for_each_kind_of_computer():
    for system, machine, want in (("Darwin", "arm64", "botport-aarch64-apple-darwin.tar.gz"),
                                  ("Darwin", "x86_64", "botport-x86_64-apple-darwin.tar.gz"),
                                  ("Linux", "x86_64", "botport-x86_64-unknown-linux-musl.tar.gz"),
                                  ("Linux", "aarch64", "botport-aarch64-unknown-linux-musl.tar.gz")):
        d = pins.download_for("v0.2.1", pins.platform_target(system, machine))
        assert d == {"url": f"https://github.com/hudsonwa/botport/releases/download/v0.2.1/{want}", "file_name": want}
    assert pins.platform_target("Windows", "AMD64") is None and pins.platform_target("Linux", "riscv64") is None
    assert pins.platform_target("FreeBSD", "x86_64") is None
    # no user input reaches the address: a tag that is not like v0.2.1 or an unknown target gives nothing
    for bad_tag in ("latest", "v0.2.1/../../x", "v1", "", None, "v0.2.1?x=1"):
        assert pins.download_for(bad_tag, "aarch64-apple-darwin") is None
    assert pins.download_for("v0.2.1", "sparc-sun-solaris") is None


def test_pin_table_accepts_only_exact_digests_for_known_files():
    good = "a" * 64
    try:
        pins.set_table({"releases": {
            "v0.2.1": {"archives": {"botport-aarch64-apple-darwin.tar.gz": good, "evil.tar.gz": good,
                                    "botport-x86_64-apple-darwin.tar.gz": "short"},
                       "programs": {"aarch64-apple-darwin": "b" * 64, "made-up-target": good}},
            "not-a-tag": {"archives": {}, "programs": {"aarch64-apple-darwin": good}}}})
        assert pins.known("v0.2.1") and not pins.known("v9.9.9") and not pins.known("not-a-tag")
        assert pins.archive_target(good, "v0.2.1") == "aarch64-apple-darwin"
        assert pins.program_target("b" * 64, "v0.2.1") == "aarch64-apple-darwin"
        assert pins.program_target(good, "v0.2.1") is None and pins.archive_target("b" * 64, "v0.2.1") is None
        assert pins.program_target("b" * 64, "v0.2.0") is None and pins.program_target(None, "v0.2.1") is None
    finally:
        pins.set_table(None)


def test_the_shipped_table_is_well_formed():
    """Whatever the file holds must be digests of known files; an empty release entry is allowed and means
    "not pinned" (then the plugin offers the GitHub check)."""
    raw = json.loads(pins.PINS_FILE.read_text(encoding="utf-8"))
    pins.set_table(None)
    table = pins.load()
    assert set(table) == set(raw["releases"])
    for tag, entry in raw["releases"].items():
        assert set(entry) == {"archives", "programs"}
        assert table[tag] == entry, "an entry that the loader would drop is in the file"
        assert set(entry["programs"]) <= set(pins.TARGETS)
        assert set(entry["archives"]) <= {pins.asset_name(t) for t in pins.TARGETS}


def test_the_expected_release_is_pinned_for_every_kind_of_computer():
    """Until this holds, a downloaded botport file is kept "not verified yet" and verifying it needs GitHub's
    tool: the page's "verified on the spot, no terminal" is true only with a filled table."""
    from hermes_plugins.grok_bot_twin.twin import engine
    pins.set_table(None)
    entry = pins.load().get(engine.EXPECTED_BOTPORT_TAG) or {"programs": {}, "archives": {}}
    assert set(entry["programs"]) == set(pins.TARGETS)
    assert set(entry["archives"]) == {pins.asset_name(t) for t in pins.TARGETS}


@pytest.mark.skipif(not os.environ.get("BOTPORT_RELEASE_DIR"),
                    reason="set BOTPORT_RELEASE_DIR to a folder with the release archives (and BOTPORT_RELEASE_TAG) "
                           "to recompute the table from the release files")
def test_the_table_matches_the_release_files():
    """Recompute the digests from the downloaded release files; the shipped table must say the same."""
    from pathlib import Path
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import pin_botport
    finally:
        sys.path.pop(0)
    tag = os.environ.get("BOTPORT_RELEASE_TAG") or "v0.2.1"
    pins.set_table(None)
    entry = pins.load().get(tag) or {"archives": {}, "programs": {}}
    seen = 0
    for target in pins.TARGETS:
        path = Path(os.environ["BOTPORT_RELEASE_DIR"]) / pins.asset_name(target)
        if not path.is_file():
            continue
        seen += 1
        data = path.read_bytes()
        assert entry["archives"].get(pins.asset_name(target)) == hashlib.sha256(data).hexdigest(), target
        assert entry["programs"].get(target) == hashlib.sha256(pin_botport.program_from_archive(data)).hexdigest()
    assert seen, "no release archive in BOTPORT_RELEASE_DIR"


def test_the_release_tool_writes_only_what_it_checked(tmp_path):
    """tools/pin_botport.py on an invented archive: a dry run prints the two digests, and without the attestation
    check nothing is written unless that is asked for in so many words."""
    import io
    import tarfile
    program = b"\xcf\xfa\xed\xfe" + b"invented program bytes"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("botport-aarch64-apple-darwin/botport")
        info.size = len(program)
        tar.addfile(info, io.BytesIO(program))
    (tmp_path / "botport-aarch64-apple-darwin.tar.gz").write_bytes(buf.getvalue())
    before = pins.PINS_FILE.read_bytes()
    run = lambda *a: subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "pin_botport.py"), str(tmp_path),  # noqa: E731
                                     "--tag", "v0.2.1", *a], capture_output=True, text=True, timeout=60,
                                    stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"})
    cp = run("--no-verify")
    assert cp.returncode == 0 and hashlib.sha256(program).hexdigest() in cp.stdout
    assert hashlib.sha256(buf.getvalue()).hexdigest() in cp.stdout and "dry run" in cp.stdout
    cp = run("--no-verify", "--write")
    assert cp.returncode != 0 and "not verified" in cp.stderr
    cp = run("--write")  # the default needs gh; this PATH has none, so it stops before anything is read
    assert cp.returncode != 0 and "gh is not installed" in cp.stderr
    assert pins.PINS_FILE.read_bytes() == before


@pytest.mark.parametrize("mark", ["\u200b", "\u2060", "\ufeff", "\u202e"])
def test_a_key_with_an_invisible_character_inside_is_hidden_all_the_same(mark):
    """The page drops invisible characters from what it shows, so the password-and-key check reads the text without
    them: one in the middle of a key must not hide the key from the check and then vanish from the shown text."""
    key = fake_key()
    split = key[:8] + mark + key[8:]
    for shown in (hs.shown("Uses " + split + " for the maps."), hs.shown_version("line one\nUses " + split + "\n")):
        assert key not in shown and split not in shown
