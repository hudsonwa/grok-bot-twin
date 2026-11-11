"""The follower's background helper on Windows: one per-user scheduled task (see bgagent.py for Linux and macOS).

What `hermes twin background on` adds, and `off` takes away again in one step:

- one scheduled task of the signed-in user, `grok-bot-twin-follower` (with a suffix for a Hermes folder that is not
  the standard one). No administrator rights, "least privilege" run level, nothing runs as SYSTEM. It starts when
  the user signs in and is looked at again every 15 minutes (a helper that is still running is left alone, one
  that died is started again). This is the only thing outside the Hermes folder.
- inside the plugin's data folder: a small launcher script (`follower-agent.vbs`) the task runs with Windows' own
  `wscript.exe` so that no window ever shows (the way Hermes starts its own gateway on Windows), plus the helper's
  process id file and its log.

The helper is `hermes twin background run`: the sync contract's foreground loop (`hermes twin fleet run`) with a
log file and a way to stop it. Windows has no "please stop" signal for a windowless process, and ending the
scheduled task ends only the launcher, not what it started (checked on Windows 11), so `off` asks the helper to
stop through a stop file, waits a few seconds, and only then ends that one process.

It only makes outgoing connections (to the computer with the Grok app); it never listens, so Windows Firewall
never asks about it.

The plugin runs `schtasks.exe`, `taskkill.exe` and `whoami.exe` from the Windows system folder only (never looked
up on PATH), with an argument list, no shell.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from xml.sax.saxutils import escape

from . import osflags as _osflags
from . import store
from .bgagent import (FOLLOW_ARGS, LOG_NAME, MSG_NO_HERMES, MSG_NOT_THERE, MSG_OFF, MSG_ON_LOGIN_ONLY,
                      MSG_REMOVED, MSG_STOPPED, UNDO_ON, UNIT_BASE, AgentError)

KIND = "schtasks"
VBS_NAME = "follower-agent.vbs"
PID_NAME = "follower-agent.pid"
STOP_NAME = "follower-agent.stop"
RUN_ARGS: tuple[str, ...] = ("twin", "background", "run")  # FOLLOW_ARGS, wrapped (log, process id, stop file)
DESCRIPTION = "Grok Bot Twin: keeps this computer's twins following your Grok Mac. Remove: hermes twin background off"
RECHECK = "PT15M"       # how often the task looks whether the helper still runs
LOGON_DELAY = "PT30S"
TOOL_TIMEOUT = 20
START_WAIT_S = 45.0     # how long `on` waits for the helper's process id file (Hermes takes ~10 s to start)
STOP_WAIT_S = 8.0       # how long `off` waits for a clean stop before ending the process
LOG_CAP = 1024 * 1024
_SLUG = re.compile(r"[^a-z0-9]+")
_BAD = re.compile(r'[\x00-\x1f\x7f"%]')  # never in a path or word that goes into the launcher or the task
MSG_NEXT_SIGN_IN = ("The background helper is set up. It starts the next time you sign in to Windows (it needs "
                    "your own desktop session).")
MSG_DENIED = "Windows did not let Hermes add a scheduled task for your account."
# the plugin's own folder as Hermes made it from an administrator window: see osflags.closed_to_ordinary_programs.
# The sentence carries no folder name (an error's text is cleaned of anything that looks like a path); the line
# that fixes it is printed next to it by whoever shows the error.
MSG_ADMIN_INSTALL = ("Grok Bot Twin was installed from an administrator window, so programs that run as you without "
                     "administrator rights (Hermes Desktop, this background helper) cannot open it. Nothing was "
                     "changed.")
MSG_ADMIN_INSTALL_NOTE = ("Grok Bot Twin was installed from an administrator window, so programs that run as you "
                          "without administrator rights (Hermes Desktop, the background helper) cannot open it.")
ADMIN_INSTALL_FIX = 'In an administrator window run:  icacls "{folder}" /reset /T /C /Q'
# The line is meant to be copied into a shell, so it is only printed for a folder name that means the same thing
# in cmd and in PowerShell inside double quotes: no quote, percent, dollar, backtick or control character.
ADMIN_INSTALL_FIX_PLAIN = ("In an administrator window, put the plugin's folder (grok-bot-twin, in the plugins "
                           "folder of your Hermes folder) back under your Hermes folder's access list: icacls "
                           "with /reset /T /C /Q on that folder.")
_NOT_FOR_A_SHELL = re.compile(r'[\x00-\x1f\x7f"%$`]')


def plugin_folder() -> Path:
    """The installed plugin's own folder (the one that holds plugin.yaml)."""
    return Path(__file__).resolve().parent.parent


def admin_install_problem(note: bool = False) -> Optional[tuple]:
    """(what is wrong, the one line that fixes it) when the installed plugin is closed to the account's ordinary
    programs, else None. Only looks. `note`: the wording for a place that is not about the helper."""
    folder = plugin_folder()
    if not _osflags.closed_to_ordinary_programs(folder):
        return None
    fix = ADMIN_INSTALL_FIX_PLAIN if _NOT_FOR_A_SHELL.search(str(folder)) else ADMIN_INSTALL_FIX.format(folder=folder)
    return (MSG_ADMIN_INSTALL_NOTE if note else MSG_ADMIN_INSTALL), fix


Runner = Callable[[list], tuple]


# ---- where things are ------------------------------------------------------------------------------------------
def _system32() -> str:
    return _osflags.windows_system_folder()


def _tool(name: str) -> str:
    return os.path.join(_system32(), name)


def _norm(path: Any) -> str:
    """One spelling per folder: Windows file names ignore case."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def standard_roots() -> list[str]:
    out = [_norm(Path(os.path.expanduser("~")) / ".hermes")]
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        out.append(_norm(Path(local) / "hermes"))
    return out


def suffix(root: Optional[Path] = None) -> str:
    """'' for the standard Hermes folder (%LOCALAPPDATA%\\hermes); else a short, stable name for this Hermes
    folder: the folder that holds it plus six characters of a hash of its full path, as on the other systems."""
    root_s = _norm(root if root is not None else store.hermes_root())
    if root_s in standard_roots():
        return ""
    parent = _SLUG.sub("-", os.path.basename(os.path.dirname(root_s)).lower()).strip("-")[:24]
    digest = hashlib.sha256(root_s.encode("utf-8", "surrogateescape")).hexdigest()[:6]
    return f"{parent}-{digest}" if parent else digest


def task_name(root: Optional[Path] = None) -> str:
    s = suffix(root)
    return f"{UNIT_BASE}-{s}" if s else UNIT_BASE


def _central(root: Path) -> Path:
    return Path(root) / "plugin-data" / "grok-bot-twin"


def files(root: Optional[Path] = None) -> dict:
    root = Path(root) if root is not None else store.hermes_root()
    c = _central(root)
    return {"vbs": c / VBS_NAME, "pid": c / PID_NAME, "stop": c / STOP_NAME, "log": c / LOG_NAME}


def hermes_command(root: Optional[Path] = None) -> Optional[str]:
    """The hermes.exe a scheduled task can start: the one of this Hermes folder first (<root>\\bin, where the
    installer puts it), then the one next to this Python, then PATH. Absolute and a regular file."""
    root = Path(root) if root is not None else store.hermes_root()
    cands = [str(Path(root) / "bin" / "hermes.exe")]
    if sys.executable and os.path.isabs(sys.executable):
        cands.append(os.path.join(os.path.dirname(sys.executable), "hermes.exe"))
    import shutil
    found = shutil.which("hermes")
    if found:
        cands.append(os.path.abspath(found))
    for cand in cands:
        if os.path.isabs(cand) and os.path.isfile(cand):
            return cand
    return None


# ---- the launcher and the task ---------------------------------------------------------------------------------
def _plain(value: Any, what: str) -> str:
    value = str(value)
    if not value or _BAD.search(value):
        raise AgentError("bad_path", f"The background helper cannot be set up: {what} has an unusual name.")
    return value


def _q(value: str) -> str:
    """A VBScript string literal."""
    return '"' + value.replace('"', '""') + '"'


def render_vbs(command: list, hermes_home: str, pid_file: str) -> str:
    """The launcher the task runs with wscript.exe: nothing to do while the helper named in the process id file
    is still running; else it starts the helper with no window and does not wait for it. \\r\\n line ends."""
    cmd = [_plain(c, "the hermes command") for c in command]
    if not cmd or not os.path.isabs(cmd[0]) and not re.match(r"^[A-Za-z]:\\", cmd[0]):
        raise AgentError("no_hermes", MSG_NO_HERMES)
    home = _plain(hermes_home, "the Hermes folder")
    line = subprocess.list2cmdline(cmd)
    lines = [
        "' Made by the Grok Bot Twin plugin for Hermes. Remove with: hermes twin background off",
        "Option Explicit",
        "Dim sh, fso, env, f, pid, procs, p, running",
        'Set sh = CreateObject("WScript.Shell")',
        'Set fso = CreateObject("Scripting.FileSystemObject")',
        f"If Not fso.FileExists({_q(cmd[0])}) Then WScript.Quit 0",
        "running = False",
        "On Error Resume Next",
        f"If fso.FileExists({_q(_plain(pid_file, 'the Hermes folder'))}) Then",
        f"  Set f = fso.OpenTextFile({_q(pid_file)}, 1)",
        "  pid = CLng(Trim(f.ReadLine))",
        "  f.Close",
        "  If Err.Number = 0 And pid > 0 Then",
        '    Set procs = GetObject("winmgmts:\\\\.\\root\\cimv2").ExecQuery('
        '"Select Name from Win32_Process where ProcessId = " & pid)',
        "    For Each p In procs",
        '      If InStr(1, p.Name, "python", 1) = 1 Or InStr(1, p.Name, "hermes", 1) = 1 Then running = True',
        "    Next",
        "  End If",
        "End If",
        "Err.Clear",
        "On Error GoTo 0",
        "If running Then WScript.Quit 0",
        'Set env = sh.Environment("PROCESS")',
        f'env.Item("HERMES_HOME") = {_q(home)}',
        'env.Item("PYTHONUTF8") = "1"',
        'env.Item("PYTHONUNBUFFERED") = "1"',
        f"sh.CurrentDirectory = {_q(home)}",
        f"sh.Run {_q(line)}, 0, False",  # 0: no window; False: do not wait
    ]
    return "\r\n".join(lines) + "\r\n"


def render_task_xml(vbs: str, user_sid: Optional[str], logon_trigger: bool = True) -> str:
    """The task: the user's own, least privilege, at sign-in and every RECHECK, never a second copy at once."""
    vbs = _plain(vbs, "the Hermes folder")
    uid = f"\n      <UserId>{escape(user_sid)}</UserId>" if user_sid else ""
    logon = f"""
    <LogonTrigger>
      <Enabled>true</Enabled>{uid}
      <Delay>{LOGON_DELAY}</Delay>
    </LogonTrigger>""" if logon_trigger else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(DESCRIPTION)}</Description>
  </RegistrationInfo>
  <Triggers>{logon}
    <TimeTrigger>
      <Repetition>
        <Interval>{RECHECK}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2000-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">{uid}
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(_tool("wscript.exe"))}</Command>
      <Arguments>//B //Nologo "{escape(vbs)}"</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _write(path: Path, data: bytes) -> None:
    """Atomic write inside the plugin's data folder (folder chain checked, a link in the file's place refused)."""
    store.ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = _osflags.open_nofollow(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _osflags.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _delete(path: Path) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


# ---- running Windows' own tools --------------------------------------------------------------------------------
def _decode(data: bytes) -> str:
    for enc in ("utf-8", "mbcs", "cp1252"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _run(argv: list) -> tuple:
    """(return code, output) of one schtasks / taskkill / whoami call; never raises, never shows a window."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=TOOL_TIMEOUT, shell=False, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return done.returncode, _decode(done.stdout or b"")[:4000]
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        return 127, exc.__class__.__name__


def _short(text: str) -> str:
    line = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")
    return re.sub(r"[\x00-\x1f\x7f]", " ", line)[:160]


def user_sid(run: Runner) -> Optional[str]:
    """The signed-in account as Windows itself names it (S-1-5-21-...). The account's name is not used: under a
    remote shell Windows reports a made-up domain for it, and a task for that name is refused."""
    rc, out = run([_tool("whoami.exe"), "/user", "/fo", "csv", "/nh"])
    m = re.search(r"S-1-\d+(?:-\d+)+", out) if rc == 0 else None
    return m.group(0) if m else None


# ---- the helper's process ---------------------------------------------------------------------------------------
def _proc_started(pid: int) -> Optional[int]:
    """When the process with this id started (Windows' own 100 ns count), or None when there is no such process."""
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        return None
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not k.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # 259: still running
            return None
        made, ended, kern, user = (wintypes.FILETIME() for _ in range(4))
        if not k.GetProcessTimes(handle, ctypes.byref(made), ctypes.byref(ended), ctypes.byref(kern),
                                 ctypes.byref(user)):
            return None
        return (made.dwHighDateTime << 32) | made.dwLowDateTime
    finally:
        k.CloseHandle(handle)


def read_pid(pid_file: Path) -> Optional[tuple]:
    """(pid, started) from the helper's process id file, or None."""
    try:
        fd = _osflags.open_nofollow(pid_file, os.O_RDONLY)
    except OSError:
        return None
    try:
        words = os.read(fd, 200).decode("ascii", "replace").split()
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return int(words[0]), int(words[1])
    except (IndexError, ValueError):
        return None


def helper_pid(pid_file: Path) -> int:
    """The running helper's process id, 0 when it is not running. The start time kept next to the id must match,
    so a stale file can never point at some other program that got the same id later."""
    rec = read_pid(pid_file)
    if rec is None or rec[0] <= 0:
        return 0
    return rec[0] if _proc_started(rec[0]) == rec[1] else 0


# ---- the three calls -------------------------------------------------------------------------------------------
def _task_exists(name: str, run: Runner) -> bool:
    return run([_tool("schtasks.exe"), "/Query", "/TN", name])[0] == 0


def status(root: Optional[Path] = None, run: Optional[Runner] = None) -> dict:
    run = run or _run
    root = Path(root) if root is not None else store.hermes_root()
    name, f = task_name(root), files(root)
    installed = _task_exists(name, run)
    running = installed and helper_pid(f["pid"]) > 0
    out = {"supported": True, "kind": KIND, "installed": installed, "running": running, "label": name,
           "file": str(f["vbs"]), "task": name, "after_logout": False}
    if not installed:
        out["state"], out["message"] = "off", MSG_OFF
    elif running:
        out["state"], out["message"] = "on", MSG_ON_LOGIN_ONLY
    else:
        out["state"], out["message"] = "stopped", MSG_STOPPED
    return out


def install(root: Optional[Path] = None, run: Optional[Runner] = None, hermes: Optional[str] = None,
            command: Optional[list] = None) -> dict:
    """Add the task and start the helper (safe to repeat). `command` replaces `hermes twin background run` for a
    machine without a hermes program (tools\\fleet.py). Raises AgentError in plain English."""
    run = run or _run
    root = Path(root) if root is not None else store.hermes_root()
    name, f = task_name(root), files(root)
    if command is None:
        problem = admin_install_problem()
        if problem:  # the helper could never load the plugin: say so now instead of waiting for a start that fails
            raise AgentError("admin_install", problem[0])
        exe = hermes if hermes is not None else hermes_command(root)
        if not exe:
            raise AgentError("no_hermes", MSG_NO_HERMES)
        command = [exe, *RUN_ARGS]
    vbs = render_vbs(list(command), str(root), str(f["pid"]))
    _write(f["vbs"], b"\xff\xfe" + vbs.encode("utf-16-le"))  # with the mark: Windows Script Host needs it
    _delete(f["stop"])
    sid = user_sid(run)
    xml_path = f["vbs"].with_name("follower-agent.task.xml")
    rc, out = 1, ""
    try:
        for logon in (True, False):  # an account that may not add a sign-in trigger still gets the timed look
            _write(xml_path, b"\xff\xfe" + render_task_xml(str(f["vbs"]), sid, logon).encode("utf-16-le"))
            rc, out = run([_tool("schtasks.exe"), "/Create", "/F", "/TN", name, "/XML", str(xml_path)])
            if rc == 0:
                break
    finally:
        _delete(xml_path)
    if rc != 0:
        _delete(f["vbs"])
        raise AgentError("task_refused", MSG_DENIED + (f" ({_short(out)})" if _short(out) else ""))
    run([_tool("schtasks.exe"), "/Run", "/TN", name])
    deadline = time.monotonic() + START_WAIT_S
    while helper_pid(f["pid"]) == 0 and time.monotonic() < deadline:
        time.sleep(0.5)
    res = status(root, run)
    if not res["running"]:
        res["message"] = MSG_NEXT_SIGN_IN
    res.update({"changed": True, "undo": UNDO_ON})
    return res


def _stop_helper(f: dict, run: Runner) -> None:
    pid = helper_pid(f["pid"])
    if pid:
        with contextlib.suppress(OSError):
            _write(f["stop"], b"stop\n")
        deadline = time.monotonic() + STOP_WAIT_S
        while helper_pid(f["pid"]) and time.monotonic() < deadline:
            time.sleep(0.25)
        if helper_pid(f["pid"]) == pid:  # still that same process: end it and what it started
            run([_tool("taskkill.exe"), "/PID", str(pid), "/T", "/F"])


def remove(root: Optional[Path] = None, run: Optional[Runner] = None) -> dict:
    """Take the task away, stop the helper, delete its launcher, process id file and log. Safe to repeat."""
    run = run or _run
    root = Path(root) if root is not None else store.hermes_root()
    name, f = task_name(root), files(root)
    was = _task_exists(name, run)
    if was:  # the task first, so nothing starts the helper again while it is being stopped
        run([_tool("schtasks.exe"), "/End", "/TN", name])
        rc, out = run([_tool("schtasks.exe"), "/Delete", "/F", "/TN", name])
        if rc != 0 and _task_exists(name, run):
            raise AgentError("task_stays", "Windows did not let Hermes remove the scheduled task"
                             + (f" ({_short(out)})." if _short(out) else "."))
    _stop_helper(f, run)
    left = [_delete(f[k]) for k in ("vbs", "pid", "stop", "log")]
    was = was or left[0]
    return {"removed": was, "file": str(f["vbs"]), "task": name,
            "message": MSG_REMOVED if was else MSG_NOT_THERE}


# ---- hermes twin background run (what the task starts) ----------------------------------------------------------
def _open_log(path: Path):
    with contextlib.suppress(OSError):
        if path.stat().st_size > LOG_CAP:
            _osflags.replace(path, path.with_name(path.name + ".1"))
    fd = _osflags.open_nofollow(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="\n", buffering=1)


def run_agent(work: Callable[[], int], root: Optional[Path] = None, interrupt: Optional[Callable[[], None]] = None,
              poll_s: float = 2.0) -> int:
    """Run `work` (the fleet's foreground loop) as the background helper: one at a time, its output in the log,
    its process id on file, and a watcher that asks it to stop when the stop file appears."""
    root = Path(root) if root is not None else store.hermes_root()
    f = files(root)
    store.ensure_dir(f["pid"].parent)
    if helper_pid(f["pid"]) not in (0, os.getpid()):
        return 0  # one is running already (the task looked again, or `on` was pressed twice)
    log = _open_log(f["log"])
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = log
    _delete(f["stop"])
    _write(f["pid"], f"{os.getpid()}\n{_proc_started(os.getpid()) or 0}\n".encode("ascii"))
    done = threading.Event()
    if interrupt is None:
        import _thread
        interrupt = _thread.interrupt_main

    def _watch() -> None:
        while not done.wait(poll_s):
            if os.path.exists(f["stop"]):
                interrupt()  # the same as Ctrl-C in a terminal: the loop ends cleanly
                return

    threading.Thread(target=_watch, name="grok-bot-twin-stop-watch", daemon=True).start()
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} background helper started (process {os.getpid()})")
    try:
        return int(work() or 0)
    except KeyboardInterrupt:
        return 0
    finally:
        done.set()
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} background helper stopped")
        rec = read_pid(f["pid"])
        if rec is not None and rec[0] == os.getpid():
            _delete(f["pid"])
        _delete(f["stop"])
        sys.stdout, sys.stderr = old_out, old_err
        with contextlib.suppress(OSError):
            log.close()
