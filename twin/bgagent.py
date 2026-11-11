"""The background helper: keeps this computer's twins up to date with your other computers while Hermes is closed.

Meant for a computer that follows another one (it runs `hermes twin fleet run`, which follows on a linked computer
and keeps sharing on the computer with the Grok app).

One small per-user service, made by install() and deleted by remove(). Nothing runs as root, nothing listens on
the network (the follower only makes outgoing connections), and the one file it adds is the only thing outside
the Hermes folder:

- Linux: a systemd user unit, `$XDG_CONFIG_HOME/systemd/user/grok-bot-twin-follower.service` (XDG_CONFIG_HOME
  defaults to ~/.config). Started and stopped with `systemctl --user`. It runs while you are logged in; with
  "lingering" on for your account (a system setting the plugin only reads, never changes) also after you log out.
- macOS: a launchd agent, `~/Library/LaunchAgents/com.grok-bot-twin.follower.plist`. Started and stopped with
  `launchctl`. It runs while you are logged in. macOS shows its own "background item added" notice once.
- Windows: a scheduled task of your own account (no administrator rights), see bgagent_windows.py.
- anything else: not offered. Twins then follow while Hermes itself is open, which needs no agent at all.

A Hermes folder other than ~/.hermes (a second install, a test sandbox) gets its own name with a suffix made from
that folder, so two installs never share or overwrite one agent.

The agent runs one command: the Hermes CLI with FOLLOW_ARGS and HERMES_HOME set. The plugin runs `systemctl`
and `launchctl` from fixed system folders only (never looked up on PATH), with an argument list, no shell.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import store
from .engine import EngineError

UNIT_BASE = "grok-bot-twin-follower"
LAUNCHD_BASE = "com.grok-bot-twin.follower"
# What the agent runs after the Hermes command: the foreground loop of the sync contract. It follows the
# linked Mac, waits quietly while this computer is not linked yet, and ends cleanly when the service is stopped.
FOLLOW_ARGS: tuple[str, ...] = ("twin", "fleet", "run")
RESTART_SECONDS = 30
# The helper must never spin when its command is gone: the plugin was removed or switched off without
# `hermes twin background off` (Hermes then answers `hermes twin ...` with exit status 2), or Hermes itself was
# removed (126 / 127 / systemd's 203). systemd is told not to restart after those; launchd cannot be told, so
# there the agent first asks `hermes twin --help` and ends quietly when that fails, else becomes the real command.
NO_RESTART_STATUS = "2 126 127 203"
LAUNCHD_GUARD = 'if "$0" twin --help >/dev/null 2>&1; then exec "$0" "$@"; fi; exit 0'
LOG_NAME = "follower-agent.log"
SYSTEMCTL_DIRS = ("/usr/bin", "/bin", "/usr/local/bin")
LAUNCHCTL = "/bin/launchctl"
TOOL_TIMEOUT = 25
STOP_WAIT_SECONDS = 10.0
START_WAIT_SECONDS = 6.0

DESCRIPTION = "Grok Bot Twin: keeps this computer's twins up to date with your other computers"
# plain-English lines for the page and the CLI
MSG_ON = "Twins on this computer stay up to date with your other computers, also while Hermes is closed."
MSG_ON_LOGIN_ONLY = ("Twins on this computer stay up to date with your other computers while you are logged in, "
                     "also while Hermes is closed.")
MSG_OFF = "Twins on this computer stay up to date with your other computers only while Hermes is open."
MSG_STOPPED = ("The background helper is set up but not running right now. Turn it off and on again, or restart "
               "this computer.")
MSG_UNSUPPORTED_LINUX = ("This computer has no systemd user services, so there is no background helper here. "
                         "Twins stay up to date while Hermes is open.")
MSG_UNSUPPORTED_OS = ("A background helper is not available on this kind of computer yet. Twins stay up to date "
                      "while Hermes is open.")
MSG_NO_HERMES = "The hermes command was not found, so the background helper cannot be set up."
MSG_REMOVED = "The background helper is removed. Nothing of it is left on this computer."
MSG_NOT_THERE = "There is no background helper on this computer."
UNDO_ON = "Undo: hermes twin background off (removes the one file it added)."

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_SLUG = re.compile(r"[^a-z0-9]+")

Runner = Callable[[list], tuple]


class AgentError(EngineError):
    """The background helper could not be set up or removed; `message` is plain English. An EngineError, so the
    Twins page gets it as the usual {"error": {...}} answer and the terminal prints it like any other problem."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@dataclass(frozen=True)
class Plan:
    kind: str                 # "systemd" | "launchd" | "unsupported"
    label: str                # unit name (grok-bot-twin-follower.service) or launchd label
    path: Optional[Path]      # the one file the agent adds
    command: tuple            # what it runs
    hermes_home: str          # HERMES_HOME for it
    log: Optional[Path]       # launchd only: where its output goes (inside the plugin's data folder)
    why: str = ""             # unsupported: the sentence to show


# ---- naming ----------------------------------------------------------------------------------------------------
def _platform() -> str:
    return sys.platform


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def suffix(root: Optional[Path] = None) -> str:
    """'' for the standard Hermes folder (~/.hermes); else a short, stable name for this Hermes folder
    ('twin-lab-3f9a1c'): the folder that holds it, plus six characters of a hash of its full path."""
    root = Path(root) if root is not None else store.hermes_root()
    root_s = os.path.normpath(os.path.abspath(str(root)))
    if root_s == os.path.normpath(str(_home() / ".hermes")):
        return ""
    parent = _SLUG.sub("-", os.path.basename(os.path.dirname(root_s)).lower()).strip("-")[:24]
    digest = hashlib.sha256(root_s.encode("utf-8", "surrogateescape")).hexdigest()[:6]
    return f"{parent}-{digest}" if parent else digest


def unit_name(root: Optional[Path] = None) -> str:
    s = suffix(root)
    return f"{UNIT_BASE}-{s}.service" if s else f"{UNIT_BASE}.service"


def launchd_label(root: Optional[Path] = None) -> str:
    s = suffix(root)
    return f"{LAUNCHD_BASE}.{s}" if s else LAUNCHD_BASE


def systemd_user_dir() -> Path:
    """$XDG_CONFIG_HOME/systemd/user (an absolute XDG_CONFIG_HOME only, as the XDG rules say), else
    ~/.config/systemd/user."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg and os.path.isabs(xdg) else _home() / ".config"
    return base / "systemd" / "user"


def launch_agents_dir() -> Path:
    return _home() / "Library" / "LaunchAgents"


# ---- the command the agent runs --------------------------------------------------------------------------------
def _usable(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or not st.st_mode & 0o111:
        return False
    if os.name == "posix" and (st.st_uid not in (os.getuid(), 0) or st.st_mode & 0o022):
        return False  # a hermes that others can rewrite is never started in the background
    return True


def hermes_command() -> Optional[str]:
    """The `hermes` command a background agent can start on its own: the one this process was started as, the one
    next to this Python (a venv install), the one on PATH, then ~/.local/bin/hermes (a Desktop app starts with a
    short PATH). Absolute, a regular executable file, owned by the user or root and not writable by others."""
    seen: list[str] = []
    argv0 = sys.argv[0] if sys.argv and isinstance(sys.argv[0], str) else ""
    if os.path.isabs(argv0) and os.path.basename(argv0) == "hermes":
        seen.append(argv0)
    if sys.executable and os.path.isabs(sys.executable):
        seen.append(os.path.join(os.path.dirname(sys.executable), "hermes"))
    found = shutil.which("hermes")
    if found:
        seen.append(os.path.abspath(found))
    seen.append(str(_home() / ".local" / "bin" / "hermes"))
    for cand in seen:
        if _usable(cand):
            return cand
    return None


def plan(root: Optional[Path] = None, hermes: Optional[str] = None) -> Plan:
    """What install() would do on this computer, without doing it."""
    root = Path(root) if root is not None else store.hermes_root()
    root_s = os.path.normpath(os.path.abspath(str(root)))
    plat = _platform()
    if plat.startswith("linux"):
        kind, label, path, log, why = "systemd", unit_name(root), systemd_user_dir() / unit_name(root), None, ""
        if _systemctl() is None:
            kind, why = "unsupported", MSG_UNSUPPORTED_LINUX
    elif plat == "darwin":
        label = launchd_label(root)
        kind, path, why = "launchd", launch_agents_dir() / f"{label}.plist", ""
        log = Path(root_s) / "plugin-data" / "grok-bot-twin" / LOG_NAME
    else:
        return Plan("unsupported", "", None, (), root_s, None, MSG_UNSUPPORTED_OS)
    exe = hermes if hermes is not None else hermes_command()
    command = (exe, *FOLLOW_ARGS) if exe else ()
    return Plan(kind, label, path, command, root_s, log, why)


# ---- the two file formats --------------------------------------------------------------------------------------
def _refuse_odd(value: str, what: str) -> str:
    if not value or _CTRL.search(value):
        raise AgentError("bad_path", f"The background helper cannot be set up: {what} has an unusual name.")
    return value


def _sd_quote(value: str) -> str:
    """One word of a systemd command line or Environment= value: double-quoted, with systemd's own specials
    (backslash, quote, % and $) made literal."""
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{value}"'


def render_systemd(p: Plan) -> str:
    cmd = [_refuse_odd(str(c), "the hermes command") for c in p.command]
    if not cmd or not os.path.isabs(cmd[0]):
        raise AgentError("no_hermes", MSG_NO_HERMES)
    home = _refuse_odd(p.hermes_home, "the Hermes folder")
    return "\n".join([
        "# Made by the Grok Bot Twin plugin for Hermes. Remove with: hermes twin background off",
        "[Unit]",
        f"Description={DESCRIPTION}",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(_sd_quote(c) for c in cmd),
        "Environment=" + _sd_quote(f"HERMES_HOME={home}"),
        "Environment=PYTHONUNBUFFERED=1",
        "Restart=on-failure",
        f"RestartSec={RESTART_SECONDS}",
        f"RestartPreventExitStatus={NO_RESTART_STATUS}",
        "Nice=10",
        "NoNewPrivileges=yes",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def render_launchd(p: Plan) -> bytes:
    cmd = [_refuse_odd(str(c), "the hermes command") for c in p.command]
    if not cmd or not os.path.isabs(cmd[0]):
        raise AgentError("no_hermes", MSG_NO_HERMES)
    body = {
        "Label": p.label,
        # /bin/sh -c GUARD hermes twin fleet run: the path and the words are separate arguments ($0, $@), never
        # part of the shell text
        "ProgramArguments": ["/bin/sh", "-c", LAUNCHD_GUARD, *cmd],
        "EnvironmentVariables": {"HERMES_HOME": _refuse_odd(p.hermes_home, "the Hermes folder"),
                                 "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},   # started again only after a crash, never after a clean stop
        "ThrottleInterval": RESTART_SECONDS,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
    }
    if p.log is not None:
        body["StandardOutPath"] = str(p.log)
        body["StandardErrorPath"] = str(p.log)
    return plistlib.dumps(body, fmt=plistlib.FMT_XML, sort_keys=True)


def _write_file(path: Path, data: bytes) -> None:
    """Atomic write of the agent's one file (0644: systemd and launchd both read it as the user). The folder is
    made when missing; a link or anything that is not a regular file in its place is refused."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise AgentError("unsafe_file", f"Refusing to write {path.name}: something that is not a plain file "
                                            "is in its place.")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            if os.name == "posix":
                os.fchmod(fh.fileno(), 0o644)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _delete_file(path: Optional[Path]) -> bool:
    """Delete the agent's file. True when a file was there. A folder in its place is left alone."""
    if path is None:
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISDIR(st.st_mode):
        return False
    with contextlib.suppress(OSError):
        os.unlink(path)
        return True
    return False


# ---- running systemctl / launchctl -----------------------------------------------------------------------------
def _systemctl() -> Optional[str]:
    for d in SYSTEMCTL_DIRS:
        cand = os.path.join(d, "systemctl")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _tool_env() -> dict:
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C", "HOME": str(_home())}
    for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XDG_CONFIG_HOME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if _platform().startswith("linux") and "XDG_RUNTIME_DIR" not in env:
        guess = f"/run/user/{os.getuid()}"  # set by the login session; missing under some remote shells
        if os.path.isdir(guess):
            env["XDG_RUNTIME_DIR"] = guess
    return env


def _run(argv: list) -> tuple:
    """(return code, output) of one systemctl / launchctl call; never raises."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=_tool_env(), timeout=TOOL_TIMEOUT, shell=False, check=False)
        return done.returncode, done.stdout.decode("utf-8", "replace")[:4000]
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        return 127, exc.__class__.__name__


def _short(text: str) -> str:
    line = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")
    return _CTRL.sub(" ", line)[:160]


# ---- systemd ---------------------------------------------------------------------------------------------------
def _linger() -> bool:
    """Whether this account's services keep running after logout (read only; the marker file systemd keeps)."""
    try:
        import pwd
        name = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        return False
    return os.path.exists(os.path.join("/var/lib/systemd/linger", name))


def _systemd_install(p: Plan, run: Runner) -> None:
    ctl = _systemctl()
    _write_file(p.path, render_systemd(p).encode("utf-8"))
    rc, out = run([ctl, "--user", "daemon-reload"])
    if rc != 0:
        _delete_file(p.path)
        raise AgentError("no_user_services", MSG_UNSUPPORTED_LINUX + (f" ({_short(out)})" if _short(out) else ""))
    run([ctl, "--user", "reset-failed", p.label])
    rc, out = run([ctl, "--user", "enable", p.label])
    rc2, out2 = run([ctl, "--user", "restart", p.label]) if rc == 0 else (rc, out)
    if rc != 0 or rc2 != 0:
        _systemd_remove(p, run)  # never leave a half-made agent behind
        raise AgentError("start_failed", "The background helper could not be started"
                         + (f" ({_short(out2 if rc == 0 else out)})." if _short(out2 if rc == 0 else out) else "."))


def _systemd_remove(p: Plan, run: Runner) -> bool:
    ctl = _systemctl()
    if ctl is not None:
        run([ctl, "--user", "disable", "--now", p.label])
    was = _delete_file(p.path)
    if ctl is not None:
        run([ctl, "--user", "daemon-reload"])
        run([ctl, "--user", "reset-failed", p.label])
    return was


def _systemd_running(p: Plan, run: Runner) -> tuple:
    ctl = _systemctl()
    if ctl is None:
        return False, ""
    rc, out = run([ctl, "--user", "show", p.label, "--property=ActiveState,SubState,MainPID"])
    facts = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln) if rc == 0 else {}
    state = facts.get("ActiveState", "")
    # "activating" covers the pause before a restart after a crash (auto-restart): set up, not running this second
    return state == "active", state


# ---- launchd ---------------------------------------------------------------------------------------------------
def _launchd_domains() -> list:
    uid = os.getuid()
    return [f"gui/{uid}", f"user/{uid}"]  # gui: a logged-in desktop session; user: a remote-only login


def _launchd_loaded(p: Plan, run: Runner) -> tuple:
    """(domain or '', running, pid)."""
    for dom in _launchd_domains():
        rc, out = run([LAUNCHCTL, "print", f"{dom}/{p.label}"])
        if rc == 0:
            running = bool(re.search(r"^\s*state = running\s*$", out, re.M))
            m = re.search(r"^\s*pid = (\d+)\s*$", out, re.M)
            return dom, running, int(m.group(1)) if m else 0
    return "", False, 0


def _launchd_install(p: Plan, run: Runner) -> None:
    if p.log is not None:
        store.ensure_dir(p.log.parent)
    data = render_launchd(p)
    dom, _running, _pid = _launchd_loaded(p, run)
    if dom:
        run([LAUNCHCTL, "bootout", f"{dom}/{p.label}"])
    _write_file(p.path, data)
    last = ""
    for dom in _launchd_domains():
        rc, out = run([LAUNCHCTL, "bootstrap", dom, str(p.path)])
        if rc == 0:
            run([LAUNCHCTL, "enable", f"{dom}/{p.label}"])
            # launchd answers before it has started the agent: wait a moment, so that the state reported right
            # after "on" is the real one (otherwise it can read "set up but not running" for an instant)
            end = time.monotonic() + START_WAIT_SECONDS
            while time.monotonic() < end and not _launchd_loaded(p, run)[1]:
                time.sleep(0.2)
            return
        last = out
    _launchd_remove(p, run)
    raise AgentError("start_failed", "The background helper could not be started"
                     + (f" ({_short(last)})." if _short(last) else "."))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # there, but not ours to signal
    return True


def _wait_gone(pid: int, seconds: float = STOP_WAIT_SECONDS) -> bool:
    """Wait (briefly) until a process has really ended. True when it is gone."""
    end = time.monotonic() + seconds
    while _alive(pid):
        if time.monotonic() >= end:
            return False
        time.sleep(0.2)
    return True


def _launchd_remove(p: Plan, run: Runner) -> bool:
    _dom, _running, pid = _launchd_loaded(p, run)
    for dom in _launchd_domains():
        run([LAUNCHCTL, "bootout", f"{dom}/{p.label}"])
    if pid:
        # launchd may answer before the helper has finished closing its connection: "off" must mean off
        _wait_gone(pid)
    was = _delete_file(p.path)
    _delete_file(p.log)
    return was


# ---- the three calls -------------------------------------------------------------------------------------------
def _windows():
    """The Windows helper's module on Windows, else None (it has the same three calls)."""
    if _platform() != "win32":
        return None
    from . import bgagent_windows
    return bgagent_windows


def status(root: Optional[Path] = None, run: Optional[Runner] = None) -> dict:
    """{"supported", "kind", "installed", "running", "state", "label", "file", "after_logout", "message"}.
    state: "on" | "stopped" | "off" | "unsupported". kind: "systemd" | "launchd" | "schtasks" | "unsupported"."""
    win = _windows()
    if win is not None:
        return win.status(root, run)
    run = run or _run
    p = plan(root)
    out = {"supported": p.kind != "unsupported", "kind": p.kind, "installed": False, "running": False,
           "state": "unsupported", "label": p.label, "file": str(p.path) if p.path else "",
           "after_logout": False, "message": p.why}
    if p.kind == "unsupported":
        return out
    out["installed"] = bool(p.path) and os.path.isfile(p.path)
    if p.kind == "systemd":
        out["after_logout"] = _linger()
        out["running"] = out["installed"] and _systemd_running(p, run)[0]
    else:
        dom, running, _pid = _launchd_loaded(p, run)
        out["running"] = out["installed"] and bool(dom) and running
    if not out["installed"]:
        out["state"], out["message"] = "off", MSG_OFF
    elif out["running"]:
        out["state"], out["message"] = "on", (MSG_ON if out["after_logout"] else MSG_ON_LOGIN_ONLY)
    else:
        out["state"], out["message"] = "stopped", MSG_STOPPED
    return out


def install(root: Optional[Path] = None, run: Optional[Runner] = None, hermes: Optional[str] = None) -> dict:
    """Set the agent up and start it (again: safe to repeat; the file is rewritten and the agent restarted).
    Returns status() plus {"changed": True, "undo": ...}. Raises AgentError with a plain sentence."""
    win = _windows()
    if win is not None:
        return win.install(root, run, hermes)
    run = run or _run
    p = plan(root, hermes)
    if p.kind == "unsupported":
        raise AgentError("unsupported", p.why)
    if not p.command:
        raise AgentError("no_hermes", MSG_NO_HERMES)
    if p.kind == "systemd":
        _systemd_install(p, run)
    else:
        _launchd_install(p, run)
    out = status(root, run)
    out.update({"changed": True, "undo": UNDO_ON})
    return out


def remove(root: Optional[Path] = None, run: Optional[Runner] = None) -> dict:
    """Stop the agent and delete its one file (and, on macOS, its log). Safe to repeat. Never raises for a
    missing agent. Returns {"removed": bool, "file", "message"}."""
    win = _windows()
    if win is not None:
        return win.remove(root, run)
    run = run or _run
    p = plan(root, hermes="")
    if p.path is None:
        return {"removed": False, "file": "", "message": MSG_NOT_THERE}
    was = _launchd_remove(p, run) if (p.kind == "launchd") else _systemd_remove(p, run)
    return {"removed": was, "file": str(p.path), "message": MSG_REMOVED if was else MSG_NOT_THERE}


# ---- what the Twins page and the rest of the plugin call ---------------------------------------------------------
GOES_ON_UNLINK = "The background helper on this computer (one small file)."
LINK_HINT = "Stay up to date while Hermes is closed:  hermes twin background on"


def switch(on: object) -> dict:
    """The page's switch (POST /fleet/background {"on": bool}): on = install(), off = remove(); the answer is
    status() plus "changed"."""
    if not isinstance(on, bool):
        raise EngineError("bad_input", "That request was not understood.")
    if on:
        return install()
    was = remove()["removed"]
    out = status()
    out["changed"] = was
    return out


def after_unlink(leaving: dict, dry_run: bool) -> dict:
    """A computer that stops following needs no helper: the unlink answer names it under "goes" when one is set up,
    and a real unlink removes it. Never raises; the unlink itself has already happened."""
    try:
        if not status()["installed"]:
            return leaving
        if isinstance(leaving.get("goes"), list):
            leaving["goes"] = list(leaving["goes"]) + [GOES_ON_UNLINK]
        if not dry_run:
            remove()
    except Exception:  # the helper is a convenience; a failure here must not undo or hide the unlink
        pass
    return leaving


def link_hint() -> str:
    """One line for the terminal after linking: how to keep following while Hermes is closed ('' when the helper
    is already on or not available here)."""
    try:
        st = status()
    except Exception:
        return ""
    return LINK_HINT if st["supported"] and not st["installed"] else ""


# ---- hermes twin background [on|off|status] ---------------------------------------------------------------------
def add_cli(subs) -> None:
    p = subs.add_parser("background", help="Keep this computer's twins up to date while Hermes is closed: on, off, status")
    # "run" is what the Windows helper's scheduled task starts; nobody types it
    p.add_argument("action", nargs="?", choices=("on", "off", "status", "run"), default="status")
    p.add_argument("--json", action="store_true", help="Machine-readable output")


def _run_helper(args) -> int:
    """`hermes twin background run`: the fleet's foreground loop as the Windows helper runs it (log, process id
    file, stop file). On the other systems the service manager does all that, so it is the plain loop there."""
    from . import fleet_cli
    win = _windows()
    if win is None:
        return fleet_cli.run(args)
    return win.run_agent(lambda: fleet_cli.run(args))


def handle_cli(args, out: Callable[[str], None] = print) -> int:
    import json
    action = getattr(args, "action", "status") or "status"
    as_json = bool(getattr(args, "json", False))
    if action == "run":
        return _run_helper(args)
    try:
        if action == "on":
            res = install()
        elif action == "off":
            res = remove()
        else:
            res = status()
    except AgentError as exc:
        fix = ""
        if exc.code == "admin_install":  # Windows: the one line that fixes it (a folder name, so not in the error)
            from . import bgagent_windows
            found = bgagent_windows.admin_install_problem()
            fix = f"{found[1]}   and then: hermes twin background on" if found else ""
        if as_json:
            out(json.dumps(dict({"ok": False, "code": exc.code, "message": exc.message}, **({"fix": fix} if fix else {}))))
        else:
            out(f"✗ {exc.message}" + (f"\n  {fix}" if fix else ""))
        return 1
    if as_json:
        out(json.dumps(dict(res, ok=True), sort_keys=True))
        return 0
    out(res["message"])
    if res.get("kind") == "schtasks" and action == "on":
        out(f"  It added one scheduled task of your account: {res['task']}")
        out("  Undo: hermes twin background off (removes that task).")
    elif res.get("kind") == "schtasks" and action == "status" and res.get("installed"):
        out(f"  Its one scheduled task: {res['task']}")
        out("  Turn it off (and remove that task): hermes twin background off")
    elif action == "on":
        out(f"  It added one file: {res['file']}")
        out(f"  {UNDO_ON}")
    elif action == "status" and res.get("installed"):
        out(f"  Its one file: {res['file']}")
        out("  Turn it off (and delete that file): hermes twin background off")
    elif action == "status" and res.get("supported"):
        out("  Keep them up to date while Hermes is closed: hermes twin background on")
    return 0
