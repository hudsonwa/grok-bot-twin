"""Attacks on the share server, the link code and the signed requests (the threat model's tests).

Who attacks: another device on the home network or tailnet that can listen to, record and send traffic; someone
who got hold of a link code; a computer that was linked and has been removed. What each test tries is said in its
name and first lines. The machine that shares is this test process (its share server on loopback, a free port).
Invented twins only.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import socket
import threading
import time

import pytest

from fleet_testlib import SCHEMA, next_count, wait_until, wire, wire_link, wire_sync, wire_unlink
from twin_testlib import central, config, home, service  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import fleet, fleet_follow as follow, fleet_net as net
from hermes_plugins.grok_bot_twin.twin import fleet_server as server, tasks_hub


@pytest.fixture
def sharing(home, tmp_path):  # noqa: F811
    """This process as the computer that shares two invented twins, listening on loopback."""
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "share_address": "127.0.0.1",
                         "share_port": 0, "machine_name": "mini"})
    data = central(home)
    data.mkdir(parents=True, exist_ok=True)
    (data / "pairs.json").write_text(json.dumps({"pairs": [
        {"pair_id": "pair-scout", "grok": {"id": "b-1", "name": "Test Scout", "description": "Finds trails."},
         "hermes": {"profile": "test-scout"}},
        {"pair_id": "pair-quill", "grok": {"id": "b-2", "name": "Test Quill", "description": "Drafts short notes."},
         "hermes": {"profile": "test-quill"}}]}), encoding="utf-8")
    svc = service.get_service()
    f = svc.fleet_share(True)
    assert f["share"]["state"] == "on"
    yield svc, f["share"]["port"], f["share"]["code"]["code"]
    server.stop()


def _signed(path: str, token: str, body: dict, nonce: str = "") -> tuple:
    """(raw body, headers) of one signed request, as a linked computer sends it and as a listener records it."""
    mid, pass_ = token.split("|", 1)
    data = json.dumps(body).encode("utf-8")
    nonce = nonce or fleet.new_nonce()
    return data, {"X-GBT-Machine": mid, "X-GBT-Nonce": nonce,
                  "X-GBT-Mac": fleet.request_mac(fleet.share_key(pass_), path, nonce, data)}


def _sync_body(name: str = "Probe", **more) -> dict:
    return {"proto": fleet.PROTO, "have": None, "wait_s": 0, "machine": {"name": name, "platform": "linux"},
            "applied": {}, "n": next_count(), **more}


# ---- the link code ---------------------------------------------------------------------------------------------------
def _first(port: int, code: str, mid: str = "m-aaaaaaaaaaaa", name: str = "Probe") -> dict:
    """The first message of a linking, sent as a machine that believes the code is `code`. Returns everything
    that side knows afterwards (and, under "said" and "heard", everything a listener saw)."""
    nonce = fleet.new_nonce()
    secret, public = fleet.dh_new(fleet.link_base(fleet.link_key(net.normalize_code(code))))
    said = {"proto": fleet.PROTO, "machine": {"id": mid, "name": name, "platform": "linux"}, "nonce": nonce, "dh": public}
    status, heard, raw = wire("POST", port, "/v1/link", said)
    out = {"status": status, "said": said, "heard": heard, "raw": raw, "nonce": nonce, "mid": mid, "secret": secret}
    if status == 200:
        out["proof"], out["confirm"], out["pass"] = fleet.link_keys(
            fleet.dh_shared(secret, heard["dh"]), nonce, heard["nonce"], mid, heard["source"]["id"], public, heard["dh"])
    return out


def _second(port: int, first: dict, confirm=None) -> tuple:
    return wire("POST", port, "/v1/link", {"proto": fleet.PROTO, "machine": first["said"]["machine"],
                                           "nonce": first["nonce"],
                                           "confirm": first["confirm"] if confirm is None else confirm})[:2]


def central_path():
    from hermes_plugins.grok_bot_twin.twin import store
    return store.central_dir()


def test_a_recorded_linking_is_worthless_even_to_someone_who_later_has_the_code(sharing):
    """The listener records everything both machines said while linking. Afterwards it also gets the link code
    (read off a screenshot, say). It still cannot make the pass: that needs the secret the two machines arrived
    at, which never travelled. And nothing that travelled can be used to find the code by trying them all."""
    svc, port, code = sharing
    real = _first(port, code)
    assert real["status"] == 200 and fleet.same(real["heard"]["proof"], real["proof"])  # the source knows the code
    assert _second(port, real)[0] == 200
    assert wire_sync(port, real["mid"] + "|" + real["pass"])[0] == 200  # (the real machine is linked)
    # the first message carries no proof at all: nothing in it depends on the code in a way that can be checked
    assert set(real["said"]) == {"proto", "machine", "nonce", "dh"}
    assert set(real["heard"]) == {"schema", "proto", "nonce", "dh", "proof", "source"}

    # what the listener has: both messages, both answers, and now the code. Every pass it can build from those:
    said, heard = real["said"], real["heard"]
    base = fleet.link_base(fleet.link_key(net.normalize_code(code)))
    eve_secret, eve_public = fleet.dh_new(base)
    parts = (said["nonce"], heard["nonce"], real["mid"], heard["source"]["id"], said["dh"], heard["dh"])
    shared_guesses = [fleet.dh_shared(eve_secret, said["dh"]), fleet.dh_shared(eve_secret, heard["dh"]),
                      hashlib.sha256((said["dh"] + heard["dh"]).encode()).hexdigest(),
                      hashlib.sha256(fleet.link_key(net.normalize_code(code))).hexdigest(),
                      fleet.link_key(net.normalize_code(code)).hex(), heard["proof"], real["confirm"],
                      fleet.dh_shared(2, format(pow(int(said["dh"], 16) * int(heard["dh"], 16), 1, fleet._DH_P), "x"))]
    guesses = [fleet.link_keys(g, *parts)[2] for g in shared_guesses]
    assert real["pass"] not in guesses
    for guess in guesses:
        status, obj, _ = wire_sync(port, real["mid"] + "|" + guess)
        assert status == 401 and obj["error"] == {"code": "unlinked"}
    # and the code itself is used up: it links nobody else
    status, obj, _ = wire_link(port, code, "m-bbbbbbbbbbbb", "Eve")
    assert (status, obj["error"]["code"]) == (403, "code_used")
    assert [m["name"] for m in svc.fleet()["machines"]] == ["mini", "Probe"]
    # nothing that would help was stored: the secret number is nowhere, the pass is nowhere
    stored = (central_path() / "fleet.json").read_text(encoding="utf-8")
    assert real["pass"] not in stored and format(real["secret"], "x") not in stored
    assert real["pass"] not in real["raw"].decode()


def test_the_key_of_a_link_code_is_slow_and_memory_hungry_to_make():
    """Even the one guess that an attempt allows costs 16 MB and a noticeable fraction of a second; the old
    fast digest is gone."""
    code = net.make_code("192.168.1.20")
    assert fleet._SCRYPT["n"] * fleet._SCRYPT["r"] * 128 >= 16 * 1024 * 1024
    key = fleet.link_key(code)
    assert len(key) == 32 and key != hashlib.sha256(b"grok-bot-twin link\n" + code.encode()).digest()
    assert key == hashlib.scrypt(code.encode(), salt=b"grok-bot-twin link 3", n=fleet._SCRYPT["n"], r=8, p=1, dklen=32)
    fleet.link_key.cache_clear()
    t0 = time.perf_counter()
    for other in ("2" * 12, "3" * 12, "4" * 12):
        fleet.link_key(other)
    per_try = (time.perf_counter() - t0) / 3
    assert per_try > 0.005, per_try  # (a plain digest takes a millionth of a second; this is ten thousand times that)
    # the number both sides start from: in the large subgroup, different for every code, never a trivial one
    q = (fleet._DH_P - 1) // 2
    bases = {fleet.link_base(fleet.link_key(c)) for c in ("2" * 12, "3" * 12, "4" * 12, code)}
    assert len(bases) == 4 and all(2 < g < fleet._DH_P - 1 and pow(g, q, fleet._DH_P) == 1 for g in bases)


def test_the_number_both_sides_calculate_with_is_the_published_2048_bit_group():
    """The modulus is RFC 3526's group 14, a safe prime: p = 2^2048 - 2^1984 - 1 + 2^64 * (floor(2^1918 * pi) +
    124476). It is worked out here from that formula (pi from Machin's formula, in whole numbers), so one
    mistyped digit in the constant cannot go unnoticed; and both p and (p - 1) / 2 are tested for being prime."""
    one = 1 << (1918 + 64)

    def arctan_inv(x: int) -> int:
        total = term = one // x
        n, sign = 3, -1
        while term:
            term //= x * x
            total += sign * (term // n)
            n, sign = n + 2, -sign
        return total

    pi = (4 * (4 * arctan_inv(5) - arctan_inv(239))) >> 64
    assert hex(pi >> (1918 - 32)) == "0x3243f6a88"  # (pi itself came out right: 3.243F6A88... in hex)
    assert fleet._DH_P == (1 << 2048) - (1 << 1984) - 1 + (1 << 64) * (pi + 124476)

    def probably_prime(n: int) -> bool:
        d, s = n - 1, 0
        while d % 2 == 0:
            d, s = d // 2, s + 1
        for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
            x = pow(a, d, n)
            if x in (1, n - 1):
                continue
            for _ in range(s - 1):
                x = pow(x, 2, n)
                if x == n - 1:
                    break
            else:
                return False
        return True

    assert probably_prime(fleet._DH_P) and probably_prime((fleet._DH_P - 1) // 2)
    assert not probably_prime(fleet._DH_P + 2) and not probably_prime((1 << 2048) - 1)  # (the test can say no)


def test_whoever_does_not_know_the_code_gets_one_guess_per_attempt_and_five_attempts(sharing):
    """Posing as a new machine with a guessed code: the source's answer tells the guesser only that this one
    guess was wrong (its proof fits no other code the guesser could try without starting again), the guesser's
    own proof is refused, and after five attempts the code is void, also for whoever knows it."""
    svc, port, code = sharing
    answers = []
    for i in range(7):
        guess = net.pretty_code(net.make_code("127.0.0.1"))
        attempt = _first(port, guess, f"m-{i:012x}", "Eve")
        if attempt["status"] != 200:
            answers.append(attempt["heard"]["error"]["code"])
            continue
        assert not fleet.same(attempt["heard"]["proof"], attempt["proof"])  # "your guess was wrong", nothing more
        status, obj = _second(port, attempt)
        answers.append(obj["error"]["code"])
        assert _second(port, attempt)[0] == 400  # one proof per attempt: a second one is not even compared
    assert answers == ["code_wrong"] * 4 + ["too_many_tries"] * 3
    status, obj, _ = wire_link(port, code)  # even the right code is void now: the owner makes a new one
    assert (status, obj["error"]["code"]) == (403, "too_many_tries")
    assert len(svc.fleet()["machines"]) == 1
    # attempts that are started and never finished count just the same (nothing is learned from them either)
    code2 = svc.fleet_code()["code"]["code"]
    for i in range(5):
        assert _first(port, net.pretty_code(net.make_code("127.0.0.1")), f"m-{i:012x}", "Eve")["status"] == 200
    assert _first(port, code2)["heard"]["error"]["code"] == "too_many_tries"
    # a proof without a first message, or with another machine's nonce, is not looked at
    code3 = svc.fleet_code()["code"]["code"]
    stray = {"said": {"machine": {"id": "m-eeeeeeeeeeee", "name": "Eve", "platform": "linux"}}, "nonce": "ab" * 16}
    assert _second(port, stray, "0" * 64)[0] == 400
    assert fleet.load()["share"]["code"]["tries"] == 0
    # two computers start with the same valid code at the same moment: the first to prove itself is linked, the
    # other is told the code was used (each code links one computer)
    code4 = svc.fleet_code()["code"]["code"]
    one, two = _first(port, code4, "m-111111111111", "One"), _first(port, code4, "m-222222222222", "Two")
    assert _second(port, two)[0] == 200
    status, obj = _second(port, one)
    assert (status, obj["error"]["code"]) == (403, "code_used")
    assert [m["name"] for m in svc.fleet()["machines"]] == ["mini", "Two"]
    svc.fleet_remove_machine("m-222222222222", False)
    code3 = svc.fleet_code()["code"]["code"]
    # a used code (read over a shoulder, found in a chat) links nobody
    assert wire_link(port, code3, "m-aaaaaaaaaaaa", "Surface")[0] == 200
    status, obj, _ = wire_link(port, code3, "m-eeeeeeeeeeee", "Eve")
    assert (status, obj["error"]["code"]) == (403, "code_used")
    assert [m["name"] for m in svc.fleet()["machines"]] == ["mini", "Surface"]


def test_someone_in_between_cannot_swap_the_exchanged_values(sharing):
    """The attacker sits between the two machines and replaces a value with its own (it does not know the code).
    The source then expects a proof nobody can give: the real machine's does not fit the swapped value, and the
    attacker cannot make one. Nobody is linked, and the attempt cost one try."""
    svc, port, code = sharing
    real_base = fleet.link_base(fleet.link_key(net.normalize_code(code)))
    nonce, mid = fleet.new_nonce(), "m-aaaaaaaaaaaa"
    secret, public = fleet.dh_new(real_base)                    # the real new machine's value ...
    eve_secret, eve_public = fleet.dh_new()                     # ... is replaced by the attacker's on the way
    machine = {"id": mid, "name": "Probe", "platform": "linux"}
    status, heard, _ = wire("POST", port, "/v1/link", {"proto": fleet.PROTO, "machine": machine, "nonce": nonce,
                                                        "dh": eve_public})
    assert status == 200
    # the real machine works with what it sent itself: its proof does not fit what the source saw
    _proof, real_confirm, _pass = fleet.link_keys(fleet.dh_shared(secret, heard["dh"]), nonce, heard["nonce"], mid,
                                                  heard["source"]["id"], public, heard["dh"])
    # the attacker works with its own secret: without the code its number is not a power of the code's number
    _p, eve_confirm, _pw = fleet.link_keys(fleet.dh_shared(eve_secret, heard["dh"]), nonce, heard["nonce"], mid,
                                           heard["source"]["id"], eve_public, heard["dh"])
    first = {"said": {"machine": machine}, "nonce": nonce}
    status, obj = _second(port, first, eve_confirm)
    assert (status, obj["error"]["code"]) == (403, "code_wrong")
    assert _second(port, first, real_confirm)[0] == 400         # (and that exchange is over)
    assert len(svc.fleet()["machines"]) == 1 and fleet.load()["share"]["code"]["tries"] == 1
    # a value sent straight back (the source's own, as if it were the new machine's) is refused as well
    status, heard, _ = wire("POST", port, "/v1/link", {"proto": fleet.PROTO, "machine": machine,
                                                        "nonce": fleet.new_nonce(), "dh": heard["dh"]})
    assert status == 200 and heard["dh"] != eve_public
    assert wire_link(port, code)[0] == 200  # the owner's own machine still links with the same code


def _fake_source(code: str, change=None, second=None):
    """What `follow._request` reaches when a machine at the code's address answers the linking. It knows `code`
    (which may differ from the one typed on this computer). `change(answer)` alters its first answer on the way;
    `second` replaces its second answer as (status, object, signed)."""
    state: dict = {}

    def request(host, port, path, body, key=None, machine_id="", timeout=10.0, holder=None, proof_key=None,
                nonce=None):
        assert path == "/v1/link"
        state.setdefault("bodies", []).append(body)
        if "confirm" in body:
            if second is not None:
                return second
            ok = fleet.same(body["confirm"], state["confirm"])
            return (200, {"schema": SCHEMA, "proto": fleet.PROTO, "linked": True}, True) if ok else \
                (403, {"schema": SCHEMA, "error": {"code": "code_wrong"}}, False)
        mid, source_id = body["machine"]["id"], "m-3f9a1c0b7d2e"
        secret, public = fleet.dh_new(fleet.link_base(fleet.link_key(net.normalize_code(code))))
        source_nonce = fleet.new_nonce()
        proof, state["confirm"], _pass = fleet.link_keys(fleet.dh_shared(secret, body["dh"]), body["nonce"],
                                                         source_nonce, mid, source_id, body["dh"], public)
        answer = {"schema": SCHEMA, "proto": fleet.PROTO, "nonce": source_nonce, "dh": public, "proof": proof,
                  "source": {"id": source_id, "name": "mini", "platform": "macos"}}
        if change is not None:
            answer = change(answer, body)
        return 200, answer, False

    request.state = state
    return request


def test_the_new_computer_links_only_to_a_machine_that_proves_it_knows_the_code(home, tmp_path, monkeypatch):  # noqa: F811
    """A machine at the address the code carries answers the linking. Unless it proves that it started from the
    very code typed here, this computer ends up not linked, and it never sends its own proof (which is all an
    impostor could use for its one guess)."""
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "machine_name": "Surface"})
    svc = service.get_service()
    monkeypatch.setattr(follow, "ensure_started", lambda *a, **k: False)
    monkeypatch.setattr(follow, "sync_once", lambda *a, **k: "offline")
    code = net.pretty_code(net.make_code("100.64.0.10"))
    other = net.pretty_code(net.make_code("100.64.0.10"))

    def refused(fake, want):
        monkeypatch.setattr(follow, "_request", fake)
        with pytest.raises(Exception) as exc:
            svc.fleet_link(code)
        assert exc.value.code == want and fleet.load()["link"] is None
        return fake.state["bodies"]

    # it knows another code (an impostor, or the owner typed a code that is not the one showing there)
    bodies = refused(_fake_source(other), "code_wrong")
    assert len(bodies) == 1 and "confirm" not in bodies[0] and set(bodies[0]) == {"proto", "machine", "nonce", "dh"}
    # its answer was changed on the way: another value, a value sent straight back, values that make the secret
    # guessable, or no number at all
    eve = fleet.dh_new()[1]
    assert len(refused(_fake_source(code, lambda a, b: {**a, "dh": eve}), "code_wrong")) == 1
    assert len(refused(_fake_source(code, lambda a, b: {**a, "dh": b["dh"]}), "unreachable")) == 1
    for bad in ("0", "1", format(fleet._DH_P - 1, "x"), format(fleet._DH_P, "x"), "f" * 513, "zz", "", None, 7,
                format(fleet._DH_P - 2, "X")):
        assert len(refused(_fake_source(code, lambda a, b, bad=bad: {**a, "dh": bad}), "unreachable")) == 1, bad
    assert len(refused(_fake_source(code, lambda a, b: {**a, "proof": "0" * 64}), "code_wrong")) == 1
    assert len(refused(_fake_source(code, lambda a, b: {k: v for k, v in a.items() if k != "proof"}), "code_wrong")) == 1
    assert len(refused(_fake_source(code, lambda a, b: {**a, "source": {"id": "nope"}}), "unreachable")) == 1
    # the last answer must be signed with the key the two just made, and must say "linked"
    ok = {"schema": SCHEMA, "proto": fleet.PROTO, "linked": True}
    assert len(refused(_fake_source(code, second=(200, ok, False)), "unreachable")) == 2
    assert len(refused(_fake_source(code, second=(200, {**ok, "linked": False}, True)), "unreachable")) == 2
    assert len(refused(_fake_source(code, second=(403, {"schema": SCHEMA, "error": {"code": "code_expired"}}, False)),
                       "code_expired")) == 2
    # and with all of it right, it links
    fake = _fake_source(code)
    monkeypatch.setattr(follow, "_request", fake)
    assert svc.fleet_link(code)["role"] == "follower"
    link = fleet.load()["link"]
    assert link["source"]["name"] == "mini" and len(link["pass"]) == 64
    sent = json.dumps(fake.state["bodies"])
    assert link["pass"] not in sent and code.replace("-", "") not in sent


def test_unusable_exchange_values_are_refused_before_the_code_is_even_looked_at(sharing):
    """0, 1 and p-1 would make the shared secret one of two or three known numbers. They, and anything that is
    not a number of the right size, are refused as a bad request: no try is used up, nobody is linked."""
    svc, port, code = sharing
    mid = "m-aaaaaaaaaaaa"
    for bad in ("0", "1", format(fleet._DH_P - 1, "x"), format(fleet._DH_P, "x"), format(fleet._DH_P + 5, "x"),
                "f" * 513, "0x12", "-5", "zz", "", None, 12, ["2"], format(fleet._DH_P - 2, "X")):
        status, obj, _ = wire("POST", port, "/v1/link", {"proto": fleet.PROTO, "nonce": fleet.new_nonce(), "dh": bad,
                                                          "machine": {"id": mid, "name": "Probe", "platform": "linux"}})
        assert (status, obj["error"]["code"]) == (400, "bad_request"), bad
    assert fleet.load()["share"]["code"]["tries"] == 0 and len(svc.fleet()["machines"]) == 1
    status, obj, _ = wire_link(port, code)  # the code still works for the real computer
    assert status == 200 and obj["proved"] is True


# ---- recorded requests -----------------------------------------------------------------------------------------------
def test_a_recorded_request_sent_again_changes_nothing(sharing):
    """The listener records a signed request of a linked computer and sends the same bytes again later: to put
    the computer's old name back, to make a computer that is off look as if it had just checked in, or to unlink
    it a second time. A signed request counts once."""
    svc, port, code = sharing
    status, linked, _ = wire_link(port, code, "m-aaaaaaaaaaaa", "Old name")
    token = linked["pass"]
    data, hdrs = _signed("/v1/sync", token, _sync_body("Old name"))
    assert wire("POST", port, "/v1/sync", raw_body=data, headers=hdrs)[0] == 200  # the real request, recorded
    data2, hdrs2 = _signed("/v1/sync", token, _sync_body("New name"))
    assert wire("POST", port, "/v1/sync", raw_body=data2, headers=hdrs2)[0] == 200
    assert svc.fleet()["machines"][1]["name"] == "New name"
    seen_before = fleet.load()["share"]["followers"]["m-aaaaaaaaaaaa"]["last_seen_at"]

    answer: dict = {}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/v1/sync", body=data, headers={"Content-Type": "application/json", **hdrs})  # sent again
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    assert resp.status == 409 and json.loads(raw)["error"] == {"code": "replayed"}
    assert b"Test Scout" not in raw  # and it is not handed the list
    st = fleet.load()
    assert st["share"]["followers"]["m-aaaaaaaaaaaa"]["name"] == "New name"
    assert st["share"]["followers"]["m-aaaaaaaaaaaa"]["last_seen_at"] == seen_before
    # the same recording against another route is no better: the signature covers the path
    assert wire("POST", port, "/v1/unlink", raw_body=data, headers=hdrs)[0] == 401
    assert "m-aaaaaaaaaaaa" in fleet.load()["share"]["followers"]
    # an unlink request, recorded and sent again after the owner linked that computer anew: the new link stands
    bye, bye_hdrs = _signed("/v1/unlink", token, {"proto": fleet.PROTO, "n": next_count()})
    assert wire("POST", port, "/v1/unlink", raw_body=bye, headers=bye_hdrs)[0] == 200
    code2 = svc.fleet_code()["code"]["code"]
    assert wire_link(port, code2, "m-aaaaaaaaaaaa", "Back again")[0] == 200
    assert wire("POST", port, "/v1/unlink", raw_body=bye, headers=bye_hdrs)[0] == 401  # (signed with the old key)
    assert fleet.load()["share"]["followers"]["m-aaaaaaaaaaaa"]["name"] == "Back again"
    del answer


def _again(port: int, path: str, data: bytes, hdrs: dict) -> tuple:
    status, obj, raw = wire("POST", port, path, raw_body=data, headers=hdrs)
    return status, obj, raw


def test_a_recording_is_still_refused_after_hermes_was_started_again_and_however_old_it_is(sharing):
    """The calls for the twin list carry no time (a linked computer with a wrong clock must still follow), so
    what makes a recording worthless for good is the call number inside the signed body: the sharing computer
    keeps the highest one it has had from each linked computer in its file."""
    svc, port, code = sharing
    token = wire_link(port, code, "m-aaaaaaaaaaaa", "Old name")[1]["pass"]
    old, old_hdrs = _signed("/v1/sync", token, _sync_body("Old name"))
    assert wire("POST", port, "/v1/sync", raw_body=old, headers=old_hdrs)[0] == 200  # the real request, recorded
    new, new_hdrs = _signed("/v1/sync", token, _sync_body("New name"))
    assert wire("POST", port, "/v1/sync", raw_body=new, headers=new_hdrs)[0] == 200
    before = fleet.load()["share"]["followers"]["m-aaaaaaaaaaaa"]
    assert before["name"] == "New name" and before["n"] == json.loads(new)["n"]
    # Hermes on the sharing computer is started again (or half an hour passes): what it remembered in memory is gone
    tasks_hub._nonces.clear()
    for data, hdrs in ((old, old_hdrs), (new, new_hdrs)):
        status, obj, raw = _again(port, "/v1/sync", data, hdrs)
        assert status == 409 and obj["error"] == {"code": "replayed"} and obj["had"] == before["n"]
        assert b"Test Scout" not in raw  # it is not handed the list
        assert fleet.load()["share"]["followers"]["m-aaaaaaaaaaaa"] == before  # no old name, no "seen just now"
    # the real computer goes on: its next call has a higher number
    assert wire_sync(port, token, name="New name")[0] == 200
    # a call without a number, or with something that is not one, is refused before it changes anything
    seen = fleet.load()["share"]["followers"]["m-aaaaaaaaaaaa"]
    for bad in ({}, {"n": None}, {"n": 0}, {"n": -4}, {"n": True}, {"n": "7"}, {"n": 1.5}, {"n": 2 ** 62}):
        body = {k: v for k, v in _sync_body("Other name").items() if k != "n"}
        data, hdrs = _signed("/v1/sync", token, {**body, **bad})
        status, obj, _raw = _again(port, "/v1/sync", data, hdrs)
        assert (status, obj["error"]["code"]) == (400, "bad_request"), bad
        data, hdrs = _signed("/v1/unlink", token, {"proto": fleet.PROTO, **bad})
        assert _again(port, "/v1/unlink", data, hdrs)[0] == 400, bad
    assert fleet.load()["share"]["followers"]["m-aaaaaaaaaaaa"] == seen
    # "unlink me", recorded while the computer was linked and never delivered, is as worthless later
    bye, bye_hdrs = _signed("/v1/unlink", token, {"proto": fleet.PROTO, "n": seen["n"]})
    assert _again(port, "/v1/unlink", bye, bye_hdrs)[0] == 409
    assert "m-aaaaaaaaaaaa" in fleet.load()["share"]["followers"]


def test_a_linked_computer_whose_clock_was_set_back_still_follows(sharing, monkeypatch):
    """Its call numbers come from its own clock. When that clock is set back they are lower than what the sharing
    computer has had; the signed refusal says the number had, and the same call goes out once more above it."""
    svc, port, code = sharing
    mid = "m-aaaaaaaaaaaa"
    token = wire_link(port, code, mid, "Surface")[1]["pass"]
    assert wire_sync(port, token, name="Surface")[0] == 200
    had = fleet.load()["share"]["followers"][mid]["n"]
    ln = {"host": "127.0.0.1", "port": port, "pass": token.split("|", 1)[1]}
    monkeypatch.setattr(follow, "_counted", [0])
    monkeypatch.setattr(follow.time, "time", lambda: 946_684_800.0)  # the year 2000, by this computer's clock
    sent = []
    real = follow._request

    def spy(host, port_, path, body, **kw):
        sent.append(body.get("n"))
        return real(host, port_, path, body, **kw)

    monkeypatch.setattr(follow, "_request", spy)
    status, obj, signed = follow._counted_request(ln, mid, "/v1/sync", _sync_body("Surface"))
    assert (status, signed) == (200, True) and obj["changed"] is True
    assert sent == [946_684_800_000, had + 1]
    assert fleet.load()["share"]["followers"][mid]["n"] == had + 1
    # an answer that is not signed by the sharing computer cannot push the numbers anywhere
    monkeypatch.setattr(follow, "_request", lambda *a, **k: (409, {"schema": SCHEMA, "error": {"code": "replayed"},
                                                                    "had": 2 ** 61}, False))
    assert follow._counted_request(ln, mid, "/v1/sync", _sync_body("Surface"))[0] == 409
    assert follow._counted[0] == had + 2


def test_the_memory_of_seen_requests_is_never_emptied_in_one_go():
    """A linked computer that sends a flood of requests must not make the sharing computer forget the requests
    of the others (which would let recordings of them through again)."""
    tasks_hub._nonces.clear()
    assert tasks_hub.fresh("m-aaaaaaaaaaaa", "victim-nonce") is True
    for i in range(20000):
        tasks_hub.fresh("m-bbbbbbbbbbbb", f"{i:032x}")
    recent = f"{19999:032x}"
    assert tasks_hub.fresh("m-bbbbbbbbbbbb", recent) is False  # the newest are all still known
    assert 4096 < len(tasks_hub._nonces) <= 16385
    tasks_hub._nonces.clear()


# ---- a computer that was removed, or never linked ---------------------------------------------------------------------
def test_a_removed_computer_is_refused_at_its_next_request_and_gets_no_names(sharing):
    svc, port, code = sharing
    token = wire_link(port, code, "m-aaaaaaaaaaaa", "Surface")[1]["pass"]
    assert wire_sync(port, token)[0] == 200
    svc.fleet_remove_machine("m-aaaaaaaaaaaa", False)
    got: dict = {}
    status, obj, raw = wire_sync(port, token, answer=got)
    assert status == 401 and obj["error"] == {"code": "unlinked"} and got["signed"] is True
    assert b"Test" not in raw and b"mini" not in raw
    for path, body in (("/v1/task/ask", {"proto": fleet.PROTO, "at": int(time.time()), "to": "m-aaaaaaaaaaaa",
                                         "twin": "t-0123456789abcdef", "text": "hello"}),
                       ("/v1/task/poll", {"proto": fleet.PROTO, "at": int(time.time()), "accept": True, "wait_s": 0})):
        data, hdrs = _signed(path, token, body)
        status, obj, raw = wire("POST", port, path, raw_body=data, headers=hdrs)
        assert status == 401 and obj["error"] == {"code": "unlinked"}
    # a computer nobody linked, with every header a linked one sends
    data, hdrs = _signed("/v1/sync", "m-ffffffffffff|" + "0" * 64, _sync_body())
    status, obj, raw = wire("POST", port, "/v1/sync", raw_body=data, headers=hdrs)
    assert status == 401 and b"Test" not in raw


# ---- the sharing computer's own secret ---------------------------------------------------------------------------------
def test_the_instance_secret_is_worth_nothing_from_another_computer(sharing, monkeypatch):
    """Hermes processes on the sharing computer itself show a secret from its owner-only state file instead of a
    signature. Should that secret ever get out, it must not open anything from the network: it is looked at only
    when the request comes from one of this computer's own addresses."""
    svc, port, code = sharing
    secret = fleet.load()["share"]["instance"]["secret"]
    assert wire("POST", port, "/v1/poke", {"ping": True}, token=secret)[0] == 204  # from this computer: accepted
    body = {"proto": fleet.PROTO, "accept": False, "wait_s": 0}
    assert wire("POST", port, "/v1/task/poll", body, token=secret)[0] == 200
    monkeypatch.setattr(net, "is_own_address", lambda address: False)  # the same bytes, from somewhere else
    assert wire("POST", port, "/v1/poke", {"ping": True}, token=secret)[0] == 401
    status, obj, _ = wire("POST", port, "/v1/task/poll", body, token=secret)
    assert status == 401 and obj["error"] == {"code": "unlinked"}
    status, obj, _ = wire("POST", port, "/v1/task/ask", {"proto": fleet.PROTO, "to": "m-aaaaaaaaaaaa",
                                                          "twin": "t-0123456789abcdef", "text": "hello"}, token=secret)
    assert status == 401


def test_the_instance_secret_is_never_sent_to_an_address_that_is_not_this_computers(monkeypatch):
    """The state file names the address the serving process listened on. After this computer moved to another
    network that address may belong to anybody: nothing is sent there, least of all the secret."""
    def no_connection(*a, **k):
        raise AssertionError("a connection was opened to an address that is not this computer's")

    monkeypatch.setattr(server.http.client, "HTTPConnection", no_connection)
    for address in ("10.255.254.253", "192.168.251.77", "100.64.0.77"):
        if net.is_own_address(address):
            continue
        assert server._poke({"address": address, "port": 18777, "secret": "s3cret-of-this-computer", "pid": 1}) is False
    assert net.is_own_address("127.0.0.1") is True and net.is_own_address("8.8.8.8") is False
    assert net.is_own_address("not an address") is False and net.is_own_address(None) is False


# ---- where the server listens, and how much it takes --------------------------------------------------------------------
def test_the_share_server_never_listens_on_every_interface_or_a_public_address():
    for address in ("0.0.0.0", "8.8.8.8", "::", "::1", "169.254.1.1", "224.0.0.1", "", "localhost"):
        assert net.share_address(address) is None or address == "", address
        assert net.kind_of(address) is None
    for peer in ("8.8.8.8", "1.2.3.4", "169.254.10.10", "::1", "fe80::1"):
        assert server._Http.verify_request(None, None, (peer, 4000)) is False  # dropped before it is read
    for peer in ("127.0.0.1", "100.100.1.2", "192.168.1.5", "10.0.0.9", "172.16.3.3"):
        assert server._Http.verify_request(None, None, (peer, 4000)) is True
    for code_address in ("8.8.8.8", "1.1.1.1"):  # a link code can never point a computer at the internet
        assert net.code_address(net.make_code(code_address)) is None


def test_oversize_bodies_slow_senders_and_connection_floods_are_cut_off(sharing, monkeypatch):
    svc, port, code = sharing
    # a body over 16 KiB is refused unread, whatever its Content-Length claims
    assert wire("POST", port, "/v1/link", raw_body=b"{" + b" " * 17000 + b"}")[0] == 400
    assert wire("POST", port, "/v1/sync", raw_body=b"x", headers={"Content-Length": "999999999"})[0] == 400
    assert wire("POST", port, "/v1/sync", raw_body=b"x", headers={"Content-Length": "-5"})[0] == 400
    # deeply nested JSON does not take the server down
    assert wire("POST", port, "/v1/link", raw_body=b"[" * 8000 + b"]" * 8000)[0] == 400
    assert wire("GET", port, "/v1/hello")[0] == 200
    # a flood of idle connections from one address: the ninth is closed unread, and nothing is held afterwards
    idle = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(server.MAX_PER_ADDRESS)]
    try:
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        extra.settimeout(5)
        try:
            extra.sendall(b"GET /v1/hello HTTP/1.1\r\nHost: x\r\n\r\n")
            got = extra.recv(100)
        except OSError:
            got = b""
        assert got == b""
        extra.close()
    finally:
        for s in idle:
            s.close()
    wait_until(lambda: wire("GET", port, "/v1/hello")[0] == 200, what="the server to answer again")
    # a sender that trickles its request in is cut off after the limit for a whole request
    monkeypatch.setattr(server, "REQUEST_READ_S", 0.6)
    slow = socket.create_connection(("127.0.0.1", port), timeout=5)
    slow.settimeout(5)
    slow.sendall(b"POST /v1/sync HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{")
    t0 = time.monotonic()
    try:
        rest = slow.recv(4096)
    except OSError:
        rest = b""
    assert time.monotonic() - t0 < 4 and b"Test Scout" not in rest
    slow.close()
    assert wire("GET", port, "/v1/hello")[0] == 200


def test_errors_and_the_hello_say_nothing_about_the_twins_or_this_computer(sharing):
    svc, port, code = sharing
    said = []
    said.append(wire("GET", port, "/v1/hello")[2])
    said.append(wire("GET", port, "/v1/sync")[2])
    said.append(wire("POST", port, "/v1/link", {"proto": fleet.PROTO})[2])
    said.append(wire("POST", port, "/v1/link", {"proto": 2, "machine": {"id": "m-aaaaaaaaaaaa"}})[2])
    said.append(wire_sync(port, "")[2])
    said.append(wire("POST", port, "/v1/task/ask", {"proto": fleet.PROTO, "to": "m-aaaaaaaaaaaa"})[2])
    said.append(wire("POST", port, "/v1/poke", {}, token="wrong")[2])
    everything = b"\n".join(said).decode("utf-8")
    for word in ("Test Scout", "Test Quill", "mini", "trails", "Traceback", "File \"", str(central_path())):
        assert word not in everything, word
