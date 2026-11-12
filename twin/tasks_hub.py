"""The mailbox for requests between the owner's computers. It lives in the share server of the computer with the
Grok app (fleet_server.py), because that is the one place every linked computer can already reach, on the one
address that server listens on (Tailscale, else the home network; never all interfaces). No new socket.

Four small routes, all POST. Each request is signed the way the twin list's are (a linked computer's own key
over the path, a fresh nonce and the body; nothing secret travels), its time must be within 15 minutes of this
computer's and its nonce new, so a recorded request cannot be sent again. The answer is signed back over that
nonce, so only the real sharing computer can hand a computer a request or an answer. A Hermes process on the
sharing computer itself shows the instance secret instead, which only that computer's own files hold:

- `/v1/task/ask`     {"proto", "to": "m-…", "twin": "t-…", "text"}       -> {"id": "k-…", "state": "queued"}
- `/v1/task/wait`    {"proto", "id", "wait_s"}                           -> {"id", "state", "code"?, "seconds"?, "reply"?}
- `/v1/task/poll`    {"proto", "accept": bool, "wait_s"}                 -> {"task": {"id", "from", "twin", "text"} | null}
- `/v1/task/update`  {"proto", "id", "state", "code"?, "seconds"?, "reply"?} -> {"ok": true}

A step that waits for approval on the asked computer is answered on that computer only (tasks.decide). `update`
says "waiting_approval" and nothing about what waits.

What the mailbox does and does not do:

- The asker is whoever the signature says it is; a request cannot name its own sender. `wait` answers only the
  computer that asked, `update` only the computer that was asked.
- It refuses before queueing: unknown computer, a computer that has not said "I take requests" lately (its
  switch is off, or Hermes is not running there), a text that is empty or too long, too many requests from one
  computer (tasks.Rate), too many waiting for one computer.
- It never runs anything and never decides whether a request may run: the asked computer checks its own switch
  again when the request arrives there (tasks_run.py), and its own Hermes applies its own approval rules.
- Requests are kept in memory only, for at most TASK_TTL_S after they end. Text passing through is written to no
  file on the sharing computer; its log keeps who asked whom and how it ended.
- Errors travel as fixed codes, never as sentences.
"""
from __future__ import annotations

import hmac
import logging
import math
import threading
import time
from typing import Any, Callable, Optional

from . import fleet, tasks
from .model import REF_RE, now_dt, now_iso, parse_iso

log = logging.getLogger(__name__)

POLL_FRESH_S = 75       # a computer that polled this recently is taken to be listening
MAX_WAIT_S = 50
MAX_TASKS = 64
MISSED_CAP = 20  # refused requests remembered per computer until it hears of them
WIRE_STATES = ("running", "waiting_approval") + tasks.END_STATES
NOT_APPROVED = ("approval_denied", "approval_timeout", "only_safe")


def _err(status: int, code: str) -> tuple:
    return status, {"schema": fleet.SCHEMA, "error": {"code": code}}


def _ok(obj: dict) -> tuple:
    return 200, {"schema": fleet.SCHEMA, "proto": fleet.PROTO, **obj}


class Hub:
    """Requests in flight, in memory. One condition wakes every waiter; each looks at its own request."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.tasks: dict = {}
        self.listening: dict = {}  # machine id -> (accept, monotonic time of its last poll)
        self.rate = tasks.Rate()
        self.closing = False
        # machine id -> who asked it while its switch was off (id, the asking computer's name, when; never the
        # request's text). That computer hears it with its next look at the twin list and writes it in its own
        # log, so a refused request shows on both computers.
        self.missed: dict = {}
        # machine id -> the picture of the other computers' switches it was last sent (see `tell`, `news`)
        self.told: dict = {}
        self._kept: Optional[dict] = None  # the picture as last written to this computer's own file

    def _missed(self, to: str, frm: dict, tid: Optional[str] = None) -> None:
        rows = self.missed.setdefault(to, [])
        rows.append({"id": tid or tasks.new_id(), "from": frm.get("name"), "at": now_iso()})
        del rows[:-MISSED_CAP]

    def take_missed(self, mid: str) -> list:
        with self.cond:
            return self.missed.pop(mid, [])

    # ---- bookkeeping ---------------------------------------------------------------------------------------------
    def _prune(self) -> None:
        now = time.monotonic()
        for tid in [k for k, t in self.tasks.items() if t["ended"] and now - t["ended"] > tasks.TASK_TTL_S]:
            del self.tasks[tid]
        if len(self.tasks) > MAX_TASKS:
            for tid in sorted((k for k, t in self.tasks.items() if t["ended"]), key=lambda k: self.tasks[k]["ended"]):
                del self.tasks[tid]
                if len(self.tasks) <= MAX_TASKS:
                    break

    def _expire(self) -> None:
        """A request nobody fetched, or one whose computer went silent, ends as a timeout."""
        now = time.monotonic()
        for t in self.tasks.values():
            if t["ended"]:
                continue
            limit = tasks.RUN_LIMIT_S + tasks.APPROVAL_LIMIT_S + 60
            if t["state"] == "queued" and now - t["asked"] > POLL_FRESH_S and not self._at_work(t["to"]):
                self._end(t, "failed", "tasks_unreachable")
            elif now - t["asked"] > limit:
                self._end(t, "timeout", "task_timeout")

    def _end(self, t: dict, state: str, code: Optional[str] = None, reply: str = "", seconds: Any = None) -> None:
        t.update(state=state, code=code, reply=reply, ended=time.monotonic())
        # (what the asked computer says, when it is a believable number: finite, from 0 to a day)
        t["seconds"] = seconds if tasks.whole_seconds(seconds) is not None else round(t["ended"] - t["asked"], 1)
        if t.get("relay"):
            tasks.log_update("relay", t["id"], state, code, t["seconds"])
        self.cond.notify_all()

    def _at_work(self, mid: str) -> bool:
        """Has this computer fetched a request it has not finished? (It fetches the next one when it has.)"""
        return any(t["to"] == mid and t["taken"] and not t["ended"] for t in self.tasks.values())

    def is_listening(self, mid: str) -> Optional[bool]:
        """True: polled lately with its switch on. False: said its switch is off. None: not heard from."""
        seen = self.listening.get(mid)
        if seen is None or time.monotonic() - seen[1] > POLL_FRESH_S:
            return None
        return bool(seen[0])

    def _heard(self, st: dict, to: str) -> Optional[bool]:
        """Does this computer take requests, as far as the sharing computer can tell? Called with `cond` held."""
        me = fleet.this_machine(st)
        heard = self.is_listening(to)
        if heard is None and self._at_work(to):
            heard = True  # it fetched a request and is still on it (a step may wait for its owner): queue behind
        if to == me["id"]:
            return tasks.accepting()  # this computer's own switch, read from its own file
        if heard is None:
            # it keeps its twins in step (seen lately) but fetches no requests: its switch is off (the default)
            seen = parse_iso(((st.get("share") or {}).get("followers", {}).get(to) or {}).get("last_seen_at"))
            if seen is not None and (now_dt() - seen).total_seconds() <= fleet.OFFLINE_AFTER_S:
                heard = False
        return heard

    def picture(self, st: dict) -> dict:
        """{machine id: bool} for every computer of the fleet whose switch is known here (this one included;
        one that is not heard from is left out). Also kept in this computer's own file when it changed, for the
        Hermes processes here that do not hold the mailbox."""
        with self.cond:
            pic = {}
            for m in fleet.fleet_members(st) if st.get("share") else []:
                heard = self._heard(st, m["id"])
                if heard is not None:
                    pic[m["id"]] = heard
            changed = pic != self._kept
            self._kept = dict(pic)
        if changed:
            tasks.set_heard(pic)
        return pic

    def tell(self, mid: str, st: dict) -> dict:
        """The optional key a signed answer to `mid` carries: what is known of the OTHER computers' switches."""
        pic = {k: v for k, v in self.picture(st).items() if k != mid}
        with self.cond:
            self.told[mid] = pic
        return {"takes_requests": pic}

    def news(self, mid: str, st: dict) -> Optional[dict]:
        """The same key, but only when it differs from what `mid` was last told (its waiting look at the twin
        list is then answered at once); None when it was told nothing yet or nothing changed."""
        pic = {k: v for k, v in self.picture(st).items() if k != mid}
        with self.cond:
            if mid not in self.told or self.told[mid] == pic:
                return None
            self.told[mid] = pic
        return {"takes_requests": pic}

    # ---- the four calls --------------------------------------------------------------------------------------------
    def ask(self, st: dict, frm: dict, body: dict) -> tuple:
        to, twin, text = body.get("to"), body.get("twin"), body.get("text")
        me = fleet.this_machine(st)
        known = {m["id"]: m for m in fleet.fleet_members(st)}
        if not isinstance(to, str) or to not in known:
            return _err(404, "unknown_machine")
        with self.cond:
            self._expire()
            self._prune()
            heard = self.is_listening(to)
            if heard is None and self._at_work(to):
                heard = True  # it fetched a request and is still on it (a step may wait for its owner): queue behind
            if to == me["id"] and not tasks.accepting():
                heard = False  # this computer's own switch, read from its own file
            elif heard is None and to != me["id"]:
                # it keeps its twins in step (seen lately) but fetches no requests: its switch is off (the default)
                seen = parse_iso((st["share"]["followers"].get(to) or {}).get("last_seen_at"))
                if seen is not None and (now_dt() - seen).total_seconds() <= fleet.OFFLINE_AFTER_S:
                    heard = False
            if heard is False:
                # a refused request shows in the asked computer's log too (who asked and when; never the text)
                if to == me["id"]:
                    tasks.log_entry("in", tasks.new_id(), frm["name"], state="refused", code="tasks_off")
                elif to != frm["id"]:
                    self._missed(to, frm)
                    fleet.hub.bump()  # its waiting look at the twin list is answered now, and carries the news
                return _err(409, "tasks_off")
            if heard is None:
                return _err(503, "tasks_unreachable")
            if not isinstance(twin, str) or not REF_RE.match(twin):
                return _err(400, "bad_request")
            if not isinstance(text, str) or len(text) > tasks.REQUEST_CAP * 4:
                return _err(413, "too_long")
            clean = tasks.clean_request(text)
            if not clean:
                return _err(400, "empty")
            if len(text.strip()) > tasks.REQUEST_CAP:
                return _err(413, "too_long")
            if to == frm["id"]:
                return _err(409, "ask_self")
            if not self.rate.allow(frm["id"]):
                return _err(429, "too_many")
            if len([t for t in self.tasks.values() if t["to"] == to and not t["ended"]]) >= tasks.QUEUE_CAP:
                return _err(429, "busy")
            tid = tasks.new_id()
            relay = frm["id"] != me["id"] and to != me["id"]
            self.tasks[tid] = {"id": tid, "from": {"id": frm["id"], "name": frm["name"]}, "to": to,
                               "to_name": known[to]["name"], "twin": twin, "text": clean, "state": "queued",
                               "code": None, "reply": "", "seconds": None, "asked": time.monotonic(),
                               "asked_at": now_iso(), "ended": 0.0, "taken": False, "relay": relay}
            if relay:
                tasks.log_entry("relay", tid, frm["name"], peer_to=known[to]["name"])
            self.cond.notify_all()
        return _ok({"id": tid, "state": "queued"})

    def wait(self, frm: dict, body: dict) -> tuple:
        tid = body.get("id")
        wait_s = body.get("wait_s")
        wait_s = min(MAX_WAIT_S, max(0, wait_s)) if isinstance(wait_s, (int, float)) and not isinstance(wait_s, bool) \
            else 0
        seen = body.get("seen") if body.get("seen") in tasks.STATES else None
        deadline = time.monotonic() + wait_s
        with self.cond:
            while True:
                self._expire()
                t = self.tasks.get(tid) if isinstance(tid, str) else None
                if t is None or t["from"]["id"] != frm["id"]:
                    return _err(404, "unknown_task")
                left = deadline - time.monotonic()
                # answered at once when the request ended, or moved on from the state the asker already knows
                if t["ended"] or left <= 0 or self.closing or (seen is not None and t["state"] != seen) \
                        or (seen is None and t["state"] == "waiting_approval"):
                    out = {"id": t["id"], "state": t["state"]}
                    if t["code"]:
                        out["code"] = t["code"]
                    if t["ended"]:
                        out["seconds"] = t["seconds"]
                        if t["state"] == "done":
                            out["reply"] = t["reply"]
                    return _ok(out)
                self.cond.wait(min(left, 5.0))

    def poll(self, who: dict, body: dict) -> tuple:
        accept = body.get("accept") is True
        wait_s = body.get("wait_s")
        wait_s = min(MAX_WAIT_S, max(0, wait_s)) if isinstance(wait_s, (int, float)) and not isinstance(wait_s, bool) \
            else 0
        deadline = time.monotonic() + wait_s
        with self.cond:
            before = self.is_listening(who["id"])
            self.listening[who["id"]] = (accept, time.monotonic())
            if before is not accept:
                fleet.hub.bump()  # the other computers' waiting looks at the twin list carry the news (`news`)
            if not accept:
                for t in self.tasks.values():  # what waited for this computer is answered now
                    if t["to"] == who["id"] and not t["ended"]:
                        if t["state"] == "queued" and not who.get("inside"):
                            self._missed(who["id"], t["from"], t["id"])  # it never saw this one: it hears of it
                        self._end(t, "refused", "tasks_off" if t["state"] == "queued" else "switched_off")
                self.cond.notify_all()  # (also ends an older waiting poll of this computer)
                return _ok({"task": None})
            mark = self.listening[who["id"]]
            while True:
                self._expire()
                if self.listening.get(who["id"]) is not mark:
                    return _ok({"task": None})  # a newer poll of this computer took over, or it switched off
                if who.get("gone") and who["gone"]():
                    # the computer that was waiting here has hung up (its program ended): nothing is handed to
                    # it. A request given to such a fetch would be lost, and its asker would hear "still
                    # working" until the request ran out of time
                    return _ok({"task": None})
                for t in sorted(self.tasks.values(), key=lambda t: t["asked"]):
                    if t["to"] == who["id"] and t["state"] == "queued" and not t["taken"]:
                        t["taken"] = True
                        t["state"] = "running"
                        self.cond.notify_all()
                        return _ok({"task": {"id": t["id"], "from": t["from"], "twin": t["twin"], "text": t["text"]}})
                left = deadline - time.monotonic()
                if left <= 0 or self.closing:
                    self.listening[who["id"]] = (True, time.monotonic())
                    return _ok({"task": None})
                self.cond.wait(min(left, 1.0 if who.get("inside") else 5.0))
                if who.get("inside") and who["inside"]():  # this process's own worker is being stopped
                    return _ok({"task": None})

    def update(self, who: dict, body: dict) -> tuple:
        tid, state = body.get("id"), body.get("state")
        if state not in WIRE_STATES:
            return _err(400, "bad_request")
        code = body.get("code") if body.get("code") in tasks.CODES else None
        with self.cond:
            t = self.tasks.get(tid) if isinstance(tid, str) else None
            if t is None or t["to"] != who["id"]:
                return _err(404, "unknown_task")
            if t["ended"]:
                # ended already (timed out, or told twice): the first ending stands. "ended" lets a computer
                # that still works on it stop
                return _ok({"ok": True, "ended": True})
            if state in tasks.END_STATES:
                reply = tasks.clean_reply(body.get("reply")) if state == "done" else ""
                if state == "done":  # the only thing a finished request may add: a step was not approved there
                    code = code if code in NOT_APPROVED else None
                self._end(t, state, code if state == "done" else (code or "task_failed"), reply, body.get("seconds"))
            else:
                t.update(state=state, code=None)
                self.cond.notify_all()
        return _ok({"ok": True})

    def forget(self, mid: str) -> None:
        """A computer was unlinked: what it asked and what was asked of it ends."""
        with self.cond:
            self.listening.pop(mid, None)
            self.told.pop(mid, None)
            self.missed.pop(mid, None)
            for t in self.tasks.values():
                if not t["ended"] and mid in (t["to"], t["from"]["id"]):
                    self._end(t, "failed", "tasks_unreachable")
                if mid == t["from"]["id"]:
                    t["reply"] = ""  # an answer that had arrived is not handed to a computer that was removed
            self.cond.notify_all()

    def close(self) -> None:
        with self.cond:
            self.closing = True
            for t in self.tasks.values():
                if not t["ended"]:
                    self._end(t, "failed", "stopped")
            self.cond.notify_all()


hub = Hub()


def forget_unlinked(st: Optional[dict] = None) -> None:
    """A computer that is no longer linked has nothing in the mailbox any more: what it asked and what was asked
    of it ends now, and a request of it that is still held open is answered (without any reply). Run whenever
    the list of linked computers changed."""
    try:
        st = st or fleet.load()
        known = {m["id"] for m in fleet.fleet_members(st)} if st.get("share") else set()
        box = hub
        with box.cond:
            there = set(box.listening) | {t["to"] for t in box.tasks.values()} \
                | {t["from"]["id"] for t in box.tasks.values()}
        for mid in there - known:
            box.forget(mid)
    except Exception:
        log.debug("grok-bot-twin: could not clear the mailbox of an unlinked computer", exc_info=True)


def reset() -> None:
    """A fresh mailbox (the share server was started anew, or a test begins)."""
    global hub
    hub.close()
    hub = Hub()


# ---- who is asking ------------------------------------------------------------------------------------------------------
CLOCK_WINDOW_S = 900      # a signed request older or newer than this is not accepted
_nonces: dict = {}        # (machine id, nonce) -> when seen: a signed request is accepted once
_nonce_lock = threading.Lock()


def fresh(machine_id: str, nonce: str) -> bool:
    """True the first time this computer's nonce is seen (kept for twice the clock window, in memory). When the
    memory is full the oldest are let go, never all at once: a computer that sends very many requests cannot
    make this one forget what the others sent."""
    now = time.monotonic()
    with _nonce_lock:
        if len(_nonces) > 4096:
            for k in [k for k, t in _nonces.items() if now - t > 2 * CLOCK_WINDOW_S]:
                del _nonces[k]
            if len(_nonces) > 16384:
                for k in sorted(_nonces, key=_nonces.get)[:len(_nonces) - 8192]:
                    del _nonces[k]
        key = (machine_id, nonce)
        seen = _nonces.get(key)
        if seen is not None and now - seen <= 2 * CLOCK_WINDOW_S:
            return False
        _nonces[key] = now
        return True


SEEN_FILE = "tasks-seen.json"


def first_ask(machine_id: str, nonce: str) -> bool:
    """True the first time this computer's request number is seen, across restarts: kept in a small file for
    twice the clock window (numbers and times only, at most 2,000). A file that cannot be read or written counts
    as "seen" for nothing: the in-memory check has already passed, and a broken disk must not stop requests."""
    try:
        from . import fleet_io as fio, store
        path = store.central_dir() / SEEN_FILE
        now = time.time()
        key = f"{machine_id}:{nonce}"
        with fleet.locked():
            raw = fio.read_json(path, None)
            rows = raw.get("seen") if isinstance(raw, dict) and isinstance(raw.get("seen"), dict) else {}
            rows = {k: t for k, t in rows.items() if isinstance(k, str) and isinstance(t, (int, float))
                    and not isinstance(t, bool) and abs(now - t) <= 2 * CLOCK_WINDOW_S}
            if key in rows:
                return False
            rows[key] = int(now)
            fio.write_json(path, {"seen": dict(sorted(rows.items(), key=lambda kv: kv[1])[-2000:])})
        return True
    except Exception:
        log.debug("grok-bot-twin: could not keep the seen request numbers", exc_info=True)
        return True


def caller(st: dict, asked: Any, bearer: str, secret: str) -> tuple:
    """(who, how to sign the answer). `who` is {"id", "name", "local"} of a linked computer whose signature over
    this very request fits (its key never travels), or of this computer itself for a Hermes process on the same
    computer that shows the instance secret only it can read; None for anybody else. The second value is
    (key, nonce) so the answer can be signed for that computer, or None."""
    if isinstance(bearer, str) and bearer and secret and hmac.compare_digest(bearer.encode(), secret.encode()):
        me = fleet.this_machine(st)
        return {"id": me["id"], "name": me["name"], "local": True}, None
    f, key = asked.by(st)
    if f is None:
        return None, ((key, asked.nonce) if key is not None else None)
    m = fleet.clean_machine(f)
    if m is None:
        return None, None
    return {"id": m["id"], "name": m["name"], "local": False}, (key, asked.nonce)


ROUTES = ("/v1/task/ask", "/v1/task/wait", "/v1/task/poll", "/v1/task/update")


def handle(path: str, asked: Any, bearer: str, body: dict, secret: str,
           gone: Optional[Callable[[], bool]] = None) -> tuple:
    """One request to the mailbox. Returns (HTTP status, answer, how to sign it or None). `gone()` says whether
    the caller has hung up meanwhile (asked while a fetch is held open)."""
    if body.get("proto") != fleet.PROTO:
        return _err(426, "version_mismatch") + (None,)
    st = fleet.load()
    sh = st.get("share")
    if not sh or not sh.get("on"):
        return _err(503, "not_sharing") + (None,)
    who, signed = caller(st, asked, bearer, secret)
    if who is None:
        return _err(401, "unlinked") + (signed,)
    if not who["local"]:
        # a recorded request is worth nothing later: its time must be near this computer's, and its nonce new
        at = body.get("at")
        if not isinstance(at, (int, float)) or isinstance(at, bool) or not math.isfinite(at) \
                or abs(time.time() - at) > CLOCK_WINDOW_S:
            return _err(400, "clock_off") + (signed,)
        if not fresh(who["id"], asked.nonce):
            return _err(409, "replayed") + (signed,)
        # a request to DO something is also remembered on disk: after this Hermes is started again its memory
        # above is empty, and a request recorded in the last minutes must still not run a second time
        if path == "/v1/task/ask" and not first_ask(who["id"], asked.nonce):
            return _err(409, "replayed") + (signed,)
    if path == "/v1/task/ask":
        out = hub.ask(st, who, body)
    elif path == "/v1/task/wait":
        out = hub.wait(who, body)
    elif path == "/v1/task/poll":
        out = hub.poll({**who, "gone": gone} if gone is not None else who, body)
    else:
        out = hub.update(who, body)
    return out + (signed,)
