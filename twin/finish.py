"""Finishing a twin and the small per-twin facts the Twins page reads.

A new twin is "started": it has the bot's name and description. What is left (`setup_left`) is the owner's:
bring the instructions over (the Grok app does not keep them on this computer) and, when the owner has no default
model, choose one. This module holds that, the text of a skill the owner copies into Grok by hand, the note
"I pasted it into Grok", and the earlier versions of pasted instructions.

Nothing here is sent to Grok and no text is logged: events carry counts only.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import hermes_side as hs
from . import importer, safe, store
from . import secrets as _secrets
from .connect import BadInput
from .engine import EngineError
from .model import derive_state, item_kind, now_iso, parse_iso, valid_id, valid_item_id

SETUP_STEPS = ("instructions", "model")
LINES_CAP = 20
BAD_REQUEST = "That request was not understood."
SKILL_SECRET = ("This skill holds something that looks like a password or key, so Hermes does not hand it over.",
                "Take it out of the skill in Hermes first.")
NOT_WAITING = "That skill is not waiting to be copied to Grok."
DESCRIPTION_SECRET = ("This description holds something that looks like a password or key, so Hermes does not hand "
                      "it over.", "Take it out of the description in Hermes first.")
DESCRIPTION_NOT_WAITING = "The description is not waiting to be copied to Grok."
DESCRIPTION_MISSING = ("Hermes could not find this twin's description.",
                       "Open the twin in Hermes and copy the description from there.")
REPLACED_BY = ("paste", "grok", "choice", "put_back", "send", "import")
_LIVE_FILE_RE = re.compile(r"live file ([A-Za-z0-9._-]{1,80}): line (\d{1,7})")
# (\ud800-\udfff: half a surrogate pair, which JSON can carry but no file or command line can be written with)
_C1_BIDI = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff\ud800-\udfff]")


def lines_message(lines: list) -> str:
    """"Line 14 looks like…" / "Lines 14 and 20 look like…" / "Lines 14, 20 and 3 more look like…": the numbers
    only, never the text."""
    if len(lines) == 1:
        return f"Line {lines[0]} looks like a password or key, so nothing was saved yet."
    if len(lines) == 2:
        return f"Lines {lines[0]} and {lines[1]} look like a password or key, so nothing was saved yet."
    return (f"Lines {lines[0]}, {lines[1]} and {len(lines) - 2} more look like a password or key, so nothing was "
            "saved yet.")


def model_hint(name: str) -> str:
    """Where a twin's model is chosen so that it counts. Hermes Desktop's picker in the message box keeps its
    choice as a preference of the app; the model of a Hermes bot itself is set under Settings."""
    # (no brackets, and short: the page shows the first 240 characters, and the old sentence ran past them)
    return (f"In Hermes, open Settings, then Model. Under \"Applies to\" choose {name} and choose a model. "
            "Then click Twins in the sidebar. A model chosen in the message box does not count here.")


def clean_pasted(text: str) -> str:
    """Pasted text as it is saved: Windows line ends become plain ones, other control and direction-override
    characters go (tab and newline stay). The number of lines never changes."""
    return _C1_BIDI.sub("", text.replace("\r\n", "\n").replace("\r", ""))


class FinishMixin:
    """The finish-a-twin half of TwinService (see service.py for the attributes it uses)."""

    # ---- small plugin-side notes kept with each real twin's record -------------------------------------------------
    def _note_entry(self, pid: str, **fields: Any) -> None:
        """Set (or, with None, drop) fields of one real twin's record in pairs.json."""
        with store.locked():
            reg = self._registry()
            if pid not in reg:
                return
            for k, v in fields.items():
                if v is None:
                    reg[pid].pop(k, None)
                else:
                    reg[pid][k] = v
            self._save_registry(reg)

    # ---- what is left before a twin is finished -------------------------------------------------------------------
    @staticmethod
    def _left(entry: dict, facts: dict, model: Optional[str]) -> list:
        ins = entry.get("instructions") if isinstance(entry.get("instructions"), dict) else {}
        left = []
        if not facts.get("has_instructions") and ins.get("by") != "none":
            left.append("instructions")
        if model == "":
            left.append("model")
        return left

    def _finish_real(self, p: dict, entry: dict) -> None:
        """The finishing facts of one real twin, from its own Hermes folder and the plugin's record of it."""
        home = self._profile_home(p["hermes"]["profile"])
        facts = hs.soul_facts(home)
        model = hs.profile_model(home)
        p["hermes"]["name"] = facts["name"]
        p["hermes"]["description"] = facts["description"]
        rec = entry.get("model") if isinstance(entry.get("model"), dict) else {}
        p["hermes"]["model_from"] = (None if not model else
                                     "default" if rec.get("from") == "default" and rec.get("value") == model else "own")
        g = entry.get("grok") if isinstance(entry.get("grok"), dict) else {}
        p["grok"]["description"] = hs.shown(g.get("description"), 2000)
        p["setup_left"] = self._left(entry, facts, model)
        if "model" in p["setup_left"]:
            p["model_hint"] = model_hint(p["grok"]["name"] or p["hermes"]["profile"])
        else:
            p.pop("model_hint", None)
        recorded = [j for j in (entry.get("job_ids") or []) if valid_id(j)] if isinstance(entry.get("job_ids"), list) else []
        sw = entry.get("switched") if isinstance(entry.get("switched"), dict) else {}
        known = list(dict.fromkeys([*recorded, *[j for j in (sw.get("job_ids") or []) if valid_id(j)]]))
        p["routines"] = {
            # (by name: a routine can be listed twice, once to turn off in Grok and once as a Hermes copy)
            "in_grok": len({str(b.get("item")) for b in p["by_hand"] if str(b.get("item", "")).startswith("routine:")}),
            "paused_in_hermes": len(importer.paused_jobs_for(home, recorded or None)),
            "on_in_hermes": len(importer.running_jobs_for(home, known)) if known else 0}
        # (the number on the page's tab is the number of versions its list shows)
        p["restore_points"] = self.versions_listed(p["pair_id"], home, p.get("restore_points"))
        # what `to_grok` counts, by item: botport lists a change the owner carries to Grok by hand under by_hand
        p["pending"]["to_grok_items"] = [b["item"] for b in p["by_hand"]
                                         if b.get("side") == "grok" and valid_item_id(b.get("item"))][:50]

    def _checked_at(self, p: dict) -> Optional[str]:
        """The newest time this twin was looked at: its last sync, a manual check of it, or the last background
        check of all twins."""
        times = [t for t in (p.get("last_sync_at"), self._checked.get(p["pair_id"]),
                             None if p.get("state") == "error" else self.last_check_at) if parse_iso(t)]
        return max(times, key=lambda t: parse_iso(t)) if times else None

    # ---- "I pasted it into Grok" ----------------------------------------------------------------------------------
    def _copied(self) -> dict:
        data = store.read_json(self.central / "copied.json", {}) or {}
        if not isinstance(data, dict):
            return {}
        return {pid: {i: d for i, d in items.items() if valid_item_id(i) and isinstance(d, str)}
                for pid, items in data.items() if valid_id(pid) and isinstance(items, dict)}

    def _apply_copied(self, p: dict, mine: dict) -> None:
        """Skills (and the description) the owner pasted into Grok by hand stop asking: they leave `waiting`, `to_grok_items`, the
        by-hand list and the counts, for as long as the Hermes version is the one that was pasted. A later change
        in Hermes forgets the note, so the skill asks again. The same holds for a routine that an imported setup
        export brought over: Grok is where it came from, so its Hermes copy does not ask to be carried to Grok
        until it is changed in Hermes."""
        if not mine:
            return
        home = self._profile_home(p["hermes"]["profile"])
        stale = []
        for item, digest in mine.items():
            if ":" not in item and item_kind(item) == "description":  # the description, pasted by hand
                try:
                    now = hs.soul_facts(home)["description"]
                except (OSError, ValueError):
                    now = ""
                got = (now, False) if now else None
            elif item.startswith("routine:"):  # (noted with the digest of the routine itself, not of a text)
                if importer.routine_digest(home, item.split(":", 1)[1]) != digest:
                    stale.append(item)
                continue
            else:
                got = hs.skill_text(home, item.split(":", 1)[1]) if item.startswith("skill:") else None
            if got is None or hs.text_digest(got[0]) != digest:
                stale.append(item)
        if stale:
            with store.locked():
                data = self._copied()
                for item in stale:
                    data.get(p["pair_id"], {}).pop(item, None)
                if not data.get(p["pair_id"]):
                    data.pop(p["pair_id"], None)
                store.atomic_write_json(self.central / "copied.json", data)
        done = set(mine) - set(stale)
        if not done:
            return
        waiting = [w for w in p["waiting"] if w["item"] not in done]
        items = [i for i in p["pending"].get("to_grok_items", []) if i not in done]
        p["pending"]["waiting_approval"] = max(0, int(p["pending"].get("waiting_approval") or 0)
                                               - (len(p["waiting"]) - len(waiting)))
        p["pending"]["to_grok"] = max(0, int(p["pending"].get("to_grok") or 0)
                                      - (len(p["pending"].get("to_grok_items", [])) - len(items)))
        p["waiting"], p["pending"]["to_grok_items"] = waiting, items
        p["by_hand"] = [b for b in p["by_hand"] if not (b.get("side") == "grok" and b.get("item") in done)]
        if p["state"] in ("needs_you", "changes_waiting"):
            p["state"], p["state_reason"] = derive_state(p)

    def _to_copy(self, pid: str) -> set:
        """The items of this twin whose Hermes version may be handed out for copying into Grok: waiting to go
        there, counted in to_grok, or kept in Hermes only."""
        try:
            p = next(x for x in self.status()["pairs"] if x["pair_id"] == pid)
        except (StopIteration, EngineError):
            return set()
        return ({w["item"] for w in p.get("waiting", []) if w.get("to") in ("grok", None)}
                | set(p.get("pending", {}).get("to_grok_items", [])) | set(p.get("skipped", [])))

    def _description_item(self, pid: str, item: Any) -> Optional[str]:
        """This twin's own item for its description (botport calls it "description" or "persona.description")
        when `item` names the description and it may be handed out for copying into Grok, else None. The page
        names it by its kind alone."""
        if not valid_item_id(item) or ":" in item or item_kind(item) != "description":
            return None
        return next((i for i in sorted(self._to_copy(pid)) if ":" not in i and item_kind(i) == "description"),
                    None)

    def _hermes_description(self, pid: str) -> str:
        """The twin's description as it is in Hermes now (botport's own lines of the bot's prompt file; the
        sample's from its pretend Hermes side)."""
        try:
            p = next(x for x in self.status()["pairs"] if x["pair_id"] == pid)
        except (StopIteration, EngineError):
            return ""
        if pid in self.demo.pair_ids():
            text = (p.get("hermes") or {}).get("description")
        else:
            try:
                text = hs.soul_facts(self._profile_home(p["hermes"]["profile"]))["description"]
            except (OSError, ValueError):
                text = ""
        return text if isinstance(text, str) else ""

    def skill_text(self, pid: str, item: Any) -> dict:
        """The Hermes version of one skill, or of the twin's description (`item=description`), for the owner to
        paste into Grok. Text only, capped, checked for anything that looks like a password or key, never logged."""
        pid = self.require_pair(pid)
        if valid_item_id(item) and ":" not in item and item_kind(item) == "description":
            found = self._description_item(pid, item)
            if found is None:
                raise EngineError("not_found", DESCRIPTION_NOT_WAITING)
            text = self._hermes_description(pid)
            if not text.strip():
                raise EngineError("not_found", *DESCRIPTION_MISSING)
            # (the prompt file's reader has already hidden anything password-like: that mark counts the same)
            clean = _C1_BIDI.sub("", text)
            if _secrets.has_secret(text) or _secrets.has_secret(clean) or hs.HIDDEN_LINE in text:
                raise EngineError("looks_like_secret", *DESCRIPTION_SECRET)
            return {"pair_id": pid, "item": found, "name": "description", "text": clean,
                    "truncated": False}
        if not valid_item_id(item) or not item.startswith("skill:") or item not in self._to_copy(pid):
            raise EngineError("not_found", NOT_WAITING)
        name = item.split(":", 1)[1]
        if pid in self.demo.pair_ids():
            got = self.demo.skill_text(pid, name)
        else:
            prof = next((p["hermes"]["profile"] for p in self.status()["pairs"] if p["pair_id"] == pid), None)
            got = hs.skill_text(self._profile_home(prof), name)
        if got is None:
            raise EngineError("not_found", "Hermes could not find that skill's text.",
                              "Open the twin in Hermes and copy the skill from there.")
        text, truncated = got
        clean = _C1_BIDI.sub("", text)  # the text handed back, checked as it is handed back (and as it was)
        if _secrets.has_secret(text) or _secrets.has_secret(clean):
            raise EngineError("looks_like_secret", *SKILL_SECRET)
        return {"pair_id": pid, "item": item, "name": safe.clean_name(name, 100), "text": clean,
                "truncated": bool(truncated)}

    def _mark_copied(self, pid: str, item: Any) -> Optional[str]:
        """Note "I pasted it into Grok" for a skill or for the description. Returns the twin's own name for the
        item (the page may say "description" where botport says "persona.description"), or None when there is
        nothing to note because the item is not waiting any more: the plugin has already seen that Grok has the
        text, which is the usual order of events (paste in Grok, come back, press Done). That is no failure."""
        if valid_item_id(item) and ":" not in item and item_kind(item) == "description":
            if pid in self.demo.pair_ids():  # (the sample's description only ever follows Grok)
                raise EngineError("usage", DESCRIPTION_NOT_WAITING)
            found = self._description_item(pid, item)
            if found is None:
                return None
            text = self._hermes_description(pid)
            if not text.strip():
                raise EngineError("not_found", *DESCRIPTION_MISSING)
            with store.locked():
                data = self._copied()
                data.setdefault(pid, {})[found] = hs.text_digest(text)
                store.atomic_write_json(self.central / "copied.json", data)
            return found
        if not valid_item_id(item) or not item.startswith("skill:"):
            raise BadInput("That item is not one of this twin's.")
        if item not in self._to_copy(pid):
            return None
        name = item.split(":", 1)[1]
        if pid in self.demo.pair_ids():
            self.demo.copied_to_grok(pid, item)
        else:
            prof = next((p["hermes"]["profile"] for p in self.status()["pairs"] if p["pair_id"] == pid), None)
            got = hs.skill_text(self._profile_home(prof), name)
            if got is None:
                raise EngineError("not_found", "Hermes could not find that skill's text.",
                                  "Open the twin in Hermes and copy the skill from there.")
            with store.locked():
                data = self._copied()
                data.setdefault(pid, {})[item] = hs.text_digest(got[0])
                store.atomic_write_json(self.central / "copied.json", data)
        return item

    # ---- the owner brings the instructions over --------------------------------------------------------------------
    def set_instructions(self, pid: str, text: Any = None, none: Any = False, accept_lines: Any = None) -> dict:
        """`text`: the instructions the owner copied in Grok and pasted on the page. Saved in the twin's Hermes bot
        on this computer only; the version before is kept under Earlier versions. A line that looks like a password
        or key refuses the whole save and names the line numbers; `accept_lines` (the owner looked at exactly those
        lines) lets those through, and only those. `none=True`: this bot has no instructions; nothing is written."""
        pid = self.require_pair(pid)
        if not isinstance(none, bool) or (none and text is not None):
            raise BadInput(BAD_REQUEST)
        if accept_lines is None:
            accept_lines = []
        if (not isinstance(accept_lines, list) or len(accept_lines) > 10_000
                or not all(isinstance(n, int) and not isinstance(n, bool) for n in accept_lines)):
            raise BadInput(BAD_REQUEST)
        sample = pid in self.demo.pair_ids()
        if none:
            if sample:
                self.demo.set_instructions(pid, None)
            else:
                self._note_entry(pid, instructions={"by": "none", "at": now_iso()})
            self.invalidate()
            self.emit("instructions", pid, {"saved": False, "none": True})
            return {"pair_id": pid, "saved": False, "setup_left": self._left_now(pid)}
        if not isinstance(text, str) or not text.strip():
            raise BadInput("Paste the bot's instructions first.")
        if len(text.encode("utf-8", errors="replace")) > hs.INSTRUCTIONS_CAP:
            raise BadInput("That text is too long to save. Instructions can be up to 200 kB.")
        text = clean_pasted(text)
        if hs.has_markers(text):
            raise BadInput("One line looks like a marker Hermes uses inside its own files (it starts with "
                           "<!-- botport or <!-- grok-bot-twin). Take that line out, then save again.")
        flagged = sorted({f["line"] for f in _secrets.detect_secrets(text)})
        accepted = set(accept_lines)
        refused = [n for n in flagged if n not in accepted]
        if refused:
            raise EngineError("looks_like_secret", lines_message(refused),
                              "Take that line out, or save without it." if len(refused) == 1
                              else "Take those lines out, or save without them.",
                              extra={"lines": refused[:LINES_CAP]})
        if sample:
            self.demo.set_instructions(pid, text)
        else:
            prof = next((p["hermes"]["profile"] for p in self.status()["pairs"] if p["pair_id"] == pid), None)
            home = self._profile_home(prof)
            if home is None or not prof or prof == "default":
                raise EngineError("bad_profile", "This twin's Hermes bot could not be found.",
                                  "Open the Twins page again; if it stays, stop twinning this bot and twin it again.")
            try:
                before = hs.write_instructions(home, text)
            except OSError:
                # (the write is all or nothing: a failed one leaves the prompt file and the kept versions as they were)
                raise EngineError("unsafe_folder", "Hermes could not save the instructions in this twin's own folder. "
                                  "Nothing was changed.",
                                  "Try again; if it stays, run hermes twin doctor to see why.")
            hs.keep(self.central, pid, before, "Before you changed the instructions" if before.strip()
                    else "Before you added the instructions", "paste")
            self._note_entry(pid, instructions={"by": "owner", "at": now_iso()})
            self._retry_stuck(pid)
        kept = len(flagged)
        said = "Instructions saved" + (f"; {kept} line{'' if kept == 1 else 's'} kept that looked like a password "
                                       "or key" if kept else "")
        self.invalidate()
        self.emit("instructions", pid, {"saved": True, "kept_lines": kept, "message": said})
        return {"pair_id": pid, "saved": True, "setup_left": self._left_now(pid)}

    def _left_now(self, pid: str) -> list:
        try:
            return list(next(p for p in self.status(fresh=True)["pairs"] if p["pair_id"] == pid).get("setup_left", []))
        except (StopIteration, EngineError):
            return []

    # ---- earlier versions the plugin itself keeps (pasted instructions of a real twin) -----------------------------
    def _own_points(self, pid: str) -> list:
        return [{"id": p["id"], "item": hs.INSTRUCTIONS_ITEM, "kind": "instructions", "name": "instructions",
                 "at": p.get("at") if parse_iso(p.get("at")) else None,
                 "reason": safe.clean_summary(p.get("reason"), 120), "scope": "item",
                 "text": hs.shown_version(p["text"]),
                 "replaced_by": p.get("why") if p.get("why") in REPLACED_BY else None}
                for p in reversed(hs.kept_list(self.central, pid))]

    def _restore_own(self, pid: str, point_id: str) -> Optional[dict]:
        """Put back one earlier version of a real twin's pasted instructions; None when `point_id` is not one of
        the plugin's own (the caller then asks botport)."""
        point = next((p for p in hs.kept_list(self.central, pid) if p["id"] == point_id), None)
        if point is None:
            return None
        prof = next((p["hermes"]["profile"] for p in self.status()["pairs"] if p["pair_id"] == pid), None)
        home = self._profile_home(prof)
        if home is None or not prof or prof == "default":
            raise EngineError("bad_profile", "This twin's Hermes bot could not be found.")
        try:
            before = hs.write_instructions(home, point["text"])
        except OSError:
            raise EngineError("unsafe_folder", "Hermes could not save the instructions in this twin's own folder.",
                              "Run hermes twin doctor to see why.")
        if before == point["text"]:
            return {"restored": [], "restore_point": None,
                    "note": "Nothing to put back: Hermes already has that version."}
        undo = hs.keep(self.central, pid, before, "Before you put an earlier version back", "put_back")
        self._note_entry(pid, instructions={"by": "owner", "at": now_iso()} if point["text"].strip() else None)
        self.invalidate()
        self._retry_stuck(pid)
        self.emit("restored", pid, {"count": 1, "items": [hs.INSTRUCTIONS_ITEM]})
        return {"restored": [hs.INSTRUCTIONS_ITEM], "restore_point": undo, "waiting_for_ok": []}

    # ---- botport stopped updating a twin because of something that looks like a password or key ---------------------
    # botport has its own, much stricter check. Before it changes anything in a twin's Hermes bot it keeps a copy
    # of the bot's files, and it refuses to do that (and so refuses the update) while one of them holds a line it
    # takes for a password or key: also a line of pasted instructions the owner accepted here. botport's status
    # then only says "a change is waiting", for ever. The plugin remembers the refusal, shows it on the twin's row
    # with the line and the way out, and tries the update again as soon as the instructions were changed.
    def _stuck(self) -> dict:
        data = store.read_json(self.central / "stuck.json", {}) or {}
        return {pid: e for pid, e in data.items() if valid_id(pid) and isinstance(e, dict)} \
            if isinstance(data, dict) else {}

    def _note_stuck(self, pid: str, error: Any) -> None:
        """Remember (or, with a sync that went through, forget) botport's refusal for one real twin. Only the
        file name and the line number are kept, never the text."""
        err = error if isinstance(error, dict) else {}
        entry = None
        if err.get("code") == "secret_refused":
            m = _LIVE_FILE_RE.search(str(err.get("message") or ""))
            entry = {"file": m.group(1) if m else "", "line": int(m.group(2)) if m else 0, "at": now_iso()}
        with store.locked():
            data = self._stuck()
            if (entry is None and pid not in data) or data.get(pid) == entry:
                return
            if entry is None:
                data.pop(pid, None)
            else:
                data[pid] = entry
            store.atomic_write_json(self.central / "stuck.json", data)

    def _apply_stuck(self, p: dict, entry: Optional[dict]) -> None:
        """Show botport's refusal on the twin's row, for as long as the change it held up is still waiting."""
        if not entry or p["state"] == "error":
            return
        if not int(p["pending"].get("to_hermes") or 0):
            self._note_stuck(p["pair_id"], None)  # nothing is waiting any more: it went through some other way
            return
        name = p["grok"]["name"] or "this twin"
        span = hs.own_block_lines(self._profile_home(p["hermes"]["profile"]))
        line = entry.get("line") if isinstance(entry.get("line"), int) else 0
        if entry.get("file") == "SOUL.md" and span and span[0] <= line <= span[1]:
            message = (f"Updates from Grok stopped: line {line - span[0] + 1} of {name}'s instructions looks like a "
                       "password or key to botport.")
            hint = ("Under Earlier versions, put back the instructions from before, then paste them again "
                    "without that line. The update from Grok then arrives by itself.")
        else:
            where = f"{entry.get('file') or 'a file'}, line {line}" if line else (entry.get("file") or "a file")
            message = (f"Updates from Grok stopped: something in {name}'s Hermes bot looks like a password or key "
                       f"to botport ({where}).")
            hint = "Take it out of that file in the Hermes bot's folder, then press Try again."
        p["state"] = "error"
        p["error"] = {"code": "secret_refused", "message": message, "hint": hint}
        p["state_reason"] = message

    def _retry_stuck(self, pid: str) -> None:
        """The instructions changed: if botport had stopped updating this twin, try again right away."""
        if pid in self._stuck():
            try:
                self.sync(pid)
            except EngineError:
                pass

    # ---- which earlier version an action left behind (so the list can say why it is there) -------------------------
    def _note_point(self, pid: str, point_id: Any, why: str) -> None:
        if not valid_item_id(point_id) or why not in REPLACED_BY:
            return
        with store.locked():
            data = store.read_json(self.central / "points.json", {}) or {}
            data = data if isinstance(data, dict) else {}
            mine = data.get(pid) if isinstance(data.get(pid), dict) else {}
            mine[point_id] = why
            data[pid] = dict(list(mine.items())[-200:])
            store.atomic_write_json(self.central / "points.json", data)

    def _point_notes(self, pid: str) -> dict:
        data = store.read_json(self.central / "points.json", {}) or {}
        mine = data.get(pid) if isinstance(data, dict) else None
        return {k: v for k, v in mine.items() if v in REPLACED_BY} if isinstance(mine, dict) else {}
