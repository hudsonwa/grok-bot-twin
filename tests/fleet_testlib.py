"""Helpers for the fleet tests: other "machines" as real processes with their own Hermes folder, on loopback.

The plugin keeps one service per process, so a second machine cannot live in the test's own process. `Machine`
runs the standalone runner (`tools/fleet.py`) and the fixture tool (`tools/fleet_fixture.py`) as subprocesses
with their own HERMES_HOME; the test process itself is usually the follower (its `home` fixture). Sockets are
real (127.0.0.1, a free port per test). Invented twins only.
"""
from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from twin_testlib import ROOT

TOOLS = ROOT / "tools"
SCHEMA = "grok-bot-twin.share.v1"
WIRE_KEYS = {
    "": {"schema", "proto", "changed", "sharing", "rev", "generated_at", "source", "twins", "removed", "fleet"},
    "source": {"id", "name", "platform"},
    "twins[]": {"id", "name", "description", "version", "updated_at"},
    "removed[]": {"id", "removed_at"},
    "fleet[]": {"id", "name", "platform", "role"},
}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until(check, timeout: float = 20.0, step: float = 0.1, what: str = "the condition"):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            last = check()
            if last:
                return last
        except (OSError, ValueError, KeyError, AssertionError) as exc:
            last = exc
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}; last: {last!r}")


class Machine:
    """One machine: a Hermes folder of its own, driven through tools/fleet.py in subprocesses."""

    def __init__(self, base: Path, name: str, port: int, address: Optional[str] = "127.0.0.1") -> None:
        self.home = base / f"home-{name.lower().replace(' ', '-')}"
        self.home.mkdir(parents=True, exist_ok=True)
        self.name, self.port, self.address = name, port, address
        self.proc: Optional[subprocess.Popen] = None
        self.log = base / f"run-{name.lower().replace(' ', '-')}.log"

    # ---- processes ------------------------------------------------------------------------------------------------
    def _argv(self, *words: str) -> list:
        argv = [sys.executable, str(TOOLS / "fleet.py"), "--home", str(self.home), "--port", str(self.port),
                "--name", self.name]
        if self.address:
            argv += ["--address", self.address]
        return argv + list(words)

    def run(self, *words: str, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
        cp = subprocess.run(self._argv(*words), capture_output=True, text=True, timeout=timeout)
        if check and cp.returncode != 0:
            raise AssertionError(f"{self.name}: {' '.join(words)} failed ({cp.returncode}):\n{cp.stdout}\n{cp.stderr}")
        return cp

    def json(self, *words: str) -> Any:
        return json.loads(self.run(*words, "--json").stdout)

    def fx(self, *words: str) -> None:
        cp = subprocess.run([sys.executable, str(TOOLS / "fleet_fixture.py"), "--home", str(self.home), *words],
                            capture_output=True, text=True, timeout=60)
        assert cp.returncode == 0, cp.stdout + cp.stderr

    def start(self) -> None:
        """`fleet run` in the background (serves when sharing is on, follows when linked)."""
        assert self.proc is None
        out = open(self.log, "ab")
        self.proc = subprocess.Popen(self._argv("fleet", "run"), stdout=out, stderr=subprocess.STDOUT)
        out.close()

    def stop(self, hard: bool = False) -> None:
        if self.proc is None:
            return
        if hard:
            self.proc.kill()
        else:
            self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)
        self.proc = None

    # ---- state on disk ----------------------------------------------------------------------------------------------
    @property
    def data(self) -> Path:
        return self.home / "plugin-data" / "grok-bot-twin"

    def state(self) -> dict:
        return json.loads((self.data / "fleet.json").read_text(encoding="utf-8"))

    def write_state(self, st: dict) -> None:
        (self.data / "fleet.json").write_text(json.dumps(st), encoding="utf-8")

    def code(self) -> str:
        """A fresh link code, as the owner would read it off the page."""
        return self.json("share", "code")["code"]["code"]

    def listening(self) -> bool:
        try:
            status, obj, _ = wire("GET", self.port, "/v1/hello")
            return status == 200 and obj.get("sharing") is True
        except OSError:
            return False

    def share(self) -> str:
        """Switch sharing on, keep serving in the background, and return a link code."""
        self.run("share", "on")
        self.start()
        wait_until(self.listening, what=f"{self.name} to listen")
        return self.code()

    def profile(self, name: str) -> Path:
        return self.home / "profiles" / name

    def soul(self, name: str) -> str:
        return (self.profile(name) / "SOUL.md").read_text(encoding="utf-8")


def _fleet():
    from hermes_plugins.grok_bot_twin.twin import fleet, fleet_net
    return fleet, fleet_net


def wire(method: str, port: int, path: str, body: Any = None, token: Optional[str] = None, timeout: float = 10,
         raw_body: Optional[bytes] = None, headers: Optional[dict] = None, answer: Optional[dict] = None) -> tuple:
    """One raw request to a share server. Returns (status, parsed JSON or None, the raw answer bytes).
    `token`: "MACHINE-ID|PASS" (what wire_link hands back) signs the request the way a linked machine does;
    any other text is sent as a bearer value (the local "read again" route). `answer`, when given, receives the
    answer's headers and whether it was signed for this request ("signed")."""
    fleet, _net = _fleet()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        hdrs = {"Content-Type": "application/json"}
        data = raw_body if raw_body is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        key = nonce = None
        if token and "|" in token:
            mid, pass_ = token.split("|", 1)
            key, nonce = fleet.share_key(pass_), fleet.new_nonce()
            hdrs.update({"X-GBT-Machine": mid, "X-GBT-Nonce": nonce,
                         "X-GBT-Mac": fleet.request_mac(key, path, nonce, data or b"")})
        elif token:
            hdrs["Authorization"] = "Bearer " + token
        hdrs.update(headers or {})
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        if answer is not None:
            answer["headers"] = dict(resp.headers.items())
            answer["signed"] = key is not None and fleet.same(resp.headers.get("X-GBT-Mac") or "",
                                                              fleet.answer_mac(key, nonce, resp.status, raw))
        try:
            obj = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            obj = None
        return resp.status, obj, raw
    finally:
        conn.close()


def wire_link(port: int, code: str, mid: str = "m-aaaaaaaaaaaa", name: str = "Probe") -> tuple:
    """Link as a machine would, in the exchange's two messages: neither the code nor anything it could be worked
    out from is sent. On success the parsed answer of the FIRST message is returned (nonce, dh, proof, source)
    with two keys made on this side: "pass" ("MACHINE-ID|PASS", for the other helpers) and "proved" (the source
    proved it knows the code too). Unlike the real follower this helper sends its own proof even when the
    source's did not fit, so that the source's answer to a wrong code can be looked at."""
    fleet, net = _fleet()
    key = fleet.link_key(net.normalize_code(code) or "2" * 12)
    nonce = fleet.new_nonce()
    machine = {"id": mid, "name": name, "platform": "linux"}
    secret, public = fleet.dh_new(fleet.link_base(key))
    status, obj, raw = wire("POST", port, "/v1/link", {"proto": fleet.PROTO, "machine": machine, "nonce": nonce,
                                                        "dh": public})
    if status != 200 or not isinstance(obj, dict):
        return status, obj, raw
    proof, confirm, pass_ = fleet.link_keys(fleet.dh_shared(secret, obj["dh"]), nonce, obj["nonce"], mid,
                                            obj["source"]["id"], public, obj["dh"])
    got: dict = {}
    status2, obj2, raw2 = wire("POST", port, "/v1/link", {"proto": fleet.PROTO, "machine": machine, "nonce": nonce,
                                                           "confirm": confirm}, answer=got)
    if status2 != 200:
        return status2, obj2, raw2
    obj = dict(obj)
    obj["proved"] = fleet.same(obj.get("proof"), proof) and fleet.same(
        got["headers"].get("X-GBT-Mac") or "", fleet.answer_mac(fleet.share_key(pass_), nonce, status2, raw2))
    obj["pass"] = mid + "|" + pass_
    return status, obj, raw


_counted = [0]


def next_count() -> int:
    """A call number as a linked computer makes them: its clock in milliseconds, and always higher than the last."""
    _counted[0] = max(_counted[0] + 1, int(time.time() * 1000))
    return _counted[0]


def wire_sync(port: int, token: str, have: Optional[str] = None, wait_s: int = 0, applied: Optional[dict] = None,
              name: str = "Probe", timeout: float = 70, answer: Optional[dict] = None) -> tuple:
    fleet, _net = _fleet()
    if "|" not in token:
        token = "m-000000000000|" + token  # a machine nobody linked, with a made-up pass
    return wire("POST", port, "/v1/sync", {"proto": fleet.PROTO, "have": have, "wait_s": wait_s,
                                           "machine": {"name": name, "platform": "linux"},
                                           "applied": applied or {}, "n": next_count()},
                token=token, timeout=timeout, answer=answer)


def wire_unlink(port: int, token: str, answer: Optional[dict] = None) -> tuple:
    fleet, _net = _fleet()
    return wire("POST", port, "/v1/unlink", {"proto": fleet.PROTO, "n": next_count()}, token=token, answer=answer)


def key_problems(obj: Any, where: str = "") -> list:
    """Keys in a list answer that the contract's allowlist does not name."""
    out = []
    if isinstance(obj, dict):
        allowed = WIRE_KEYS.get(where)
        if allowed is None:
            return [f"{where or 'answer'}: an object where the contract has none"]
        out += [f"{where or 'answer'}: key {k!r} is not on the allowlist" for k in obj if k not in allowed]
        for k, v in obj.items():
            if isinstance(v, dict):
                out += key_problems(v, k if not where else f"{where}.{k}")
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        out += key_problems(item, f"{k}[]")
                    elif isinstance(item, (list,)):
                        out.append(f"{k}[]: a nested list")
    return out


def cpu_seconds(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    parts = out.replace("-", ":").split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0.0
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


# ---- the contract's shapes (the same rules as tests/test_api_contract.py, which needs FastAPI to import) -----------
_SHAPES = json.loads((ROOT / "tests" / "contract" / "api_shapes.json").read_text(encoding="utf-8"))["shapes"]
_BASIC = {"str": lambda v: isinstance(v, str), "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
          "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
          "bool": lambda v: isinstance(v, bool), "null": lambda v: v is None, "dict": lambda v: isinstance(v, dict),
          "list": lambda v: isinstance(v, list), "any": lambda v: True}


def _type_problems(value: Any, typ: str, path: str) -> list:
    found = []
    for t in [x.strip() for x in typ.split("|")]:
        if t in _BASIC:
            probs = [] if _BASIC[t](value) else [f"{path}: expected {t}"]
        elif t.startswith("list["):
            probs = [f"{path}: expected a list"] if not isinstance(value, list) else \
                [p for i, v in enumerate(value) for p in _type_problems(v, t[5:-1], f"{path}[{i}]")]
        else:
            probs = shape_problems(value, t, path)
        if not probs:
            return []
        found.append(probs)
    return min(found, key=len)


def shape_problems(value: Any, name: str, path: str = "") -> list:
    """Where `value` does not fit the named shape of tests/contract/api_shapes.json (empty when it fits)."""
    spec = _SHAPES[name]
    path = path or name
    if not isinstance(value, dict):
        return [f"{path}: expected {name}"]
    required, optional = {**spec.get("required", {}), **spec.get("backend", {})}, spec.get("optional", {})
    out = []
    for key, typ in required.items():
        out += [f"{path}: missing key {key!r}"] if key not in value else _type_problems(value[key], typ, f"{path}.{key}")
    for key, typ in optional.items():
        if key in value:
            out += _type_problems(value[key], typ, f"{path}.{key}")
    if spec.get("closed"):
        extra = sorted(set(value) - set(required) - set(optional))
        if extra:
            out.append(f"{path}: keys not in the contract: {extra}")
    return out
