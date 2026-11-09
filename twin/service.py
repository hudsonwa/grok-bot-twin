"""TwinService: one facade over DemoEngine (demo pairs) and BotportEngine (real pairs).

Used by the REST API, tools, CLI, slash command, timer and awareness block. Caches status, records
metadata-only events (events.jsonl, capped) and broadcasts plugin events to an attached Desktop.
"""
from __future__ import annotations

import fnmatch
import logging
import re
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import config, importer, safe, store
from . import secrets as _secrets
from .demo import DemoEngine, unique_profile
from .engine import (EXPECTED_BOTPORT_TAG, INSTALL_HINT, UNATTESTED_TEXT, BotportEngine, Engine, EngineError,
                     _home_label, find_botport, resolve_tool, tool_problem_text, valid_tag)
from .model import (DIRECTIONS, KEEP_SIDES, MIRROR_NOTE, PLUGIN_ID, clean_waiting, clean_withheld, normalize_pair,
                    now_iso, pair_ref, parse_iso, valid_id, valid_item_id)

log = logging.getLogger(__name__)

STATUS_TTL = 5.0
SESSION_SYNC_DEBOUNCE = 30.0
ACTIVITY_DEBOUNCE = 30.0
EVENTS_CAP = 500
DEMO_ACTIONS = ("start", "remove", "grok-edit", "hermes-edit", "conflict", "secret")
DIGEST_MAX = 20


def _lst(data: Any, key: str, cap: int = 2000) -> list:
    """data[key] when it is a list (capped), else []: engine results are type-checked before use."""
    v = data.get(key) if isinstance(data, dict) else None
    return v[:cap] if isinstance(v, list) else []


def _n(v: Any) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


class PairNotFound(EngineError):
    def __init__(self, pid: str) -> None:
        super().__init__("pair_not_found", f"No twin called {safe.clean_name(pid, 64)}.")


class BadInput(EngineError):
    def __init__(self, message: str) -> None:
        super().__init__("bad_input", message)


_C1_BIDI = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]")
CONFLICT_TEXT_CAP = 64 * 1024


def _shown_text(text: str) -> str:
    """Multi-line text from the engine (conflict copies and diffs) as shown in the UI and the terminal:
    suspected secrets masked here as well (defence in depth; botport masks them first), control,
    escape and bidi characters dropped (tab and newline kept), capped."""
    text = _secrets.redact(text[: CONFLICT_TEXT_CAP * 2], "[held back: suspected secret]")
    return _C1_BIDI.sub("", text)[:CONFLICT_TEXT_CAP]


_KIND_ORDER = ["name", "description", "instructions", "skill", "skills", "routine", "routines", "connector", "connectors"]


def _kind_order(k: str) -> tuple:
    return (_KIND_ORDER.index(k) if k in _KIND_ORDER else len(_KIND_ORDER), k)


def select_engine(pair_id: Optional[str], demo: DemoEngine, botport: Optional[BotportEngine]) -> Engine:
    """Demo pairs always go to the DemoEngine; real pairs to botport (EngineError when it is missing)."""
    if pair_id and pair_id in demo.pair_ids():
        return demo
    if botport is None:
        raise EngineError("botport_missing", "botport is not installed, so real twins cannot be reached.",
                          INSTALL_HINT)
    return botport


class TwinService:
    def __init__(self) -> None:
        self.ctx: Any = None
        self.demo = DemoEngine()
        self.demo.reserved = lambda: set(self._registry())
        self._botport: Optional[BotportEngine] = None
        self._lock = threading.RLock()
        self._status: Optional[tuple[float, dict]] = None
        self.last_check_at: Optional[str] = None
        self._last_session_sync = 0.0
        self._last_activity: dict[str, float] = {}

    # ---- environment -----------------------------------------------------------------------------
    @property
    def central(self) -> Path:
        return store.central_dir()

    def profile(self) -> str:
        if self.ctx is not None:
            try:
                name = self.ctx.profile_name
                if isinstance(name, str) and name:
                    return name
            except Exception:
                pass
        try:
            from hermes_constants import get_hermes_home, profile_name_for_home
            return profile_name_for_home(get_hermes_home()) or "default"
        except Exception:
            return "default"

    def botport(self) -> Optional[BotportEngine]:
        cfg = config.settings()
        path = find_botport(cfg["botport_path"])
        if not path:
            self._botport = None
            return None
        if (self._botport is None or self._botport.path != path or self._botport.grok_app_dir != cfg["grok_app_dir"]
                or self._botport.account != cfg["grok_account"]):
            self._botport = BotportEngine(path, store.hermes_root(), store.ensure_dir(self.central / "botport-state"),
                                          cfg["grok_app_dir"], account=cfg["grok_account"])
        return self._botport

    def engine_info(self) -> dict:
        bp = self.botport()
        if bp is None:
            configured = config.get("botport_path")
            problem = ("botport_path in the plugin settings is not an executable file" if configured
                       else "botport was not found on PATH")
            return {"available": False, "version": None, "problem": problem, "install_hint": INSTALL_HINT}
        info = bp.info()
        out = {"available": bool(info.get("available")), "version": safe.clean_name(info.get("version"), 30) or None,
               "problem": safe.clean_summary(info.get("problem"), 300) or None, "install_hint": INSTALL_HINT,
               "trusted": bool(info.get("trusted")), "attested": bool(info.get("attested")),
               "allow_unattested": bool(info.get("allow_unattested"))}
        if out["trusted"] and out["allow_unattested"] and not out["attested"]:
            out["trust_warning"] = UNATTESTED_TEXT
        return out

    # ---- botport trust (explicit owner step: digest, attestation, version check) ---------------------------------
    @staticmethod
    def release_tag(tag: Any = None) -> str:
        """The botport release tag the attestation must come from: `tag` (CLI --tag / API), else the
        botport_release_tag setting, else EXPECTED_BOTPORT_TAG. Anything not like v0.2.0 is refused."""
        if tag is None or tag == "":
            cfg = config.settings()
            if cfg.get("botport_release_tag_invalid"):
                raise BadInput("the botport_release_tag setting must look like v0.2.0")
            tag = cfg.get("botport_release_tag") or EXPECTED_BOTPORT_TAG
        if not valid_tag(tag):
            raise BadInput("the botport release tag must look like v0.2.0")
        return tag

    def trust_botport(self, allow_unattested: Any = False, tag: Any = None) -> dict:
        """Trust botport. The GitHub build attestation, pinned to the expected release tag (release_tag()), must
        pass unless the owner explicitly opts out (allow_unattested=True, for a local build); the opt-out is
        recorded and shown in doctor and status, and so is the verified tag."""
        if not isinstance(allow_unattested, bool):
            raise BadInput("allow_unattested must be true or false")
        tag = self.release_tag(tag)
        bp = self.botport()
        if bp is None:
            raise EngineError("not_found", "botport was not found", INSTALL_HINT)
        out = bp.trust(allow_unattested=allow_unattested, tag=tag)
        self.invalidate()
        return self._trust_view(out)

    def attest_botport(self, tag: Any = None) -> dict:
        # an explicit tag must look like v0.2.0; none: the tag recorded at setup (else the expected one)
        tag = self.release_tag(tag) if tag not in (None, "") else None
        bp = self.botport()
        if bp is None:
            raise EngineError("not_found", "botport was not found", INSTALL_HINT)
        res = bp.verify_attestation(tag)
        tag = res.get("tag") if valid_tag(res.get("tag")) else tag
        self.invalidate()
        return {"verified": res.get("verified") is True, "problem": safe.clean_summary(res.get("problem"), 200) or None,
                "repo": safe.clean_name(res.get("repo"), 80), "workflow": safe.clean_name(res.get("workflow"), 120),
                "tag": tag}

    def trust_info(self) -> dict:
        bp = self.botport()
        if bp is None:
            return {"trusted": False, "problem": "botport was not found", "install_hint": INSTALL_HINT}
        return self._trust_view(bp.trust_status())

    @staticmethod
    def _trust_view(ts: dict) -> dict:
        att = ts.get("attestation") if isinstance(ts.get("attestation"), dict) else {}
        sha = ts.get("sha256")
        out = {"trusted": ts.get("trusted") is True,
               "sha256": sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) else None,
               "version": safe.clean_name(ts.get("version"), 30) or None,
               "trusted_at": ts.get("trusted_at") if parse_iso(ts.get("trusted_at")) else None,
               "attested": att.get("verified") is True,
               "allow_unattested": ts.get("allow_unattested") is True and att.get("verified") is not True,
               "release_tag": ts.get("release_tag") if valid_tag(ts.get("release_tag")) else None,
               "problem": safe.clean_summary(ts.get("problem"), 200) or None}
        if out["allow_unattested"]:
            out["warning"] = UNATTESTED_TEXT
        return out

    # ---- registry of real pairs ------------------------------------------------------------------
    def _registry(self) -> dict:
        reg = store.read_json(self.central / "pairs.json", {}) or {}
        pairs = reg.get("pairs") if isinstance(reg, dict) else None
        return {p["pair_id"]: p for p in pairs if isinstance(p, dict) and valid_id(p.get("pair_id"))} \
            if isinstance(pairs, list) else {}

    def _save_registry(self, reg: dict) -> None:
        with store.locked():
            store.atomic_write_json(self.central / "pairs.json", {"pairs": list(reg.values())})

    def _remember(self, pairs: list[dict], job_ids: Optional[dict] = None) -> None:
        """Record real twins in pairs.json; `job_ids` maps a pair id to the ids of the cron jobs made from its
        bot's routines (recorded at create; kept on later updates)."""
        reg = self._registry()
        changed = False
        for p in pairs:
            old = reg.get(p["pair_id"], {})
            entry = {"pair_id": p["pair_id"], "grok": {"id": p["grok"]["id"], "name": safe.clean_name(p["grok"]["name"], 100)},
                     "hermes": {"profile": p["hermes"]["profile"]},
                     "created_at": old.get("created_at") or now_iso()}
            jobs = (job_ids or {}).get(p["pair_id"])
            if jobs is None:
                jobs = old.get("job_ids")
            if isinstance(jobs, list):
                entry["job_ids"] = [j for j in jobs if valid_id(j)][:200]
            if reg.get(p["pair_id"]) != entry:
                reg[p["pair_id"]] = entry
                changed = True
        if changed:
            self._save_registry(reg)

    def pair_ids(self) -> list[str]:
        return self.demo.pair_ids() + [p for p in self._registry() if p not in self.demo.pair_ids()]

    def require_pair(self, pid: Any) -> str:
        if not valid_id(pid):
            raise BadInput("pair_id must match [A-Za-z0-9._-]{1,64}")
        if pid not in self.pair_ids():
            # a real pair created outside the plugin (botport CLI) is still reachable
            bp = self.botport()
            if bp is not None:
                try:
                    pairs = [normalize_pair(p) for p in _lst(bp.status(pid), "pairs")]
                    pairs = [p for p in pairs if p]
                    if pairs:
                        self._remember(pairs)
                        return pid
                except EngineError:
                    pass
            raise PairNotFound(pid)
        return pid

    def engine_for(self, pid: str) -> Engine:
        return select_engine(pid, self.demo, self.botport())

    def mode(self, real_pairs: Optional[int] = None, botport_ok: Optional[bool] = None) -> str:
        real = len([p for p in self._registry() if p not in self.demo.pair_ids()]) if real_pairs is None else real_pairs
        bp = (self.botport() is not None) if botport_ok is None else botport_ok
        if real:
            return "botport"
        if self.demo.pair_ids():
            return "demo"
        return "botport" if bp else "setup"

    # ---- events ----------------------------------------------------------------------------------
    def emit(self, etype: str, pair_id: Optional[str], payload: Optional[dict] = None) -> None:
        payload = safe.clean_obj(payload or {}, 200)
        ev = {"at": now_iso(), "type": etype, "pair_id": pair_id, "payload": payload}
        try:
            store.append_jsonl(self.central / "events.jsonl", ev, cap=EVENTS_CAP)
        except OSError as exc:
            log.debug("grok-bot-twin: could not record event: %s", exc)
        if "tui_gateway.server" not in sys.modules:
            return  # not inside the gateway (CLI, tests): no Desktop to tell; the desktop also polls /events
        try:
            from hermes_cli.plugin_events import broadcast_plugin_event
            broadcast_plugin_event(PLUGIN_ID, etype, {"pair_id": pair_id, **payload})
        except Exception:
            pass

    def events(self, since: Optional[str] = None, limit: int = 100) -> dict:
        evs = store.read_jsonl(self.central / "events.jsonl")
        if since:
            cut = parse_iso(since)
            if cut is None:
                raise BadInput("since must be an ISO-8601 time")
            evs = [e for e in evs if (parse_iso(e.get("at")) or cut) > cut]
        return {"events": evs[-max(1, min(limit, 500)):]}

    # ---- status ----------------------------------------------------------------------------------
    def invalidate(self) -> None:
        self._status = None

    def status(self, fresh: bool = False, allow_engine: bool = True) -> dict:
        with self._lock:
            if not fresh and self._status and time.monotonic() - self._status[0] < STATUS_TTL:
                return self._status[1]
            pairs: list[dict] = []
            for raw in self._demo_status():
                pairs.append(raw)
            reg = self._registry()
            real_ids = [p for p in reg if p not in self.demo.pair_ids()]
            bp = self.botport()
            if bp is not None and allow_engine:
                engine = self.engine_info()
                try:
                    got = [normalize_pair(p) for p in _lst(bp.status(None), "pairs")]
                    got = [p for p in got if p and p["pair_id"] not in self.demo.pair_ids()]
                    for p in got:
                        if not p["waiting"]:  # botport's status has counts only; offer the items it listed last
                            p["waiting"] = self._waiting_for(p["pair_id"])
                    self._remember(got)
                    pairs.extend(got)
                    seen = {p["pair_id"] for p in got}
                    pairs.extend(self._error_pair(reg[pid], "Not known to botport") for pid in real_ids if pid not in seen)
                except EngineError as exc:
                    pairs.extend(self._error_pair(reg[pid], exc.message) for pid in real_ids)
            else:
                engine = self.engine_info() if allow_engine else {"available": bp is not None, "version": None,
                                                                  "problem": None, "install_hint": INSTALL_HINT}
                cached = {p["pair_id"]: p for p in (self.cached_status(compute=False) or {}).get("pairs", [])}
                for pid in real_ids:
                    if not allow_engine and pid in cached:
                        pairs.append(cached[pid])
                    else:
                        pairs.append(self._error_pair(reg[pid], "botport is not installed"))
            for p in pairs:
                p["grok"]["name"] = safe.clean_name(p["grok"]["name"], 100)
                p["state_reason"] = safe.clean_summary(p.get("state_reason"), 120)
                p["ref"] = pair_ref(p["pair_id"])
                p["digest"] = self.digest_settings(p["pair_id"])
            out = {"mode": self.mode(len(real_ids), bp is not None), "engine": engine, "pairs": pairs,
                   "last_check_at": self.last_check_at, "interval_s": config.get("sync_interval_seconds"),
                   "profile": self.profile()}
            self._status = (time.monotonic(), out)
            try:
                store.atomic_write_json(self.central / "status-cache.json", {"at": now_iso(), "status": out})
            except OSError:
                pass
            return out

    def _demo_status(self) -> list[dict]:
        try:
            return [p for p in (normalize_pair(r, demo=True) for r in _lst(self.demo.status(), "pairs")) if p]
        except EngineError as exc:
            log.warning("grok-bot-twin: demo status failed: %s", exc.message)
            return []

    @staticmethod
    def _error_pair(entry: dict, reason: str) -> dict:
        p = normalize_pair({**entry, "state": "error", "state_reason": reason}) or {}
        return p

    def cached_status(self, compute: bool = True) -> Optional[dict]:
        """Status without calling botport (for the per-turn hook): memory, then disk, then a demo-only build."""
        if self._status:
            return self._status[1]
        disk = store.read_json(self.central / "status-cache.json", None)
        if isinstance(disk, dict) and isinstance(disk.get("status"), dict):
            st = disk["status"]
            pairs = []
            for raw in (st.get("pairs") if isinstance(st.get("pairs"), list) else [])[:200]:
                p = normalize_pair(raw)  # the file is ours, but only allowlisted fields are ever handed on
                if p:
                    p["ref"] = pair_ref(p["pair_id"])
                    p["digest"] = self.digest_settings(p["pair_id"])
                    pairs.append(p)
            return {"mode": st.get("mode") if st.get("mode") in ("botport", "demo", "setup") else "setup",
                    "engine": {"available": False, "version": None, "problem": None, "install_hint": INSTALL_HINT},
                    "pairs": pairs, "last_check_at": st.get("last_check_at") if parse_iso(st.get("last_check_at")) else None,
                    "interval_s": config.get("sync_interval_seconds"), "profile": self.profile()}
        if not compute:
            return None
        return self.status(allow_engine=False)

    def pair_status(self, pid: str) -> dict:
        pid = self.require_pair(pid)
        for p in self.status(fresh=True)["pairs"]:
            if p["pair_id"] == pid:
                return p
        raise PairNotFound(pid)

    # ---- sync / check ----------------------------------------------------------------------------
    def check_changed(self) -> list[str]:
        changed: list[str] = []
        if self.demo.exists():
            changed += [c for c in _lst(self.demo.check(), "changed") if valid_id(c)]
        bp = self.botport()
        real = [p for p in self._registry() if p not in self.demo.pair_ids()]
        if bp is not None and real:
            changed += [c for c in _lst(bp.check(), "changed") if valid_id(c)]
        self.last_check_at = now_iso()
        if changed:
            self.emit("changed", None, {"pairs": changed[:20], "count": len(changed)})
        return changed

    def sync(self, pair: Optional[str] = None) -> dict:
        """Sync now (Grok -> Hermes plus the engine's ownership merge). One pair or all."""
        results: list[dict] = []
        if pair:
            pid = self.require_pair(pair)
            results += _lst(self.engine_for(pid).sync(pid), "pairs")
        else:
            if self.demo.pair_ids():
                results += _lst(self.demo.sync(None), "pairs")
            real = [p for p in self._registry() if p not in self.demo.pair_ids()]
            bp = self.botport()
            if real and bp is not None:
                try:
                    results += _lst(bp.sync(None), "pairs")
                except EngineError as exc:
                    # botport could not run at all: report it on each real twin, keep the demo results
                    results += [{"pair_id": pid, "state": "error", "error": exc.as_dict()} for pid in real]
        out = [self._clean_sync(r) for r in results if isinstance(r, dict) and valid_id(r.get("pair_id"))]
        self._note_waiting(out)
        self.invalidate()
        self._emit_sync(out)
        return {"pairs": out}

    # ---- waiting items (botport lists them in sync/create/approve results, not in status) -----------
    def _note_waiting(self, entries: list[dict]) -> None:
        """Remember each real twin's latest waiting[] so status, the CLI and the Desktop can offer Approve."""
        demo = set(self.demo.pair_ids())
        todo = {e["pair_id"]: e.get("waiting") or [] for e in entries
                if e.get("pair_id") not in demo and e.get("state") != "error" and not e.get("dry_run")}
        if not todo:
            return
        with store.locked():
            data = store.read_json(self.central / "waiting.json", {}) or {}
            data = data if isinstance(data, dict) else {}
            for pid, items in todo.items():
                if items:
                    data[pid] = items[:50]
                else:
                    data.pop(pid, None)
            store.atomic_write_json(self.central / "waiting.json", data)

    def _waiting_for(self, pid: str) -> list[dict]:
        data = store.read_json(self.central / "waiting.json", {}) or {}
        items = data.get(pid) if isinstance(data, dict) else None
        return clean_waiting(items)

    @staticmethod
    def _clean_sync(r: dict) -> dict:
        def lst(key):
            v = r.get(key)
            return [x for x in v if isinstance(x, (dict, str))][:200] if isinstance(v, list) else []
        applied = [{"item": safe.clean_name(a.get("item"), 140), "kind": safe.clean_name(a.get("kind"), 30),
                    "name": safe.clean_name(a.get("name"), 100),
                    "direction": a.get("direction") if a.get("direction") in DIRECTIONS else "g2h"}
                   for a in lst("applied") if isinstance(a, dict)]
        withheld = clean_withheld(lst("secrets_withheld"))
        waiting = clean_waiting(lst("waiting"))
        out_err = None
        if r.get("state") == "error":  # one twin failed (sync --all reports it in place; the others went on)
            err = r.get("error") if isinstance(r.get("error"), dict) else {}
            code = safe.clean_name(err.get("code"), 40) or "internal"
            msg = safe.clean_summary(err.get("message"), 300) or "botport reported an error"
            if code == "account_choice":
                from .engine import account_choice_error
                msg = account_choice_error(err.get("message")).message
            out_err = {"code": code, "message": msg}
        return {"pair_id": r["pair_id"], "applied": applied, "secrets_withheld": withheld, "error": out_err,
                "dry_run": r.get("dry_run") is True,
                "conflicts": [safe.clean_name(c, 140) for c in lst("conflicts") if isinstance(c, str)],
                "waiting": waiting, "to_grok": _n(r.get("to_grok")), "wall_ms": _n(r.get("wall_ms")),
                "restore_point": r.get("restore_point") if valid_item_id(r.get("restore_point")) else None,
                "state": r.get("state") if r.get("state") in ("in_step", "changes_waiting", "needs_you", "grok_missing",
                                                              "error") else "in_step"}

    def _emit_sync(self, results: list[dict]) -> None:
        names = {}
        try:
            names = {p["pair_id"]: p["grok"]["name"] for p in (self.cached_status(compute=False) or {}).get("pairs", [])}
        except Exception:
            pass
        for r in results:
            pid = r["pair_id"]
            if r["applied"]:
                kinds = sorted({a["kind"] for a in r["applied"]}, key=_kind_order)
                g2h = any(a.get("direction") == "g2h" for a in r["applied"])
                name = names.get(pid, pid)
                self.emit("synced", pid, {"name": name, "kinds": kinds, "count": len(r["applied"]),
                                          "applied": [{"item": a["item"], "kind": a["kind"], "direction": a["direction"]}
                                                      for a in r["applied"][:20]],
                                          "message": f"{name}: {' & '.join(kinds)} updated from "
                                                     f"{'Grok' if g2h else 'Hermes'}"})
            if r["secrets_withheld"]:
                n = len({s["item"] for s in r["secrets_withheld"]})
                self.emit("secrets_withheld", pid, {"count": n, "message": _secrets.held_back_text(n),
                                                    "note": _secrets.NOTE,
                                                    "secrets_withheld": r["secrets_withheld"][:20],
                                                    "items": sorted({s['item'] for s in r['secrets_withheld']})})
            if r["conflicts"]:
                self.emit("conflict", pid, {"count": len(r["conflicts"]), "items": r["conflicts"][:10]})
            if r.get("error"):
                self.emit("sync_error", pid, {"code": r["error"]["code"], "message": r["error"]["message"]})

    def tick(self) -> int:
        """One timer cycle: metadata check, then a Grok->Hermes sync of each changed pair."""
        changed = self.check_changed()
        for pid in changed:
            try:
                self.sync(pid)
            except EngineError as exc:
                if exc.code == "locked":
                    continue  # another botport run holds the pair; try next tick
                raise
        if changed:
            try:
                self.status(fresh=True)
            except EngineError:
                pass
            from . import awareness
            awareness.invalidate()
        return len(changed)

    # ---- grok roster / create --------------------------------------------------------------------
    def grok_list(self) -> dict:
        globs = config.get("bots") or []
        bots: list[dict] = []
        app_found = False
        warnings: list = []
        bp = self.botport()
        demo_on = self.demo.exists()
        # While the demo runs, real Grok bots are listed only when the owner set a `bots` filter, so a demo
        # (or a screen recording of it) never shows real bot names by accident.
        if bp is not None and (globs or not demo_on):
            try:
                data = bp.grok_list(globs)
                app_found = bool(data.get("app_found"))
                warnings = _lst(data, "_warnings")
                bots += [dict(b, demo=False) for b in _lst(data, "bots") if isinstance(b, dict)]
            except EngineError as exc:
                if not demo_on:
                    raise
                warnings = [{"code": exc.code, "message": exc.message}]
        if demo_on:
            d = self.demo.grok_list(globs)
            demo_bots = [dict(b, demo=True) for b in _lst(d, "bots")]
            bots += demo_bots
            # the demo's pretend Grok app is "found" whenever it lists bots, botport on PATH or not
            app_found = app_found or bp is None or bool(demo_bots)
        out = []
        for b in bots:
            if not valid_id(b.get("grok_id")):
                continue
            name = safe.clean_name(b.get("name"), 100)
            if globs and not any(fnmatch.fnmatch(name.lower(), g.lower()) for g in globs):
                continue
            out.append({"grok_id": b["grok_id"], "name": name,
                        "description": safe.clean_summary(b.get("description"), 200),
                        "updated_at": b.get("updated_at") if parse_iso(b.get("updated_at")) else None,
                        "twinned_pair_id": b.get("twinned_pair_id") if valid_id(b.get("twinned_pair_id")) else None,
                        "routines": [safe.clean_name(r, 100) for r in (b.get("routines") or [])[:30]
                                     if isinstance(r, str)] if isinstance(b.get("routines"), list) else [],
                        "demo": bool(b.get("demo"))})
        res = {"app_found": app_found, "bots": out}
        warnings = [{"code": safe.clean_name(w.get("code"), 40), "message": safe.clean_summary(w.get("message"), 200)}
                    for w in warnings[:20] if isinstance(w, dict)]
        if warnings:
            res["warnings"] = warnings
        return res

    def _route_grok(self, gid: str) -> Engine:
        if self.demo.exists() and self.demo._grok(gid) is not None:
            return self.demo
        bp = self.botport()
        if bp is None:
            raise EngineError("botport_missing", "botport is needed to twin a real Grok bot.", INSTALL_HINT)
        return bp

    def _check_create_input(self, grok_ids: Any, profiles: Any) -> tuple[list[str], dict]:
        if not isinstance(grok_ids, list) or not grok_ids or len(grok_ids) > 50 or not all(valid_id(g) for g in grok_ids):
            raise BadInput("grok_ids must be a non-empty list of ids")
        profiles = profiles or {}
        if not isinstance(profiles, dict) or not all(valid_id(k) and valid_id(v) for k, v in profiles.items()):
            raise BadInput("profiles must map grok_id to a profile name")
        return list(dict.fromkeys(grok_ids)), profiles

    def profiles_in_use(self) -> dict:
        """Hermes profile -> pair id, for every twin (real and demo): one profile backs at most one twin."""
        used = {}
        for pid, e in self._registry().items():
            prof = (e.get("hermes") or {}).get("profile") if isinstance(e.get("hermes"), dict) else None
            if valid_id(prof):
                used[prof] = pid
        used.update({k: v for k, v in self.demo.profiles().items() if valid_id(k)})
        return used

    def _plan_profiles(self, ids: list[str], profiles: dict) -> tuple[dict, list[dict], dict, dict]:
        """Pick each bot's Hermes profile: the one asked for, else a slug of the bot's name that backs no twin.
        A profile that already backs another twin (or one picked twice in this batch) is refused, as botport's
        profile_already_paired does, so two bots never end up in one profile. Returns the chosen profiles, the
        refused bots, each bot's engine, and botport dry runs already made (reused by the preview)."""
        used = self.profiles_in_use()
        twinned = {(e.get("grok") or {}).get("id"): pid for pid, e in self._registry().items()
                   if isinstance(e.get("grok"), dict)}
        twinned.update({v: k for k, v in self.demo.twinned().items()})
        chosen: dict = {}
        skipped: list[dict] = []
        engines: dict = {}
        dry: dict = {}
        for gid in ids:
            eng = engines[gid] = self._route_grok(gid)
            prof = profiles.get(gid)
            if not prof:
                if eng is self.demo:
                    prof = unique_profile((self.demo._grok(gid) or {}).get("name") or gid,
                                          set(used) | set(chosen.values()))
                else:
                    # botport picks the slug of the bot's name; its dry run says which (the Grok roster is not
                    # read again here). botport refuses its own paired profiles; demo ones are checked below.
                    prof, dry[gid] = self._botport_default_profile(eng, gid)
            owner = used.get(prof) if prof else None
            if (owner is not None and owner != twinned.get(gid)) or (prof and prof in chosen.values()):
                skipped.append({"grok_id": gid, "reason": "profile_already_paired",
                                **({"pair_id": owner} if valid_id(owner) else {})})
                continue
            chosen[gid] = prof
        return chosen, skipped, engines, dry

    @staticmethod
    def _botport_default_profile(eng: Engine, gid: str) -> tuple[Optional[str], Optional[dict]]:
        try:
            data = eng.create(gid, None, dry_run=True)
        except EngineError:
            return None, None  # the real call reports the problem
        for p in _lst(data, "pairs"):
            prof = (p.get("hermes") or {}).get("profile") if isinstance(p, dict) and isinstance(p.get("hermes"), dict) else None
            if valid_id(prof):
                return prof, data
        return None, data

    def preview(self, grok_ids: Any, profiles: Any = None) -> dict:
        ids, profiles = self._check_create_input(grok_ids, profiles)
        chosen, skipped, engines, dry = self._plan_profiles(ids, profiles)
        pairs = []
        for gid in ids:
            if gid not in chosen:
                continue
            eng = engines[gid]
            data = dry.get(gid) or eng.create(gid, chosen[gid], dry_run=True)
            # a bot botport will not twin (already_twinned, profile_already_paired): the Desktop lists it as skipped
            for k in _lst(data, "skipped", 50):
                if isinstance(k, dict) and valid_id(k.get("grok_id")):
                    skipped.append({"grok_id": k["grok_id"], "reason": safe.clean_name(k.get("reason"), 40) or "skipped",
                                    **({"pair_id": k["pair_id"]} if valid_id(k.get("pair_id")) else {})})
            for p in _lst(data, "pairs"):
                if not isinstance(p, dict):
                    continue
                items = []
                for it in (p.get("items") if isinstance(p.get("items"), list) else [])[:200]:
                    if isinstance(it, dict):
                        prev = it.get("preview")
                        items.append({"item": safe.clean_name(it.get("item"), 140), "kind": safe.clean_name(it.get("kind"), 30),
                                      "name": safe.clean_name(it.get("name"), 100), "action": safe.clean_name(it.get("action"), 20),
                                      "owner": safe.clean_name(it.get("owner"), 10),
                                      "scan": it.get("scan") if it.get("scan") in ("safe", "caution", "dangerous") else None,
                                      # what the twin will hold (botport masks suspected secrets first; masked here too)
                                      **({"preview": _shown_text(prev[:8000])[:4000]} if isinstance(prev, str) else {})})
                entry = {"grok": {"id": gid, "name": safe.clean_name((p.get("grok") or {}).get("name"), 100)},
                         "hermes": {"profile": safe.clean_name((p.get("hermes") or {}).get("profile"), 64)},
                         "items": items, "secrets_withheld": self._clean_sync(p)["secrets_withheld"],
                         "schedules_paused": _n(p.get("schedules_paused")), "demo": eng is self.demo}
                pairs.append(entry)
        # the Desktop warns inline when a profile typed in the preview already backs a twin
        return {"pairs": pairs, "skipped": skipped, "profiles_in_use": sorted(self.profiles_in_use())[:200]}

    def create(self, grok_ids: Any, profiles: Any = None) -> dict:
        ids, profiles = self._check_create_input(grok_ids, profiles)
        chosen, skipped, engines, _dry = self._plan_profiles(ids, profiles)
        if skipped and not chosen:
            raise EngineError("profile_already_paired", "That Hermes profile already backs another twin. "
                                                        "Pick a new profile name.")
        created: list[str] = []
        notes: list[str] = []
        for gid in ids:
            if gid not in chosen:
                continue
            eng = engines[gid]
            data = eng.create(gid, chosen[gid], dry_run=False)
            for k in _lst(data, "skipped", 50):
                if isinstance(k, dict) and valid_id(k.get("grok_id")):
                    skipped.append({"grok_id": k["grok_id"], "reason": safe.clean_name(k.get("reason"), 40) or "skipped"})
            for p in (data.get("pairs") if isinstance(data.get("pairs"), list) else [])[:50]:
                if isinstance(p, dict) and valid_id(p.get("pair_id")):
                    created.append(p["pair_id"])
                    if eng is not self.demo:
                        np_ = normalize_pair(p)
                        if np_:
                            # the cron jobs made from the bot's routines, by their stable job ids (never by name)
                            self._remember([np_], {np_["pair_id"]: importer.imported_job_ids(
                                self._profile_home(np_["hermes"]["profile"]))})
                            # routines and connectors stay waiting for their own approval after create
                            self._note_waiting([self._clean_sync(p)])
        self.invalidate()
        st = self.status(fresh=True)
        out = {"pairs": [p for p in st["pairs"] if p["pair_id"] in created]}
        if notes:
            out["notes"] = notes
        if skipped:
            out["skipped"] = skipped
        for pid in created:
            self.emit("created", pid, {})
        return out

    # ---- per-pair actions ------------------------------------------------------------------------
    def conflicts(self, pid: str) -> dict:
        pid = self.require_pair(pid)
        data = self.engine_for(pid).conflicts(pid)
        out = []
        for c in (data.get("conflicts") if isinstance(data.get("conflicts"), list) else [])[:100]:
            if not isinstance(c, dict) or not valid_item_id(c.get("conflict_id")):
                continue
            def side(s: str) -> dict:
                sd = c.get(s) if isinstance(c.get(s), dict) else {}
                text = sd.get("text")
                return {"digest": safe.clean_name(sd.get("digest"), 64),
                        "text": _shown_text(text) if isinstance(text, str) else None}
            out.append({"conflict_id": c["conflict_id"], "kind": safe.clean_name(c.get("kind"), 30),
                        "name": safe.clean_name(c.get("name"), 100),
                        "since": c.get("since") if parse_iso(c.get("since")) else None,
                        "hermes": side("hermes"), "grok": side("grok"),
                        "diff": _shown_text(c.get("diff")) if isinstance(c.get("diff"), str) else "",
                        "text_truncated": c.get("text_truncated") is True})
        return {"conflicts": out}

    def resolve(self, pid: str, conflict_id: Any, keep: Any) -> dict:
        pid = self.require_pair(pid)
        if not valid_item_id(conflict_id):
            raise BadInput("conflict_id is not valid")
        if keep not in KEEP_SIDES:
            raise BadInput("keep must be hermes or grok")
        data = self.engine_for(pid).resolve(pid, conflict_id, keep)
        self.invalidate()
        self.emit("resolved", pid, {"conflict_id": conflict_id, "keep": keep})
        applied = data.get("applied") if isinstance(data.get("applied"), list) else []
        return {"applied": [{"item": a["item"], "keep": a.get("keep") if a.get("keep") in KEEP_SIDES else keep}
                            for a in applied[:50] if isinstance(a, dict) and valid_item_id(a.get("item"))],
                "restore_point": data.get("restore_point") if valid_item_id(data.get("restore_point")) else None}

    def restore_points(self, pid: str) -> dict:
        pid = self.require_pair(pid)
        pts = self.engine_for(pid).restore_points(pid).get("points")
        pts = pts if isinstance(pts, list) else []
        return {"points": [{"id": p["id"], "item": safe.clean_name(p.get("item"), 140),
                            "kind": safe.clean_name(p.get("kind"), 30), "name": safe.clean_name(p.get("name"), 100),
                            "at": p.get("at") if parse_iso(p.get("at")) else None,
                            "reason": safe.clean_summary(p.get("reason"), 120),
                            "scope": p.get("scope") if p.get("scope") in ("item", "profile") else "item",
                            **({"items": _n(p.get("items"))} if isinstance(p.get("items"), int) else {})}
                           for p in pts[:200] if isinstance(p, dict) and valid_item_id(p.get("id"))]}

    def restore(self, pid: str, point_id: Any) -> dict:
        pid = self.require_pair(pid)
        if not valid_item_id(point_id):
            raise BadInput("point_id is not valid")
        data = self.engine_for(pid).restore(pid, point_id)
        self.invalidate()
        restored = [safe.clean_name(x, 140) for x in (data.get("restored") if isinstance(data.get("restored"), list)
                                                       else [])[:100] if isinstance(x, str)]
        self.emit("restored", pid, {"count": len(restored), "items": restored[:20]})
        out = {"restored": restored,
               "restore_point": data.get("restore_point") if valid_item_id(data.get("restore_point")) else None}
        if not restored:
            out["note"] = safe.clean_summary(data.get("note"), 200) or "Nothing to restore: Hermes already has that copy."
        else:
            # a restored Hermes copy that differs from Grok now waits for the owner's OK before it goes to Grok
            try:
                waiting = {w["item"] for w in self.pair_status(pid).get("waiting", [])}
            except (EngineError, PairNotFound):
                waiting = set()
            out["waiting_for_ok"] = [i for i in restored if i in waiting]
        return out

    def recent(self, pid: str, limit: int = 10) -> dict:
        pid = self.require_pair(pid)
        limit = max(1, min(int(limit), 50))
        data = self.engine_for(pid).recent(pid, limit)
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        return {"entries": [{"at": e.get("at") if parse_iso(e.get("at")) else None,
                             "side": e.get("side") if e.get("side") in ("grok", "hermes", "both") else "hermes",
                             "kind": safe.clean_name(e.get("kind"), 30), "summary": safe.clean_summary(e.get("summary"))}
                            for e in entries[:limit] if isinstance(e, dict)]}

    def parity(self, pid: str) -> dict:
        pid = self.require_pair(pid)
        eng = self.engine_for(pid)
        if eng is not self.demo:
            info = eng.info() if hasattr(eng, "info") else {}
            if "twin-parity" not in (info.get("features") or []):
                return {"passed": None, "total": None, "checks": [], "note": "This botport has no parity battery yet."}
        data = eng.parity(pid)
        raw = data.get("checks") if isinstance(data.get("checks"), list) else []
        checks = [{"id": safe.clean_name(c.get("id"), 140), "name": safe.clean_name(c.get("name"), 100),
                   "pass": c.get("pass") is True, "detail": safe.clean_summary(c.get("detail"), 200)}
                  for c in raw[:200] if isinstance(c, dict)]
        passed, total = data.get("passed"), data.get("total")
        return {"passed": _n(passed) if isinstance(passed, int) else None,
                "total": _n(total) if isinstance(total, int) else None, "checks": checks}

    def checklist(self, pid: str) -> dict:
        pid = self.require_pair(pid)
        if pid in self.demo.pair_ids():
            return self.demo.checklist(pid)
        st = self.pair_status(pid)
        routines = [{"name": b.get("name")} for b in st["by_hand"] if str(b.get("item", "")).startswith("routine:")]
        # the twin's jobs by stable id: those recorded at create, plus any job an importer marked as made from
        # the bot's routines (never matched by a "<bot name>: " prefix)
        recorded = self._registry().get(pid, {}).get("job_ids")
        jobs = importer.paused_jobs_for(self._profile_home(st["hermes"]["profile"]),
                                        recorded if isinstance(recorded, list) else None)
        return {"by_hand": st["by_hand"], "switch": {"grok_routines_to_turn_off": routines, "hermes_jobs_paused": jobs}}

    @staticmethod
    def _profile_home(profile: Any) -> Optional[Path]:
        try:
            return store.profile_home(profile)
        except (ValueError, TypeError):  # an engine-provided profile name that is not a plain id is never a path
            return None

    def switch(self, pid: str, job_ids: Any) -> dict:
        pid = self.require_pair(pid)
        if not isinstance(job_ids, list) or len(job_ids) > 100 or not all(valid_id(j) for j in job_ids):
            raise BadInput("job_ids must be a list of job ids")
        if pid in self.demo.pair_ids():
            got = self.demo.switch(pid, job_ids)
            res = {"resumed": [j for j in (got.get("resumed") or [])[:100] if valid_id(j)],
                   "failed": [{"job_id": f["job_id"], "error": safe.clean_summary(f.get("error"), 160)}
                              for f in (got.get("failed") or [])[:100]
                              if isinstance(f, dict) and valid_id(f.get("job_id"))]}
        else:
            allowed = {j["job_id"] for j in self.checklist(pid)["switch"]["hermes_jobs_paused"]}
            prof = self.pair_status(pid)["hermes"]["profile"]
            if not valid_id(prof) or prof.startswith("-"):
                raise EngineError("bad_profile", "This twin's Hermes profile name is not valid.")
            res = {"resumed": [], "failed": []}
            stopped = False
            for jid in job_ids:
                if stopped:
                    res["failed"].append({"job_id": jid, "error": "not tried: an earlier resume was refused or failed"})
                    continue
                if jid not in allowed:
                    res["failed"].append({"job_id": jid, "error": "not a paused job of this twin"})
                    continue
                ok, err = importer.resume_cron(jid, prof, self.ctx)
                if ok:
                    res["resumed"].append(jid)
                else:  # a refusal (or failure) in Hermes' approval step stops the switch; it is never retried
                    res["failed"].append({"job_id": jid, "error": err})
                    stopped = True
        self.invalidate()
        if res["resumed"]:
            self.emit("switched", pid, {"resumed": len(res["resumed"])})
        return res

    def send_to_grok(self, pid: str) -> dict:
        """"Send to Grok": botport has no separate pack command, so this is a sync of the pair (approved
        changes then travel through the Mirror bot when it is connected). The demo's pretend Grok side
        takes them at once. The activity digest goes along only when the owner turned it on for this twin."""
        pid = self.require_pair(pid)
        eng = self.engine_for(pid)
        digest = self.digest_preview(pid) if self.digest_settings(pid)["enabled"] else None
        data = self.demo.pack(pid, digest) if eng is self.demo else eng.pack(pid)
        pairs = [self._clean_sync(r) for r in _lst(data, "pairs") if isinstance(r, dict) and valid_id(r.get("pair_id"))]
        pack = data.get("pack") if isinstance(data.get("pack"), dict) else {}
        skills = data.get("skills")
        if skills is None:
            skills = sum(1 for p in pairs for a in p["applied"] if a["kind"] == "skill" and a.get("direction") == "h2g")
        self.invalidate()
        self._emit_sync(pairs)
        self.emit("sent_to_grok", pid, {"skills": _n(skills)})
        return {"pairs": pairs, "pack": {"gen": _n(pack["gen"]) if isinstance(pack.get("gen"), int) else None,
                                         "items": _n(pack.get("items")),
                                         "written_to": safe.clean_name(pack.get("written_to"), 120) or None},
                "skills": _n(skills), "digest_entries": _n(data.get("digest_entries")),
                "note": safe.clean_summary(data.get("note"), 200) or MIRROR_NOTE}

    # ---- v1.2: approve / mirror / remove ---------------------------------------------------------
    def approve(self, pid: str, item: Any) -> dict:
        pid = self.require_pair(pid)
        if not valid_item_id(item):
            raise BadInput("item is not a valid item id")
        data = self.engine_for(pid).approve(pid, item)
        sync = data.get("sync") if isinstance(data.get("sync"), dict) else None
        sync = self._clean_sync(sync) if sync and valid_id(sync.get("pair_id")) else None
        approved = [{"item": safe.clean_name(a.get("item"), 140),
                     "to": a.get("to") if a.get("to") in ("local", "remote") else "remote"}
                    for a in (data.get("approved") if isinstance(data.get("approved"), list) else [])[:50]
                    if isinstance(a, dict)]
        if sync:
            self._note_waiting([sync])
        self.invalidate()
        if sync:
            self._emit_sync([sync])
        self.emit("approved", pid, {"count": len(approved)})
        return {"approved": approved, "sync": sync}

    def mirror(self, pid: str, pair_folder: Any = None, off: Any = False, rotate_key: Any = False) -> dict:
        pid = self.require_pair(pid)
        if not isinstance(off, bool) or not isinstance(rotate_key, bool):
            raise BadInput("off and rotate_key must be true or false")
        eng = self.engine_for(pid)
        if rotate_key:
            if off or pair_folder is not None:
                raise BadInput("rotate_key goes on its own (no pair_folder, no off)")
            if eng is self.demo:
                raise BadInput("The demo twin has no pair key.")
            data = eng.mirror(pid, None, False, rotate_key=True)
            self.invalidate()
            self.emit("mirror", pid, {"key_rotated": data.get("key_rotated") is True})
            return {"pair_id": pid, "key_rotated": data.get("key_rotated") is True,
                    "note": "A new pair key is in place. If a Mirror bot is connected, attach the new key file in "
                            "its chat (never paste it); the old key verifies nothing any more."}
        folder: Optional[str] = None
        if off:
            if pair_folder is not None:
                raise BadInput("give either pair_folder or off, not both")
        elif eng is not self.demo:
            folder = self._check_pair_folder(pair_folder)
        # (the demo has no real Mirror bot, so a folder is not used there)
        data = eng.mirror(pid, folder, off)
        steps = [{"id": safe.clean_name(s.get("id"), 40), "what": safe.clean_summary(s.get("what"), 400)}
                 for s in (data.get("steps") or [])[:10] if isinstance(s, dict)]
        charter = data.get("charter") if isinstance(data.get("charter"), str) else ""
        self.invalidate()
        self.emit("mirror", pid, {"connected": bool(data.get("connected"))})
        return {"pair_id": pid, "connected": data.get("connected") is True, "steps": steps,
                "charter": _shown_text(charter[:20000])}

    def _check_pair_folder(self, value: Any) -> str:
        """The Mirror pair folder: an existing real folder (not a link), given as an absolute path, outside
        the Hermes home and the plugin data folder."""
        if not isinstance(value, str) or not value.strip() or len(value) > 1024 or "\x00" in value:
            raise BadInput("pair_folder must be the absolute path of a folder your sync app keeps in step")
        raw = Path(value.strip()).expanduser()
        if not raw.is_absolute():
            raise BadInput("pair_folder must be an absolute path")
        if raw.is_symlink() or not raw.is_dir():
            raise BadInput("pair_folder must be an existing folder (not a link)")
        for part in [raw, *raw.parents]:  # botport refuses a link on the way that root does not own
            try:
                st = part.lstat()
            except OSError:
                raise BadInput("pair_folder must be an existing folder")
            if stat.S_ISLNK(st.st_mode) and st.st_uid != 0:
                raise BadInput("pair_folder must not go through a link; give the folder's real path")
        real = raw.resolve()
        for base in (store.hermes_root(), self.central, self.central / "botport-state"):
            if store.contained(base, real) or store.contained(real, base):
                raise BadInput("pair_folder must sit outside the Hermes home, the plugin data folder and "
                               "botport's state folder")
        return str(real)

    def remove(self, pid: str, dry_run: Any = False) -> dict:
        pid = self.require_pair(pid)
        if not isinstance(dry_run, bool):
            raise BadInput("dry_run must be true or false")
        data = self.engine_for(pid).remove(pid, dry_run)
        removed = [safe.clean_name(x, 200) for x in (data.get("removed") if isinstance(data.get("removed"), list)
                                                      else [])[:100] if isinstance(x, str)]
        if not dry_run:
            reg = self._registry()
            if reg.pop(pid, None) is not None:
                self._save_registry(reg)
            with store.locked():
                ds = store.read_json(self.central / "digest.json", {}) or {}
                if isinstance(ds, dict) and ds.pop(pid, None) is not None:
                    store.atomic_write_json(self.central / "digest.json", ds)
            self.invalidate()
            self.emit("removed", pid, {"count": len(removed)})
        return {"pair_id": pid, "dry_run": dry_run, "removed": removed, "profile_kept": True}

    # ---- activity digest to Grok: OFF by default per twin, opt-in by the owner ------------------------------
    def digest_settings(self, pid: str) -> dict:
        ds = store.read_json(self.central / "digest.json", {}) or {}
        e = ds.get(pid) if isinstance(ds, dict) and isinstance(ds.get(pid), dict) else {}
        allowed = bool(config.get("digest_to_grok"))
        enabled = allowed and e.get("enabled") is True
        return {"enabled": enabled, "titles": enabled and e.get("titles") is True and bool(config.get("digest_titles")),
                "allowed": allowed}

    def set_digest(self, pid: str, enabled: Any, titles: Any = False) -> dict:
        pid = self.require_pair(pid)
        if not isinstance(enabled, bool) or not isinstance(titles, bool):
            raise BadInput("enabled and titles must be true or false")
        if enabled and not config.get("digest_to_grok"):
            raise BadInput("Turn on digest_to_grok in the plugin settings first")
        if titles and not config.get("digest_titles"):
            raise BadInput("Turn on digest_titles in the plugin settings first")
        with store.locked():
            ds = store.read_json(self.central / "digest.json", {}) or {}
            ds = ds if isinstance(ds, dict) else {}
            ds[pid] = {"enabled": enabled, "titles": bool(enabled and titles)}
            store.atomic_write_json(self.central / "digest.json", ds)
        self.invalidate()
        return {"pair_id": pid, **self.digest_settings(pid), "preview": self.digest_preview(pid)}

    def digest_preview(self, pid: str) -> dict:
        """Exactly what would be sent: counts and times; a short title only with its own opt-in.
        Never message content; titles are capped and suspected secrets masked."""
        pid = self.require_pair(pid)
        st = self.digest_settings(pid)
        out = []
        for e in self.recent(pid, DIGEST_MAX)["entries"]:
            row = {"at": e["at"], "side": e["side"], "kind": e["kind"]}
            if st["titles"]:
                row["title"] = safe.clean_summary(e.get("summary"), 60)
            out.append(row)
        counts: dict[str, int] = {}
        for e in out:
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        return {"enabled": st["enabled"], "titles": st["titles"], "counts": counts, "entries": out}

    # ---- demo ------------------------------------------------------------------------------------
    def demo_action(self, action: Any, profile: Any = None) -> dict:
        if action not in DEMO_ACTIONS:
            raise BadInput("action must be one of: " + ", ".join(DEMO_ACTIONS))
        if profile is not None and not valid_id(profile):
            raise BadInput("profile is not a valid profile name")
        did = ""
        if action == "start":
            prof = profile or config.get("demo_profile") or self.profile()
            res = self.demo.start(prof)
            did = "Demo twin of Acme Helper is ready" if res["created"] else "The demo is already running"
            self.emit("demo", None, {"action": "start"})
        elif action == "remove":
            gone = self.demo.remove()
            did = "Demo removed" if gone else "No demo to remove"
            self.emit("demo", None, {"action": "remove"})
        else:
            if not self.demo.exists():
                raise EngineError("demo_missing", "Start the demo first: hermes twin demo")
            did = self.demo.simulate(action)["did"]
            self.emit("demo", None, {"action": action})
        self.invalidate()
        st = self.status(fresh=True, allow_engine=False)
        return {"ok": True, "did": did, "pairs": [p for p in st["pairs"] if p.get("demo")]}

    # ---- doctor ----------------------------------------------------------------------------------
    def doctor(self, probe_importers: bool = True) -> dict:
        checks = []
        info = self.engine_info()
        bp = self.botport()
        if bp is None or info["available"]:
            checks.append({"id": "botport", "ok": bool(info["available"]),
                           "message": f"botport {info['version']} found" if info["available"]
                           else (info["problem"] or "botport missing"),
                           "fix": "" if info["available"] else INSTALL_HINT})
        if bp is not None:  # botport's own checks, incl. trust (SHA-256) and attestation
            try:
                for c in _lst(bp.doctor(), "checks")[:30]:
                    if isinstance(c, dict):
                        checks.append({"id": safe.clean_name(c.get("id"), 60), "ok": bool(c.get("ok")),
                                       "message": safe.clean_summary(c.get("message"), 200),
                                       "fix": safe.clean_summary(c.get("fix"), 200)})
            except EngineError as exc:
                checks.append({"id": "botport_doctor", "ok": False, "message": exc.message, "fix": exc.hint})
        try:
            probe = self.central / ".write-test"
            store.atomic_write_text(probe, "ok")
            probe.unlink()
            checks.append({"id": "data_dir", "ok": True, "message": "Plugin data folder is writable", "fix": ""})
        except OSError:
            checks.append({"id": "data_dir", "ok": False, "message": "Plugin data folder is not writable",
                           "fix": "Check permissions of plugin-data/grok-bot-twin in your Hermes home"})
        checks += self._tool_checks()
        checks.append({"id": "demo", "ok": True, "message": "Demo running" if self.demo.exists() else "Demo not running",
                       "fix": "" if self.demo.exists() else "Try it: hermes twin demo"})
        from . import timer
        checks.append({"id": "timer", "ok": True, "message": timer.describe(), "fix": ""})
        if not [p for p in self._registry() if p not in self.demo.pair_ids()]:
            # No real twin yet: botport is only needed for real twins, so its checks are advice, not failures.
            for c in checks:
                if c["id"] != "data_dir" and not c["ok"]:
                    c["optional"] = True
        return {"checks": checks}

    @staticmethod
    def _tool_checks() -> list[dict]:
        """gh and hermes, the two other programs the plugin may run: their absolute real paths and the result of
        the same ownership/permission check as botport's (a failing one is refused when it would run)."""
        out = []
        gh, problem = resolve_tool("gh")
        out.append({"id": "gh_path", "ok": gh is not None and not problem,
                    "message": (f"gh: {_home_label(gh)} (owner and permissions checked: ok)" if gh and not problem
                                else tool_problem_text("gh", gh, problem)),
                    "fix": "" if gh and not problem else
                    ("Install the GitHub CLI (gh): `hermes twin setup` needs it to verify botport's attestation"
                     if gh is None else "Reinstall gh where only you (or root) can change it")})
        chk = importer.hermes_check()
        path = chk.get("path")
        via = " (Hermes' own Python, -m hermes_cli.main)" if chk.get("via") == "interpreter" else ""
        out.append({"id": "hermes_path", "ok": bool(path) and not chk.get("problem"),
                    "message": (f"hermes: {_home_label(path)}{via} (owner and permissions checked: ok)"
                                if path and not chk.get("problem")
                                else tool_problem_text("hermes", path, chk.get("problem"))),
                    "fix": "" if path and not chk.get("problem") else
                    "Ready to switch resumes jobs with the hermes command; install Hermes where only you (or root) "
                    "can change it"})
        return out

    # ---- hook helpers ----------------------------------------------------------------------------
    def twinned_pairs(self, profile: str) -> list[dict]:
        st = self.cached_status() or {}
        return [p for p in st.get("pairs", []) if p.get("hermes", {}).get("profile") == profile]

    def on_session_start(self) -> bool:
        """True when a debounced background sync should run for the running profile."""
        now = time.monotonic()
        if now - self._last_session_sync < SESSION_SYNC_DEBOUNCE:
            return False
        if not self.twinned_pairs(self.profile()):
            return False
        self._last_session_sync = now
        return True

    def record_activity(self) -> None:
        """Cheap per-turn marker (timestamp + kind only), debounced; no content is read or stored."""
        prof = self.profile()
        now = time.monotonic()
        if now - self._last_activity.get(prof, 0.0) < ACTIVITY_DEBOUNCE:
            return
        self._last_activity[prof] = now
        pairs = self.twinned_pairs(prof)
        if not pairs:
            return
        with store.locked():
            path = self.central / "activity.json"
            data = store.read_json(path, {}) or {}
            for p in pairs:
                e = data.get(p["pair_id"]) if isinstance(data.get(p["pair_id"]), dict) else {}
                data[p["pair_id"]] = {"last_at": now_iso(), "kind": "session", "turns": int(e.get("turns") or 0) + 1}
            store.atomic_write_json(path, data)
        for p in pairs:
            if p.get("demo"):
                try:
                    self.demo.note_activity(p["pair_id"], "hermes", "session", "Chat in Hermes")
                except EngineError:
                    pass


_service: Optional[TwinService] = None
_service_lock = threading.Lock()


def get_service() -> TwinService:
    global _service
    with _service_lock:
        if _service is None:
            _service = TwinService()
        return _service


def reset_service() -> None:
    """Tests: drop the singleton (e.g. after HERMES_HOME changes)."""
    global _service
    with _service_lock:
        _service = None
