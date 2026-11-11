"""Helpers for the tests of requests between the owner's computers ("do X on Surface").

The chat turn itself is Hermes' job, so the tests put a stand-in `hermes` program on PATH. It records exactly how
it was started (the words, the request on standard input, and the approval-related variables it could see) and
answers by a tiny script, so a test can check what reached the asked computer and what came back:

- the last line of the request is echoed as "<bot> heard: <line>";
- "nap N" sleeps N seconds first; "fail" exits with an error; "leak" answers with a made-up key;
- "long" answers with 40,000 characters; "needs approval" answers the way Hermes does when a step was refused.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

FAKE = r'''
import json, os, sys, time
words = sys.argv[1:]
text = sys.stdin.read()
rec = {"argv": words, "stdin": text, "home": os.environ.get("HERMES_HOME"), "pid": os.getpid(),
       "asked": os.environ.get("GROK_BOT_TWIN_ASKED_TURN"),
       "env": {k: v for k, v in os.environ.items() if k.startswith("HERMES_") and k not in ("HERMES_HOME", "HERMES_GATEWAY_LOCK_DIR")}}
log = os.environ.get("FAKE_HERMES_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
last = text.strip().splitlines()[-1] if text.strip() else ""
bot = words[words.index("-p") + 1] if "-p" in words else "default"
low = last.lower()
if low.startswith("nap "):
    time.sleep(float(low.split()[1]))
if "fail" in low:
    sys.exit(3)
sys.stderr.write("\nsession_id: local-0123456789abcdef\n")
if "leak" in low:
    print("the key is " + "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4")
elif "long" in low:
    print("x" * 40000)
elif "needs approval" in low:
    print("BLOCKED: Command flagged as dangerous (recursive delete) but single-query mode (-q) runs without a user present to approve it.")
else:
    print(f"{bot} heard: {last}")
'''


def install_fake_hermes(base: Path) -> Path:
    """Write the stand-in `hermes` into `base`/bin and return that folder (to be put first on PATH)."""
    bindir = base / "fake-bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "fake_hermes.py"
    script.write_text(FAKE, encoding="utf-8")
    if os.name == "nt":
        (bindir / "hermes.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        prog = bindir / "hermes"
        prog.write_text(f"#!{sys.executable}\n" + FAKE, encoding="utf-8")
        prog.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    return bindir


def calls(log: Path) -> list:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---- a stand-in for Hermes' stdio interface (python -m tui_gateway.entry), for the approval tests ------------------
FAKE_RPC = r'''
import json, os, sys, threading, time
ATTACHED = "attached" in sys.argv[1:]
lock = threading.Lock()
def out(obj):
    with lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
def event(kind, payload=None, sid="s1s1s1s1"):
    out({"jsonrpc": "2.0", "method": "event", "params": {"type": kind, "session_id": sid, "payload": payload or {}}})
rec = {"rpc": True, "argv": sys.argv[1:], "home": os.environ.get("HERMES_HOME"), "cwd": os.getcwd(), "got": [],
       "asked": os.environ.get("GROK_BOT_TWIN_ASKED_TURN"),
       "env": {k: v for k, v in os.environ.items() if k.startswith("HERMES_") and k not in ("HERMES_HOME", "HERMES_GATEWAY_LOCK_DIR")}}
def save():
    log = os.environ.get("FAKE_HERMES_LOG")
    if log:
        with open(log + ".rpc", "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec))
bot = os.path.basename(os.environ.get("HERMES_HOME", "").rstrip("/\\")) or "default"
state = {"answered": False}
def complete(text):
    event("message.complete", {"text": text})
def ask_approval():
    # ("padded": a step far longer than a page shows, with the part that matters at its very end)
    q = {"command": ("echo " + "x " * 300 + "&& echo the-end-nobody-was-shown") if state.get("padded")
         else "rm -rf ./scratch-folder", "description": "recursive delete",
         "choices": ["once", "session", "always", "deny"], "timeout_s": 300}
    if ATTACHED:
        event("approval.request", {**q, "kind": "approval", "prompt_id": "p1", "execution_generation": 1})
    else:
        out({"jsonrpc": "2.0", "id": "srq-1", "method": "approval", "params": {**q, "session_id": "s1s1s1s1", "request_id": "r1"}})
def answered(choice):
    state["answered"] = True
    if ATTACHED:
        event("approval.settled", {"prompt_id": "p1"})
    complete("did the risky thing" if choice == "once" else "BLOCKED: Command denied by user.")
event("gateway.ready", {"skin": {}}, sid="")
for line in sys.stdin:
    try:
        f = json.loads(line)
    except ValueError:
        continue
    rec["got"].append(f)
    save()
    m, p = f.get("method"), f.get("params") or {}
    if m == "client.capabilities":
        out({"jsonrpc": "2.0", "id": f["id"], "result": {"server_requests": ["approval", "sudo", "clarify"]}})
    elif m == "session.create":
        out({"jsonrpc": "2.0", "id": f["id"], "result": {"session_id": "s1s1s1s1", "info": {"profile_name": bot}}})
    elif m == "prompt.submit":
        if ATTACHED and not p.get("submission_id"):
            out({"jsonrpc": "2.0", "id": f["id"], "error": {"code": 4001, "message": "invalid_params"}})
            continue
        out({"jsonrpc": "2.0", "id": f["id"], "result": {"status": "queued" if ATTACHED else "streaming"}})
        last = (p.get("text") or "").strip().splitlines()[-1]
        low = last.lower()
        if "risky" in low:
            state["padded"] = "padded" in low
            ask_approval()
            if "nobody" in low:
                def give_up():
                    time.sleep(1.5)
                    if not state["answered"]:
                        if ATTACHED:
                            event("approval.settled", {"prompt_id": "p1"})
                        else:
                            event("request.cancel", {"id": "srq-1", "method": "approval", "reason": "timeout"})
                        complete("BLOCKED: Command timed out without user response. Silence is not consent.")
                threading.Thread(target=give_up, daemon=True).start()
        elif "password" in low:
            if ATTACHED:  # (no answer can come for this: the turn goes on without one)
                event("secret.request", {"prompt_id": "s1"})
                def no_password():
                    time.sleep(1.0)
                    complete("no password was given" if not state.get("secret") else "a password was given")
                threading.Thread(target=no_password, daemon=True).start()
            else:
                out({"jsonrpc": "2.0", "id": "srq-2", "method": "sudo", "params": {"session_id": "s1s1s1s1"}})
        elif "which one" in low:
            if ATTACHED:
                event("clarify.request", {"prompt_id": "q1", "question": "which one?", "execution_generation": 1})
            else:
                out({"jsonrpc": "2.0", "id": "srq-3", "method": "clarify", "params": {"session_id": "s1s1s1s1"}})
        else:
            complete(f"{bot} heard: {last}")
    elif m == "approval.respond" and ATTACHED:
        out({"jsonrpc": "2.0", "id": f["id"], "result": {}})
        answered(p.get("choice") if p.get("prompt_id") == "p1" and p.get("session_id") == "s1s1s1s1" else "wrong")
    elif m == "clarify.respond" and ATTACHED:
        out({"jsonrpc": "2.0", "id": f["id"], "result": {}})
        complete("no choice was made" if p.get("answer") == "" else "a choice was made")
    elif m == "session.interrupt":
        out({"jsonrpc": "2.0", "id": f["id"], "result": {"status": "interrupted"}})
    elif m in ("secret.respond", "sudo.respond"):
        state["secret"] = True
    elif f.get("id") == "srq-1":
        answered((f.get("result") or {}).get("choice"))
    elif f.get("id") == "srq-2":
        complete("no password was given" if "error" in f else "a password was given: " + json.dumps(f.get("result")))
    elif f.get("id") == "srq-3":
        complete("no choice was made" if f.get("result") == {} else "a choice was made")
save()
'''


def install_fake_rpc(base: Path, dialect: str = "stdio") -> list:
    """Write the stand-in for Hermes' interface and return the command that starts it. `dialect`: "stdio" (the
    stand-alone interface: questions are requests) or "attached" (a running Hermes: questions are events)."""
    script = base / "fake_rpc.py"
    script.write_text(FAKE_RPC, encoding="utf-8")
    return [sys.executable, str(script), dialect]


def approval_answers(rec: dict) -> list:
    """The choices the plugin sent for the approval question, in either dialect."""
    out = []
    for f in rec.get("got", []):
        if f.get("id") == "srq-1":
            out.append((f.get("result") or {}).get("choice"))
        elif f.get("method") == "approval.respond":
            out.append((f.get("params") or {}).get("choice"))
    return out


def rpc_record(log: Path) -> dict:
    path = Path(str(log) + ".rpc")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
