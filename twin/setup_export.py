"""Import setup export: what a Grok bot writes about its own setup, brought into its twin.

The owner asks the bot, in the bot's own Grok chat, for a setup export (`REQUEST` is the text to paste there). The
bot answers with one `grok-bot-export` document, version 1: the same file botport reads with `--from-export` and
Nous' agent-importers reads with `hermes import-agent grok-bot`. The owner drops that file on the Twins page, or
runs `hermes twin import FILE`.

What is taken from the file, and nothing else:
- `instructions`: saved as the twin's instructions the same way a paste is (the plugin's own marked block in the
  twin's prompt file; the version before is kept under Earlier versions). Every line a password or key touches
  is left out and its line number is shown, never its text.
- `routines` (name, five-field schedule, time zone, the prompt it runs): each becomes a Hermes routine that is
  made switched off. Turning them on stays "Move routines to Hermes". A routine whose name or prompt looks like
  it holds a password or key is not made at all.
- `apps`: the names only, as a list of things to set up on this computer. No address, login or tool list is kept.

Nothing is imported that the owner was not shown: the preview carries the instructions and every prompt whole,
exactly as they would be saved, and what is too long for that is not copied. Characters nobody can see are
taken out first, and the check reads each line in its plain form.

What is never taken: `memories` and `user_facts` are decoded with the rest of the file, counted and dropped
before anything else looks at the bot (the two lists are never walked, copied, shown, logged or written), and
so is every key this module does not name. The file's text is used for the length of one call and is not
written anywhere: not to the plugin's folder, not to a temporary file, not to the event list (events carry
counts only). The file is a bot's own writing and is not trusted: what it put into the fields that are taken
is taken as that, whatever it is, which is why the owner reads it before pressing Import.

Why the plugin reads the file itself instead of handing it to botport: botport takes an export only at the
moment a twin is first made (`twin create --from-export`), keeps its own copy of the items, and treats the
instructions as text that follows Grok. This import is for a twin that exists, must not store what it strips,
and saves the instructions as the owner's own (as a paste does), so nothing asks to send them back to Grok.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Any, Optional

from . import hermes_side as hs
from . import importer, safe, store
from . import secrets as _secrets
from .connect import BadInput
from .engine import EngineError
from .finish import BAD_REQUEST, clean_pasted
from .model import now_iso, parse_iso, valid_id, valid_item_id

FORMAT = "grok-bot-export"
TEXT_CAP = 2_000_000          # characters of the dropped file
ROUTINES_MAX = 50
CONNECTORS_MAX = 50
BOTS_MAX = 50
PROMPT_CAP = 16 * 1024        # bytes of one routine's prompt
PROMPT_SHOWN = 2000
NOTES_FILE = "setup-export.json"
PARTS = ("instructions", "routines", "connectors")
SCHEDULE_TRIGGERS = ("", "schedule", "scheduled", "time", "cron", "interval")

FIELD_CUT = 2000              # characters of a short field (a name, a schedule, a time zone) that are looked at
NAME_CUT = 300                # characters of a name that are made plain and checked (120 at most are used)
LEFT_OUT_MAX = 200            # lines left out of the instructions; more than that and none of it is copied

PRIVACY = "Its lists of memories and facts about you are left out: Hermes never stores them."

# The text the owner pastes into the bot's own Grok chat. It names no bot and holds nothing of the owner's.
REQUEST = """\
Please write your setup export for me: one JSON file (or, if you cannot attach a file, one plain code block and \
nothing else). Use exactly this shape:

{"format": "grok-bot-export", "version": 1, "bots": [{
  "name": "<your name>",
  "instructions": "<your instructions, in full, word for word>",
  "routines": [{"name": "<its name>", "instruction": "<the prompt it runs>", "trigger": "schedule",
                "schedule_cron": "<five-field cron, like 0 8 * * 1-5>", "schedule_text": "<the schedule in words>",
                "timezone": "<like UTC or Europe/Paris>", "paused": false}],
  "apps": [{"name": "<the name of a connector or app you use>"}],
  "memories": [],
  "user_facts": []
}]}

Rules:
- Setup only. Leave "memories" and "user_facts" as empty lists: put in nothing you remember about me.
- Never put a password, token, key or cookie anywhere. For connectors give the name only, no address and no login.
- A routine that is started by an event instead of a time gets "trigger": "event" and no "schedule_cron".
- Change nothing about yourself. This is a read of your setup, nothing more.
"""

NOT_AN_EXPORT = ("That file is not a setup export.",
                 "Copy the export request, paste it into the bot's own chat in Grok, and use the file it answers with.")
SAMPLE = ("This is the sample twin: it has invented data only.",
          "Import a setup export into the twin of one of your own bots.")
MARKER_WHY = ("One line looks like a marker Hermes uses inside its own files, so the instructions were not copied. "
              "Paste them yourself without that line.")
TOO_LONG_WHY = "The instructions are longer than 200 kB, so they were not copied."
ALL_HELD_WHY = "Not copied: too much of it looks like a password or key."
EXISTS_WHY = "a routine of this name is already in Hermes (not compared)"
NO_PAUSED = "This Hermes cannot add a routine switched off, so none was added. Update Hermes, then import again."
NO_ANSWER = "Hermes did not answer in time, so no routine was added. Import the same file again."
NOT_TRIED = "not tried: an earlier one failed"
LATER = "not added yet: import the same file again to add it"
ROUTINES_BUDGET = 90.0        # seconds one import spends on adding routines (each is one Hermes command)

_CRON_FIELD = re.compile(r"[0-9*/,\-]{1,40}")
_TZ = re.compile(r"[A-Za-z][A-Za-z0-9_+\-/]{0,63}")
_UTC = ("utc", "etc/utc", "gmt", "etc/gmt", "z")
_LINE_CTRL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff\ud800-\udfff]")
_WS = re.compile(r"\s+")
def _chars(*spans: Any) -> "re.Pattern[str]":
    """A pattern for a set of characters given as code points, single ones or (first, last) pairs."""
    parts = [re.escape(chr(x)) if isinstance(x, int) else f"{re.escape(chr(x[0]))}-{re.escape(chr(x[1]))}"
             for x in spans]
    return re.compile("[" + "".join(parts) + "]")


# Characters nobody can see, which are taken out of what an import saves (control and direction characters
# and half surrogates go with clean_pasted): the soft hyphen, the grapheme joiner, the Arabic letter mark,
# fillers and blanks, annotation and shorthand marks, and the tag characters (which can carry a whole hidden
# text). Left alone on purpose: private-use and not-yet-assigned characters (they show, as a box at worst).
_UNSEEN = _chars(0xAD, 0x34F, 0x61C, (0x115F, 0x1160), (0x17B4, 0x17B5), 0x180E, 0x2800, 0x3164, 0xFFA0,
                 (0xFFF9, 0xFFFC), (0x1BCA0, 0x1BCA3), 0x1D159, (0x1D173, 0x1D17A), (0xE0000, 0xE007F))
# Variation selectors change how the character before them is drawn (an emoji in colour, a variant of an
# ideograph, a Mongolian letter shape). They stay in the text; the password-and-key check reads without them.
_SELECTORS = _chars((0x180B, 0x180D), 0x180F, (0xFE00, 0xFE0F), (0xE0100, 0xE01EF))


def _now() -> float:
    return time.monotonic()


def bad_export(why: str = "") -> EngineError:
    return EngineError("bad_export", why or NOT_AN_EXPORT[0], NOT_AN_EXPORT[1])


# ---- reading the document -------------------------------------------------------------------------------------------
def parse(text: str) -> dict:
    """The export as a JSON object. Strict JSON first; else the text from the first `{` to the last `}` (a bot
    that answered with a sentence or a code fence around the JSON). Nothing is repaired or guessed."""
    if not isinstance(text, str) or not text.strip():
        raise bad_export()
    if len(text) > TEXT_CAP:
        raise EngineError("too_large", "That file is too large to be a setup export.",
                          "Ask the bot for its setup only: instructions, routines and connector names.")
    doc = None
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text and "}" in text else ""):
        if not candidate.strip():
            continue
        try:
            doc = json.loads(candidate)
            break
        except (ValueError, RecursionError):
            doc = None
    if not isinstance(doc, dict):
        raise bad_export()
    if doc.get("format") != FORMAT and not isinstance(doc.get("bots"), list):
        raise bad_export()
    version = doc.get("version")
    if version is not None and (isinstance(version, bool) or version != 1):
        raise bad_export("That setup export is of a kind this version of Grok Bot Twin does not read.")
    bots = doc.get("bots")
    if not isinstance(bots, list) or not any(isinstance(b, dict) for b in bots):
        raise bad_export("That setup export names no bot.")
    return doc


def _cut(value: Any, cap: int = FIELD_CUT) -> str:
    """A field of the file as text, cut before anything else looks at it: no check here ever runs over more
    than `cap` characters of a field that is meant to be short."""
    return value[:cap] if isinstance(value, str) else ""


def _visible(text: str) -> str:
    """Text without the characters nobody can see (see _UNSEEN). What the owner is shown is then all there is,
    and a key cannot hide from the check behind an invisible character. One pass, whatever the length."""
    return _UNSEEN.sub("", text)


def _plain_form(text: str) -> str:
    """The text as the password-and-key check reads it: every line in its plain (NFKC) form and without
    variation selectors, so a key written in full-width or other look-alike characters, or with a selector
    inside it, is seen. Line for line the same text."""
    return "\n".join(_SELECTORS.sub("", unicodedata.normalize("NFKC", ln)).replace("\n", " ")
                     for ln in text.split("\n"))


def _grew(text: str, plain: str) -> bool:
    """A few characters stand for eighteen each in plain form. A text that grows like that is not checked
    (the check would run for seconds on what looked short): it is treated as not safe to copy."""
    return len(plain) > 4 * len(text) + FIELD_CUT


def _looks_secret(text: str) -> bool:
    plain = _plain_form(text)
    return _grew(text, plain) or _secrets.has_secret(plain)


def _one_line(value: Any, cap: int) -> str:
    """A name from the file as one plain line: plain (NFKC) form, nothing invisible, spaces collapsed, cut to
    `cap`. Not masked: the caller decides what a name that looks like a password means, and checks that on the
    whole line before it is cut to its final length."""
    if not isinstance(value, str):
        return ""
    # (cut before it is made plain, and again after: the plain form can be eighteen times as long, and making
    # 2,000 such characters plain took 40 ms a name. A key that starts in the part of a name that is used is
    # still whole inside these 300 characters.)
    text = unicodedata.normalize("NFKC", value[:NAME_CUT])[:NAME_CUT]
    return _WS.sub(" ", _visible(_LINE_CTRL.sub(" ", text))).strip()[:cap].strip()


def _text(value: Any) -> str:
    """A long text field as botport and agent-importers read it: trimmed, and when it has no real line break
    but spells them as backslash-n (a chat app's doing), those become line breaks."""
    if not isinstance(value, str):
        return ""
    t = value.strip()
    if "\n" not in t and "\\n" in t:
        t = t.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
    return t


def _count(value: Any) -> int:
    """How many entries a list in the file has. The entries themselves are never looked at."""
    return len(value) if isinstance(value, list) else 0


def pick_bot(doc: dict, twin_name: str, wanted: Optional[str] = None) -> tuple[dict, str, bool]:
    """(the bot's own object, its name, whether that name is the twin's bot's). With `wanted`, the bot of that
    name; else the one whose name is the twin's; else the only one. Several bots and no match: refused."""
    bots = [b for b in doc.get("bots", [])[:BOTS_MAX] if isinstance(b, dict)]
    # (a name as the page is shown it, so the name the page sends back finds the same bot again; the whole line
    # is masked before it is cut, so half a key never stays at the cut)
    named = [(b, safe.clean_name(_one_line(b.get("name"), FIELD_CUT), 100)) for b in bots]
    named = [(b, n) for b, n in named if n]
    if not named:
        raise bad_export("That setup export names no bot.")
    key = lambda s: safe.clean_name(s, 100).casefold()  # noqa: E731
    mine = key(twin_name or "")
    if wanted is not None:
        hit = [(b, n) for b, n in named if key(n) == key(wanted)]
        if len(hit) != 1:
            raise bad_export("That setup export has no bot of that name.")
        return hit[0][0], hit[0][1], key(hit[0][1]) == mine
    hit = [(b, n) for b, n in named if key(n) == mine]
    if len(hit) == 1:
        return hit[0][0], hit[0][1], True
    if len(named) == 1:
        return named[0][0], named[0][1], False
    raise EngineError("several_bots", f"That file holds {len(named)} bots and none of them is this twin's.",
                      "Ask this bot itself for its setup export, in its own chat.")


# ---- instructions ---------------------------------------------------------------------------------------------------
def screen_instructions(raw: Any) -> dict:
    """{"state": "new"|"none"|"held", "text": what would be saved, "left_out": [line numbers], "lines": int,
    "why": str}. Every line a password or key touches is left out (a label on one line and its value on the
    next are two lines; a private key block of any kind is all its lines); what is left goes through the same
    check again and is held back whole if it still trips it. The check reads the plain (NFKC) form of each line
    with nothing invisible in it; what is saved is that same text as it was written."""
    out = {"state": "none", "text": "", "left_out": [], "lines": 0, "why": ""}
    text = _text(raw)
    if len(text) > hs.INSTRUCTIONS_CAP:  # (characters: never more bytes than that are cleaned or checked)
        return {**out, "state": "held", "why": TOO_LONG_WHY}
    text = _visible(clean_pasted(text))
    if not text.strip():
        return out
    if len(text.encode("utf-8", errors="replace")) > hs.INSTRUCTIONS_CAP:
        return {**out, "state": "held", "why": TOO_LONG_WHY}
    plain = _plain_form(text)
    if _grew(text, plain):
        return {**out, "state": "held", "why": TOO_LONG_WHY}
    if hs.has_markers(text) or hs.has_markers(plain):
        return {**out, "state": "held", "why": MARKER_WHY}
    lines = text.split("\n")
    flagged = _secrets.flagged_lines(plain)
    if len(flagged) > LEFT_OUT_MAX:  # (the list of line numbers the owner is shown is always the whole list)
        return {**out, "state": "held", "why": ALL_HELD_WHY}
    kept = [ln for i, ln in enumerate(lines) if i + 1 not in flagged]
    rest = "\n".join(kept).strip("\n")
    left_out = sorted(flagged)
    if not rest.strip() or _looks_secret(rest):
        # nothing readable is left, or taking lines out joined something that still looks like a key
        return {**out, "state": "held", "left_out": left_out, "why": ALL_HELD_WHY}
    return {"state": "new", "text": rest, "left_out": left_out, "lines": rest.count("\n") + 1, "why": ""}


# ---- routines -------------------------------------------------------------------------------------------------------
def _cron(value: Any) -> str:
    """A five-field schedule of numbers, `*`, `/`, `,` and `-`, or "". Anything else is left to the owner."""
    if not isinstance(value, str):
        return ""
    fields = value.split()
    if len(fields) != 5 or not all(_CRON_FIELD.fullmatch(f) for f in fields):
        return ""
    return " ".join(fields)


def read_routines(raw: Any) -> tuple[list, int]:
    """([routine], how many beyond ROUTINES_MAX were not read). Each routine: {"name", "schedule", "timezone",
    "prompt" (as it would be handed to Hermes; internal), "shown" (as the page shows it), "state", "why"} with
    state new | held | manual. For a routine that would be made, `shown` is the whole prompt, exactly as Hermes
    would get it: nothing is imported that was not shown. Short fields are cut before any check runs over them,
    and a prompt that is too long is not checked at all. Nothing here touches Hermes."""
    items = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    out: list = []
    seen: set = set()
    for r in items[:ROUTINES_MAX]:
        whole = _one_line(r.get("name"), FIELD_CUT)  # (the whole name is checked; then it is cut)
        name = whole[:120].strip()
        raw_prompt = r.get("instruction") if isinstance(r.get("instruction"), str) else ""
        too_long = len(raw_prompt) > PROMPT_CAP
        prompt = "" if too_long else _visible(clean_pasted(_text(raw_prompt)))
        cron_raw = _cut(r.get("schedule_cron"), 200)
        cron = _cron(cron_raw)
        words = safe.clean_name(_cut(r.get("schedule_text"), 200), 60)
        tz = _cut(r.get("timezone"), 200).strip()
        trigger = _one_line(_cut(r.get("trigger"), 200), 40).lower()
        row = {"name": safe.clean_name(name, 120) or "(no name)", "schedule": cron or words, "timezone": "",
               "prompt": "", "shown": "", "state": "manual", "why": ""}
        if any(_looks_secret(v) for v in (whole, prompt, tz, cron_raw)):
            # (its name is masked as a whole line and then cut, so no part of a key shows at the cut; a
            # schedule that tripped the check is not shown at all)
            row.update(state="held", why="not copied: looks like a password or key",
                       name=safe.clean_name(whole, 120) or "(no name)",
                       schedule="" if _looks_secret(cron_raw) else row["schedule"])
        elif not name:
            row["why"] = "the file gives it no name: add it in Hermes yourself"
        elif name.casefold() in seen:
            row["why"] = "another routine in the file has the same name: add it in Hermes yourself"
        elif trigger not in SCHEDULE_TRIGGERS:
            row["why"] = "it starts on an event, not at a time: add it in Hermes yourself"
        elif not cron:
            row["why"] = "Hermes cannot read its schedule: add it in Hermes yourself"
        elif tz and not _TZ.fullmatch(tz):
            row["why"] = "Hermes cannot read its time zone: add it in Hermes yourself"
        elif too_long or len(prompt.encode("utf-8", errors="replace")) > PROMPT_CAP:
            row["why"] = "what it runs is too long to copy: add it in Hermes yourself"
        elif not prompt.strip():
            row["why"] = "the file does not say what it runs: add it in Hermes yourself"
        else:
            row.update(state="new", prompt=prompt, shown=prompt, timezone=tz, _name=name)
        if row["state"] == "manual" and prompt:
            # (not made by this import: the start of it, password-like text hidden, for the owner's own copy)
            row["shown"] = hs.shown(prompt, PROMPT_SHOWN)
        if tz and _TZ.fullmatch(tz) and row["state"] != "held":
            row["timezone"] = tz
        if name:
            seen.add(name.casefold())
        out.append(row)
    return out, max(0, len(items) - ROUTINES_MAX)


# ---- connectors -----------------------------------------------------------------------------------------------------
def read_connectors(raw: Any) -> list:
    """The names of the bot's connectors and apps: one line each, links, addresses and anything password-like
    taken out, no duplicates. Only `name` is read from an entry."""
    out: list = []
    seen: set = set()
    for a in (raw if isinstance(raw, list) else [])[: CONNECTORS_MAX * 4]:
        name = a.get("name") if isinstance(a, dict) else a if isinstance(a, str) else None
        plain = _one_line(name, FIELD_CUT)  # (checked whole, cut to 80 characters below)
        if not plain or _looks_secret(plain):
            continue
        shown = safe.clean_summary(plain, 80)
        if not shown or shown.casefold() in seen:
            continue
        seen.add(shown.casefold())
        out.append(shown)
        if len(out) >= CONNECTORS_MAX:
            break
    return out


def read_setup(doc: dict, twin_name: str, wanted: Optional[str] = None) -> dict:
    """Everything this import takes from the file, and the counts of what it leaves. The two lists of memories
    are only measured; the returned object holds nothing of them, and the caller drops `doc` after this."""
    bot, name, matches = pick_bot(doc, twin_name, wanted)
    routines, more = read_routines(bot.get("routines"))
    return {"bot": {"name": name, "matches": matches},
            "instructions": screen_instructions(bot.get("instructions")),
            "routines": routines,
            "connectors": read_connectors(bot.get("apps")),
            "left_out": {"memories": _count(bot.get("memories")), "user_facts": _count(bot.get("user_facts")),
                         "skills": _count(bot.get("skills")), "routines": more}}


def _routine_view(r: dict) -> dict:
    return {"name": r["name"], "schedule": r["schedule"], "timezone": r["timezone"], "prompt": r["shown"],
            "state": r["state"], "why": r["why"]}


def paused_reason(timezone: str) -> str:
    zone = timezone or "this computer's time zone (the file named none)"
    return (f"{importer.SETUP_EXPORT_MARKER}; its schedule is in {zone}; turn it on with Move routines to Hermes "
            "once the Grok routine is off")


# ---- the service half -----------------------------------------------------------------------------------------------
class SetupExportMixin:
    """The import half of TwinService (see service.py for the attributes it uses)."""

    def _setup_notes(self) -> dict:
        data = store.read_json(self.central / NOTES_FILE, {}) or {}
        return {pid: e for pid, e in data.items() if valid_id(pid) and isinstance(e, dict)} \
            if isinstance(data, dict) else {}

    def _last_setup_export(self, pid: str) -> Optional[dict]:
        e = self._setup_notes().get(pid)
        if not e or not parse_iso(e.get("at")):
            return None
        names = e.get("connectors") if isinstance(e.get("connectors"), list) else []
        return {"at": e["at"], "connectors": [safe.clean_summary(n, 80) for n in names[:CONNECTORS_MAX]
                                              if isinstance(n, str) and n.strip()]}

    def setup_export_info(self, pid: str) -> dict:
        """What the import sheet shows before a file is there: the request to paste into the bot's own chat, the
        one sentence about what is left out, and what the last import left to set up on this computer."""
        pid = self.require_pair(pid)
        return {"pair_id": pid, "request": REQUEST, "privacy": PRIVACY, "last": self._last_setup_export(pid)}

    def _setup_target(self, pid: str) -> tuple:
        """(the bot's name in Grok, the twin's profile, its Hermes folder) of one twin."""
        try:
            p = next(x for x in self.status()["pairs"] if x["pair_id"] == pid)
        except StopIteration:  # (not one of the twins the page shows: nothing is read for it)
            raise EngineError("pair_not_found", "That is not one of your twins.", "Open the Twins page again.")
        prof = (p.get("hermes") or {}).get("profile")
        return (p.get("grok") or {}).get("name") or "", prof, self._profile_home(prof)

    def import_setup(self, pid: str, text: Any = None, apply: Any = False, bot: Any = None, skip: Any = None) -> dict:
        """Read a setup export for one twin. `apply=False`: say what an import would do (nothing is written).
        `apply=True`: save the instructions, add the routines switched off, note the connector names. `bot`: the
        name of the bot in the file to take (the page sends back the one it showed). `skip`: parts to leave
        (instructions, routines, connectors). The file's text is never written anywhere and never logged."""
        pid = self.require_pair(pid)
        if not isinstance(apply, bool) or (bot is not None and (not isinstance(bot, str) or len(bot) > 400)):
            raise BadInput(BAD_REQUEST)
        skip = [] if skip is None else skip
        if not isinstance(skip, list) or not all(s in PARTS for s in skip):
            raise BadInput(BAD_REQUEST)
        if not isinstance(text, str) or not text.strip():
            raise BadInput("Choose the setup export file first.")
        sample = pid in self.demo.pair_ids()
        if sample and apply:
            raise EngineError("sample", *SAMPLE)
        twin_name, prof, home = self._setup_target(pid)
        setup = read_setup(parse(text), twin_name, bot)
        del text  # (from here on only what was taken from the file exists)
        ins = setup["instructions"]
        own = ""
        if not sample and home is not None:
            try:
                own = hs.soul_facts(home)["own"]
            except (OSError, ValueError):
                own = ""
        if ins["state"] == "new" and own.strip("\n") == ins["text"]:
            ins["state"] = "same"
        replaces = ins["state"] == "new" and bool(own.strip())
        # (by name alone, compared the way the names are shown: the routine in Hermes is not opened or compared)
        there = {_one_line(n, FIELD_CUT).casefold() for n in importer.job_names(home)} if not sample else set()
        for r in setup["routines"]:
            if r["state"] == "new" and r["_name"].casefold() in there:
                r.update(state="exists", why=EXISTS_WHY, shown="")
        saved, added, new_ids = False, 0, []
        if apply:
            if home is None or not prof or prof == "default" or not valid_id(prof) or prof.startswith("-"):
                raise EngineError("bad_profile", "This twin's Hermes bot could not be found.",
                                  "Open the Twins page again; if it stays, stop twinning this bot and twin it again.")
            if "instructions" not in skip and ins["state"] == "new":
                saved = self._import_instructions(pid, home, ins["text"])
            if "routines" not in skip:
                added, new_ids = self._import_routines(pid, prof, home, setup["routines"])
            if "connectors" not in skip:
                with store.locked():
                    notes = self._setup_notes()
                    notes[pid] = {"at": now_iso(), "connectors": list(setup["connectors"])}
                    store.atomic_write_json(self.central / NOTES_FILE, notes)
            self.invalidate()
            self.emit("setup_export", pid, {"instructions": saved, "routines": added,
                                            "connectors": 0 if "connectors" in skip else len(setup["connectors"]),
                                            "lines_left_out": len(ins["left_out"]) if saved else 0})
        return {"pair_id": pid, "applied": apply, "bot": setup["bot"],
                # (`text` is the whole of what an import saves, character for character: it passed the check above,
                # and nothing is saved that is not shown)
                "instructions": {"state": ins["state"], "lines": ins["lines"], "left_out": ins["left_out"],
                                 "replaces": replaces, "text": ins["text"], "saved": saved, "why": ins["why"]},
                "routines": [_routine_view(r) for r in setup["routines"]],
                "connectors": [{"name": n} for n in setup["connectors"]],
                "left_out": setup["left_out"], "added": added, "privacy": PRIVACY}

    def _import_instructions(self, pid: str, home: Any, text: str) -> bool:
        """Save screened instructions exactly as a paste is saved (the plugin's own block; the version before is
        kept under Earlier versions)."""
        try:
            before = hs.write_instructions(home, text)
        except (OSError, UnicodeError):  # (the write is all or nothing)
            raise EngineError("unsafe_folder", "Hermes could not save the instructions in this twin's own folder. "
                              "Nothing was changed.", "Try again; if it stays, run hermes twin doctor to see why.")
        hs.keep(self.central, pid, before, "Before you imported its setup export", "import")
        self._note_entry(pid, instructions={"by": "owner", "at": now_iso(), "from": "export"})
        self._retry_stuck(pid)
        return True

    def _import_routines(self, pid: str, prof: str, home: Any, routines: list) -> tuple[int, list]:
        """Add every routine that is `new`, switched off. A Hermes that cannot make a routine switched off makes
        none. After each one the plugin looks at what Hermes wrote: a routine that is not switched off is switched
        off at once, and counts as added only when it is. Returns (how many were added, their ids)."""
        todo = [r for r in routines if r["state"] == "new"]
        if not todo:
            return 0, []
        deadline = _now() + ROUTINES_BUDGET
        flags = importer.cron_create_flags(prof)
        if flags is None or "--paused" not in flags or "--paused-reason" not in flags:
            for r in todo:
                r.update(state="failed", why=NO_ANSWER if flags is None else NO_PAUSED)
            return 0, []
        added: list = []
        came_from_grok: dict = {}
        stopped = False
        for r in todo:
            if stopped:
                r.update(state="failed", why=NOT_TRIED)
                continue
            if _now() > deadline:
                # (the page waits for this answer: the rest is added by importing the same file again, which
                # makes nothing twice)
                r.update(state="failed", why=LATER)
                continue
            tz = r["timezone"]
            if tz and "--timezone" not in flags:
                if tz.lower() not in _UTC:
                    r.update(state="failed", why="this Hermes cannot keep a routine's time zone: add it in Hermes "
                                                 "yourself")
                    continue
                tz = ""
            before = set(importer.job_states(home))
            ok, err = importer.create_paused_cron(prof, r["_name"], r["schedule"], r["prompt"], tz,
                                                  paused_reason(r["timezone"]))
            # every routine that is there now and was not before, whatever name Hermes gave it and whatever the
            # command said (one that ran out of time may still have made it): each must be switched off
            made = [jid for jid in importer.job_states(home) if jid and jid not in before]
            running = []
            for jid in made:
                if not importer.job_states(home).get(jid):
                    # never leave one running: it was not asked for and the Grok routine is still on
                    importer.pause_cron(jid, prof, None)
                    if not importer.job_states(home).get(jid):
                        running.append(jid)
            # (noted in every case, so "Move routines to Hermes" and its undo know them. A routine somebody
            # else added to this twin in the same seconds would be taken for this one: paused if it runs,
            # and noted)
            added += made
            if made and not running:
                r.update(state="added", why="")
                came_from_grok[f"routine:{r['_name']}"] = importer.routine_digest(home, r["_name"])
            elif running:
                r.update(state="failed", why="it was added but is not switched off: switch it off in Hermes now")
                stopped = True
            elif ok:
                r.update(state="failed", why="Hermes said it added it, but it is not in this twin's routines")
                stopped = True
            else:
                r.update(state="failed", why=safe.clean_summary(err, 160))
                stopped = True
        notes = {i: d for i, d in came_from_grok.items() if d and valid_item_id(i)}
        if notes:
            # Grok is where these came from: their Hermes copies do not ask to be carried to Grok (botport lists
            # any Hermes routine that way) until one is changed in Hermes. The same note a pasted skill gets.
            with store.locked():
                data = self._copied()
                data.setdefault(pid, {}).update(notes)
                store.atomic_write_json(self.central / "copied.json", data)
        if added:
            with store.locked():
                reg = self._registry()
                if pid in reg:
                    old = reg[pid].get("job_ids") if isinstance(reg[pid].get("job_ids"), list) else []
                    reg[pid]["job_ids"] = list(dict.fromkeys([*[j for j in old if valid_id(j)], *added]))[:200]
                    self._save_registry(reg)
        return sum(1 for r in todo if r["state"] == "added"), added
