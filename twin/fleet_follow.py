"""The follower: how a machine without the Grok app keeps its twins in line with the machine that has it.

Standard library only (http.client). Outbound connections only, and only to a Tailscale, home-network or
loopback address (fleet_net.kind_of): the address the link code carries, or one the owner typed.

- `link`: the two machines prove to each other that they know the one-time link code, without sending it or
  anything it could be worked out from (two messages; fleet.py, "keys and signatures"), and both then work out
  this machine's pass. Nothing secret crosses the wire, and a machine that does not prove it is not linked to.
- Every later request is signed, and an answer counts only when it carries the source's signature over that
  request: another machine at the source's address can neither read the list nor tell this one "you are
  unlinked" nor hand it a list.
- `sync_once`: one request. The source holds it (up to 50 s) until its list changes, so a change arrives in
  about a second and an idle link costs one small request a minute. What comes back is checked, cleaned and
  capped again here (fleet.clean_list) before any file is written.
- The loop: one request after another. When the source cannot be reached the twins stay as they are, the state
  says since when, and the next try comes after 5 s, then 10 and from then on every 15 s (one refused
  connection: next to nothing), so it is back in step within half a minute of the source returning. Only one Hermes process on a
  machine follows at a time (a lock file); the others leave it to that one.
- `unlink`: this machine's twins are taken away (kept 30 days) and the source is told when it can be reached.

Nothing the source sends is ever shown as a message or run; errors travel as codes and are put into this
machine's own words.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import logging
import select
import socket
import threading
import time
from typing import Any, Callable, Optional

from . import config, fleet, fleet_io as fio, fleet_items, fleet_net as net, fleet_profiles as fp, store
from .model import now_dt, now_iso, parse_iso, plural
from . import xs_config

log = logging.getLogger(__name__)

ANSWER_CAP = 2 * 1024 * 1024
LONG_WAIT_S = 50
MIN_BACKOFF_S = 5.0
# 15 s: "back in sync" shows within half a minute of the source returning, its own start-up included (a longer
# gap between tries can take it past half a minute)
MAX_BACKOFF_S = 15.0
IDLE_CHECK_S = 5.0
CONNECT_TIMEOUT_S = 6.0
CONTACT_WRITE_EVERY_S = 60
FORGOTTEN_AFTER = 3  # answers in a row of "I do not know you" before this machine says so (see sync_once)
SLEPT_SLACK_S = 20  # a request that took this much longer than it is ever allowed: this machine was asleep
_wall = time.time  # (the clock on the wall: it moves on while the machine sleeps or the process is held)
LINK_ERRORS = ("code_wrong", "code_expired", "code_used", "too_many_tries", "own_code", "fleet_full",
               "version_mismatch", "not_sharing")

_stop = threading.Event()
_wake = threading.Event()
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
_conn: list = []  # the loop's open connection, so stop() can end a waiting request at once
_aborting = threading.Event()  # set by _abort(): the loop's waiting request ends within ABORT_LOOK_S
ABORT_LOOK_S = 0.5
_state = {"backoff": MIN_BACKOFF_S, "holding": False}


class Unreachable(Exception):
    """No usable answer from the other machine (not there, not ours, or not understood)."""


def _request(host: str, port: int, path: str, body: dict, key: Optional[bytes] = None, machine_id: str = "",
             timeout: float = 10.0, holder: Optional[list] = None, proof_key: Optional[bytes] = None,
             nonce: Optional[str] = None) -> tuple:
    """POST a small JSON body; returns (HTTP status, JSON object, signed). With `key` the request is signed and
    `signed` says whether the answer carries the source's signature over this request's nonce (with `proof_key`:
    the key just made, for the last answer of the linking). No secret is put on the wire. Raises Unreachable for everything that is not
    an answer in the share schema, and when the whole answer has not arrived in time."""
    if not net.kind_of(host) or not isinstance(port, int) or not 0 < port < 65536:
        raise Unreachable()
    deadline = time.monotonic() + timeout
    cut: Optional[threading.Timer] = None
    conn = http.client.HTTPConnection(host, port, timeout=min(timeout, CONNECT_TIMEOUT_S))
    if holder is not None:
        holder[:] = [conn]
        _aborting.clear()
    try:
        data = json.dumps(body).encode("utf-8")
        nonce = nonce or fleet.new_nonce()
        headers = {"Content-Type": "application/json", "Connection": "close"}
        if key is not None:
            headers.update({"X-GBT-Machine": machine_id, "X-GBT-Nonce": nonce,
                            "X-GBT-Mac": fleet.request_mac(key, path, nonce, data)})
        conn.connect()
        sock = conn.sock  # (kept: http.client lets go of it once the answer's headers are in)
        sock.settimeout(timeout)
        # one limit for the whole exchange. The socket's own timeout counts per read, so a machine at the other
        # end that trickles its answer's first lines a byte at a time would otherwise hold this thread for good
        cut = threading.Timer(timeout + 5, _cut, args=(sock,))
        cut.daemon = True
        cut.start()
        conn.request("POST", path, body=data, headers=headers)
        if holder is not None:
            _wait_for_answer(sock, timeout)
        resp = conn.getresponse()
        chunks, total = [], 0
        while True:  # the whole answer within the time allowed, however slowly it is sent
            left = deadline - time.monotonic()
            if left <= 0:
                raise Unreachable()
            with contextlib.suppress(OSError):
                sock.settimeout(max(0.05, left))
            chunk = resp.read1(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > ANSWER_CAP:
                raise Unreachable()
        raw = b"".join(chunks)
        obj = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(obj, dict) or obj.get("schema") != fleet.SCHEMA:
            raise Unreachable()
        check = key if key is not None else proof_key
        signed = check is not None and fleet.same(resp.headers.get("X-GBT-Mac") or "",
                                                  fleet.answer_mac(check, nonce, resp.status, raw))
        return resp.status, obj, signed
    except (OSError, http.client.HTTPException, ValueError, UnicodeDecodeError, RecursionError, AttributeError):
        raise Unreachable() from None
    finally:
        if cut is not None:
            cut.cancel()
        if holder is not None:
            holder[:] = []
        conn.close()


def _cut(sock: Any) -> None:
    with contextlib.suppress(Exception):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(Exception):
        sock.close()


def _wait_for_answer(sock: Any, timeout: float) -> None:
    """Wait until the source starts to answer the loop's long request, in short looks, so that _abort() or stop()
    ends the wait within ABORT_LOOK_S on every system. On Windows a shutdown or close of the socket from another
    thread does not wake a thread that waits for data: without the short looks a computer's new name
    reaches the others only when the long request runs out by itself. Raises OSError when ended or out of time."""
    end = time.monotonic() + timeout
    while not _aborting.is_set() and not _stop.is_set():
        left = end - time.monotonic()
        if left <= 0:
            raise socket.timeout("the source did not answer in time")
        readable, _, _ = select.select([sock], [], [], min(ABORT_LOOK_S, left))
        if readable:
            return
    raise OSError("the waiting request was ended")


def _signed_request(ln: dict, machine_id: str, path: str, body: dict, timeout: float = 10.0,
                    holder: Optional[list] = None) -> tuple:
    """A request to this machine's source, signed with the key from its link."""
    pass_ = ln.get("pass")
    if not isinstance(pass_, str) or not pass_:
        raise Unreachable()
    return _request(ln.get("host"), ln.get("port"), path, body, key=fleet.share_key(pass_), machine_id=machine_id,
                    timeout=timeout, holder=holder)


_count_lock = threading.Lock()
_counted = [0]


def _next_count(above: int = 0) -> int:
    """A number that only grows, for the calls the other computer counts: the time in milliseconds by this
    computer's own clock, or one more than the last number used, or than the one the other computer says it has
    already had, whichever is largest. So a clock that was set back cannot lock this computer out."""
    with _count_lock:
        _counted[0] = max(_counted[0] + 1, int(time.time() * 1000), above + 1)
        return _counted[0]


def _counted_request(ln: dict, machine_id: str, path: str, body: dict, timeout: float = 10.0,
                     holder: Optional[list] = None) -> tuple:
    """A signed request that also carries a number ("n") higher than any this computer sent before. The source
    keeps the highest one it has had from each linked computer in its own file and refuses anything at or below
    it, so a recording of this request is worth nothing later, also after Hermes was started again there. When
    the source answers (signed) that it has had a higher number, the request goes out once more above that."""
    above = 0
    for _ in range(2):
        status, obj, signed = _signed_request(ln, machine_id, path, {**body, "n": _next_count(above)},
                                              timeout=timeout, holder=holder)
        had = obj.get("had")
        if not (status == 409 and signed and _wire_code(obj) == "replayed" and isinstance(had, int)
                and not isinstance(had, bool) and 0 < had < fleet.MAX_COUNT):
            break
        above = had
    return status, obj, signed


def _older_side(host: Any, port: Any) -> Optional[str]:
    """After "versions cannot work together": "this" when the other computer speaks a newer wire version than
    this one, "source" when an older one; None when that cannot be told. Reads only the open hello answer, and
    only to choose which sentence to show."""
    if not net.kind_of(host) or not isinstance(port, int):
        return None
    conn = http.client.HTTPConnection(host, port, timeout=4)
    try:
        conn.request("GET", "/v1/hello")
        obj = json.loads(conn.getresponse().read(4096).decode("utf-8"))
        theirs = obj.get("proto") if isinstance(obj, dict) and obj.get("schema") == fleet.SCHEMA else None
        if not isinstance(theirs, int) or isinstance(theirs, bool) or theirs == fleet.PROTO:
            return None
        return "this" if theirs > fleet.PROTO else "source"
    except (OSError, http.client.HTTPException, ValueError, UnicodeDecodeError):
        return None
    finally:
        conn.close()


def _wire_code(obj: dict) -> str:
    err = obj.get("error")
    code = err.get("code") if isinstance(err, dict) else None
    return code if isinstance(code, str) and len(code) <= 40 else ""


def _port_setting() -> int:
    try:
        port = int(config.get("share_port"))
    except (TypeError, ValueError):
        port = 0
    return port if 0 < port < 65536 else net.DEFAULT_PORT


def _get_service() -> Any:
    from .service import get_service
    return get_service()


def ask_again() -> None:
    """End the loop's waiting request so the next one goes out now (it carries this machine's new name)."""
    if _conn:
        _state["aborted"] = True
        _abort()


def _own_code(st: dict, raw: str) -> bool:
    """Was this link code made on this very computer (the one showing now, or one of its last few)?"""
    sh = st.get("share")
    if not isinstance(sh, dict):
        return False
    code = sh.get("code")
    if isinstance(code, dict) and code.get("code") == raw:
        return True
    old = [c.get("key") for c in sh.get("old_codes") or [] if isinstance(c, dict)]
    return bool(old) and fleet.link_key(raw).hex() in old


# ---- link --------------------------------------------------------------------------------------------------------------
def link(svc: Any, code: Any, host: Any = None, port: Any = None) -> None:
    """Link this machine to the source the code came from, then fetch the first list. Raises FleetError."""
    raw = net.normalize_code(code)
    address = net.code_address(raw) if raw else None
    if raw is None or address is None:
        raise fleet.FleetError("bad_code")
    if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536):
        raise fleet.FleetError("bad_code", hint="A port is a number from 1 to 65535.")
    with fleet.locked():
        st = fleet.load()
        if st.get("link"):
            raise fleet.FleetError("already_linked", name=fp.machine_name(st["link"]["source"].get("name")))
        if _own_code(st, raw):
            # (asked first: the computer that made a code always shares, and "switch sharing off here" would be
            # the wrong advice for someone who only typed the code on the wrong computer)
            raise fleet.FleetError("own_code")
        if fleet.role(st) == "source":
            raise fleet.FleetError("sharing_here")
        if fleet.ensure_machine(st):
            fleet.save(st)
        me = fleet.this_machine(st)
    if host not in (None, ""):
        address = net.resolve_host(host)
        if address is None:
            raise fleet.FleetError("unreachable")
    port = port or _port_setting()
    # The code itself is not sent, nor anything it could be worked out from (fleet.py, "keys and signatures"):
    # both machines start from a number made from the code, exchange one value each, and then prove to each other
    # that they arrived at the same secret. Whoever is at the other end without the code gets one guess.
    nonce = fleet.new_nonce()
    secret, public = fleet.dh_new(fleet.link_base(fleet.link_key(raw)))

    def said_no(status: int, obj: dict) -> fleet.FleetError:
        wire = _wire_code(obj)
        if wire == "version_mismatch":
            side = _older_side(address, port)
            return fleet.FleetError({"this": "update_here", "source": "update_there"}.get(side, "version_mismatch"),
                                    name="the other computer")
        return fleet.FleetError(wire if wire in LINK_ERRORS else "unreachable")

    try:
        status, obj, _ = _request(address, port, "/v1/link",
                                  {"proto": fleet.PROTO, "machine": me, "nonce": nonce, "dh": public}, nonce=nonce)
    except Unreachable:
        raise fleet.FleetError("unreachable") from None
    if status != 200:
        raise said_no(status, obj)
    source = fleet.clean_machine(obj.get("source"))
    source_nonce, source_public = obj.get("nonce"), obj.get("dh")
    shared = fleet.dh_shared(secret, source_public)
    if source is None or obj.get("proto") != fleet.PROTO or not fleet._hex_ok(source_nonce, 32) or shared is None \
            or source_public == public:
        raise fleet.FleetError("unreachable")  # not an answer this exchange can use
    proof, confirm, token = fleet.link_keys(shared, nonce, source_nonce, me["id"], source["id"], public,
                                            source_public)
    if not fleet.same(obj.get("proof"), proof):
        # the other machine started from another number: the code typed here is not the one it shows (or it is
        # not the owner's machine at all). Nothing more is sent, least of all this machine's own proof.
        raise fleet.FleetError("code_wrong")
    try:
        status, obj, signed = _request(address, port, "/v1/link",
                                       {"proto": fleet.PROTO, "machine": me, "nonce": nonce, "confirm": confirm},
                                       proof_key=fleet.share_key(token), nonce=nonce)
    except Unreachable:
        raise fleet.FleetError("unreachable") from None
    if status != 200:
        raise said_no(status, obj)
    if not signed or obj.get("linked") is not True:
        raise fleet.FleetError("unreachable")
    now = now_iso()
    with fleet.locked():
        st = fleet.load()
        if st.get("link"):
            raise fleet.FleetError("already_linked", name=fp.machine_name(st["link"]["source"].get("name")))
        st["link"] = {"source": source, "host": address, "port": port, "pass": token, "linked_at": now, "rev": None,
                      "last_sync_at": None, "last_contact_at": now, "offline_since": None, "state": "syncing",
                      "fleet": [], "twins": {}}
        st["last_unlink"] = None
        st["goodbye"] = None  # (linked again: an older unlink that was never heard is of no interest now)
        fleet.save(st)
    svc.emit("fleet", None, {"what": "linked", "count": 1})
    if sync_once(svc, wait_s=0) == "synced":
        sync_once(svc, wait_s=0)  # tells the source what this machine made, so it shows "in sync" there at once
    ensure_started(_get_service)
    kick()


# ---- one sync ----------------------------------------------------------------------------------------------------------
def _set_state(svc: Any, state: str, contact: bool, why: Optional[str] = None) -> None:
    """Note how the link is doing; the state file is written only when something changed (or, while all is well,
    at most once a minute for the time of the last answer). `why`: with "error", which kind ("forgotten": the
    other computer no longer knows this one; "removed": Grok Bot Twin was removed there; None: the versions do
    not fit)."""
    with fleet.locked():
        st = fleet.load()
        ln = st.get("link")
        if not ln:
            return
        was = ln.get("state")
        now = now_iso()
        dirty = False
        if ln.get("why") != (why if state == "error" else None):
            ln["why"] = why if state == "error" else None
            dirty = True
        if contact:
            last = parse_iso(ln.get("last_contact_at"))
            if last is None or (now_dt() - last).total_seconds() > CONTACT_WRITE_EVERY_S or was != state:
                ln["last_contact_at"] = now
                dirty = True
        if state in ("offline", "sharing_off"):
            if was != state:
                ln["offline_since"] = now
                dirty = True
        elif ln.get("offline_since"):
            ln["offline_since"] = None
            dirty = True
        if was != state:
            ln["state"] = state
            dirty = True
        if dirty:
            fleet.save(st)
    if was != state:
        if state in ("offline", "sharing_off"):
            svc.emit("fleet", None, {"what": "source_offline", "count": 1})
        elif was in ("offline", "sharing_off") and why not in ("forgotten", "removed"):  # (neither is a return)
            svc.emit("fleet", None, {"what": "source_back", "count": 1})


def _removed_there(ln: dict) -> bool:
    return ln.get("state") == "error" and ln.get("why") == "removed"


def sync_once(svc: Any, wait_s: int = 0, holder: Optional[list] = None) -> str:
    """One request to the source. Returns "synced", "unchanged", "offline", "sharing_off", "unlinked", "error",
    "not_linked" or "slept" (no answer, because this machine was asleep meanwhile). Never raises."""
    try:
        st = fleet.load()
        ln = st.get("link")
        if not ln:
            return "not_linked"
        me = fleet.this_machine(st)
        wait_s = xs_config.wait_for(wait_s)  # (shorter while a twin's settings are still owed; xs_config.py)
        body = {"proto": fleet.PROTO, "have": ln.get("rev"), "wait_s": wait_s,
                "machine": {"name": me["name"], "platform": me["platform"]}, "applied": fleet.applied_report(st)}
        # further things this computer can take (skills, ...), and the settings rev it last heard: one optional
        # key, "xs", that both go into (neither overwrites the other); an older build ignores it
        items = {**fleet_items.ask(), **(xs_config.sync_body().get(fleet_items.KEY) or {})}
        if items:
            body[fleet_items.KEY] = items
        from . import routines as _routines
        rkey = _routines.request_key(st)
        if rkey is not None and len(json.dumps({**body, "routines": rkey})) < 14000:
            # (optional key; a sharing computer on an older build ignores it. A request must stay well under
            # the 16 KiB a sharing computer reads: without the key routines simply stay where they are)
            body["routines"] = rkey
        else:
            rkey = None
        began = _wall()
        try:
            status, obj, signed = _counted_request(ln, me["id"], "/v1/sync", body, timeout=wait_s + 15, holder=holder)
        except Unreachable:
            _state["unknown"] = 0
            if _stop.is_set():
                return "offline"
            if _state.pop("aborted", False):
                return "unchanged"  # this process ended the waiting request itself, to ask again at once
            if _wall() - began > wait_s + 15 + SLEPT_SLACK_S:
                # the request "took" far longer than it can: this machine slept (or the process was held) with
                # the request open. That says nothing about the other computer, so nothing is noted and the loop
                # asks again at once. Otherwise every wake-up shows "offline since now" for a moment.
                return "slept"
            if _removed_there(ln):
                return "offline"  # it said so itself (signed) before it stopped listening: that stays the truth
            # a source that said "sharing is off" and then stopped listening is still "off", not "gone"
            _set_state(svc, "sharing_off" if ln.get("state") == "sharing_off" else "offline", contact=False)
            return "offline"
        wire = _wire_code(obj)
        if status in (200, 401) and not signed:
            # not from this machine's source (something else sits at its address, or the source was set up anew
            # and no longer knows this machine): nothing is believed and nothing is taken away
            forgot = status == 401 and wire == "unlinked"
            _state["unknown"] = _state.get("unknown", 0) + 1 if forgot else 0
            if forgot and (_state["unknown"] >= FORGOTTEN_AFTER
                           or (ln.get("state") == "error" and ln.get("why") == "forgotten")):
                # A Grok Bot Twin at that address keeps answering that it does not know this computer: it was
                # set up again there (its plugin removed and installed anew), so no "back" will ever come. This
                # machine says so and how to get going again, instead of "catches up by itself" for ever.
                # Still nothing is taken away, and it keeps asking: a signed answer puts all right again.
                _set_state(svc, "error", contact=False, why="forgotten")
                return "error"
            if _removed_there(ln):
                return "offline"
            _set_state(svc, "sharing_off" if ln.get("state") == "sharing_off" else "offline", contact=False)
            return "offline"
        _state["unknown"] = 0
        if status == 401 and wire == "unlinked":
            with fleet.locked():
                st = fleet.load()
                now_ln = st.get("link")
                if not now_ln or now_ln.get("pass") != ln.get("pass"):
                    return "not_linked"  # this machine unlinked itself (or was linked anew) in the meantime
                n = fleet.drop_link(st, "source")
                fleet.save(st)
            svc.emit("fleet", None, {"what": "unlinked", "count": n})
            return "unlinked"
        if status != 200:
            if wire == "version_mismatch":
                side = _older_side(ln.get("host"), ln.get("port"))
                with fleet.locked():
                    st = fleet.load()
                    if st.get("link") and st["link"].get("older") != side:
                        st["link"]["older"] = side
                        fleet.save(st)
            _set_state(svc, "error" if wire == "version_mismatch" else "offline", contact=wire == "version_mismatch")
            return "error"
        if obj.get("sharing") is False:
            if obj.get("removed") is True:
                # The owner removed Grok Bot Twin on the other computer (its own signed word, sent as it stopped
                # sharing). Switching sharing on there again cannot bring this link back, so this computer says
                # that, not "catch up when it is switched on again". Nothing is taken away here.
                _set_state(svc, "error", contact=True, why="removed")
                return "sharing_off"
            _set_state(svc, "sharing_off", contact=True)
            return "sharing_off"
        if isinstance(obj.get("takes_requests"), dict):  # which of the owner's other computers take requests
            from . import tasks as _tasks
            _tasks.set_heard(obj.get("takes_requests"))
        xs_config.heard(obj)  # what this signed answer says about the settings that follow (xs_config.py)
        if rkey is not None:
            _routines.heard_answer(obj)  # the routines of the sharing computer, or that its build has none
        if obj.get("changed") is not True:
            if obj.get("asked_while_off"):
                from . import tasks
                tasks.log_missed(obj.get("asked_while_off"))  # shown in this computer's own list of requests
            with fleet.locked():
                st = fleet.load()
                if st.get("link") and fleet.retry_failed(st, fleet.save):
                    fleet.save(st)
            _set_state(svc, "in_sync", contact=True)
            fleet_items.heard(svc, obj, items)  # what the other computer says about skills etc. (never raises)
            xs_config.follow(svc)  # fetch and apply the twins' settings when there is something new (never raises)
            return "unchanged"
        lst = fleet.clean_list(obj)
        if lst is None or lst["source"]["id"] != ln["source"].get("id"):
            _set_state(svc, "error", contact=True)
            return "error"
        with fleet.locked():
            st = fleet.load()
            if not st.get("link"):
                return "not_linked"
            counts = fleet.apply_list(st, lst, fleet.save)
            fleet.prune_kept(st)
            fleet.save(st)
        _set_state(svc, "in_sync", contact=True)
        for what in ("added", "updated", "removed"):
            if counts[what]:
                svc.emit("fleet", None, {"what": f"twin_{what}", "count": counts[what]})
        svc.emit("fleet", None, {"what": "synced", "count": len(lst["twins"])})
        xs_config.follow(svc)  # a twin that is new here gets its settings too, when they were already heard of
        return "synced"
    except Exception:
        log.debug("grok-bot-twin: sync with the source failed", exc_info=True)
        return "error"


# ---- unlink --------------------------------------------------------------------------------------------------------------
def leaving(st: dict) -> dict:
    """The sentence and the lists shown before (and after) this machine stops following."""
    ln = st["link"]
    name = fp.machine_name(ln["source"].get("name"))
    n = len([e for e in ln["twins"].values() if isinstance(e, dict)])
    its = f"its {plural(n, 'twin')} {'leaves' if n == 1 else 'leave'} this computer only" if n else \
        "nothing else changes"
    kept = "It is kept for 30 days with its chats." if n == 1 else "They are kept for 30 days with their chats."
    goes = [f"The {plural(n, 'twin')} on this computer. {kept}"] if n else [f"The link to {name}."]
    return {"machine": {"id": ln["source"].get("id"), "name": name},
            "sentence": f"This computer stops following {name}; {its}.",
            "stays": [f"Your twins on {name} and on your other computers.",
                      "Everything else in Hermes on this computer."],
            "goes": goes,
            "undo": "Link this computer again within 30 days and the twins come back with their chats."}


def unlink(svc: Any, dry_run: bool = False) -> dict:
    st = fleet.load()
    ln = st.get("link")
    if not ln:
        raise fleet.FleetError("not_linked")
    out = {"dry_run": dry_run, "unlinked": not dry_run, **leaving(st)}
    if dry_run:
        return out
    with fleet.locked():  # first here, so that the source's answer to the waiting request finds nothing to do
        st = fleet.load()
        if st.get("link"):
            from . import routines as _routines
            if not _routines.link_dropped(st):
                # a routine this computer runs for the others could not be seen paused (Hermes holds its job
                # file right now): it stays linked, because the goodbye would let that routine start elsewhere
                raise _routines.RoutinesError("busy")
        n = fleet.drop_link(st, "this") if st.get("link") else 0
        fleet.save(st)
    _abort()
    if not say_goodbye(ln):
        _note_goodbye(ln)  # it could not be told now (off, asleep, sharing off): told again later, see below
    svc.emit("fleet", None, {"what": "unlinked", "count": n})
    return out


def say_goodbye(ln: dict) -> bool:
    """Tell the source this machine left. True when it heard (or already knows this machine no more); False when
    it could not be reached. Never raises. "Heard" counts only with the source's signature. "Knows this machine no
    more" cannot be signed (the source has no key for a machine it does not know), so that answer is taken as it
    comes: all it ends is the saying again, and nothing is taken away or brought by it."""
    try:
        status, obj, signed = _counted_request(ln, fleet.load()["machine"]["id"], "/v1/unlink",
                                               {"proto": fleet.PROTO}, timeout=5)
        return (status == 200 and signed) or (status == 401 and _wire_code(obj) == "unlinked")
    except Exception:
        return False


# An unlink the other computer could not be told of. Its list would go on showing this computer ("NAME ·
# waiting for sharing to be switched on", "also on NAME") until the owner removed it there by hand.
# The goodbye is now kept and said again, less and less often, for at most
# 30 days: at the background check while Hermes is open here. What is kept is what the link itself held (address,
# port, this computer's own pass); it goes the moment the goodbye is heard, when this computer is linked again, or
# with the plugin's data.
GOODBYE_DAYS = 30
GOODBYE_MAX_GAP_S = 3600


def _note_goodbye(ln: dict) -> None:
    try:
        with fleet.locked():
            st = fleet.load()
            if st.get("link"):
                return
            st["goodbye"] = {"host": ln.get("host"), "port": ln.get("port"), "pass": ln.get("pass"),
                             "since": now_iso(), "tries": 1, "next_at": time.time() + 60}
            fleet.save(st)
    except Exception:
        log.debug("grok-bot-twin: could not keep the goodbye for later", exc_info=True)


def retry_goodbye(force: bool = False) -> Optional[bool]:
    """Say a kept goodbye again when its time has come. True: heard, and forgotten here; False: still not heard
    (or given up after 30 days); None: nothing to do now. Never raises."""
    try:
        gb = fleet.load().get("goodbye")
        if not isinstance(gb, dict):
            return None
        since = parse_iso(gb.get("since"))
        old = since is None or (now_dt() - since).total_seconds() > GOODBYE_DAYS * 86400
        due = isinstance(gb.get("next_at"), (int, float)) and time.time() >= gb["next_at"]
        if not old and not due and not force:
            return None
        heard = False if old else say_goodbye(gb)
        with fleet.locked():
            st = fleet.load()
            if st.get("goodbye") != gb:
                return None  # (linked again, or another process was quicker)
            if heard or old or st.get("link"):
                st["goodbye"] = None
            else:
                tries = int(gb.get("tries") or 0) + 1
                st["goodbye"] = {**gb, "tries": tries,
                                 "next_at": time.time() + min(GOODBYE_MAX_GAP_S, 60 * 2 ** min(tries, 10))}
            fleet.save(st)
        return heard
    except Exception:
        log.debug("grok-bot-twin: the kept goodbye could not be said", exc_info=True)
        return False


# ---- the loop --------------------------------------------------------------------------------------------------------------
def _abort() -> None:
    _aborting.set()  # what ends the wait on Windows; the shutdown below does it at once on macOS and Linux
    for conn in list(_conn):
        sock = conn.sock
        if sock is None:
            continue
        with contextlib.suppress(Exception):
            sock.shutdown(socket.SHUT_RDWR)  # wakes the thread that waits in recv(); close() alone may not
        with contextlib.suppress(Exception):
            sock.close()


def _linked() -> bool:
    try:
        return fleet.exists() and bool(fleet.load().get("link"))
    except OSError:
        return False


def run_linked(svc: Any, should_stop: Callable[[], bool]) -> None:
    """Follow the source until this machine is unlinked or told to stop. Holds the follow lock; when another
    Hermes process holds it, waits and looks again."""
    with fio.exclusive(store.central_dir() / "fleet-follow.lock") as got:
        if not got:
            _wake.wait(30)
            _wake.clear()
            return
        _state["holding"] = True
        try:
            _state["backoff"] = MIN_BACKOFF_S
            well = False
            while not should_stop():
                # after trouble the first request does not wait, so "back in sync" shows the moment the source
                # answers again; only then does this machine settle into long requests
                result = sync_once(svc, wait_s=LONG_WAIT_S if well else 0, holder=_conn)
                if result in ("unlinked", "not_linked"):
                    return
                if result == "slept":  # this machine was asleep: ask again now, without a long wait
                    well = False
                    continue
                well = result in ("synced", "unchanged")
                if well:
                    _state["backoff"] = MIN_BACKOFF_S
                    continue
                _wake.wait(_state["backoff"])  # offline, sharing off, or an answer that was not understood
                _wake.clear()
                _state["backoff"] = min(MAX_BACKOFF_S, _state["backoff"] * 2)
        finally:
            _state["holding"] = False


def _loop(get_service: Callable) -> None:
    while not _stop.is_set():
        try:
            if _linked():
                run_linked(get_service(), _stop.is_set)
            else:
                _wake.wait(IDLE_CHECK_S if _state.get("watch_idle") else None)
                _wake.clear()
        except Exception:
            log.debug("grok-bot-twin: the follower loop stumbled", exc_info=True)
            _wake.wait(30)
            _wake.clear()


def ensure_started(get_service: Callable, watch_idle: bool = False) -> bool:
    """Start the follower thread once per process (it sleeps until this machine is linked). `watch_idle`: also
    look at the state file every few seconds while not linked, for a process that another one may link
    (`hermes twin fleet run`). True when a new thread was started."""
    global _thread
    with _lock:
        if _stop.is_set():
            return False
        _state["watch_idle"] = watch_idle or _state.get("watch_idle", False)
        if _thread is not None and _thread.is_alive():
            return False
        if not watch_idle and not _linked():
            return False  # nothing to follow: no thread at all for a machine that is not linked
        t = config.thread(_loop, "grok-bot-twin-follow", (get_service,))
        t.start()
        _thread = t
        return True


def kick() -> None:
    _wake.set()


def running() -> bool:
    return _thread is not None and _thread.is_alive() and not _stop.is_set()


def stop() -> None:
    _stop.set()
    _wake.set()
    _abort()


def _reset_for_tests() -> None:
    global _thread
    stop()
    if _thread is not None:
        _thread.join(5)
    _thread = None
    _stop.clear()
    _wake.clear()
    _state.update(backoff=MIN_BACKOFF_S, holding=False, watch_idle=False, unknown=0)
