"""`hermes twin share | link | unlink | fleet`: your twins on your other computers, from the terminal.

The same service calls as the Twins page. `--json` prints exactly what the page's
route answers. `fleet run` is the foreground loop a background unit runs on a machine where Hermes is not open
all day: it serves (the computer with the Grok app) or follows (every other computer) until it is stopped.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from typing import Any

from . import fleet, fleet_follow as follow, fleet_server as server, fleet_skills as skills
from . import osflags as _osflags
from . import xs_config
from .engine import EngineError
from .model import ago, plural

HELP_LINES = """\
Your other computers
  share on|off|code      share your twins with your other computers; shows the link code
  link CODE              on another computer: link it to the one that shares (paste the code)
  unlink                 stop following; this computer's twins leave (kept 30 days)
  fleet                  which computers hold your twins, and how up to date each is
  fleet skills [on|off]  each twin's skills on your linked computers too (off until you switch it on)
  fleet settings         which settings follow your twins (tools, preferences, persona, model);
                         `fleet settings ITEM on|off`, `fleet settings restore TWIN` puts back
"""


def _svc():
    from .service import get_service
    return get_service()


def _out(text: str = "") -> None:
    print(text, flush=True)


def _json(obj: Any) -> int:
    _out(json.dumps(obj, ensure_ascii=False, indent=1))
    return 0


def _confirm(args: Any, question: str) -> bool:
    if getattr(args, "yes", False):
        return True
    if not _osflags.is_terminal(sys.stdin):
        _out("Add --yes to go ahead without being asked.")
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _leaving(out: dict) -> None:
    _out(out.get("sentence") or "")
    for title, key in (("Stays", "stays"), ("Removed", "goes")):
        lines = [x for x in out.get(key) or [] if isinstance(x, str)]
        if lines:
            _out(f"{title}:")
            for line in lines:
                _out(f"  - {line}")
    if out.get("undo"):
        _out(out["undo"])


MACHINE_WORDS = {"in_sync": "in sync", "behind": "catching up", "offline": "not seen lately",
                 "never_synced": "linked, getting its first twins", "unknown": ""}
# With sharing off nothing reaches a linked computer. The page says when it was last seen ("last seen 2 hr ago"),
# so nothing is promised about a computer that may have left meanwhile; one never seen has only the plain line.
WAITING_FOR_SHARING = "waiting for sharing to be switched on"


def _while_off(m: dict) -> str:
    return f"last seen {ago(m['last_seen_at'])}" if m.get("last_seen_at") else WAITING_FOR_SHARING
KIND_WORDS = {"macos": "Mac", "linux": "Linux", "windows": "Windows"}
SOURCE_WORDS = {"syncing": "linking", "in_sync": "in sync", "offline": "not answering", "sharing_off": "sharing is off there",
                "error": "cannot work with this version"}


def _source_words(src: dict) -> str:
    code = (src.get("problem") or {}).get("code") if src["state"] == "error" else None
    if code == "source_forgot":
        return "no longer answers as linked"
    if code == "source_removed":
        return "Grok Bot Twin was removed there"
    return SOURCE_WORDS.get(src["state"], src["state"])


def _on_its_way(f: dict, t: dict) -> str:
    """"; goes to Studio when it is next on" for the computers a twin has not reached yet (the page says the same)."""
    waiting = [m for m in t["machines"] if m.get("state") == "pending"]
    if not waiting:
        return ""
    names = [m["name"] for m in waiting]
    to = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    if not (f.get("share") or {}).get("on"):
        return f"; goes to {to} when sharing is on again"
    rows = {m["id"]: m for m in f["machines"]}
    if all((rows.get(m["id"]) or {}).get("state") in ("in_sync", "behind", "never_synced") for m in waiting):
        return f"; on its way to {to}"
    return f"; goes to {to} when {'it is' if len(names) == 1 else 'each is'} next on"


def describe(f: dict) -> list:
    """The fleet in words (the Twins page shows the same facts)."""
    me = f["this_machine"]
    lines = []
    if f["role"] == "none":
        lines.append(f"{me['name']}: not sharing twins and not linked to another computer.")
        lu = f.get("last_unlink")
        if lu:
            who = "here" if lu.get("by") == "this" else f"by {lu.get('source_name')}"
            lines.append(f"It was unlinked from {lu.get('source_name')} {who}, {ago(lu.get('at'))}. "
                         "Link it again to bring its twins back.")
        lines.append("Share this computer's twins:  hermes twin share on" if f["can_share"]
                     else "Link this computer to the one with the Grok app:  hermes twin link CODE")
    elif f["role"] == "source":
        sh = f["share"]
        if sh["state"] == "on":
            # (where exactly it listens is in --json; the plain line says only over what)
            via = {"tailscale": " over Tailscale", "lan": " on your home network", "loopback": " on this computer only"}
            lines.append(f"{me['name']} shares its twins{via.get(sh['network'], '')}.")
        elif sh["state"] == "error":
            lines.append(f"{me['name']} should share its twins, but: {sh['problem']['message']}")
            if sh["problem"].get("hint"):
                lines.append(f"  {sh['problem']['hint']}")
        elif sh["on"]:
            lines.append(f"{me['name']} shares its twins (starting).")
        else:
            lines.append(f"Sharing is switched off on {me['name']}; linked computers stay linked.")
        if sh.get("code"):
            lines.append(f"Link code: {sh['code']['code']}   (one computer, {max(1, sh['code']['expires_in_s'] // 60)} "
                         "more minutes). On the other computer:  hermes twin link CODE")
        others = [m for m in f["machines"] if not m["this"]]
        # (no "yet": the line is also what shows right after a computer was unlinked)
        lines.append("Computers:" if others else "No other computer is linked.")
        for m in others:
            when = f", {ago(m['last_sync_at'])}" if m["state"] == "in_sync" and m.get("last_sync_at") else \
                (f", last seen {ago(m['last_seen_at'])}" if m["state"] == "offline" and m.get("last_seen_at") else "")
            bad = f"; {m['failed']} could not be made there" if m.get("failed") else ""
            kind = f", {KIND_WORDS[m['platform']]}" if m.get("platform") in KIND_WORDS else ""
            if not sh["on"]:  # nothing reaches it while sharing is off: it is not "in sync" with anything
                lines.append(f"  {m['name']}{kind}: {_while_off(m)}")
                continue
            lines.append(f"  {m['name']}{kind}: {MACHINE_WORDS.get(m['state'], m['state'])}{when}{bad}")
    else:
        src = f["source"]
        lines.append(f"{me['name']} follows {src['name']}: {_source_words(src)}"
                     + (f", {ago(src['last_sync_at'])}" if src["state"] == "in_sync" and src.get("last_sync_at") else "")
                     + ".")
        if src.get("problem"):
            lines.append(f"  {src['problem']['message']} {src['problem'].get('hint', '')}".rstrip())
    if f["twins"]:
        lines.append("Twins:")
        for t in f["twins"]:
            # a computer the twin has not reached yet is not one it is "on": said apart, with when it gets there
            where = ", ".join(m["name"] for m in t["machines"] if m.get("state") != "pending") + _on_its_way(f, t)
            extra = (f"  [{t['note']}]" if t.get("note") else "") if t["state"] == "in_sync" else \
                (f"  [{t['error']['message']} {t['error'].get('hint', '')}]".rstrip() if t.get("error")
                 else "  [no longer listed there; kept as it is]")
            lines.append(f"  {t['name']}: on {where}{extra}")
    if f["removed"]:
        lines.append(f"Kept after a removal ({len(f['removed'])}): " + ", ".join(k["name"] for k in f["removed"]))
    return lines


def cmd_share(args: Any) -> int:
    svc = _svc()
    what = getattr(args, "what", None) or "status"
    if what == "code":
        out = svc.fleet_code()
        if args.json:
            return _json(out)
        _out(f"Link code: {out['code']['code']}   (one computer, 10 minutes)")
        _out("On the other computer:  hermes twin link CODE")
        return 0
    f = svc.fleet_share(what == "on") if what in ("on", "off") else svc.fleet()
    if args.json:
        return _json(f)
    for line in describe(f):
        _out(line)
    if what == "on" and not server.other_serving() and not getattr(args, "stay", False):
        _out("Sharing runs while Hermes is open on this computer (or while `hermes twin fleet run` runs).")
    return 0


def cmd_link(args: Any) -> int:
    f = _svc().fleet_link(args.code, getattr(args, "host", None), getattr(args, "port", None))
    if args.json:
        return _json(f)
    for line in describe(f):
        _out(line)
    from . import bgagent
    hint = bgagent.link_hint()
    if hint:
        _out(hint)
    return 0


def cmd_unlink(args: Any) -> int:
    svc = _svc()
    plan = svc.fleet_unlink(True)
    if getattr(args, "dry_run", False):
        return _json(plan) if args.json else (_leaving(plan) or 0)
    if not args.json:
        _leaving(plan)
    if not _confirm(args, "Unlink this computer?"):
        return 1
    out = svc.fleet_unlink(False)
    if args.json:
        return _json(out)
    _out("Unlinked.")
    return 0


def _in_sync(f: dict) -> bool:
    if f["role"] == "follower":
        return f["source"]["state"] == "in_sync" and all(t["state"] == "in_sync" for t in f["twins"])
    return f["role"] == "source" and f["share"]["state"] in ("on", "off")


def cmd_fleet(args: Any) -> int:
    svc = _svc()
    what = getattr(args, "what", None)
    if what == "run":
        return run(args)
    if what == "skills":
        return skills.cli(svc, args, _out, _json)
    if what == "settings":  # which settings follow a twin to the other computers (xs_config.py)
        return xs_config.cli(args, svc, _out, _json)
    if getattr(args, "more", None) and what != "memory":
        # (more words belong to `fleet settings`, `fleet skills` and `fleet memory` only: a name of two words is quoted)
        raise EngineError("usage", 'Put a name of several words in quotes: hermes twin fleet name "My Surface"')
    if what == "once":
        try:
            f = svc.fleet_sync()
        except EngineError as exc:
            if exc.code != "not_linked":
                raise
            f = svc.fleet()
        if args.json:
            _json(f)
        else:
            for line in describe(f):
                _out(line)
        return 0 if _in_sync(f) else 1
    if what == "memory":  # a twin's memory between the owner's computers (xs_memory.py)
        from . import xs_memory
        words = ([args.value] if args.value else []) + list(getattr(args, "more", None) or [])
        return xs_memory.cli(words, bool(args.json), _out, _svc)
    if what == "remove":
        if not args.value:
            raise EngineError("usage", "Say which computer: hermes twin fleet remove NAME")
        plan = svc.fleet_remove_machine(args.value, True)
        if not args.json:
            _leaving(plan)
        if not _confirm(args, f"Unlink {plan['machine']['name']}?"):
            return 1
        out = svc.fleet_remove_machine(plan["machine"]["id"], False)
        return _json(out) if args.json else (_out("Done.") or 0)
    if what == "routines":  # the item's switch, here too: hermes twin fleet routines on|off
        if args.value not in ("on", "off"):
            raise EngineError("usage", "Say on or off: hermes twin fleet routines on")
        v = svc.routines_switch(on=args.value == "on")
        if args.json:
            return _json(v)
        f = svc.fleet()
    elif what == "name":
        if not args.value:
            raise EngineError("usage", "Say the name: hermes twin fleet name NAME")
        f = svc.fleet_name(args.value)
    else:
        f = svc.fleet()
    if args.json:
        return _json(f)
    for line in describe(f):
        _out(line)
    if what in (None, "show"):
        from . import xs_memory
        line = xs_memory.summary_line()  # one line on memory, only on a computer that shares or is linked
        if line:
            _out(line)
        for line in skills.fleet_lines():  # what else goes between the computers, and how to switch it
            _out(line)
    if f["role"] != "none":
        _out(xs_config.summary_line(xs_config.view(svc)))
    if f["role"] != "none":  # what copies between the computers, item by item (twin/routines.py)
        from . import routines as _routines
        for line in _routines.lines(svc.routines()):
            _out(line)
    return 0


# The state files whose change another process on this computer is noticed by.
# (tasks.json: the requests switch, twin/tasks.py; fleet-skills.json: the skills switches, twin/fleet_skills.py;
# routines.json: the routines switches, twin/routines.py)
_WATCHED = (fleet.STATE_FILE, "pairs.json", "tasks.json", skills.STATE_FILE, "routines.json")
_ROUTINES_AT = _WATCHED.index("routines.json")  # (found by name: the list grows with every item that syncs)


def _mtimes() -> tuple:
    out = []
    for name in _WATCHED:
        try:
            st = os.stat(fleet.state_path().parent / name)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _headline(f: dict) -> str:
    if f["role"] == "source":
        return (f"sharing {plural(len(f['twins']), 'twin')} with {plural(len(f['machines']) - 1, 'computer')}: "
                f"{f['share']['state']}"
                + "".join(f"; {m['name']} "
                          + (MACHINE_WORDS.get(m['state'], m['state']) if f["share"]["on"] else _while_off(m))
                          for m in f["machines"] if not m["this"]))
    if f["role"] == "follower":
        bad = len([t for t in f["twins"] if t["state"] == "error"])
        return (f"following {f['source']['name']}: {_source_words(f['source'])}, "
                f"{plural(len(f['twins']), 'twin')}" + (f", {bad} could not be made" if bad else ""))
    return "waiting: this computer is not linked and does not share"


RESTART_STATUS = 75  # "try again": the background helper's unit, agent or task starts the loop again after it
MSG_UPDATED_HELPER = "Grok Bot Twin was updated on this computer: starting again with the new version"
MSG_UPDATED_TERMINAL = ("Grok Bot Twin was updated on this computer: this loop still runs the version from before. "
                        "Stop it (Ctrl-C) and start it again to run the new one.")


def started_by_helper(args: Any = None) -> bool:
    """This loop was started by the background helper (a systemd user unit, a launchd agent, the Windows task),
    which starts it again after it ends with an error status. A loop typed into a terminal is not."""
    if getattr(args, "twin_command", None) == "background" and getattr(args, "action", None) == "run":
        return True  # Windows: hermes twin background run
    if str(os.environ.get("XPC_SERVICE_NAME") or "").startswith("com.grok-bot-twin.follower"):
        return True  # macOS: launchd names the agent it started
    try:
        with open("/proc/self/cgroup", encoding="utf-8", errors="replace") as fh:
            return "grok-bot-twin-follower" in fh.read(4000)  # Linux: the unit's own control group
    except OSError:
        return False


def run(args: Any) -> int:
    """Serve or follow in the foreground until stopped. Sleeps between looks; a look is two file dates, and the
    state is read only when one of them changed (or once a minute).

    The plugin updated under this process: the loop would keep running the version it started with (the helper
    would follow with old code for as long as the owner stays logged in). Under the
    background helper the loop therefore ends with RESTART_STATUS and the helper starts it again with the new
    version; in a terminal it says so once and carries on."""
    svc = _svc()
    stop = threading.Event()
    from . import fleet_profiles as _fp
    update, updated, told = _fp.UpdateWatch(), False, False
    by_helper = started_by_helper(args)

    def _stop(*_a: Any) -> None:
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass
    follow.ensure_started(_svc, watch_idle=True)
    last_line, seen, last_full, wanted, name = "", None, 0.0, False, None
    try:
        while not stop.is_set():
            now = time.monotonic()
            cur = _mtimes()
            changed = cur != seen
            # a full look when a state file changed, once a minute as a safety net, and every time while sharing
            # is switched on but no process on this machine listens (the one that did may just have ended)
            if changed or now - last_full >= 60 or (wanted and server.serving() is None):
                last_full = now
                if update.changed():
                    if by_helper:
                        updated = True
                        break
                    if not told:
                        told = True
                        _out(f"{time.strftime('%H:%M:%S')} {MSG_UPDATED_TERMINAL}")
                elif update.pending is not None:
                    last_full = now - 50  # new files seen once: look again in about 10 s, not in a minute
                svc.fleet_tick()
                if changed:
                    follow.kick()  # another process linked or unlinked this machine, or changed its name
                    if seen is not None and cur[_ROUTINES_AT] != seen[_ROUTINES_AT]:
                        follow.ask_again()  # a routines switch or wish from another process: reported now
                view = svc.fleet()
                if name is not None and view["this_machine"]["name"] != name:
                    follow.ask_again()  # renamed from another process: the source hears it now, not in a minute
                name = view["this_machine"]["name"]
                wanted = view["share"]["on"] and view["share"]["state"] != "on"
                line = _headline(view)
                if line != last_line:
                    last_line = line
                    _out(f"{time.strftime('%H:%M:%S')} {line}")
                seen = _mtimes()
            elif wanted is False:
                skills.source_tick()  # a changed skill goes out within seconds (names and dates only are read)
            stop.wait(5.0)
    except KeyboardInterrupt:
        pass
    finally:
        from . import tasks_run  # (imported here: the requests worker is not this module's business otherwise)
        tasks_run.stop()
        follow.stop()
        server.stop()
    if updated:
        _out(f"{time.strftime('%H:%M:%S')} {MSG_UPDATED_HELPER}")
        return RESTART_STATUS
    _out(f"{time.strftime('%H:%M:%S')} stopped")
    return 0


def setup(subs: Any) -> None:
    p = subs.add_parser("share", help="Share your twins with your other computers")
    p.add_argument("what", nargs="?", choices=("on", "off", "code", "status"), default="status")
    p.add_argument("--json", action="store_true")
    p = subs.add_parser("link", help="Link this computer to the one that shares its twins")
    p.add_argument("code", metavar="CODE", help="The link code shown there, like ABCD-EFGH-JKMN")
    p.add_argument("--host", help="Where to look, if the code's own address cannot be reached from here")
    p.add_argument("--port", type=int)
    p.add_argument("--json", action="store_true")
    p = subs.add_parser("unlink", help="Stop following; this computer's twins leave (kept 30 days)")
    p.add_argument("--dry-run", action="store_true", help="Only show what would happen")
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument("--json", action="store_true")
    p = subs.add_parser("fleet", help="Which computers hold your twins")
    p.add_argument("what", nargs="?",
                   choices=("show", "once", "run", "remove", "name", "memory", "skills", "settings", "routines"),
                   default="show")
    p.add_argument("value", nargs="?", metavar="NAME")
    # (fleet memory TWIN on|off|forget|restore|keep ...; fleet skills set SKILL on|off, keep SKILL mine|theirs ...;
    # fleet settings ITEM on|off, fleet settings restore TWIN)
    p.add_argument("more", nargs="*", help=argparse.SUPPRESS)
    p.add_argument("--twin", help="With `fleet skills`: which twin, when two have a skill of that name")
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument("--json", action="store_true")


HANDLERS = {"share": cmd_share, "link": cmd_link, "unlink": cmd_unlink, "fleet": cmd_fleet}
