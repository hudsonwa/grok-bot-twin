#!/usr/bin/env python3
"""Attach to this computer's running Hermes and pass one program's chat messages through. Started only by the
plugin (twin/tasks_turn.py) as a program of its own, with Hermes' own Python, for a request from another of the
owner's computers. It imports nothing of the plugin (it sits in this folder because a twin's own Hermes bot
carries a copy of this folder).

Hermes lets one process at a time own a Hermes folder. When Hermes is open (its Desktop, a chat, a messaging
gateway), a second stand-alone interface for the same bot is refused ("attach to it instead"). This is the
attaching: it uses Hermes' own client for that (`hermes_cli.gateway_client.connect_gateway`, the one `hermes
chat` uses; same user only, over a local socket and a one-time ticket) and then only copies JSON lines:

    standard input  -> Hermes      a line with "method": a call (its answer comes back under the same "id");
                                   a line without one: the answer to a question Hermes asked
    Hermes -> standard output      events and questions, one JSON object per line

So the plugin talks to an attached Hermes exactly as it talks to `python -m tui_gateway.entry`. Nothing is added,
changed or answered here; an approval question passes through to the plugin, which answers it only with what the
owner of this computer chose. The bot is the one named by HERMES_HOME (set by the plugin to the twin's own Hermes
bot). Ends when standard input closes or the connection to Hermes ends. No arguments.
"""
from __future__ import annotations

import os
import sys

# This file sits among the plugin's modules, one of which is called like a standard-library module (secrets.py).
# Its own folder must never be on the import path, or Hermes' client would load the wrong one.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]

import asyncio  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402

CAP = 2 * 1024 * 1024
_lock = threading.Lock()


def out(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False)
    with _lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _stdin_lines(loop: asyncio.AbstractEventLoop, lines: "asyncio.Queue") -> None:
    try:
        for raw in sys.stdin:
            if len(raw) <= CAP:
                loop.call_soon_threadsafe(lines.put_nowait, raw)
    except (OSError, ValueError):
        pass
    loop.call_soon_threadsafe(lines.put_nowait, None)


async def _calls(client, lines: "asyncio.Queue") -> None:
    """What the plugin sends: calls are made through Hermes' client; answers to Hermes' questions go as they are."""
    from hermes_cli.gateway_client import GatewayClientError
    while True:
        raw = await lines.get()
        if raw is None:
            return
        try:
            frame = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue
        method, params = frame.get("method"), frame.get("params")
        if isinstance(method, str):
            try:
                result = await client.rpc(method, **(params if isinstance(params, dict) else {}))
                out({"jsonrpc": "2.0", "id": frame.get("id"), "result": result})
            except GatewayClientError as exc:
                out({"jsonrpc": "2.0", "id": frame.get("id"),
                     "error": {"code": getattr(exc, "code", None) or -32000, "message": str(exc)[:300]}})
        elif "id" in frame and ("result" in frame or "error" in frame):
            await client.websocket.send(json.dumps(frame))


async def _events(client) -> None:
    """What Hermes sends: events and questions, as they are."""
    while True:
        frame = await client.events.get()
        if not isinstance(frame, dict):  # the connection ended
            return
        out(frame)


async def main() -> int:
    from hermes_cli.gateway_client import connect_gateway
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue = asyncio.Queue()
    threading.Thread(target=_stdin_lines, args=(loop, lines), name="bridge-stdin", daemon=True).start()
    async with connect_gateway() as client:  # (Hermes itself says "gateway.ready" to a client that attaches)
        tasks = [asyncio.create_task(_calls(client, lines)), asyncio.create_task(_events(client))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # said in one line on standard error (the plugin logs it; it is never sent anywhere)
        sys.stderr.write(f"turn_bridge: {type(exc).__name__}: {str(exc)[:300]}\n")
        sys.exit(1)
