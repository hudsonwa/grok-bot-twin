"""demo roster with a botport on PATH, one profile per twin,
`twin add --hermes-profile`, restore explains the wait for approval, list times relative to the demo start."""
from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from twin_testlib import cli, home, service, svc  # noqa: F401
from hermes_plugins.grok_bot_twin.twin.demo import profile_slug, unique_profile
from hermes_plugins.grok_bot_twin.twin.engine import EngineError

RECIPE = "b-recipe-0002"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class _Untrusted:
    """A helper that is there but not checked yet: never asked for the owner's bots."""
    path = "/nonexistent/botport"

    def grok_list(self, globs):
        raise AssertionError("an unchecked helper must not be run")

    def quick_status(self):
        return {"checked": False, "version": None, "problem": "botport has not been verified yet."}


# --------------------------------------------------------------------------------------------------
def test_sample_bots_are_listed_apart_with_an_unchecked_helper(svc, monkeypatch):
    svc.demo_action("start")
    monkeypatch.setattr(svc, "botport", lambda: _Untrusted())
    res = svc.grok_list()
    # invented bots never pass as the owner's own, and the page learns which fact is missing (not "no Grok app")
    assert res["bots"] == [] and {b["name"] for b in res["sample_bots"]} == {"Acme Support", "Recipe Planner"}
    assert res["ready"] is False and res["helper_found"] is True and res["helper_checked"] is False


def test_checked_helper_and_no_grok_app_says_so(svc, monkeypatch):
    class Empty:
        path = "/nonexistent/botport"

        def grok_list(self, globs):
            return {"app_found": False, "bots": []}

        def quick_status(self):
            return {"checked": True, "version": "0.2.1", "problem": None}

    monkeypatch.setattr(svc, "botport", lambda: Empty())
    # only now (botport verified, the Grok app truly not there) may the page say "Grok app not found"; the bot
    # list is not ready then: ready needs botport AND the Grok app
    assert svc.grok_list() == {"ready": False, "app_found": False, "helper_found": True, "helper_checked": True,
                               "bots": [], "sample_bots": [], "real_hidden": False}


# --------------------------------------------------------------------------------------------------
def test_slug_rules():
    assert profile_slug("Recipe Planner") == "recipe-planner"
    assert profile_slug("  Ünïcode -- Bot!! ") == "n-code-bot"
    assert profile_slug("***") == "bot"
    long = profile_slug("x" * 200)
    assert len(long) == 64 and set(long) == {"x"}
    assert unique_profile("Recipe Planner", {"recipe-planner"}) == "recipe-planner-2"
    assert unique_profile("Recipe Planner", {"recipe-planner", "recipe-planner-2"}) == "recipe-planner-3"
    assert len(unique_profile("y" * 80, {"y" * 64})) == 64


def test_sample_preview_never_names_a_real_profile(svc):
    svc.demo_action("start")  # the sample twin lives under the reserved sample label, not the current profile
    prev = svc.preview([RECIPE])
    assert prev["pairs"][0]["hermes"]["profile"] == "grok-bot-twin-sample-2" and prev["pairs"][0]["sample"] is True
    assert prev["profiles_in_use"] == ["grok-bot-twin-sample"] and prev["skipped"] == []
    assert svc.profile() not in prev["profiles_in_use"]


def test_profile_backing_a_twin_is_refused(svc):
    svc.demo_action("start")
    used = svc.demo._world()["profile"]
    prev = svc.preview([RECIPE], {RECIPE: used})
    assert prev["pairs"] == [] and prev["skipped"] == [
        {"grok_id": RECIPE, "reason": "profile_already_paired", "pair_id": "acme-support", "name": "Recipe Planner"}]
    with pytest.raises(EngineError) as exc:
        svc.create([RECIPE], {RECIPE: used})
    assert exc.value.code == "profile_already_paired"
    assert "recipe-planner" not in svc.pair_ids()
    # the demo engine refuses on its own as well, like botport's profile_already_paired
    direct = svc.demo.create(RECIPE, used, dry_run=False)
    assert direct["pairs"] == [] and direct["skipped"][0]["reason"] == "profile_already_paired"


def test_profile_of_a_real_twin_is_refused_for_demo(svc):
    svc.demo_action("start")
    svc._remember([{"pair_id": "garden-buddy", "grok": {"id": "b-garden-0003", "name": "Garden Buddy"},
                    "hermes": {"profile": "garden"}}])
    prev = svc.preview([RECIPE], {RECIPE: "garden"})
    assert prev["skipped"][0]["reason"] == "profile_already_paired" and prev["skipped"][0]["pair_id"] == "garden-buddy"
    with pytest.raises(EngineError):
        svc.create([RECIPE], {RECIPE: "garden"})


def test_botport_default_profile_backing_a_demo_twin_is_refused(svc, monkeypatch):
    calls = []

    class Fake:
        def create(self, gid, profile, dry_run):
            calls.append((gid, profile, dry_run))
            return {"pairs": [{"pair_id": "garden-buddy", "grok": {"id": gid, "name": "Garden Buddy"},
                               "hermes": {"profile": profile or "default"}, "items": []}], "skipped": []}

    svc.demo_action("start", "default")  # on purpose: this sample twin backs "default"
    monkeypatch.setattr(svc, "botport", lambda: Fake())
    prev = svc.preview(["b-garden-0003"])
    # a name that is taken is fixed by itself (never two bots in one profile, and no error to decode)
    assert prev["skipped"] == [] and prev["pairs"][0]["hermes"]["profile"] == "default-2"
    assert calls == [("b-garden-0003", None, True), ("b-garden-0003", "default-2", True)]
    calls.clear()
    # a name the owner typed that is taken is still refused, as before
    prev = svc.preview(["b-garden-0003"], {"b-garden-0003": "default"})
    assert prev["pairs"] == [] and prev["skipped"][0]["reason"] == "profile_already_paired" and calls == []
    prev = svc.preview(["b-garden-0003"], {"b-garden-0003": "garden"})
    assert prev["pairs"][0]["hermes"]["profile"] == "garden" and calls == [("b-garden-0003", "garden", True)]


def test_create_uses_the_slug(svc):
    svc.demo_action("start")
    made = svc.create([RECIPE])
    assert [p["hermes"]["profile"] for p in made["pairs"]] == ["grok-bot-twin-sample-2"]
    assert svc.profiles_in_use()["grok-bot-twin-sample-2"] == "recipe-planner"
    # one bot, one twin: a second twin of the same bot is refused, through the service and so through REST
    prev = svc.preview([RECIPE])
    assert prev["pairs"] == [] and prev["skipped"] == [
        {"grok_id": RECIPE, "reason": "already_twinned", "pair_id": "recipe-planner", "name": "Recipe Planner"}]
    with pytest.raises(EngineError) as exc:
        svc.create([RECIPE])
    assert exc.value.code == "already_twinned" and svc.pair_ids().count("recipe-planner") == 1


# --------------------------------------------------------------------------------------------------
def test_cli_add_with_a_new_profile(svc):
    run(["demo"])
    rc, out, err = run(["add", "--bot", "Recipe Planner", "--hermes-profile", "meal-plans", "--yes"])
    assert rc == 0, err
    # the same three plain lines the page shows (built once, in the service), then the twin exists
    assert "Recipe Planner" in out and "Copies now: name and description." in out
    assert "2 skills: optional, set up later." in out and "recipe-planner" in svc.pair_ids()
    assert svc.profiles_in_use()["meal-plans"] == "recipe-planner"


def test_cli_add_default_profile_and_refusal(svc):
    run(["demo"])
    taken = svc.demo._world()["profile"]  # the name the sample twin already uses
    rc, out, err = run(["add", "--bot", "Recipe Planner", "--hermes-profile", taken, "--yes"])
    # a name the owner typed that is taken is refused: nothing is created, and the way out is named
    assert rc == 1 and "recipe-planner" not in svc.pair_ids() and "--hermes-profile" in out + err
    rc, out, _ = run(["add", "--bot", "Recipe Planner", "--yes"])
    assert rc == 0 and "recipe-planner" in svc.pair_ids()
    assert svc.profiles_in_use()["grok-bot-twin-sample-2"] == "recipe-planner"  # a free name, by itself


def test_cli_flag_is_not_hermes_profile_flag():
    # hermes pre-parses --profile/-p anywhere on the command line; the twin commands must not use them
    parser = cli.build_parser()
    a = parser.parse_args(["add", "--bot", "X", "--hermes-profile", "x-y"])
    assert a.profile == "x-y"
    with pytest.raises(SystemExit):
        with redirect_stderr(io.StringIO()):
            parser.parse_args(["add", "--bot", "X", "--profile", "x-y"])
    assert parser.parse_args(["demo", "--hermes-profile", "p1"]).profile == "p1"


# --------------------------------------------------------------------------------------------------
def test_restore_reports_the_wait_for_approval(svc):
    svc.demo_action("start")
    svc.demo_action("conflict")
    svc.sync()
    rp = svc.resolve("acme-support", "skill:greet-visitors", "grok")["restore_point"]
    res = svc.restore("acme-support", rp)
    assert res["waiting_for_ok"] == ["skill:greet-visitors"]
    again = svc.restore("acme-support", rp)
    assert "waiting_for_ok" not in again


def test_cli_restore_line(svc):
    run(["demo"])
    run(["demo", "conflict"])
    run(["sync"])
    rp = svc.resolve("acme-support", "skill:greet-visitors", "grok")["restore_point"]
    rc, out, _ = run(["restore", rp])
    # the consequence is said: the put-back version now differs from Grok, with the command that sends it
    assert rc == 0 and "greet-visitors now differs from Grok until you send it" in out
    assert 'hermes twin approve "skill:greet-visitors"' in out and "waits for your OK" not in out


# --------------------------------------------------------------------------------------------------
def test_grok_list_times_relative_to_demo_start(svc, home):
    from hermes_plugins.grok_bot_twin.twin.model import ago, now_dt, parse_iso
    svc.demo_action("start")
    bots = {b["name"]: b for b in svc.grok_list()["sample_bots"]}
    for name in ("Acme Support", "Recipe Planner"):
        at = parse_iso(bots[name]["updated_at"])
        age = (now_dt() - at).total_seconds()
        assert 0 < age < 6 * 3600 and not ago(bots[name]["updated_at"]).endswith("days ago")
    assert ago(bots["Acme Support"]["updated_at"]) == "12 min ago"
    # stored fixture dates stay in the year 2000
    assert svc.demo._grok(RECIPE)["updated_at"].startswith("2000-")
    # a real (simulated) edit after the start shows its own time
    svc.demo_action("grok-edit")
    acme = next(b for b in svc.grok_list()["sample_bots"] if b["name"] == "Acme Support")
    assert (now_dt() - parse_iso(acme["updated_at"])).total_seconds() < 60
