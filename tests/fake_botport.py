"""A fake `botport` for tests: answers every botport.twin.v1 command with contract envelopes on invented Acme data.

Tests install it as an executable (with their own interpreter in the shebang) and steer it through
`<state-dir>/fake_mode.json` (the child gets a scrubbed environment, so env vars cannot steer it):
  {"mode": "normal" | "badjson" | "nonobject" | "wrongschema" | "huge" | "sleep" | "locked" | "error" |
           "badtypes" | "okbadexit" | "empty",
   "rc": int (override the exit code), "attention": bool (exit 1 with ok:true), "features": [..],
   "name": str (the bot's name as the Grok cache would give it),
   "accounts": [label, ..] (the cache holds several accounts: commands that read it need --account=LABEL,
                else error account_choice, exit 2),
   "sync_error_pair": id (`twin sync --all` reports this twin in place as state "error"; the others go on),
   "status": {..} (fields laid over the twin's `twin status` entry: pending, by_hand, conflicts, waiting, ...),
   "sync_refused": {"code", "message"} (`twin sync` of one twin reports it as state "error" with this error),
   "doctor_checks": [{..}] (extra lines in `twin doctor`'s answer, exactly as given),
   "extras": true (every dict in every answer gets unexpected fields, incl. a nested dict with a fake key,
                   marked EXTRA_MARKER: none of it may reach the plugin's REST/tool/status-cache output)}
Every call appends {"argv": [...], "env": {...}} to `<state-dir>/fake_calls.jsonl`.
"""
import json
import os
import sys
import time

SCHEMA = "botport.twin.v1"
FEATURES = ["grok-list", "twin-create", "twin-check", "twin-sync", "twin-status", "twin-conflicts", "twin-resolve",
            "twin-approve", "twin-restore", "twin-recent", "twin-remove", "twin-mirror", "twin-parity", "twin-doctor",
            "twin-demo"]
AT = "2000-01-01T09:00:00Z"
EXTRA_MARKER = "EXTRA-MARKER-7f3a"


def with_extras(value):
    """Unexpected fields on every dict, as a newer (or hostile) botport might send them."""
    if isinstance(value, dict):
        out = {k: with_extras(v) for k, v in value.items()}
        out["zz_extra_" + EXTRA_MARKER] = {"fake_key": "sk-" + EXTRA_MARKER, "note": EXTRA_MARKER,
                                           "nested": {"deeper": EXTRA_MARKER}}
        out["comment_" + EXTRA_MARKER] = EXTRA_MARKER
        return out
    if isinstance(value, list):
        return [with_extras(v) for v in value]
    return value


def parse(argv):
    pos, vals, flags = [], {}, []
    i = 0
    while i < len(argv):
        s = argv[i]
        if s.startswith("--"):
            k, eq, v = s[2:].partition("=")
            if eq:
                vals.setdefault(k, []).append(v)
            elif k in ("json", "all", "dry-run", "off", "help", "yes", "rotate-key"):
                flags.append(k)
            else:
                i += 1
                vals.setdefault(k, []).append(argv[i] if i < len(argv) else "")
        else:
            pos.append(s)
        i += 1
    return pos, vals, flags


def sync_entry(pair, dry=False):
    return {"pair_id": pair, "state": "in_step", "dry_run": dry,
            "applied": [{"item": "persona.name", "kind": "name", "name": "name", "direction": "g2h"}],
            "waiting": [], "conflicts": [], "to_grok": 1, "restore_point": None, "wall_ms": 12,
            "secrets_withheld": [], "future_field": {"x": 1}}


READS_CACHE = ("grok list", "twin create", "twin sync", "twin status", "twin approve")
# create approves name/description/instructions/skills; routines and connectors keep waiting for their own OK
CREATE_WAITING = [
    {"item": "routine:Morning summary", "reason": "needs_approval", "detail": "New routine from Grok: approve it"},
    {"item": "connector:example-files", "reason": "needs_approval", "detail": "New connector from Grok: approve it"},
]


def data_for(cmd, vals, flags, mode):
    one = lambda k, d=None: vals.get(k, [d])[-1]
    pair = one("pair", "acme-support")
    name = mode.get("name", "Acme Support")
    if cmd == "version":
        return {"version": "0.1.2", "schema": SCHEMA, "features": mode.get("features", FEATURES)}
    if cmd == "grok list":
        return {"app_found": True, "bots": [
            {"grok_id": "b-acme-0001", "name": name, "description": "Answers questions about Acme widgets.",
             "routines": ["Morning summary"], "twinned_pair_id": "acme-support",
             "fields": {"name": "live", "description": "live", "instructions": "mirror", "skills": "mirror",
                        "routines": "names"}}], "only": vals.get("only", [])}
    if cmd == "twin create":
        return {"dry_run": "dry-run" in flags, "skipped": [], "pairs": [{**sync_entry("acme-support"),
                "waiting": [] if "dry-run" in flags else CREATE_WAITING,
                "grok": {"id": one("grok-id"), "name": name},
                "hermes": {"profile": one("profile", "acme-support"), "created": True, "adopted": False},
                "items": [{"item": "persona.name", "kind": "name", "name": "name", "action": "create",
                           "owner": "grok", "preview": name}]}]}
    if cmd == "twin check":
        return {"changed": ["acme-support"], "checked_at": AT}
    if cmd == "twin sync":
        out = [sync_entry(pair, "dry-run" in flags)]
        if isinstance(mode.get("sync_refused"), dict):  # botport refuses to apply anything to this twin
            out = [{"pair_id": pair, "state": "error", "applied": [], "waiting": [], "conflicts": [],
                    "secrets_withheld": [], "to_grok": 0, "restore_point": None, "dry_run": False,
                    "error": mode["sync_refused"]}]
        bad = mode.get("sync_error_pair")
        if bad and "all" in flags:
            out.append({"pair_id": bad, "state": "error", "applied": [], "waiting": [], "conflicts": [],
                        "secrets_withheld": [], "to_grok": 0, "restore_point": None, "dry_run": False,
                        "error": {"code": "internal", "message": "this twin's state file could not be read"}})
        return {"pairs": out}
    if cmd == "twin status":
        return {"pairs": [{"pair_id": pair, "state": "in_step",
                           "grok": {"id": "b-acme-0001", "name": mode.get("name", "Acme\u0000 Support\u0007"),
                                    "present": True},
                           "hermes": {"profile": "acme-support"}, "last_sync_at": AT, "last_change_at": AT,
                           "pending": {"to_hermes": 0, "to_grok": 0, "in_flight": 0, "waiting_approval": 0},
                           "conflicts": 0, "coverage": [], "by_hand": [], "mirror_bot": {"connected": False},
                           "restore_points": 1, "secrets_withheld_total": 0, "secrets_withheld": [], "error": None,
                           **(mode.get("status") if isinstance(mode.get("status"), dict) else {})}]}
    if cmd == "twin conflicts":
        return {"conflicts": [{"conflict_id": "skill:greet-visitors", "kind": "skill", "name": "greet-visitors",
                               "since": AT, "grok": {"digest": "ea8d70adba66", "text": "Welcome.\n",
                                                     "text_truncated": False},
                               "hermes": {"digest": "aa44e30d6551", "text": "Hello.\n", "text_truncated": False},
                               "diff": "--- grok\n+++ hermes\n-Welcome.\n+Hello.\n"}]}
    if cmd == "twin resolve":
        return {"applied": [{"item": one("conflict"), "keep": one("keep")}], "restore_point": None}
    if cmd == "twin approve":
        return {"approved": [{"item": one("item"), "to": "local"}], "sync": sync_entry(pair)}
    if cmd == "twin restore-points":
        return {"points": [{"id": "persona.name@8ef22c6d10ba", "item": "persona.name", "kind": "name",
                            "name": "name", "at": AT, "reason": "kept copy", "scope": "item"}]}
    if cmd == "twin restore":
        return {"restored": [one("point").split("@")[0]], "dry_run": "dry-run" in flags, "restore_point": None}
    if cmd == "twin recent":
        return {"entries": [{"at": AT, "side": "grok", "kind": "name_change", "summary": "renamed"}],
                "limit": one("limit")}
    if cmd == "twin remove":
        return {"pair_id": pair, "dry_run": "dry-run" in flags, "profile_kept": True,
                "removed": [f"state:pairs/{pair}.toml", f"state:pairs/{pair}.key"]}
    if cmd == "twin mirror":
        if "rotate-key" in flags:
            return {"pair_id": pair, "key_rotated": True, "pair_key": f"state:pairs/{pair}.key"}
        if "off" in flags:
            return {"pair_id": pair, "connected": False}
        return {"pair_id": pair, "connected": True, "briefs": f"state:pairs/{pair}.brief",
                "pair_key": f"state:pairs/{pair}.key", "steps": [{"id": "mirror_bot", "what": "Create a Mirror bot"}],
                "charter": "# botport Mirror bot charter\n", "folder_seen": one("pair-folder")}
    if cmd == "twin parity":
        return {"kind": "setup", "passed": 1, "total": 2, "checks": [
            {"id": "skill:greet-visitors", "name": "skill greet-visitors", "pass": True, "detail": "same"},
            {"id": "model", "name": "model", "pass": False, "detail": "no model"}]}
    if cmd == "twin doctor":
        return {"checks": [{"id": "state_dir", "ok": True, "message": "state folder is fine", "fix": ""}]}
    if cmd == "twin demo":
        return {"pairs": [sync_entry("acme-support")], "step": one("step")}
    return None


def main():
    argv = sys.argv[1:]
    pos, vals, flags = parse(argv)
    state = vals.get("state-dir", [""])[-1]
    mode = {}
    if state:
        try:
            with open(os.path.join(state, "fake_mode.json")) as fh:
                mode = json.load(fh)
        except (OSError, ValueError):
            mode = {}
        with open(os.path.join(state, "fake_calls.jsonl"), "a") as fh:
            fh.write(json.dumps({"argv": argv, "env": dict(os.environ), "exe": sys.argv[0]}) + "\n")
    cmd = " ".join(pos[:2]) if pos and pos[0] in ("twin", "grok") else (pos[0] if pos else "")
    m = mode.get("mode", "normal")
    out = sys.stdout
    if m == "sleep":
        time.sleep(30)
    if m == "empty":
        sys.stderr.write("something went wrong\n")
        return 4
    if m == "badjson":
        out.write("this is not json {\n")
        return 0
    if m == "nonobject":
        out.write("[1, 2]\n")
        return 0
    if m == "huge":
        chunk = "x" * 65536
        for _ in range(64):
            out.write(chunk)
        out.flush()
        return 0
    env = {"schema": SCHEMA, "ok": True, "command": cmd, "data": None, "warnings": [], "error": None}
    if m == "wrongschema":
        env["schema"] = "botport.twin.v0"
    if m == "locked":
        env.update(ok=False, error={"code": "locked", "message": "state folder is locked by another botport run",
                                    "hint": "try again"})
        out.write(json.dumps(env) + "\n")
        return 3
    if m == "error":
        env.update(ok=False, error={"code": "pair_not_found", "message": 'no twin "nobody"', "hint": None})
        out.write(json.dumps(env) + "\n")
        return 3
    accounts = mode.get("accounts")
    if accounts and cmd in READS_CACHE and vals.get("account", [None])[-1] not in accounts:
        env.update(ok=False, error={"code": "account_choice", "hint": "botport grok list --account LABEL",
                                    "message": f"the Grok app's cache holds {len(accounts)} accounts "
                                               f"({', '.join(accounts)}): choose one with --account LABEL"})
        out.write(json.dumps(env) + "\n")
        return 2
    if m == "badtypes":
        env.update(ok="yes", warnings={})
        out.write(json.dumps(env) + "\n")
        return 0
    data = data_for(cmd, vals, flags, mode)
    if data is None:
        env.update(ok=False, error={"code": "usage", "message": f"unknown command {cmd!r}", "hint": None})
        out.write(json.dumps(env) + "\n")
        return 2
    if cmd == "twin doctor" and isinstance(mode.get("doctor_checks"), list):
        data["checks"] = data["checks"] + mode["doctor_checks"]
    env["data"] = with_extras(data) if mode.get("extras") else data
    env["warnings"] = [{"code": "no_bots", "message": "nothing new"}]
    env["unknown_top_level"] = 1
    out.write(json.dumps(env) + "\n")
    if m == "okbadexit":
        return 4
    if "rc" in mode:
        return int(mode["rc"])
    return 1 if mode.get("attention") else 0


if __name__ == "__main__":
    sys.exit(main())
