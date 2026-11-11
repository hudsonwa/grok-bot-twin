"""The plugin's files on Windows: opened without following a link, locked between processes, replaced while
another program reads them, written with the same line ends on every system, kept out of names Windows reserves.

Each test in the first part runs twice on macOS/Linux: as it is ("native") and under tests/win_sim.py, which
switches the plugin's Windows code paths on ("simwin"). On a real Windows machine both are the real thing, so
the second run is skipped. Tests marked `windows` need the real system (file-sharing rules, junctions, the
\\r\\n text mode) and are skipped elsewhere."""
from __future__ import annotations

import ast
import errno
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import win_sim
from twin_testlib import ROOT, central, home, svc  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import demo, hermes_side, importer, osflags, store

windows = pytest.mark.windows
only_windows = pytest.mark.skipif(os.name != "nt", reason="needs a real Windows machine")


@pytest.fixture(params=["native", "simwin"])
def fs(request, monkeypatch, home):
    """The Hermes home of the test, with the plugin's file layer as on this system or as on Windows."""
    if request.param == "simwin":
        if win_sim.REAL_WINDOWS:
            pytest.skip("this is a real Windows machine: the native run is the Windows run")
        win_sim.enable(monkeypatch.setattr, lambda obj, name: monkeypatch.delattr(obj, name))
    return home


def cdir(h: Path) -> Path:
    """The plugin's data folder of this Hermes home, made the plugin's own way."""
    return Path(store.ensure_dir(central(h)))


def _symlink(target: Path, link: Path, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("this account may not create symbolic links")


# ---- one set of bytes on every system ---------------------------------------------------------------------------
def test_json_and_text_are_written_with_plain_line_ends(fs):
    c = cdir(fs)
    store.atomic_write_json(c / "a.json", {"name": "Test Scout", "lines": "one\ntwo"})
    store.atomic_write_text(c / "b.txt", "first\nsecond\n")
    store.append_jsonl(c / "log.jsonl", {"n": 1})
    store.append_jsonl(c / "log.jsonl", {"n": 2})
    for name in ("a.json", "b.txt", "log.jsonl"):
        assert b"\r" not in (c / name).read_bytes(), name
    assert (c / "b.txt").read_bytes() == b"first\nsecond\n"
    assert store.read_json(c / "a.json") == {"name": "Test Scout", "lines": "one\ntwo"}
    assert store.read_jsonl(c / "log.jsonl") == [{"n": 1}, {"n": 2}]


def test_a_file_saved_by_a_windows_tool_is_still_read(fs):
    """PowerShell and some editors put a byte-order mark first and end lines with \\r\\n."""
    c = cdir(fs)
    (c / "bom.json").write_bytes(b"\xef\xbb\xbf{\r\n \"twins\": 2\r\n}\r\n")
    assert store.read_json(c / "bom.json") == {"twins": 2}
    (c / "log.jsonl").write_bytes(b"{\"n\": 1}\r\n{\"n\": 2}\r\n")
    assert store.read_jsonl(c / "log.jsonl") == [{"n": 1}, {"n": 2}]


def test_the_salt_is_made_once_and_read_back(fs):
    first = store.ref_salt()
    store._salts.clear()
    assert store.ref_salt() == first and len(first) == 32
    assert b"\r" not in (cdir(fs) / store.SALT_FILE).read_bytes()


# ---- links are never followed -----------------------------------------------------------------------------------
def test_a_linked_file_is_not_read_or_appended_to(fs, tmp_path):
    c = cdir(fs)
    outside = tmp_path / "outside.json"
    outside.write_text('{"private": true}', encoding="utf-8")
    _symlink(outside, c / "link.json")
    assert store.read_json(c / "link.json", "refused") == "refused"
    assert store.read_jsonl(c / "link.json") == []
    with pytest.raises(OSError):
        store.append_jsonl(c / "link.json", {"n": 1})
    assert outside.read_text(encoding="utf-8") == '{"private": true}'


def test_a_linked_folder_on_the_way_is_refused(fs, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _symlink(elsewhere, fs / "profiles", is_dir=True)
    with pytest.raises(store.UnsafePath):
        store.ensure_dir(fs / "profiles" / "test-scout")
    (elsewhere / "test-scout").mkdir()
    (elsewhere / "test-scout" / "SOUL.md").write_text("text", encoding="utf-8")
    assert hermes_side._read_below_root(fs / "profiles" / "test-scout" / "SOUL.md", 1000) is None


def test_open_nofollow_makes_a_missing_file_and_never_opens_a_link(fs, tmp_path):
    c = cdir(fs)
    fd = osflags.open_nofollow(c / "new.txt", os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, b"x\n")
    os.close(fd)
    assert (c / "new.txt").read_bytes() == b"x\n"
    with pytest.raises(FileNotFoundError):
        osflags.open_nofollow(c / "absent.txt", os.O_RDONLY)
    _symlink(c / "new.txt", c / "alias.txt")
    with pytest.raises(OSError):
        osflags.open_nofollow(c / "alias.txt", os.O_RDONLY)


def test_a_file_swapped_between_the_look_and_the_open_is_refused(fs, monkeypatch):
    """The Windows way looks at the name first, then opens: a different file behind the name by then is refused."""
    if not osflags.WINDOWS:
        pytest.skip("macOS and Linux refuse in the open call itself (O_NOFOLLOW)")
    c = cdir(fs)
    (c / "a.txt").write_bytes(b"a")
    (c / "b.txt").write_bytes(b"b")
    real_open = os.open

    def swapping_open(path, flags, mode=0o777, **kw):
        if str(path).endswith("a.txt"):
            return real_open(c / "b.txt", flags, mode, **kw)
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", swapping_open)
    with pytest.raises(OSError):
        osflags.open_nofollow(c / "a.txt", os.O_RDONLY)


def test_two_files_without_a_file_number_are_never_taken_for_the_same(tmp_path):
    """Where the system gives no file number, the look and the open cannot be compared: that is a refusal, not a
    pass."""
    (tmp_path / "a.txt").write_bytes(b"a")
    real = os.stat(tmp_path / "a.txt")
    numberless = os.stat_result((real.st_mode, 0, real.st_dev, 1, 0, 0, 1, 0, 0, 0))
    assert osflags.same_file(real, real)
    assert not osflags.same_file(numberless, real) and not osflags.same_file(real, numberless)
    assert not osflags.same_file(numberless, numberless)


def test_a_stopped_request_is_ended_with_the_systems_own_taskkill_never_one_from_the_path():
    """Requests on Windows: the program that ends a stopped chat turn is named by its full path in the system
    folder, like every other Windows tool the plugin starts."""
    source = (ROOT / "twin" / "tasks_run.py").read_text(encoding="utf-8")
    assert 'os.path.join(_osflags.windows_system_folder(), "taskkill.exe")' in source
    assert '["taskkill"' not in source


def test_the_system_folder_is_asked_from_windows_and_the_variables_are_only_what_is_left(monkeypatch):
    """A changed SystemRoot must not point the plugin's system tools somewhere else: Windows itself is asked
    first. Where it does not answer (this test runs anywhere), the variables are used, then the usual place."""
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    if os.name != "nt":
        assert osflags.windows_system_folder() == os.path.join(r"C:\Windows", "System32")
        monkeypatch.delenv("SystemRoot")
        monkeypatch.delenv("WINDIR", raising=False)
        assert osflags.windows_system_folder() == os.path.join(r"C:\Windows", "System32")
    else:
        monkeypatch.setenv("SystemRoot", r"C:\elsewhere")
        assert "elsewhere" not in osflags.windows_system_folder()
    source = (ROOT / "twin" / "osflags.py").read_text(encoding="utf-8")
    assert "GetSystemDirectoryW" in source


# ---- a twin's own files -----------------------------------------------------------------------------------------
def _soul(fs, data: bytes) -> Path:
    prof = fs / "profiles" / "test-scout"
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "SOUL.md").write_bytes(data)
    return prof


def test_a_prompt_file_with_windows_line_ends_keeps_them(fs):
    """Only the plugin's own block changes: the owner's text keeps its \\r\\n line ends and its byte-order mark."""
    prof = _soul(fs, b"\xef\xbb\xbfOwner's own first line.\r\nSecond line.\r\n")
    hermes_side.write_instructions(prof, "Be brief.\nBe kind.")
    raw = (prof / "SOUL.md").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbfOwner's own first line.\r\nSecond line.\r\n")
    assert b"\n" not in raw.replace(b"\r\n", b"")  # no bare \n anywhere: one style in the whole file
    facts = hermes_side.soul_facts(prof)
    assert facts["own"] == "Be brief.\nBe kind." and facts["has_instructions"] is True
    before = (prof / "SOUL.md").read_bytes()
    hermes_side.write_instructions(prof, "Be brief.\nBe kind.")  # the same text again changes no byte
    assert (prof / "SOUL.md").read_bytes() == before


def test_a_new_prompt_file_gets_plain_line_ends(fs):
    prof = fs / "profiles" / "test-quill"
    prof.mkdir(parents=True)
    hermes_side.write_instructions(prof, "Write clearly.\nNo jargon.")
    raw = (prof / "SOUL.md").read_bytes()
    assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")
    assert hermes_side.soul_facts(prof)["own"] == "Write clearly.\nNo jargon."


def test_names_and_descriptions_are_read_from_a_windows_style_file(fs):
    prof = _soul(fs, b"<!-- botport:begin persona-name -->\r\nTest Scout\r\n<!-- botport:end persona-name -->\r\n"
                     b"<!-- botport:begin persona-description -->\r\nFinds trails.\r\n"
                     b"<!-- botport:end persona-description -->\r\n")
    facts = hermes_side.soul_facts(prof)
    assert (facts["name"], facts["description"], facts["has_instructions"]) == ("Test Scout", "Finds trails.", False)


def test_a_file_below_the_root_is_opened_when_the_root_is_spelled_another_way(fs, tmp_path, monkeypatch):
    """HERMES_HOME may be written through a linked folder above it (or, on Windows, with a short 8.3 name)."""
    alias = tmp_path / "alias-of-tmp"
    _symlink(tmp_path, alias, is_dir=True)
    monkeypatch.setenv("HERMES_HOME", str(alias / fs.name))
    prof = fs / "profiles" / "test-scout"
    prof.mkdir(parents=True)
    (prof / "SOUL.md").write_bytes(b"hello\n")
    assert hermes_side._read_below_root(alias / fs.name / "profiles" / "test-scout" / "SOUL.md", 100) == "hello\n"


# ---- two processes -----------------------------------------------------------------------------------------------
_CHILD = """
import sys
sys.path.insert(0, {tests!r})
import twin_testlib  # loads the plugin package the way Hermes does
from hermes_plugins.grok_bot_twin.twin import store
if {simwin!r}:
    import win_sim
    win_sim.enable_for_process()
from pathlib import Path
counter = Path({counter!r})
for _ in range({rounds}):
    with store.locked():
        n = int(counter.read_text(encoding="utf-8"))
        counter.write_text(str(n + 1), encoding="utf-8")
"""


def test_two_processes_never_write_at_the_same_time(fs, request, tmp_path):
    """Hermes Desktop, the terminal and the background follower can all be running: the file lock keeps a
    read-change-write of one of them whole. Without the lock the count comes out short."""
    assert osflags.can_lock()
    counter = cdir(fs) / "counter.txt"
    counter.write_text("0", encoding="utf-8")
    simwin = "simwin" in request.node.name
    code = _CHILD.format(tests=str(ROOT / "tests"), simwin=simwin, counter=str(counter), rounds=40)
    env = dict(os.environ, HERMES_HOME=str(fs), PYTHONUTF8="1")
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode("utf-8", "replace")[-800:]
    assert counter.read_text(encoding="utf-8") == "160"


def test_the_lock_is_given_back_and_can_be_taken_again(fs):
    for _ in range(3):
        with store.locked():
            with store.locked():  # the same process may take it again inside
                store.atomic_write_json(cdir(fs) / "x.json", {"ok": True})
    assert store.read_json(cdir(fs) / "x.json") == {"ok": True}


def test_a_change_to_the_list_of_computers_never_goes_on_without_its_lock(home, monkeypatch):
    """On Windows the list of linked computers has a lock file of its own. When another process keeps it for
    longer than the wait, the change stops with an error before anything is read or written."""
    if win_sim.REAL_WINDOWS:
        pytest.skip("needs a lock that is never given back, which only the simulated Windows can stage")
    win_sim.enable(monkeypatch.setattr, lambda obj, name: monkeypatch.delattr(obj, name))
    from hermes_plugins.grok_bot_twin.twin import fleet, fleet_io
    path = cdir(home) / "fleet.lock"
    fh = fleet_io.lock_file(path)
    assert fh is not None  # free: had at once
    fleet_io.unlock_file(fh)

    class Busy(win_sim.FakeMsvcrt):
        @staticmethod
        def locking(fd: int, mode: int, nbytes: int) -> None:
            if mode == win_sim.FakeMsvcrt.LK_NBLCK and os.path.samestat(os.fstat(fd), os.stat(path)):
                raise OSError(errno.EACCES, "Permission denied")
            win_sim.FakeMsvcrt.locking(fd, mode, nbytes)

    monkeypatch.setattr(osflags, "_msvcrt", Busy)
    monkeypatch.setattr(fleet_io, "LOCK_WAIT_S", 0.2)
    with pytest.raises(OSError):
        fleet_io.lock_file(path)
    ran = []
    with pytest.raises(OSError):
        with fleet.locked():
            ran.append("inside")
    assert ran == [] and fleet._lock_depth == 0
    monkeypatch.setattr(osflags, "_msvcrt", win_sim.FakeMsvcrt)
    with fleet.locked():  # the other process let go: the next change goes through, and the store's lock was given back
        with fleet.locked():
            ran.append("inside")
    assert ran == ["inside"] and fleet._lock_depth == 0


# ---- replacing a file another program has open ------------------------------------------------------------------
def test_replace_waits_for_a_busy_file(fs, monkeypatch):
    if not osflags.WINDOWS:
        pytest.skip("only Windows refuses to replace an open file")
    calls = []

    def busy_twice(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "The process cannot access the file because it is being used")
        os.replace(src, dst)

    monkeypatch.setattr(osflags, "_os_replace", busy_twice)
    store.atomic_write_json(cdir(fs) / "state.json", {"v": 2})
    assert len(calls) == 3 and store.read_json(cdir(fs) / "state.json") == {"v": 2}


def test_replace_gives_up_after_a_while_and_leaves_no_half_file(fs, monkeypatch):
    if not osflags.WINDOWS:
        pytest.skip("only Windows refuses to replace an open file")
    c = cdir(fs)
    store.atomic_write_json(c / "state.json", {"v": 1})

    def always_busy(src, dst):
        raise PermissionError(13, "in use")

    with monkeypatch.context() as while_busy:  # only these two go back afterwards; the test's Hermes folder stays
        while_busy.setattr(osflags, "_os_replace", always_busy)
        while_busy.setattr(osflags, "REPLACE_WAIT_S", 0.2)
        with pytest.raises(PermissionError):
            store.atomic_write_json(c / "state.json", {"v": 2})
    assert store.read_json(c / "state.json") == {"v": 1}
    assert sorted(p.name for p in c.iterdir() if p.name.endswith(".tmp")) == []


@windows
@only_windows
def test_a_file_held_open_by_a_reader_is_replaced_once_it_is_free(home):
    c = cdir(home)
    store.atomic_write_json(c / "state.json", {"v": 1})
    reader = open(c / "state.json", "rb")  # an ordinary Windows open: no other program may replace it meanwhile
    threading.Timer(0.4, reader.close).start()
    t0 = time.monotonic()
    store.atomic_write_json(c / "state.json", {"v": 2})
    assert time.monotonic() - t0 >= 0.3
    assert store.read_json(c / "state.json") == {"v": 2}


@windows
@only_windows
def test_text_is_not_rewritten_to_windows_line_ends_by_the_system(home):
    """Without newline="\\n" Windows text mode writes \\r\\n for every \\n; the plugin's bytes must not depend on it."""
    c = cdir(home)
    store.atomic_write_text(c / "note.txt", "a\nb\n")
    assert (c / "note.txt").read_bytes() == b"a\nb\n"
    prof = home / "profiles" / "test-scout"
    prof.mkdir(parents=True)
    hermes_side.write_instructions(prof, "one\ntwo")
    assert b"\r" not in (prof / "SOUL.md").read_bytes()


@windows
@only_windows
def test_a_junction_is_refused_like_a_link(home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(home / "profiles"), str(elsewhere)],
                          capture_output=True)
    if done.returncode != 0:
        pytest.skip("could not make a junction")
    assert osflags.is_link(os.lstat(home / "profiles"))
    with pytest.raises(store.UnsafePath):
        store.ensure_dir(home / "profiles" / "test-scout")


@windows
@only_windows
def test_read_only_files_do_not_survive_a_removed_folder(home):
    """A copied plugin folder can hold read-only files (git keeps its pack files that way)."""
    c = cdir(home)
    d = c / "fleet-kept" / "old"
    d.mkdir(parents=True)
    (d / "pack.idx").write_bytes(b"x")
    os.chmod(d / "pack.idx", 0o444)
    store.remove_tree(c, c / "fleet-kept")
    assert not (c / "fleet-kept").exists()


def test_read_only_files_are_removed_with_their_folder(fs):
    c = cdir(fs)
    d = c / "kept" / "old"
    d.mkdir(parents=True)
    (d / "pack.idx").write_bytes(b"x")
    os.chmod(d / "pack.idx", 0o444)
    store.remove_tree(c, c / "kept")
    assert not (c / "kept").exists()


def test_remove_everything_leaves_an_empty_folder(fs, svc):
    """On Windows the lock file cannot be deleted while it is held; it must still be gone afterwards."""
    store.append_jsonl(cdir(fs) / "events.jsonl", {"type": "synced"})
    svc.uninstall(dry_run=False)
    assert sorted(p.name for p in cdir(fs).iterdir()) == []


# ---- names Windows cannot keep ----------------------------------------------------------------------------------
@pytest.mark.parametrize("name,bad", [("con", True), ("CON", True), ("nul", True), ("Aux", True), ("com1", True),
                                      ("LPT9", True), ("con.txt", True), ("test-scout.", True), ("test-scout ", True),
                                      ("", True), ("conman", False), ("com10", False), ("test-scout", False),
                                      ("console", False), ("a.b", False)])
def test_reserved_names(name, bad):
    assert osflags.reserved_name(name) is bad


@pytest.mark.parametrize("bot,profile", [("Con", "con-bot"), ("AUX", "aux-bot"), ("Com1", "com1-bot"),
                                         ("Nul!", "nul-bot"), ("Test Scout", "test-scout"), ("Console", "console")])
def test_a_bot_named_like_a_windows_device_gets_a_usable_profile_name(bot, profile):
    """The same on every system, so a twin's Hermes bot has one name on all of the owner's machines."""
    assert demo.profile_slug(bot) == profile
    assert not osflags.reserved_name(demo.profile_slug(bot))


def test_profile_names_that_differ_only_in_case_do_not_share_a_folder():
    assert demo.unique_profile("Test Scout", {"Test-Scout"}) == "test-scout-2"
    assert demo.unique_profile("Test Scout", {"TEST-SCOUT", "test-scout-2"}) == "test-scout-3"


def test_a_reserved_profile_name_is_never_turned_into_a_path_on_windows(fs):
    if not osflags.WINDOWS:
        assert store.profile_home("aux") == fs / "profiles" / "aux"  # an existing macOS/Linux profile keeps working
        return
    for name in ("con", "NUL", "com1", "test-scout."):
        with pytest.raises(ValueError):
            store.profile_home(name)
    assert store.profile_home("test-scout") == fs / "profiles" / "test-scout"


# ---- nothing platform-bound slips back in -----------------------------------------------------------------------
# tools/pin_botport.py is the maintainer's release-time tool (run on the release machine, never on a user's)
_PLUGIN_FILES = sorted(p for d in ("twin", "dashboard", "tools") for p in (ROOT / d).glob("*.py")
                       if p.name != "pin_botport.py") + [ROOT / "__init__.py"]
_ONLY_IN_OSFLAGS = {"fchmod", "O_NOFOLLOW", "O_DIRECTORY", "killpg", "replace"}
# reached on macOS/Linux only, checked by hand (bgagent.py is the systemd / launchd helper)
# fleet_io.py has its own replace-with-retry and chmods on macOS/Linux only
_GUARDED = {("engine.py", "killpg"), ("importer.py", "O_DIRECTORY"), ("bgagent.py", "fchmod"), ("bgagent.py", "replace"),
            ("fleet_io.py", "fchmod"), ("fleet_io.py", "replace"),
            ("tasks_run.py", "killpg"),  # requests between computers: behind `os.name != "nt"`, as in engine.py
            ("tasks_turn.py", "killpg")}


def _attr_uses(tree: ast.AST) -> list:
    return [(n.attr, n.lineno) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "os"]


def test_calls_windows_lacks_are_used_through_the_platform_layer_only():
    """os.fchmod, os.O_NOFOLLOW, os.O_DIRECTORY and os.killpg do not exist on Windows, and a bare os.replace fails
    there while a reader has the file open: the plugin reaches them through twin/osflags.py only."""
    found = []
    for path in _PLUGIN_FILES:
        if path.name == "osflags.py":
            continue
        for attr, line in _attr_uses(ast.parse(path.read_text(encoding="utf-8"))):
            if attr in _ONLY_IN_OSFLAGS and (path.name, attr) not in _GUARDED:
                found.append(f"{path.name}:{line} os.{attr}")
    assert found == []


def _text_writes_without_newline(tree: ast.AST) -> list:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        kw = {k.arg for k in node.keywords}
        owner = getattr(getattr(func, "value", None), "id", "")
        # Path.write_text only: fleet_io's own write_text (called as fio.write_text or by name) writes bytes
        if (name == "write_text" and isinstance(func, ast.Attribute) and "newline" not in kw
                and owner not in ("fio", "fleet_io")):
            out.append((node.lineno, "write_text"))
        if name in ("open", "fdopen"):
            mode = None
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            for k in node.keywords:
                if k.arg == "mode" and isinstance(k.value, ast.Constant):
                    mode = k.value.value
            if isinstance(mode, str) and any(c in mode for c in "wax+") and "b" not in mode and "newline" not in kw:
                out.append((node.lineno, f"{name}(..., {mode!r})"))
    return out


def test_every_text_file_the_plugin_writes_names_its_line_ends():
    """A text-mode write without newline= gives \\r\\n on Windows and \\n elsewhere: different bytes for the same
    twin on two machines. Every such write says newline="\\n" (or writes bytes)."""
    found = []
    for path in _PLUGIN_FILES:
        for line, what in _text_writes_without_newline(ast.parse(path.read_text(encoding="utf-8"))):
            found.append(f"{path.name}:{line} {what}")
    assert found == []


def test_the_plugin_says_it_runs_on_windows():
    import yaml
    doc = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert doc["platforms"] == ["macos", "linux", "windows"]


# ---- the fleet code's own file helpers (twin/fleet_io.py) -------------------------------------------------------
from hermes_plugins.grok_bot_twin.twin import fleet_io  # noqa: E402


def test_a_twins_folder_that_is_in_use_is_not_half_moved(fs, monkeypatch):
    """Taking a twin away moves its Hermes bot's folder. While a program has a file in it open, Windows refuses
    the move: then nothing may be moved at all, and the next try must find things as they were."""
    src = fs / "profiles" / "test-scout"
    (src / "sessions").mkdir(parents=True)
    (src / "SOUL.md").write_bytes(b"# Test Scout\n")
    (src / "sessions" / "chat.db").write_bytes(b"open in a chat")
    dst = cdir(fs) / "fleet-kept" / "r-1" / "profile"
    calls = []

    def busy(a, b):
        calls.append(1)
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")

    with monkeypatch.context() as while_busy:  # only these two go back afterwards; the test's Hermes folder stays
        while_busy.setattr(fleet_io, "MOVE_TRIES", 3)
        while_busy.setattr(os, "rename", busy)
        with pytest.raises(PermissionError):
            fleet_io.move_dir(src, dst)
    assert len(calls) == 3
    assert sorted(p.name for p in src.rglob("*")) == ["SOUL.md", "chat.db", "sessions"] and not dst.exists()
    fleet_io.move_dir(src, dst)  # the chat is closed: the same call now goes through
    assert not src.exists() and (dst / "sessions" / "chat.db").read_bytes() == b"open in a chat"


@windows
@only_windows
def test_a_folder_with_an_open_file_stays_whole_until_the_file_is_closed(home):
    src = home / "profiles" / "test-scout"
    src.mkdir(parents=True)
    (src / "SOUL.md").write_bytes(b"# Test Scout\n")
    dst = cdir(home) / "fleet-kept" / "r-1" / "profile"
    held = open(src / "SOUL.md", "rb")
    try:
        with pytest.raises(OSError):
            fleet_io.move_dir(src, dst)
        assert (src / "SOUL.md").is_file() and not dst.exists()
    finally:
        held.close()
    fleet_io.move_dir(src, dst)
    assert not src.exists() and (dst / "SOUL.md").read_bytes() == b"# Test Scout\n"


def test_the_fleet_note_goes_into_a_windows_style_prompt_file_without_rewriting_the_rest(fs):
    from hermes_plugins.grok_bot_twin.twin import fleet_profiles
    prof = _soul(fs, b"\xef\xbb\xbfOwner's own line.\r\nAnother.\r\n")
    note = fleet_profiles.fleet_note("Surface", "Test Scout", ["mini"], "mini", False)
    assert fleet_profiles.write_soul(prof, twin=("Test Scout", "Finds trails."), fleet=note) is True
    raw = (prof / "SOUL.md").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbfOwner's own line.\r\nAnother.\r\n")
    assert b"\n" not in raw.replace(b"\r\n", b"")                     # one style in the whole file
    assert b"You are the Surface copy of Test Scout" in raw
    assert fleet_profiles.soul_fleet_note(prof) == note               # read back the same on every system
    assert fleet_profiles.write_soul(prof, twin=("Test Scout", "Finds trails."), fleet=note) is False  # no churn
    assert (prof / "SOUL.md").read_bytes() == raw


def test_a_new_twins_prompt_file_has_plain_line_ends_on_every_system(fs):
    from hermes_plugins.grok_bot_twin.twin import fleet_profiles
    prof = fs / "profiles" / "test-quill"
    prof.mkdir(parents=True)
    note = fleet_profiles.fleet_note("Surface", "Test Quill", [], "mini", False)
    fleet_profiles.write_soul(prof, twin=("Test Quill", "Writes clearly."), fleet=note)
    raw = (prof / "SOUL.md").read_bytes()
    assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")


def test_removing_a_folder_also_removes_read_only_files(fs):
    c = cdir(fs)
    d = c / "fleet-kept" / "r-9"
    (d / "profile").mkdir(parents=True)
    (d / "profile" / "pack.idx").write_bytes(b"x")
    os.chmod(d / "profile" / "pack.idx", 0o444)
    fleet_io.remove_dir(d, c / "fleet-kept")
    assert not d.exists()


@windows
@only_windows
def test_a_junction_is_not_a_real_folder(home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = home / "profiles"
    if subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(elsewhere)], capture_output=True).returncode:
        pytest.skip("could not make a junction")
    assert fleet_io.is_real_dir(elsewhere) is True and fleet_io.is_real_dir(link) is False


# ---- folders made from an elevated process ----------------------------------------------------------------------
def test_an_elevated_process_makes_folders_its_user_can_still_open(fs, monkeypatch):
    """Python 3.13+ on Windows gives mkdir(0700) an owner-only access list; for an elevated process the owner is
    the Administrators group, and the same account's ordinary programs are then locked out of the twin."""
    modes = []
    real_mkdir = os.mkdir

    def recording(path, mode=0o777, **kw):
        modes.append(mode)
        return real_mkdir(path, mode, **kw)

    monkeypatch.setattr(os, "mkdir", recording)
    monkeypatch.setattr(osflags, "_elevated", lambda: True)
    store.ensure_dir(fs / "profiles" / "made-elevated")
    elevated_modes, modes[:] = list(modes), []
    monkeypatch.setattr(osflags, "_elevated", lambda: False)
    store.ensure_dir(fs / "profiles2" / "made-plainly")
    if osflags.WINDOWS:
        assert elevated_modes == [0o777, 0o777]   # the plain way: the access list of the folder it is made in
    else:
        assert elevated_modes == [0o700, 0o700]   # macOS and Linux: always owner-only
    assert modes == [0o700, 0o700]


@windows
@only_windows
def test_a_folder_made_here_can_be_opened_by_the_users_ordinary_programs(home):
    """Real Windows: the user's own name is on the access list of a folder the plugin makes, whether or not this
    test runs elevated (the owner-only list of an elevated process names the Administrators group instead)."""
    d = store.ensure_dir(home / "profiles" / "test-scout")
    who = subprocess.run([os.path.join(os.environ["SystemRoot"], "System32", "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                         capture_output=True).stdout.decode("mbcs", "replace")
    sid = who.strip().split(",")[-1].strip('"')
    acl = subprocess.run([os.path.join(os.environ["SystemRoot"], "System32", "icacls.exe"), str(d), "/save",
                          str(home / "acl.txt")], capture_output=True)
    assert acl.returncode == 0
    text = (home / "acl.txt").read_bytes().decode("utf-16", "replace")
    if osflags._elevated():
        assert "D:P" not in text.replace("D:PAI", "")      # not a closed list of its own: it inherits
    else:
        assert sid in text or "OW" in text                 # the owner is the user, and the owner is named


# ---- the follower's long request can be cut short on every system ------------------------------------------------
def test_a_waiting_request_ends_at_once_when_asked(home, monkeypatch):
    """The follower holds one long request open. A new name for this computer, an unlink or a stop must end it now.
    On Windows a shutdown of the socket from another thread does not wake the waiting thread (the
    request simply runs its full time), so the wait itself looks for the "end it" mark twice a second. Here only that
    mark is set, the socket is left alone: what Windows gives."""
    import socket
    from hermes_plugins.grok_bot_twin.twin import fleet_follow as follow
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    result, holder = {}, []

    def ask():
        t0 = time.monotonic()
        try:
            follow._request("127.0.0.1", port, "/v1/sync", {"proto": 1, "wait_s": 50}, key=b"k" * 32,
                            machine_id="m-000000000000", timeout=65, holder=holder)  # signed, as since wire version 2
            result["raised"] = None
        except follow.Unreachable:
            result["raised"] = "unreachable"
        result["seconds"] = time.monotonic() - t0

    t = threading.Thread(target=ask, daemon=True)
    t.start()
    peer, _ = server.accept()   # a source that holds the request and says nothing
    time.sleep(0.4)
    assert t.is_alive() and holder, "the request should be waiting"
    follow._aborting.set()      # the mark alone: no shutdown, no close
    t.join(5)
    peer.close()
    server.close()
    assert not t.is_alive(), "the waiting request did not end"
    assert result["raised"] == "unreachable" and result["seconds"] < 3.0
    assert holder == []


def test_no_input_is_not_a_person_to_ask(monkeypatch):
    """A program started with no input gets the NUL device on Windows, and Python calls that a terminal."""
    class Nul:
        def isatty(self):
            return True

        def fileno(self):
            return 0

    class NotATty:
        def isatty(self):
            return False

    assert osflags.is_terminal(NotATty()) is False and osflags.is_terminal(None) is False
    if os.name != "nt":
        assert osflags.is_terminal(Nul()) is True   # macOS and Linux: isatty() is the whole answer
        return
    with open(os.devnull) as nul:
        assert nul.isatty() is True and osflags.is_terminal(nul) is False
