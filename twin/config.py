"""Plugin settings (plugins.entries.grok-bot-twin.settings in config.yaml), validated."""
from __future__ import annotations

import os
import re
import time
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
    try:
        from hermes_constants import get_hermes_home_override
        home = str(get_hermes_home_override() or "").strip()
        if home:
            return home
    except Exception:
        pass
    home = os.environ.get("HERMES_HOME", "").strip()
    if not home:
        try:
            from hermes_constants import get_hermes_home
            home = str(get_hermes_home())
        except Exception:
            home = ""
    return home


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
        try:
            from hermes_cli.config import load_config_readonly
            own = _entry_settings(load_config_readonly() or {})
        except Exception:
            own = {}
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
