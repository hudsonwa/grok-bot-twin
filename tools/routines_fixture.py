#!/usr/bin/env python3
"""Invented routines for live tests of routines between computers, on a SANDBOX Hermes folder.

    python3 tools/routines_fixture.py --home <HERMES_HOME> add "Test Scout" "Morning trails" "0 7 * * *" "List three trails."
    python3 tools/routines_fixture.py --home <HERMES_HOME> add "Test Scout" "Hourly check" "every 60m" "Say the time."
    python3 tools/routines_fixture.py --home <HERMES_HOME> edit "Test Scout" "Morning trails" "List four trails."
    python3 tools/routines_fixture.py --home <HERMES_HOME> remove "Test Scout" "Morning trails"
    python3 tools/routines_fixture.py --home <HERMES_HOME> list

It writes a job the way Hermes records one into `profiles/<the twin's bot>/cron/jobs.json` of that folder (made by
tools/fleet_fixture.py), switched on. A test tool: it is not part of what the plugin runs, and it refuses a folder
that is not named on the command line. Never point it at a real Hermes folder.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fleet import load_plugin  # noqa: E402


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="routines_fixture.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", required=True)
    ap.add_argument("action", choices=("add", "edit", "remove", "list"))
    ap.add_argument("twin", nargs="?")
    ap.add_argument("name", nargs="?")
    ap.add_argument("a", nargs="?")
    ap.add_argument("b", nargs="?")
    ap.add_argument("--script", help="make it a script routine (it then stays on its own computer)")
    args = ap.parse_args(argv)
    home = Path(args.home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    load_plugin()
    from hermes_plugins.grok_bot_twin.twin import config, fleet_io, routines_jobs as rj, store
    config.set_override({"botport_path": str(home / "no-botport-here")})
    raw = fleet_io.read_json(store.central_dir() / "pairs.json", {}) or {}
    pairs = [p for p in raw.get("pairs", []) if isinstance(p, dict)]
    if args.action == "list":
        for p in pairs:
            prof = (p.get("hermes") or {}).get("profile")
            for j in rj.load(store.hermes_root() / "profiles" / str(prof)) or []:
                print(f"{(p.get('grok') or {}).get('name')}\t{j.get('id')}\t{j.get('name')}\t"
                      f"{'on' if rj.runnable(j) else 'paused'}\t{j.get('paused_reason') or ''}")
        return 0
    pair = next((p for p in pairs if (p.get("grok") or {}).get("name") == args.twin), None)
    if pair is None or not args.name:
        ap.error("give the twin's name (see fleet_fixture.py list) and the routine's name")
    prof_home = store.hermes_root() / "profiles" / pair["hermes"]["profile"]

    def change(jobs: list) -> tuple:
        job = next((j for j in jobs if j.get("name") == args.name), None)
        if args.action == "add":
            if job is not None:
                ap.error("a routine of that name exists")
            sched = args.a or "0 7 * * *"
            m = re.match(r"^every (\d+)m$", sched)
            schedule = {"kind": "interval", "minutes": int(m.group(1)), "display": sched} if m else \
                {"kind": "cron", "expr": sched, "display": sched}
            job = rj.new_record({"name": args.name, "prompt": args.b or "Say hello.", "schedule": schedule,
                                 "skills": []}, "")
            job.update(enabled=True, state="scheduled", paused_at=None, paused_reason=None)
            if args.script:
                job["script"] = args.script
            jobs.append(job)
        elif job is None:
            ap.error("no routine of that name; see `list`")
        elif args.action == "edit":
            job["prompt"] = args.a or ""
        else:
            jobs.remove(job)
        return True, None
    rj.edit(prof_home, change)
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
