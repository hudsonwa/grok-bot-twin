"""Further things that travel between the owner's linked computers, beside the twin list (skills, for one).

The twin list has a closed allowlist and a fixed wire version; nothing is added to it. An ITEM is a kind of
thing both computers must name before any of it travels (one convention for every such item):

- A linked computer that knows an item says so in its ordinary `/v1/sync` request: `"xs": {"skills": "<the rev it
  was last told, or empty>"}`. A sharing computer on an older build ignores the key, and the linked computer
  then knows that the other one does not send that item yet.
- A sharing computer that knows the item answers in its signed "nothing changed" answer with `"xs": {"skills":
  "<its rev>"}`, and answers a waiting request at once when its rev is not the one the linked computer named.
  It adds nothing for a computer that did not name the item, so an older linked computer gets exactly the
  answers it always got.
- The item's content travels on a route of its own, `/v1/xs/<item>` (signed and counted like every request).

An item registers itself here; the share server and the follower's loop know only this module, so several
items share one line in each.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

KEY = "xs"
_items: dict = {}   # name -> {"rev", "have", "heard"}
routes: dict = {}   # wire path -> handler(asked, body) -> (status, object, signed)
MAX_ITEMS = 16
REV_CAP = 64
MIN_GAP_S = 2.0
_last = [0.0]
SKIP = object()  # an item's `have` answers this while the owner has it switched off here: it is not named


def register(name: str, rev: Callable[[], Optional[str]], have: Callable[[], Any],
             heard: Callable[[Any, Optional[str]], None], route: Optional[str] = None,
             handler: Optional[Callable] = None) -> None:
    _items[name] = {"rev": rev, "have": have, "heard": heard}
    if route and handler:
        routes[route] = handler


def ask() -> dict:
    """Linked computer: the value of "xs" in its sync request, {} when it names nothing."""
    out = {}
    for name, it in _items.items():
        try:
            have = it["have"]()
            if have is not SKIP:
                out[name] = have if isinstance(have, str) else ""
        except Exception:
            log.debug("grok-bot-twin: item %s could not say what it holds", name, exc_info=True)
    return out


def answer(body: dict) -> dict:
    """Sharing computer: {"xs": {...}} for the items the asking computer named and this one knows, else {}."""
    asked = body.get(KEY)
    if not isinstance(asked, dict):
        return {}
    out = {}
    for name in list(asked)[:MAX_ITEMS]:
        it = _items.get(name)
        if it is None or not isinstance(asked[name], str):
            continue
        try:
            r = it["rev"]()
        except Exception:
            log.debug("grok-bot-twin: item %s could not say its rev", name, exc_info=True)
            continue
        if isinstance(r, str) and 0 < len(r) <= REV_CAP:
            out[name] = r
    return {KEY: out} if out else {}


def differs(body: dict, ans: dict) -> bool:
    """True when the asking computer named another rev of some item than the one this computer has now."""
    asked = body.get(KEY) if isinstance(body.get(KEY), dict) else {}
    return any(rev != asked.get(name) for name, rev in (ans.get(KEY) or {}).items())


def heard(svc: Any, obj: dict, asked: Optional[dict] = None) -> None:
    """Linked computer: a signed "nothing changed" answer arrived. Every item hears its rev (None when the
    sharing computer said nothing about it: an older build there). `asked`: what this computer named in the
    request (the value of `ask()` it sent). Never raises."""
    said = obj.get(KEY) if isinstance(obj.get(KEY), dict) else {}
    # An answer comes back at once whenever a rev differs. Should one keep differing (a state file that cannot be
    # written here, a list that changes without pause there), the loop must not turn into a request a moment:
    # two such answers within MIN_GAP_S make this thread wait out the rest of it.
    # Only an answer with a rev other than the one asked with counts. A request that waits for nothing (as the
    # ones at linking) is answered at once too, with the very revs it named: that must not make the loop wait,
    # because while this thread waits no request is held open on the sharing computer, and sharing switched off
    # there in that moment would be heard as "not answering".
    try:
        named = ask() if asked is None else asked
        early = bool(_items) and differs({KEY: named}, {KEY: said})
    except Exception:
        early = bool(_items)
    if early:
        now = time.monotonic()
        gap = now - _last[0]
        _last[0] = now
        if 0 <= gap < MIN_GAP_S:
            time.sleep(MIN_GAP_S - gap)
            _last[0] = time.monotonic()
    for name, it in list(_items.items()):
        try:
            rev = said.get(name)
            it["heard"](svc, rev if isinstance(rev, str) and 0 < len(rev) <= REV_CAP else None)
        except Exception:
            log.debug("grok-bot-twin: item %s failed on what it heard", name, exc_info=True)
