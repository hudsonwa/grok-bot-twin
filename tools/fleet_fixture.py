#!/usr/bin/env python3
"""Invented twins for live tests of sharing between machines, on a SANDBOX Hermes folder without the Grok app.

    python3 tools/fleet_fixture.py --home <HERMES_HOME> add "Test Scout" "Finds trails."
    python3 tools/fleet_fixture.py --home <HERMES_HOME> rename "Test Scout" "Test Ranger"
    python3 tools/fleet_fixture.py --home <HERMES_HOME> describe "Test Ranger" "Finds better trails."
    python3 tools/fleet_fixture.py --home <HERMES_HOME> remove "Test Ranger"
    python3 tools/fleet_fixture.py --home <HERMES_HOME> list
    python3 tools/fleet_fixture.py --home <HERMES_HOME> config "Test Scout" --toolsets web,terminal \\
        --connector acme-notes --personality concise --persona "Answer briefly." --model test-model --provider test-ai

It writes straight into that folder's plugin registry (`plugin-data/grok-bot-twin/pairs.json`) the way a real twin
is recorded, makes a small Hermes bot for it (`profiles/<name>/SOUL.md`), and tells a running share server on
that folder to publish. A test tool: it refuses a folder that is not named on the command line, and it is not
part of what the plugin runs. Never point it at a real Hermes folder: the Twins page would show these as twins it
cannot check.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fleet import load_plugin  # noqa: E402


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "twin"


def _config(args, home: Path, fleet_io) -> None:
    from hermes_plugins.grok_bot_twin.twin import yamlio
    yaml = yamlio.module()
    if yaml is None:
        raise SystemExit("this Python has no YAML module (PyYAML or ruamel.yaml)")
    text = fleet_io.read_text(home / "config.yaml") or ""
    doc = yaml.safe_load(text) if text.strip() else {}
    doc = doc if isinstance(doc, dict) else {}
    if args.toolsets is not None:
        doc["toolsets"] = [t for t in args.toolsets.split(",") if t]
    for name in args.connector:
        servers = doc.setdefault("mcp_servers", {})
        servers[name] = {"command": "acme-connector", "args": ["--stdio"],
                         "env": {"ACME_TOKEN": args.secret or "set-on-each-computer"},
                         "headers": {"Authorization": f"Bearer {args.secret or 'set-on-each-computer'}"}}
    if args.personality is not None:
        doc.setdefault("display", {})["personality"] = args.personality
    if args.max_turns is not None:
        doc.setdefault("agent", {})["max_turns"] = args.max_turns
    if args.model is not None:
        doc["model"] = {"default": args.model, **({"provider": args.provider} if args.provider else {})}
        if args.secret:
            doc["model"]["api_key"] = args.secret
            doc["providers"] = {args.provider or "test-ai": {"api_key": args.secret, "base_url": "http://127.0.0.1:9/v1"}}
    fleet_io.write_text(home / "config.yaml", yaml.safe_dump(doc, default_flow_style=False, sort_keys=True,
                                                             allow_unicode=True))
    if args.secret:
        fleet_io.write_text(home / ".env", f"ACME_TOKEN={args.secret}\n")
        fleet_io.write_text(home / "auth.json", '{"token": "%s"}\n' % args.secret)
    if args.persona is not None:
        import re
        current = fleet_io.read_text(home / "SOUL.md") or ""
        kept = "".join(m.group(0) for m in re.finditer(
            r"<!-- (botport|grok-bot-twin):begin ([a-z-]+) -->\n.*?<!-- \1:end \2 -->\n", current, re.S)
            if m.group(2) != "persona")  # the name, the description and the plugin's own blocks stay
        soul = kept + args.persona.rstrip("\n") + "\n"
        if args.secret:
            soul += f"password: {args.secret}\n"
        fleet_io.write_text(home / "SOUL.md", soul)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fleet_fixture.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", required=True)
    ap.add_argument("action", choices=("add", "rename", "describe", "remove", "list", "config"))
    ap.add_argument("name", nargs="?")
    ap.add_argument("value", nargs="?")
    # `config`: invented settings in that twin's own Hermes bot (config.yaml, SOUL.md), for tests of the settings
    # that follow a twin. --secret puts a harmless marker everywhere a real secret would live, so a test can
    # prove that it never reaches another computer
    ap.add_argument("--toolsets", help="comma-separated tool set names")
    ap.add_argument("--connector", action="append", default=[], help="a connector's name (may be given twice)")
    ap.add_argument("--personality")
    ap.add_argument("--max-turns", type=int)
    ap.add_argument("--persona", help="the instructions in the bot's prompt file")
    ap.add_argument("--model")
    ap.add_argument("--provider")
    ap.add_argument("--secret", help="a harmless marker written where secrets live")
    args = ap.parse_args(argv)
    home = Path(args.home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    load_plugin()
    from hermes_plugins.grok_bot_twin.twin import config, fleet, fleet_io, fleet_server, service, store
    from hermes_plugins.grok_bot_twin.twin.model import now_iso
    config.set_override({"botport_path": str(home / "no-botport-here")})
    svc = service.get_service()
    path = store.central_dir() / "pairs.json"
    with store.locked():
        raw = fleet_io.read_json(path, {}) or {}
        pairs = [p for p in raw.get("pairs", []) if isinstance(p, dict)] if isinstance(raw, dict) else []
        by_name = {str((p.get("grok") or {}).get("name")): p for p in pairs}
        if args.action == "list":
            for p in pairs:
                g = p.get("grok") or {}
                print(f"{p.get('pair_id')}\t{g.get('name')}\t{g.get('description', '')}")
            return 0
        if not args.name:
            ap.error("give the twin's name")
        if args.action == "config":
            p = by_name.get(args.name)
            if p is None:
                ap.error("no twin of that name; see `list`")
            _config(args, store.hermes_root() / "profiles" / p["hermes"]["profile"], fleet_io)
            fleet.hub.bump()
            fleet_server._tell_server()
            print("ok (settings written)")
            return 0
        if args.action == "add":
            if args.name in by_name:
                ap.error("a twin of that name exists")
            pid = _slug(args.name)
            n = 2
            while any(p.get("pair_id") == pid for p in pairs):
                pid = f"{_slug(args.name)}-{n}"
                n += 1
            pairs.append({"pair_id": pid, "grok": {"id": f"fx-{pid}", "name": args.name, "description": args.value or ""},
                          "hermes": {"profile": pid}, "created_at": now_iso()})
            soul = store.hermes_root() / "profiles" / pid / "SOUL.md"
            if not soul.exists():
                # the name and description between the markers the real helper keeps them in, so they are facts
                # about the twin and not instructions (the instructions are what `config --persona` writes)
                fleet_io.write_text(soul, "<!-- botport:begin persona-name -->\n" + args.name
                                    + "\n<!-- botport:end persona-name -->\n<!-- botport:begin persona-description -->\n"
                                    + (args.value or "") + "\n<!-- botport:end persona-description -->\n")
        else:
            p = by_name.get(args.name)
            if p is None:
                ap.error("no twin of that name; see `list`")
            if args.action == "rename":
                if not args.value:
                    ap.error("give the new name")
                p["grok"]["name"] = args.value
            elif args.action == "describe":
                p["grok"]["description"] = args.value or ""
            else:
                pairs.remove(p)
        fleet_io.write_json(path, {"pairs": pairs})
    changed = fleet.publish(svc)
    fleet_server._tell_server()
    print(f"ok ({'published' if changed else 'saved; it is published when sharing is on'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
