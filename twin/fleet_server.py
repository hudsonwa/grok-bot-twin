"""The share server: how the machine with the Grok app hands its twin list to the owner's other machines.

Standard library only (http.server). What it does and does not do:

- Listens on ONE address: the Tailscale address when Tailscale is up, else the private home-network address
  (fleet_net.share_address). Never all interfaces, never a public address. Port 18777 unless the owner chose
  another. A request from an address outside those ranges is dropped before it is read.
- Four small JSON routes (schema grok-bot-twin.share.v1): hello, link (two messages in which both sides prove they
  know the one-time link code, without sending it or anything it could be worked out from), sync
  (signed with that machine's key; held until the list changes), unlink. Plus `poke`, which only another Hermes
  process on this same machine can use (it needs a secret from the owner-only state file).
- Idle cost: the accept loop sleeps in the operating system until a connection arrives, and a waiting sync
  request sleeps on a condition until the list changes. No timer, no polling.
- It never shows or logs what a request carried, answers errors as a code without text, caps every request at
  16 KiB, gives a whole request 20 s to arrive, serves at most 32 requests at a time and holds at most 64
  connections (8 from one address).
- Nothing secret is sent in either direction: linking is a proof of knowing the code, and every later request
  and answer is signed (HMAC-SHA256) with a key both sides worked out at linking.

Only one Hermes process per machine can hold the port. The others notice (the port is taken and `poke` is
answered) and leave the serving to it; when it exits, another takes over at its next background check.
"""
from __future__ import annotations

import contextlib
import contextvars
import errno
import hmac
import http.client
import json
import logging
import os
import re
import secrets
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator, Optional

from . import config, fleet, fleet_items, fleet_net as net, routines, store, tasks_hub
from . import xs_memory  # a twin's memory between the owner's computers: one sealed route of its own
from . import xs_config
from .model import now_iso, parse_iso

log = logging.getLogger(__name__)

BODY_CAP = 16 * 1024
MAX_WAIT_S = 50
MAX_OPEN = 32
MAX_CONNECTIONS = 64  # open sockets, counting those that have not sent a request yet
MAX_PER_ADDRESS = 8  # of those, from one address (a linked machine needs one, two for a moment)
REQUEST_READ_S = 20  # for a whole request to arrive, headers and body
READ_TIMEOUT_S = 20
SEEN_WRITE_EVERY_S = 30

_lock = threading.RLock()
_server: Optional["ShareServer"] = None
_problem: Optional[str] = None  # why this process could not start serving (a contract code), if it tried
_other: tuple = (0.0, False)  # (when checked, another process on this machine serves)


def _err(status: int, code: str) -> tuple:
    return status, {"schema": fleet.SCHEMA, "error": {"code": code}}


# ---- the four routes, as functions of the state ----------------------------------------------------------------------
class Asked:
    """What a signed request carried: which machine, its nonce, its signature, the path and the raw body."""

    def __init__(self, machine_id: str, nonce: str, mac: str, path: str, raw: bytes) -> None:
        self.machine_id, self.nonce, self.mac, self.path, self.raw = machine_id, nonce, mac, path, raw

    def by(self, st: dict) -> tuple:
        """(the linked machine's record, its key) when the signature fits; (None, key) for a machine that was
        unlinked here and signed with its old key; (None, None) otherwise."""
        f, key = fleet.follower_key(st, self.machine_id)
        if key is None or not fleet._hex_ok(self.nonce, 32) or not fleet._hex_ok(self.mac, 64):
            return None, None
        if not fleet.same(fleet.request_mac(key, self.path, self.nonce, self.raw), self.mac):
            return None, None
        return f, key


def wire_hello() -> tuple:
    st = fleet.load()
    return 200, {"schema": fleet.SCHEMA, "app": "grok-bot-twin", "proto": fleet.PROTO,
                 "sharing": bool((st.get("share") or {}).get("on"))}, None


LINK_STEP_S = 30   # the new machine's proof must follow its first message within this
MAX_LINKING = 32   # exchanges under way at once (each started one uses up a try of the code, so five per code)
_linking: dict = {}  # (machine id, its nonce) -> what the source expects next
_linking_lock = threading.Lock()


def wire_link(svc: Any, body: dict) -> tuple:
    """A new machine and this one prove to each other that they know the link code, without sending it or anything
    it could be worked out from (fleet.py, "keys and signatures"). Two messages on the same route:

    1. {"machine", "nonce", "dh"}      -> {"nonce", "dh", "proof", "source"}   one try of the code is used
    2. {"machine", "nonce", "confirm"} -> {"linked": true}, signed with the new machine's key

    The machine is linked, and the code used up, only by the second message."""
    if body.get("proto") != fleet.PROTO:
        return _err(426, "version_mismatch") + (None,)
    machine = fleet.clean_machine(body.get("machine"))
    nonce = body.get("nonce")
    if machine is None or not fleet._hex_ok(nonce, 32):
        return _err(400, "bad_request") + (None,)
    if "confirm" in body:
        return _link_confirm(svc, machine, nonce, body.get("confirm"))
    public = body.get("dh")
    if fleet.dh_value(public) is None:
        return _err(400, "bad_request") + (None,)
    with fleet.locked():
        st = fleet.load()
        sh = st.get("share")
        if not sh or not sh.get("on"):
            return _err(503, "not_sharing") + (None,)
        if machine["id"] == st["machine"]["id"]:
            return _err(409, "own_code") + (None,)
        if machine["id"] not in sh["followers"] and len(sh["followers"]) >= fleet.MAX_MACHINES:
            return _err(409, "fleet_full") + (None,)
        problem, code_key = fleet.code_for_link(st, machine["id"])
        fleet.save(st)  # (a try was used, or a code that ran out was put away)
        if problem:
            return _err(403, problem) + (None,)
        me = fleet.this_machine(st)
    secret, source_public = fleet.dh_new(fleet.link_base(code_key))
    shared = fleet.dh_shared(secret, public)
    if shared is None or source_public == public:
        return _err(400, "bad_request") + (None,)
    source_nonce = fleet.new_nonce()
    proof, confirm, pass_ = fleet.link_keys(shared, nonce, source_nonce, machine["id"], me["id"], public,
                                            source_public)
    now = time.monotonic()
    with _linking_lock:
        for k in [k for k, v in _linking.items() if now - v["at"] > LINK_STEP_S]:
            del _linking[k]
        while len(_linking) >= MAX_LINKING:
            del _linking[min(_linking, key=lambda k: _linking[k]["at"])]
        _linking[(machine["id"], nonce)] = {"at": now, "machine": machine, "confirm": confirm, "pass": pass_,
                                            "code_key": code_key.hex()}
    return 200, {"schema": fleet.SCHEMA, "proto": fleet.PROTO, "nonce": source_nonce, "dh": source_public,
                 "proof": proof, "source": {"id": me["id"], "name": me["name"], "platform": me["platform"]}}, None


def _link_confirm(svc: Any, machine: dict, nonce: str, confirm: Any) -> tuple:
    with _linking_lock:
        step = _linking.pop((machine["id"], nonce), None)  # (one proof per exchange, right or wrong)
    if step is None or time.monotonic() - step["at"] > LINK_STEP_S:
        return _err(400, "bad_request") + (None,)
    machine = step["machine"]
    with fleet.locked():
        st = fleet.load()
        sh = st.get("share")
        if not sh or not sh.get("on"):
            return _err(503, "not_sharing") + (None,)
        if not fleet.same(confirm, step["confirm"]):
            problem = fleet.code_failed(st)
            fleet.save(st)
            return _err(403, problem) + (None,)
        if machine["id"] not in sh["followers"] and len(sh["followers"]) >= fleet.MAX_MACHINES:
            return _err(409, "fleet_full") + (None,)
        if not fleet.code_spent(st, step["code_key"], machine["id"]):  # another was quicker, or it ran out
            problem = fleet.no_code_reason(st) if not st["share"].get("code") else "code_expired"
            fleet.save(st)
            return _err(403, problem) + (None,)
        now = now_iso()
        if isinstance(sh.get("gone"), dict):
            sh["gone"].pop(machine["id"], None)
        key = fleet.share_key(step["pass"])
        sh["followers"][machine["id"]] = {**machine, "key": key.hex(), "linked_at": now,
                                          "last_seen_at": now, "applied": {}}
        fleet.rebuild_list(svc, st)
        fleet.save(st)
    fleet.tend_source_twins(svc, st)
    fleet.announce()
    svc.emit("fleet", None, {"what": "machine_linked", "count": 1})
    return 200, {"schema": fleet.SCHEMA, "proto": fleet.PROTO, "linked": True}, (key, nonce)


def _clean_applied(raw: Any) -> dict:
    """What a follower reports about itself, reduced to ids, numbers and the fixed failure codes."""
    if not isinstance(raw, dict):
        return {}
    rev = raw.get("rev") if isinstance(raw.get("rev"), str) and fleet.REV_RE.match(raw["rev"]) else None
    at = raw.get("at") if isinstance(raw.get("at"), str) and len(raw["at"]) <= 40 and parse_iso(raw["at"]) else None
    twins, failed = {}, {}
    for tid, ver in list((raw.get("twins") if isinstance(raw.get("twins"), dict) else {}).items())[:fleet.MAX_TWINS]:
        if isinstance(tid, str) and fleet.REF_RE.match(tid) and isinstance(ver, int) and not isinstance(ver, bool) \
                and 0 < ver <= 10 ** 9:
            twins[tid] = ver
    for tid, code in list((raw.get("failed") if isinstance(raw.get("failed"), dict) else {}).items())[:fleet.MAX_TWINS]:
        if isinstance(tid, str) and fleet.REF_RE.match(tid) and code in fleet.FAIL_CODES:
            failed[tid] = code
    return {"rev": rev, "at": at, "twins": twins, "failed": failed}


def _note_seen(asked: Asked, body: Optional[dict]) -> tuple:
    """Record that a linked machine checked in (and what it reports), writing the state file only when its report
    changed or the last note is older than half a minute. Returns (state, record, key); record is None when the
    request is not from a linked machine (key is then the old key of a machine unlinked here, or None)."""
    st = fleet.load()
    f, key = asked.by(st)
    if f is None:
        return st, None, key
    applied = _clean_applied(body.get("applied")) if body is not None else f.get("applied")
    machine = body.get("machine") if body is not None and isinstance(body.get("machine"), dict) else {}
    name = fleet.fp.machine_name(machine.get("name"), f.get("name") or "computer") if machine else f.get("name")
    platform = machine.get("platform") if machine.get("platform") in fleet.PLATFORMS else f.get("platform")
    last = parse_iso(f.get("last_seen_at"))
    stale = last is None or (fleet.now_dt() - last).total_seconds() > SEEN_WRITE_EVERY_S
    if applied == f.get("applied") and name == f.get("name") and platform == f.get("platform") and not stale:
        return st, f, key
    renamed = name != f.get("name") or platform != f.get("platform")
    with fleet.locked():
        st = fleet.load()
        f, key = asked.by(st)
        if f is None:
            return st, None, key
        f.update(applied=applied, name=name, platform=platform, last_seen_at=now_iso())
        if applied.get("rev") == st["share"]["list"]["rev"] and not applied.get("failed"):
            # when it was last seen holding the current list, by THIS computer's clock: the time the other one
            # reports is by its own clock, which may be hours off
            f["in_sync_at"] = f["last_seen_at"]
        changed = renamed and _rebuild(st)
        fleet.save(st)
    if changed:
        from .service import get_service
        fleet.tend_source_twins(get_service(), st)
        fleet.announce()
    return st, f, key


def _rebuild(st: dict) -> bool:
    from .service import get_service
    return fleet.rebuild_list(get_service(), st)


def _unlinked(key: Optional[bytes], asked: Asked) -> tuple:
    """"You are not linked (any more)". Signed when this source still knows the key that machine used, so that
    only the real source can make a machine take its twins away; unsigned (and then not acted on) otherwise."""
    return _err(401, "unlinked") + ((key, asked.nonce) if key is not None else None,)


def _replayed(asked: Asked, body: dict) -> Optional[tuple]:
    """A signed request counts once. The answer for a recording of one that is sent again (by someone on the
    network who has no key: it could otherwise put back a computer's old name and old report, or make a computer
    that is off look as if it had just checked in); None for a request seen for the first time, and for one
    that is not signed by a linked computer at all (that is refused where it always was).

    Two things must hold. The request's random number is new (kept in memory; cheap, and enough while this Hermes
    runs). And its call number "n", which is inside the signed body, is higher than the highest this computer
    has had from that linked computer. That one is kept in the state file, so a recording stays worthless after
    this Hermes was started again and however old it is; these calls carry no time, because a linked computer
    whose clock is wrong must still be able to follow. The refusal is signed and says the number had ("had"),
    so the real computer (after its clock was set back, say) can go on above it; a recording cannot."""
    f, key = asked.by(fleet.load())
    if f is None:
        return None
    signed = (key, asked.nonce)
    n = fleet.count_of(body.get("n"))
    if n is None:
        return _err(400, "bad_request") + (signed,)
    if not tasks_hub.fresh(f["id"], asked.nonce):
        return _err(409, "replayed") + (signed,)
    with fleet.locked():
        st = fleet.load()
        f, key = asked.by(st)
        if f is None:
            return None
        had = fleet.count_of(f.get("n")) or 0
        if n <= had:
            status, obj = _err(409, "replayed")
            return status, {**obj, "had": had}, signed
        f["n"] = n
        fleet.save(st)
    return None


def wire_sync(asked: Asked, body: dict) -> tuple:
    if body.get("proto") != fleet.PROTO:
        return _err(426, "version_mismatch") + (None,)
    again = _replayed(asked, body)
    if again is not None:
        return again
    seq = fleet.hub.seq  # read before the state, so a change in between is not slept through
    st, f, key = _note_seen(asked, body)
    if f is None:
        return _unlinked(key, asked)
    signed = (key, asked.nonce)
    if "routines" in body:  # (optional key: what that computer reports about the routines it holds)
        routines.heard(asked.machine_id, body.get("routines"))
    have = body.get("have") if isinstance(body.get("have"), str) else None
    wait_s = body.get("wait_s")
    wait_s = min(MAX_WAIT_S, max(0, wait_s)) if isinstance(wait_s, (int, float)) and not isinstance(wait_s, bool) else 0
    deadline = time.monotonic() + wait_s
    while True:
        sh = st["share"]
        rev = sh["list"]["rev"]
        if not sh.get("on"):  # the owner switched sharing off: the waiting machine is told so
            off = {"schema": fleet.SCHEMA, "proto": fleet.PROTO, "changed": False, "sharing": False, "rev": rev}
            if sh.get("leaving") is True:
                # "Remove Grok Bot Twin" is under way here: switching sharing on again will never bring this
                # link back, and the linked computer must not promise that (optional key; older builds ignore it)
                off["removed"] = True
            return 200, off, signed
        if have != rev:
            return 200, fleet.wire_list(st), signed
        # settings that follow a twin (xs_config.py): an optional key in every "nothing changed" answer, and only
        # for a computer that said it takes them. The twin list above carries nothing new
        xs, xs_new = xs_config.sync_extra(body, st, asked.machine_id)
        # an answer of "nothing changed" also says which of the owner's other computers take requests, as far as
        # this one knows (`takes_requests`, an optional key: a computer on an older build ignores it). The twin
        # list itself carries nothing new: its allowlist stands
        # further items (skills, ...): said only to a computer that named them in its request, in every "nothing
        # changed" answer, and a waiting request is answered at once when this computer's rev of one is not the
        # rev that computer holds
        named = fleet_items.answer(body)
        # (settings and those items share the one key "xs": both go into one object, neither overwrites the other.
        # `named` alone decides the registry's at-once answer; settings keep their own limit, `xs_new`)
        both = {**(named.get(fleet_items.KEY) or {}), **(xs.get("xs") or {})}
        items = {fleet_items.KEY: both} if both else {}
        missed = tasks_hub.hub.take_missed(asked.machine_id)
        if missed:  # it was asked to do something while its switch was off: it hears who asked and when
            _note_seen(asked, None)
            return 200, {"schema": fleet.SCHEMA, "proto": fleet.PROTO, "changed": False, "sharing": True, "rev": rev,
                         "asked_while_off": missed, **tasks_hub.hub.tell(asked.machine_id, st), **items,
                         **routines.tell(body)}, signed
        news = tasks_hub.hub.news(asked.machine_id, st)
        if news:  # a computer switched requests on or off since this one was last told
            _note_seen(asked, None)
            return 200, {"schema": fleet.SCHEMA, "proto": fleet.PROTO, "changed": False, "sharing": True,
                         "rev": rev, **news, **items, **routines.tell(body)}, signed
        rnews = routines.news(body)
        if rnews:  # that computer speaks routines and holds another list of them than the current one
            _note_seen(asked, None)
            return 200, {"schema": fleet.SCHEMA, "proto": fleet.PROTO, "changed": False, "sharing": True,
                         "rev": rev, **tasks_hub.hub.tell(asked.machine_id, st), **items, **rnews}, signed
        left = deadline - time.monotonic()
        if xs_new or left <= 0 or fleet.hub.closing or fleet_items.differs(body, named):
            # (closing: this Hermes process is ending; sharing itself stays on)
            # (xs_new: its twins' settings differ from what that computer last heard, so it is told at once)
            _note_seen(asked, None)
            return 200, {"schema": fleet.SCHEMA, "proto": fleet.PROTO, "changed": False, "sharing": True,
                         "rev": rev, **tasks_hub.hub.tell(asked.machine_id, st), **items, **routines.tell(body)}, signed
        seq = fleet.hub.wait(seq, left)
        st = fleet.load()
        f, key = asked.by(st)
        if f is None or not st.get("share"):
            return _unlinked(key, asked)


def wire_unlink(svc: Any, asked: Asked, body: dict) -> tuple:
    again = _replayed(asked, body)
    if again is not None:
        return again
    with fleet.locked():
        st = fleet.load()
        f, key = asked.by(st)
        if f is None:
            return _unlinked(key, asked)
        fleet.forget_follower(st, f["id"])
        fleet.rebuild_list(svc, st)
        fleet.save(st)
    routines.peer_left(f["id"])  # (its own signed goodbye: what it ran may now run elsewhere)
    fleet.tend_source_twins(svc, st)
    fleet.announce()
    svc.emit("fleet", None, {"what": "machine_removed", "count": 1})
    return 200, {"schema": fleet.SCHEMA, "unlinked": True}, (key, asked.nonce)


# ---- HTTP ------------------------------------------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "grok-bot-twin"
    sys_version = ""
    timeout = READ_TIMEOUT_S

    def setup(self) -> None:
        super().setup()
        # one limit for the whole request (headers and body), however slowly it trickles in: after it the
        # connection is shut, which ends whatever read is waiting
        self._limit = threading.Timer(REQUEST_READ_S, self._cut)
        self._limit.daemon = True
        self._limit.start()

    def _cut(self) -> None:
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def finish(self) -> None:
        self._limit.cancel()
        with contextlib.suppress(Exception):
            super().finish()

    def log_message(self, fmt: str, *args: Any) -> None:  # nothing a request carried is ever logged
        return

    def _send(self, status: int, obj: Optional[dict], signed: Optional[tuple] = None) -> None:
        data = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            if signed is not None:  # (key, the request's nonce): only the real source can make this
                self.send_header("X-GBT-Mac", fleet.answer_mac(signed[0], signed[1], status, data))
            self.end_headers()
            if data:
                self.wfile.write(data)
        except OSError:
            pass

    def _raw_body(self) -> Optional[bytes]:
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return None
        if n < 0 or n > BODY_CAP:
            return None
        try:
            data = self.rfile.read(n) if n else b""
        except OSError:
            return None
        return data if len(data) == n else None

    @staticmethod
    def _json(raw: bytes) -> Optional[dict]:
        try:
            obj = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None
        return obj if isinstance(obj, dict) else None

    def _header(self, name: str, cap: int) -> str:
        value = self.headers.get(name) or ""
        return value.strip() if len(value) <= cap else ""

    def _route(self, method: str) -> None:
        srv: "ShareServer" = self.server.owner  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0]
        raw = b""
        if method == "POST":  # the body is read before a slot is taken: a slow sender holds no slot
            got = self._raw_body()
            if got is None:
                self._limit.cancel()
                self._send(*_err(400, "bad_request"))
                return
            raw = got
        self._limit.cancel()  # the request is in; what follows may wait (for a new list) as long as it likes
        if not srv.slots.acquire(blocking=False):
            self._send(*_err(429, "busy"))
            return
        try:
            if method == "GET":
                self._send(*(wire_hello() if path == "/v1/hello" else _err(404, "not_found") + (None,)))
                return
            if path not in ("/v1/link", "/v1/sync", "/v1/unlink", "/v1/poke", xs_config.ROUTE) + tasks_hub.ROUTES \
                    + (xs_memory.ROUTE,) and path not in fleet_items.routes:
                self._send(*_err(404, "not_found"))
                return
            body = self._json(raw)
            if body is None:
                self._send(*_err(400, "bad_request"))
                return
            # the instance secret is for Hermes processes on THIS machine. A connection from this machine to its
            # own address arrives with one of this machine's addresses as its sender, so from any other sender
            # the secret is not looked at at all: should it ever get out, it is worth nothing on the network
            own = bool(self.headers.get("Authorization")) and net.is_own_address(str(self.client_address[0]))
            if path in tasks_hub.ROUTES:  # requests between the owner's computers (tasks_hub.py)
                bearer = self._header("Authorization", 300) if own else ""
                conn = self.connection
                self._send(*tasks_hub.handle(
                    path, Asked(self._header("X-GBT-Machine", 20), self._header("X-GBT-Nonce", 40),
                                self._header("X-GBT-Mac", 80), path, raw),
                    bearer[7:].strip() if bearer.startswith("Bearer ") else "", body, srv.secret,
                    gone=lambda: hung_up(conn)))
                return
            if path == "/v1/poke":
                auth = self._header("Authorization", 300) if own else ""
                token = auth[7:].strip() if auth.startswith("Bearer ") else ""
                if not hmac.compare_digest(token.encode(), srv.secret.encode()):
                    self._send(*_err(401, "unlinked"))
                    return
                self._send(204, None)  # answered first: the caller may hold the store's lock
                if body.get("ping") is not True:  # (a ping only asks "are you there?")
                    srv.poked()
                return
            if path == "/v1/link":
                status, obj, proved = wire_link(srv.get_service(), body)
                if proved is not None:  # signed with the newly linked machine's key over its nonce
                    self._send(status, obj, proved)
                else:
                    self._send(status, obj)
                return
            asked = Asked(self._header("X-GBT-Machine", 20), self._header("X-GBT-Nonce", 40),
                          self._header("X-GBT-Mac", 80), path, raw)
            if path == xs_config.ROUTE:  # the settings that follow a twin (xs_config.py)
                self._send(*xs_config.wire(asked, body))
            elif path == "/v1/sync":
                self._send(*wire_sync(asked, body))
            elif path == xs_memory.ROUTE:  # (xs_memory.py: everything about memory is inside an encrypted box)
                self._send(*xs_memory.wire(asked, body))
            elif path in fleet_items.routes:  # further things between the owner's computers (fleet_items.py)
                self._send(*fleet_items.routes[path](asked, body))
            else:
                self._send(*wire_unlink(srv.get_service(), asked, body))
        except Exception:
            log.debug("grok-bot-twin: share request failed", exc_info=True)
            self._send(*_err(500, "internal"))
        finally:
            srv.slots.release()

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")


class _Http(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows that flag would let another program share the port
    request_queue_size = 16

    def verify_request(self, request: Any, client_address: Any) -> bool:
        return net.kind_of(client_address[0]) is not None

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.debug("grok-bot-twin: share connection failed", exc_info=True)

    def process_request(self, request: Any, client_address: Any) -> None:
        owner = self.owner  # type: ignore[attr-defined]
        peer = str(client_address[0])
        if not owner.admit(peer):  # too many open connections (in all, or from this address): closed unread
            self.shutdown_request(request)
            return
        ctx = owner.ctx.copy()  # each thread runs in its own copy of Hermes' context

        def serve() -> None:
            try:
                self.process_request_thread(request, client_address)
            finally:
                owner.leave(peer)

        threading.Thread(target=ctx.run, args=(serve,), name="grok-bot-twin-share-request", daemon=True).start()


class ShareServer:
    """One listening socket and its accept thread."""

    def __init__(self, address: str, port: int, kind: str, get_service: Callable) -> None:
        self.address, self.port, self.kind, self.get_service = address, port, kind, get_service
        self.secret = secrets.token_urlsafe(24)
        self.slots = threading.BoundedSemaphore(MAX_OPEN)
        self._peers: dict = {}
        self._peers_lock = threading.Lock()
        self.ctx = contextvars.copy_context()
        self._stopping = False
        self.http = _Http((address, port), _Handler)  # raises OSError when the address or port cannot be had
        self.http.owner = self  # type: ignore[attr-defined]
        self.port = self.http.server_address[1]
        self.thread = threading.Thread(target=self.ctx.copy().run, args=(self._serve,), name="grok-bot-twin-share",
                                       daemon=True)
        self.thread.start()

    def admit(self, peer: str) -> bool:
        with self._peers_lock:
            if sum(self._peers.values()) >= MAX_CONNECTIONS or self._peers.get(peer, 0) >= MAX_PER_ADDRESS:
                return False
            self._peers[peer] = self._peers.get(peer, 0) + 1
            return True

    def leave(self, peer: str) -> None:
        with self._peers_lock:
            n = self._peers.get(peer, 0) - 1
            if n > 0:
                self._peers[peer] = n
            else:
                self._peers.pop(peer, None)

    def here(self) -> bool:
        """Is the address this server listens on still one of this machine's? (A home network may hand out a new
        one; a socket bound to the old one hears nothing.)"""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((self.address, 0))
            return True
        except OSError as exc:
            return exc.errno != errno.EADDRNOTAVAIL and getattr(exc, "winerror", None) != 10049
        finally:
            probe.close()

    def _serve(self) -> None:
        while not self._stopping:
            try:
                self.http.handle_request()  # sleeps in the operating system until a connection arrives
            except Exception:
                if self._stopping:
                    break
                log.debug("grok-bot-twin: share accept failed", exc_info=True)
                time.sleep(1)

    def poked(self) -> None:
        """Another Hermes process on this machine changed the twin list or the settings."""
        try:
            st = fleet.load()
            if not (st.get("share") or {}).get("on") or st.get("link"):
                fleet.hub.bump()  # sharing was switched off in another process: stop listening
                threading.Thread(target=ensure, args=(self.get_service,), name="grok-bot-twin-share-stop",
                                 daemon=True).start()
                return
            tasks_hub.forget_unlinked(st)  # (another process may have removed a computer)
            if not fleet.publish(self.get_service()):
                fleet.hub.bump()
        except Exception:
            fleet.hub.bump()

    def alive(self) -> bool:
        return self.thread.is_alive() and not self._stopping

    def stop(self) -> None:
        self._stopping = True
        tasks_hub.reset()  # requests in flight end as "stopped"
        fleet.hub.closing = True
        fleet.hub.bump()  # waiting machines are told that sharing is being switched off
        time.sleep(0.2)
        with contextlib.suppress(OSError):  # wake the accept loop
            socket.create_connection((self.address, self.port), timeout=1).close()
        self.thread.join(3)
        with contextlib.suppress(Exception):
            self.http.server_close()
        fleet.hub.closing = False


def hung_up(sock: Any) -> bool:
    """Has the other side closed this connection? Looked at without reading anything: a caller that waits for
    its answer has sent all it had to send, so the only thing that can arrive is the end. (A request must not be
    handed to a computer whose program has just ended: it would be lost, and its asker left waiting.)"""
    try:
        readable, _w, _x = select.select([sock], [], [], 0)
        return bool(readable) and sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


# ---- which process serves ----------------------------------------------------------------------------------------------
def _poke(inst: Any, timeout: float = 1.5, ping: bool = False) -> bool:
    """Ask the serving process on this machine to read the state again (`ping`: only whether it is there). True
    when it answered as ours."""
    if not isinstance(inst, dict) or not net.kind_of(inst.get("address")) or not isinstance(inst.get("port"), int) \
            or not isinstance(inst.get("secret"), str):
        return False
    if not net.is_own_address(inst["address"]):
        return False  # this machine moved to another network: whoever has that address now is not asked
    conn = http.client.HTTPConnection(inst["address"], inst["port"], timeout=timeout)
    try:
        conn.request("POST", "/v1/poke", body=b'{"ping": true}' if ping else b"{}", headers={"Authorization": "Bearer " + inst["secret"],
                                                              "Content-Type": "application/json"})
        return conn.getresponse().status == 204
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def other_serving(st: Optional[dict] = None, fresh: bool = False) -> bool:
    """Is another Hermes process on this machine serving? (asked at most every 5 s)"""
    global _other
    if not fresh and time.monotonic() - _other[0] < 5:
        return _other[1]
    st = st or fleet.load()
    inst = (st.get("share") or {}).get("instance")
    ok = bool(inst) and inst.get("pid") != os.getpid() and _poke(inst, ping=True)
    _other = (time.monotonic(), ok)
    return ok


def _tell_server() -> None:
    """Run after this process changed the list: when another process serves, poke it. Takes no lock of this
    module: the caller may hold the store's lock, which `ensure` takes while holding this module's."""
    srv = _server
    if srv is not None and srv.alive():
        return
    st = fleet.load()
    sh = st.get("share")
    if sh and isinstance(sh.get("instance"), dict) and sh["instance"].get("pid") != os.getpid():
        _poke(sh["instance"], timeout=1.0)  # answered at once by the serving process (it works after answering)


def serving() -> Optional["ShareServer"]:
    srv = _server  # (read without the lock, see _tell_server)
    return srv if srv is not None and srv.alive() else None


def problem() -> Optional[str]:
    return _problem


def _port() -> int:
    try:
        port = int(config.get("share_port"))
    except (TypeError, ValueError):
        port = net.DEFAULT_PORT
    return port if 0 <= port <= 65535 else net.DEFAULT_PORT


_one_shot = 0  # > 0 while a terminal command that ends in a moment runs in this process (see one_shot)


@contextlib.contextmanager
def one_shot() -> Iterator[None]:
    """While a short terminal command runs (`hermes twin fleet`, `share on`, `share code` ...), this process must
    not become the one that serves: it would take the share port for the fraction of a second it lives and leave
    it closed when it ends, and a link code typed on the other computer in that gap answers "could not reach"
    (the long-running Hermes gets the port back only seconds later). Inside this block `ensure` only
    checks that the port could be taken; the process that stays (Hermes itself, or `hermes twin fleet run`) takes
    it at its next look."""
    global _one_shot
    _one_shot += 1
    try:
        yield
    finally:
        _one_shot -= 1


def _port_free(address: str, port: int) -> bool:
    """Could a share server be bound to this address and port right now? (bound and released at once; nothing
    ever listens on the probe)"""
    probe = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as the server itself binds
        probe.bind((address, port))
        return True
    finally:
        probe.close()


def ensure(get_service: Callable) -> Optional[str]:
    """Make this process's server match the sharing switch: start it when sharing is on and nobody serves, stop
    it when sharing is off. Returns the contract code of what stands in the way (no_network, port_busy), else
    None. Never raises. Cheap when there is nothing to do (one small file read)."""
    global _server, _problem, _other
    with _lock:
        try:
            if not fleet.exists():
                want = False
                st = None
            else:
                st = fleet.load()
                sh = st.get("share")
                want = bool(sh and sh.get("on") and not st.get("link"))
            if not want:
                if _server is not None:
                    _server.stop()
                    _server = None
                    _forget_instance()
                _problem = None
                return None
            if _server is not None and _server.alive() and _server.here():
                _problem = None
                return None
            if _server is not None:  # its thread ended, or its address is no longer this machine's
                _server.stop()
                _forget_instance()
            _server = None
            if not _one_shot and other_serving(st, fresh=True):
                # Another Hermes process on this computer serves already, and answers. This one must not open a
                # second server beside it, on whatever port its own settings name (the Hermes of a twin's own
                # bot can carry other settings than the main one: a second server on the default
                # port took the record over, and the linked computers and this computer's own commands then
                # talked to two different servers).
                _problem = None
                return None
            sh = st["share"]
            port = _port()
            cands = []
            configured = str(config.get("share_address") or "")
            if configured:
                found = net.share_address(configured)
                cands = [found] if found else []
            else:
                if net.kind_of(sh.get("address")):  # the address the linked machines know, while it still works
                    cands.append((sh["address"], net.kind_of(sh["address"])))
                auto = net.share_address("")
                if auto and auto not in cands:
                    cands.append(auto)
            if not cands:
                _problem = "no_network"
                return _problem
            last = "no_network"
            for address, kind in cands:
                try:
                    if _one_shot:  # a command that ends in a moment: say what stands in the way, serve nothing
                        _port_free(address, port)
                        if (st.get("share") or {}).get("address") != address:
                            with fleet.locked():
                                st2 = fleet.load()
                                if st2.get("share"):
                                    st2["share"]["address"] = address  # what a new link code carries
                                    fleet.save(st2)
                        _problem = None
                        return None
                    srv = ShareServer(address, port, kind, get_service)
                except OSError as exc:
                    if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1), 10048):
                        if other_serving(st, fresh=True):
                            _problem = None
                            return None
                        last = "port_busy"
                    continue
                _server = srv
                _other = (0.0, False)
                with fleet.locked():
                    st2 = fleet.load()
                    if st2.get("share"):
                        st2["share"]["address"] = address
                        st2["share"]["instance"] = {"address": address, "port": srv.port, "kind": kind,
                                                    "secret": srv.secret, "pid": os.getpid(), "at": now_iso()}
                        fleet.rebuild_list(get_service(), st2)
                        fleet.save(st2)
                fleet.hub.bump()
                _problem = None
                return None
            _problem = last
            return _problem
        except Exception:
            log.debug("grok-bot-twin: could not start sharing", exc_info=True)
            _problem = _problem or "no_network"
            return _problem


def _forget_instance() -> None:
    with contextlib.suppress(Exception):
        with fleet.locked():
            if not fleet.exists():
                return
            st = fleet.load()
            sh = st.get("share")
            if sh and isinstance(sh.get("instance"), dict) and sh["instance"].get("pid") == os.getpid():
                sh["instance"] = None
                fleet.save(st)


def stop() -> None:
    """Stop serving in this process (the sharing switch is left as it is)."""
    global _server
    with _lock:
        if _server is not None:
            _server.stop()
            _server = None
            _forget_instance()


def state_view(st: dict) -> dict:
    """The `share` object of the local API."""
    sh = st.get("share")
    port = _port()
    if not sh or not sh.get("on") or st.get("link"):
        return {"on": False, "state": "off", "network": None, "address": None, "port": port, "since": None,
                "problem": None, "code": None}
    srv = serving()
    inst = sh.get("instance") if isinstance(sh.get("instance"), dict) else None
    out = {"on": True, "state": "starting", "network": None, "address": None, "port": port,
           "since": sh.get("since"), "problem": None, "code": fleet.code_view(st)}
    if srv is not None:
        out.update(state="on", network=srv.kind, address=srv.address, port=srv.port)
    elif inst and other_serving(st):
        out.update(state="on", network=inst.get("kind") if inst.get("kind") in ("tailscale", "lan", "loopback")
                   else None, address=inst.get("address"), port=inst.get("port"))
    elif _problem:
        out.update(state="error", problem=fleet.message(_problem, port=port, program=holder_name(port)
                                                        if _problem == "port_busy" else ""))
    return out


# ---- which program is in the way ------------------------------------------------------------------------------------
_holder: tuple = (0.0, -1, "")  # (when asked, port, name): asked of the system at most once a minute
_NAME_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,39}")
_NOT_A_NAME = {"", "node", "java", "ruby", "perl", "php", "deno", "bun", "sh", "bash", "zsh", "fish", "pwsh",
               "powershell", "cmd", "conhost", "svchost", "system", "launchd", "systemd", "hermes", "uv", "docker"}


def _run_quiet(argv: list) -> str:
    """One fixed system command with a fixed argument list (no shell), 3 seconds at most; "" on any failure."""
    import subprocess
    try:
        kwargs: dict = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=3, **kwargs)
        return done.stdout.decode("utf-8", "replace")[:200_000] if done.returncode == 0 else ""
    except Exception:
        return ""


def _clean_program(name: Any) -> str:
    """A program's name as the system gave it, fit to show: plain characters only, 40 at most, without a folder
    or ".exe"; "" when nothing plain is left (the sentence then says "Another program")."""
    if not isinstance(name, str):
        return ""
    name = name.strip().replace("\\x20", " ").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    m = _NAME_OK.fullmatch(name.strip())
    name = m.group(0).strip() if m else ""
    # a language's own runner or a part of the system says nothing to the owner ("Quit Python"?), and may be
    # Hermes itself: then the plain sentence is the better one
    plain = re.sub(r"[\d.]+$", "", name.lower())
    return "" if plain in _NOT_A_NAME or plain.startswith("python") else name


def _ask_holder(port: int) -> str:
    """Ask the system which program listens on this TCP port. It names only programs of this account (another
    account's are not shown to it), reads nothing else and changes nothing."""
    if os.name == "nt":
        from . import osflags as _osflags
        system = _osflags.windows_system_folder()
        pid = ""
        for line in _run_quiet([os.path.join(system, "netstat.exe"), "-ano", "-p", "TCP"]).splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "TCP" and parts[1].endswith(f":{port}") and parts[3] == "LISTENING" \
                    and parts[4].isdigit():
                pid = parts[4]
                break
        if not pid:
            return ""
        out = _run_quiet([os.path.join(system, "tasklist.exe"), "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
        first = out.strip().splitlines()[0] if out.strip() else ""
        return _clean_program(first.split(",")[0].strip('"')) if first.startswith('"') else ""
    for tool in ("/usr/sbin/lsof", "/usr/bin/lsof"):
        if os.path.isfile(tool):
            for line in _run_quiet([tool, "+c", "0", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fc"]).splitlines():
                if line.startswith("c"):
                    return _clean_program(line[1:])
            return ""
    for tool in ("/usr/bin/ss", "/usr/sbin/ss", "/sbin/ss", "/bin/ss"):
        if os.path.isfile(tool):
            m = re.search(r'users:\(\("([^"]{1,64})"', _run_quiet([tool, "-H", "-ltnp", f"sport = :{port}"]))
            return _clean_program(m.group(1)) if m else ""
    return ""


def holder_name(port: int) -> str:
    """The name of the program that holds the share port, for "NAME on this computer is in the way of sharing";
    "" when the system does not say. Remembered for a minute."""
    global _holder
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port <= 65535:
        return ""
    if _holder[1] == port and time.monotonic() - _holder[0] < 60:
        return _holder[2]
    name = _ask_holder(port)
    _holder = (time.monotonic(), port, name)
    return name


def _clear_mailbox() -> None:
    if serving() is not None:
        tasks_hub.forget_unlinked()


fleet.on_change(_tell_server)  # every process tells the serving one when it changed the list
fleet.on_change(_clear_mailbox)  # a computer that was unlinked or removed has nothing in the mailbox any more
