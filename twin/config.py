"""Plugin settings (plugins.entries.grok-bot-twin.settings in config.yaml), validated."""
from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

from .model import PLUGIN_ID, valid_id

DEFAULTS = {
    "sync_interval_seconds": 60,
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
}
MIN_INTERVAL = 5
MAX_INTERVAL = 86400

_cache: tuple[float, dict] | None = None
_override: Optional[dict] = None  # tests


def set_override(values: Optional[dict]) -> None:
    global _override, _cache
    _override = dict(values) if values is not None else None
    _cache = None


def _raw() -> dict:
    if _override is not None:
        return _override
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
    except Exception:
        return {}
    plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    entry = entries.get(PLUGIN_ID) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        return {}
    settings = entry.get("settings")
    if not isinstance(settings, dict):
        settings = entry.get("config") if isinstance(entry.get("config"), dict) else {}
    return dict(settings)


def settings() -> dict:
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < 5:
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
    dp = raw.get("demo_profile")
    if valid_id(dp):
        out["demo_profile"] = dp
    _cache = (time.monotonic(), out)
    return out


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip().lower() in ("true", "1", "yes", "on")
    return default


def get(key: str) -> Any:
    return settings().get(key, DEFAULTS.get(key))
