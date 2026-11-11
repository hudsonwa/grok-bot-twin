"""TwinService: one facade over DemoEngine (demo pairs) and BotportEngine (real pairs).

Used by the REST API, tools, CLI, slash command, timer and awareness block. Caches status, records
metadata-only events (events.jsonl, capped) and broadcasts plugin events to an attached Desktop.
"""
from __future__ import annotations

import contextlib
import fnmatch
import logging
import os
import re
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import config, importer, safe, store, watch
from . import hermes_side as hs
from . import secrets as _secrets
from .connect import CAUSE_ERROR_CODES, BadInput, ConnectMixin
from .demo import DemoEngine, unique_profile
from .finish import REPLACED_BY, FinishMixin
from .setup_export import SetupExportMixin
from .batch import BatchMixin
from .fleet_api import FleetMixin
from .skill_sync import SkillSyncMixin
from .routines import RoutinesMixin
from .instr_sync import InstrSyncMixin
from .h2g_service import H2GMixin
from .tasks_api import TasksMixin
from .briefs import BriefsMixin
from .engine import (INSTALL_HINT, TRUST_HINT, UNATTESTED_TEXT, BotportEngine, Engine, EngineError, _home_label,
                     find_botport, find_gh, tool_problem_text, valid_tag)
from .model import (DIRECTIONS, KEEP_SIDES, MIRROR_NOTE, PLUGIN_ID, SAMPLE_PROFILE, STATE_LABELS, clean_waiting,
                    clean_withheld, derive_state, item_kind, normalize_pair, now_iso, pair_ref, parse_iso, plural,
                    preview_lines, preview_note, valid_id, valid_item_id)

log = logging.getLogger(__name__)

STATUS_TTL = 5.0
SESSION_SYNC_DEBOUNCE = 30.0
ACTIVITY_DEBOUNCE = 30.0
EVENTS_CAP = 500
DEMO_ACTIONS = ("start", "remove", "grok-edit", "hermes-edit", "conflict", "secret", "remove-secret", "hide-real",
                "show-real")
# Plain sentences, shared by the page, the CLI and the README.
BAD_REQUEST = "That request was not understood."
HELPER_MISSING = "botport is not on this computer yet."
# Stop twinning one bot. A line is listed only for a thing that exists now.
REACH_HINT = "On the Twins page, press Get botport again. Then try this once more."
STOP_NEEDS_BOTPORT = "botport is missing, so this cannot be done yet. On the Twins page, press Get botport again first."
STOP_STAYS = ["Your bot in Grok, unchanged.", "Its Hermes bot and all its chats."]
# ("link" is the word for two computers; what ends here is the twin following its Grok bot)
STOP_GOES_LINK = "Following Grok."
STOP_GOES_VERSIONS = "The earlier versions kept for undo."
STOP_AGAIN_SAME = "You can twin it again later; it picks up this Hermes bot again."
STOP_AGAIN_NEW = "You can twin it again later; that makes a new Hermes bot."
SAMPLE_STAYS = ["Everything of yours. The sample holds invented data only."]
SAMPLE_GOES = ["The sample twin and its invented data."]
# Remove Grok Bot Twin.
ALL_STAYS = ["Your Grok bots.", "Your Hermes bots and their chats."]
GOES_TWINS = "Every twin."             # (when the twins cannot be counted)
GOES_VERSIONS = "Earlier versions."    # (likewise)
GOES_NAMES_CAP = 5


def goes_twins_line(names: list, n: int) -> str:
    """"3 twins (Trail Guide, Garden Notes, Book Club)." : how many, and which, in the Remove dialog. (Their
    Hermes bots stay, and are listed under Stays; "twin link" made readers ask whether that was the twin.)"""
    if n <= 0:
        return GOES_TWINS
    shown = [x for x in (safe.clean_name(nm, 40) for nm in names) if x][:GOES_NAMES_CAP]
    more = n - len(shown)
    if not shown:
        return f"{plural(n, 'twin')}."
    listed = ", ".join(shown) + (f" and {more} more" if more > 0 else "")
    return f"{plural(n, 'twin')} ({listed})."


# botport's own activity lines name a part by its key ("persona.name restored from a kept copy"). The page and
# the terminal print the line as it comes, so the key is put in the owner's words here.
_SUMMARY_KEY = re.compile(r"(?<![\w.])(?:persona\.(name|description|instructions)|(skill|routine|connector):([^\s,;]{1,80}))")


def plain_summary(text: str) -> str:
    """"persona.name restored from a kept copy" -> "name put back from an earlier version"."""
    if not isinstance(text, str) or not text:
        return ""

    def words(m: "re.Match[str]") -> str:
        if m.group(1):
            return m.group(1)
        return f'{"connection" if m.group(2) == "connector" else m.group(2)} "{m.group(3)}"'

    out = _SUMMARY_KEY.sub(words, text)
    return out.replace("restored from a kept copy", "put back from an earlier version")


def goes_versions_line(n: int) -> str:
    return f"Earlier versions of {plural(n, 'twin')}." if n > 0 else GOES_VERSIONS
GOES_SAMPLE = "The sample."
GOES_BOTPORT = "Hermes' copy of botport (your download stays in Downloads)."
GOES_WORKING_COPY = "Hermes' working copy of botport (the botport on this computer stays)."
GOES_NOTES = "Grok Bot Twin's own notes on this computer."
GOES_BACKGROUND = "What keeps your twins following while Hermes is closed: one small file."
SKILL_SYNC_LEFTOVER = "In Grok, delete the sync bot's routine yourself; Hermes cannot reach it."
UNINSTALL_NEXT = "Switch the plugin off in Settings → Capabilities → Plugins."
# The one thing to do for a twin that fails alone (a twin held up by a shared cause gets the cause's sentence).
RETRY_HINT = "Open the Grok app once, then press Try again."
UNKNOWN_TWIN_HINT = "Stop twinning it, then twin the bot again."
# Files in the plugin's data folder that hold a per-twin note, keyed by twin id.
PER_TWIN_NOTES = ("digest.json", "skipped.json", "waiting.json", "copied.json", "kept.json", "points.json",
                  "stuck.json", "setup-export.json", "instructions-sync.json")
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


def default_grok_app_dir() -> Path:
    """The Grok desktop app's usual data folder (the same default botport uses)."""
    home = Path(os.path.expanduser("~"))
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Grok Bot"
    return home / ".config" / "Grok Bot"


class PairNotFound(EngineError):
    def __init__(self, pid: str) -> None:
        super().__init__("pair_not_found", f"No twin called {safe.clean_name(pid, 64)}.",
                         "It may have been removed. Open the Twins page to see your twins.")


_C1_BIDI = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]")
CONFLICT_TEXT_CAP = 64 * 1024


def _shown_text(text: str) -> str:
    """Multi-line text from the engine (conflict copies and diffs) as shown in the UI and the terminal:
    suspected secrets masked here as well (defence in depth; botport masks them first), control,
    escape and bidi characters dropped (tab and newline kept), capped."""
    text = _secrets.redact(text[: CONFLICT_TEXT_CAP * 2], "[not shown: looks like a password or key]")
    return _C1_BIDI.sub("", text)[:CONFLICT_TEXT_CAP]


# "Compare with Grok", one plain sentence per row for the page's list (`ParityCheck.why`). Chosen here from the
# row's kind and result alone: botport's own text (`detail`) is for "Technical details" and is never quoted.
_WHY_SAME = "The same in Grok and Hermes."
_WHY = {
    ("name", False): "Differs between Grok and Hermes. To change it in Grok, type it in the Grok app yourself.",
    ("description", False): "Differs between Grok and Hermes. To change it in Grok, paste it in the Grok app "
                            "yourself.",
    ("instructions", True): "You keep these yourself. Hermes cannot compare them with Grok.",
    ("instructions", False): "Not brought over yet. The Grok app does not keep the instructions on this computer.",
    ("skill", False): "Differs between Grok and Hermes.",
    ("routine", True): "A routine with this name is in Hermes.",
    ("routine", False): "Not in Hermes yet. Grok keeps routine schedules in its cloud, so you set this one up "
                        "yourself.",
    ("connector", True): "A connection with this name is in Hermes.",
    ("connector", False): "Not in Hermes yet. You connect this service again in Hermes yourself.",
    ("model", False): "No model chosen yet. Choose one for this twin's Hermes bot.",
}


def parity_why(row: dict) -> str:
    """The plain sentence for one comparison row."""
    kind = "model" if row.get("action") == "choose_model" else item_kind(str(row.get("id") or ""))
    kind = {"skills": "skill", "routines": "routine", "connectors": "connector", "connection": "connector"}.get(kind, kind)
    ok = row.get("pass") is True
    return _WHY.get((kind, ok)) or (_WHY_SAME if ok else "Differs between Grok and Hermes.")


_KIND_ORDER = ["name", "description", "instructions", "skill", "skills", "routine", "routines", "connector", "connectors"]


def _kind_order(k: str) -> tuple:
    return (_KIND_ORDER.index(k) if k in _KIND_ORDER else len(_KIND_ORDER), k)


def select_engine(pair_id: Optional[str], demo: DemoEngine, botport: Optional[BotportEngine]) -> Engine:
    """Demo pairs always go to the DemoEngine; real pairs to botport (EngineError when it is missing)."""
    if pair_id and pair_id in demo.pair_ids():
        return demo
    if botport is None:
        # (a real twin exists, so the Twins page shows its banner with "Get botport again…": name that button,
        # not the first-run one, which is not on the page in this state)
        raise EngineError("botport_missing", "botport is missing, so Hermes cannot reach this twin.", REACH_HINT)
    return botport


class TwinService(ConnectMixin, FinishMixin, SetupExportMixin, BatchMixin, FleetMixin, TasksMixin, SkillSyncMixin,
                  RoutinesMixin, InstrSyncMixin, BriefsMixin, H2GMixin):
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
        self._checked: dict[str, str] = {}  # twin id -> when it was last checked by hand (this process)

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
        path = find_botport(cfg["botport_path"], self.chosen_helper())
        if not path:
            self._botport = None
            return None
        if (self._botport is None or self._botport.path != path or self._botport.grok_app_dir != cfg["grok_app_dir"]
                or self._botport.account != cfg["grok_account"]):
            # (the state folder is made, with its safety checks, right before the helper's first run: an install
            # that never runs it, or that was just emptied by "Remove Grok Bot Twin", keeps no empty folder around)
            self._botport = BotportEngine(path, store.hermes_root(), self.central / "botport-state",
                                          cfg["grok_app_dir"], account=cfg["grok_account"])
        return self._botport

    def chosen_helper(self) -> Path:
        """Where the copy of the helper the owner picked on the Twins page lives: <plugin data>/helper/botport."""
        return self.central / "helper" / "botport"

    def engine_info(self) -> dict:
        bp = self.botport()
        if bp is None:
            configured = config.get("botport_path")
            problem = ("The botport location in the plugin's settings does not point at a program" if configured
                       else HELPER_MISSING)
            return {"available": False, "version": None, "problem": problem, "install_hint": INSTALL_HINT}
        info = bp.info()
        out = {"available": bool(info.get("available")), "version": safe.clean_name(info.get("version"), 30) or None,
               "problem": safe.clean_summary(info.get("problem"), 300) or None, "install_hint": INSTALL_HINT,
               "trusted": bool(info.get("trusted")), "attested": bool(info.get("attested")),
               "allow_unattested": bool(info.get("allow_unattested"))}
        if out["trusted"] and out["allow_unattested"] and not out["attested"]:
            out["trust_warning"] = UNATTESTED_TEXT
        return out

    def attest_botport(self, tag: Any = None) -> dict:
        # an explicit tag must look like v0.2.0; none: the tag recorded at setup (else the expected one)
        tag = self.release_tag(tag) if tag not in (None, "") else None
        bp = self.botport()
        if bp is None:
            raise EngineError("not_found", HELPER_MISSING, INSTALL_HINT)
        res = bp.verify_attestation(tag)
        tag = res.get("tag") if valid_tag(res.get("tag")) else tag
        self.invalidate()
        return {"verified": res.get("verified") is True, "problem": safe.clean_summary(res.get("problem"), 200) or None,
                "repo": safe.clean_name(res.get("repo"), 80), "workflow": safe.clean_name(res.get("workflow"), 120),
                "tag": tag}

    def trust_info(self) -> dict:
        bp = self.botport()
        if bp is None:
            return {"trusted": False, "problem": HELPER_MISSING, "install_hint": INSTALL_HINT}
        return self._trust_view(bp.trust_status())

    # ---- connect: the three facts the Twins page shows, and the helper the owner picked ---------------------------
    @staticmethod
    def grok_app_folder() -> Path:
        """Where the Grok desktop app keeps its data (the `grok_app_dir` setting, else the app's usual place).
        Only its existence is checked here. The one other thing the plugin does with it is in watch.py: file
        names, dates and sizes in one sub-folder. No file in it is ever opened for reading by the plugin."""
        cfg = config.get("grok_app_dir")
        return Path(cfg) if cfg else default_grok_app_dir()

    def grok_app_found(self) -> bool:
        try:
            return self.grok_app_folder().is_dir()
        except OSError:
            return False

    def note_grok_app(self) -> None:
        """Tell the page when the Grok app's folder went away or came back (it reads its status again on any
        event), so its notice appears and clears within seconds instead of at its next slow reading. Only on a
        computer with real twins; the first look tells nothing. Never raises."""
        try:
            if not self._real_pair_ids():
                self._grok_seen = None
                return
            found = self.grok_app_found()
            before = getattr(self, "_grok_seen", None)
            self._grok_seen = found
            if before is not None and before != found:
                self.invalidate()
                self.emit("setup", None, {"grok_app": found})
        except Exception:
            log.debug("grok-bot-twin: could not look for the Grok app's folder", exc_info=True)

    def _real_pair_ids(self) -> list[str]:
        demo_ids = self.demo.pair_ids()
        return [p for p in self._registry() if p not in demo_ids]

    def watch_folder(self) -> Optional[Path]:
        """The one folder the fast check may look at (file names, dates and sizes only; see watch.py), or None
        when there is nothing to watch for: the owner switched it off, or there is no real twin."""
        if not config.get("watch_grok_changes") or not self._real_pair_ids():
            return None
        return self.grok_app_folder() / watch.CACHE_SUBDIR

    def fast_check_state(self) -> dict:
        """Is a change of name or description in Grok picked up right away in this process, and how does the
        plugin wait for it ("events": the system tells it; "poll": it compares file dates every 2 s)."""
        on = False
        if watch.running():
            try:
                folder = self.watch_folder()
                on = folder is not None and folder.is_dir()
            except OSError:
                on = False
        return {"on": on, "mode": watch.mode() if on else "off"}

    def sample_state(self) -> dict:
        on = self.demo.exists()
        return {"on": on, "hide_real": on and self.demo.hide_real(),
                # the sample bot's instructions as they stand in the pretend Grok (for the walk-through's paste)
                "instructions_example": self.demo.instructions_example() if on else None}

    # The plugin's own small working files: alone they are nothing an owner would look for under "Remove".
    WORKING_FILES = frozenset({".lock", "events.jsonl", "status-cache.json", "ref-salt"})

    def has_data(self) -> bool:
        """True while the plugin's data folder holds something to remove: a twin, the sample, earlier versions,
        sharing or a link, kept bots, Hermes' copy of botport (a botport that merely sits on PATH is not the
        plugin's data). Its own working files alone (the lock, the activity log, the remembered status, the salt
        for twin references) do not count: after "End sample" on a computer that never had anything else the
        first screen offered "Remove Grok Bot Twin…" with nothing left to remove. `hermes twin uninstall` still
        clears those files."""
        try:
            return any(e.name not in self.WORKING_FILES for e in os.scandir(self.central))
        except OSError:
            return False

    @staticmethod
    def _trust_view(ts: dict) -> dict:
        att = ts.get("attestation") if isinstance(ts.get("attestation"), dict) else {}
        sha = ts.get("sha256")
        out = {"trusted": ts.get("trusted") is True,
               "sha256": sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) else None,
               "version": safe.clean_name(ts.get("version"), 30) or None,
               "trusted_at": ts.get("trusted_at") if parse_iso(ts.get("trusted_at")) else None,
               "attested": att.get("verified") is True or ts.get("pinned") is True,
               "allow_unattested": (ts.get("allow_unattested") is True and att.get("verified") is not True
                                    and ts.get("pinned") is not True),
               "method": ts.get("method") if ts.get("method") in ("pinned", "gh", "local") else None,
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
        self.fleet_registry_changed()  # the owner's other machines hear of it at once (when sharing is on)

    def _remember(self, pairs: list[dict], job_ids: Optional[dict] = None) -> None:
        """Record real twins in pairs.json; `job_ids` maps a pair id to the ids of the cron jobs made from its
        bot's routines (recorded at create; kept on later updates). The plugin's own notes about a twin (routines
        moved, instructions brought over, the model it started with, the description last read from Grok) are
        kept across refreshes."""
        reg = self._registry()
        changed = False
        for p in pairs:
            old = reg.get(p["pair_id"], {})
            og = old.get("grok") if isinstance(old.get("grok"), dict) else {}
            entry = {"pair_id": p["pair_id"], "grok": {"id": p["grok"]["id"], "name": safe.clean_name(p["grok"]["name"], 100)},
                     "hermes": {"profile": p["hermes"]["profile"]},
                     "created_at": old.get("created_at") or now_iso()}
            if isinstance(og.get("description"), str) and og.get("id") == p["grok"]["id"]:
                entry["grok"]["description"] = og["description"]
            jobs = (job_ids or {}).get(p["pair_id"])
            if jobs is None:
                jobs = old.get("job_ids")
            if isinstance(jobs, list):
                entry["job_ids"] = [j for j in jobs if valid_id(j)][:200]
            for key in ("switched", "instructions", "model"):
                if isinstance(old.get(key), dict):
                    entry[key] = old[key]
            if reg.get(p["pair_id"]) != entry:
                reg[p["pair_id"]] = entry
                changed = True
        if changed:
            self._save_registry(reg)

    def _note_descriptions(self, bots: list) -> None:
        """Keep what each twinned bot says in Grok, as just read from the Grok app's bot list (password-like text
        hidden before it is stored)."""
        seen = {b.get("grok_id"): b.get("description") for b in bots if isinstance(b, dict)}
        with store.locked():
            reg = self._registry()
            changed = False
            for e in reg.values():
                g = e.get("grok") if isinstance(e.get("grok"), dict) else {}
                if g.get("id") in seen and isinstance(seen[g["id"]], str):
                    text = hs.shown(seen[g["id"]], 2000)
                    if g.get("description") != text:
                        g["description"] = text
                        changed = True
            if changed:
                self._save_registry(reg)

    def pair_ids(self) -> list[str]:
        return self.demo.pair_ids() + [p for p in self._registry() if p not in self.demo.pair_ids()]

    def require_pair(self, pid: Any) -> str:
        if not valid_id(pid):
            raise BadInput("That is not one of your twins.")
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
                raise BadInput("That time was not understood.")
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
            setup = self.setup_brief()
            cause = setup["cause"] if setup["cause"] in CAUSE_ERROR_CODES else None
            if not allow_engine:
                # (the per-turn hook, sample actions) never run botport: the last known rows, else a plain row
                engine = {"available": bp is not None, "version": None, "problem": None, "install_hint": INSTALL_HINT}
                cached = {p["pair_id"]: p for p in (self.cached_status(compute=False) or {}).get("pairs", [])}
                for pid in real_ids:
                    pairs.append(cached[pid] if pid in cached else self._error_pair(
                        reg[pid], setup["problem"] or "Hermes has not checked this twin yet",
                        CAUSE_ERROR_CODES.get(cause, "not_checked")))
            elif cause or bp is None:
                # one cause holds every real twin up (the Grok app is gone, botport is gone or has to be verified
                # again): each twin still arrives, with the cause's sentence; the page shows it once, as a banner
                engine = self.engine_info()
                problem = setup["problem"] or "botport is missing, so Hermes cannot check this twin"
                pairs.extend(self._error_pair(reg[pid], problem, CAUSE_ERROR_CODES.get(cause, "botport_missing"))
                             for pid in real_ids)
            else:
                engine = self.engine_info()
                try:
                    got = [normalize_pair(p) for p in _lst(bp.status(None), "pairs")]
                    got = [p for p in got if p and p["pair_id"] not in self.demo.pair_ids()]
                    for p in got:
                        if not p["waiting"]:  # botport's status has counts only; offer the items it listed last
                            p["waiting"] = self._waiting_for(p["pair_id"])
                    self._remember(got)
                    reg = self._registry()
                    pairs.extend(got)
                    seen = {p["pair_id"] for p in got}
                    pairs.extend(self._error_pair(reg[pid], "botport no longer knows this twin", "unknown_twin",
                                                  UNKNOWN_TWIN_HINT)
                                 for pid in real_ids if pid not in seen)
                except EngineError as exc:
                    pairs.extend(self._error_pair(reg[pid], exc.message, exc.code, exc.hint) for pid in real_ids)
            skipped = self._skipped()
            copied = self._copied()
            stuck = self._stuck()
            for p in pairs:
                p["grok"]["name"] = safe.clean_name(p["grok"]["name"], 100)
                if not p.get("demo"):
                    entry = reg.get(p["pair_id"]) or {}
                    p["switched"] = self._switched_view(entry, p["hermes"]["profile"])
                    self._finish_real(p, entry)
                    self._apply_copied(p, copied.get(p["pair_id"]) or {})
                    self._apply_stuck(p, stuck.get(p["pair_id"]))
                self._apply_skipped(p, skipped.get(p["pair_id"]) or [])
                if p["state"] == "error" and isinstance(p.get("error"), dict) and not cause \
                        and not p["error"].get("hint"):
                    p["error"]["hint"] = RETRY_HINT  # a twin that fails alone says the one thing to do
                p["state_reason"] = self._plain_reason(p)
                p["checked_at"] = self._checked_at(p)
                p["ref"] = pair_ref(p["pair_id"])
                p["digest"] = self.digest_settings(p["pair_id"])
            fleet_of = self.fleet_pairs()  # on a machine that shares: where each twin's copies are
            for p in pairs:
                if p["pair_id"] in fleet_of:
                    p["fleet"] = fleet_of[p["pair_id"]]
            out = {"mode": self.mode(len(real_ids), bp is not None), "engine": engine, "pairs": pairs,
                   "last_check_at": self.last_check_at, "interval_s": config.get("sync_interval_seconds"),
                   "fast_check": self.fast_check_state(),
                   "profile": self.profile(), "setup": setup, "sample": self.sample_state(),
                   "fleet": self.fleet_brief()}
            self._status = (time.monotonic(), out)
            cache = self.central / "status-cache.json"
            if pairs or cache.exists():  # nothing is written for an install that holds no twins
                try:
                    store.atomic_write_json(cache, {"at": now_iso(), "status": out})
                except OSError:
                    pass
            out["has_data"] = self.has_data()
            return out

    # ---- plain words + plugin-side filters applied to every pair in status -----------------------------------
    @staticmethod
    def _apply_skipped(p: dict, skipped: list) -> None:
        """Items the owner keeps in Hermes only stop asking: they leave `waiting`, what `to_grok` counts, the
        counts and the state."""
        p["skipped"] = [i for i in skipped if valid_item_id(i)][:100]
        if not p["skipped"]:
            return
        keep = [w for w in p.get("waiting") or [] if w["item"] not in p["skipped"]]
        items = [i for i in p["pending"].get("to_grok_items") or [] if i not in p["skipped"]]
        gone = len(p.get("waiting") or []) - len(keep)
        gone_items = len(p["pending"].get("to_grok_items") or []) - len(items)
        if not gone and not gone_items:
            return
        p["waiting"] = keep
        p["pending"]["to_grok_items"] = items
        p["pending"]["waiting_approval"] = max(0, int(p["pending"].get("waiting_approval") or 0) - gone)
        p["pending"]["to_grok"] = max(0, int(p["pending"].get("to_grok") or 0) - gone_items)
        if p["state"] in ("needs_you", "changes_waiting"):
            p["state"], p["state_reason"] = derive_state(p)

    @staticmethod
    def _plain_reason(p: dict) -> str:
        """The reason in the page's words, built from the pair's own counts (every open matter, never one of
        several); engine wording is used only for an error, cleaned."""
        if p["state"] == "error":
            err = p.get("error") if isinstance(p.get("error"), dict) else {}
            return safe.clean_summary(err.get("message") or p.get("state_reason"), 160) or STATE_LABELS["error"]
        state, reason = derive_state(p)
        if state == p["state"]:
            return reason
        return STATE_LABELS.get(p["state"], STATE_LABELS["error"])

    def _switched_view(self, entry: dict, profile: Any) -> Optional[dict]:
        sw = entry.get("switched") if isinstance(entry.get("switched"), dict) else None
        if not sw or not parse_iso(sw.get("at")):
            return None
        ids = [j for j in (sw.get("job_ids") or []) if valid_id(j)]
        on = importer.running_jobs_for(self._profile_home(profile), ids) if ids else []
        return {"at": sw["at"], "routines_on": len(on)} if on else None

    def _demo_status(self) -> list[dict]:
        try:
            return [p for p in (normalize_pair(r, demo=True) for r in _lst(self.demo.status(), "pairs")) if p]
        except EngineError as exc:
            log.warning("grok-bot-twin: demo status failed: %s", exc.message)
            return []

    @staticmethod
    def _error_pair(entry: dict, reason: str, code: str = "internal", hint: str = "") -> dict:
        err = {"code": code, "message": reason}
        p = normalize_pair({**entry, "state": "error", "state_reason": reason, "error": err}) or {}
        if p and hint:
            p["error"]["hint"] = safe.clean_summary(hint, 300)
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
                    if isinstance(raw, dict) and isinstance(raw.get("skipped"), list):
                        p["skipped"] = [i for i in raw["skipped"][:100] if valid_item_id(i)]
                    p["ref"] = pair_ref(p["pair_id"])
                    p["digest"] = self.digest_settings(p["pair_id"])
                    pairs.append(p)
            return {"mode": st.get("mode") if st.get("mode") in ("botport", "demo", "setup") else "setup",
                    "engine": {"available": False, "version": None, "problem": None, "install_hint": INSTALL_HINT},
                    "pairs": pairs, "last_check_at": st.get("last_check_at") if parse_iso(st.get("last_check_at")) else None,
                    "interval_s": config.get("sync_interval_seconds"), "fast_check": self.fast_check_state(),
                    "profile": self.profile(), "setup": self.setup_brief(), "sample": self.sample_state(),
                    "has_data": self.has_data()}
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
        at = now_iso()
        demo_ids = set(self.demo.pair_ids())
        for r in results:  # (from botport's own words, before they are cleaned for showing)
            if isinstance(r, dict) and valid_id(r.get("pair_id")) and r["pair_id"] not in demo_ids:
                self._note_stuck(r["pair_id"], r.get("error") if r.get("state") == "error" else None)
        for r in out:
            if r["state"] != "error":
                self._checked[r["pair_id"]] = at
            if r["pair_id"] in demo_ids:
                continue
            if r["restore_point"]:
                self._note_point(r["pair_id"], r["restore_point"], "grok")
            if any(a["item"] == "persona.description" and a["direction"] == "g2h" for a in r["applied"]):
                self._refresh_description(r["pair_id"])
        self.invalidate()
        self._drop_skipped(out)
        self._emit_sync(out)
        return {"pairs": out}

    def _drop_skipped(self, entries: list[dict]) -> None:
        """Sync/approve results never list an item the owner keeps in Hermes only, nor count it in the state."""
        skipped = self._skipped()
        fresh: Optional[dict] = None
        for e in entries:
            mine = skipped.get(e.get("pair_id")) or []
            if not mine or not e.get("waiting"):
                continue
            keep = [w for w in e["waiting"] if w["item"] not in mine]
            if len(keep) == len(e["waiting"]):
                continue
            e["waiting"] = keep
            if e.get("state") in ("needs_you", "changes_waiting"):
                try:
                    if fresh is None:
                        fresh = {p["pair_id"]: p["state"] for p in self.status(fresh=True)["pairs"]}
                    e["state"] = fresh.get(e["pair_id"], e["state"])
                except EngineError:
                    pass

    # ---- "Keep it in Hermes only" -----------------------------------------------------------------------------
    def _skipped(self) -> dict:
        data = store.read_json(self.central / "skipped.json", {}) or {}
        if not isinstance(data, dict):
            return {}
        return {pid: [i for i in items[:200] if valid_item_id(i)] for pid, items in data.items()
                if valid_id(pid) and isinstance(items, list)}

    def skip(self, pid: str, item: Any, undo: Any = False, copied: Any = False) -> dict:
        """Keep one changed item in Hermes only: it stops asking to be sent to Grok (undo=True offers it again).
        copied=True: the owner pasted this version of a skill into Grok by hand; it stops asking, is NOT listed
        as kept in Hermes only, and asks again when the skill changes in Hermes later.
        A plugin-side note; botport is not told, and nothing is copied or deleted."""
        pid = self.require_pair(pid)
        if not valid_item_id(item):
            raise BadInput("That item is not one of this twin's.")
        if not isinstance(undo, bool) or not isinstance(copied, bool) or (undo and copied):
            raise BadInput(BAD_REQUEST)
        if copied:
            item = self._mark_copied(pid, item)  # (the twin's own name for it: "description" may be "persona.description")
            if item is None:  # already settled (Grok has the text): "Done" is answered as done, nothing is written
                self.invalidate()
                return {"pair_id": pid, "skipped": self._skipped().get(pid, [])}
        with store.locked():
            data = self._skipped()
            items = [i for i in data.get(pid, []) if i != item]
            if not undo and not copied:
                items.append(item)
            if items:
                data[pid] = items[-200:]
            else:
                data.pop(pid, None)
            store.atomic_write_json(self.central / "skipped.json", data)
        self.invalidate()
        self.emit("skipped", pid, {"undo": undo, **({"copied": True} if copied else {})})
        return {"pair_id": pid, "skipped": data.get(pid, [])}

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
        self.fleet_tick()  # the safety net for sharing with, or following, the owner's other machines
        try:
            self.briefs_tick()  # session briefs: nothing at all while every twin's briefs are off
        except Exception:
            log.exception("grok-bot-twin: session briefs round failed")
        self.note_grok_app()
        self.instructions_sync_tick()  # Grok's instructions re-read from the newest setup exports (every 15 min)
        try:  # memory facts with Grok (every 15 min): on any computer, linked or not; nothing until the Mirror is there
            from . import mem_facts as _mem_facts
            _mem_facts.tick(lambda: self)
        except Exception:
            log.exception("grok-bot-twin: memory facts round failed")
        self.to_grok_tick()  # Hermes -> Grok: the Mirror's answers (files only; nothing when it was never used)
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
        """The owner's Grok bots (`bots`: real ones only) and, while the sample is on, the invented ones apart in
        `sample_bots`: the two are never mixed, and real bots are hidden only when the owner chose "Hide my real
        bots while the sample is on" (`real_hidden`). A missing or unchecked helper is not an error here: the
        answer says so (`ready: false`) and the page opens "Connect your Grok bots" instead of a list."""
        globs = config.get("bots") or []
        setup = self.setup_brief()
        bots: list[dict] = []
        warnings: list = []
        app_found = setup["grok_app_found"]
        ready = setup["ready"]
        demo_on = self.demo.exists()
        real_hidden = demo_on and self.demo.hide_real()
        bp = self.botport()
        unusable = False
        if bp is not None and ready and not real_hidden:
            try:
                data = bp.grok_list(globs)
                app_found = bool(data.get("app_found"))
                warnings = _lst(data, "_warnings")
                bots = [dict(b, demo=False) for b in _lst(data, "bots") if isinstance(b, dict)]
                self._note_descriptions(bots)
                ready = ready and app_found
            except EngineError as exc:
                if exc.code in ("untrusted_binary", "not_found", "botport_missing"):
                    unusable = True
                    ready = False  # the full check before the run found the helper changed or gone
                    warnings = [{"code": exc.code, "message": exc.message}]
                elif demo_on:
                    warnings = [{"code": exc.code, "message": exc.message}]
                else:
                    raise
        sample = [dict(b, demo=True) for b in _lst(self.demo.grok_list(globs), "bots")] if demo_on else []
        res = {"ready": ready, "app_found": app_found, "helper_found": setup["helper_found"],
               "helper_checked": setup["helper_checked"] and not unusable, "bots": self._roster(bots, globs),
               "sample_bots": self._roster(sample, globs), "real_hidden": real_hidden}
        warnings = [{"code": safe.clean_name(w.get("code"), 40), "message": safe.clean_summary(w.get("message"), 200)}
                    for w in warnings[:20] if isinstance(w, dict)]
        if warnings:
            res["warnings"] = warnings
        return res

    @staticmethod
    def _roster(bots: list[dict], globs: list) -> list[dict]:
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
                        "demo": bool(b.get("demo")), "sample": bool(b.get("demo"))})
        return out

    def _route_grok(self, gid: str) -> Engine:
        if self.demo.exists() and self.demo._grok(gid) is not None:
            return self.demo
        bp = self.botport()
        if bp is None:
            raise EngineError("botport_missing", "Connect your Grok bots first: botport is not on this "
                                                 "computer yet.", INSTALL_HINT)
        return bp

    def _check_create_input(self, grok_ids: Any, profiles: Any) -> tuple[list[str], dict]:
        if not isinstance(grok_ids, list) or not grok_ids or len(grok_ids) > 50 or not all(valid_id(g) for g in grok_ids):
            raise BadInput("Choose at least one bot.")
        profiles = profiles or {}
        if not isinstance(profiles, dict) or not all(valid_id(k) and valid_id(v) for k, v in profiles.items()):
            raise BadInput("A Hermes name may use only letters, digits, dots, dashes and underscores.")
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
        """Pick each bot's Hermes profile: the one asked for, else a name made from the bot's name that backs no
        twin (a taken name is fixed by itself: `recipe-planner-2`). A bot that already has a twin is skipped
        (`already_twinned`): one bot, one twin. A profile the caller NAMED that already backs another twin (or is
        named twice in this batch) is refused, as botport's profile_already_paired does, so two bots never end up
        in one profile. Returns the chosen profiles, the skipped bots, each bot's engine, and botport dry runs
        already made (reused by the preview)."""
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
            if valid_id(twinned.get(gid)):
                skipped.append({"grok_id": gid, "reason": "already_twinned", "pair_id": twinned[gid]})
                continue
            prof = profiles.get(gid)
            named = bool(prof)
            if not prof:
                taken = set(used) | set(chosen.values())
                if eng is self.demo:
                    prof = unique_profile(SAMPLE_PROFILE, taken)  # a sample twin never names a real profile
                elif self._former_profile(gid, taken):
                    # this bot was twinned before and its Hermes bot is still there: the twin picks it up again
                    # (botport adopts a profile that is named to it; left alone it would make a second one)
                    prof = self._former_profile(gid, taken)
                else:
                    # botport picks the slug of the bot's name; its dry run says which (the Grok roster is not
                    # read again here). A name that is taken is fixed by itself instead of an error.
                    prof, dry[gid] = self._botport_default_profile(eng, gid)
                    old = self._old_twin_of(eng, gid, prof, taken)
                    if old:
                        # botport would make a second Hermes bot ("name-2") beside the one this very bot's twin
                        # lived in before Grok Bot Twin was removed and set up again: that one is picked up
                        # again instead, as after Stop twinning (its chats and pasted instructions are in it)
                        prof = old
                        dry.pop(gid, None)
                    if prof and prof in taken:
                        prof = unique_profile(prof, taken)
                        dry.pop(gid, None)
                    elif not prof and self._paired_only(dry.get(gid)):
                        prof = unique_profile(self._grok_name(eng, gid) or gid, taken | self._botport_profiles(eng))
                        dry.pop(gid, None)
            owner = used.get(prof) if prof else None
            if named and (owner is not None or prof in chosen.values()):
                skipped.append({"grok_id": gid, "reason": "profile_already_paired",
                                **({"pair_id": owner} if valid_id(owner) else {})})
                continue
            chosen[gid] = prof
        return chosen, skipped, engines, dry

    _PERSONA_NAME_RE = re.compile(r"<!-- botport:begin persona-name -->\r?\n(.*?)\r?\n<!-- botport:end persona-name -->",
                                  re.S)

    def _old_twin_of(self, eng: Engine, gid: str, prof: Optional[str], taken: set) -> Optional[str]:
        """After "Remove Grok Bot Twin" and a new setup nothing remembers which Hermes bot was whose (the record
        went with the data, as promised). botport then proposes a numbered name ("acme-support-desk-2") because
        the old Hermes bot still sits at the plain one. That old bot is this Grok bot's former twin when its
        prompt file still carries botport's own name block with exactly this bot's name, and no twin uses it now:
        then its name is returned, to be named to botport, which picks it up. Anything else (no such block, another
        name, a link, a bot in use) returns None and botport's own choice stands: a bot the owner made is never
        taken over.

        A bot that was renamed in Grok while it was twinned sits in a folder named after its OLD name
        ("test-scout" for "Test Scout Pro"), so botport's proposal ("test-scout-pro") is free and names nothing
        to look at. For that case every Hermes bot that no twin uses is looked at: one that carries the name
        block with exactly this name AND botport's own traces of a former twin (its lock file or its folder of
        kept copies) is the former twin, when it is the only such bot."""
        try:
            m = re.fullmatch(r"(.+?)-(\d{1,3})", prof or "")
            base = m.group(1) if m else None
            # (files first: botport is asked for the bot's name only when some bot here could be the former twin)
            cands = []
            if base and self._name_block(base, taken) is not None:
                cands.append(base)
            root = store.hermes_root() / "profiles"
            with os.scandir(root) as it:
                names = sorted(e.name for _, e in zip(range(2000), it) if not e.name.startswith("."))
            for cand in names:
                if cand in (prof, base) or self._name_block(cand, taken) is None:
                    continue
                home = Path(self._profile_home(cand))
                if os.path.lexists(home / ".botport.lock") or self._real_dir(home.joinpath(*hs.BOTPORT_KEPT_DIR)):
                    cands.append(cand)
            if not cands:
                return None
            name = self._grok_name(eng, gid)
            if not name:
                return None
            if base in cands and self._name_block(base, taken) == name:
                return base
            found = [c for c in cands if c != base and self._name_block(c, taken) == name]
            return found[0] if len(found) == 1 else None  # two bots could be it: neither is taken
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return None

    @staticmethod
    def _real_dir(path: Path) -> bool:
        try:
            return stat.S_ISDIR(os.lstat(path).st_mode)
        except OSError:
            return False

    def _name_block(self, cand: Any, taken: set) -> Optional[str]:
        """The name in botport's name block of a Hermes bot's prompt file, for a bot that is a real folder and
        that no twin uses, when the block is there exactly once; else None."""
        if not valid_id(cand) or cand.startswith("-") or cand == "default" or cand in taken:
            return None
        home = self._profile_home(cand)
        if home is None or not self._real_dir(Path(home)):
            return None
        from . import fleet_io as fio
        names = self._PERSONA_NAME_RE.findall(fio.read_text(Path(home) / "SOUL.md", hs.SOUL_CAP) or "")
        return (safe.clean_name(names[0].strip(), 100) or None) if len(names) == 1 else None

    def _former_profile(self, gid: str, taken: set) -> Optional[str]:
        """The Hermes bot (profile) a Grok bot's twin lived in before the owner stopped twinning it, when that
        Hermes bot still exists and backs no other twin; else None."""
        data = store.read_json(self.central / "former.json", {}) or {}
        prof = data.get(gid) if isinstance(data, dict) else None
        if not valid_id(prof) or prof.startswith("-") or prof == "default" or prof in taken:
            return None
        home = self._profile_home(prof)
        try:
            return prof if home is not None and stat.S_ISDIR(os.lstat(home).st_mode) else None
        except OSError:
            return None

    def _remember_former(self, gid: Any, prof: Any) -> None:
        if not valid_id(gid) or not valid_id(prof) or prof == "default":
            return
        with store.locked():
            data = store.read_json(self.central / "former.json", {}) or {}
            data = data if isinstance(data, dict) else {}
            data.pop(gid, None)
            data[gid] = prof
            store.atomic_write_json(self.central / "former.json", dict(list(data.items())[-500:]))

    @staticmethod
    def _paired_only(data: Any) -> bool:
        return bool(isinstance(data, dict) and not _lst(data, "pairs") and any(
            isinstance(k, dict) and k.get("reason") == "profile_already_paired" for k in _lst(data, "skipped", 50)))

    @staticmethod
    def _grok_name(eng: Engine, gid: str) -> str:
        try:
            for b in _lst(eng.grok_list([]), "bots"):
                if isinstance(b, dict) and b.get("grok_id") == gid:
                    return safe.clean_name(b.get("name"), 100)
        except EngineError:
            pass
        return ""

    @staticmethod
    def _botport_profiles(eng: Engine) -> set:
        try:
            return {(p.get("hermes") or {}).get("profile") for p in _lst(eng.status(None), "pairs")
                    if isinstance(p, dict) and isinstance(p.get("hermes"), dict)} - {None}
        except EngineError:
            return set()

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
                # switched-off copies of the bot's routines come with the twin only when botport says it made some
                # (the sample always does; the real botport makes none from the Grok app's files)
                copies = eng is self.demo or _n(p.get("schedules_paused")) > 0
                for it in (p.get("items") if isinstance(p.get("items"), list) else [])[:200]:
                    if isinstance(it, dict):
                        prev = it.get("preview")
                        items.append({"item": safe.clean_name(it.get("item"), 140), "kind": safe.clean_name(it.get("kind"), 30),
                                      "name": safe.clean_name(it.get("name"), 100), "action": safe.clean_name(it.get("action"), 20),
                                      "owner": safe.clean_name(it.get("owner"), 10),
                                      "scan": it.get("scan") if it.get("scan") in ("safe", "caution", "dangerous") else None,
                                      **({"note": n} if (n := preview_note(it.get("kind"), it.get("action"), copies)) else {}),
                                      # what the twin will hold (botport masks suspected secrets first; masked here too)
                                      **({"preview": _shown_text(prev[:8000])[:4000]} if isinstance(prev, str) else {})})
                entry = {"grok": {"id": gid, "name": safe.clean_name((p.get("grok") or {}).get("name"), 100)},
                         "hermes": {"profile": safe.clean_name((p.get("hermes") or {}).get("profile"), 64)},
                         "items": items, "secrets_withheld": self._clean_sync(p)["secrets_withheld"],
                         "schedules_paused": _n(p.get("schedules_paused")), "demo": eng is self.demo,
                         "sample": eng is self.demo}
                entry["lines"] = preview_lines(entry)  # the same three lines on the page and in `hermes twin add`
                pairs.append(entry)
        self._name_skipped(skipped)
        # the Desktop warns inline when a profile typed in the preview already backs a twin
        return {"pairs": pairs, "skipped": skipped, "profiles_in_use": sorted(self.profiles_in_use())[:200]}

    def _name_skipped(self, skipped: list[dict]) -> None:
        """Give each left-out bot its name when it is known without asking the helper again (the twin's own
        record, or the sample's roster), so the page and the terminal can say which bot was left out."""
        if not skipped:
            return
        names: dict = {}
        for e in self._registry().values():
            g = e.get("grok") if isinstance(e.get("grok"), dict) else {}
            if valid_id(g.get("id")) and g.get("name"):
                names[g["id"]] = safe.clean_name(g["name"], 100)
        if self.demo.exists():
            for gid in {k.get("grok_id") for k in skipped} - set(names):
                g = self.demo._grok(gid) if valid_id(gid) else None
                if g and g.get("name"):
                    names[gid] = safe.clean_name(g["name"], 100)
        for k in skipped:
            if k.get("grok_id") in names and not k.get("name"):
                k["name"] = names[k["grok_id"]]

    def create(self, grok_ids: Any, profiles: Any = None) -> dict:
        ids, profiles = self._check_create_input(grok_ids, profiles)
        chosen, skipped, engines, _dry = self._plan_profiles(ids, profiles)
        if skipped and not chosen:
            if all(k.get("reason") == "already_twinned" for k in skipped):
                raise EngineError("already_twinned", "That bot already has a twin." if len(skipped) == 1
                                  else "Those bots already have twins.",
                                  "One bot has one twin. Stop twinning it first to start fresh.")
            raise EngineError("profile_already_paired", "That Hermes name already belongs to another twin.",
                              "Choose another name under Options.")
        created: list[str] = []
        notes: list[str] = []
        fresh_real = False
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
                            self._start_real(np_)
                            fresh_real = True
        if fresh_real:
            with contextlib.suppress(EngineError):  # what each new twin's bot says in Grok, as read just now
                bp = self.botport()
                if bp is not None:
                    self._note_descriptions(_lst(bp.grok_list(config.get("bots") or []), "bots"))
        self.invalidate()
        watch.poke()  # the first real twin switches the fast check on at once
        st = self.status(fresh=True)
        out = {"pairs": [p for p in st["pairs"] if p["pair_id"] in created]}
        if notes:
            out["notes"] = notes
        if skipped:
            out["skipped"] = skipped
        for pid in created:
            self.emit("created", pid, {})
        return out

    def _start_real(self, p: dict) -> None:
        """A new real twin gets the owner's default Hermes model (the one their other Hermes chats use), when
        there is one and the twin's Hermes bot has none: model ids and provider names only. botport 0.2 does this
        itself when it makes the twin; the plugin does it for a botport that did not. Either way, a twin that
        starts with the default model is noted as such, so the page can say "It uses the same model as your other
        Hermes chats" until the owner picks another."""
        prof = p["hermes"]["profile"]
        home = self._profile_home(prof)
        if home is None or not prof or prof == "default":
            return
        root = store.hermes_root()
        try:
            hs.copy_default_model(home, root)
        except OSError:
            pass
        model = hs.profile_model(home)
        if model and model == hs.profile_model(root):
            self._note_entry(p["pair_id"], model={"from": "default", "value": model})
        self.fleet_twin_started(p["pair_id"])  # the fleet note, and the plugin switched on in the twin's own bot

    def _refresh_description(self, pid: str) -> None:
        """After a description arrived from Grok: both sides say the same, so what the bot says in Grok is what
        the twin's Hermes bot holds now."""
        entry = self._registry().get(pid) or {}
        prof = (entry.get("hermes") or {}).get("profile") if isinstance(entry.get("hermes"), dict) else None
        g = entry.get("grok") if isinstance(entry.get("grok"), dict) else None
        if g is None or not valid_id(prof):
            return
        text = hs.soul_facts(self._profile_home(prof))["description"]
        if text and g.get("description") != text:
            self._note_entry(pid, grok={**g, "description": text})

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
            raise BadInput("That choice is not one of this twin's.")
        if keep not in KEEP_SIDES:
            raise BadInput("Choose the Hermes version or the Grok version.")
        eng = self.engine_for(pid)
        # "Keep Grok" replaces the Hermes version, and the helper keeps the replaced one under Earlier versions
        # without saying which entry it is. What is listed there is noted before and compared after, so the
        # answer can name it and the page can offer Undo for a real twin exactly as it does for the sample.
        before = self._undo_baseline(eng, pid, conflict_id) if keep == "grok" and eng is not self.demo else None
        data = eng.resolve(pid, conflict_id, keep)
        self.invalidate()
        self.emit("resolved", pid, {"conflict_id": conflict_id, "keep": keep})
        applied = data.get("applied") if isinstance(data.get("applied"), list) else []
        point = data.get("restore_point") if valid_item_id(data.get("restore_point")) else None
        if point is None and before is not None:
            point = self._replaced_version(eng, pid, conflict_id, before)
        if point and eng is not self.demo:
            self._note_point(pid, point, "choice")
        return {"applied": [{"item": a["item"], "keep": a.get("keep") if a.get("keep") in KEEP_SIDES else keep}
                            for a in applied[:50] if isinstance(a, dict) and valid_item_id(a.get("item"))],
                "restore_point": point}

    @staticmethod
    def _point_ids(eng: Engine, pid: str) -> Optional[set]:
        try:
            pts = eng.restore_points(pid).get("points")
        except EngineError:
            return None
        return {(p["id"], p.get("item")) for p in (pts if isinstance(pts, list) else [])[:200]
                if isinstance(p, dict) and valid_item_id(p.get("id"))}

    def _undo_baseline(self, eng: Engine, pid: str, item: str) -> Optional[dict]:
        """Before "Keep Grok": the earlier versions already listed, and the id the Grok version itself will be
        kept under (item@digest), so it is never mistaken for the replaced Hermes version."""
        ids = self._point_ids(eng, pid)
        if ids is None:
            return None
        grok_id = None
        try:
            for c in _lst(eng.conflicts(pid), "conflicts", 200):
                if isinstance(c, dict) and c.get("conflict_id") == item and isinstance(c.get("grok"), dict):
                    digest = c["grok"].get("digest")
                    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{6,64}", digest):
                        grok_id = f"{item}@{digest}"
        except EngineError:
            return None
        return {"ids": ids, "grok_id": grok_id}

    def _replaced_version(self, eng: Engine, pid: str, item: str, before: dict) -> Optional[str]:
        """The earlier version of `item` that this choice added and that is not the Grok version now in place.
        None whenever that cannot be told for sure (none appeared, or more than one candidate did): no Undo is
        offered then, and the entry is still under Earlier versions."""
        after = self._point_ids(eng, pid)
        if after is None:
            return None
        new = [i for i, it in after - before["ids"] if it == item and i != before["grok_id"]]
        return new[0] if len(new) == 1 else None

    def restore_points(self, pid: str) -> dict:
        """Earlier versions of this twin's items. `text`: that version, as the page may show it (password-like
        lines hidden, 20 kB at most); None when the plugin cannot show it (botport keeps its own copies in its own
        format). `replaced_by`: what took this version's place, when that is known."""
        pid = self.require_pair(pid)
        sample = pid in self.demo.pair_ids()
        own = [] if sample else self._own_points(pid)
        try:
            pts = self.engine_for(pid).restore_points(pid).get("points")
        except EngineError:
            if not own:
                raise
            pts = []  # botport cannot be asked right now: the versions the plugin itself keeps are still there
        pts = pts if isinstance(pts, list) else []
        notes = {} if sample else self._point_notes(pid)
        out = [{"id": p["id"], "item": safe.clean_name(p.get("item"), 140),
                "kind": safe.clean_name(p.get("kind"), 30), "name": safe.clean_name(p.get("name"), 100),
                "at": p.get("at") if parse_iso(p.get("at")) else None,
                "reason": safe.clean_summary(p.get("reason"), 120),
                "scope": p.get("scope") if p.get("scope") in ("item", "profile") else "item",
                "text": hs.shown_version(p.get("text")),
                "replaced_by": (p.get("replaced_by") if p.get("replaced_by") in REPLACED_BY
                                else notes.get(p["id"])),
                **({"items": _n(p.get("items"))} if isinstance(p.get("items"), int) else {})}
               for p in pts[:200] if isinstance(p, dict) and valid_item_id(p.get("id"))]
        if out and not sample:
            out = self._with_kept_texts(self._kept_home(pid), out)
        # (an earlier version with nothing in it is no version to go back to: before a part existed)
        own = [p for p in own if (p.get("text") or "").strip()]
        merged = own + out
        merged.sort(key=lambda x: x["at"] or "", reverse=True)
        return {"points": merged[:200]}

    def _kept_home(self, pid: str) -> Optional[Path]:
        prof = next((p["hermes"]["profile"] for p in self.status()["pairs"] if p["pair_id"] == pid), None)
        return self._profile_home(prof) if prof and prof != "default" else None

    def _part_now(self, pid: str, point_id: str) -> Optional[str]:
        """The text this twin holds right now for the part `point_id` is a version of; None when not known."""
        try:
            return hs.botport_kept_texts(self._kept_home(pid))["now"].get(point_id.partition("@")[0])
        except Exception:
            return None

    def _version_holding(self, pid: str, put_back: str, text: str) -> Optional[str]:
        """After a put back: the earlier version whose proven text is `text` (what the part held just before),
        when the part really changed. None when that cannot be told."""
        part = put_back.partition("@")[0]
        try:
            known = hs.botport_kept_texts(self._kept_home(pid))
            if known["now"].get(part) is None or known["now"][part].strip() == text.strip():
                return None  # nothing was replaced
            pts = self.engine_for(pid).restore_points(pid).get("points")
            for p in pts if isinstance(pts, list) else []:
                if isinstance(p, dict) and valid_item_id(p.get("id")) and p["id"].partition("@")[0] == part \
                        and known["versions"].get((part, p["id"].partition("@")[2][:12])) == text:
                    return p["id"]
        except Exception:
            log.debug("grok-bot-twin: could not find the version a put back replaced", exc_info=True)
        return None

    def versions_listed(self, pid: str, home: Optional[Path], raw: Any) -> int:
        """How many earlier versions the list shows for a real twin (the number on the page's tab), so the count
        and the list agree: the plugin's own versions that hold something, plus botport's without the ones the
        list leaves out. botport is asked again only when its count, its kept copies or the prompt file changed."""
        own = sum(1 for k in hs.kept_list(self.central, pid) if (k.get("text") or "").strip())
        raw = _n(raw)
        if raw <= 0 or home is None:
            return own + raw

        def stamp(path: Path) -> int:
            try:
                return os.lstat(path).st_mtime_ns
            except OSError:
                return 0

        key = (raw, stamp(Path(home).joinpath(*hs.BOTPORT_KEPT_DIR)), stamp(Path(home) / "SOUL.md"))
        seen = self.__dict__.setdefault("_versions_seen", {})
        if seen.get(pid, (None, 0))[0] != key:
            try:
                pts = self.engine_for(pid).restore_points(pid).get("points")
                pts = [p for p in (pts if isinstance(pts, list) else [])[:200]
                       if isinstance(p, dict) and valid_item_id(p.get("id"))]
                n = len(self._with_kept_texts(home, [{**p, "scope": p.get("scope") if p.get("scope") in
                                                      ("item", "profile") else "item"} for p in pts]))
            except Exception:
                n = raw  # botport cannot be asked right now: its own count stands
            seen[pid] = (key, n)
        return own + seen[pid][1]

    def _with_kept_texts(self, home: Optional[Path], points: list) -> list:
        """botport's earlier versions of the name and the description, with their text where it can be proven
        (see hermes_side.botport_kept_texts), so the page can show a version before it is put back. A version
        proven to be empty or the same as what the twin holds now is not listed: putting it back changes nothing.
        One whose text cannot be proven stays listed without text, as before."""
        try:
            known = hs.botport_kept_texts(home)
        except Exception:
            log.debug("grok-bot-twin: could not read botport's kept copies", exc_info=True)
            return points
        out, seen = [], set()
        for p in points:
            part, _, digest = p["id"].partition("@")
            text = known["versions"].get((part, digest[:12])) if p.get("scope") == "item" else None
            if text is None:
                out.append(p)
                continue
            now = known["now"].get(part)
            if not text.strip() or (now is not None and text.strip() == now.strip()) or (part, text) in seen:
                continue  # empty, what the twin holds now, or the same text under a second digest
            seen.add((part, text))
            out.append({**p, "text": hs.shown_version(text)})
        return out

    def restore(self, pid: str, point_id: Any) -> dict:
        pid = self.require_pair(pid)
        if not valid_item_id(point_id):
            raise BadInput("That earlier version is not one of this twin's.")
        if pid not in self.demo.pair_ids():
            own = self._restore_own(pid, point_id)  # the owner's pasted instructions: kept by the plugin itself
            if own is not None:
                return own
        real = pid not in self.demo.pair_ids()
        was = self._part_now(pid, point_id) if real else None
        data = self.engine_for(pid).restore(pid, point_id)
        if real and not valid_item_id(data.get("restore_point")) and was is not None:
            # botport keeps the replaced text but does not say under which version: found by its text, so the
            # page can offer Undo right after a put back
            data["restore_point"] = self._version_holding(pid, point_id, was)
        if valid_item_id(data.get("restore_point")) and real:
            self._note_point(pid, data["restore_point"], "put_back")
        self.invalidate()
        restored = [safe.clean_name(x, 140) for x in (data.get("restored") if isinstance(data.get("restored"), list)
                                                       else [])[:100] if isinstance(x, str)]
        self.emit("restored", pid, {"count": len(restored), "items": restored[:20]})
        out = {"restored": restored,
               "restore_point": data.get("restore_point") if valid_item_id(data.get("restore_point")) else None}
        if not restored:
            out["note"] = safe.clean_summary(data.get("note"), 200) or "Nothing to put back: Hermes already has that version."
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
                             "kind": safe.clean_name(e.get("kind"), 30),
                             "summary": plain_summary(safe.clean_summary(e.get("summary")))}
                            for e in entries[:limit] if isinstance(e, dict)]}

    def parity(self, pid: str) -> dict:
        """Compare with Grok: is each item the same in both places? Read from files; no model is called."""
        pid = self.require_pair(pid)
        eng = self.engine_for(pid)
        if eng is not self.demo:
            info = eng.info() if hasattr(eng, "info") else {}
            if "twin-parity" not in (info.get("features") or []):
                return {"passed": None, "total": None, "checks": [],
                        "note": "This version of botport cannot compare with Grok yet."}
        data = eng.parity(pid)
        raw = data.get("checks") if isinstance(data.get("checks"), list) else []
        checks = []
        mine = {} if eng is self.demo else next((p for p in self.status()["pairs"] if p["pair_id"] == pid), {})
        # the same reading of the twin's own settings that decides `setup_left`
        has_model = bool(mine) and "model" not in mine.get("setup_left", []) and bool(
            hs.profile_model(self._profile_home(mine.get("hermes", {}).get("profile"))))
        for c in raw[:200]:
            if not isinstance(c, dict):
                continue
            row = {"id": safe.clean_name(c.get("id"), 140), "name": safe.clean_name(c.get("name"), 100),
                   "pass": c.get("pass") is True, "detail": safe.clean_summary(c.get("detail"), 200)}
            if c.get("action") == "set_instructions":
                row["action"] = "set_instructions"
            if "model" in (row["id"], row["name"].lower()):
                if row["pass"] or has_model:
                    continue  # the model is listed only while none is chosen
                row.update(name="No model chosen for this twin yet", action="choose_model",
                           detail="Choose a model for this twin's Hermes bot.")
            row["why"] = parity_why(row)
            checks.append(row)
        passed, total = data.get("passed"), data.get("total")
        if eng is not self.demo:
            # instructions are the owner's own: one check, failing only while they still have to be brought over.
            # (When botport itself tracks instructions for this twin, its own check stands.)
            left = mine.get("setup_left", [])
            if not any(r["id"] == "persona.instructions" for r in checks):
                checks.insert(0, {"id": "persona.instructions", "name": "instructions", "pass": False,
                                  "action": "set_instructions",
                                  "detail": "you bring these over yourself: the Grok app does not keep the "
                                            "instructions on this computer"}
                              if "instructions" in left else
                              {"id": "persona.instructions", "name": "instructions", "pass": True,
                               "detail": "you keep these yourself; Hermes cannot compare them with Grok"})
            checks[0]["why"] = parity_why(checks[0])
            passed, total = sum(1 for r in checks if r["pass"]), len(checks)
        return {"passed": _n(passed) if isinstance(passed, int) else None,
                "total": _n(total) if isinstance(total, int) else None, "checks": checks}

    def checklist(self, pid: str) -> dict:
        pid = self.require_pair(pid)
        if pid in self.demo.pair_ids():
            out = self.demo.checklist(pid)
            out["blocked_by"] = self._blocked_by(pid, self.pair_status(pid))
            return out
        st = self.pair_status(pid)
        routines = list({str(b.get("item")): {"name": b.get("name")} for b in st["by_hand"]
                         if str(b.get("item", "")).startswith("routine:")}.values())
        # the twin's jobs by stable id: those recorded at create, plus any job an importer marked as made from
        # the bot's routines (never matched by a "<bot name>: " prefix)
        entry = self._registry().get(pid, {})
        recorded = entry.get("job_ids")
        recorded = recorded if isinstance(recorded, list) else []
        sw = entry.get("switched") if isinstance(entry.get("switched"), dict) else {}
        home = self._profile_home(st["hermes"]["profile"])
        jobs = importer.paused_jobs_for(home, recorded or None)
        known = list(dict.fromkeys([*recorded, *[j for j in (sw.get("job_ids") or []) if valid_id(j)]]))
        running = importer.running_jobs_for(home, known) if known else []
        return {"by_hand": st["by_hand"],
                "switch": {"grok_routines_to_turn_off": routines, "hermes_jobs_paused": jobs,
                           "hermes_jobs_resumed": running},
                "blocked_by": self._blocked_by(pid, st)}

    def _blocked_by(self, pid: str, st: dict) -> Optional[str]:
        """One plain sentence when moving this twin's routines has to wait, else None. Only two things hold it up:
        something changed in both places, or the twin cannot be checked. (A change waiting to be sent to Grok and a
        possible password in the description do not.)"""
        name = st.get("grok", {}).get("name") or "this twin"
        if st.get("state") == "error":
            setup = self.setup_brief()
            if setup["cause"] and setup["problem"] and not st.get("sample"):
                return f"{name} is not following Grok right now. {setup['problem']}"
            return f"First press Try again for {name}; Hermes cannot check it right now."
        if _n(st.get("conflicts")):
            found: list[dict] = []
            try:
                found = [c for c in self.conflicts(pid)["conflicts"] if c.get("name")]
            except EngineError:
                pass
            if found:
                first = found[0]
                what = (f"the {first['kind']} \"{first['name']}\"" if first.get("kind") and first["kind"] != first["name"]
                        else f"the {first['name']}")
                more = f" (and {len(found) - 1} more)" if len(found) > 1 else ""
                return f"First choose which version of {what} to keep{more}."
            return "First choose which version to keep where something changed in both places."
        return None

    @staticmethod
    def _profile_home(profile: Any) -> Optional[Path]:
        try:
            return store.profile_home(profile)
        except (ValueError, TypeError):  # an engine-provided profile name that is not a plain id is never a path
            return None

    def switch(self, pid: str, job_ids: Any = None, undo: Any = False) -> dict:
        """"Move routines to Hermes": turn on the chosen routines of the twin (they arrived switched off). What was turned
        on is recorded, so `undo=True` can switch off exactly those routines again."""
        pid = self.require_pair(pid)
        if not isinstance(undo, bool):
            raise BadInput(BAD_REQUEST)
        if undo:
            return self._switch_undo(pid)
        if not isinstance(job_ids, list) or len(job_ids) > 100 or not all(valid_id(j) for j in job_ids):
            raise BadInput("Choose the routines to turn on.")
        # the same rule as the page: no move while the twin still needs its owner (undo is always allowed)
        blocked = self._blocked_by(pid, self.pair_status(pid))
        if blocked:
            raise EngineError("blocked", blocked, "Then choose Move routines to Hermes again.")
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
                raise EngineError("bad_profile", "This twin's Hermes name is not valid.")
            res = {"resumed": [], "failed": []}
            stopped = False
            for jid in job_ids:
                if stopped:
                    res["failed"].append({"job_id": jid, "error": "not tried: an earlier one was refused or failed"})
                    continue
                if jid not in allowed:
                    res["failed"].append({"job_id": jid, "error": "not a switched-off routine of this twin"})
                    continue
                ok, err = importer.resume_cron(jid, prof, self.ctx)
                if ok:
                    res["resumed"].append(jid)
                else:  # a refusal (or failure) in Hermes' approval step stops the switch; it is never retried
                    res["failed"].append({"job_id": jid, "error": err})
                    stopped = True
            if res["resumed"]:
                with store.locked():
                    reg = self._registry()
                    if pid in reg:
                        old = reg[pid].get("switched") if isinstance(reg[pid].get("switched"), dict) else {}
                        reg[pid]["switched"] = {"at": old.get("at") if parse_iso(old.get("at")) else now_iso(),
                                                "job_ids": sorted(set(old.get("job_ids") or []) | set(res["resumed"]))[:200]}
                        self._save_registry(reg)
        self.invalidate()
        if res["resumed"]:
            self.emit("switched", pid, {"resumed": len(res["resumed"])})
        return res

    def _switch_undo(self, pid: str) -> dict:
        """Switch off again exactly the routines "Move routines to Hermes" turned on (never any other job)."""
        if pid in self.demo.pair_ids():
            got = self.demo.unswitch(pid)
            res = {"paused": [j for j in (got.get("paused") or [])[:100] if valid_id(j)], "failed": []}
        else:
            reg = self._registry()
            sw = reg.get(pid, {}).get("switched")
            ids = [j for j in (sw.get("job_ids") or []) if valid_id(j)] if isinstance(sw, dict) else []
            prof = self.pair_status(pid)["hermes"]["profile"]
            if not valid_id(prof) or prof.startswith("-"):
                raise EngineError("bad_profile", "This twin's Hermes name is not valid.")
            running = {j["job_id"] for j in importer.running_jobs_for(self._profile_home(prof), ids)}
            res = {"paused": [], "failed": []}
            left = []
            stopped = False
            for jid in ids:
                if jid not in running:
                    continue  # already off: nothing to do
                if stopped:
                    res["failed"].append({"job_id": jid, "error": "not tried: an earlier one was refused or failed"})
                    left.append(jid)
                    continue
                ok, err = importer.pause_cron(jid, prof, self.ctx)
                if ok:
                    res["paused"].append(jid)
                else:
                    res["failed"].append({"job_id": jid, "error": err})
                    left.append(jid)
                    stopped = True
            with store.locked():
                reg = self._registry()
                if pid in reg:
                    if left and isinstance(sw, dict):
                        reg[pid]["switched"] = {"at": sw.get("at"), "job_ids": left}
                    else:
                        reg[pid].pop("switched", None)
                    self._save_registry(reg)
        self.invalidate()
        if res["paused"]:
            self.emit("switch_undone", pid, {"paused": len(res["paused"])})
        return res

    def send_to_grok(self, pid: str) -> dict:
        """"Send to Grok": botport has no separate pack command, so this is a sync of the pair (approved
        changes then travel through the sync bot when it is connected). The demo's pretend Grok side
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
        if eng is not self.demo:
            for r in pairs:
                if r["restore_point"]:
                    self._note_point(pid, r["restore_point"], "send")
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
            raise BadInput("That item is not one of this twin's.")
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
            self._drop_skipped([sync])
            self._emit_sync([sync])
        self.emit("approved", pid, {"count": len(approved)})
        return {"approved": approved, "sync": sync}

    def mirror(self, pid: str, pair_folder: Any = None, off: Any = False, rotate_key: Any = False) -> dict:
        """Skill sync (optional): connect the twin to the owner's sync bot in Grok through a shared folder, turn
        it off, or replace the twin's pass (its key file)."""
        pid = self.require_pair(pid)
        if not isinstance(off, bool) or not isinstance(rotate_key, bool):
            raise BadInput(BAD_REQUEST)
        eng = self.engine_for(pid)
        if rotate_key:
            if off or pair_folder is not None:
                raise BadInput("Replace the pass on its own, without a folder.")
            if eng is self.demo:
                raise BadInput("The sample twin has no pass.")
            data = eng.mirror(pid, None, False, rotate_key=True)
            self._skill_sync_rekeyed(pid)
            self.invalidate()
            self.emit("mirror", pid, {"key_rotated": data.get("key_rotated") is True})
            return {"pair_id": pid, "key_rotated": data.get("key_rotated") is True,
                    "reveal": self._reveal(pid, data),
                    "note": "A new pass (a small file) is in place. If skill sync is on, attach the new pass in the "
                            "sync bot's chat (never paste what is in it); the old one no longer works."}
        folder: Optional[str] = None
        if off:
            if pair_folder is not None:
                raise BadInput("Choose a folder or turn skill sync off, not both.")
        elif eng is not self.demo:
            folder = self._check_pair_folder(pair_folder)
        # (the sample has no real sync bot, so a folder is not used there)
        data = eng.mirror(pid, folder, off)
        if off:  # the sync bot's signed list of twins no longer names this one
            self._skill_sync_dropped(pid)
        steps = [{"id": safe.clean_name(s.get("id"), 40), "what": safe.clean_summary(s.get("what"), 400)}
                 for s in (data.get("steps") or [])[:10] if isinstance(s, dict)]
        charter = data.get("charter") if isinstance(data.get("charter"), str) else ""
        self.invalidate()
        self.emit("mirror", pid, {"connected": bool(data.get("connected"))})
        return {"pair_id": pid, "connected": data.get("connected") is True, "steps": steps,
                "charter": _shown_text(charter[:20000]),
                "reveal": self._reveal(pid, data) if eng is not self.demo and not off
                else {"key_file": None, "guide_file": None}}

    def _reveal(self, pid: str, data: Any) -> dict:
        """Absolute paths of this twin's pass (its key file) and of the guide file to attach in the sync bot's chat,
        for "Show in Finder". Only files inside the plugin's own data folder that exist and are real files;
        never the key's content."""
        base = self.central / "botport-state"

        def inside(label: Any) -> Optional[str]:
            if not isinstance(label, str) or not label.startswith("state:") or len(label) > 300:
                return None
            rel = label[len("state:"):]
            if not rel or rel.startswith(("/", "\\")) or ".." in rel.replace("\\", "/").split("/"):
                return None
            cand = base / rel
            try:
                st = os.lstat(cand)
            except OSError:
                return None
            if not stat.S_ISREG(st.st_mode) or not store.contained(base, cand.resolve()):
                return None
            return str(cand)

        d = data if isinstance(data, dict) else {}
        key = inside(d.get("pair_key")) or inside(f"state:pairs/{pid}.key")
        briefs = d.get("briefs")
        labels = list(briefs.values()) if isinstance(briefs, dict) else (briefs if isinstance(briefs, list) else [])
        guide = next((inside(b) for b in labels[:10] if isinstance(b, str) and b.endswith("AGENT-MIRROR.md")
                      and inside(b)), None) or inside(f"state:pairs/{pid}.brief/AGENT-MIRROR.md")
        return {"key_file": key, "guide_file": guide}

    def _check_pair_folder(self, value: Any) -> str:
        """The shared folder for skill sync: an existing real folder (not a link), given as an absolute path,
        outside the Hermes home and the plugin data folder."""
        if not isinstance(value, str) or not value.strip() or len(value) > 1024 or "\x00" in value:
            raise BadInput("Choose a folder that exists.")
        raw = Path(value.strip()).expanduser()
        if not raw.is_absolute():
            raise BadInput("Choose a folder that exists.")
        if raw.is_symlink():
            raise BadInput("Choose the real folder, not a shortcut to it.")
        if not raw.is_dir():
            raise BadInput("Choose a folder that exists.")
        for part in [raw, *raw.parents]:  # botport refuses a link on the way that root does not own
            try:
                st = part.lstat()
            except OSError:
                raise BadInput("Choose a folder that exists.")
            if stat.S_ISLNK(st.st_mode) and st.st_uid != 0:
                raise BadInput("Choose the real folder, not a shortcut to it.")
        real = raw.resolve()
        for base in (store.hermes_root(), self.central, self.central / "botport-state"):
            if store.contained(base, real) or store.contained(real, base):
                raise BadInput("Choose a folder outside Hermes' own folders.")
        return str(real)

    def remove(self, pid: str, dry_run: Any = False) -> dict:
        """Stop twinning one bot. The bot in Grok and its Hermes bot stay (and so do routines already switched on
        there); the link and the earlier versions kept for undo go. The lists name only what exists now.
        Removing the last sample twin ends the sample completely."""
        pid = self.require_pair(pid)
        if not isinstance(dry_run, bool):
            raise BadInput(BAD_REQUEST)
        blocked = False  # botport is missing or not verified: what stays and what goes can still be said
        try:
            eng = self.engine_for(pid)
        except EngineError as exc:
            if not dry_run or exc.code != "botport_missing":
                raise
            eng, blocked = None, True
        sample = eng is self.demo
        try:  # as it is right now: the plan must be true at the moment the owner reads it
            st = next((p for p in self.status(fresh=True)["pairs"] if p["pair_id"] == pid), None) or {}
        except EngineError:
            st = {}
        # botport already let go of this twin (Hermes was stopped in the middle of Stop twinning, after botport's
        # half and before the plugin's own): it is not asked again, it would only answer that there is no such
        # twin. What is left is the plugin's half below, so the row's own advice, "Stop twinning it", works.
        let_go = not sample and (st.get("error") or {}).get("code") == "unknown_twin"
        if eng is None or let_go:
            data: dict = {}
        else:
            try:
                data = eng.remove(pid, dry_run)
            except EngineError as exc:
                if not dry_run or sample or exc.code not in ("botport_missing", "untrusted_binary", "not_found"):
                    raise
                data, blocked = {}, True
        removed = [safe.clean_name(x, 200) for x in (data.get("removed") if isinstance(data.get("removed"), list)
                                                      else [])[:100] if isinstance(x, str)]
        if not dry_run:
            reg = self._registry()
            entry = reg.pop(pid, None)
            if entry is not None:
                self._save_registry(reg)
            if not sample:
                self._skill_sync_dropped(pid)
            if not sample:  # which Hermes bot this Grok bot had, so twinning it again picks that one up
                self._remember_former((st.get("grok") or {}).get("id"), (st.get("hermes") or {}).get("profile"))
                from . import fleet as _fleet  # its Hermes bot stays, without the fleet note and the plugin in it
                _fleet.untend_profile((st.get("hermes") or {}).get("profile"))
            self._drop_notes([pid])
            if not sample:  # what the owner chose in its "What syncs" panel goes with it
                try:
                    from . import sync_panel as _sync_panel
                    _sync_panel.forget_twin(pid)
                except Exception:
                    log.warning("grok-bot-twin: could not drop a removed twin's sync choices", exc_info=True)
                try:  # and so do its session briefs (each file by its exact name) and their bookkeeping
                    from . import briefs as _briefs
                    _briefs.forget(pid)
                except Exception:
                    log.warning("grok-bot-twin: could not delete a removed twin's session briefs", exc_info=True)
                # the fresh status may have lost the Hermes side (botport let go first): the registry still knows it
                was = entry.get("hermes") if isinstance(entry, dict) else None
                prof = (st.get("hermes") or {}).get("profile") or (was.get("profile") if isinstance(was, dict) else None)
                if prof:
                    try:  # its memory-facts ledger, restore points and Grok stand-in (MEMORY.md / USER.md stay)
                        from . import mem_facts as _mem_facts
                        _mem_facts.forget(prof)
                    except Exception:
                        log.warning("grok-bot-twin: could not drop a removed twin's memory-facts records", exc_info=True)
            self._checked.pop(pid, None)
            self.invalidate()
            watch.poke()  # with the last real twin gone, nothing is watched any more
            self.emit("removed", pid, {"count": len(removed)})
        out = {"pair_id": pid, "dry_run": dry_run, "removed": removed, "sample": sample,
               "profile_kept": not sample}  # a sample twin never had a Hermes profile of its own
        if sample:
            out.update(stays=list(SAMPLE_STAYS), goes=list(SAMPLE_GOES), again="", skill_sync=False)
            return out
        stays = list(STOP_STAYS)
        on = _n((st.get("switched") or {}).get("routines_on")) if isinstance(st.get("switched"), dict) else 0
        if on:  # routines already moved keep running in the Hermes bot: say so before the owner agrees
            stays.append(f"Its {plural(on, 'routine')} {'keeps' if on == 1 else 'keep'} running in Hermes.")
        # (without botport the earlier versions cannot be counted: the line is said, since they would go too)
        goes = [STOP_GOES_LINK] + ([STOP_GOES_VERSIONS] if blocked or _n(st.get("restore_points")) else [])
        copies = self.fleet_copies_line(pid)  # on a computer that shares: the twin leaves the linked computers too
        if copies:
            goes.append(copies)
        # twinning the same bot again: the plugin remembers which Hermes bot it had and names it to botport, so
        # the twin picks that Hermes bot up again (see _former_profile). Only a twin that lives in the default
        # Hermes profile cannot be picked up that way.
        gid, prof = (st.get("grok") or {}).get("id"), (st.get("hermes") or {}).get("profile")
        same = valid_id(gid) and valid_id(prof) and prof != "default"
        out.update(stays=stays, goes=goes, again=STOP_AGAIN_SAME if same else STOP_AGAIN_NEW,
                   skill_sync=(st.get("mirror_bot") or {}).get("connected") is True)
        if blocked:  # the lists are true; the act itself needs botport back first
            out["note"] = STOP_NEEDS_BOTPORT
        return out

    def _drop_notes(self, pids: list) -> None:
        """Forget the plugin's own per-twin notes (kept in Hermes only, pasted into Grok, earlier versions of
        instructions, ...) of these twins."""
        with store.locked():
            for name in PER_TWIN_NOTES:
                ds = store.read_json(self.central / name, {}) or {}
                if isinstance(ds, dict) and any(ds.pop(pid, None) is not None for pid in list(pids)):
                    store.atomic_write_json(self.central / name, ds)

    # ---- Remove Grok Bot Twin (before the plugin itself is turned off or removed) -----------------------------
    def uninstall(self, dry_run: Any = False) -> dict:
        """Remove all of Grok Bot Twin's data: every twin's record (each twin's pass included), the sample, earlier
        versions, caches, Hermes' copy of botport and its check record. The owner's Grok bots, Hermes bots
        (profiles) and chats are never touched. `dry_run=True` only reports. The plugin's own code is removed by Hermes afterwards
        (`next`)."""
        if not isinstance(dry_run, bool):
            raise BadInput(BAD_REQUEST)
        if not dry_run:  # while the inbox pass is still here: the sync bot's list is closed, signed
            self._skill_sync_closed()
        reg = self._registry()
        demo_ids = set(self.demo.pair_ids())
        real = [pid for pid in reg if pid not in demo_ids]
        # (taken now: the registry is gone by the time the twins' Hermes bots are tidied below)
        twin_profiles = [h.get("profile") for h in (reg[pid].get("hermes") for pid in real) if isinstance(h, dict)]
        sample = self.demo.exists()
        bp = self.botport()
        skill_sync: list[str] = []
        try:
            cached = self.status(fresh=True)
        except EngineError:
            cached = self.cached_status(compute=False) or {}
        for p in cached.get("pairs", []):
            if not p.get("demo") and (p.get("mirror_bot") or {}).get("connected"):
                skill_sync.append(p["grok"]["name"])
        removed: list[str] = []
        via_helper = True
        for pid in real:
            try:
                if bp is None:
                    raise EngineError("botport_missing", HELPER_MISSING)
                data = bp.remove(pid, dry_run)
                removed += [safe.clean_name(x, 200) for x in _lst(data, "removed", 100) if isinstance(x, str)]
            except EngineError:
                via_helper = False  # the plugin's own state folder (links and key files) is deleted below anyway
                removed += [f"state:pairs/{pid}.toml", f"state:pairs/{pid}.key"]
        try:
            entries = sorted(e.name for e in self.central.iterdir() if e.name != ".lock")
        except OSError:
            entries = []
        removed += [f"data:{safe.clean_name(n, 80)}" for n in entries]
        # the list names only what exists now
        goes = []
        if real:  # how many, and which: the names as the page shows them
            by_id = {p.get("pair_id"): (p.get("grok") or {}).get("name") for p in cached.get("pairs", [])
                     if isinstance(p, dict) and not p.get("demo")}
            goes.append(goes_twins_line([by_id.get(pid) for pid in real], len(real)))
        with_versions = sum(1 for p in cached.get("pairs", []) if not p.get("demo") and _n(p.get("restore_points")))
        if with_versions:
            goes.append(goes_versions_line(with_versions))
        if sample:
            goes.append(GOES_SAMPLE)
        if os.path.lexists(self.central / "helper"):  # the copy made from the owner's download
            goes.append(GOES_BOTPORT)
        elif os.path.lexists(self.central / "botport-bin"):  # only the working copy of a botport that sits elsewhere
            goes.append(GOES_WORKING_COPY)
        goes += self.fleet_leaving_lines()  # sharing with, or the link to, the owner's other machines
        if not goes and entries:  # nothing of the above, but the folder is not empty (the activity log, say)
            goes.append(GOES_NOTES)
        from . import bgagent
        try:
            background = bgagent.status()["installed"]
        except Exception:
            background = False
        if background:
            goes.append(GOES_BACKGROUND)
        routines_on = sum(_n((p.get("switched") or {}).get("routines_on")) for p in cached.get("pairs", [])
                          if not p.get("demo") and isinstance(p.get("switched"), dict))
        out = {"dry_run": dry_run, "twins": len(real), "sample": sample, "skill_sync": skill_sync[:50],
               "stays": list(ALL_STAYS) + self.fleet_staying_lines(), "goes": goes, "removed": removed[:300],
               "next": UNINSTALL_NEXT,
               # routines switched on in Hermes over all real twins: they keep running in their Hermes bots
               "routines_on": routines_on}
        if skill_sync:
            out["goes_note"] = SKILL_SYNC_LEFTOVER
        if real and not via_helper:
            out["note"] = ("botport could not be used, so files it keeps inside each Hermes bot's own "
                           "folder stay there. They are small and harmless.")
        if dry_run:
            return out
        from . import awareness
        if background:  # first: it must not make new files while the data folder is emptied
            try:
                bgagent.remove()
            except Exception:
                log.warning("grok-bot-twin: the background helper could not be removed", exc_info=True)
        self.fleet_shutdown_for_uninstall()
        # every real twin's Hermes bot stays, as an ordinary one: without the fleet note and without the plugin's
        # switch and copy in it. Whatever this computer's part was (sharing, or never shared at all: then the
        # fleet code above has nothing to go by and left both in)
        from . import fleet as _fleet
        for prof in twin_profiles:
            _fleet.untend_profile(prof)
        with store.locked():
            if sample:
                with contextlib.suppress(EngineError):
                    self.demo.remove()
            for name in entries:
                path = self.central / name
                try:
                    if path.is_symlink() or not path.is_dir():
                        path.unlink(missing_ok=True)
                    else:
                        store.remove_tree(self.central, path)
                except OSError:
                    log.warning("grok-bot-twin: could not remove %s from the plugin data folder", name)
            with contextlib.suppress(OSError):
                (self.central / ".lock").unlink()
        with contextlib.suppress(OSError):  # Windows cannot delete the lock file while it is held: once more, after
            (self.central / ".lock").unlink()
        self._botport = None
        self._status = None
        self.last_check_at = None
        self._checked = {}
        awareness.invalidate()
        watch.poke()
        return out

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
            raise BadInput(BAD_REQUEST)
        if enabled and not config.get("digest_to_grok"):
            raise BadInput('Turn on "Allow an activity digest to Grok" in the plugin\'s settings first.')
        if titles and not config.get("digest_titles"):
            raise BadInput('Turn on "Include short titles in the digest" in the plugin\'s settings first.')
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
        """The built-in sample (invented bots only). `start` always ends with the sample twin present; it lives
        under the reserved sample label and never under one of the owner's profiles, unless the `demo_profile`
        setting or `profile` names one on purpose."""
        if action not in DEMO_ACTIONS:
            raise BadInput("That is not one of the sample's actions.")
        if profile is not None and not valid_id(profile):
            raise BadInput("That is not a valid Hermes profile name.")
        did = ""
        created = False
        if action == "start":
            prof = profile or config.get("demo_profile") or SAMPLE_PROFILE
            res = self.demo.start(prof)
            created = bool(res["created"])
            did = "Sample twin ready"
            self.emit("demo", None, {"action": "start"})
        elif action == "remove":
            gone = self.demo.exists()
            # every sample file goes, the plugin's own notes about the sample twins included (a skill kept in
            # Hermes only must be offered again the next time the sample starts)
            self._drop_notes(self.demo.remove() or [])
            did = "Sample removed. Nothing of yours was changed" if gone else "No sample to remove"
            self.emit("demo", None, {"action": "remove"})
        else:
            if not self.demo.exists():
                raise EngineError("demo_missing", "Start the sample first.",
                                  "On the Twins page press See a sample, or run hermes twin demo.")
            if action in ("hide-real", "show-real"):
                hidden = self.demo.set_hide_real(action == "hide-real")
                did = ("Your real bots are hidden while the sample is on" if hidden
                       else "Your real bots are shown again")
            else:
                did = self.demo.simulate(action)["did"]
            self.emit("demo", None, {"action": action})
        self.invalidate()
        st = self.status(fresh=True, allow_engine=False)
        # that answer was built without asking the helper (a sample action must not wait for it), so it carries
        # no helper facts: it is not kept as "the" status, or the page would lose them for the next few seconds
        self.invalidate()
        return {"ok": True, "did": did, "created": created, "sample": self.sample_state(),
                "pairs": [p for p in st["pairs"] if p.get("demo")]}

    # ---- doctor ----------------------------------------------------------------------------------
    def doctor(self, probe_importers: bool = True) -> dict:
        checks = []
        info = self.engine_info()
        bp = self.botport()
        if bp is None or info["available"]:
            checks.append({"id": "botport", "ok": bool(info["available"]),
                           "message": f"botport {info['version']} is ready" if info["available"]
                           else (info["problem"] or HELPER_MISSING),
                           "fix": "" if info["available"] else (INSTALL_HINT if bp is None else TRUST_HINT)})
        if bp is not None:  # botport's own checks, incl. trust (SHA-256) and attestation
            try:
                for c in _lst(bp.doctor(), "checks")[:30]:
                    if isinstance(c, dict):
                        # the plugin's own lines (`_own`) are shown as written: a file path in them is not a
                        # secret and must not be masked. Whatever botport itself reports is cleaned as untrusted.
                        clean = (lambda v, n: safe.cap(safe._strip(v), n)) if c.get("_own") is True \
                            else safe.clean_summary
                        checks.append({"id": safe.clean_name(c.get("id"), 60), "ok": bool(c.get("ok")),
                                       "message": clean(c.get("message"), 300),
                                       "fix": clean(c.get("fix"), 400)})
            except EngineError as exc:
                checks.append({"id": "botport_doctor", "ok": False, "message": exc.message, "fix": exc.hint})
        try:
            probe = self.central / ".write-test"
            store.atomic_write_text(probe, "ok")
            probe.unlink()
            checks.append({"id": "data_dir", "ok": True, "message": "Grok Bot Twin can save its data", "fix": ""})
        except OSError:
            checks.append({"id": "data_dir", "ok": False, "message": "Grok Bot Twin cannot save its data",
                           "fix": "Check permissions of plugin-data/grok-bot-twin in your Hermes home"})
        app_here = self.grok_app_found()
        real = [p for p in self._registry() if p not in self.demo.pair_ids()]
        link = self._doctor_link()  # on a computer linked to the one with the Grok app: how that link is doing
        if link:
            checks.insert(0, link["check"])
        checks.append({"id": "grok_app", "ok": app_here,
                       "message": "Grok app found" if app_here else "Grok app not found on this computer",
                       "fix": "" if app_here else
                       ("Not needed here: this computer's twins come over the link" if link else
                        "Open the Grok app once and sign in, then try again. Are your Grok bots on another "
                        "computer? Then link this one to it instead: hermes twin link CODE")})
        tools = self._tool_checks()
        if not app_here and not real:
            # gh only ever verifies botport, and botport is only for the computer with the Grok app: on any other
            # computer "install gh" or "get botport" would be advice for something that is never used there
            for c in tools:
                if c["id"] == "gh_path" and not c["ok"]:
                    c["fix"] = "Not needed here: gh is only used to verify botport, on the computer with the Grok app"
            for c in checks:
                if c["id"] == "botport" and not c["ok"]:
                    c["fix"] = "Not needed here: botport is only for the computer with the Grok app"
        checks += tools
        try:  # Windows only (None everywhere else): installed from an administrator window
            from . import bgagent_windows
            closed = bgagent_windows.admin_install_problem(note=True)
        except Exception:
            closed = None
        if closed:
            checks.append({"id": "windows_admin_install", "ok": False, "message": closed[0].rstrip("."),
                           "fix": closed[1]})
        checks.append({"id": "demo", "ok": True, "message": "Sample is on" if self.demo.exists() else "Sample is off",
                       "fix": "" if self.demo.exists() else "To look around first: hermes twin demo"})
        from . import timer
        checks.append({"id": "timer", "ok": True, "message": timer.describe(), "fix": ""})
        if not real:
            # No real twin yet: botport is only needed for real twins, so its checks are advice, not failures.
            for c in checks:
                if c["id"] not in ("data_dir", "fleet_link", "windows_admin_install") and not c["ok"]:
                    c["optional"] = True
        out = {"checks": checks, "grok_app_here": app_here}
        if link:
            out["linked_to"] = link["name"]
        return out

    def _doctor_link(self) -> Optional[dict]:
        """On a computer that follows another one: one doctor line saying how the link is doing, in the words
        `hermes twin fleet` uses. None on every other computer, and when the fleet cannot be read (doctor must
        never fail because of it)."""
        try:
            f = self.fleet()
            if not isinstance(f, dict) or f.get("role") != "follower":
                return None
            from . import fleet_cli
            lines = fleet_cli.describe(f)
            src = f.get("source") or {}
            problem = src.get("problem") if isinstance(src.get("problem"), dict) else {}
            ok = fleet_cli._in_sync(f)
            return {"name": str(src.get("name") or "the other computer"),
                    "check": {"id": "fleet_link", "ok": ok, "message": str(lines[0]).rstrip("."),
                              "fix": "" if ok else (str(problem.get("hint") or "").strip()
                                                    or "See what it says: hermes twin fleet")}}
        except Exception:
            return None

    @staticmethod
    def _tool_checks() -> list[dict]:
        """gh and hermes, the two other programs the plugin may run: their absolute real paths and the result of
        the same ownership/permission check as botport's (a failing one is refused when it would run)."""
        out = []
        gh, problem = find_gh()
        out.append({"id": "gh_path", "ok": gh is not None and not problem,
                    "message": (f"gh: {_home_label(gh)} (owner and permissions checked: ok)" if gh and not problem
                                else tool_problem_text("gh", gh, problem)),
                    "fix": "" if gh and not problem else
                    ("Install GitHub's command-line tool (gh) and run gh auth login: verifying botport "
                     "needs it" if gh is None else "Reinstall gh where only you (or root) can change it")})
        chk = importer.hermes_check()
        path = chk.get("path")
        via = " (Hermes' own Python, -P -m hermes_cli.main)" if chk.get("via") == "interpreter" else ""
        out.append({"id": "hermes_path", "ok": bool(path) and not chk.get("problem"),
                    "message": (f"hermes: {_home_label(path)}{via} (owner and permissions checked: ok)"
                                if path and not chk.get("problem")
                                else tool_problem_text("hermes", path, chk.get("problem"))),
                    "fix": "" if path and not chk.get("problem") else
                    "Move routines to Hermes turns them on with the hermes command; install Hermes where only you (or "
                    "root) can change it"})
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


def _register_panel_rows() -> None:
    """The "What syncs" panel's name, description and routines rows learn Hermes -> Grok (when the panel is there)."""
    from .h2g_service import register_panel
    try:
        register_panel()
    except Exception:  # the panel keeps its own default rows
        logging.getLogger(__name__).debug("grok-bot-twin: Hermes -> Grok rows not registered", exc_info=True)


_register_panel_rows()


def reset_service() -> None:
    """Tests: drop the singleton (e.g. after HERMES_HOME changes)."""
    global _service
    with _service_lock:
        _service = None
