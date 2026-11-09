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
    """A botport on PATH that is not trusted yet: skipped while the demo runs."""

    def grok_list(self, globs):
        raise EngineError("untrusted_binary", "botport has not been checked and trusted yet")


# --------------------------------------------------------------------------------------------------
def test_demo_bots_mean_app_found_with_botport_on_path(svc, monkeypatch):
    svc.demo_action("start")
    monkeypatch.setattr(svc, "botport", lambda: _Untrusted())
    res = svc.grok_list()
    assert res["app_found"] is True and {b["name"] for b in res["bots"]} == {"Acme Helper", "Recipe Planner"}


def test_no_demo_no_bots_app_not_found(svc, monkeypatch):
    class Empty:
        def grok_list(self, globs):
            return {"app_found": False, "bots": []}

    monkeypatch.setattr(svc, "botport", lambda: Empty())
    assert svc.grok_list() == {"app_found": False, "bots": []}


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


def test_preview_defaults_to_a_slug_never_a_used_profile(svc):
    svc.demo_action("start")  # Acme Helper's demo twin backs this profile ("default")
    prev = svc.preview([RECIPE])
    assert prev["pairs"][0]["hermes"]["profile"] == "recipe-planner"
    assert "default" in prev["profiles_in_use"] and prev["skipped"] == []


def test_profile_backing_a_twin_is_refused(svc):
    svc.demo_action("start")
    used = svc.demo._world()["profile"]
    prev = svc.preview([RECIPE], {RECIPE: used})
    assert prev["pairs"] == [] and prev["skipped"] == [
        {"grok_id": RECIPE, "reason": "profile_already_paired", "pair_id": "acme-helper"}]
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

    svc.demo_action("start")  # the demo twin backs "default"
    monkeypatch.setattr(svc, "botport", lambda: Fake())
    prev = svc.preview(["b-garden-0003"])
    assert prev["pairs"] == [] and prev["skipped"][0]["reason"] == "profile_already_paired"
    assert calls == [("b-garden-0003", None, True)]  # one dry run; the Grok roster is not read again
    calls.clear()
    prev = svc.preview(["b-garden-0003"], {"b-garden-0003": "garden"})
    assert prev["pairs"][0]["hermes"]["profile"] == "garden" and calls == [("b-garden-0003", "garden", True)]


def test_create_uses_the_slug(svc):
    svc.demo_action("start")
    made = svc.create([RECIPE])
    assert [p["hermes"]["profile"] for p in made["pairs"]] == ["recipe-planner"]
    assert svc.profiles_in_use()["recipe-planner"] == "recipe-planner"


# --------------------------------------------------------------------------------------------------
def test_cli_add_with_a_new_profile(svc):
    run(["demo"])
    rc, out, err = run(["add", "--bot", "Recipe Planner", "--hermes-profile", "meal-plans", "--yes"])
    assert rc == 0, err
    assert "Recipe Planner → profile meal-plans" in out and "recipe-planner" in svc.pair_ids()
    assert svc.profiles_in_use()["meal-plans"] == "recipe-planner"


def test_cli_add_default_profile_and_refusal(svc):
    run(["demo"])
    rc, out, err = run(["add", "--bot", "Recipe Planner", "--hermes-profile", "default", "--yes"])
    assert rc == 1 and "that Hermes profile already has a twin" in out and "--hermes-profile NAME" in err
    rc, out, _ = run(["add", "--bot", "Recipe Planner", "--yes"])
    assert rc == 0 and "profile recipe-planner" in out


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
    rp = svc.resolve("acme-helper", "skill:greet-visitors", "grok")["restore_point"]
    res = svc.restore("acme-helper", rp)
    assert res["waiting_for_ok"] == ["skill:greet-visitors"]
    again = svc.restore("acme-helper", rp)
    assert "waiting_for_ok" not in again


def test_cli_restore_line(svc):
    run(["demo"])
    run(["demo", "conflict"])
    run(["sync"])
    rp = svc.resolve("acme-helper", "skill:greet-visitors", "grok")["restore_point"]
    rc, out, _ = run(["restore", rp])
    assert rc == 0 and "now waits for your OK before going to Grok" in out


# --------------------------------------------------------------------------------------------------
def test_grok_list_times_relative_to_demo_start(svc, home):
    from hermes_plugins.grok_bot_twin.twin.model import ago, now_dt, parse_iso
    svc.demo_action("start")
    bots = {b["name"]: b for b in svc.grok_list()["bots"]}
    for name in ("Acme Helper", "Recipe Planner"):
        at = parse_iso(bots[name]["updated_at"])
        age = (now_dt() - at).total_seconds()
        assert 0 < age < 6 * 3600 and not ago(bots[name]["updated_at"]).endswith("days ago")
    assert ago(bots["Acme Helper"]["updated_at"]) == "12 min ago"
    # stored fixture dates stay in the year 2000
    assert svc.demo._grok(RECIPE)["updated_at"].startswith("2000-")
    # a real (simulated) edit after the start shows its own time
    svc.demo_action("grok-edit")
    acme = next(b for b in svc.grok_list()["bots"] if b["name"] == "Acme Helper")
    assert (now_dt() - parse_iso(acme["updated_at"])).total_seconds() < 60
