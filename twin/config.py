"""Plugin settings (plugins.entries.grok-bot-twin.settings in config.yaml), validated; and the one place the
plugin imports Hermes' own code (see "Hermes' own code" below: every other module asks these functions)."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from .model import PLUGIN_ID, valid_id

DEFAULTS = {
    "sync_interval_seconds": 60,
    "watch_grok_changes": True,
    "bots": [],
    "botport_path": "",
    "botport_release_tag": "",
    "grok_app_dir": "",
    "grok_account": "",
    "inject_context": True,
    "demo_profile": "",
    "digest_to_grok": False,
    "digest_titles": False,
    "share_names_with_model": False,
    "share_port": 18777,
    "share_address": "",
    "machine_name": "",
    "mirror_name": "",  # the Mirror bot's name in Grok, for the Auto-Review rule text (Hermes to Grok beta)
}
MIN_INTERVAL = 5
MAX_INTERVAL = 86400

_cache: tuple[float, dict, str] | None = None  # (when, settings, whose folder)
_override: Optional[dict] = None  # tests


def set_override(values: Optional[dict]) -> None:
    global _override, _cache
    _override = dict(values) if values is not None else None
    _cache = None


def _entry_settings(cfg: Any) -> dict:
    plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    entry = entries.get(PLUGIN_ID) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        return {}
    settings = entry.get("settings")
    if not isinstance(settings, dict):
        settings = entry.get("config") if isinstance(entry.get("config"), dict) else {}
    return dict(settings)


def _own_home() -> str:
    """This chat's own Hermes folder. One Hermes process can serve several bots at once (the Desktop's and the
    gateway's "one process for every bot"): then the process's HERMES_HOME is the main folder and the bot in use is
    named for the current task only, so that is asked first. Without it a twin's chat in such a process took the
    process for the main folder's, read the twin's own (empty) settings through Hermes and used the default share
    port: a second share server, and "requests are switched off on NAME" while they were on."""
    return hermes_home() or os.environ.get("HERMES_HOME", "").strip()


def _inside_a_bot() -> tuple:
    """(main Hermes folder, this chat's own Hermes folder) when this code runs inside one of the main folder's
    bots (`<root>/profiles/<name>`, a twin's own Hermes bot say); (None, None) when it IS the main folder's or
    that cannot be told."""
    try:
        from . import store
        root = store.hermes_root()
        home = _own_home()
        if not home or os.path.abspath(os.path.expanduser(home)) == os.path.abspath(str(root)):
            return None, None
        return root, os.path.abspath(os.path.expanduser(home))
    except Exception:
        return None, None


def _root_settings(root: Any) -> dict:
    """The plugin's settings in the owner's main Hermes folder; {} when that file cannot be read. Read-only,
    capped, never through a link."""
    try:
        from . import fleet_io as fio, yamlio
        yaml = yamlio.module()
        text = fio.read_text(root / "config.yaml", 2 * 1024 * 1024)
        if yaml is None or not text:
            return {}
        return _entry_settings(yaml.safe_load(text))
    except Exception:
        return {}


def _raw() -> dict:
    """The settings as the owner set them. The plugin keeps one state for the whole computer (its data folder
    sits in the main Hermes folder), so its settings are the main folder's too: a copy of the plugin that runs
    inside a twin's own Hermes bot reads them from there, and that bot's own file only fills in what the main one
    does not set. Without this a twin's chat knew the computer by another name and looked for the sharing
    computer on the default port."""
    if _override is not None:
        return _override
    root, home = _inside_a_bot()
    own: dict = {}
    # (a bot whose folder has just been moved away, an unlinked twin's say, is not asked: reading its settings
    # through Hermes would make Hermes set the folder up again)
    if root is None or os.path.isdir(home):
        own = _entry_settings(own_hermes_config())
    return {**own, **_root_settings(root)} if root is not None else own


def settings() -> dict:
    global _cache
    whose = "" if _override is not None else _own_home()  # one process can serve several bots: never mix them
    if _cache is not None and time.monotonic() - _cache[0] < 5 and _cache[2] == whose:
        return _cache[1]
    raw = _raw()
    out = dict(DEFAULTS)
    try:
        out["sync_interval_seconds"] = min(MAX_INTERVAL, max(MIN_INTERVAL, int(raw.get("sync_interval_seconds", 60))))
    except (TypeError, ValueError):
        pass
    bots = raw.get("bots")
    if isinstance(bots, str):
        bots = [b.strip() for b in bots.split(",")]
    if isinstance(bots, list):
        out["bots"] = [str(b)[:100] for b in bots if isinstance(b, str) and b.strip()][:50]
    for key in ("botport_path", "grok_app_dir"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = os.path.expanduser(val.strip())
    out["inject_context"] = _bool(raw.get("inject_context"), True)
    out["watch_grok_changes"] = _bool(raw.get("watch_grok_changes"), True)
    out["digest_to_grok"] = _bool(raw.get("digest_to_grok"), False)
    out["digest_titles"] = _bool(raw.get("digest_titles"), False)
    out["share_names_with_model"] = _bool(raw.get("share_names_with_model"), False)
    acct = raw.get("grok_account")
    if isinstance(acct, str) and re.fullmatch(r"[0-9a-f]{8}", acct.strip().lower()):
        out["grok_account"] = acct.strip().lower()
    tag = raw.get("botport_release_tag")
    if isinstance(tag, str) and re.fullmatch(r"v\d+\.\d+\.\d+", tag.strip()):
        out["botport_release_tag"] = tag.strip()
    elif tag not in (None, ""):
        out["botport_release_tag_invalid"] = True  # set but not like v0.2.0: setup refuses rather than guess
    try:  # 0 lets the system choose a free port (tests)
        port = int(raw.get("share_port", 18777))
        out["share_port"] = port if 0 <= port <= 65535 else 18777
    except (TypeError, ValueError):
        pass
    for key, cap in (("share_address", 45), ("machine_name", 40), ("mirror_name", 60)):
        val = raw.get(key)
        if isinstance(val, str) and val.strip() and len(val.strip()) <= cap:
            out[key] = val.strip()
    dp = raw.get("demo_profile")
    if valid_id(dp):
        out["demo_profile"] = dp
    _cache = (time.monotonic(), out, whose)
    return out


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip().lower() in ("true", "1", "yes", "on")
    return default


def get(key: str) -> Any:
    return settings().get(key, DEFAULTS.get(key))


# ---- Hermes' own code: the one place it is imported --------------------------------------------------------------------
# Each function names what it needs from Hermes and what it does when that is missing or looks different (imported
# when asked: Hermes is not there everywhere this runs). FAIL CLOSED: chat_not_local and skill_scan; the rest falls back.
SURFACE_VARS = ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_SOURCE")


def default_hermes_root() -> Path:
    """The main Hermes folder: hermes_constants.get_default_hermes_root(); without it HERMES_HOME, else ~/.hermes."""
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except Exception:
        env = os.environ.get("HERMES_HOME", "").strip()
        return Path(env).expanduser() if env else Path.home() / ".hermes"


def hermes_home() -> Optional[str]:
    """This chat's Hermes folder: the bot named for the current task only (hermes_constants.get_hermes_home_override,
    when one process serves several bots), else the process's own (get_hermes_home). None when that cannot be asked."""
    try:
        import hermes_constants as hc
        return str(hc.get_hermes_home_override() or hc.get_hermes_home() or "").strip() or None
    except Exception:
        return None


def profile_name(home: Any) -> Optional[str]:
    """The name of the Hermes bot whose folder `home` is (hermes_constants.profile_name_for_home); without it only
    a folder inside a `profiles` folder is named (by its folder name). None when it cannot be named."""
    try:
        from hermes_constants import profile_name_for_home
        name = profile_name_for_home(home)
    except Exception:
        p = Path(str(home))
        name = p.name if p.parent.name == "profiles" else None
    return name if isinstance(name, str) and name else None


def own_hermes_config() -> dict:
    """This bot's own config.yaml as Hermes reads it (hermes_cli.config.load_config_readonly); {} without it."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def broadcast_event(etype: str, payload: dict) -> None:
    """Tell Hermes' Desktop about an event (hermes_cli.plugin_events.broadcast_plugin_event); nothing without it
    (the Desktop also polls /events)."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(PLUGIN_ID, etype, payload)
    except Exception:
        pass


def thread(target: Any, name: str, args: tuple = ()) -> Any:
    """A daemon thread (not started) that keeps Hermes' per-bot context (agent.memory_provider.spawn_context_thread);
    a plain thread without it."""
    import threading
    try:
        from agent.memory_provider import spawn_context_thread
        return spawn_context_thread(target, name=name, daemon=True, args=args)
    except Exception:
        return threading.Thread(target=target, name=name, daemon=True, args=args)


def known_toolsets() -> Optional[set]:
    """The tool sets this Hermes knows (toolsets.get_toolset_names); None when that cannot be told."""
    try:
        import toolsets
        names = set(toolsets.get_toolset_names())
        return names or None
    except Exception:
        return None


def hermes_default_soul(text: str) -> bool:
    """Is this prompt file Hermes' own starting text, never edited (hermes_cli.default_soul)? False when this Hermes
    cannot say (the folder is then the owner's)."""
    try:
        from hermes_cli.default_soul import DEFAULT_SOUL_MD, is_legacy_template_soul
        return text.strip() == DEFAULT_SOUL_MD.strip() or bool(is_legacy_template_soul(text))
    except Exception:
        return False


def skill_scan(path: Any) -> Optional[str]:
    """Hermes' skill scanner (tools.skills_guard.scan_skill) on one skill folder: 'safe', 'caution' or 'dangerous'.
    FAILS CLOSED: None (not scanned, never "safe") when the scanner is missing, raises, or answers in a shape not
    known here."""
    try:
        from tools.skills_guard import scan_skill
        verdict = getattr(scan_skill(Path(path), source="community"), "verdict", None)
    except Exception:
        return None
    return verdict if isinstance(verdict, str) and verdict in ("safe", "caution", "dangerous") else None


def chat_not_local() -> bool:
    """Is the chat this runs in NOT known to be on this computer? FAILS CLOSED. Hermes names a chat's origin per chat
    (gateway.session_context.get_session_env): a messaging app, a webhook or the API server in
    HERMES_SESSION_PLATFORM, its own terminal, terminal UI and Desktop in HERMES_SESSION_SOURCE. Local only when a
    named origin is one of those local surfaces (slash.LOCAL_PLATFORMS), none is another, and Hermes' own answer
    (session_is_messaging_surface), where it has one, is False. Anything Hermes cannot answer here is refused.

    HERMES_GATEWAY_SESSION is deliberately NOT looked at: Hermes sets it in the process behind its own terminal
    UI and Desktop too (it means "questions go to the interface, not to the keyboard")."""
    from .slash import LOCAL_PLATFORMS
    try:
        from gateway.session_context import get_session_env  # (per chat, also when several run at once)
        named = [get_session_env(n, "") or "" for n in SURFACE_VARS]
        if not all(isinstance(v, str) for v in named):
            return True
        named = [v.strip().lower() for v in named]
    except Exception:
        return True
    if not any(named) or any(v and v not in LOCAL_PLATFORMS for v in named):
        return True
    try:
        from gateway.session_context import session_is_messaging_surface as says
    except Exception:  # (an older Hermes: the named origin is all there is)
        return False
    try:
        return says() is not False
    except Exception:
        return True
