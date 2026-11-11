#!/usr/bin/env python3
"""Run `hermes twin share | link | unlink | fleet ...` without a `hermes` program.

For a machine (or a sandbox) that has Python 3.11+ and this plugin's folder but no Hermes on PATH, and for
background units that keep a machine following while Hermes is closed:

    python3 tools/fleet.py --home <HERMES_HOME> link 7KQ4-M2XD-P9RA
    python3 tools/fleet.py --home <HERMES_HOME> fleet run
    py -3.12 tools\\fleet.py --home <HERMES_HOME> fleet --json

`--home` is the Hermes folder to work in (the twins' Hermes bots are made under its `profiles` folder). It must
be given: this tool never guesses the real one. `--port`, `--address` and `--name` stand in for the plugin's
settings of the same meaning (share_port, share_address, machine_name), which a Hermes install reads from its
config. Everything after those options is passed to the same code `hermes twin` runs.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "hermes_plugins.grok_bot_twin"


def load_plugin():
    if PKG in sys.modules:
        return sys.modules[PKG]
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []  # type: ignore[attr-defined]
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fleet.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", required=True, help="The Hermes folder to work in")
    ap.add_argument("--port", type=int, help="Port to share on / to reach the sharing computer on (default 18777)")
    ap.add_argument("--address", help="Address to share on (default: Tailscale, else the home network)")
    ap.add_argument("--name", help="This computer's name (default: its host name)")
    ap.add_argument("rest", nargs=argparse.REMAINDER, help="share|link|unlink|fleet|setup ...")
    args = ap.parse_args(argv)
    home = Path(args.home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    load_plugin()
    from hermes_plugins.grok_bot_twin.twin import cli, config
    settings = {"botport_path": str(home / "no-botport-here")}
    if args.port is not None:
        settings["share_port"] = args.port
    if args.address:
        settings["share_address"] = args.address
    if args.name:
        settings["machine_name"] = args.name
    config.set_override(settings)
    if [a for a in args.rest if a != "--"][:1] == ["setup"]:  # the one setup command (hermes twin setup ...)
        return cli.main([a for a in args.rest if a != "--"])
    rest = [a for a in args.rest if a != "--"]
    if not rest or rest[0] not in ("share", "link", "unlink", "fleet", "ask", "requests", "routines"):
        ap.error("give one of: share, link, unlink, fleet, ask, requests, routines")
    return cli.main(rest)


if __name__ == "__main__":
    sys.exit(main())
