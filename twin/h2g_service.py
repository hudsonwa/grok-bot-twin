"""Hermes -> Grok (h2g.py) inside the plugin: the service's methods, the terminal command and the panel rows.

What this file adds to h2g.py is only where things are on this computer:

- the twin's Hermes side: its name and description (botport's lines in SOUL.md), its instructions (the owner's
  pasted block, else botport's `persona` section) and its routines (the twin's own cron jobs: the ids recorded
  for it, or a job an importer marked; never another bot's job);
- the Grok bot's id (the twin's record);
- the writer (`_h2g_writer`). Until the Mirror's way of changing a bot is wired in, a real twin gets
  `NoMirrorWriter`, which says "the Mirror is not set up" and changes nothing. The sample twin gets the pretend
  Grok account (FakeGrokWriter in the plugin's data folder, filled from the sample's own invented bot).
- pausing and switching on the twin's own Hermes job for a routine, and checking afterwards that it really is.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from . import config, h2g, h2g_mirror, hermes_side as hs, importer, skill_sync
from .engine import EngineError
from .model import ago, now_iso, valid_id

log = logging.getLogger(__name__)

FAKE_PATH = h2g.FAKE_FILE
SAMPLE_PROMPT = "Sample routine: {name}. Say one friendly line."

# Hermes -> Grok is off for every twin until the owner allows that twin (per twin, per item); there is no switch for
# the whole plugin any more. `hermes twin to-grok beta ...` is still understood, to say so.
NO_BETA = ("Hermes to Grok is no longer a beta: it is off for every twin until you allow it "
           "(hermes twin to-grok TWIN allow).")
GRANT_WHERE = h2g.GRANT_WHERE


def owner_only(own_page: bool = False) -> None:
    """Changes in Grok are the owner's own: never from a request from another computer, never from a command a
    chat started. `own_page`: the Twins page's own route inside the running Hermes (see tasks._chat_numbers)."""
    from . import tasks
    if tasks.asked_turn(env=not own_page):  # (the chat turn of a request from another computer never changes a twin's setup)
        raise EngineError("refused", "A request from another computer cannot change a Grok bot.",
                          "Do it yourself, on the Twins page or in your own terminal.")
    if not own_page and tasks._chat_numbers():  # a command a chat started: the owner must see the preview and say yes
        raise EngineError("refused", "Only you can allow or make changes in Grok, not a chat.",
                          "Look at the preview and apply it in your own terminal or on the Twins page.")


class NoMirrorWriter:
    """No way to change Grok yet: every answer says so honestly, nothing is ever changed."""

    kind = "none"

    def read(self, bot: str) -> Optional[dict]:
        return None

    def apply(self, bot: str, ops: list, change_id: str) -> dict:
        return {"state": "failed", "done": [], "failed": list(range(len(ops))), "at": None,
                "message": "The Mirror is not set up, so Grok was not changed."}

    def poll(self, change_id: str, n: Optional[int] = None) -> Optional[dict]:
        return None

    def permission(self, bot: str) -> str:
        return "not_set_up"


def _instructions(profile_home: Optional[Path]) -> str:
    """The instructions of the twin's Hermes bot: the owner's pasted block when there is one, else botport's own
    `persona` section ("" when neither)."""
    if profile_home is None:
        return ""
    text = hs._read_below_root(Path(profile_home) / "SOUL.md", hs.SOUL_CAP)
    if not text:
        return ""
    m = hs._OWN_RE.search(text)
    if m and m.group(1).strip():
        return m.group(1)
    sections = {name: body for name, body in hs._BOTPORT_RE.findall(hs._FLEET_RE.sub("", text))}
    return sections.get("persona") or ""


def _schedule(j: dict) -> str:
    s = j.get("schedule")
    if isinstance(s, dict):
        s = s.get("expr") or s.get("display") or ""
    if not isinstance(s, str) or not h2g.CRON_RE.match(s.strip()):
        s = j.get("schedule_display") if isinstance(j.get("schedule_display"), str) else ""
    return " ".join(str(s or "").split())


def _enabled(j: dict) -> bool:
    return bool(j.get("enabled", True)) and j.get("state") != "paused"


class H2GMixin:
    """TwinService methods for Hermes -> Grok. Errors are EngineErrors (code + owner words)."""

    h2g_test_writer: Any = None  # a test may set a writer for real twins (never set by the plugin itself)

    # ---- where things are -----------------------------------------------------------------------------------------
    def _h2g_is_sample(self, pid: str) -> bool:
        try:
            return pid in self.demo.pair_ids()
        except Exception:
            return False

    def _h2g_writer(self, pid: Optional[str] = None) -> Any:
        """The one place that says how Grok is changed: the sample's pretend Grok account, a test's writer, or
        (until the Mirror's mechanism is wired in) none at all."""
        if pid is not None and self._h2g_is_sample(pid):
            w = h2g.FakeGrokWriter(self.central / FAKE_PATH)
            self._h2g_seed_sample(w, pid)
            return w
        if self.h2g_test_writer is not None:
            return self.h2g_test_writer
        if skill_sync.shared_root(self.central) is not None:  # the Mirror is reached through skill sync's folder
            return h2g_mirror.MirrorFolderWriter(self.central, name_of=self._h2g_grok_name,
                                                 local_read=self._h2g_local_read)
        return NoMirrorWriter()

    def _h2g_grok_side(self, bot: str) -> Optional[dict]:
        """The bot as botport last read it from the Grok app (the cached status, no engine call). Runs inside
        store.locked() (H2G's apply/undo/resolve), so never status(): its lock comes first (store.not_inside)."""
        try:
            for p in (self.cached_status(compute=False) or {}).get("pairs") or []:
                g = p.get("grok") or {}
                if g.get("id") == bot and g.get("present", True):
                    return g
        except (EngineError, OSError, ValueError):
            return None
        return None

    def _h2g_grok_name(self, bot: str) -> str:
        return str((self._h2g_grok_side(bot) or {}).get("name") or "")

    def _h2g_local_read(self, bot: str) -> Optional[dict]:
        g = self._h2g_grok_side(bot)
        return None if g is None else {"name": str(g.get("name") or ""), "description": str(g.get("description") or "")}

    def _h2g_seed_sample(self, w: h2g.FakeGrokWriter, pid: str) -> None:
        """The sample's pretend Grok account starts as the sample's own invented Grok bot (and allows the Mirror)."""
        try:
            t = self.demo._twin(pid)
            gid = t.get("grok_id")
            if not valid_id(gid) or w.permission(gid) != "not_set_up":
                return
            g = self.demo._grok(gid) or {}
            from .demo import _GROK_FIXTURES  # the sample's invented schedules (the same ones its Hermes jobs got)
            sched = g.get("routine_schedules") or (_GROK_FIXTURES.get(gid) or {}).get("routine_schedules") or {}
            routines = [{"key": h2g.routine_key(r), "name": r, "schedule": sched.get(r, "0 9 * * *"),
                         "prompt": SAMPLE_PROMPT.format(name=r), "enabled": True}
                        for r in (g.get("routines") or []) if isinstance(r, str)]
            w.put_bot(gid, name=str(g.get("name") or ""), description=str(g.get("description") or ""),
                      instructions=str(g.get("instructions") or ""), routines=routines, allowed=True)
        except EngineError:
            pass

    def _h2g_bot(self, pid: str) -> str:
        if self._h2g_is_sample(pid):
            try:
                gid = self.demo._twin(pid).get("grok_id")
                return gid if valid_id(gid) else ""
            except EngineError:
                return ""
        g = (self._registry().get(pid) or {}).get("grok") or {}
        gid = g.get("id")
        return gid if isinstance(gid, str) and gid else ""

    def _h2g_jobs(self, pid: str) -> tuple:
        """(profile, profile home, [the twin's own cron jobs])."""
        entry = self._registry().get(pid) or {}
        prof = ((entry.get("hermes") or {}).get("profile")) or ""
        home = self._profile_home(prof) if prof else None
        ids = {j for j in (entry.get("job_ids") or []) if isinstance(j, str) and valid_id(j)}
        jobs = [j for j in importer._load_jobs(home)
                if importer._job_id(j) and (importer._job_id(j) in ids or importer._imported(j))]
        return prof, home, jobs

    def _h2g_hermes(self, pid: str) -> dict:
        if self._h2g_is_sample(pid):
            t = self.demo._twin(pid)
            prefix = ""
            try:
                prefix = str((self.demo._grok(t.get("grok_id")) or {}).get("name") or "") + ": "
            except Exception:
                pass
            routines = []
            for j in t.get("jobs") or []:
                name = str(j.get("name") or "")
                name = name[len(prefix):] if prefix and name.startswith(prefix) else name
                routines.append({"name": name, "schedule": j.get("schedule"), "enabled": not j.get("paused"),
                                 "prompt": j.get("prompt") or SAMPLE_PROMPT.format(name=name)})
            return {"name": t.get("name") or "", "description": t.get("description") or "",
                    "instructions": t.get("instructions") or "", "routines": routines}
        prof, home, jobs = self._h2g_jobs(pid)
        facts = hs.soul_facts(home)
        routines = [{"name": j.get("name"), "schedule": _schedule(j), "prompt": j.get("prompt"),
                     "enabled": _enabled(j)} for j in jobs]
        return {"name": facts["name"], "description": facts["description"], "instructions": _instructions(home),
                "routines": routines}

    def _h2g_switch(self, pid: str, rkey: str, on: bool) -> bool:
        """Pause (on=False) or switch on the twin's Hermes job for routine `rkey`; True only when the job is in that
        state afterwards (read back, never assumed). No job for it in Hermes counts as paused."""
        if self._h2g_is_sample(pid):
            t = self.demo._twin(pid)
            prefix = str((self.demo._grok(t.get("grok_id")) or {}).get("name") or "") + ": "
            hit = False
            for j in t.get("jobs") or []:
                name = str(j.get("name") or "")
                if h2g.routine_key(name[len(prefix):] if name.startswith(prefix) else name) == rkey:
                    j["paused"] = not on
                    hit = True
            if hit:
                self.demo._write_twin(pid, t)
            return hit or not on
        prof, home, jobs = self._h2g_jobs(pid)
        mine = [j for j in jobs if h2g.routine_key(str(j.get("name") or "")) == rkey]
        if not mine:
            return not on
        for j in mine:
            if _enabled(j) != on:
                # (not resume_cron: its guard refuses a routine that runs in Grok, and this IS the move back)
                importer._cron_action("resume" if on else "pause", importer._job_id(j), prof, getattr(self, "ctx", None))
        _, _, after = self._h2g_jobs(pid)
        return all(_enabled(j) == on for j in after if h2g.routine_key(str(j.get("name") or "")) == rkey)

    def _h2g(self, pid: Optional[str] = None) -> h2g.H2G:
        wanted = mark = None
        try:
            from . import sync_panel  # the "What syncs" panel; absent in older builds
            wanted, mark = sync_panel.wanted, (lambda p, item: sync_panel.mark_synced(p, item, "hermes_to_grok"))
        except ImportError:
            pass
        return h2g.H2G(self.central, self._h2g_writer(pid), hermes=self._h2g_hermes, bot=self._h2g_bot,
                       wanted=wanted, pause_hermes=lambda p, k: self._h2g_switch(p, k, False),
                       resume_hermes=lambda p, k: self._h2g_switch(p, k, True), mark_synced=mark)

    def _h2g_owner_only(self, own_page: bool = False) -> None:
        owner_only(own_page)

    def _h2g_call(self, pid: Any, fn: str, *args: Any) -> Any:
        pid = self.require_pair(pid)
        try:
            return getattr(self._h2g(pid), fn)(pid, *args)
        except h2g.H2GError as exc:
            raise EngineError(exc.code, exc.message) from None

    # ---- what the page, the terminal and REST call ---------------------------------------------------------------
    def to_grok_info(self, pid: Any) -> dict:
        """The twin's allowlist, record of changes, and (when allowed) the preview of what would be sent now."""
        info = self._h2g_call(pid, "info")
        w = self._h2g_writer(info["pair_id"])
        bot = self._h2g_bot(info["pair_id"])
        info["permission"] = w.permission(bot or "-")
        if getattr(w, "kind", "") == "mirror":  # what the owner does once in Grok: the rule, and the Mirror's steps
            info["in_grok"] = {"allow_rule": (info.get("grant") or {}).get("rule") or self._h2g_rule(bot),
                               "where": GRANT_WHERE, "mirror_steps": h2g_mirror.mirror_instructions()}
        if info["allowed"]:
            try:
                info["preview"] = self.to_grok_preview(info["pair_id"])
            except EngineError as exc:
                info["preview"] = {"error": exc.as_dict()}
        return info

    def _h2g_rule(self, bot: str) -> str:
        """The narrowest Auto-Review rule for this bot: the Mirror (setting mirror_name, only when it is a plain
        name) may ask this one bot, named by its id, to edit its own fields and existing routines."""
        return h2g_mirror.allow_rule(config.settings().get("mirror_name") or "",
                                     [{"id": bot, "name": self._h2g_grok_name(bot)}])

    def to_grok_allow(self, pid: Any, items: Any = "all", own_page: bool = False) -> dict:
        self._h2g_owner_only(own_page)
        pid = self.require_pair(pid)
        bot = self._h2g_bot(pid)
        out = self._h2g_call(pid, "allow", items, self._h2g_rule(bot) if bot else None)
        self._h2g_panel_follow(pid, out.get("allowed") or [], [])
        return self.to_grok_info(pid)

    def _h2g_panel_follow(self, pid: str, allowed: list, stopped: list) -> None:
        """Allowing an item here IS the owner's choice of direction: the What syncs row follows it (Both ways for
        name, description and instructions, which keep coming from Grok too; Hermes -> Grok for routines). Stopping
        puts the row back to Grok -> Hermes (routines: Off). Without the panel module, the allowlist alone decides."""
        try:
            from . import sync_panel
        except ImportError:
            return
        way_on = {"routines": "hermes_to_grok"}
        way_off = {"routines": "off"}
        with sync_panel.edit() as f:
            t = f["twins"].setdefault(pid, {})
            for item, ways in ((i, (way_on, "both")) for i in allowed):
                t[item] = {"direction": ways[0].get(item, ways[1]), "at": now_iso()}
            for item in stopped:
                if (t.get(item) or {}).get("direction") in ("both", "hermes_to_grok"):
                    t[item] = {"direction": way_off.get(item, "grok_to_hermes"), "at": now_iso()}

    def to_grok_disallow(self, pid: Any, items: Any = "all") -> dict:
        """Stopping is always allowed (also from a chat): it only ever takes changes back."""
        before = self._h2g_call(pid, "info")["allowed"]
        out = self._h2g_call(pid, "disallow", items)
        self._h2g_panel_follow(out["pair_id"], [], [i for i in before if i not in (out.get("allowed") or [])])
        info = self.to_grok_info(out["pair_id"])
        if out.get("revoke"):
            info["revoke"] = out["revoke"]
        return info

    def to_grok_preview(self, pid: Any) -> dict:
        return self._h2g_call(pid, "preview")

    def to_grok_apply(self, pid: Any, change_id: Any, own_page: bool = False) -> dict:
        self._h2g_owner_only(own_page)
        return self._h2g_call(pid, "apply", change_id)

    def to_grok_undo(self, pid: Any, change_id: Any, own_page: bool = False) -> dict:
        self._h2g_owner_only(own_page)
        return self._h2g_call(pid, "undo", change_id)

    def to_grok_resolve(self, pid: Any, item: Any, keep: Any, own_page: bool = False) -> dict:
        self._h2g_owner_only(own_page)
        return self._h2g_call(pid, "resolve", item, keep)

    def to_grok_runner(self, pid: Any, rkey: Any, to: Any, own_page: bool = False) -> dict:
        self._h2g_owner_only(own_page)
        if isinstance(rkey, str) and not h2g.RKEY_RE.match(rkey):
            rkey = h2g.routine_key(rkey)  # the routine's name works as well as its key
        return self._h2g_call(pid, "move_runner", rkey, to)

    def to_grok_poll(self) -> list:
        """Final answers for changes the Mirror makes on its next run, then the handovers waiting on them (the real
        writer, and the sample's pretend Grok account when the sample is on)."""
        pids: list = [None]
        try:
            pids += self.demo.pair_ids()[:1] if self.demo.exists() else []
        except EngineError:
            pass
        out: list = []
        for pid in pids:
            try:
                out += self._h2g(pid).finish_moves()
            except (h2g.H2GError, OSError, ValueError, KeyError, TypeError):
                log.warning("grok-bot-twin: the Mirror's answers could not be read")
        return out

    def to_grok_tick(self) -> None:
        """The timer's part: read the Mirror's answers, only when Hermes -> Grok was ever used here. Never raises."""
        try:
            if os.path.lexists(self.central / h2g.STATE_FILE):
                self.to_grok_poll()
        except Exception:  # the timer's other work goes on whatever happens here
            log.exception("grok-bot-twin: reading the Mirror's answers failed")

    def to_grok_give_up(self, pid: Any, own_page: bool = False) -> dict:
        self._h2g_owner_only(own_page)
        return self._h2g_call(pid, "give_up")

    def h2g_directions(self, pid: str, item: str) -> dict:
        """For the panel: hermes_to_grok / both are "ok" while the twin is allowed for `item` (the owner's own choice in
        the Hermes -> Grok sheet or terminal; the preview itself says when the Mirror is not ready yet); else
        "needs_mirror"."""
        x = self._h2g(pid)
        bot = self._h2g_bot(pid)
        ok = item in x.allowed(pid) and bool(bot)
        s = "ok" if ok else "needs_mirror"
        return {"hermes_to_grok": s, "both": s}

    def h2g_last(self, pid: str, items: tuple) -> Optional[tuple]:
        """(when, "hermes_to_grok") of the last change of `items` made in Grok, for the panel."""
        try:
            d = self._h2g(pid)._read()
        except Exception:
            return None
        for c in reversed(d["changes"]):
            if c.get("pid") == pid and c.get("state") == "done" and any(
                    i in items or (i.startswith("routine:") and "routines" in items) for i in c.get("items") or []):
                return c.get("at"), "hermes_to_grok"
        return None


# ---- the "What syncs" panel rows (when the panel module is there) ---------------------------------------------------
def register_panel() -> bool:
    try:
        from . import sync_panel
    except ImportError:
        return False

    def svc() -> Any:
        from .service import get_service
        return get_service()

    def rows(item: str):
        def directions(ctx: Any) -> dict:
            base = {"grok_to_hermes": "ok", "off": "not_yet"} if item != "routines" else \
                {"grok_to_hermes": "not_yet", "off": "ok"}
            try:
                return {**base, **svc().h2g_directions(ctx.pair_id, item)}
            except Exception:
                return {**base, "hermes_to_grok": "needs_mirror", "both": "needs_mirror"}

        def last(ctx: Any) -> Any:
            try:
                return svc().h2g_last(ctx.pair_id, (item,))
            except Exception:
                return None
        return directions, last

    for item in ("name", "description", "routines"):
        d, l = rows(item)
        sync_panel.provide(item, directions=d, last_synced=l)
    return True


# ---- the terminal: hermes twin to-grok ... ---------------------------------------------------------------------------
ACTIONS = ("show", "allow", "disallow", "apply", "undo", "resolve", "runner", "give-up", "beta")  # beta: says it is gone
_ITEM_WORDS = {"name": "Name", "description": "Description", "instructions": "Instructions", "routines": "Routines"}


def _change_lines(p: dict) -> list:
    out = []
    for c in p.get("changes") or []:
        item = str(c.get("item") or "")
        if item.startswith("routine:"):
            b, a = c.get("before"), c.get("after")
            if a is None:
                out.append(f"  Routine \"{(b or {}).get('name')}\": removed from Grok")
            else:
                state = "on in Grok" if a.get("enabled") else "paused in Grok"
                if a.get("enabled") is None:
                    state = "on or paused in Grok as it is now"
                new = (": replaces any routine of this name in Grok (Hermes cannot see Grok's routines)"
                       if c.get("grok_unknown") else ": new in Grok")
                sched = a.get("schedule_words") or a.get("schedule")
                out.append(f"  Routine \"{a.get('name')}\" ({sched}, {state})" + (new if b is None else ": changed"))
                if b is not None and b.get("schedule") != a.get("schedule"):
                    out.append(f"    schedule: {b.get('schedule')} → {a.get('schedule')}")
                if b is None:
                    out += ["    prompt: " + line for line in str(a.get("prompt") or "").splitlines()]
                elif b.get("prompt") != a.get("prompt"):
                    out += ["    " + line for line in h2g.text_diff(str(b.get("prompt") or ""),
                                                                    str(a.get("prompt") or ""), label="Grok")]
        elif item == "instructions":
            out.append("  Instructions:")
            out += ["    " + line for line in c.get("diff") or []]
        else:
            out.append(f"  {_ITEM_WORDS.get(item, item)}: \"{c.get('before')}\" → \"{c.get('after')}\"")
        if c.get("note"):
            out.append(f"    ({c['note']})")
    for h in p.get("held") or []:
        out.append(f"  ! {h.get('why')}")
    return out


def cmd_to_grok(a: Any) -> int:
    from . import cli
    svc = cli._svc()
    vals = list(a.args or [])
    action = "show"
    pair = None
    # [TWIN] [ACTION ARGS...]: the first word that is an action splits the twin's name from the rest
    idx = next((i for i, v in enumerate(vals) if v in ACTIONS), None)
    if idx is not None:
        pair = " ".join(vals[:idx]) or None
        action, rest = vals[idx], vals[idx + 1:]
    else:
        pair, rest = (" ".join(vals) or None), []
    if action == "beta":  # gone: Hermes to Grok is per twin now. Says so, changes nothing
        cli._out(NO_BETA)
        return 0
    pid = cli._one(svc, pair)
    if action in ("allow", "apply", "undo", "resolve", "runner", "give-up"):
        owner_only(False)
    if action == "give-up":
        v = svc.to_grok_give_up(pid)
        cli._out(v.get("sentence") or "")
        return 0
    if action == "show":
        svc.to_grok_poll()  # the Mirror's answers first, so the preview is up to date
        v = svc.to_grok_info(pid)
        if a.json:
            cli._out(json.dumps(v, ensure_ascii=False, indent=1))
            return 0
        if not v["allowed"]:
            cli._out("The Mirror may not change this bot in Grok. To allow it: hermes twin to-grok "
                     "allow (all, or name description instructions routines).")
            return 0
        cli._out("The Mirror may change in Grok: " + ", ".join(_ITEM_WORDS[i].lower() for i in v["allowed"]) + ".")
        p = v.get("preview") or {}
        if p.get("error"):
            cli._err(f"✗ {p['error']['message']}")
            return 1
        if p.get("blocked"):
            cli._out(p["blocked"])
            g = v.get("in_grok") or {}
            if v.get("permission") != "ready" and g.get("allow_rule"):
                cli._out(f"In {g['where']}, add: \"{g['allow_rule']}\"")
        cli._out(p.get("sentence") or "")
        for line in _change_lines(p):
            cli._out(line)
        if p.get("changes") and not p.get("blocked"):
            cli._out(f"To make these changes in Grok: hermes twin to-grok apply {p['change_id']}")
        for c in reversed(v.get("changes") or []):
            if c.get("state") == "done" and not c.get("undone_by") and not c.get("undo_of"):
                cli._out(f"Last change made in Grok {ago(c.get('at'))}; undo: hermes twin to-grok undo {c['id']}")
                break
        return 1 if p.get("held") else 0
    if action in ("allow", "disallow"):
        items = [w.lower() for w in rest] or ["all"]
        items = "all" if "all" in items else items
        v = getattr(svc, f"to_grok_{action}")(pid, items)
        cli._out("The Mirror may change in Grok: " + (", ".join(_ITEM_WORDS[i].lower() for i in v["allowed"])
                                                      or "nothing") + ".")
        g = v.get("grant") or {}
        if action == "allow" and g.get("rule"):
            cli._out(f"Grok needs this once. In {g['where']}, add: \"{g['rule']}\"")
        r = v.get("revoke") or {}
        if r:
            cli._out(r.get("sentence") or "")
            if r.get("rule"):
                cli._out(f"  \"{r['rule']}\"")
            if r.get("forgot"):
                cli._out(f"{r['forgot']} change(s) still waiting for the Mirror were taken back.")
        return 0
    if action in ("apply", "undo"):
        if len(rest) != 1 or not h2g.CHANGE_RE.match(rest[0]):
            raise EngineError("usage", f"Give the change id: hermes twin to-grok {action} c-...")
        v = getattr(svc, f"to_grok_{action}")(pid, rest[0])
        cli._out(v.get("sentence") or "")
        if v.get("state") in ("done", "sent") and action == "apply" and v.get("change_id"):
            cli._out(f"Undo: hermes twin to-grok undo {v['change_id']}")
        return 0 if v.get("state") in ("done", "sent") else 1
    if action == "resolve":
        if len(rest) != 2:
            raise EngineError("usage", "Say the item and which one to keep: hermes twin to-grok resolve name hermes")
        p = svc.to_grok_resolve(pid, rest[0].lower(), rest[1].lower())
        cli._out(p.get("sentence") or "")
        for line in _change_lines(p):
            cli._out(line)
        return 0
    if len(rest) < 2:
        raise EngineError("usage", "Say the routine and where it runs: hermes twin to-grok runner ROUTINE grok|hermes")
    v = svc.to_grok_runner(pid, " ".join(rest[:-1]), rest[-1].lower())
    cli._out(v.get("sentence") or "")
    return 0 if v.get("state") in ("done", "sent") else 1


def setup(subs: Any) -> None:
    p = subs.add_parser("to-grok", help="Changes made in Hermes, made in Grok by your Mirror bot: preview, apply, undo")
    p.add_argument("args", nargs="*", metavar="[TWIN] [ACTION ...]",
                   help="show (default) | allow [ITEMS|all] | disallow [ITEMS|all] | apply ID | undo ID | "
                        "resolve ITEM hermes|grok | runner ROUTINE grok|hermes | give-up (stop waiting for the Mirror) | "
                        "(off for every twin until you allow it)")
    p.add_argument("--json", action="store_true", help="Machine-readable output")


HANDLERS = {"to-grok": cmd_to_grok}
