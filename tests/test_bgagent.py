"""The follower's background agent (twin/bgagent.py): one systemd user unit on Linux, one launchd agent on macOS.

Most tests drive it with a recording stand-in for systemctl / launchctl, on any computer. The two at the end
talk to the real systemd / launchd of the computer that runs them and are skipped unless
GROK_TWIN_LIVE_AGENT=1 is set (they add one clearly named file to the user's own unit folder and delete it again;
the command they start is a stand-in that only sleeps).
"""
from __future__ import annotations

import argparse
import os
import plistlib
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from twin_testlib import home, plugin, svc  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import bgagent, cli


class Tool:
    """Stands in for systemctl / launchctl: records every call, answers from a table of (words -> (rc, text))."""

    def __init__(self, answers=None):
        self.calls: list[list[str]] = []
        self.answers = dict(answers or {})

    def __call__(self, argv):
        self.calls.append(list(argv))
        for words, answer in self.answers.items():
            if all(w in argv for w in words.split()):
                return answer
        return 0, ""

    def verbs(self):
        return [" ".join(a for a in c[1:] if not a.startswith(("/", "gui/", "user/")) and a != "--user")
                for c in self.calls]


def fake_hermes(tmp_path: Path, body: str = "exit 0") -> str:
    exe = tmp_path / "bin" / "hermes"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    exe.chmod(0o755)
    return str(exe)


@pytest.fixture
def linux(monkeypatch, tmp_path, home):  # noqa: F811
    monkeypatch.setattr(bgagent, "_platform", lambda: "linux")
    monkeypatch.setattr(bgagent, "_systemctl", lambda: "/usr/bin/systemctl")
    monkeypatch.setattr(bgagent, "_linger", lambda: False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    return home


@pytest.fixture
def mac(monkeypatch, tmp_path, home):  # noqa: F811
    monkeypatch.setattr(bgagent, "_platform", lambda: "darwin")
    monkeypatch.setattr(bgagent, "_alive", lambda pid: False)  # the stand-in's process id is nobody's
    monkeypatch.setattr(bgagent, "START_WAIT_SECONDS", 0.0)  # the stand-in never starts anything
    return home


_REAL_ALIVE = bgagent._alive


def test_alive_tells_a_running_process_from_one_that_ended():
    assert _REAL_ALIVE(os.getpid()) is True
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    assert _REAL_ALIVE(done.pid) is False


# ---- names ------------------------------------------------------------------------------------------------------
def test_standard_hermes_folder_gets_the_plain_name_and_any_other_folder_its_own(tmp_path):
    std = Path(os.path.expanduser("~")) / ".hermes"
    assert bgagent.suffix(std) == ""
    assert bgagent.unit_name(std) == "grok-bot-twin-follower.service"
    assert bgagent.launchd_label(std) == "com.grok-bot-twin.follower"
    a = tmp_path / "Twin Lab" / "hermes-home"
    b = tmp_path / "twin-lab2" / "hermes-home"
    sa, sb = bgagent.suffix(a), bgagent.suffix(b)
    assert sa.startswith("twin-lab-") and len(sa) == len("twin-lab-") + 6 and sa != sb
    assert bgagent.suffix(a) == sa  # stable
    assert bgagent.unit_name(a) == f"grok-bot-twin-follower-{sa}.service"
    assert bgagent.launchd_label(a) == f"com.grok-bot-twin.follower.{sa}"
    # only plain characters ever reach a unit name or a launchd label
    odd = tmp_path / "we$ird %n\"ame;" / "h"
    assert all(c.isalnum() or c in "-." for c in bgagent.unit_name(odd))


def test_unit_folder_follows_xdg_config_home_only_when_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert bgagent.systemd_user_dir() == tmp_path / "cfg" / "systemd" / "user"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/place")  # the XDG rules say: ignore
    assert bgagent.systemd_user_dir() == Path(os.path.expanduser("~")) / ".config" / "systemd" / "user"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert bgagent.systemd_user_dir() == Path(os.path.expanduser("~")) / ".config" / "systemd" / "user"


# ---- the two files ----------------------------------------------------------------------------------------------
def test_systemd_unit_runs_the_follow_loop_with_its_own_hermes_home(linux, tmp_path):
    p = bgagent.plan(hermes="/opt/hermes/bin/hermes")
    text = bgagent.render_systemd(p)
    assert 'ExecStart="/opt/hermes/bin/hermes" "twin" "fleet" "run"' in text
    assert f'Environment="HERMES_HOME={linux}"' in text
    assert "Restart=on-failure" in text and "RestartSec=30" in text
    assert "RestartPreventExitStatus=2 126 127 203" in text  # plugin or Hermes gone: no restart loop
    assert "WantedBy=default.target" in text
    assert "User=" not in text and "sudo" not in text  # a user unit: never root
    assert p.path == tmp_path / "xdg-config" / "systemd" / "user" / p.label
    assert p.label.endswith(".service") and p.log is None


def test_systemd_unit_makes_special_characters_in_paths_literal(linux, monkeypatch, tmp_path):
    odd = tmp_path / 'my "odd" 100% $HOME\\dir' / "hermes home"
    p = bgagent.plan(root=odd, hermes=str(tmp_path / "pro grams" / "hermes"))
    text = bgagent.render_systemd(p)
    exec_line = next(ln for ln in text.splitlines() if ln.startswith("ExecStart="))
    env_line = next(ln for ln in text.splitlines() if ln.startswith("Environment=\"HERMES_HOME"))
    assert f'"{tmp_path}/pro grams/hermes" "twin"' in exec_line
    assert '\\"odd\\"' in env_line and "100%%" in env_line and "$$HOME" in env_line and "\\\\dir" in env_line
    for bad in ("line\nbreak", "tab\there", "nul\x00"):
        with pytest.raises(bgagent.AgentError):
            bgagent.render_systemd(bgagent.plan(root=tmp_path / bad / "h", hermes="/usr/bin/hermes"))
    with pytest.raises(bgagent.AgentError):
        bgagent.render_systemd(bgagent.plan(hermes="hermes"))  # never a bare name looked up at start time


def test_launchd_agent_is_a_valid_plist_with_the_follow_loop(mac):
    p = bgagent.plan(hermes="/Users/example/.local/bin/hermes")
    body = plistlib.loads(bgagent.render_launchd(p))
    assert body["Label"] == p.label and p.label.startswith("com.grok-bot-twin.follower")
    # the guard is fixed text; the path and the words are separate arguments, never pasted into shell text
    assert body["ProgramArguments"] == ["/bin/sh", "-c", bgagent.LAUNCHD_GUARD,
                                        "/Users/example/.local/bin/hermes", "twin", "fleet", "run"]
    assert "/Users/example" not in bgagent.LAUNCHD_GUARD and "$0" in bgagent.LAUNCHD_GUARD
    assert body["EnvironmentVariables"]["HERMES_HOME"] == str(mac)
    assert body["RunAtLoad"] is True and body["KeepAlive"] == {"SuccessfulExit": False}
    assert body["ProcessType"] == "Background" and body["ThrottleInterval"] == 30
    assert body["StandardOutPath"] == str(mac / "plugin-data" / "grok-bot-twin" / "follower-agent.log")
    assert "UserName" not in body
    assert p.path == Path(os.path.expanduser("~")) / "Library" / "LaunchAgents" / f"{p.label}.plist"


# ---- Linux: install, status, remove ------------------------------------------------------------------------------
def test_linux_install_writes_one_file_enables_and_starts_then_remove_deletes_it(linux, tmp_path):
    exe = fake_hermes(tmp_path)
    tool = Tool({"show": (0, "ActiveState=active\nSubState=running\nMainPID=4242\n")})
    res = bgagent.install(run=tool, hermes=exe)
    unit = Path(res["file"])
    assert unit.is_file() and stat.S_IMODE(unit.stat().st_mode) == 0o644
    assert unit.parent == tmp_path / "xdg-config" / "systemd" / "user"
    assert sorted(p.name for p in unit.parent.iterdir()) == [unit.name]  # one file, no leftovers
    assert res["state"] == "on" and res["running"] and res["installed"] and res["kind"] == "systemd"
    assert "while you are logged in" in res["message"] and "background off" in res["undo"]
    assert all(c[0] == "/usr/bin/systemctl" and c[1] == "--user" for c in tool.calls)
    verbs = tool.verbs()
    assert verbs.index("daemon-reload") < verbs.index(f"enable {unit.name}") < verbs.index(f"restart {unit.name}")
    # again: same file, restarted, still one file
    bgagent.install(run=tool, hermes=exe)
    assert sorted(p.name for p in unit.parent.iterdir()) == [unit.name]

    tool2 = Tool()
    gone = bgagent.remove(run=tool2)
    assert gone["removed"] is True and not unit.exists() and gone["file"] == str(unit)
    assert tool2.verbs()[0] == f"disable --now {unit.name}" and "daemon-reload" in tool2.verbs()
    again = bgagent.remove(run=Tool())
    assert again["removed"] is False and "no background helper" in again["message"]
    st = bgagent.status(run=Tool())
    assert st["state"] == "off" and not st["installed"] and "only while Hermes is open" in st["message"]


def test_linux_status_tells_stopped_from_running_and_says_when_it_survives_logout(linux, tmp_path, monkeypatch):
    exe = fake_hermes(tmp_path)
    bgagent.install(run=Tool({"show": (0, "ActiveState=active\n")}), hermes=exe)
    st = bgagent.status(run=Tool({"show": (0, "ActiveState=failed\nSubState=failed\n")}))
    assert st["state"] == "stopped" and st["installed"] and not st["running"]
    st = bgagent.status(run=Tool({"show": (0, "ActiveState=activating\nSubState=auto-restart\n")}))
    assert st["state"] == "stopped"
    monkeypatch.setattr(bgagent, "_linger", lambda: True)
    st = bgagent.status(run=Tool({"show": (0, "ActiveState=active\n")}))
    assert st["state"] == "on" and st["after_logout"] and "logged in" not in st["message"]


def test_linux_install_that_cannot_start_leaves_nothing_behind(linux, tmp_path):
    exe = fake_hermes(tmp_path)
    tool = Tool({"enable": (1, "Failed to enable unit: Access denied\n")})
    with pytest.raises(bgagent.AgentError) as err:
        bgagent.install(run=tool, hermes=exe)
    assert err.value.code == "start_failed" and "Access denied" in err.value.message
    assert not list((tmp_path / "xdg-config" / "systemd" / "user").iterdir())
    # no user service manager at all (a container, a remote shell without a session)
    tool = Tool({"daemon-reload": (1, "Failed to connect to user scope bus via local transport: No medium found\n")})
    with pytest.raises(bgagent.AgentError) as err:
        bgagent.install(run=tool, hermes=exe)
    assert err.value.code == "no_user_services" and "while Hermes is open" in err.value.message
    assert not list((tmp_path / "xdg-config" / "systemd" / "user").iterdir())


def test_linux_without_systemd_is_not_offered_and_says_so(linux, monkeypatch):
    monkeypatch.setattr(bgagent, "_systemctl", lambda: None)
    st = bgagent.status(run=Tool())
    assert st["state"] == "unsupported" and not st["supported"] and "no systemd user services" in st["message"]
    with pytest.raises(bgagent.AgentError) as err:
        bgagent.install(run=Tool(), hermes="/usr/bin/hermes")
    assert err.value.code == "unsupported"
    assert bgagent.remove(run=Tool())["removed"] is False  # and removing is still safe


def test_a_link_or_folder_where_the_unit_file_goes_is_refused_or_left_alone(linux, tmp_path):
    exe = fake_hermes(tmp_path)
    p = bgagent.plan(hermes=exe)
    p.path.parent.mkdir(parents=True)
    target = tmp_path / "somewhere-else.txt"
    target.write_text("mine", encoding="utf-8")
    p.path.symlink_to(target)
    with pytest.raises(bgagent.AgentError) as err:
        bgagent.install(run=Tool(), hermes=exe)
    assert err.value.code == "unsafe_file" and target.read_text(encoding="utf-8") == "mine"
    assert bgagent.remove(run=Tool())["removed"] is True  # the link itself goes, never what it points at
    assert target.read_text(encoding="utf-8") == "mine" and not os.path.lexists(p.path)
    p.path.mkdir()
    assert bgagent.remove(run=Tool())["removed"] is False and p.path.is_dir()


def test_tools_run_with_a_small_environment_and_a_runtime_dir(linux, monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_TOKEN_FOR_TEST", "x")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    env = bgagent._tool_env()
    assert "SECRET_TOKEN_FOR_TEST" not in env and env["XDG_RUNTIME_DIR"] == "/run/user/4242"
    assert env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    assert bgagent._tool_env()["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"  # remote shells often lack it


# ---- macOS: install, status, remove ------------------------------------------------------------------------------
PRINT_RUNNING = "com.x = {\n\tstate = running\n\tpid = 777\n}\n"


def test_mac_install_loads_the_agent_and_remove_unloads_and_deletes(mac, tmp_path):
    exe = fake_hermes(tmp_path)
    uid = os.getuid()
    state = {"loaded": False}

    def tool(argv):
        tool.calls.append(list(argv))
        verb = argv[1]
        if verb == "print":
            return (0, PRINT_RUNNING) if state["loaded"] and argv[2].startswith(f"gui/{uid}/") else (113, "not found")
        if verb == "bootstrap":
            state["loaded"] = True
            return 0, ""
        if verb == "bootout":
            state["loaded"] = False
        return 0, ""
    tool.calls = []

    res = bgagent.install(run=tool, hermes=exe)
    plist = Path(res["file"])
    assert plist.is_file() and stat.S_IMODE(plist.stat().st_mode) == 0o644
    assert plist.parent == Path(os.path.expanduser("~")) / "Library" / "LaunchAgents"
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][3] == exe
    assert res["state"] == "on" and res["kind"] == "launchd" and "logged in" in res["message"]
    assert all(c[0] == "/bin/launchctl" for c in tool.calls)
    boot = next(c for c in tool.calls if c[1] == "bootstrap")
    assert boot[2] == f"gui/{uid}" and boot[3] == str(plist)
    log = mac / "plugin-data" / "grok-bot-twin" / "follower-agent.log"
    log.write_text("one line\n", encoding="utf-8")

    # install again while loaded: unloaded first, so launchd reads the new file
    tool.calls.clear()
    bgagent.install(run=tool, hermes=exe)
    verbs = [c[1] for c in tool.calls]
    assert verbs.index("bootout") < verbs.index("bootstrap")

    tool.calls.clear()
    gone = bgagent.remove(run=tool)
    assert gone["removed"] is True and not plist.exists() and not log.exists()
    assert [c[2] for c in tool.calls if c[1] == "bootout"] == [f"gui/{uid}/{res['label']}", f"user/{uid}/{res['label']}"]
    assert bgagent.status(run=tool)["state"] == "off"
    assert bgagent.remove(run=tool)["removed"] is False


def test_mac_remove_returns_only_when_the_helper_process_has_ended(mac, tmp_path, monkeypatch):
    """launchd can answer "booted out" while the helper is still closing its connection (a
    process can still be there right after unlink). remove() waits for that process, for a bounded time."""
    exe = fake_hermes(tmp_path)
    bgagent.install(run=Tool({"print": (0, PRINT_RUNNING)}), hermes=exe)
    looks = []

    def alive(pid):
        looks.append(pid)
        return len(looks) < 3  # still there twice, then gone

    monkeypatch.setattr(bgagent, "_alive", alive)
    monkeypatch.setattr(bgagent.time, "sleep", lambda s: None)
    gone = bgagent.remove(run=Tool({"print": (0, PRINT_RUNNING)}))
    assert gone["removed"] is True and looks == [777, 777, 777]
    # a helper that will not end does not hold the removal for ever: the file still goes
    bgagent.install(run=Tool({"print": (0, PRINT_RUNNING)}), hermes=exe)
    monkeypatch.setattr(bgagent, "_alive", lambda pid: True)
    monkeypatch.setattr(bgagent, "STOP_WAIT_SECONDS", 0.0)
    clock = iter(range(1000))
    monkeypatch.setattr(bgagent.time, "monotonic", lambda: float(next(clock)))
    assert bgagent._wait_gone(777, 3.0) is False
    assert bgagent.remove(run=Tool({"print": (0, PRINT_RUNNING)}))["removed"] is True


def test_mac_on_answers_with_the_real_state_once_launchd_has_started_the_agent(mac, tmp_path, monkeypatch):
    """launchd says "bootstrapped" before the agent runs. install() waits (briefly) until it does, so the answer
    to "on" is "on", not "set up but not running"."""
    exe = fake_hermes(tmp_path)
    monkeypatch.setattr(bgagent, "START_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(bgagent.time, "sleep", lambda s: None)
    asked = {"n": 0, "loaded": False}

    def tool(argv):
        if argv[1] == "bootstrap":
            asked["loaded"] = True
            return 0, ""
        if argv[1] == "print":
            if not asked["loaded"] or not argv[2].startswith("gui/"):
                return 113, "not found"
            asked["n"] += 1
            return 0, ("x = {\n\tstate = spawn scheduled\n}\n" if asked["n"] < 4 else PRINT_RUNNING)
        return 0, ""

    res = bgagent.install(run=tool, hermes=exe)
    assert res["state"] == "on" and res["running"] and asked["n"] >= 4
    bgagent.remove(run=Tool())


def test_mac_without_a_desktop_session_falls_back_to_the_user_domain(mac, tmp_path):
    exe = fake_hermes(tmp_path)
    uid = os.getuid()
    tool = Tool({f"bootstrap gui/{uid}": (125, "Bootstrap failed: 125: Domain does not support specified action"),
                 "print": (113, "Could not find service")})
    res = bgagent.install(run=tool, hermes=exe)
    assert [c[2] for c in tool.calls if c[1] == "bootstrap"] == [f"gui/{uid}", f"user/{uid}"]
    assert Path(res["file"]).is_file() and res["state"] == "stopped"  # loaded, this stand-in never reports it running
    bgagent.remove(run=Tool())
    assert not Path(res["file"]).exists()


def test_mac_install_that_cannot_load_leaves_nothing_behind(mac, tmp_path):
    exe = fake_hermes(tmp_path)
    tool = Tool({"bootstrap": (5, "Bootstrap failed: 5: Input/output error"), "print": (113, "")})
    with pytest.raises(bgagent.AgentError) as err:
        bgagent.install(run=tool, hermes=exe)
    assert err.value.code == "start_failed" and "Input/output error" in err.value.message
    agents = Path(os.path.expanduser("~")) / "Library" / "LaunchAgents"
    assert not list(agents.iterdir())


# ---- other computers, and finding hermes --------------------------------------------------------------------------
def test_other_systems_are_told_plainly_and_nothing_is_run(home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bgagent, "_platform", lambda: "freebsd14")  # Windows has its own helper now
    tool = Tool()
    st = bgagent.status(run=tool)
    assert st["state"] == "unsupported" and "while Hermes is open" in st["message"] and st["file"] == ""
    with pytest.raises(bgagent.AgentError):
        bgagent.install(run=tool)
    assert bgagent.remove(run=tool) == {"removed": False, "file": "", "message": bgagent.MSG_NOT_THERE}
    assert tool.calls == []


def test_hermes_command_prefers_the_one_next_to_this_python_and_refuses_one_others_can_rewrite(
        home, monkeypatch, tmp_path):  # noqa: F811
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    exe = venv / "hermes"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(venv / "python"))
    monkeypatch.setattr(sys, "argv", ["-c"])
    monkeypatch.setenv("PATH", "/nonexistent")
    assert bgagent.hermes_command() == str(exe)
    exe.chmod(0o775)  # group-writable: someone else could swap what runs in the background
    assert bgagent.hermes_command() is None
    exe.chmod(0o644)  # not executable
    assert bgagent.hermes_command() is None
    # the per-user place a Desktop app's short PATH misses
    local = Path(os.path.expanduser("~")) / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
    (local / "hermes").chmod(0o755)
    assert bgagent.hermes_command() == str(local / "hermes")
    # and the command this process was started as wins
    started = tmp_path / "started" / "hermes"
    started.parent.mkdir()
    started.write_text("#!/bin/sh\n", encoding="utf-8")
    started.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(started), "twin", "background", "on"])
    assert bgagent.hermes_command() == str(started)


def test_install_without_any_hermes_command_says_so(linux, monkeypatch):
    monkeypatch.setattr(bgagent, "hermes_command", lambda: None)
    with pytest.raises(bgagent.AgentError) as err:
        bgagent.install(run=Tool())
    assert err.value.code == "no_hermes" and "hermes command was not found" in err.value.message


# ---- hermes twin background --------------------------------------------------------------------------------------
def test_cli_words(linux, monkeypatch, tmp_path):
    exe = fake_hermes(tmp_path)
    monkeypatch.setattr(bgagent, "hermes_command", lambda: exe)
    monkeypatch.setattr(bgagent, "_run", Tool({"show": (0, "ActiveState=active\n")}))
    parser = argparse.ArgumentParser()
    bgagent.add_cli(parser.add_subparsers(dest="twin_command"))
    said: list[str] = []

    def run(*words):
        said.clear()
        return bgagent.handle_cli(parser.parse_args(["background", *words]), out=said.append)

    assert run() == 0 and "only while Hermes is open" in said[0] and "background on" in said[1]
    assert run("on") == 0 and "also while Hermes is closed" in said[0] and "one file" in said[1] and "background off" in said[2]
    assert run("status") == 0 and "also while Hermes is closed" in said[0] and "background off" in said[2]
    assert run("status", "--json") == 0 and '"state": "on"' in said[0]
    assert run("off") == 0 and "removed" in said[0]
    monkeypatch.setattr(bgagent, "hermes_command", lambda: None)
    assert run("on") == 1 and said[0].startswith("✗ ")


def test_hermes_twin_background_is_a_real_command_and_in_the_help(linux, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bgagent, "hermes_command", lambda: fake_hermes(tmp_path))
    monkeypatch.setattr(bgagent, "_run", Tool({"show": (0, "ActiveState=active\n")}))
    assert cli.main(["background"]) == 0
    assert "only while Hermes is open" in capsys.readouterr().out
    assert cli.main(["background", "on"]) == 0
    out = capsys.readouterr().out
    assert "stay up to date with your other computers" in out and "hermes twin background off" in out
    assert cli.main(["background", "off"]) == 0
    assert "Nothing of it is left" in capsys.readouterr().out
    assert "background on|off" in cli.HELP_TEXT


def test_remove_grok_bot_twin_names_the_background_helper_and_takes_it_away(linux, monkeypatch, tmp_path, svc):  # noqa: F811
    monkeypatch.setattr(bgagent, "hermes_command", lambda: fake_hermes(tmp_path))
    tool = Tool({"show": (0, "ActiveState=active\n")})
    monkeypatch.setattr(bgagent, "_run", tool)
    assert not any("while Hermes is closed" in g for g in svc.uninstall(True)["goes"])
    unit = Path(bgagent.install()["file"])
    plan = svc.uninstall(True)
    assert any("while Hermes is closed" in g for g in plan["goes"]) and unit.is_file()  # a dry run changes nothing
    tool.calls.clear()
    done = svc.uninstall(False)
    assert any("while Hermes is closed" in g for g in done["goes"])
    assert not unit.exists() and any("disable" in c for c in tool.calls)
    assert not any("while Hermes is closed" in g for g in svc.uninstall(True)["goes"])


# ---- the page's switch, unlinking, the hint after linking ----------------------------------------------------------
def test_page_routes_show_and_switch_the_helper(linux, monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    import importlib.util
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from twin_testlib import ROOT, timer
    spec = importlib.util.spec_from_file_location("gbt_plugin_api_bg_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(timer, "ensure_started", lambda *a, **k: False)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/grok-bot-twin")
    client = TestClient(app)
    url = "/api/plugins/grok-bot-twin/fleet/background"
    monkeypatch.setattr(bgagent, "hermes_command", lambda: fake_hermes(tmp_path))
    monkeypatch.setattr(bgagent, "_run", Tool({"show": (0, "ActiveState=active\n")}))

    st = client.get(url).json()
    assert st["state"] == "off" and st["supported"] is True and st["kind"] == "systemd" and st["installed"] is False
    on = client.post(url, json={"on": True})
    assert on.status_code == 200 and on.json()["state"] == "on" and on.json()["changed"] is True
    assert Path(on.json()["file"]).is_file() and "background off" in on.json()["undo"]
    assert client.get(url).json()["state"] == "on"
    off = client.post(url, json={"on": False}).json()
    assert off["state"] == "off" and off["changed"] is True and not Path(on.json()["file"]).exists()
    assert client.post(url, json={"on": False}).json()["changed"] is False  # nothing there: nothing changed
    assert client.post(url, json={}).status_code == 400
    assert client.post(url, json={"on": "yes"}).status_code == 400
    # a problem is the usual answer in plain words, HTTP 200
    monkeypatch.setattr(bgagent, "hermes_command", lambda: None)
    bad = client.post(url, json={"on": True})
    assert bad.status_code == 200 and bad.json()["error"]["code"] == "no_hermes"
    assert "hermes command was not found" in bad.json()["error"]["message"]


def test_unlinking_names_the_helper_and_takes_it_away(linux, monkeypatch, tmp_path):
    monkeypatch.setattr(bgagent, "hermes_command", lambda: fake_hermes(tmp_path))
    monkeypatch.setattr(bgagent, "_run", Tool({"show": (0, "ActiveState=active\n")}))
    plain = {"dry_run": True, "unlinked": False, "goes": ["The 2 twins on this computer (kept for 30 days, chats included)."]}
    assert bgagent.after_unlink(dict(plain), True) == plain  # no helper: the answer is untouched
    unit = Path(bgagent.install()["file"])
    asked = bgagent.after_unlink(dict(plain), True)
    assert asked["goes"][-1] == bgagent.GOES_ON_UNLINK and len(asked["goes"]) == 2 and unit.is_file()
    done = bgagent.after_unlink(dict(plain, dry_run=False, unlinked=True), False)
    assert done["goes"][-1] == bgagent.GOES_ON_UNLINK and not unit.exists()
    assert bgagent.after_unlink(dict(plain), True) == plain
    # whatever goes wrong with the helper, the unlink answer comes back
    monkeypatch.setattr(bgagent, "status", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    assert bgagent.after_unlink(dict(plain), False) == plain


def test_an_unlink_that_is_refused_leaves_the_helper_alone(linux, monkeypatch, tmp_path, svc):  # noqa: F811
    from hermes_plugins.grok_bot_twin.twin.engine import EngineError
    monkeypatch.setattr(bgagent, "hermes_command", lambda: fake_hermes(tmp_path))
    monkeypatch.setattr(bgagent, "_run", Tool({"show": (0, "ActiveState=active\n")}))
    unit = Path(bgagent.install()["file"])
    with pytest.raises(EngineError) as err:  # this computer is not linked
        svc.fleet_unlink(False)
    assert err.value.code == "not_linked" and unit.is_file()


def test_the_hint_after_linking_appears_only_when_it_helps(linux, monkeypatch, tmp_path):
    monkeypatch.setattr(bgagent, "hermes_command", lambda: fake_hermes(tmp_path))
    monkeypatch.setattr(bgagent, "_run", Tool({"show": (0, "ActiveState=active\n")}))
    assert bgagent.link_hint() == bgagent.LINK_HINT and "hermes twin background on" in bgagent.LINK_HINT
    bgagent.install()
    assert bgagent.link_hint() == ""
    bgagent.remove()
    monkeypatch.setattr(bgagent, "_platform", lambda: "freebsd14")  # a system with no helper (Windows has one now)
    assert bgagent.link_hint() == ""


def test_everything_the_helper_runs_and_writes_is_disclosed():
    import yaml
    from twin_testlib import ROOT
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))["description"]
    disclosure = (ROOT / "docs" / "DISCLOSURE.md").read_text(encoding="utf-8")
    for words in ("systemctl --user", "launchctl", "hermes twin background on", "hermes twin fleet run", "never root"):
        assert words in manifest, words
    for words in (f"{bgagent.UNIT_BASE}.service", f"{bgagent.LAUNCHD_BASE}.plist", bgagent.LOG_NAME,
                  "daemon-reload", "`enable`", "`restart`", "`disable --now`", "`reset-failed`", "`show`",
                  "launchctl bootstrap", "`bootout`", "`print`", "/bin/launchctl", "XDG_CONFIG_HOME",
                  " ".join(("hermes",) + bgagent.FOLLOW_ARGS), "never as root", "/bin/sh -c", "hermes twin --help",
                  "exit status 2, 126, 127 or 203"):
        assert words in disclosure, words
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for words in ("hermes twin background on", "hermes twin background off", "systemd user unit", "launchd agent",
                  "never as root", "95 MB"):
        assert words in readme, words


def test_the_launchd_guard_ends_quietly_when_the_command_is_gone_and_otherwise_becomes_it(tmp_path):
    """The guard run by the real /bin/sh: with a hermes that no longer knows `twin` (plugin removed: exit 2) or
    that is missing, it ends with status 0 (launchd then leaves it alone); else it becomes the real command, with
    the words passed through untouched and the command's own exit status."""
    log = tmp_path / "calls.txt"
    good = fake_hermes(tmp_path / "good dir", f'echo "$*" >> "{log}"\n[ "$1 $2" = "twin --help" ] && exit 0\nexit 7')
    gone = fake_hermes(tmp_path / "gone", f'echo "$*" >> "{log}"\nexit 2')

    def run(exe, *words):
        return subprocess.run(["/bin/sh", "-c", bgagent.LAUNCHD_GUARD, exe, *words], capture_output=True).returncode

    assert run(good, "twin", "fleet", "run; echo pwned > x") == 7  # the real command's own status
    assert log.read_text().splitlines() == ["twin --help", "twin fleet run; echo pwned > x"]
    assert not (tmp_path / "x").exists() and not Path("x").exists()
    log.write_text("")
    assert run(gone, "twin", "fleet", "run") == 0
    assert log.read_text().splitlines() == ["twin --help"]  # asked once, never started
    assert run(str(tmp_path / "no-such-hermes"), "twin", "fleet", "run") == 0


# ---- the real thing (opt-in) --------------------------------------------------------------------------------------
LIVE = os.environ.get("GROK_TWIN_LIVE_AGENT") == "1"


def _wait(check, seconds=20.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.5)
    return False


@pytest.mark.skipif(not (LIVE and sys.platform.startswith("linux")), reason="needs GROK_TWIN_LIVE_AGENT=1 on Linux")
def test_live_systemd_user_unit_starts_restarts_and_is_gone_after_remove(tmp_path, monkeypatch):
    """Real systemd: the unit starts the stand-in command with HERMES_HOME set, a crash is restarted, and after
    remove() the unit is unknown to systemd and its file is gone."""
    root = Path(os.environ["GROK_TWIN_LIVE_ROOT"]) / "live-agent-test" / "hermes-home"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", os.environ["GROK_TWIN_LIVE_HOME"])  # the real home: systemd reads the real folder
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    mark = root / "started.txt"
    mark.unlink(missing_ok=True)  # an earlier run's lines
    exe = fake_hermes(root.parent, f'echo "$HERMES_HOME $*" >> "{mark}"\nexec sleep 300')
    monkeypatch.setattr(bgagent, "RESTART_SECONDS", 1)
    try:
        res = bgagent.install(root=root, hermes=exe)
        assert res["state"] == "on" and Path(res["file"]).is_file()
        assert "live-agent-test" in res["label"]
        assert _wait(mark.exists) and mark.read_text().splitlines()[0] == f"{root} twin fleet run"
        ctl = bgagent._systemctl()
        pid = subprocess.run([ctl, "--user", "show", res["label"], "--property=MainPID", "--value"],
                             capture_output=True, text=True, env=bgagent._tool_env()).stdout.strip()
        os.kill(int(pid), 9)  # a crash
        assert _wait(lambda: len(mark.read_text().splitlines()) >= 2), "not restarted after a crash"
        assert _wait(lambda: bgagent.status(root=root)["state"] == "on")
    finally:
        gone = bgagent.remove(root=root)
    assert gone["removed"] is True and not Path(gone["file"]).exists()
    assert bgagent.status(root=root)["state"] == "off"
    out = subprocess.run([bgagent._systemctl(), "--user", "list-unit-files", res["label"]],
                         capture_output=True, text=True, env=bgagent._tool_env()).stdout
    assert res["label"] not in out


@pytest.mark.skipif(not (LIVE and sys.platform == "darwin"), reason="needs GROK_TWIN_LIVE_AGENT=1 on macOS")
def test_live_launchd_agent_starts_restarts_and_is_gone_after_remove(tmp_path, monkeypatch):
    """Real launchd: the agent starts the stand-in command with HERMES_HOME set, a crash is restarted, and after
    remove() launchd no longer knows the label and the plist is gone."""
    root = Path(os.environ["GROK_TWIN_LIVE_ROOT"]) / "live-agent-test" / "hermes-home"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", os.environ["GROK_TWIN_LIVE_HOME"])  # the real home: launchd reads the real folder
    mark = root / "started.txt"
    mark.unlink(missing_ok=True)  # an earlier run's lines
    exe = fake_hermes(root.parent, f'[ "$1 $2" = "twin --help" ] && exit 0\necho "$HERMES_HOME $*" >> "{mark}"\nexec sleep 300')
    monkeypatch.setattr(bgagent, "RESTART_SECONDS", 1)
    try:
        res = bgagent.install(root=root, hermes=exe)
        assert Path(res["file"]).is_file() and "live-agent-test" in res["label"]
        assert _wait(mark.exists) and mark.read_text().splitlines()[0] == f"{root} twin fleet run"
        assert _wait(lambda: bgagent.status(root=root)["state"] == "on")
        dom, running, pid = bgagent._launchd_loaded(bgagent.plan(root=root, hermes=exe), bgagent._run)
        assert dom and running and pid
        os.kill(pid, 9)  # a crash
        assert _wait(lambda: len(mark.read_text().splitlines()) >= 2, 30), "not restarted after a crash"
        last_pid = bgagent._launchd_loaded(bgagent.plan(root=root, hermes=exe), bgagent._run)[2]
    finally:
        gone = bgagent.remove(root=root)
    assert gone["removed"] is True and not Path(gone["file"]).exists()
    assert bgagent.status(root=root)["state"] == "off"
    assert bgagent._launchd_loaded(bgagent.plan(root=root, hermes=exe), bgagent._run)[0] == ""
    assert last_pid and not bgagent._alive(last_pid)  # the process itself has ended when remove() returns


@pytest.mark.skipif(not (LIVE and sys.platform.startswith("linux")), reason="needs GROK_TWIN_LIVE_AGENT=1 on Linux")
def test_live_systemd_does_not_loop_when_the_plugin_was_removed_under_the_helper(monkeypatch):
    """Real systemd: a hermes that answers `twin ...` with status 2 (what Hermes does once the plugin is gone) is
    started once and never again."""
    root = Path(os.environ["GROK_TWIN_LIVE_ROOT"]) / "live-agent-gone" / "hermes-home"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", os.environ["GROK_TWIN_LIVE_HOME"])
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    mark = root / "started.txt"
    mark.unlink(missing_ok=True)
    exe = fake_hermes(root.parent, f'echo "$*" >> "{mark}"\nexit 2')
    monkeypatch.setattr(bgagent, "RESTART_SECONDS", 1)
    try:
        res = bgagent.install(root=root, hermes=exe)
        assert _wait(mark.exists)
        time.sleep(5)  # five restart intervals
        assert mark.read_text().splitlines() == ["twin fleet run"], "restarted although the command is gone"
        assert bgagent.status(root=root)["state"] == "stopped"
        ctl = bgagent._systemctl()
        n = subprocess.run([ctl, "--user", "show", res["label"], "--property=NRestarts", "--value"],
                           capture_output=True, text=True, env=bgagent._tool_env()).stdout.strip()
        assert n == "0"
    finally:
        gone = bgagent.remove(root=root)
    assert gone["removed"] is True and not Path(gone["file"]).exists()


@pytest.mark.skipif(not (LIVE and sys.platform == "darwin"), reason="needs GROK_TWIN_LIVE_AGENT=1 on macOS")
def test_live_launchd_does_not_loop_when_the_plugin_was_removed_under_the_helper(monkeypatch):
    """Real launchd: with a hermes that answers `twin --help` with status 2 the agent asks once, ends cleanly and
    is not started again."""
    root = Path(os.environ["GROK_TWIN_LIVE_ROOT"]) / "live-agent-gone" / "hermes-home"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", os.environ["GROK_TWIN_LIVE_HOME"])
    mark = root / "started.txt"
    mark.unlink(missing_ok=True)
    exe = fake_hermes(root.parent, f'echo "$*" >> "{mark}"\nexit 2')
    monkeypatch.setattr(bgagent, "RESTART_SECONDS", 1)
    try:
        bgagent.install(root=root, hermes=exe)
        assert _wait(mark.exists)
        time.sleep(6)
        assert mark.read_text().splitlines() == ["twin --help"], "asked again, or started, although the command is gone"
        st = bgagent.status(root=root)
        assert st["installed"] and not st["running"] and st["state"] == "stopped"
    finally:
        gone = bgagent.remove(root=root)
    assert gone["removed"] is True and not Path(gone["file"]).exists()
