"""`hermes twin ...` command line. Handlers return an int exit code (0 ok, 1 attention/failure, 2 usage).

Every message names the Twins page first and a command second, and uses the page's words: twin, Hermes bot,
up to date, changed in both places, earlier versions, skill sync, the sync bot and its pass, botport, sample.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Optional

from . import bgagent, safe
from . import osflags as _osflags
from .engine import EngineError
from .model import (SAMPLE_PROFILE, STATE_LABELS, ago, item_kind, item_name, open_conflicts, plural, status_sentence,
                    valid_id, valid_item_id, view)

DOT = {"in_step": "●", "changes_waiting": "◐", "needs_you": "!", "grok_missing": "?", "error": "×"}
PAGE = "the Twins page in Hermes Desktop"
CONNECT = f"Open {PAGE} and press Connect my Grok bots"
SAMPLE_LINE = "Sample twin with invented data. Nothing of yours is touched."

# Fixed sentences (ux contract section 4). The service sends its own lines as `stays` / `goes` / `again`; these are
# used only when an answer comes without them.
REMOVE_STAYS = ("Your bot in Grok, unchanged.", "Its Hermes bot and all its chats.")
REMOVE_GOES = ("Following Grok.", "The earlier versions kept for undo.", "This twin's pass (a small file).")
REMOVE_AGAIN = "You can twin it again later."
UNINSTALL_STAYS = ("Your Grok bots.", "Your Hermes bots and their chats.")
UNINSTALL_GOES = ("Every twin.", "Earlier versions.", "Each twin's pass.", "The sample.",
                  "Hermes' copy of botport.")
UNINSTALL_SKILL_SYNC = "In Grok, delete the sync bot's routine yourself; Hermes cannot reach it."
UNINSTALL_NEXT = "Switch the plugin off in Settings → Capabilities → Plugins."

HELP_TEXT = """\
Give your Grok bots a twin in Hermes. A twin is a Hermes bot that follows your Grok bot. Your Grok bot stays
exactly as it is. Everything here is also on the Twins page in Hermes Desktop.

  hermes twin            your twins and the one next step

Everyday
  status                 your twins and what to do next (--json for scripts)
  add                    give a Grok bot a twin: add --bot "Acme Support" (or --all); asks first
  finish [TWIN ...]      new twins: set their model and print what to ask each bot for (--all)
  stop [TWIN]            stop twinning a bot; its Hermes bot and chats stay

Your other computers
  share on|off|code      share your twins with your other computers; shows the link code
  link CODE              on another computer: link it to the one that shares (paste the code)
  unlink                 stop following; this computer's twins leave it (kept 30 days)
  fleet                  which computers hold your twins, and how up to date each is
  routines [on|off]      copy each twin's routines to your other computers; each runs on one computer
                         (move ROUTINE COMPUTER, run-here on|off, restore)
  background on|off      keep this computer's twins up to date while Hermes is closed (adds one small file;
                         off deletes it)

Ask another computer
  ask COMPUTER TEXT      ask this twin's copy on another computer to do something there
  requests [on|off]      let your other computers ask this one (off by default); shows the log

Fix things
  conflicts [TWIN]       show what changed in both places
  resolve [TWIN] ITEM --keep hermes|grok    keep one version
  instructions [TWIN]    Grok's instructions kept in step (check, on|off, keep grok|hermes, downloads on|off,
                         request)
  versions [TWIN]        earlier versions kept for undo
  restore [TWIN] NUMBER  put an earlier version back
  doctor                 check the setup and say what to fix
  setup                  set up this computer or finish what is left (--check, --plan; safe to run again)

Advanced
  check [TWIN]           check Grok now
  list-grok              the bots in your Grok app
  recent [TWIN]          recent activity (when and what kind; never content)
  compare [TWIN]         compare with Grok, item by item
  copies                 what copies over and how: the whole table, as on the Twins page
  checklist [TWIN]       what you do yourself, and what moving routines to Hermes takes
  syncs [TWIN]           what syncs with Grok, which way, and when it last did (ITEM from-grok|to-grok|both|off)
  import [TWIN] FILE     import the setup export a Grok bot wrote about itself (--request prints what to ask it)
  import-exports DIR     import every setup export in a folder, each into its bot's twin (--show)
  switch [TWIN]          move routines to Hermes: turn the copied routines on (--undo pauses them again)
  approve [TWIN] ITEM    let one waiting change go ahead
  skip [TWIN] ITEM       keep a changed skill in Hermes only (--undo offers it again)
  send-to-grok [TWIN]    send approved skill changes to Grok (needs skill sync)
  skill-sync [TWIN]      skill sync (optional): --folder DIR turns it on, --off turns it off, --status: last run
  digest [TWIN]          the optional activity digest to Grok: preview, --on, --off
  transcripts ACTION [TWIN]  whole conversations kept here, encrypted, searchable by the twin (off by
                         default). ACTION: on, off, add-grok, refresh, search, export, to-hermes, forget
  facts [TWIN]           memory facts shared with Grok: sync, keep CONFLICT hermes|grok, restore POINT
  briefs [TWIN]          session briefs to and from Grok, off until you choose: --way to-grok|from-grok|both|off
  to-grok [TWIN]         changes made in Hermes, made in Grok by your Mirror bot: preview, then
                         allow ITEMS, apply ID, undo ID, resolve ITEM hermes|grok, runner ROUTINE grok|hermes
  verify                 check botport against GitHub's signed build record
  demo [ACTION]          a sample twin with invented data (demo remove ends it)
  uninstall              remove Grok Bot Twin: every twin and its records; Hermes bots and chats stay

TWIN is a twin's name, such as "Acme Support" (shown by status). With one twin you can leave it out.
"""


def _svc():
    from .service import get_service
    return get_service()


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _confirm(question: str) -> bool:
    if not _osflags.is_terminal(sys.stdin):
        _err("Not a terminal: pass --yes to go ahead.")
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


class _NoTwin(Exception):
    pass


NO_TWINS = f"No twins yet. {CONNECT}, or run hermes twin add. To look around first: hermes twin demo"
TO_REAL_BOTS = f"to twin your real Grok bots, open {PAGE} and press Connect my Grok bots, or run hermes twin "
NOT_NEEDED = "Not needed here"  # how a doctor line starts its "fix" when there is nothing to do on this computer
# the first look on a computer that has no Grok app: its twins come from the computer that has
NO_GROK_HERE = ("Grok Bot Twin: the Grok app is not on this computer. Next: link it to the computer with your Grok "
                "bots: hermes twin link CODE")
WHERE_THE_CODE_IS = ("The code shows on that computer: on its Twins page press Share my twins with my other "
                     "computers, or run hermes twin share on.")


def _quoted(names: list) -> str:
    return ", ".join(f'"{n}"' for n in names[:20])


def _twin_names(svc) -> dict:
    """Twin -> its bot's name, for every twin there is (the short name stands in where no name is known)."""
    ids = svc.pair_ids()
    names = _names(svc)
    if any(i not in names for i in ids):
        try:  # nothing remembered yet (a first command): ask once
            names = {**names, **{p["pair_id"]: p["grok"]["name"] for p in svc.status().get("pairs", [])
                                 if isinstance(p, dict) and p.get("pair_id") and isinstance(p.get("grok"), dict)}}
        except Exception:
            pass
    return {i: safe.clean_name(names.get(i), 100) or i for i in ids}


def _call(names: dict, pid: str) -> str:
    """How a suggested command names a twin: by its name in quotes; by its short name only when two twins share
    a name (or none is known)."""
    name = names.get(pid)
    if not name or name == pid or sum(1 for n in names.values() if n.lower() == name.lower()) > 1:
        return pid
    return f'"{name}"'


def _resolve(svc, given: str) -> str:
    """The twin the owner named: by its bot's name ("Acme Support", any case) or by its short name."""
    given = str(given or "").strip()
    ids = svc.pair_ids()
    if given in ids:
        return given
    names = _twin_names(svc)
    hits = [pid for pid, name in names.items() if name.lower() == given.lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise EngineError("usage", f"More than one twin is called \"{safe.clean_name(given, 100)}\". "
                                   f"Use its short name instead: {', '.join(hits[:20])}")
    if valid_id(given):
        return given  # (a twin made outside the plugin is still reachable by its short name)
    raise EngineError("usage", f"No twin is called \"{safe.clean_name(given, 100)}\".",
                      f"Your twins: {_quoted(list(names.values()))}" if names else "You have no twins yet.")


def _one(svc, pair) -> str:
    """The twin a command is about: the one named, else the only twin there is."""
    if pair:
        return _resolve(svc, pair)
    ids = svc.pair_ids()
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise _NoTwin(NO_TWINS)
    names = _twin_names(svc)
    raise EngineError("usage", f"You have {len(ids)} twins; say which one: "
                      + ", ".join(_call(names, i) for i in ids[:20]))


def _two(a, second: str) -> tuple:
    """`CMD [TWIN] X`: one argument is X (for the only twin), two are TWIN X."""
    vals = list(getattr(a, "args", None) or [])
    if len(vals) == 1:
        return None, vals[0]
    if len(vals) == 2:
        return vals[0], vals[1]
    raise EngineError("usage", f"Give the {second} (and the twin's name first, in quotes, when you have more "
                               "than one twin).")


def _is_sample(p: dict) -> bool:
    return bool(p.get("sample") or p.get("demo"))


def _names(svc) -> dict:
    """Twin id -> bot name, from the last status (never runs botport)."""
    try:
        st = svc.cached_status() or {}
        return {p["pair_id"]: p["grok"]["name"] for p in st.get("pairs", [])
                if isinstance(p, dict) and p.get("pair_id") and isinstance(p.get("grok"), dict)}
    except Exception:
        return {}


def _kind_word(kind: Any) -> str:
    """An item kind in the page's words (the engine's "connector" is a "connection")."""
    return "connection" if kind == "connector" else str(kind or "")


def _item_words(item: Any) -> str:
    """'skill:greet-visitors' -> 'greet-visitors'; 'persona.description' -> 'description'."""
    return safe.clean_name(item_name(str(item or "")), 100) or "item"


def _reason(p: dict) -> str:
    label = STATE_LABELS.get(p.get("state"), "?")
    reason = p.get("state_reason") or ""
    return reason if reason and reason.lower() not in (label.lower(), "up to date") else ""


def _pair_line(p: dict) -> str:
    """One twin, in the sentence its row on the Twins page shows ("Follows Grok · ready", "1 change to send to
    Grok · 2 things left"); a twin that cannot be checked also says why."""
    label = status_sentence(p)
    reason = _reason(p) if p.get("state") == "error" else ""
    tail = f" ({reason})" if reason else ""
    sw = p.get("switched") if isinstance(p.get("switched"), dict) else None
    moved = (f" · runs in Hermes ({plural(int(sw.get('routines_on') or 0), 'routine')} on)"
             if sw and "in Hermes" not in label else "")
    return (f" {DOT.get(p.get('state'), '·')} {p['grok']['name']}{' (sample)' if _is_sample(p) else ''}: "
            f"{label}{tail}{moved} · checked {ago(p.get('last_sync_at'))}")


def _header(pairs: list) -> str:
    """"2 twins, 1 to finish setting up." : the page's header sentence (`summarize` in desktop/plugin.js), with
    every kind of open thing counted, so the terminal and the page tell one story."""
    real = [p for p in pairs if not _is_sample(p)]
    what = plural(len(pairs), "twin") if real else plural(len(pairs), "sample twin")
    seen = [view(p) for p in pairs]  # (twin/model.py: the one place a twin's matters are counted)
    attention = sum(1 for v in seen if v["needs"] == "you")
    sending = sum(1 for v in seen if v["needs"] == "send")
    finishing = sum(1 for v in seen if not v["ready"])
    parts = []
    if attention:
        parts.append(f"{attention} {'needs' if attention == 1 else 'need'} your attention")
    if sending:
        parts.append(f"{sending} with a change to send to Grok")
    if finishing:
        parts.append(f"{finishing} to finish setting up")
    if not parts:
        updating = any(isinstance(p.get("pending"), dict) and isinstance(p["pending"].get("to_hermes"), int)
                       and p["pending"]["to_hermes"] > 0 for p in pairs)
        parts.append("updating" if updating else "nothing needs you")
    return f"{what}, {', '.join(parts)}."


def _setup_ready(svc, st: Optional[dict] = None) -> bool:
    setup = (st or {}).get("setup") if isinstance((st or {}).get("setup"), dict) else None
    if setup is None:
        try:
            setup = svc.setup_state()
        except Exception:
            eng = (st or {}).get("engine") or {}
            return bool(eng.get("available")) and not eng.get("problem")
    return bool(setup.get("ready"))


def _next_step(p: dict, many: bool, names: Optional[dict] = None) -> str:
    """One next step for a twin that needs attention."""
    target = f" {_call(names or {}, p['pair_id'])}" if many else ""
    if p.get("state") == "error":
        return "run hermes twin doctor to see why Grok could not be checked."
    if int(p.get("conflicts") or 0):
        return f"choose which version to keep on {PAGE}, or run hermes twin conflicts{target}."
    for c in open_conflicts(p)[:1]:  # (the instructions sync's card, or the Hermes -> Grok preview)
        cmd = "instructions" if c.get("where") == "instructions" else "to-grok"
        return f"choose which version to keep: hermes twin {cmd}{target}."
    if int(p.get("secrets_held") or 0) or p.get("secrets_withheld"):
        if _is_sample(p):  # (no Grok app behind the sample: its own command changes the bot's text)
            return ("the sample holds its fake password back until the sample bot's text changes: hermes twin "
                    "demo grok-edit, then hermes twin check (or end the sample: hermes twin demo remove).")
        return ("remove the password or key from the bot's text in the Grok app; this clears by itself at the "
                "next check.")
    waiting = [w for w in (p.get("waiting") or []) if isinstance(w, dict) and w.get("item")]
    if waiting:
        item = safe.clean_name(waiting[0]["item"], 140)
        if item_kind(item) == "skill":
            return (f"send the change to Grok or keep it in Hermes only on {PAGE} "
                    f"(or: hermes twin approve{target} \"{item}\").")
        return f"give your go-ahead on {PAGE} (or: hermes twin approve{target} \"{item}\")."
    if p.get("state") == "grok_missing":
        return ("this bot is not in Grok any more; its Hermes bot keeps working. Stop twinning it with "
                f"hermes twin stop{target}, or leave it.")
    if view(p)["needs"] == "send":  # (approved, not sent yet)
        return f"send it to Grok on {PAGE}, or run hermes twin send-to-grok{target}."
    return f"open {PAGE}."


# ---- handlers ----------------------------------------------------------------------------------------
def cmd_status(a) -> int:
    svc = _svc()
    st = svc.status(fresh=True)
    if getattr(a, "json", False):
        _out(json.dumps(st, indent=1, ensure_ascii=False))
        return 0
    eng = st.get("engine") or {}
    pairs = st["pairs"]
    ready = _setup_ready(svc, st)
    brief = st.get("fleet") or {}
    if not pairs and brief.get("role") == "follower":
        # this computer has no Grok app: its twins are copies of the ones on the computer it is linked to
        fl = svc.fleet()
        for line in _fleet_cli.describe(fl):
            _out(line)
        return 0 if _fleet_cli._in_sync(fl) else 1
    if not pairs:
        if ready:
            _out(f"Grok Bot Twin: connected to Grok, no twins yet. Next: open {PAGE} and press Connect my Grok "
                 "bots, or run hermes twin add.")
        elif svc.grok_app_found():
            _out(f"Grok Bot Twin: not connected to Grok yet. Next: open {PAGE}, or run hermes twin setup.")
        else:
            # a computer without the Grok app (every Linux and Windows one, and any other Mac): its twins come
            # over a link, so the one next step is the link, never botport
            _out(NO_GROK_HERE)
            _out(f"  {WHERE_THE_CODE_IS}")
        _out("No twins yet. To look around first, see a sample with invented data: hermes twin demo")
        if eng.get("trust_warning"):
            _out(f" ! Note: {eng['trust_warning']}")
        return 0
    real = [p for p in pairs if not _is_sample(p)]
    attention = [p for p in pairs if p["state"] in ("needs_you", "error")]
    _out(f"Grok Bot Twin: {_header(pairs)}" + ("" if real else f" {SAMPLE_LINE}"))
    many = len(pairs) > 1
    names = {p["pair_id"]: p["grok"]["name"] for p in pairs}
    for p in pairs:
        _out(_pair_line(p))
        _waiting_lines(p, many, names)
    if eng.get("trust_warning"):
        _out(f" ! Note: {eng['trust_warning']}")
    if brief.get("role") == "source":
        n = brief.get("machines") or 0
        _out(("Shared with " + plural(n, "other computer") if n else "Sharing is on; no other computer is linked"
              if brief.get("sharing") else "Sharing with your other computers is switched off")
             + " (hermes twin fleet).")
    first = attention or [p for p in pairs if view(p)["needs"] == "send"]
    if first:
        _out(f"Next ({first[0]['grok']['name']}): " + _next_step(first[0], many, names))
    elif not real:
        _out(f"Next: {TO_REAL_BOTS}{'add' if ready else 'setup'}.")
    return 1 if attention else 0


def _waiting_label(items: list) -> str:
    """A skill changed in Hermes is "ready to send to Grok"; anything else (a routine or connection that came with
    a new twin) is "waiting for your go-ahead"."""
    kinds = {item_kind(str(w.get("item") if isinstance(w, dict) else w)) for w in items}
    return "ready to send to Grok" if kinds <= {"skill"} else "waiting for your go-ahead"


def _waiting_lines(p: dict, many: bool = True, names: Optional[dict] = None) -> None:
    """Changes waiting for the owner's go-ahead (e.g. a skill changed in Hermes), each with its command."""
    target = f"{_call(names or {}, p['pair_id'])} " if many else ""
    for w in (p.get("waiting") or [])[:20]:
        item = safe.clean_name(w["item"], 140)
        kind = item_kind(item)
        what = _item_words(item) + ("" if kind == "skill" else f" ({_kind_word(kind)})")
        _out(f"    {_waiting_label([w])}: {what}  ->  hermes twin approve {target}\"{item}\"")


def _sync_bits(r: dict) -> tuple[list, bool]:
    """Plain-word parts of one twin's check result, and whether it needs the owner."""
    bits, attention = [], False
    if r["applied"]:
        bits.append("updated " + ", ".join(sorted({_kind_word(x["kind"]) for x in r["applied"]})))
    if r["secrets_withheld"]:
        n = len({s["item"] for s in r["secrets_withheld"]})
        bits.append(f"{plural(n, 'thing')} that {'looks' if n == 1 else 'look'} like a password or key "
                    f"{'was' if n == 1 else 'were'} not copied (this check can miss things)")
        attention = True
    if r.get("waiting"):
        bits.append(f"{plural(len(r['waiting']), 'change')} {_waiting_label(r['waiting'])}")
        attention = True
    if r["conflicts"]:
        bits.append(f"{plural(len(r['conflicts']), 'item')} changed in both places")
        attention = True
    return bits, attention


def cmd_sync(a) -> int:
    svc = _svc()
    names = _names(svc)
    given = a.pair or getattr(a, "pair_opt", None)
    res = svc.sync(_resolve(svc, given) if given else None)
    if not res["pairs"]:
        _out("No twins to check yet.")
        return 0
    rc = 0
    for r in res["pairs"]:
        name = names.get(r["pair_id"], r["pair_id"])
        if r.get("error"):  # this twin failed; the others were still checked
            _out(f" {name}: can't check right now: {r['error']['message']}")
            rc = 1
            continue
        bits, attention = _sync_bits(r)
        if attention:
            rc = 1
        line = f" {name}: " + ("; ".join(bits) if bits else "up to date")
        if r.get("restore_point"):
            line += " (the earlier Hermes version is kept: hermes twin versions)"
        _out(line)
        if r.get("waiting"):
            first = safe.clean_name(r["waiting"][0]["item"], 140)
            _out(f"    decide on {PAGE}, or: hermes twin approve {_call(names, r['pair_id'])} \"{first}\"")
        if r["conflicts"]:
            _out(f"    choose on {PAGE}, or: hermes twin conflicts {_call(names, r['pair_id'])}")
    return rc


def _roster(data: dict) -> tuple[list, list]:
    """(the owner's real Grok bots, the sample's invented bots): never mixed."""
    bots = [b for b in data.get("bots") or [] if isinstance(b, dict)]
    sample = data.get("sample_bots")
    if not isinstance(sample, list):
        sample = [b for b in bots if b.get("demo")]
    return [b for b in bots if not b.get("demo")], [b for b in sample if isinstance(b, dict)]


def cmd_list_grok(a) -> int:
    svc = _svc()
    data = svc.grok_list()
    real, sample = _roster(data)
    ready = data.get("ready", data.get("app_found") or bool(real))
    # a bot is named by its name (`add --bot NAME`); only two bots with one name are told apart by Grok's own mark
    seen = [str(b["name"]).lower() for b in real + sample]

    def mark(b: dict) -> str:
        return f"   [{b['grok_id']}]" if seen.count(str(b["name"]).lower()) > 1 else ""
    for b in real:
        _out(f" {b['name']}{' · has a twin' if b.get('twinned_pair_id') else ''}{mark(b)}")
    for b in sample:
        _out(f" {b['name']} (sample){' · has a twin' if b.get('twinned_pair_id') else ''}{mark(b)}")
    if data.get("real_hidden"):
        _out("Your real Grok bots are hidden while the sample is on (you chose this on the Twins page).")
        return 0
    if real:
        return 0
    if not ready:
        _out(f"Not connected to Grok yet. {CONNECT}, or run hermes twin setup.")
        return 0 if sample else 1
    if not data.get("app_found"):
        _out("Grok app not found on this computer. Open the Grok app, sign in, then try again.")
        return 0 if sample else 1
    from . import config
    _out("No bots in your Grok app yet. Make one in Grok, then try again."
         + (" (The \"Bots to list\" setting of this plugin is filtering the list.)" if config.get("bots") else ""))
    return 0


SKIP_REASON = {"already_twinned": "it already has a twin",
               "profile_already_paired": "that Hermes profile already has a twin"}


def cmd_add(a) -> int:
    svc = _svc()
    real, sample = _roster(svc.grok_list())
    if a.all:
        if not real:
            _err("No Grok bots found. Run hermes twin doctor to see why.")
            return 1
        chosen = [b for b in real if not b.get("twinned_pair_id")]  # never the sample's invented bots
        if not chosen:
            _out("Every Grok bot already has a twin.")
            return 0
    else:
        asked = [n for n in (a.bot or []) if isinstance(n, str) and n.strip()]
        if not asked:
            _err("Say which bots: hermes twin add --bot \"Acme Support\"  (or --all). "
                 "See the names with hermes twin list-grok.")
            return 2
        chosen, missing = [], []
        for want in asked:
            hits = [b for b in real + sample if b["name"].lower() == want.lower() or b["grok_id"] == want]
            if hits:
                chosen += [b for b in hits if b not in chosen]
            else:
                missing.append(safe.clean_name(want, 100))
        if missing:
            for m in missing:
                _err(f"No Grok bot called \"{m}\". See the names with hermes twin list-grok.")
            return 1
    ids = [b["grok_id"] for b in chosen]
    profiles = {gid: a.profile for gid in ids} if a.profile else {}
    prev = svc.preview(ids, profiles)
    names = {b["grok_id"]: b["name"] for b in real + sample}
    for k in prev.get("skipped", []):
        _out(f" {names.get(k['grok_id'], k['grok_id'])}: skipped, {SKIP_REASON.get(k['reason'], k['reason'])}")
    if not prev["pairs"]:
        _err("Nothing to twin." + (" Pick another name: --hermes-profile NAME" if any(
            k["reason"] == "profile_already_paired" for k in prev.get("skipped", [])) else ""))
        return 1
    ids = [p["grok"]["id"] for p in prev["pairs"]]
    # create exactly what the preview showed, including the profile picked for each bot
    profiles = {p["grok"]["id"]: p["hermes"]["profile"] for p in prev["pairs"] if p["hermes"]["profile"]}
    for p in prev["pairs"]:
        tag = " (sample)" if p.get("sample") or p.get("demo") else ""
        _out(f"Ready to twin {p['grok']['name']}{tag}")
        for line in p.get("lines") or []:
            _out(f"  {line}")
        flagged = [it["name"] for it in p.get("items", []) if it.get("scan") in ("caution", "dangerous")]
        if flagged:
            _out(f"  Hermes' skill safety scan flagged: {', '.join(flagged)}")
        prof = p["hermes"]["profile"]
        if prof and not str(prof).startswith(SAMPLE_PROFILE):  # a sample twin sits on a reserved label, not a profile
            _out(f"  Its Hermes bot: profile {prof} (another name: --hermes-profile NAME)")
    if not a.yes and not _confirm(f"Twin {plural(len(ids), 'bot')}? Nothing changes in Grok."):
        _out("Nothing changed.")
        return 1
    res = svc.create(ids, profiles)
    many = len(svc.pair_ids()) > 1
    called = _twin_names(svc) if many else {}
    for p in res["pairs"]:
        _out(f"{p['grok']['name']} now has a twin.")
        _out(_pair_line(p))
        _waiting_lines(p, many, called)
    for k in res.get("skipped", []):
        _out(f" {names.get(k['grok_id'], k['grok_id'])}: skipped, {SKIP_REASON.get(k['reason'], k['reason'])}")
    for n in res.get("notes", []):
        _out(f" note: {n}")
    if res["pairs"]:
        _out(f"See it on {PAGE}, or run hermes twin.")
    return 0


def cmd_conflicts(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    data = svc.conflicts(pid)
    if not data["conflicts"]:
        _out("Nothing changed in both places.")
        return 0
    n = len(data["conflicts"])
    _out(f"{plural(n, 'item')} changed in both places:")
    for c in data["conflicts"]:
        kind = _kind_word(item_kind(c["conflict_id"])) or "item"
        _out(f"── {_item_words(c['conflict_id'])} ({kind}), since {ago(c.get('since'))}   [{c['conflict_id']}]")
        _out(c["diff"].rstrip() or "(no line-by-line changes to show)")
    target = f"{_call(_twin_names(svc), pid)} " if a.pair else ""
    first = data["conflicts"][0]["conflict_id"]
    _out(f"Which one do you want? Choose on {PAGE}, or here:")
    _out(f"  hermes twin resolve {target}\"{first}\" --keep grok     the Grok version replaces the one in Hermes; "
         "you can undo this under Earlier versions (hermes twin versions)")
    _out(f"  hermes twin resolve {target}\"{first}\" --keep hermes   the Hermes version stays; Grok keeps its own "
         "until you send or copy this one over")
    return 1


def cmd_resolve(a) -> int:
    pair, cid = _two(a, "item")
    if not valid_item_id(cid):
        raise EngineError("usage", "Items look like skill:greet-visitors (hermes twin conflicts shows them).")
    svc = _svc()
    pid = _one(svc, pair)
    res = svc.resolve(pid, cid, a.keep)
    what = _item_words(cid)
    if a.keep == "grok":
        _out(f"Kept the Grok version of {what}. It replaced the one in Hermes."
             + (f" Undo: {_undo_words(svc, pid, res['restore_point'], bool(pair))}" if res.get("restore_point")
                else ""))
    else:
        _out(f"Kept the Hermes version of {what}. Grok keeps its own until you send or copy this one over.")
    return 0


def _version_words(p: dict) -> str:
    """One earlier version in words: "description · Before an update from Grok · 28 min ago"."""
    item = str(p.get("item") or str(p.get("id") or "").split("@", 1)[0])
    kind = _kind_word(item_kind(item))
    what = _item_words(item) + (f" ({kind})" if ":" in item and kind else "")
    return " · ".join(x for x in (what, str(p.get("reason") or ""), ago(p.get("at"))) if x)


def _undo_words(svc, pid: str, point_id: Any, named: bool) -> str:
    """"hermes twin restore 1" when the number of that earlier version can be told; else where to look."""
    try:
        ids = [p["id"] for p in svc.restore_points(pid)["points"]]
        if point_id in ids:
            target = f"{_call(_twin_names(svc), pid)} " if named or len(svc.pair_ids()) > 1 else ""
            return f"hermes twin restore {target}{ids.index(point_id) + 1}"
    except Exception:
        pass
    return "see hermes twin versions"


def cmd_restore_points(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    pts = svc.restore_points(pid)["points"]
    if getattr(a, "json", False):
        _out(json.dumps({"points": pts}, indent=1, ensure_ascii=False))
        return 0
    if not pts:
        _out("No earlier versions yet. When a check replaces something in the twin, the version before is "
             "kept here.")
        return 0
    # numbered, newest first: the number is what `restore` takes (the long names are in --json)
    for n, p in enumerate(pts, 1):
        _out(f" {n:>2}. {_version_words(p)}")
    target = f"{_call(_twin_names(svc), pid)} " if len(svc.pair_ids()) > 1 else ""
    _out(f"Put one back: hermes twin restore {target}NUMBER")
    return 0


def cmd_restore(a) -> int:
    pair, point = _two(a, "number of the earlier version")
    svc = _svc()
    pid = _one(svc, pair)
    if str(point).isdigit():  # its number in `hermes twin versions`
        pts = svc.restore_points(pid)["points"]
        n = int(point)
        if not 1 <= n <= len(pts):
            raise EngineError("usage", f"There is no earlier version {n}." if pts else "There are no earlier "
                              "versions yet.", "hermes twin versions lists them, numbered.")
        point = pts[n - 1]["id"]
    elif not valid_item_id(point):
        raise EngineError("usage", "Give the number of the earlier version (hermes twin versions shows them).")
    res = svc.restore(pid, point)
    if not res["restored"]:
        _out(res.get("note") or "Nothing to put back.")
        return 0
    _out(f"Put back the earlier version of {', '.join(_item_words(i) for i in res['restored'])}."
         + (" Your version from just now is kept too (hermes twin versions)." if res.get("restore_point") else ""))
    for item in res.get("waiting_for_ok", []):
        _out(f" {_item_words(item)} now differs from Grok until you send it: hermes twin approve \"{item}\"")
    return 0


def cmd_recent(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    entries = svc.recent(pid, a.limit)["entries"]
    if not entries:
        _out("Nothing yet. Changes in Grok or Hermes show up here.")
    for e in entries:
        side = {"grok": "Grok", "hermes": "Hermes", "both": "both"}.get(e["side"], "Hermes")
        _out(f" {ago(e['at']):>12} · {side:<6} · {e['summary']}")
    return 0


def cmd_parity(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    res = svc.parity(pid)
    if res.get("total") is None:
        _out(res.get("note") or "Nothing to compare yet.")
        return 0
    _out(f"Compare with Grok: {res['passed']} of {res['total']} match")
    for c in res["checks"]:
        _out(f" {'✓' if c['pass'] else '✗'} {c['name']}" + ("" if c["pass"] else f": {c.get('detail', '')}"))
    return 0 if res["passed"] == res["total"] else 1


def cmd_copies(a) -> int:
    """The whole table of what copies over (twin/copies.py: the one the page and the README show)."""
    from . import copies
    if getattr(a, "json", False):
        _out(json.dumps({"table": copies.table()}, indent=2, ensure_ascii=False))
        return 0
    _out("What copies over, and how:")
    for line in copies.lines():
        _out(line if line.startswith("  ") else f"\n{line}")
    return 0


def cmd_checklist(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    res = svc.checklist(pid)
    _out("You do these yourself:")
    for b in res["by_hand"]:
        where = "in Grok" if b.get("side") == "grok" else "in Hermes"
        _out(f" ☐ [{where}] {b.get('name') or b.get('item')}: {b.get('what')}")
    if not res["by_hand"]:
        _out(" ✓ Nothing left.")
    sw = res["switch"]
    _out("Move routines to Hermes:")
    if res.get("blocked_by"):
        _out(f" ! {res['blocked_by']}")
    for r in sw["grok_routines_to_turn_off"]:
        _out(f" ☐ Turn off in Grok: {r['name']}")
    for j in sw["hermes_jobs_paused"]:
        _out(f" ☐ Turn on in Hermes: {j['name']} ({j['schedule']})")
    for j in sw.get("hermes_jobs_resumed") or []:
        _out(f" ✓ On in Hermes: {j['name']} ({j['schedule']})")
    if not sw["hermes_jobs_paused"]:
        _out(" ✓ No routines left to turn on in Hermes.")
    else:
        _out(" Turn them on with: hermes twin switch")
    return 0


def cmd_switch(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    if getattr(a, "undo", False):
        res = svc.switch(pid, None, undo=True)
        n = len(res.get("paused") or [])
        _out(f"Paused {plural(n, 'routine')} in Hermes again." if n else "No routines to pause again.")
        for f in res.get("failed") or []:
            _out(f"  {f['job_id']}: {f['error']}")
        return 1 if res.get("failed") else 0
    plan = svc.checklist(pid)
    if plan.get("blocked_by"):
        _err(plan["blocked_by"])
        return 1
    jobs = plan["switch"]["hermes_jobs_paused"]
    if not jobs:
        _out("No routines to turn on in Hermes.")
        return 0
    for j in jobs:
        _out(f" will turn on in Hermes: {j['name']} ({j['schedule']})")
    _out("Turn the matching routines off in Grok first, so nothing runs twice.")
    if not a.yes and not _confirm(f"Turn on {plural(len(jobs), 'routine')} in Hermes?"):
        _out("Nothing changed.")
        return 1
    res = svc.switch(pid, [j["job_id"] for j in jobs])
    _out(f"Turned on {plural(len(res['resumed']), 'routine')} in Hermes."
         + (f" Failed: {len(res['failed'])}" if res["failed"] else "")
         + (" Undo: hermes twin switch --undo" if res["resumed"] else ""))
    for f in res["failed"]:
        _out(f"  {f['job_id']}: {f['error']}")
    return 1 if res["failed"] else 0


IMPORT_FILE_CAP = 8 * 1024 * 1024  # bytes read from the file the owner names (the text is capped again after)
_ROUTINE_MARK = {"new": "+", "added": "+", "exists": "=", "held": "!", "manual": "!", "failed": "×"}
_ROUTINE_WORDS = {"new": "arrives switched off", "added": "added, switched off",
                  "held": "not copied: looks like a password or key"}


def _read_export_file(path: str) -> str:
    """The file the owner named, as text: a regular file, read once, capped. Never a folder, a pipe or a device."""
    import stat as _stat
    problem = EngineError("usage", "That file could not be read.",
                          "Give the setup export the bot answered with: hermes twin import FILE")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    except (OSError, ValueError, TypeError):
        raise problem
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise problem
        if st.st_size > IMPORT_FILE_CAP:
            raise EngineError("too_large", "That file is too large to be a setup export.")
        chunks, total = [], 0
        while total <= IMPORT_FILE_CAP:
            chunk = os.read(fd, min(1 << 20, IMPORT_FILE_CAP + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > IMPORT_FILE_CAP:
            raise EngineError("too_large", "That file is too large to be a setup export.")
        return b"".join(chunks).decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise problem
    finally:
        os.close(fd)


def _line_words(nums: list) -> str:
    if len(nums) == 1:
        return f"line {nums[0]}"
    if len(nums) <= 3:
        return f"lines {', '.join(str(n) for n in nums[:-1])} and {nums[-1]}"
    return f"lines {nums[0]}, {nums[1]} and {len(nums) - 2} more"


def _show_export_text(res: dict) -> None:
    """`--show`: the instructions and every prompt an import would bring in, whole, exactly as they would be
    saved. This is the read before the question; nothing else of the file is ever printed."""
    ins = res["instructions"]
    if ins["state"] == "new" and ins["text"]:
        _out("\n--- Instructions, as they would be saved ---")
        _out(ins["text"])
    for r in res["routines"]:
        if r["state"] == "new" and r["prompt"]:
            _out(f"\n--- What \"{r['name']}\" would run ---")
            _out(r["prompt"])
    _out("---")


def _show_setup_export(res: dict, twin: str) -> None:
    """What an import would do (or did): counts, sizes, names and line numbers. The instructions and the prompts
    themselves are printed only with --show."""
    bot = res["bot"]
    _out(f"Setup export of \"{bot['name']}\" for the twin {twin}")
    if not bot["matches"]:
        _out(f" ! This export is from \"{bot['name']}\", not from {twin}.")
    ins = res["instructions"]
    if ins["state"] == "new":
        line = (f" Instructions: {plural(ins['lines'], 'line')}, {plural(len(ins['text']), 'character')}"
                + (", saved" if ins.get("saved") else ""))
        if ins["left_out"]:
            n = len(ins["left_out"])
            line += (f" ({_line_words(ins['left_out'])} left out: {'it looks' if n == 1 else 'they look'} "
                     "like a password or key)")
        _out(line)
        if ins["replaces"]:
            _out("   Replaces the instructions this twin has now. The version before is kept: hermes twin versions")
    elif ins["state"] == "same":
        _out(" Instructions: already in Hermes.")
    elif ins["state"] == "held":
        _out(f" Instructions: {ins.get('why') or 'not copied: too much of it looks like a password or key.'}")
    else:
        _out(" Instructions: the file has none.")
    if res["routines"]:
        _out(" Routines:")
    for r in res["routines"]:
        when = " ".join(x for x in (r["schedule"], r["timezone"]) if x)
        what = _ROUTINE_WORDS.get(r["state"], r["why"])
        if r["state"] == "new":
            what += f"; what it runs is {plural(len(r['prompt']), 'character')}"
        _out(f"   {_ROUTINE_MARK.get(r['state'], ' ')} {r['name']}" + (f" ({when})" if when else "") + f": {what}")
    if any(r["state"] in ("new", "added") for r in res["routines"]):
        _out("   They stay switched off until you turn them on: hermes twin switch")
    if res["connectors"]:
        _out(f" Set up on this computer: {', '.join(c['name'] for c in res['connectors'])}")
        _out("   Hermes copies no logins. Connect each of these in Hermes yourself.")
    lo = res["left_out"]
    parts = [plural(lo["memories"], "memory").replace("memorys", "memories") if lo["memories"] else "",
             (f"{lo['user_facts']} fact{'' if lo['user_facts'] == 1 else 's'} about you") if lo["user_facts"] else "",
             plural(lo["skills"], "skill") if lo["skills"] else "",
             (f"{lo['routines']} more routine{'' if lo['routines'] == 1 else 's'}") if lo.get("routines") else ""]
    parts = [x for x in parts if x]
    if parts:
        _out(f" Left out: {', '.join(parts)}.")
    _out(f" {res['privacy']}")


def cmd_import(a) -> int:
    from . import setup_export, tasks
    if getattr(a, "request", False):
        _out("Paste this into the bot's own chat in the Grok app. It answers with a file:\n")
        _out(setup_export.REQUEST)
        _out("Then: hermes twin import FILE")
        return 0
    if tasks.asked_turn():  # (the chat turn of a request from another computer never changes a twin's setup)
        raise EngineError("refused", "A request from another computer cannot import a setup export.",
                          "Import it yourself, on the Twins page or in your own terminal.")
    vals = list(getattr(a, "args", None) or [])
    if len(vals) not in (1, 2):
        raise EngineError("usage", "Give the setup export file (and the twin's name first, in quotes, when you "
                                   "have more than one twin).", "hermes twin import --request prints what to ask "
                                                                 "the bot for.")
    pair, path = (None, vals[0]) if len(vals) == 1 else (vals[0], vals[1])
    svc = _svc()
    pid = _one(svc, _twin(pair) if pair else None)
    text = _read_export_file(path)
    twin = _call(_twin_names(svc), pid)
    plan = svc.import_setup(pid, text, False, a.bot)
    _show_setup_export(plan, twin)
    todo = (plan["instructions"]["state"] == "new" or bool(plan["connectors"])
            or any(r["state"] == "new" for r in plan["routines"]))
    if not todo:
        _out("Nothing to import.")
        return 0
    if getattr(a, "show", False):
        _show_export_text(plan)
    elif plan["instructions"]["state"] == "new" or any(r["state"] == "new" for r in plan["routines"]):
        # the bot wrote this text: the owner reads it before it becomes a twin's instructions or a routine
        _out(" Read the text before you import it: add --show")
    if not a.yes and not _confirm("Import this?"):
        _out("Nothing changed.")
        return 1
    res = svc.import_setup(pid, text, True, plan["bot"]["name"])
    failed = [r for r in res["routines"] if r["state"] == "failed"]
    said = []
    if res["instructions"]["saved"]:
        said.append("instructions saved")
    if res["added"]:
        said.append(f"{plural(res['added'], 'routine')} added, switched off")
    if res["connectors"]:
        said.append(f"{plural(len(res['connectors']), 'connector')} listed to set up on this computer")
    _out("Imported: " + ("; ".join(said) if said else "nothing new") + ". Nothing was sent to Grok.")
    for r in failed:
        _out(f"  × {r['name']}: {r['why']}")
    if res["added"]:
        _out("Turn the routines on when the Grok ones are off: hermes twin switch")
    return 1 if failed else 0


# ---- several twins at once: finish (model + the request to paste) and import-exports (a folder of answers) -------
EXPORTS_DIR_HINT = "~/Downloads"
EXPORT_SUFFIXES = (".json", ".txt", ".md")
EXPORT_FILES_MAX = 40


def _batch_name(t: dict) -> str:
    return safe.clean_name(t.get("name"), 100) or str(t.get("pair_id") or "?")


def _batch_pids(svc, given: list) -> list:
    """The twins named on the command line, each once, in the order given."""
    out: list = []
    for g in given:
        pid = _resolve(svc, g)
        if pid not in out:
            out.append(pid)
    return out


def cmd_finish(a) -> int:
    """Set the model on several twins at once and print, once, what to paste into each bot's chat in Grok."""
    named = list(getattr(a, "twins", None) or [])
    if named and a.all:
        raise EngineError("usage", "Name the twins or use --all, not both.")
    if not named and not a.all:
        raise EngineError("usage", "Say which twins: hermes twin finish \"Acme Support\" \"Recipe Planner\" "
                                   "(or --all for every twin with something left).")
    from . import tasks
    if tasks.asked_turn():  # (the chat turn of a request from another computer never changes a twin's setup)
        raise EngineError("refused", "A request from another computer cannot set twins' models.",
                          "Do it yourself, on the Twins page or in your own terminal.")
    svc = _svc()
    pids = _batch_pids(svc, named) if named else None
    info = svc.batch_info(pids)
    twins = [t for t in info.get("twins") or [] if isinstance(t, dict) and t.get("pair_id")]
    samples = [_batch_name(t) for t in twins if _is_sample(t)]
    twins = [t for t in twins if not _is_sample(t)]
    if not twins and not samples:
        raise _NoTwin(NO_TWINS)
    if samples and (named or not twins):
        _out(f" {', '.join(samples)}: the sample, with invented data; nothing to finish.")
    if not twins:
        _out(f"Next: {TO_REAL_BOTS}{'add' if _setup_ready(svc) else 'setup'}.")
        return 0
    explicit = (a.model or "").strip()
    if a.all:
        twins = [t for t in twins if t.get("left") or explicit]
        if not twins:
            _out("Nothing is left: every twin has its instructions and a model.")
            return 0
    model = explicit or str(info.get("default_model") or "").strip()
    to_set = [t for t in twins if explicit or "model" in (t.get("left") or [])]
    if to_set and not model:
        names = " ".join(_call({t["pair_id"]: _batch_name(t) for t in twins}, t["pair_id"]) for t in twins[:20])
        _err("This Hermes has no model chosen yet, so say which one the twins should use:")
        _err(f"  hermes twin finish {'--all' if a.all else names} --model MODEL")
        for e in (info.get("endpoints") or [])[:6]:
            nm = safe.clean_name(e.get("name"), 60)
            ids = [safe.clean_name(m, 100) for m in (e.get("models") or [])[:20] if isinstance(m, str)]
            if nm and ids:
                where = " (local)" if e.get("local") else ""
                note = "" if e.get("reachable") else " (not answering now)"
                _err(f"  {nm}{where}{note}: --endpoint \"{nm}\" --model {' | '.join(m for m in ids if m)}")
        _err("  Grok Bot Twin never downloads a model; add a model server you already run with `hermes model`.")
        known = [safe.clean_name(m, 100) for m in (info.get("models") or [])[:20] if isinstance(m, str)]
        if known:
            _err(f"  Models this Hermes already uses: {', '.join(m for m in known if m)}")
        _err("Nothing changed.")
        return 2
    shown_model = safe.clean_name(model, 100)
    _out(f"For {plural(len(twins), 'twin')}:")
    for t in twins:
        left = t.get("left") or []
        bits = []
        if t in to_set:
            bits.append(f"model: set to {shown_model}")
        else:
            now = safe.clean_name(t.get("model"), 100)
            bits.append(f"model: stays {now}" if now else "model: chosen")
        bits.append("instructions: still to come from Grok" if "instructions" in left else "instructions: in")
        _out(f" {_batch_name(t)}: " + "; ".join(bits))
    rc = 0
    if to_set:
        if not a.yes and not _confirm(f"Set the model to {shown_model} on {plural(len(to_set), 'twin')}? "
                                      "You can change it later, twin by twin."):
            _out("Nothing changed.")
            return 1
        ep = getattr(a, "endpoint", None)
        res = svc.batch_models([t["pair_id"] for t in to_set], model, *([ep] if ep else []))
        names = {t["pair_id"]: _batch_name(t) for t in twins}
        for r in res.get("results") or []:
            name = names.get(r.get("pair_id"), safe.clean_name(r.get("name"), 100) or "?")
            state = r.get("state")
            if state == "set":
                _out(f" {name}: model set to {shown_model} ✓")
            elif state == "same":
                _out(f" {name}: already uses {shown_model} ✓")
            elif state == "sample":
                _out(f" {name}: the sample; not changed.")
            else:
                rc = 1
                _out(f" × {name}: the model could not be set"
                     + (f": {safe.clean_summary(r.get('why'), 200)}" if r.get("why") else "."))
    waiting = [t for t in twins if "instructions" in (t.get("left") or [])]
    if not waiting:
        _out("Every one of these twins has its instructions.")
        return rc
    request = str(info.get("request") or "")
    if not request:
        from . import setup_export
        request = setup_export.REQUEST
    _out("")
    _out(f"Now the instructions. Paste this into the chat of each of these bots in Grok: "
         f"{_quoted([_batch_name(t) for t in waiting])}")
    _out("--- paste from the next line ---")
    _out(request.rstrip("\n"))
    _out("--- up to the line above ---")
    _out(f"Each bot answers with a file. Save each answer file, then run: hermes twin import-exports "
         f"{EXPORTS_DIR_HINT}")
    if info.get("privacy"):
        _out(safe.clean_summary(info["privacy"], 300))
    return rc


def _read_text_capped(path: str, cap: int) -> tuple[Optional[str], str]:
    """(text, "") for a regular file read once as UTF-8, or (None, why). Never follows a link, never opens a pipe."""
    import stat as _stat
    try:
        fd = _osflags.open_nofollow(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    except (OSError, ValueError):
        return None, "could not be read"
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            return None, "not a file"
        if st.st_size > cap:
            return None, "too large to be a setup export"
        chunks, total = [], 0
        while total <= cap:
            chunk = os.read(fd, min(1 << 20, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > cap:
            return None, "too large to be a setup export"
        return b"".join(chunks).decode("utf-8-sig"), ""
    except UnicodeDecodeError:
        return None, "not text"
    except OSError:
        return None, "could not be read"
    finally:
        os.close(fd)


def _export_files(folder: str) -> tuple[list, list]:
    """([{"name","text"}], [(name, why)]): the regular files in FOLDER ending .json/.txt/.md (no links, no hidden
    files), newest first, at most EXPORT_FILES_MAX, each at most setup_export.TEXT_CAP bytes."""
    from . import setup_export
    path = os.path.expanduser(folder)
    try:
        entries = list(os.scandir(path))
    except (OSError, ValueError, TypeError):
        raise EngineError("usage", "That folder could not be read.",
                          f"Give the folder the answer files are in: hermes twin import-exports {EXPORTS_DIR_HINT}")
    import stat as _stat
    found, names = [], set()
    for e in entries:
        # (the name as it is shown and matched: one line, capped; two files that would show alike are one too many)
        name = safe.clean_name(e.name, 120)
        if not name or name in names or e.name.startswith(".") or not e.name.lower().endswith(EXPORT_SUFFIXES):
            continue
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if not _stat.S_ISREG(st.st_mode) or _osflags.is_link(st):
            continue
        names.add(name)
        found.append((st.st_mtime, name, e.path))
    found.sort(key=lambda x: (-x[0], x[1]))
    files, skipped = [], []
    for _mtime, name, full in found[:EXPORT_FILES_MAX]:
        text, why = _read_text_capped(full, setup_export.TEXT_CAP)
        if text is None:
            skipped.append((name, why))
        else:
            files.append({"name": name, "text": text})
    if len(found) > EXPORT_FILES_MAX:
        skipped.append((f"{len(found) - EXPORT_FILES_MAX} older files", "not looked at (40 at most, newest first)"))
    return files, skipped


def _file_name(name: Any) -> str:
    return safe.clean_name(name, 120) or "?"


def _plan_counts(prev: dict) -> str:
    """"instructions (12 lines), 2 routines (arrive switched off), 1 connector to set up here" for one preview."""
    bits = []
    ins = prev.get("instructions") or {}
    if ins.get("state") == "new":
        bits.append(f"instructions ({plural(int(ins.get('lines') or 0), 'line')}"
                    + (", replaces the ones it has now" if ins.get("replaces") else "") + ")")
    elif ins.get("state") == "same":
        bits.append("instructions already in")
    elif ins.get("state") == "held":
        bits.append("instructions not copied (too much looks like a password or key)")
    new = sum(1 for r in prev.get("routines") or [] if r.get("state") == "new")
    if new:
        bits.append(f"{plural(new, 'routine')} ({'arrives' if new == 1 else 'arrive'} switched off)")
    if prev.get("connectors"):
        bits.append(f"{plural(len(prev['connectors']), 'connector')} to set up here")
    return ", ".join(bits) or "nothing new"


def _has_new(prev: dict) -> bool:
    return ((prev.get("instructions") or {}).get("state") == "new" or bool(prev.get("connectors"))
            or any(r.get("state") == "new" for r in prev.get("routines") or []))


def _result_line(r: dict) -> str:
    """"Acme Helper: instructions in ✓, 2 routines added (switched off), 1 connector to set up here"."""
    name = safe.clean_name(r.get("name"), 100) or str(r.get("pair_id") or "?")
    state = r.get("state")
    if state == "failed":
        return f" × {name}: not imported" + (f": {safe.clean_summary(r.get('why'), 200)}" if r.get("why") else ".")
    if state == "no_file":
        return f" {name}: no file for it yet"
    if state == "sample":
        return f" {name}: the sample; not changed"
    bits = ["instructions in ✓" if r.get("instructions") else "instructions unchanged"]
    n = int(r.get("routines") or 0)
    if n:
        bits.append(f"{plural(n, 'routine')} added (switched off)")
    c = int(r.get("connectors") or 0)
    if c:
        bits.append(f"{plural(c, 'connector')} to set up here")
    lo = int(r.get("lines_left_out") or 0)
    if lo:
        bits.append(f"{plural(lo, 'line')} left out (looked like a password or key)")
    return f" {name}: " + ", ".join(bits)


MATCH_WORDS = {"not_export": "not a setup export", "ambiguous": "more than one twin could take it"}


def cmd_import_exports(a) -> int:
    """Every setup export in a folder, each into the twin of its bot: plan, one question, import."""
    from . import tasks
    if tasks.asked_turn():  # (the chat turn of a request from another computer never changes a twin's setup)
        raise EngineError("refused", "A request from another computer cannot import a setup export.",
                          "Import it yourself, on the Twins page or in your own terminal.")
    svc = _svc()
    pids = _batch_pids(svc, list(a.twin or [])) if a.twin else None
    if pids is None and not [p for p in svc.pair_ids() if p not in svc.demo.pair_ids()]:
        raise _NoTwin(NO_TWINS)
    files, skipped = _export_files(a.folder)
    for name, why in skipped:
        _out(f" {_file_name(name)}: {why}")
    if not files:
        _out("No setup export files there (it looks at .json, .txt and .md files).")
        return 1
    texts = {f["name"]: f["text"] for f in files}
    matches = [m for m in svc.batch_match(files, pids).get("matches") or [] if isinstance(m, dict)]
    _out(f"Setup exports found: {plural(len(files), 'file')}")
    plans = []  # (match, preview)
    for m in matches:
        fname = _file_name(m.get("file"))
        state = m.get("state")
        if state == "matched" and m.get("pair_id") and m.get("file") in texts:
            twin = safe.clean_name(m.get("name"), 100) or m["pair_id"]
            try:
                prev = svc.import_setup(m["pair_id"], texts[m["file"]], False, m.get("bot") or None)
            except EngineError as exc:
                _out(f" {fname} -> {twin}: {exc.message}")
                continue
            plans.append((m, prev))
            _out(f" {fname} -> {twin}: {_plan_counts(prev)}")
        elif state == "no_twin":
            bot = safe.clean_name(m.get("bot"), 100)
            _out(f" {fname}: no twin for the bot \"{bot}\"" if bot else f" {fname}: no twin for its bot")
        elif state == "ambiguous":
            why = safe.clean_summary(m.get("why"), 200) or MATCH_WORDS["ambiguous"]
            bot = safe.clean_name(m.get("bot"), 100)
            _out(f" {fname}" + (f" (\"{bot}\")" if bot else "") + f": {why}")
        else:
            why = MATCH_WORDS.get(state) or safe.clean_summary(m.get("why"), 200) or "not used"
            _out(f" {fname}: {why}")
    todo = [(m, p) for m, p in plans if _has_new(p)]
    if not todo:
        _out("Nothing to import.")
        return 0 if plans else 1
    if getattr(a, "show", False):
        for m, prev in todo:
            _out(f"\n=== {safe.clean_name(m.get('name'), 100) or m['pair_id']}, from {_file_name(m.get('file'))} ===")
            _show_export_text(prev)
    elif any((p.get("instructions") or {}).get("state") == "new" or any(r.get("state") == "new"
             for r in p.get("routines") or []) for _m, p in todo):
        # the bots wrote this text: the owner reads it before it becomes a twin's instructions or a routine
        _out(" Read the text before you import it: add --show")
    if todo[0][1].get("privacy"):
        _out(" " + safe.clean_summary(todo[0][1]["privacy"], 300))
    if not a.yes and not _confirm(f"Import into {plural(len({m['pair_id'] for m, _p in todo}), 'twin')}?"):
        _out("Nothing changed.")
        return 1
    chosen = [m["pair_id"] for m, _p in todo]
    send = [f for f in files if f["name"] in {m.get("file") for m, _p in todo}]
    res = svc.batch_import(send, list(dict.fromkeys(chosen)))
    results = [r for r in res.get("results") or [] if isinstance(r, dict) and r.get("pair_id") in chosen]
    for r in results:
        _out(_result_line(r))
    _out("Nothing was sent to Grok.")
    if any(int(r.get("routines") or 0) for r in results):
        _out("Turn the routines on when the Grok ones are off: hermes twin switch")
    if any(int(r.get("connectors") or 0) for r in results):
        _out("Hermes copies no logins. Connect each connector in Hermes yourself.")
    return 1 if any(r.get("state") == "failed" for r in results) else 0


def cmd_send(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    try:
        before = svc.pair_status(pid)
    except EngineError:
        before = {}
    res = svc.send_to_grok(pid)
    skills, n = res["skills"], res["digest_entries"]
    if not skills and not n:
        _out("Nothing to send to Grok right now.")
        return 0
    sent = f"Sent {plural(skills, 'skill')}" + (f" and {n} digest {'entry' if n == 1 else 'entries'}" if n else "")
    if _is_sample(before):
        _out(f"{sent}. (Sample: the invented Grok side took it at once.)")
    elif (before.get("mirror_bot") or {}).get("connected"):
        _out(f"{sent}. It arrives when the sync bot's routine next runs in Grok.")
    else:
        _out(f"{plural(skills, 'skill')} changed in Hermes. Copy {'it' if skills == 1 else 'them'} into Grok "
             "yourself, or turn on skill sync: hermes twin skill-sync --folder DIR")
    return 0


def cmd_approve(a) -> int:
    pair, item = _two(a, "item")
    if not valid_item_id(item):
        raise EngineError("usage", "Items look like skill:greet-visitors (hermes twin status shows them).")
    svc = _svc()
    pid = _one(svc, pair)
    res = svc.approve(pid, item)
    target = f" {_call(_names(svc), pid)}" if pair else ""
    for x in res["approved"]:
        what = _item_words(x["item"])
        _out(f"Approved {what}. " + (f"It goes to Grok at the next send: hermes twin send-to-grok{target}."
                                     if x.get("to") != "local" else "It is in the twin now."))
    s = res.get("sync")
    if s:
        _out(f" {_names(svc).get(s['pair_id'], s['pair_id'])}: {STATE_LABELS.get(s['state'], s['state'])}")
    return 0


def cmd_skip(a) -> int:
    pair, item = _two(a, "item")
    if not valid_item_id(item):
        raise EngineError("usage", "Items look like skill:greet-visitors (hermes twin status shows them).")
    svc = _svc()
    undo = bool(getattr(a, "undo", False))
    svc.skip(_one(svc, pair), item, undo)
    what = _item_words(item)
    _out(f"{what} will be offered for Grok again." if undo
         else f"{what} stays in Hermes only. Offer it again: hermes twin skip \"{item}\" --undo")
    return 0


def cmd_mirror(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    add = [n for v in (getattr(a, "take", None) or []) for n in v.split(",") if n]
    remove = [n for v in (getattr(a, "untake", None) or []) for n in v.split(",") if n]
    every = True if getattr(a, "take_all", False) else (False if getattr(a, "take_none", False) else None)
    if add or remove or every is not None:
        others = [f for f, on in (("--off", a.off), ("--status", getattr(a, "status", False)),
                                  ("--new-pass", getattr(a, "rotate_key", False)), ("--folder", a.pair_folder),
                                  ("--again", getattr(a, "again", False))) if on]
        if others:
            raise EngineError("usage", f"Choose skills made in Grok on their own, without {others[0]}.")
        from . import tasks
        if tasks.asked_turn():  # (the chat turn of a request from another computer never changes a twin's setup)
            raise EngineError("usage", "Choose the skills made in Grok from your own terminal or the Twins page.")
        st = svc.skill_sync_take(pid, add, remove, every)
        taken = st["grok_skills"]["taken"]
        if taken == ["*"]:
            _out("Every skill in Grok's shared library comes back to this twin from the sync bot's next run. A new one "
                 "waits for your approval in Hermes; later changes made in Grok come back like any change from Grok.")
        elif taken:
            _out(f"From the sync bot's next run these skills made in Grok come back to this twin: {', '.join(taken)}.")
        else:
            _out("No skill made in Grok comes back to this twin. Skills it sent to Grok still do.")
        return 0
    if getattr(a, "rotate_key", False):
        res = svc.mirror(pid, None, False, True)
        _out(res["note"])
        return 0
    if a.off:
        _out("Turning off skill sync: skills go back to copy-yourself. The pass is replaced, so the sync bot "
             "must be set up again to turn this back on.")
        if not getattr(a, "yes", False) and not _confirm("Turn off skill sync?"):
            _out("Nothing changed.")
            return 1
    if a.off:
        svc.mirror(pid, None, True)
        _out("Skill sync is off. Skills go back to copy-yourself.")
        return 0
    if getattr(a, "status", False):
        st = svc.skill_sync_status(pid)
        if not st["connected"]:
            _out("Skill sync is off for this twin.")
            return 0
        _out(st["heartbeat"].rstrip(".") + ".")
        if st["last_run_at"] and not st["served"] and not st["box"]["note"]:
            _out("The sync bot has not picked this twin up yet; it does on its next run.")
        if st["box"]["note"]:
            _out(st["box"]["note"])
        gs = st.get("grok_skills") or {}
        if gs.get("taken"):
            _out("Skills made in Grok that come back here: "
                 + ("every one" if gs["taken"] == ["*"] else ", ".join(gs["taken"])) + ".")
        if gs.get("note"):
            _out(gs["note"])
        _out(st["credit_note"])
        return 0
    res = svc.skill_sync_setup(pid, a.pair_folder, bool(getattr(a, "again", False)))
    kit, texts = res["kit"], None
    if kit["folder"]:
        texts = os.path.join(os.path.dirname(kit["folder"]), "texts")
    if res["via"] == "inbox":
        _out("Skill sync is on. Added through the shared folder: the sync bot picks this twin up on its next run; "
             "nothing to do in Grok.")
        _out(res["credit_note"])
        return 0
    _out("Skill sync is on. Finish in the Grok app:")
    where = (lambda name: f": {os.path.join(texts, name)}") if texts else (lambda name: "")
    _out(f" 1. Make a new bot to be the sync bot and paste its instructions{where('INSTRUCTIONS.md')}")
    _out(" 2. Attach every file in the set-up folder to one message in the sync bot's chat (attach the files; never "
         "paste what is in one)" + (f": {kit['folder']}" if kit["folder"] else ""))
    _out(f"    with the first message{where('FIRST-MESSAGE.txt')}")
    for m in kit["missing"]:
        _out(f"    One file is missing: {m['file_name']} (botport for the sync bot's box). Download it"
             + (f" from {m['url']}" if m["url"] else "") + ", leave it in your Downloads folder, then run this "
             "command again.")
    _out(f" 3. Give the sync bot a routine with this prompt{where('ROUTINE-PROMPT.txt')}")
    _out(" 4. After its first run: hermes twin skill-sync --status")
    _out(res["credit_note"] + " Once an hour is plenty.")
    return 0


def _lines(title: str, lines) -> None:
    for i, line in enumerate(lines):
        _out(f"  {title if i == 0 else ' ' * len(title)} {line}")


def cmd_remove(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    name = _names(svc).get(pid, pid)
    plan = svc.remove(pid, True)
    sample = bool(plan.get("sample", pid in svc.demo.pair_ids()))
    # the last sample twin ends the sample; with two of them, removing one leaves the sample on
    ends = sample and len(svc.demo.pair_ids()) <= 1
    ask = ("End the sample?" if ends else f"Remove the sample twin of {name}?") if sample else f"Stop twinning {name}?"
    if sample:
        _out(f"{ask} Nothing of yours changes.")
    else:
        _out(ask)
        _lines("Stays:  ", plan.get("stays") or REMOVE_STAYS)
        _lines("Removed:", plan.get("goes") or REMOVE_GOES)
        _out(f"  {plan.get('again') or REMOVE_AGAIN}")
    if a.dry_run:
        _out("  Files:")
        for r in plan["removed"]:
            _out(f"    {r}")
        _out("Nothing changed (dry run).")
        return 0
    if not a.yes and not _confirm(ask):
        _out("Nothing changed.")
        return 1
    svc.remove(pid, False)
    if sample:
        _out("Sample removed. Nothing of yours was changed." if ends
             else f"The sample twin of {name} is removed. Nothing of yours was changed.")
    else:
        _out(f"{name} is no longer twinned. Its Hermes bot is still here.")
    return 0


def cmd_uninstall(a) -> int:
    svc = _svc()
    plan = svc.uninstall(True)
    _out("Remove Grok Bot Twin?")
    _lines("Stays:  ", plan.get("stays") or UNINSTALL_STAYS)
    # the service lists only what exists now; an install that holds nothing has nothing to remove
    goes = plan.get("goes") if isinstance(plan.get("goes"), list) else list(UNINSTALL_GOES)
    _lines("Removed:", goes or ["Nothing: Grok Bot Twin holds no data on this computer."])
    if plan.get("skill_sync"):
        _out(f"  {UNINSTALL_SKILL_SYNC} (skill sync is on for: {', '.join(plan['skill_sync'])})")
    n = int(plan.get("twins") or 0)
    if n and plan.get("sample"):
        _out(f"  This stops twinning {plural(n, 'bot')} and ends the sample.")
    elif n:
        _out(f"  This stops twinning {plural(n, 'bot')}.")
    elif plan.get("sample"):
        _out("  This ends the sample.")
    if a.dry_run:
        _out("Nothing changed (dry run).")
        return 0
    if not a.yes and not _confirm("Remove Grok Bot Twin?"):
        _out("Nothing changed.")
        return 1
    res = svc.uninstall(False)
    _out("Grok Bot Twin's data is removed. Your Grok bots, your Hermes bots and their chats are untouched.")
    _out(res.get("next") or UNINSTALL_NEXT)
    _out("Or, in the terminal: hermes plugins remove grok-bot-twin")
    return 0


def _setup_info(svc) -> tuple[str, str]:
    from .engine import EXPECTED_BOTPORT_TAG
    try:
        info = svc.setup_state()
    except Exception:
        info = {}
    tag = info.get("release_tag") or EXPECTED_BOTPORT_TAG
    return tag, info.get("download_url") or f"https://github.com/hudsonwa/botport/releases/tag/{tag}"


GH_MISSING = ("This check needs GitHub's command-line tool. Install it, run gh auth login, then run "
              "hermes twin setup --with-github again.")


def cmd_setup(a) -> int:
    """One setup command for every way in (the owner, a coding agent, Hermes in a chat). `--check` says where this
    computer stands and `--plan` what a run with the same options would do; neither changes anything. A run does
    only what is left: it verifies botport as it always did, and asks once (or takes --yes) before it uses a
    download it found by itself, twins a bot, shares or links. See onboard.py."""
    from . import onboard
    svc = _svc()
    onboard.check_usage(svc, a)
    plan = onboard.build(svc, a)
    as_json = bool(getattr(a, "json", False))
    if getattr(a, "check", False) or getattr(a, "plan", False):
        if as_json:
            _out(json.dumps(onboard.public(plan), indent=1, ensure_ascii=False))
        else:
            for line in onboard.lines(plan, "check" if a.check else "plan"):
                _out(line)
        # --check: 0 when nothing is left on this computer; --plan only describes
        return 0 if getattr(a, "plan", False) or plan["complete"] else 1
    todo = [st for st in plan["steps"] if st["state"] == onboard.WILL]
    if not any(st["ask"] for st in todo):
        if plan["kind"] == "grok":
            # nothing here needs a yes: verify botport (or say how to get it), as this command always did
            twins = onboard.has_twins(svc)
            rc = _setup_botport(a, svc, tail=not twins)
            if rc == 0 and twins:
                _left_for_you(onboard, svc)
            return rc
        for line in onboard.lines(plan, "idle"):  # a computer that links: linked already, or the code is missing
            _out(line)
        return 0 if plan["complete"] else 1
    for line in onboard.lines(plan, "run"):
        _out(line)
    if not getattr(a, "yes", False) and not _confirm("Go ahead?"):
        _out(onboard.NOTHING_CHANGED)
        return 1
    want = onboard._wants(a)
    for st in todo:
        if st["id"] == "botport":
            if st.get("from_downloads"):  # the download setup found by itself, by its exact name
                svc.choose_helper(None, True)
                _out("Hermes now keeps its own copy of botport; you can delete the download.")
            rc = _setup_botport(a, svc, tail=False)
        elif st["id"] == "twins":
            rc = onboard.twin_bots(svc, want["bots"], want["all"], _out, _err)
        elif st["id"] == "share":
            rc = onboard.share_on(svc, _out)
        else:
            rc = onboard.link(svc, want["link"], _out)
        if rc:
            _err("Setup stopped at this step; the steps before it are done. " + onboard.RUN_AGAIN)
            return rc
    _left_for_you(onboard, svc)
    return 0


def _left_for_you(onboard, svc) -> None:
    """After a run: what is left, and who does it (the same lines --check prints)."""
    svc.invalidate()
    after = onboard.build(svc, argparse.Namespace())
    left = [st for st in after["steps"] if st["state"] in (onboard.YOU, onboard.WAITS)]
    if left:
        _out("Left for you:")
        for st in left:
            _out(f" {onboard.MARK[st['state']]} {st['title']}")
            if st["how"]:
                _out(f"     {st['how']}")
    else:
        _out("Setup is complete on this computer.")
    _out("Check any time: hermes twin setup --check")


def _setup_botport(a, svc, tail: bool = True) -> int:
    """Verify botport once (owner step): ownership/permissions, then its SHA-256 against the release files this
    plugin version lists (no other program needed), then version + features. --with-github asks for GitHub's
    signed build record instead and is the only place the GitHub route is printed (the download command, the
    exact check that runs). --allow-unattested is the loud, recorded opt-out for a build of your own.
    --file FILE first takes the downloaded file (or archive)."""
    from .engine import UNATTESTED_TEXT, attestation_command, release_target, terminal_steps
    with_github = bool(getattr(a, "with_github", False))
    if getattr(a, "helper", None):
        svc.choose_helper(os.path.abspath(os.path.expanduser(a.helper)))
        _out("Hermes now keeps its own copy of botport; you can delete the download.")
    bp = svc.botport()
    tag, url = _setup_info(svc)
    if bp is None:
        archive = f"botport-{release_target()}.tar.gz"
        _err("botport, a small free app that reads your bot list from Grok, is not on this computer yet.")
        _err(f"Easiest: open {PAGE} and press Connect my Grok bots.")
        _err(f"In the terminal: download {archive} from {url}")
        _err("Then run this, with the place you saved it:")
        _err(f"  hermes twin setup --file ~/Downloads/{archive}")
        if with_github:
            _err("With GitHub's command-line tool (signed in once with gh auth login):")
            for step in terminal_steps(tag):  # one source for these lines: download, then hand the file over
                _err(f"  {step}")
        return 1
    if a.allow_unattested:
        _err("WARNING: --allow-unattested accepts botport WITHOUT verifying it. Use it only for a botport you "
             "built yourself. This is recorded and shown by hermes twin doctor and status.")
    tag = svc.release_tag(getattr(a, "tag", None))
    if a.allow_unattested:
        with_github = False
    elif with_github:  # the owner asked for GitHub's build record: say exactly what runs
        _out(f"Verifying botport against the publisher's signed build record on GitHub (release {tag}):")
        _out("  " + " ".join(attestation_command(bp.path, tag)))
    else:
        _out(f"Verifying botport (release {tag}).")
    try:
        res = (svc.trust_botport(False, tag, "gh") if with_github
               else svc.trust_botport(bool(a.allow_unattested), tag))
    except EngineError as exc:
        if exc.code == "gh_missing" or (exc.code == "attestation_failed" and "gh" in exc.message
                                        and "not on PATH" in exc.message):
            _err(f"✗ {GH_MISSING}")
            return 1
        if exc.code in ("check_failed", "attestation_failed"):
            _err(f"✗ {exc.message}")
            _err(f"  Download botport again from {url}, then run hermes twin setup --file with the new file.")
            return 1
        raise
    sha = f" Fingerprint (SHA-256) {str(res['sha256'] or '')[:12]}…"
    if res["attested"]:
        _out(f"botport {res['version'] or ''} is verified (release {res['release_tag']})."
             .replace("  ", " ") + sha)
    else:
        _out(f"Accepted botport {res['version'] or ''} as it is: {UNATTESTED_TEXT}."
             .replace("  ", " ") + sha)
    if tail:
        _out(f"Next: {CONNECT[0].lower() + CONNECT[1:]}, or run hermes twin add.")
    return 0


def cmd_attest(a) -> int:
    res = _svc().attest_botport(a.tag)
    _out("botport matches the publisher's signed build record on GitHub." if res["verified"]
         else f"The check against GitHub's build record did NOT pass: {res['problem']}")
    return 0 if res["verified"] else 1


def cmd_facts(a) -> int:
    """Memory facts between a Grok bot and its twin (twin/mem_facts.py). Off until switched on."""
    from . import mem_facts as mf, mem_layers as ml, tasks
    words = [str(w) for w in (a.words or [])]
    verbs = ("sync", "keep", "restore", "way")
    twin = None
    if words and words[0] not in verbs:
        twin, words = words[0], words[1:]
    svc = _svc()
    pid = _one(svc, twin)
    prof = mf._pairs(svc).get(pid)
    if prof is None:
        raise EngineError("no_twin", "That twin has no Hermes bot on this computer.")
    if tasks.asked_turn():  # a request from another computer neither reads nor changes a twin's memory facts
        raise EngineError("refused", "A request from another computer cannot read or change a twin's memory facts.",
                          "Do it yourself, on the Twins page or in your own terminal.")
    res = None
    if words[:1] == ["sync"] and len(words) == 1:
        res = mf.sync_twin(svc, pid)
    elif words[:1] == ["keep"] and len(words) == 3:
        res = mf.resolve(prof, words[1], words[2], key=pid)
    elif words[:1] == ["restore"] and len(words) == 2:
        res = mf.restore(prof, words[1])
    elif words[:1] == ["way"] and len(words) == 2:
        if ml._panel() is not None:
            raise EngineError("usage", "Choose the direction in the What syncs panel.",
                              "hermes twin syncs TWIN memory from-grok|to-grok|both|off")
        ml.set_direction(pid, "facts", words[1])
    elif words:
        raise EngineError("usage", "Use: hermes twin facts [TWIN] [sync | keep CONFLICT hermes|grok | restore POINT]")
    v = mf.view(prof, key=pid)
    if a.json:
        _out(json.dumps({"facts": v, **({"result": res} if res is not None else {})}, indent=1, ensure_ascii=False))
        return 0
    if res is not None and res.get("state"):
        th, tg = res.get("to_hermes") or {}, res.get("to_grok") or {}
        _out(f"Sync: {res['state']}. Into Hermes: {th.get('added', 0)} added, {th.get('updated', 0)} changed, "
             f"{th.get('removed', 0)} removed. To Grok: {tg.get('added', 0)} added, {tg.get('updated', 0)} changed, "
             f"{tg.get('removed', 0)} removed.")
    _out(f"Memory facts: {v['direction_words']}. {v['sentence']}")
    _out(f"  {v['facts']} shared facts; last synced {v['last'] or 'never'}.")
    for line in v["held"]:
        _out(f"  Kept back: {line}")
    if v["no_room"]:
        _out(f"  {v['no_room']} not added: this twin's memory is full.")
    for c in v["conflicts"]:
        _out(f"  Differs ({c['why']}), {c['id']}:")
        _out(f"    Hermes: {c['hermes'] if c['hermes'] is not None else '(removed)'}")
        _out(f"    Grok:   {c['grok'] if c['grok'] is not None else '(removed)'}")
        _out(f"    keep one: hermes twin facts keep {c['id']} hermes|grok")
    return 0


def cmd_digest(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    if a.on or a.off:
        svc.set_digest(pid, bool(a.on), bool(a.titles))
    prev = svc.digest_preview(pid)
    _out(f"Activity digest to Grok: {'on' if prev['enabled'] else 'off'}"
         + (" (with short titles)" if prev["titles"] else " (counts and times only)"))
    _out("Exactly what would be sent:")
    _out(json.dumps({"counts": prev["counts"], "entries": prev["entries"]} if prev["enabled"] else {}, indent=1))
    return 0


def cmd_syncs(a) -> int:
    """The "What syncs" panel of one twin, and changing one of its rows."""
    from . import sync_panel as sp, tasks
    words = [str(w) for w in (a.words or [])]
    if len(words) > 3:
        raise EngineError("usage", "Too many words. Use: hermes twin syncs [TWIN] [ITEM DIRECTION]")
    twin = item = direction = None
    if len(words) == 3:
        twin, item, direction = words
    elif len(words) == 2 and sp.item_word(words[0]) and sp.direction_word(words[1]):
        item, direction = words
    elif len(words) == 2:
        raise EngineError("usage", f"\"{safe.clean_name(words[0], 60)} {safe.clean_name(words[1], 60)}\" is not an "
                                   "item and a direction.",
                          "Items: name, description, instructions, skills, routines, memory, briefs, conversations. "
                          "Directions: from-grok, to-grok, both, off")
    elif words:
        twin = words[0]
    svc = _svc()
    pid = _one(svc, twin)
    said = ""
    if item is not None:
        key, way = sp.item_word(item), sp.direction_word(direction)
        if not key or not way:
            raise EngineError("usage", f"\"{safe.clean_name(item if not key else direction, 60)}\" is not "
                                       + ("an item." if not key else "a direction."),
                              "Items: name, description, instructions, skills, routines, memory, briefs, "
                              "conversations. Directions: from-grok, to-grok, both, off")
        if tasks.asked_turn():  # (the chat turn of a request from another computer never changes a twin's setup)
            raise EngineError("refused", "A request from another computer cannot change what syncs.",
                              "Change it yourself, on the Twins page or in your own terminal.")
        view = sp.choose(svc, pid, key, way)
        said = view.get("said", {}).get("sentence", "")
    else:
        view = sp.view(svc)
    twin_view = next((t for t in view.get("twins", []) if t.get("id") == pid), None)
    if twin_view is None:
        raise EngineError("no_twin", "That twin is not on this computer.")
    if a.json:
        _out(json.dumps({"schema": view["schema"], "mirror": view["mirror"], "twins": [twin_view],
                         **({"said": view["said"]} if "said" in view else {})}, indent=1, ensure_ascii=False))
        return 0
    if said:
        _out(said)
        _out()
    for line in sp.lines(twin_view, ago):
        _out(line)
    if twin_view["mirror"] != "ready" and any(it["state"] == "needs_mirror" for it in twin_view["items"]):
        _out(view["mirror"]["sentence"] + " Set it up with `hermes twin skill-sync`, or Skill sync… in the twin's "
             "Details on the Twins page.")


_BRIEF_WORDS = {"from-grok": "grok_to_hermes", "to-grok": "hermes_to_grok", "both": "both", "off": "off",
                "grok_to_hermes": "grok_to_hermes", "hermes_to_grok": "hermes_to_grok"}
_BRIEF_SAID = {"grok_to_hermes": "Grok → Hermes", "hermes_to_grok": "Hermes → Grok", "both": "both ways", "off": "off"}


def _brief_round(r: dict) -> list:
    """One round's counts as plain sentences."""
    if r.get("failed"):
        return ["That round did not finish. It is tried again on the next check."]
    out = []

    def were(n: int) -> str:
        return "was" if n == 1 else "were"

    if r["made"]:
        out.append(f"Made {plural(r['made'], 'brief')} of finished Hermes chats.")
    if r["sent"]:
        out.append(f"Handed {plural(r['sent'], 'brief')} to the Grok bot.")
    if r["waiting"]:
        out.append(f"{plural(r['waiting'], 'brief')} {'waits' if r['waiting'] == 1 else 'wait'} on this computer "
                   "until the Mirror can carry them.")
    if r["received"]:
        out.append(f"Took in {plural(r['received'], 'brief')} from the Grok bot.")
    if r["refused"]:
        out.append(f"{plural(r['refused'], 'file')} from the Grok bot {were(r['refused'])} not a brief and "
                   f"{were(r['refused'])} left alone.")
    if r["left_out"]:
        out.append(f"{plural(r['left_out'], 'line')} {were(r['left_out'])} left out for privacy.")
    return out or ["Nothing new to brief."]


def cmd_briefs(a) -> int:
    """Session briefs: show, choose a direction, run one round now, show the latest from Grok, or forget."""
    from . import tasks
    svc = _svc()
    pid = _one(svc, a.pair)
    if tasks.asked_turn():  # neither read nor changed from a request from another computer
        # (the chat turn of a request from another computer never changes a twin's setup)
        raise EngineError("refused", "A request from another computer cannot read or change session briefs.",
                          "Change it yourself, on the Twins page or in your own terminal.")
    if a.forget and not a.yes:
        t = svc.briefs_info(pid)["twins"][0]
        n = t["count"]["out"] + t["count"]["in"]
        if n:
            _out(f"This deletes the {plural(n, 'brief')} kept for {t['name']} and turns their briefs off. "
                 "It cannot be undone. Run again with --yes.")
            return 1
    if a.way:
        svc.briefs_set(pid, _BRIEF_WORDS[a.way])
    if a.now:
        res = svc.briefs_now(pid)
        if not res["results"]:
            _out("Briefs are off for this twin, so nothing was made.")
        for r in res["results"]:
            _out(" ".join(_brief_round(r)))
    if a.forget:
        out = svc.briefs_forget(pid)["forgot"]
        _out(f"Deleted {plural(out['deleted'], 'brief')} kept for this twin. Its briefs are off.")
        if out.get("said"):
            _out(out["said"])
    info = svc.briefs_info(pid)
    if a.json:
        _out(json.dumps(info, indent=1, ensure_ascii=False))
        return 0
    t = info["twins"][0]
    wait = " (waiting for the Mirror to carry them)" if t["transport"] != "ok" and t["direction"] != "off" else ""
    _out(f"Session briefs for {t['name']}: {_BRIEF_SAID[t['direction']]}{wait}")
    _out(f"  kept here: {t['count']['out']} made for Grok, {t['count']['in']} from Grok")
    if t["direction"] == "off":
        _out("  " + info["sentence"])
        if t.get("can_turn_on", True):
            _out("  Turn on: hermes twin briefs TWIN --way to-grok | from-grok | both")
        else:
            _out("  Briefs can be turned on once the Mirror is set up.")
    if a.show:
        from . import briefs as _briefs
        for b in _briefs.stored(pid, "in")[:3]:
            _out("")
            _out(_briefs.render(b).rstrip())
    return 0


def cmd_doctor(a) -> int:
    doc = _svc().doctor()
    checks = doc["checks"]
    linked = doc.get("linked_to")
    if linked and any(c.get("optional") for c in checks):
        _out(f" (this computer gets its twins from {linked}: lines marked – matter only on the computer with the "
             "Grok app; nothing to do for them here)")
    elif doc.get("grok_app_here") is False and any(c.get("optional") for c in checks):
        _out(" (the Grok app is not on this computer. To have your twins here, link it to the computer with your "
             "Grok bots: hermes twin link CODE. Lines marked – matter only on that computer)")
    elif any(c.get("optional") for c in checks):
        _out(" (no real twins yet: lines marked – only matter for your real Grok bots; the sample works "
             "without them)")
    for c in checks:
        mark = "✓" if c["ok"] else ("–" if c.get("optional") else "✗")
        fix = str(c.get("fix") or "") if not c["ok"] else ""
        # "Not needed here: ..." is a reason, not something to do: never under the word "fix"
        tail = (f"\n     ({fix[0].lower()}{fix[1:]})" if fix.startswith(NOT_NEEDED) else f"\n     fix: {fix}") if fix else ""
        _out(f" {mark} {c['message']}{tail}")
    hard = [c for c in checks if not c["ok"] and c["id"] in ("data_dir",)]
    return 1 if hard else 0


def cmd_demo(a) -> int:
    action = a.action or "start"
    svc = _svc()
    res = svc.demo_action(action, a.profile)
    _out(res["did"].rstrip(".") + ".")
    if action == "start":
        for p in res["pairs"]:
            _out(_pair_line(p))
        _out(f"{SAMPLE_LINE} Next: open {PAGE}, or try hermes twin demo grok-edit and then hermes twin sync. "
             "End it with hermes twin demo remove.")
    elif action != "remove":
        _out(demo_next_step(svc))
    return 0


def demo_next_step(svc) -> str:
    from . import config
    try:
        every = int(config.get("sync_interval_seconds"))
    except (TypeError, ValueError):
        every = 60
    return (f"The twin notices at its next check (every {max(config.MIN_INTERVAL, every)} s), "
            "or now with `hermes twin check`.")


HANDLERS: dict[str, Callable[[Any], int]] = {
    "status": cmd_status, "sync": cmd_sync, "list-grok": cmd_list_grok, "add": cmd_add,
    "conflicts": cmd_conflicts, "resolve": cmd_resolve, "versions": cmd_restore_points,
    "restore": cmd_restore, "recent": cmd_recent, "compare": cmd_parity, "copies": cmd_copies, "checklist": cmd_checklist,
    "switch": cmd_switch, "send-to-grok": cmd_send, "doctor": cmd_doctor, "demo": cmd_demo,
    "approve": cmd_approve, "skill-sync": cmd_mirror, "remove": cmd_remove, "stop": cmd_remove, "digest": cmd_digest,
    "facts": cmd_facts, "briefs": cmd_briefs,
    "setup": cmd_setup, "verify": cmd_attest, "uninstall": cmd_uninstall, "skip": cmd_skip, "import": cmd_import,
    "finish": cmd_finish, "import-exports": cmd_import_exports, "syncs": cmd_syncs,
    "background": lambda a: bgagent.handle_cli(a, _out),
    # plain-word aliases
    "check": cmd_sync, "move": cmd_switch,
    # the older names of versions, compare, skill-sync and verify: they still work
    "restore-points": cmd_restore_points, "parity": cmd_parity, "mirror": cmd_mirror, "attest": cmd_attest,
}


from . import fleet_cli as _fleet_cli  # noqa: E402  (share, link, unlink, fleet)
from .fleet_server import one_shot as _one_shot  # noqa: E402  (a short command never holds the share port)

HANDLERS.update(_fleet_cli.HANDLERS)

from . import transcripts_cli as _transcripts_cli  # noqa: E402  (transcripts)

HANDLERS.update(_transcripts_cli.HANDLERS)

from . import tasks_api as _tasks_cli  # noqa: E402  (ask, requests)

HANDLERS.update(_tasks_cli.HANDLERS)

from . import routines as _routines_cli  # noqa: E402  (routines)

HANDLERS.update(_routines_cli.HANDLERS)

from . import instr_sync_cli as _instr_cli  # noqa: E402  (instructions)

HANDLERS.update(_instr_cli.HANDLERS)

from . import h2g_service as _h2g_cli  # noqa: E402  (to-grok)

HANDLERS.update(_h2g_cli.HANDLERS)


def _tag(value: str) -> str:
    from .engine import valid_tag
    if not valid_tag(value):
        raise argparse.ArgumentTypeError("release tags look like v0.2.0")
    return value


def _pid(value: str) -> str:
    if not valid_id(value):
        raise argparse.ArgumentTypeError("short names look like acme-support")
    return value


def _twin(value: str) -> str:
    """TWIN on the command line: the bot's name ("Acme Support") or the twin's short name. Which twin it is, is
    settled when the command runs (`_resolve`)."""
    name = safe.clean_name(value, 100)
    if not name:
        raise argparse.ArgumentTypeError("give the twin's name, such as \"Acme Support\"")
    return name


def _twin_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("pair", nargs="?", type=_twin, metavar="TWIN",
                   help="The twin's name, in quotes (not needed with one twin)")


def setup(parser: argparse.ArgumentParser) -> None:
    # the grouped overview (Everyday / Fix things / Advanced) replaces argparse's flat command list
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.description = HELP_TEXT
    parser.usage = "%(prog)s [COMMAND] [options]    (plain `%(prog)s` shows your twins and the next step)"
    subs = parser.add_subparsers(dest="twin_command", metavar="COMMAND", help=argparse.SUPPRESS)
    p = subs.add_parser("status", help="Your twins and what to do next")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    p = subs.add_parser("check", aliases=["sync"], help="Check Grok now")  # (`sync`: the older name still works)
    _twin_arg(p)
    p.add_argument("--pair", dest="pair_opt", type=_twin, help=argparse.SUPPRESS)  # the older spelling still works
    subs.add_parser("list-grok", help="The bots in your Grok app")
    p = subs.add_parser("add", help="Give a Grok bot a twin (asks first; nothing changes in Grok)")
    p.add_argument("--all", action="store_true", help="Every Grok bot without a twin")
    p.add_argument("--bot", action="append", metavar="NAME", help="Bot name (repeatable)")
    # not --profile: hermes reads --profile/-p anywhere on the command line to pick its own home
    p.add_argument("--hermes-profile", dest="profile", type=_pid, metavar="NAME",
                   help="Name of the twin's Hermes profile (made when missing; default: from the bot's name)")
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask")
    p = subs.add_parser("conflicts", help="Show what changed in both places")
    _twin_arg(p)
    p = subs.add_parser("resolve", help="Keep one version: resolve [TWIN] ITEM --keep hermes|grok")
    p.add_argument("args", nargs="+", metavar="[TWIN] ITEM")
    p.add_argument("--keep", choices=("hermes", "grok"), required=True)
    p = subs.add_parser("versions", aliases=["restore-points"], help="Earlier versions kept for undo, numbered")
    _twin_arg(p)
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    p = subs.add_parser("restore", help="Put an earlier version back: restore [TWIN] NUMBER")
    p.add_argument("args", nargs="+", metavar="[TWIN] NUMBER")
    p = subs.add_parser("recent", help="Recent activity (when and what kind; never content)")
    _twin_arg(p)
    p.add_argument("--limit", type=int, default=10)
    p = subs.add_parser("compare", aliases=["parity"], help="Compare with Grok, item by item")
    _twin_arg(p)
    p = subs.add_parser("copies", help="What copies over and how: the whole table")
    p.add_argument("--json", action="store_true")
    p = subs.add_parser("checklist", help="What you do yourself, and what moving routines to Hermes takes")
    _twin_arg(p)
    p = subs.add_parser("import", help="Import the setup export a Grok bot wrote about itself: import [TWIN] FILE")
    p.add_argument("args", nargs="*", metavar="[TWIN] FILE")
    p.add_argument("--request", action="store_true", help="Print the request to paste into the bot's own Grok chat")
    p.add_argument("--bot", metavar="NAME", help="The bot in the file to take, when it holds several")
    p.add_argument("--show", action="store_true",
                   help="Print the instructions and the routines' prompts, whole, before the question")
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask")
    p = subs.add_parser("finish", help="Several new twins at once: set the model and print what to ask each bot "
                                       "in Grok for its instructions")
    p.add_argument("twins", nargs="*", type=_twin, metavar="TWIN", help="The twins' names, in quotes")
    p.add_argument("--all", action="store_true", help="Every twin that still needs its instructions or a model")
    p.add_argument("--model", metavar="MODEL",
                   help="The model for these twins (default: the one this Hermes uses); changeable later, twin by twin")
    p.add_argument("--endpoint", metavar="NAME",
                   help="A model server already set up in this Hermes (see the list `finish` prints) that serves MODEL")
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask")
    p = subs.add_parser("import-exports",
                        help="Import every setup export in a folder, each into the twin of its bot: import-exports DIR")
    p.add_argument("folder", metavar="DIR", help="The folder the bots' answer files are in, such as ~/Downloads")
    p.add_argument("--twin", action="append", type=_twin, metavar="NAME",
                   help="Only this twin (repeatable; default: every twin)")
    p.add_argument("--show", action="store_true",
                   help="Print the instructions and the routines' prompts, whole, before the question")
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask")
    p = subs.add_parser("switch", aliases=["move"],
                        help="Move routines to Hermes: turn the twin's copied routines on")
    _twin_arg(p)
    p.add_argument("--undo", action="store_true", help="Pause those routines in Hermes again")
    p.add_argument("--yes", "-y", action="store_true")
    p = subs.add_parser("send-to-grok", help="Send approved skill changes to Grok (needs skill sync)")
    _twin_arg(p)
    p = subs.add_parser("approve", help="Let one waiting change go ahead: approve [TWIN] ITEM")
    p.add_argument("args", nargs="+", metavar="[TWIN] ITEM")
    p = subs.add_parser("skip", help="Keep a changed skill in Hermes only: skip [TWIN] ITEM")
    p.add_argument("args", nargs="+", metavar="[TWIN] ITEM")
    p.add_argument("--undo", action="store_true", help="Offer it for Grok again")
    p = subs.add_parser("skill-sync", aliases=["mirror"],
                        help="Skill sync (optional): skills travel through a small sync bot in your Grok account")
    _twin_arg(p)
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask before turning it off")
    g = p.add_mutually_exclusive_group()  # the sample needs no folder; a real twin is refused without one
    g.add_argument("--folder", "--pair-folder", dest="pair_folder", metavar="DIR",
                   help="The shared folder: one the sync bot can reach (for example one that Google Drive keeps on "
                        "both sides), outside Hermes. Later twins need no folder: they use the same one")
    g.add_argument("--off", action="store_true", help="Turn skill sync off (the pass is replaced too)")
    p.add_argument("--again", action="store_true",
                   help="Make the set-up folder again and show the steps (to send it to the sync bot once more)")
    g.add_argument("--status", action="store_true",
                   help="When the sync bot last ran, and whether it runs the same botport as this computer")
    p.add_argument("--take", action="append", metavar="SKILL",
                   help="Bring a skill made in Grok (shared library) back to this twin; repeat or use commas")
    p.add_argument("--untake", action="append", metavar="SKILL", help="Stop bringing that skill back")
    g.add_argument("--take-all", action="store_true",
                   help="Bring every skill in Grok's shared library back to this twin (every bot's)")
    g.add_argument("--take-none", action="store_true", help="Bring no skill made in Grok back to this twin")
    g.add_argument("--new-pass", "--rotate-key", dest="rotate_key", action="store_true",
                   help="Replace the twin's pass, the small file the sync bot needs (also: --rotate-key)")
    p = subs.add_parser("stop", aliases=["remove"], help="Stop twinning a bot (its Hermes bot and chats stay)")
    _twin_arg(p)
    p.add_argument("--dry-run", action="store_true", help="Only show what would happen")
    p.add_argument("--yes", "-y", action="store_true")
    p = subs.add_parser("uninstall", help="Remove Grok Bot Twin: every twin and its records")
    p.add_argument("--dry-run", action="store_true", help="Only show what would happen")
    p.add_argument("--yes", "-y", action="store_true")
    p = subs.add_parser("syncs", help="What syncs with Grok and which way: syncs [TWIN] [ITEM DIRECTION]")
    p.add_argument("words", nargs="*", metavar="[TWIN] [ITEM DIRECTION]",
                   help="The twin's name in quotes (not needed with one twin); to change a row, its item (name, "
                        "description, instructions, skills, routines, memory, briefs, conversations) and a direction "
                        "(from-grok, to-grok, both, off)")
    p.add_argument("--json", action="store_true", help="The panel as JSON")
    p = subs.add_parser("digest", help="Activity digest to Grok: preview, or turn it on/off for one twin")
    _twin_arg(p)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")
    p.add_argument("--titles", action="store_true", help="Also send short titles (separate opt-in)")
    p = subs.add_parser("facts", help="Memory facts shared with Grok (off until switched on)")
    p.add_argument("words", nargs="*", help="[TWIN] [sync | keep CONFLICT hermes|grok | restore POINT]")
    p.add_argument("--json", action="store_true")
    p = subs.add_parser("briefs", help="Session briefs to and from Grok (off until you turn them on)")
    _twin_arg(p)
    p.add_argument("--way", choices=["from-grok", "to-grok", "both", "off"], help="Which way briefs go")
    p.add_argument("--now", action="store_true", help="Make and take briefs now")
    p.add_argument("--show", action="store_true", help="Show the latest briefs from Grok")
    p.add_argument("--forget", action="store_true", help="Delete every brief kept for this twin")
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask before --forget deletes")
    p.add_argument("--json", action="store_true")
    subs.add_parser("doctor", help="Check the setup and say what to fix")
    _fleet_cli.setup(subs)
    _tasks_cli.setup(subs)
    _transcripts_cli.setup(subs)
    _routines_cli.setup(subs)
    _instr_cli.setup(subs)
    _h2g_cli.setup(subs)
    p = subs.add_parser("setup", help="Set up this computer or finish what is left (--check, --plan; safe to run again)")
    p.add_argument("--file", "--helper", dest="helper", metavar="FILE",
                   help="The downloaded botport file or archive; Hermes keeps its own copy of it (also: --helper)")
    p.add_argument("--allow-unattested", action="store_true",
                   help="DANGER: accept botport WITHOUT verifying it (a build of your own only); recorded and "
                        "shown by doctor and status")
    p.add_argument("--with-github", action="store_true",
                   help="Verify against the publisher's signed build record on GitHub instead (needs GitHub's "
                        "command-line tool); prints the download command and the exact check that runs")
    p.add_argument("--tag", type=_tag, metavar="vX.Y.Z",
                   help="The botport release to verify against (default: the plugin's setting, else the release "
                        "this plugin version expects)")
    # the one setup command for every way in (onboard.py): where this computer stands, the plan, the run
    p.description = ("Sets up Grok Bot Twin on this computer, from wherever it stands, and can be "
                    "run again at any time: a step that is done is skipped. --check says where "
                    "this computer stands, --plan says exactly what a run with the same options "
                    "would do; neither changes anything. A run verifies botport, and asks once "
                    "(or takes --yes) before it uses a download it found, twins a bot, shares or "
                    "links. It never prints the name of a bot you did not type.")
    p.add_argument("--check", action="store_true",
                   help="Say where this computer stands and what is left; changes nothing (exit status 0 when "
                        "nothing is left)")
    p.add_argument("--plan", action="store_true",
                   help="Say exactly what a run with the same options would do; changes nothing")
    p.add_argument("--yes", "-y", action="store_true", help="Go ahead without being asked")
    p.add_argument("--json", action="store_true", help="With --check or --plan: machine-readable output")
    p.add_argument("--bot", action="append", metavar="NAME",
                   help="Also give this Grok bot a twin (repeatable; nothing changes in Grok)")
    p.add_argument("--all", action="store_true", help="Also give every Grok bot without a twin one")
    p.add_argument("--share", action="store_true",
                   help="Also share your twins with your other computers; prints a link code")
    p.add_argument("--link", metavar="CODE",
                   help="On a computer without the Grok app: link it with the code the sharing computer shows")
    p = subs.add_parser("verify", aliases=["attest"],
                        help="Check botport against the publisher's signed build record on GitHub (needs GitHub's "
                             "command-line tool)")
    p.add_argument("--tag", type=_tag, metavar="vX.Y.Z",
                   help="The release to check against (default: the one recorded at setup)")
    p = subs.add_parser("demo", help="A sample twin with invented data (no Grok account needed)")
    p.add_argument("action", nargs="?", choices=("start", "remove", "grok-edit", "hermes-edit", "conflict", "secret"))
    p.add_argument("--hermes-profile", dest="profile", type=_pid, metavar="NAME",
                   help="Show the sample in this Hermes profile's chats (default: in none)")
    bgagent.add_cli(subs)
    parser.set_defaults(func=handle)


def _admin_install_note(sub, args) -> None:
    """Windows: after the first look or a link, say so when the plugin was installed from an administrator window
    (Hermes Desktop could then never show the Twins page). Never in --json output; never fails a command."""
    if not _osflags.WINDOWS or sub not in (None, "status", "link", "fleet") or getattr(args, "json", False):
        return
    if sub == "fleet" and getattr(args, "what", None) == "run":
        return
    try:
        from . import bgagent_windows
        found = bgagent_windows.admin_install_problem(note=True)
        if found:
            _err(f" ! {found[0]}\n   {found[1]}")
    except Exception:
        pass


def handle(args) -> int:
    sub = getattr(args, "twin_command", None)
    try:
        return _handle(args, sub)
    finally:
        _admin_install_note(sub, args)


def _handle(args, sub) -> int:
    try:
        if not sub:  # plain `hermes twin`: the status plus one next step
            with _one_shot():
                return cmd_status(argparse.Namespace(json=False)) or 0
        stays = (sub == "fleet" and getattr(args, "what", None) == "run") or \
                (sub == "background" and getattr(args, "action", None) == "run")
        if stays:  # the commands that keep running may serve the share port; every other one ends in a moment
            return HANDLERS[sub](args) or 0
        with _one_shot():
            return HANDLERS[sub](args) or 0
    except _NoTwin as exc:
        _out(str(exc))
        return 0
    except EngineError as exc:
        _err(f"✗ {exc.message}" + (f"\n  {exc.hint}" if exc.hint else ""))
        return 2 if exc.code in ("bad_input", "usage") else 1
    except KeyboardInterrupt:
        return 130


def build_parser() -> argparse.ArgumentParser:
    """Standalone parser (tests and `python -m`)."""
    parser = argparse.ArgumentParser(prog="hermes twin")
    setup(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    return handle(build_parser().parse_args(argv))
