"""The follower's background helper on Windows (twin/bgagent_windows.py): one scheduled task of the user's own.

Most tests drive it with a recording stand-in for schtasks / taskkill / whoami and a stand-in for "is that
process running", so they run on any computer (the helper's files are plain files in the test's Hermes folder).
The test at the end adds a real scheduled task on a real Windows machine and takes it away again; it is skipped
unless GROK_TWIN_LIVE_AGENT=1 is set (the same switch as the systemd / launchd tests). What the task starts there
is a stand-in that only waits."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from xml.etree import ElementTree

import pytest

from twin_testlib import ROOT, central, home, plugin, svc  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import bgagent, bgagent_windows as bw, cli, store

SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
HERMES = r"C:\Users\example user\AppData\Local\hermes\bin\hermes.exe"
ALIVE: dict = {}  # process id -> start time of the processes that "run" in a test


class Tool:
    """Stands in for schtasks / taskkill / whoami: records every call; the task "exists" once created."""

    def __init__(self, create=(0, "SUCCESS"), delete=(0, "SUCCESS")):
        self.calls: list[list[str]] = []
        self.exists = False
        self.create, self.delete = create, delete
        self.creates = 0
        self.xml: list[str] = []

    def __call__(self, argv):
        self.calls.append([str(a) for a in argv])
        tool = os.path.basename(str(argv[0]).replace("\\", "/")).lower()
        if tool == "whoami.exe":
            return 0, f'"test-pc\\test user","{SID}"\r\n'
        if tool == "taskkill.exe":
            return 0, "SUCCESS"
        if "/Query" in argv:
            return (0, "Ready") if self.exists else (1, "ERROR: The system cannot find the file specified.")
        if "/Create" in argv:
            self.creates += 1
            self.xml.append(Path(argv[argv.index("/XML") + 1]).read_bytes().decode("utf-16"))
            answer = self.create[min(self.creates, len(self.create)) - 1] if isinstance(self.create, list) else self.create
            self.exists = self.exists or answer[0] == 0
            return answer
        if "/Delete" in argv:
            if self.delete[0] == 0:
                self.exists = False
            return self.delete
        return 0, ""

    def verbs(self):
        out = []
        for c in self.calls:
            tool = os.path.basename(c[0].replace("\\", "/")).lower().replace(".exe", "")
            out.append(tool + " " + " ".join(a for a in c[1:] if a.startswith("/") and len(a) < 9))
        return out


@pytest.fixture
def win(monkeypatch, home):  # noqa: F811
    """A Windows machine as far as the helper's code can tell; no process is "running" unless a test says so."""
    monkeypatch.setattr(bgagent, "_platform", lambda: "win32")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(home.parent / "AppData" / "Local"))
    ALIVE.clear()
    monkeypatch.setattr(bw, "_proc_started", lambda pid: ALIVE.get(int(pid)))
    monkeypatch.setattr(bw, "START_WAIT_S", 0.0)
    monkeypatch.setattr(bw, "STOP_WAIT_S", 0.6)
    return home


def _task(xml: str) -> ElementTree.Element:
    return ElementTree.fromstring(xml.split("?>", 1)[1])


# ---- the launcher ------------------------------------------------------------------------------------------------
def test_the_launcher_starts_hermes_without_a_window_and_only_when_no_helper_runs():
    vbs = bw.render_vbs([HERMES, "twin", "background", "run"], r"C:\Users\example user\AppData\Local\hermes",
                        r"C:\Users\example user\AppData\Local\hermes\plugin-data\grok-bot-twin\follower-agent.pid")
    assert vbs.endswith("\r\n") and "\n" not in vbs.replace("\r\n", "")  # Windows line ends throughout
    # the path has a space: one quoted program, then its words; quotes doubled inside the VBScript string
    assert 'sh.Run """C:\\Users\\example user\\AppData\\Local\\hermes\\bin\\hermes.exe"" twin background run", 0, False' in vbs
    assert 'env.Item("HERMES_HOME") = "C:\\Users\\example user\\AppData\\Local\\hermes"' in vbs
    assert vbs.index("If running Then WScript.Quit 0") < vbs.index("sh.Run ")
    assert "cmd.exe" not in vbs and "powershell" not in vbs.lower()


@pytest.mark.parametrize("bad", ['C:\\a"b\\hermes.exe', "C:\\a%PATH%\\hermes.exe", "C:\\a\nb\\hermes.exe", ""])
def test_a_path_that_could_change_the_launcher_is_refused(bad):
    with pytest.raises(bgagent.AgentError):
        bw.render_vbs([bad, "twin", "background", "run"], r"C:\h", r"C:\h\x.pid")
    with pytest.raises(bgagent.AgentError):
        bw.render_vbs([HERMES], bad or "\x01", r"C:\h\x.pid")


def test_a_command_that_is_not_a_full_path_is_refused():
    with pytest.raises(bgagent.AgentError) as exc:
        bw.render_vbs(["hermes.exe", "twin"], r"C:\h", r"C:\h\x.pid")
    assert exc.value.code == "no_hermes"


# ---- the task ----------------------------------------------------------------------------------------------------
def test_the_task_is_the_users_own_with_least_privilege():
    xml = bw.render_task_xml(r"C:\Users\example\A&B\hermes\plugin-data\grok-bot-twin\follower-agent.vbs", SID)
    task = _task(xml)
    principal = task.find(f"{NS}Principals/{NS}Principal")
    assert principal.findtext(f"{NS}UserId") == SID
    assert principal.findtext(f"{NS}LogonType") == "InteractiveToken"   # the user's own session, no stored password
    assert principal.findtext(f"{NS}RunLevel") == "LeastPrivilege"      # never elevated
    assert "HighestAvailable" not in xml and "SYSTEM" not in xml and "Password" not in xml
    logon = task.find(f"{NS}Triggers/{NS}LogonTrigger")
    assert logon.findtext(f"{NS}UserId") == SID                          # this user's sign-in only
    again = task.find(f"{NS}Triggers/{NS}TimeTrigger/{NS}Repetition")
    assert again.findtext(f"{NS}Interval") == "PT15M"
    settings = task.find(f"{NS}Settings")
    assert settings.findtext(f"{NS}MultipleInstancesPolicy") == "IgnoreNew"
    assert settings.findtext(f"{NS}DisallowStartIfOnBatteries") == "false"
    assert settings.findtext(f"{NS}WakeToRun") == "false"
    action = task.find(f"{NS}Actions/{NS}Exec")
    assert action.findtext(f"{NS}Command").replace("/", "\\").lower().endswith("\\system32\\wscript.exe")
    assert action.findtext(f"{NS}Arguments") == \
        '//B //Nologo "C:\\Users\\example\\A&B\\hermes\\plugin-data\\grok-bot-twin\\follower-agent.vbs"'


def test_the_account_is_named_by_its_sid_never_by_a_domain_name(win):
    """Under a remote shell Windows reports a made-up domain for the account; a task for that name is refused."""
    assert bw.user_sid(Tool()) == SID
    assert bw.user_sid(lambda argv: (1, "")) is None
    assert "<UserId>" not in bw.render_task_xml(r"C:\h\x.vbs", None)


def test_a_second_hermes_folder_gets_its_own_task_name(win, tmp_path):
    std = Path(os.environ["LOCALAPPDATA"]) / "hermes"
    assert bw.task_name(std) == "grok-bot-twin-follower"
    other = tmp_path / "twin-lab" / "hermes-home"
    name = bw.task_name(other)
    assert name.startswith("grok-bot-twin-follower-twin-lab-") and len(name) == len("grok-bot-twin-follower-twin-lab-") + 6
    assert bw.task_name(other) == name and bw.task_name(tmp_path / "second" / "hermes-home") != name


# ---- on / off / status -------------------------------------------------------------------------------------------
def test_on_adds_one_task_and_one_launcher_and_says_how_to_undo(win):
    tool = Tool()
    res = bgagent.install(run=tool, hermes=HERMES)
    name = bw.task_name(win)
    assert tool.verbs() == ["whoami /user /fo /nh", "schtasks /Create /F /TN /XML", "schtasks /Run /TN",
                            "schtasks /Query /TN"]
    assert tool.calls[1][tool.calls[1].index("/TN") + 1] == name
    c = central(win)
    raw = (c / "follower-agent.vbs").read_bytes()
    assert raw.startswith(b"\xff\xfe") and "twin background run" in raw.decode("utf-16")
    assert not (c / "follower-agent.task.xml").exists()          # the task's description is not left lying around
    assert sorted(p.name for p in c.iterdir() if not p.name.startswith(".")) == ["follower-agent.vbs"]
    assert (res["kind"], res["installed"], res["task"], res["changed"]) == ("schtasks", True, name, True)
    assert res["state"] == "stopped" and "next time you sign in" in res["message"]   # nothing started it here
    assert "background off" in res["undo"]


def test_status_follows_the_helpers_process(win):
    tool = Tool()
    assert bgagent.status(run=tool)["state"] == "off"
    bgagent.install(run=tool, hermes=HERMES)
    assert bgagent.status(run=tool)["state"] == "stopped"
    (central(win) / "follower-agent.pid").write_bytes(b"4242\n777\n")
    ALIVE[4242] = 777
    st = bgagent.status(run=tool)
    assert (st["state"], st["running"], st["after_logout"]) == ("on", True, False)
    assert "while you are logged in" in st["message"]
    ALIVE[4242] = 999  # another program got that process id later: not our helper
    assert bgagent.status(run=tool)["state"] == "stopped"


def test_an_account_that_may_not_add_a_sign_in_trigger_still_gets_the_timed_look(win):
    tool = Tool(create=[(1, "ERROR: Access is denied."), (0, "SUCCESS")])
    res = bgagent.install(run=tool, hermes=HERMES)
    assert res["installed"] is True and tool.creates == 2
    assert "<LogonTrigger>" in tool.xml[0] and "<LogonTrigger>" not in tool.xml[1] and "<TimeTrigger>" in tool.xml[1]


def test_a_refused_task_leaves_nothing_behind_and_says_so(win):
    tool = Tool(create=(1, "ERROR: Access is denied."))
    with pytest.raises(bgagent.AgentError) as exc:
        bgagent.install(run=tool, hermes=HERMES)
    assert exc.value.code == "task_refused" and "Access is denied" in exc.value.message
    assert not central(win).exists() or [p.name for p in central(win).iterdir() if not p.name.startswith(".")] == []


def test_off_takes_the_task_away_first_then_asks_the_helper_to_stop(win):
    tool = Tool()
    bgagent.install(run=tool, hermes=HERMES)
    c = central(win)
    (c / "follower-agent.pid").write_bytes(b"4242\n777\n")
    (c / "follower-agent.log").write_bytes(b"line\n")
    ALIVE[4242] = 777

    def helper():  # a helper that sees the stop file and ends cleanly
        while not (c / "follower-agent.stop").exists():
            time.sleep(0.02)
        ALIVE.pop(4242)

    threading.Thread(target=helper, daemon=True).start()
    tool.calls.clear()
    res = bgagent.remove(run=tool)
    assert tool.verbs() == ["schtasks /Query /TN", "schtasks /End /TN", "schtasks /Delete /F /TN"]  # no taskkill
    assert res["removed"] is True and "Nothing of it is left" in res["message"]
    assert [p.name for p in c.iterdir() if not p.name.startswith(".")] == []
    assert bgagent.remove(run=tool)["removed"] is False  # safe to repeat


def test_off_ends_a_helper_that_does_not_stop_by_itself(win):
    tool = Tool()
    bgagent.install(run=tool, hermes=HERMES)
    (central(win) / "follower-agent.pid").write_bytes(b"4242\n777\n")
    ALIVE[4242] = 777
    tool.calls.clear()
    bgagent.remove(run=tool)
    kill = tool.calls[-1]
    assert kill[0].lower().replace("/", "\\").endswith("\\system32\\taskkill.exe")
    assert kill[1:] == ["/PID", "4242", "/T", "/F"]  # that one process and what it started; never by name


def test_off_never_ends_a_process_it_cannot_prove_is_the_helper(win):
    tool = Tool()
    bgagent.install(run=tool, hermes=HERMES)
    (central(win) / "follower-agent.pid").write_bytes(b"4242\n777\n")
    ALIVE[4242] = 123456  # some other program has that id now
    tool.calls.clear()
    bgagent.remove(run=tool)
    assert not any("taskkill" in c[0].lower() for c in tool.calls)


def test_remove_grok_bot_twin_takes_the_helper_away_too(win, svc, monkeypatch):  # noqa: F811
    tool = Tool()
    bgagent.install(run=tool, hermes=HERMES)
    monkeypatch.setattr(bw, "_run", tool)
    svc.uninstall(dry_run=False)
    assert tool.exists is False
    assert sorted(p.name for p in central(win).iterdir()) == []


def test_the_command_line_says_task_not_file(win, monkeypatch, capsys):
    tool = Tool()
    monkeypatch.setattr(bw, "_run", tool)
    monkeypatch.setattr(bw, "hermes_command", lambda root=None: HERMES)
    assert cli.main(["background", "on"]) == 0
    out = capsys.readouterr().out
    assert "one scheduled task of your account: grok-bot-twin-follower" in out and "background off" in out
    assert cli.main(["background", "status", "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert (st["ok"], st["kind"], st["installed"]) == (True, "schtasks", True)
    assert cli.main(["background", "off"]) == 0
    assert "Nothing of it is left" in capsys.readouterr().out


# ---- the helper itself -------------------------------------------------------------------------------------------
def test_the_helper_logs_keeps_its_process_id_and_stops_when_asked(win):
    c = store.ensure_dir(central(win))
    ALIVE[os.getpid()] = 555
    stop = threading.Event()
    seen = {}

    def work() -> int:
        print("following mini: in sync, 2 twin(s)")
        seen["pid"] = bw.read_pid(c / "follower-agent.pid")
        seen["running"] = bw.helper_pid(c / "follower-agent.pid")
        (c / "follower-agent.stop").write_bytes(b"stop\n")  # what `off` does
        assert stop.wait(5), "the helper was not asked to stop"
        return 0

    assert bw.run_agent(work, interrupt=stop.set, poll_s=0.05) == 0
    assert seen == {"pid": (os.getpid(), 555), "running": os.getpid()}
    log = (c / "follower-agent.log").read_bytes()
    assert b"background helper started" in log and b"following mini" in log and b"background helper stopped" in log
    assert b"\r" not in log
    assert not (c / "follower-agent.pid").exists() and not (c / "follower-agent.stop").exists()


def test_a_second_helper_leaves_at_once_while_one_runs(win):
    c = store.ensure_dir(central(win))
    (c / "follower-agent.pid").write_bytes(b"4242\n777\n")
    ALIVE[4242] = 777
    assert bw.run_agent(lambda: pytest.fail("a second helper ran")) == 0
    assert (c / "follower-agent.pid").read_bytes() == b"4242\n777\n"


def test_a_stale_process_id_file_does_not_stop_a_new_helper(win):
    c = store.ensure_dir(central(win))
    (c / "follower-agent.pid").write_bytes(b"4242\n777\n")  # that helper died; nothing has the id now
    ran = []
    assert bw.run_agent(lambda: ran.append(1) or 0) == 0
    assert ran == [1]


# ---- a real scheduled task (Windows only, opt-in) ----------------------------------------------------------------
_STAND_IN = """
import sys, time, threading
sys.path.insert(0, {tests!r})
import twin_testlib
from hermes_plugins.grok_bot_twin.twin import bgagent_windows as bw
def work():
    print("stand-in helper running")
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        return 0
sys.exit(bw.run_agent(work))
"""


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt" or os.environ.get("GROK_TWIN_LIVE_AGENT") != "1",
                    reason="adds a real scheduled task: Windows with GROK_TWIN_LIVE_AGENT=1 only")
def test_live_a_real_task_starts_the_helper_and_off_leaves_nothing(home, tmp_path):  # noqa: F811
    from hermes_plugins.grok_bot_twin.twin import osflags
    if osflags._elevated():
        # pytest makes its temp folder owner-only (mode 0700); made by an elevated process its owner is the
        # Administrators group, so the task (the account's ordinary session) cannot read the stand-in there
        pytest.skip("pytest runs elevated here: run this test from an ordinary (non-administrator) prompt")
    script = tmp_path / "stand_in_helper.py"
    script.write_text(_STAND_IN.format(tests=str(ROOT / "tests")), encoding="utf-8", newline="\n")
    name = bw.task_name(home)
    assert name != "grok-bot-twin-follower"  # a test folder never uses the real install's task name
    try:
        res = bw.install(home, command=[sys.executable, str(script)])
        assert res["installed"] is True
        if not res["running"]:
            pytest.skip("no desktop session on this machine right now: the task waits for the next sign-in")
        pid = bw.helper_pid(central(home) / "follower-agent.pid")
        assert pid > 0 and bw.status(home)["state"] == "on"
        query = subprocess.run([bw._tool("schtasks.exe"), "/Query", "/TN", name, "/XML"], capture_output=True)
        xml = query.stdout.decode("utf-16", "replace") if query.stdout[:2] in (b"\xff\xfe", b"\xfe\xff") \
            else query.stdout.decode("mbcs", "replace")
        # least privilege is the default run level, and Windows leaves a default out of what it shows
        assert "HighestAvailable" not in xml and "InteractiveToken" in xml and "S4U" not in xml
        t0 = time.monotonic()
        out = bw.remove(home)
        took = time.monotonic() - t0
        assert out["removed"] is True and took < 15
        assert bw._proc_started(pid) is None                      # the helper is gone
        assert bw._task_exists(name, bw._run) is False            # the task is gone
        assert [p.name for p in central(home).iterdir() if p.name.startswith("follower-agent")] == []
        log_note = f"live: task {name} started pid {pid}; off took {took:.1f} s"
        print(log_note)
    finally:
        bw.remove(home)
