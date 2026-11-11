"""Shared safety net for every test: the plugin never looks in this computer's real program folders.

Outside tests, the helper (botport) and GitHub's command-line tool are also looked for in the usual per-user and
Homebrew folders, because a Desktop app starts with a short PATH. Tests must find only what they put on PATH
themselves, so those extra folders are emptied here. The Grok app's usual folder is pointed at an empty folder
of the test's own (the plugin only looks whether that folder is there), so no test depends on, or looks at, a Grok
app installed on the machine that runs it. A test about a missing Grok app points `grok_app_dir` at a place that
does not exist. The pinned-release table is whatever the test sets; it is read from the file again afterwards.
The user's Downloads folder is looked for under a HOME of the test's own."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from twin_testlib import plugin  # noqa: F401  (loads the plugin package the way Hermes does)
from hermes_plugins.grok_bot_twin.twin import engine, pins, service


# ---- the real Hermes folder is never a test's folder -----------------------------------------------------------------
# No test may read or write the Hermes folder of the person who runs the tests. Three things see to that:
#   1. for the whole run, HERMES_HOME names a throwaway folder of the run (so code that runs outside one test, or
#      after a test's own settings were taken back, still does not fall back to ~/.hermes);
#   2. every test starts with HERMES_HOME set to a folder of its own, whatever the caller had exported;
#   3. every test is checked when it ends: it FAILS if HERMES_HOME is then unset, names the real folder (or the real
#      home), or names a place outside the temporary folders, and if it called monkeypatch.undo() (which drops the
#      test's HERMES_HOME and HOME in one go: use `with monkeypatch.context() as m:` for a setting that must go back).
def _real_home() -> Path:
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)  # not $HOME: a test may have changed that
    except Exception:  # Windows
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


_REAL_HOME = _real_home()


class _Armed:
    on = False


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def unsafe_hermes_home(tmp_roots) -> str:
    """Why HERMES_HOME, as it is set now, is not a safe place for a test; "" when it is safe."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        return "HERMES_HOME is not set, so the plugin would use the real ~/.hermes"
    here = Path(raw).expanduser().resolve()
    real_home = _REAL_HOME.resolve()
    if here == real_home or _inside(here, real_home / ".hermes"):
        return f"HERMES_HOME is the real Hermes folder of this computer ({here})"
    if not any(_inside(here, Path(t).resolve()) for t in tmp_roots):
        return f"HERMES_HOME is outside the test's temporary folders ({here})"
    return ""


@pytest.fixture(scope="session", autouse=True)
def _a_hermes_home_for_the_whole_run(tmp_path_factory):
    before = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(tmp_path_factory.mktemp("hermes-home-of-the-run"))
    yield
    if before is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = before


@pytest.fixture(autouse=True)
def _never_the_real_hermes_home(_a_hermes_home_for_the_whole_run, monkeypatch, tmp_path, tmp_path_factory):
    # the same place an unset HERMES_HOME would mean under the HOME each test gets below; not made here
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-of-the-test" / ".hermes"))
    monkeypatch.setattr(_Armed, "on", True)
    roots = (tmp_path, tmp_path_factory.getbasetemp(), tempfile.gettempdir())
    why = unsafe_hermes_home(roots)
    if why:
        pytest.fail("before the test: " + why, pytrace=False)
    yield
    if not _Armed.on:
        pytest.fail("the test called monkeypatch.undo(), which drops its HERMES_HOME and HOME: use "
                    "`with monkeypatch.context() as m:` instead", pytrace=False)
    why = unsafe_hermes_home(roots)
    if why:
        pytest.fail("after the test: " + why, pytrace=False)


@pytest.fixture(autouse=True)
def _no_extra_program_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "EXTRA_BIN_DIRS", ())
    monkeypatch.setattr(engine, "HELPER_EXTRA_DIRS", ())
    grok = tmp_path / "grok-app-folder"
    grok.mkdir(exist_ok=True)
    monkeypatch.setattr(service, "default_grok_app_dir", lambda: grok)
    fake_home = tmp_path / "home-of-the-test"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # the stand-in botport of these tests is in no table of digests, so every test starts without one; a test
    # about the table sets its own, or reads the one that ships with pins.set_table(None)
    pins.set_table({"releases": {}})
    yield
    pins.set_table(None)


# ---- Windows ------------------------------------------------------------------------------------------------------
# There is no botport for Windows (a Windows machine follows the twins of the computer with the Grok app; it never
# reads Grok itself), and the stand-in tests/fake_botport.py is a script Windows cannot start as a program. A test
# that got as far as starting it is reported as skipped there, with the reason, instead of failed.
def _could_not_start_a_program(exc) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 193:  # "not a valid Win32 application"
            return True
        exc = exc.__cause__ or exc.__context__
    return False


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if os.name == "nt" and rep.failed and call.excinfo is not None and _could_not_start_a_program(call.excinfo.value):
        rep.outcome = "skipped"
        rep.longrepr = (str(item.fspath), item.location[1] or 0,
                        "Skipped: needs botport, which has no Windows build (Windows follows, it never reads Grok)")


# Tests that check things only macOS and Linux have (owner and permission bits, FIFOs), and tests of the side that
# reads Grok (botport, GitHub's tool, the Grok app's folder), which a Windows machine never runs. Listed by name so
# nothing else is skipped by accident; tests/test_windows.py covers what Windows has instead.
_POSIX_ONLY = {
    "test_input_hardening.py::test_read_json_and_jsonl_refuse_symlinks",
    "test_release_pin_folders_ids.py::test_ancestors_above_root_sticky_ok_shared_refused",
    "test_release_pin_folders_ids.py::test_central_dir_created_0700_per_component",
    "test_release_pin_folders_ids.py::test_group_or_world_writable_component_refused",
    "test_release_pin_folders_ids.py::test_jobs_json_fifo_and_oversize_refused",
    "test_release_pin_folders_ids.py::test_ref_is_salted_hmac_and_stable",
    "test_store_timer.py::test_atomic_write_perms",
    "test_store_timer.py::test_plugin_files_are_private",
    "test_watch.py::test_no_content_is_read",
}
_GROK_SIDE = {
    "test_allowlist_attestation_names.py::test_cli_setup_flag",
    "test_allowlist_attestation_names.py::test_tools_resolved_and_checked",
    "test_allowlist_attestation_names.py::test_trust_needs_attestation_or_explicit_opt_out",
    "test_allowlist_attestation_names.py::test_unsafe_gh_is_refused_and_doctor_shows_paths",
    "test_engine_fake.py::test_find_botport_resolves",
    "test_engine_fake.py::test_required_features_check",
    "test_engine_fake.py::test_trust_never_runs_binary_before_attestation",
    "test_engine_fake.py::test_trust_refuses_unsafe",
    "test_engine_fake.py::test_trust_require_attestation",
    "test_release_pin_folders_ids.py::test_cli_setup_tag",
    "test_release_pin_folders_ids.py::test_engine_refuses_linked_xdg_and_bin_folders",
    "test_release_pin_folders_ids.py::test_setup_pins_expected_tag_and_records_it",
    "test_release_pin_folders_ids.py::test_setup_tag_from_config_and_re_attests_on_a_new_tag",
    "test_setup_download_and_twin_facts.py::test_a_pinned_download_connects_without_githubs_tool",
    "test_setup_download_and_twin_facts.py::test_downloads_that_is_a_link_is_not_looked_into",
    "test_setup_download_and_twin_facts.py::test_one_cause_when_twins_cannot_follow_grok",
    "test_setup_download_and_twin_facts.py::test_setup_names_the_download_for_this_computer",
    "test_setup_download_and_twin_facts.py::test_the_bare_program_and_a_repacked_archive_are_judged_by_the_program",
    "test_setup_download_and_twin_facts.py::test_the_download_is_found_in_downloads_by_exact_name",
    "test_setup_download_and_twin_facts.py::test_use_it_verifies_the_found_download",
    "test_setup_download_and_twin_facts.py::test_verify_botport_uses_the_table_first",
    "test_ux_flows.py::test_choose_the_downloaded_program",
    "test_ux_flows.py::test_first_run_no_helper",
    "test_ux_flows.py::test_gh_is_found_in_the_usual_folders_and_a_miss_is_not_remembered",
    "test_ux_flows.py::test_helper_lookup_order",
    "test_watch.py::test_signature_is_names_dates_and_sizes",
}


def pytest_collection_modifyitems(config, items):
    if os.name != "nt":
        return
    for item in items:
        key = f"{item.fspath.basename}::{getattr(item, 'originalname', None) or item.name}"
        if item.fspath.basename == "test_bgagent.py":
            item.add_marker(pytest.mark.skip(reason="the systemd / launchd helper; Windows has test_bgagent_windows.py"))
        elif key in _POSIX_ONLY:
            item.add_marker(pytest.mark.skip(reason="checks owner and permission bits or FIFOs (macOS/Linux only)"))
        elif key in _GROK_SIDE:
            item.add_marker(pytest.mark.skip(reason="the Grok-reading side (botport); Windows only follows"))
