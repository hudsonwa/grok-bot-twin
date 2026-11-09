"""`hermes twin ...` command line. Handlers return an int exit code (0 ok, 1 attention/failure, 2 usage)."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from . import safe
from . import secrets as _secrets
from .engine import EngineError
from .model import STATE_LABELS, ago, plural, valid_id, valid_item_id

DOT = {"in_step": "●", "changes_waiting": "◐", "needs_you": "!", "grok_missing": "?", "error": "×"}


def _svc():
    from .service import get_service
    return get_service()


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _confirm(question: str) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        _err("Not a terminal: pass --yes to go ahead.")
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


class _NoTwin(Exception):
    pass


def _one(svc, pair) -> str:
    """The twin a command is about: the one given, else the only twin there is."""
    if pair:
        return pair
    ids = svc.pair_ids()
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise _NoTwin("No twins yet. Try the demo: hermes twin demo")
    raise EngineError("usage", f"You have {len(ids)} twins; say which one: " + ", ".join(ids[:20]))


def _two(a, second: str) -> tuple:
    """`CMD [PAIR] X`: one argument is X (for the only twin), two are PAIR X."""
    vals = list(getattr(a, "args", None) or [])
    if len(vals) == 1:
        return None, vals[0]
    if len(vals) == 2:
        if not valid_id(vals[0]):
            raise EngineError("usage", "Twin ids look like acme-helper.")
        return vals[0], vals[1]
    raise EngineError("usage", f"Give the {second} (and the twin id first when you have more than one twin).")


def _pair_line(p: dict) -> str:
    demo = " (demo)" if p.get("demo") else ""
    reason = p.get("state_reason") or ""
    label = STATE_LABELS.get(p.get("state"), "?")
    tail = f" — {reason}" if reason and reason != label else ""
    return (f" {DOT.get(p.get('state'), '·')} {p['pair_id']}{demo}: {p['grok']['name']} ⇄ profile "
            f"{p['hermes']['profile']} · {label}{tail} · synced {ago(p.get('last_sync_at'))}")


# ---- handlers ----------------------------------------------------------------------------------------
def cmd_status(a) -> int:
    st = _svc().status(fresh=True)
    if a.json:
        _out(json.dumps(st, indent=1, ensure_ascii=False))
        return 0
    eng = st["engine"]
    if eng.get("available"):
        bp = f"botport {eng.get('version') or ''}".strip() + (" (attested)" if eng.get("attested") else "")
        if eng.get("trust_warning"):
            bp += f" · {eng['trust_warning']}"
    else:
        bp = ("botport: not set up (only needed for real twins)" if st.get("mode") == "demo"
              else f"botport: {eng.get('problem') or 'not found'}")
    _out(f"Grok Bot Twin · mode {st['mode']} · {bp} · check every {st['interval_s']} s")
    if not st["pairs"]:
        _out("No twins yet. Try the demo: hermes twin demo" + (
            "   ·   add your Grok bots: hermes twin add" if eng.get("available") and not eng.get("problem")
            else "   ·   real Grok bots: hermes twin setup, then hermes twin add"))
        return 0
    for p in st["pairs"]:
        _out(_pair_line(p))
        _waiting_lines(p)
    return 1 if any(p["state"] in ("needs_you", "error") for p in st["pairs"]) else 0


def _waiting_lines(p: dict) -> None:
    """Changes waiting for the owner's OK (e.g. routines and connectors after create), each with its command."""
    for w in (p.get("waiting") or [])[:20]:
        item = safe.clean_name(w["item"], 140)
        _out(f"    waiting for your OK: {item}  ->  hermes twin approve {p['pair_id']} \"{item}\"")


def cmd_sync(a) -> int:
    res = _svc().sync(a.pair)
    if not res["pairs"]:
        _out("Nothing to sync.")
        return 0
    rc = 0
    for r in res["pairs"]:
        if r.get("error"):  # this twin failed; the others were still synced
            _out(f" {r['pair_id']}: could not sync: {r['error']['message']}")
            rc = 1
            continue
        bits = []
        if r["applied"]:
            bits.append("updated " + ", ".join(sorted({x["kind"] for x in r["applied"]})))
        if r["secrets_withheld"]:
            n = len({s["item"] for s in r["secrets_withheld"]})
            bits.append(f"{_secrets.held_back_text(n)} ({_secrets.NOTE})")
            rc = 1
        if r.get("waiting"):
            bits.append(f"{plural(len(r['waiting']), 'change')} waiting for approval: hermes twin approve "
                        f"{r['pair_id']} ITEM")
            rc = 1
        if r["conflicts"]:
            bits.append(f"{plural(len(r['conflicts']), 'conflict')}: hermes twin conflicts {r['pair_id']}")
            rc = 1
        _out(f" {r['pair_id']}: " + ("; ".join(bits) if bits else "already in step")
             + (f" (old Hermes copy kept: restore point {r['restore_point']})" if r.get("restore_point") else ""))
    return rc


def cmd_list_grok(a) -> int:
    data = _svc().grok_list()
    if not data["app_found"] and not data["bots"]:
        _out("Grok app not found on this computer. Try the demo: hermes twin demo")
        return 1
    for b in data["bots"]:
        tw = f" ⇄ {b['twinned_pair_id']}" if b["twinned_pair_id"] else ""
        _out(f" {b['grok_id']}: {b['name']}{' (demo)' if b.get('demo') else ''}{tw}")
    if not data["bots"]:
        _out("No Grok bots found (check the `bots` filter in the plugin settings).")
    return 0


_ACTION_WORDS = {"by_hand": "by hand", "via_mirror": "via Mirror bot", "held_back": "held back"}
SKIP_REASON = {"already_twinned": "already a twin", "profile_already_paired": "that Hermes profile already has a twin"}


def cmd_add(a) -> int:
    svc = _svc()
    roster = svc.grok_list()["bots"]
    if a.all:
        chosen = [b for b in roster if not b["twinned_pair_id"]]
    else:
        names = {n.lower() for n in (a.bot or [])}
        if not names:
            _err("Say which bots: hermes twin add --bot \"Acme Helper\"  (or --all)")
            return 2
        chosen = [b for b in roster if b["name"].lower() in names or b["grok_id"] in (a.bot or [])]
        missing = names - {b["name"].lower() for b in chosen} - {b["grok_id"].lower() for b in chosen}
        if missing:
            _err("Not found in the Grok roster: " + ", ".join(sorted(missing)))
            return 1
    if not chosen:
        _out("Every Grok bot already has a twin.")
        return 0
    ids = [b["grok_id"] for b in chosen]
    profiles = {gid: a.profile for gid in ids} if a.profile else {}
    prev = svc.preview(ids, profiles)
    names = {b["grok_id"]: b["name"] for b in roster}
    for k in prev.get("skipped", []):
        _out(f" skipped {names.get(k['grok_id'], k['grok_id'])}: {SKIP_REASON.get(k['reason'], k['reason'])}")
    if not prev["pairs"]:
        _err("Nothing to create." + (" Pick a new profile name: --hermes-profile NAME" if any(
            k["reason"] == "profile_already_paired" for k in prev.get("skipped", [])) else ""))
        return 1
    ids = [p["grok"]["id"] for p in prev["pairs"]]
    # create exactly what the preview showed, including the profile picked for each bot
    profiles = {p["grok"]["id"]: p["hermes"]["profile"] for p in prev["pairs"] if p["hermes"]["profile"]}
    for p in prev["pairs"]:
        acts: dict[str, int] = {}
        for it in p["items"]:
            acts[it["action"]] = acts.get(it["action"], 0) + 1
        flagged = [it["name"] for it in p["items"] if it.get("scan") in ("caution", "dangerous")]
        _out(f" {p['grok']['name']} → profile {p['hermes']['profile'] or '(new)'}: "
             + ", ".join(f"{n} {_ACTION_WORDS.get(k, k)}" for k, n in sorted(acts.items()))
             + (f"; {_secrets.held_back_text(len(p['secrets_withheld']))}" if p["secrets_withheld"] else "")
             + (f"; {plural(p['schedules_paused'], 'schedule')} created paused" if p["schedules_paused"] else "")
             + (f"; scanner flagged: {', '.join(flagged)}" if flagged else ""))
    if not a.yes and not _confirm(f"Create {plural(len(ids), 'twin')}?"):
        _out("Nothing changed.")
        return 1
    res = svc.create(ids, profiles)
    for p in res["pairs"]:
        _out(_pair_line(p))
        _waiting_lines(p)
    for k in res.get("skipped", []):
        _out(f" skipped {names.get(k['grok_id'], k['grok_id'])}: {SKIP_REASON.get(k['reason'], k['reason'])}")
    for n in res.get("notes", []):
        _out(f" note: {n}")
    return 0


def cmd_conflicts(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    data = svc.conflicts(pid)
    if not data["conflicts"]:
        _out("No conflicts.")
        return 0
    _out(f"{plural(len(data['conflicts']), 'conflict')}:")
    for c in data["conflicts"]:
        _out(f"── {c['conflict_id']} (since {ago(c.get('since'))})")
        _out(c["diff"].rstrip() or "(no text diff)")
    target = f"{a.pair} " if a.pair else ""
    _out(f"Keep one side: hermes twin resolve {target}<conflict_id> --keep hermes|grok")
    _out("  Keep Grok replaces the Hermes copy (the old one is kept as a restore point).")
    _out("  Keep Hermes keeps it and puts it on the list for Grok (by hand, or through a Mirror bot).")
    return 1


def cmd_resolve(a) -> int:
    pair, cid = _two(a, "conflict id")
    if not valid_item_id(cid):
        raise EngineError("usage", "Conflict ids look like skill:greet-visitors.")
    svc = _svc()
    pid = _one(svc, pair)
    res = svc.resolve(pid, cid, a.keep)
    if a.keep == "grok":
        _out("Kept Grok: the Hermes copy was replaced."
             + (f" The old Hermes copy is restore point {res['restore_point']}" if res.get("restore_point") else ""))
    else:
        _out("Kept Hermes: Grok gets this copy by hand, or through your Mirror bot (see hermes twin checklist).")
    return 0


def cmd_restore_points(a) -> int:
    svc = _svc()
    pts = svc.restore_points(_one(svc, a.pair))["points"]
    if not pts:
        _out("No restore points yet.")
    for p in pts:
        _out(f" {p['id']} · {p.get('reason', '')} · {ago(p.get('at'))}")
    return 0


def cmd_restore(a) -> int:
    pair, point = _two(a, "restore point id")
    if not valid_item_id(point):
        raise EngineError("usage", "Restore point ids look like skill:greet-visitors@3f2a9c1b7e40.")
    svc = _svc()
    res = svc.restore(_one(svc, pair), point)
    if not res["restored"]:
        _out(res.get("note") or "Nothing to restore.")
        return 0
    _out(f"Restored {', '.join(res['restored'])}."
         + (f" Undo point: {res['restore_point']}" if res.get("restore_point") else ""))
    for item in res.get("waiting_for_ok", []):
        _out(f" {item} now waits for your OK before going to Grok: hermes twin approve {item}")
    return 0


def cmd_recent(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    for e in svc.recent(pid, a.limit)["entries"]:
        _out(f" {ago(e['at']):>12} · {e['side']:<6} · {e['kind']:<14} {e['summary']}")
    return 0


def cmd_parity(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    res = svc.parity(pid)
    if res.get("total") is None:
        _out(res.get("note", "No parity data."))
        return 0
    _out(f"Setup parity {res['passed']}/{res['total']} (no model call)")
    for c in res["checks"]:
        _out(f" {'✓' if c['pass'] else '✗'} {c['name']}" + ("" if c["pass"] else f" — {c.get('detail', '')}"))
    return 0 if res["passed"] == res["total"] else 1


def cmd_checklist(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    res = svc.checklist(pid)
    _out("Set up by hand:")
    for b in res["by_hand"]:
        _out(f" ☐ [{b.get('side')}] {b.get('name') or b.get('item')}: {b.get('what')}")
    sw = res["switch"]
    _out("Ready to switch:")
    for r in sw["grok_routines_to_turn_off"]:
        _out(f" ☐ Turn off in Grok: {r['name']}")
    for j in sw["hermes_jobs_paused"]:
        _out(f" ☐ Paused in Hermes: {j['name']} ({j['schedule']}) id {j['job_id']}")
    if not sw["hermes_jobs_paused"]:
        _out(" ✓ No paused Hermes jobs left.")
    return 0


def cmd_switch(a) -> int:
    svc = _svc()
    pid = _one(svc, a.pair)
    jobs = svc.checklist(pid)["switch"]["hermes_jobs_paused"]
    if not jobs:
        _out("No paused jobs to resume.")
        return 0
    for j in jobs:
        _out(f" will resume: {j['name']} ({j['schedule']})")
    _out("Turn the matching routines off in Grok first, so nothing runs twice.")
    if not a.yes and not _confirm(f"Resume {plural(len(jobs), 'job')}?"):
        _out("Nothing changed.")
        return 1
    res = svc.switch(pid, [j["job_id"] for j in jobs])
    _out(f"Resumed {len(res['resumed'])}." + (f" Failed: {len(res['failed'])}" if res["failed"] else ""))
    for f in res["failed"]:
        _out(f"  {f['job_id']}: {f['error']}")
    return 1 if res["failed"] else 0


def cmd_send(a) -> int:
    svc = _svc()
    res = svc.send_to_grok(_one(svc, a.pair))
    n = res["digest_entries"]
    _out(f"Sent {plural(res['skills'], 'skill')}" + (f" and {n} digest {'entry' if n == 1 else 'entries'}" if n
                                                     else "") + f". {res['note']}")
    return 0


def cmd_approve(a) -> int:
    pair, item = _two(a, "item")
    if not valid_item_id(item):
        raise EngineError("usage", "Item ids look like skill:greet-visitors.")
    svc = _svc()
    res = svc.approve(_one(svc, pair), item)
    for x in res["approved"]:
        _out(f" approved {x['item']} (to {x['to']})")
    s = res.get("sync")
    if s:
        _out(f" {s['pair_id']}: {STATE_LABELS.get(s['state'], s['state'])}")
    return 0


def cmd_mirror(a) -> int:
    svc = _svc()
    if getattr(a, "rotate_key", False):
        res = svc.mirror(_one(svc, a.pair), None, False, True)
        _out(res["note"])
        return 0
    res = svc.mirror(_one(svc, a.pair), a.pair_folder, bool(a.off))
    if not res["connected"]:
        _out("Mirror bot disconnected; skills to Grok are back on the by-hand list.")
        return 0
    _out("Connected. In the Grok app:")
    for i, st in enumerate(res["steps"], 1):
        _out(f" {i}. {st['what']}")
    return 0


def cmd_remove(a) -> int:
    svc = _svc()
    plan = svc.remove(a.pair, True)
    _out(f"Stops twinning {a.pair}; the Hermes profile stays. Removes:")
    for r in plan["removed"]:
        _out(f"  {r}")
    if a.dry_run:
        return 0
    if not a.yes and not _confirm("Remove this twin?"):
        _out("Nothing changed.")
        return 1
    res = svc.remove(a.pair, False)
    _out(f"Removed {len(res['removed'])} item(s). The profile was kept.")
    return 0


def cmd_setup(a) -> int:
    """Check and trust the botport binary (owner step): ownership/permissions, SHA-256, `gh attestation verify`
    against the botport release workflow (mandatory), then version + features. --allow-unattested is the loud,
    recorded opt-out for a local build."""
    svc = _svc()
    from .engine import UNATTESTED_TEXT, attestation_command
    bp = svc.botport()
    if bp is None:
        _err("botport was not found. Install it from its GitHub release, then run this again.")
        return 1
    if a.allow_unattested:
        _err("WARNING: --allow-unattested trusts botport WITHOUT checking its GitHub build attestation. Use it only "
             "for a botport you built yourself. This is recorded and shown by hermes twin doctor and status.")
    tag = svc.release_tag(getattr(a, "tag", None))
    if not a.allow_unattested:
        _out(f"Checking botport's GitHub build attestation (release {tag}):")
        _out("  " + " ".join(attestation_command(bp.path, tag)))
    res = svc.trust_botport(bool(a.allow_unattested), tag)
    _out(f"botport {res['version'] or ''} trusted (SHA-256 {str(res['sha256'] or '')[:12]}…)."
         + (f" Attestation verified (release {res['release_tag']})." if res["attested"] else f" {UNATTESTED_TEXT}."))
    return 0


def cmd_attest(a) -> int:
    res = _svc().attest_botport(a.tag)
    _out("Attestation verified." if res["verified"] else f"Attestation NOT verified: {res['problem']}")
    return 0 if res["verified"] else 1


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


def cmd_doctor(a) -> int:
    checks = _svc().doctor()["checks"]
    if any(c.get("optional") for c in checks):
        _out(" (no real twins yet: botport items marked – are only needed for real twins; the demo works without them)")
    for c in checks:
        mark = "✓" if c["ok"] else ("–" if c.get("optional") else "✗")
        _out(f" {mark} {c['message']}" + (f"\n     fix: {c['fix']}" if c.get("fix") and not c["ok"] else ""))
    hard = [c for c in checks if not c["ok"] and c["id"] in ("data_dir",)]
    return 1 if hard else 0


def cmd_demo(a) -> int:
    action = a.action or "start"
    svc = _svc()
    res = svc.demo_action(action, a.profile)
    _out(res["did"] + ".")
    if action in ("start", "remove"):
        for p in res["pairs"]:
            _out(_pair_line(p))
    if action == "start":
        _out("Next: open Twins in Hermes Desktop, or try `hermes twin demo grok-edit` then `hermes twin sync`.")
    else:
        _out(demo_next_step(svc))
    return 0


def demo_next_step(svc) -> str:
    from . import config
    try:
        every = int(config.get("sync_interval_seconds"))
    except (TypeError, ValueError):
        every = 60
    return f"Run `hermes twin sync` or wait for the background check (every {max(config.MIN_INTERVAL, every)} s)."


HANDLERS: dict[str, Callable[[Any], int]] = {
    "status": cmd_status, "sync": cmd_sync, "list-grok": cmd_list_grok, "add": cmd_add,
    "conflicts": cmd_conflicts, "resolve": cmd_resolve, "restore-points": cmd_restore_points,
    "restore": cmd_restore, "recent": cmd_recent, "parity": cmd_parity, "checklist": cmd_checklist,
    "switch": cmd_switch, "send-to-grok": cmd_send, "doctor": cmd_doctor, "demo": cmd_demo,
    "approve": cmd_approve, "mirror": cmd_mirror, "remove": cmd_remove, "digest": cmd_digest,
    "setup": cmd_setup, "attest": cmd_attest,
}


def _tag(value: str) -> str:
    from .engine import valid_tag
    if not valid_tag(value):
        raise argparse.ArgumentTypeError("release tags look like v0.2.0")
    return value


def _pid(value: str) -> str:
    if not valid_id(value):
        raise argparse.ArgumentTypeError("twin ids look like acme-helper")
    return value


def setup(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="twin_command", metavar="COMMAND")
    p = subs.add_parser("status", help="Show every twin and its state")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    p = subs.add_parser("sync", help="Sync now")
    p.add_argument("--pair", type=_pid)
    subs.add_parser("list-grok", help="List bots in the Grok app")
    p = subs.add_parser("add", help="Twin Grok bots (preview first)")
    p.add_argument("--all", action="store_true", help="Every bot without a twin")
    p.add_argument("--bot", action="append", metavar="NAME", help="Bot name or id (repeatable)")
    # not --profile: hermes reads --profile/-p anywhere on the command line to pick its own home
    p.add_argument("--hermes-profile", dest="profile", type=_pid, metavar="NAME",
                   help="Hermes profile for the twin (created when missing; default: a slug of the bot name)")
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask")
    p = subs.add_parser("conflicts", help="Show conflicts of a twin (default: the only twin)")
    p.add_argument("pair", nargs="?", type=_pid)
    p = subs.add_parser("resolve", help="Resolve a conflict: resolve [PAIR] CONFLICT_ID --keep hermes|grok")
    p.add_argument("args", nargs="+", metavar="[PAIR] CONFLICT_ID")
    p.add_argument("--keep", choices=("hermes", "grok"), required=True)
    p = subs.add_parser("restore-points", help="List restore points (default: the only twin)")
    p.add_argument("pair", nargs="?", type=_pid)
    p = subs.add_parser("restore", help="Put a kept copy back: restore [PAIR] POINT_ID")
    p.add_argument("args", nargs="+", metavar="[PAIR] POINT_ID")
    p = subs.add_parser("recent", help="Recent activity (metadata only)")
    p.add_argument("pair", nargs="?", type=_pid)
    p.add_argument("--limit", type=int, default=10)
    p = subs.add_parser("parity", help="Parity check results")
    p.add_argument("pair", nargs="?", type=_pid)
    p = subs.add_parser("checklist", help="By-hand checklist and switch readiness")
    p.add_argument("pair", nargs="?", type=_pid)
    p = subs.add_parser("switch", help="Resume the paused Hermes jobs of a twin")
    p.add_argument("pair", nargs="?", type=_pid)
    p.add_argument("--yes", "-y", action="store_true")
    p = subs.add_parser("send-to-grok", help="Send approved changes to Grok (through the Mirror bot)")
    p.add_argument("pair", nargs="?", type=_pid)
    p = subs.add_parser("approve", help="Approve one change that is waiting: approve [PAIR] ITEM")
    p.add_argument("args", nargs="+", metavar="[PAIR] ITEM")
    p = subs.add_parser("mirror", help="Connect the twin to your Mirror bot (or --off)")
    p.add_argument("pair", nargs="?", type=_pid)
    g = p.add_mutually_exclusive_group()  # the demo needs no folder; a real twin is refused without one
    g.add_argument("--pair-folder", metavar="DIR", help="A folder your sync app keeps in step (outside Hermes)")
    g.add_argument("--off", action="store_true", help="Disconnect (botport also replaces the pair key)")
    g.add_argument("--rotate-key", action="store_true", help="Replace the twin's pair key (botport twin mirror "
                                                              "--rotate-key)")
    p = subs.add_parser("remove", help="Stop twinning (the Hermes profile stays)")
    p.add_argument("pair", type=_pid)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", "-y", action="store_true")
    p = subs.add_parser("digest", help="Activity digest to Grok: preview, or turn it on/off for one twin")
    p.add_argument("pair", nargs="?", type=_pid)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")
    p.add_argument("--titles", action="store_true", help="Also send short titles (separate opt-in)")
    subs.add_parser("doctor", help="Check the setup")
    p = subs.add_parser("setup", help="Check and trust the botport binary (do this once, and after updates)")
    p.add_argument("--allow-unattested", action="store_true",
                   help="DANGER: trust botport WITHOUT a passing gh attestation verify (a local build only); "
                        "recorded and shown by doctor and status")
    p.add_argument("--tag", type=_tag, metavar="vX.Y.Z",
                   help="The botport release tag the attestation must come from (default: the botport_release_tag "
                        "setting, else v0.2.0)")
    p = subs.add_parser("attest", help="Run gh attestation verify on the trusted botport")
    p.add_argument("--tag", type=_tag, metavar="vX.Y.Z",
                   help="The release tag to pin (default: the tag recorded at setup, else the expected one)")
    p = subs.add_parser("demo", help="Try it with an invented bot (no Grok account needed)")
    p.add_argument("action", nargs="?", choices=("start", "remove", "grok-edit", "hermes-edit", "conflict", "secret"))
    p.add_argument("--hermes-profile", dest="profile", type=_pid, metavar="NAME",
                   help="Profile the demo twin belongs to (default: this one)")
    parser.set_defaults(func=handle)


def handle(args) -> int:
    sub = getattr(args, "twin_command", None)
    if not sub:
        _out("Usage: hermes twin <status|sync|list-grok|add|conflicts|resolve|restore-points|restore|recent|"
             "parity|checklist|switch|send-to-grok|approve|mirror|remove|digest|setup|attest|doctor|demo>   (try: hermes twin demo)")
        return 2
    try:
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

