"""Model servers already set up in this Hermes, for the batch model picker.

Read from the default profile's config.yaml only: the `model` block's provider/base_url and the custom endpoints
Hermes keeps under `custom_providers` (a list) and `providers` (a mapping, Hermes v12+). No key, key command or key
environment variable is read. Each endpoint gets one quick `GET <base_url>/models` (2 s, no Authorization header,
256 KB cap) to list the models it answers with. The plugin never downloads a model.
"""
from __future__ import annotations

import ipaddress
import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from . import hermes_side as hs
from . import store

ENDPOINTS_MAX = 6
IDS_MAX = 20
TIMEOUT = 2.0
BODY_CAP = 256 * 1024
CACHE_S = 30.0
NAME_MAX = 60

_cache: dict = {}
_lock = threading.Lock()


def _url_ok(url: str) -> Optional[urllib.parse.SplitResult]:
    if not isinstance(url, str) or "{" in url or len(url) > 500:
        return None
    try:
        u = urllib.parse.urlsplit(url.strip())
        u.port
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        return None
    return u


def host_label(url: str) -> str:
    """host:port only (never the path, query or credentials)."""
    u = _url_ok(url)
    if u is None:
        return ""
    host = u.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    return host + (":" + str(u.port) if u.port else "")


def is_local(url: str) -> bool:
    u = _url_ok(url)
    if u is None:
        return False
    host = (u.hostname or "").lower()
    if host == "localhost" or host.endswith(".local") or host.endswith(".localhost") or host.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")


def _name(value: Any) -> str:
    s = " ".join(str(value or "").split())[:NAME_MAX]
    return s if s and s.isprintable() else ""


def configured(root: Optional[Path] = None) -> list:
    """[{"name", "provider", "base_url", "model"}] from the default profile's config.yaml (at most ENDPOINTS_MAX)."""
    from .batch import model_id
    root = Path(root) if root is not None else store.hermes_root()
    text = hs._read_below_root(root / "config.yaml", hs.CONFIG_CAP)
    if not text:
        return []
    try:
        doc = hs._yaml_load(text)
    except Exception:
        return []
    if not isinstance(doc, dict):
        return []
    out: list = []
    seen: set = set()

    def add(name: Any, url: Any, model: Any, provider: str) -> None:
        nm = _name(name)
        if not nm or _url_ok(url) is None or len(out) >= ENDPOINTS_MAX:
            return
        key = url.strip().rstrip("/").lower()
        if key in seen or nm.lower() in {e["name"].lower() for e in out}:
            return
        seen.add(key)
        out.append({"name": nm, "provider": provider, "base_url": url.strip().rstrip("/"),
                    "model": model_id(model) or ""})

    entries: list = []
    cp = doc.get("custom_providers")
    if isinstance(cp, list):
        entries += [e for e in cp if isinstance(e, dict)]
    pv = doc.get("providers")
    if isinstance(pv, dict):
        for k, e in pv.items():
            if isinstance(e, dict):
                entries.append({**e, "name": e.get("name") or k})
    for e in entries:
        url = next((e.get(k) for k in ("base_url", "url", "api") if isinstance(e.get(k), str) and e.get(k)), "")
        model = e.get("model") or e.get("default_model")
        add(e.get("name"), url, model, "custom:" + str(_name(e.get("name"))).lower())
    m = doc.get("model")
    if isinstance(m, dict) and isinstance(m.get("base_url"), str):
        prov = str(m.get("provider") or "custom")
        nm = prov.split(":", 1)[1] if prov.startswith("custom:") else host_label(m["base_url"])
        add(nm, m["base_url"], m.get("default") or m.get("model"), prov)
    return out


def probe(base_url: str) -> Optional[list]:
    """Model ids from GET <base_url>/models, or None when it does not answer. No Authorization header is sent."""
    from .batch import model_id
    if _url_ok(base_url) is None:
        return None
    req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"Accept": "application/json"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=TIMEOUT) as r:
            body = r.read(BODY_CAP + 1)
        if len(body) > BODY_CAP:
            return None
        doc = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, list):
        return None
    ids: list = []
    for d in data:
        i = model_id(d.get("id")) if isinstance(d, dict) else None
        if i and i not in ids:
            ids.append(i)
        if len(ids) >= IDS_MAX:
            break
    return ids


PROBE: Callable[[str], Optional[list]] = probe   # tests replace this; no real network in tests


def endpoints(root: Optional[Path] = None, fresh: bool = False) -> list:
    """What batch_info lists: name, base_url_label, local, reachable, models, provider (base_url kept private)."""
    eps = configured(root)
    key = tuple((e["name"], e["base_url"], e["model"]) for e in eps)
    with _lock:
        hit = _cache.get("v")
        if not fresh and hit and hit[0] == key and time.monotonic() - hit[1] < CACHE_S and hit[3] is PROBE:
            return [dict(e) for e in hit[2]]
    results: dict = {}

    def run(i: int, url: str) -> None:
        try:
            results[i] = PROBE(url)
        except Exception:
            results[i] = None

    threads = [threading.Thread(target=run, args=(i, e["base_url"]), daemon=True) for i, e in enumerate(eps)]
    for t in threads:
        t.start()
    end = time.monotonic() + TIMEOUT + 0.5
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    out = []
    for i, e in enumerate(eps):
        ids = results.get(i)
        models = list(ids or [])
        if e["model"] and e["model"] not in models:
            models.insert(0, e["model"])
        out.append({"name": e["name"], "base_url_label": host_label(e["base_url"]), "local": is_local(e["base_url"]),
                    "reachable": ids is not None, "models": models[:IDS_MAX], "provider": e["provider"],
                    "_base_url": e["base_url"]})
    with _lock:
        _cache["v"] = (key, time.monotonic(), out, PROBE)
    return [dict(e) for e in out]


def public(eps: list) -> list:
    return [{k: v for k, v in e.items() if not k.startswith("_")} for e in eps]


def find(eps: list, name: Any, model: str) -> Optional[dict]:
    """The listed endpoint called `name` that lists `model`, or None."""
    if not isinstance(name, str):
        return None
    for e in eps:
        if e["name"] == name and model in e["models"]:
            return e
    return None
