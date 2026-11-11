"""Privacy proof for sharing twins between machines: only the contract's allowlisted fields cross the wire.

The machine that shares is this test process (its share server on loopback, a free port). Its Hermes folder is
loaded with things that must never leave it, each carrying a marker: the Grok bot id, the twin's internal id, the
Hermes bot's name, pasted instructions, a skill, routine ids, the model settings, an .env file, chat files, the
plugin's own notes, and password-like text inside the twin's description. A raw client then links and reads
everything the server will say, and the test looks at the bytes.

The other direction is checked too: what a following machine sends about itself.
Invented data only.
"""
from __future__ import annotations

import json
import time

import pytest

from fleet_testlib import SCHEMA, free_port, key_problems, wait_until, wire, wire_link, wire_sync, wire_unlink
from twin_testlib import central, config, fake_key, home, service  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import fleet, fleet_follow as follow, fleet_server as server

MARK = "PRIVATE-MARKER-7c1e"
GROK_ID = f"b-{MARK}-grokid"
PAIR_ID = f"pair-{MARK}"
PROFILE = "private-profile-7c1e"
JOB_ID = f"job-{MARK}"


def _load_private_things(home, key: str) -> None:  # noqa: F811
    data = central(home)
    data.mkdir(parents=True, exist_ok=True)
    reg = {"pairs": [{
        "pair_id": PAIR_ID,
        "grok": {"id": GROK_ID, "name": "Test Scout", "account": f"acct-{MARK}",
                 "description": f"Finds trails.\x07\nService key: {key}\nMeets on Fridays."},
        "hermes": {"profile": PROFILE, "model": f"model-{MARK}"},
        "created_at": "2000-01-01T09:00:00Z", "job_ids": [JOB_ID],
        "switched": {"at": "2000-01-02T09:00:00Z", "job_ids": [JOB_ID]},
        "instructions": {"from": "paste", "digest": f"dig-{MARK}"},
        "model": {"from": "default", "value": f"model-{MARK}"},
        f"extra-{MARK}": {"anything": f"nested-{MARK}"},
    }]}
    (data / "pairs.json").write_text(json.dumps(reg), encoding="utf-8")
    (data / "kept.json").write_text(json.dumps({PAIR_ID: [{"id": "persona.instructions@0123456789ab", "at": "2000",
                                                         "text": f"instructions {MARK}"}]}), encoding="utf-8")
    (data / "events.jsonl").write_text(json.dumps({"type": "synced", "payload": {"note": f"event-{MARK}"}}) + "\n",
                                       encoding="utf-8")
    prof = home / "profiles" / PROFILE
    (prof / "skills" / "general" / f"skill-{MARK}").mkdir(parents=True)
    (prof / "skills" / "general" / f"skill-{MARK}" / "SKILL.md").write_text(f"# a skill {MARK}", encoding="utf-8")
    (prof / "sessions").mkdir()
    (prof / "sessions" / "chat.jsonl").write_text(f"a chat {MARK}", encoding="utf-8")
    (prof / "SOUL.md").write_text("<!-- botport:begin persona -->\nYou are " + MARK + " and you keep secrets.\n"
                                  "<!-- botport:end persona -->\n<!-- grok-bot-twin:begin instructions -->\n"
                                  f"pasted {MARK}\n<!-- grok-bot-twin:end instructions -->\n", encoding="utf-8")
    (prof / "config.yaml").write_text(f"model:\n  default: model-{MARK}\n  provider: provider-{MARK}\n", "utf-8")
    (prof / ".env").write_text(f"SOME_KEY={MARK}\n", encoding="utf-8")
    (home / ".env").write_text(f"ROOT_KEY={MARK}\n", encoding="utf-8")
    (home / "config.yaml").write_text(f"model:\n  default: model-{MARK}\n", encoding="utf-8")


@pytest.fixture
def sharing(home, tmp_path):  # noqa: F811
    """This process as the machine that shares, listening on loopback."""
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "share_address": "127.0.0.1",
                         "share_port": 0, "machine_name": "mini"})
    key = fake_key()
    _load_private_things(home, key)
    svc = service.get_service()
    svc.demo_action("start")  # the sample twin is on too: it must not be shared
    f = svc.fleet_share(True)
    assert f["share"]["state"] == "on" and f["share"]["network"] == "loopback"
    return svc, f["share"]["port"], f["share"]["code"]["code"], key


def test_only_the_allowlisted_fields_cross_the_wire(sharing, home):  # noqa: F811
    svc, port, code, key = sharing
    seen = []  # every byte the server said to anyone
    status, hello, raw = wire("GET", port, "/v1/hello")
    seen.append(raw)
    assert set(hello) == {"schema", "app", "proto", "sharing"}
    status, linked, raw = wire_link(port, code)
    seen.append(raw)
    assert status == 200 and set(json.loads(raw)) == {"schema", "proto", "nonce", "dh", "proof", "source"}  # no secret in it
    assert set(linked["source"]) == {"id", "name", "platform"} and linked["source"]["name"] == "mini"
    token = linked["pass"]
    status, lst, raw = wire_sync(port, token)
    seen.append(raw)
    assert status == 200 and lst["schema"] == SCHEMA and lst["changed"] is True
    # 1. the keys: exactly the contract's, at every level
    assert key_problems(lst) == []
    assert set(lst) == {"schema", "proto", "changed", "sharing", "rev", "generated_at", "source", "twins", "removed",
                        "fleet"}
    assert len(lst["twins"]) == 1  # the real twin; the sample twin is not shared
    twin = lst["twins"][0]
    assert set(twin) == {"id", "name", "description", "version", "updated_at"}
    assert twin["name"] == "Test Scout" and twin["id"].startswith("t-") and len(twin["id"]) == 18
    assert all(set(m) == {"id", "name", "platform", "role"} for m in lst["fleet"])
    # 2. the description went through the plugin's filter: the password-like line is hidden, the rest arrives
    assert "Finds trails." in twin["description"] and "Meets on Fridays." in twin["description"]
    assert key not in twin["description"] and "\x07" not in twin["description"]
    assert "[not shown: looks like a password or key]" in twin["description"]
    # 3. more traffic: unchanged, a removal, errors, unlink
    seen.append(wire_sync(port, token, have=lst["rev"])[2])
    reg = json.loads((central(home) / "pairs.json").read_text(encoding="utf-8"))
    (central(home) / "pairs.json").write_text(json.dumps({"pairs": []}), encoding="utf-8")
    fleet.publish(svc)
    status, after, raw = wire_sync(port, token, have=lst["rev"])
    seen.append(raw)
    assert key_problems(after) == [] and after["twins"] == [] and [r["id"] for r in after["removed"]] == [twin["id"]]
    assert set(after["removed"][0]) == {"id", "removed_at"}
    (central(home) / "pairs.json").write_text(json.dumps(reg), encoding="utf-8")
    fleet.publish(svc)
    seen.append(wire_sync(port, token, have=after["rev"])[2])
    seen.append(wire_sync(port, "not-the-pass-" * 3)[2])
    seen.append(wire_link(port, "AAAA-AAAA-AAAA", "m-bbbbbbbbbbbb")[2])
    seen.append(wire("POST", port, "/v1/nope", {})[2])
    seen.append(wire_unlink(port, token)[2])
    # 4. the bytes: none of the things that must stay on this machine
    everything = b"\n".join(seen).decode("utf-8")
    for secret in (MARK, key, GROK_ID, PAIR_ID, PROFILE, JOB_ID, str(home), "acme-support", "Acme Support",
                   "botport", "instructions", "skill", "SOUL", ".env", "profile", "grok_id", "pair_id"):
        assert secret not in everything, secret
    # 5. the same list, read off the state the server answers from: nothing else is even kept next to it
    stored = fleet.load()["share"]["list"]["twins"][twin["id"]]
    assert set(stored) == {"name", "description", "version", "updated_at", "pair_id", "profile"}
    assert set(fleet.wire_list(fleet.load())["twins"][0]) == {"id", "name", "description", "version", "updated_at"}


def test_extra_fields_in_the_registry_can_never_ride_along(sharing, home):  # noqa: F811
    """Whatever a later version (or a damaged file) puts into the registry entry or the stored list, the answer
    is built field by field."""
    svc, port, code, key = sharing
    with service.store.locked():
        st = fleet.load()
        for t in st["share"]["list"]["twins"].values():
            t[f"smuggled-{MARK}"] = MARK
            t["grok_id"] = GROK_ID
        st["share"]["list"][f"also-{MARK}"] = MARK
        st["share"][f"more-{MARK}"] = {"deep": MARK}
        st["share"]["followers"]["m-dddddddddddd"] = {"id": "m-dddddddddddd", "name": "Orchard", "platform": "linux",
                                                      "key": "0" * 64, f"note-{MARK}": MARK,
                                                      "applied": {"rev": None, f"x-{MARK}": MARK}}
        fleet.save(st)
    token = wire_link(port, code)[1]["pass"]
    status, lst, raw = wire_sync(port, token)
    assert status == 200 and key_problems(lst) == []
    assert MARK not in raw.decode("utf-8") and GROK_ID not in raw.decode("utf-8")
    assert [m["name"] for m in lst["fleet"]] == ["mini", "Orchard", "Probe"]  # machine names do travel: by design
    # the key of another machine is in no answer
    assert '"key"' not in raw.decode("utf-8") and "0" * 64 not in raw.decode("utf-8")


def test_what_a_following_machine_sends_about_itself(home, tmp_path, monkeypatch):  # noqa: F811
    """A follower sends: the link code and its own id, name and kind of computer once; afterwards its name, the
    list version it holds, per twin id a version number or one of two fixed codes, and a call number (its clock
    in milliseconds); and about routines: two switches, the list of routines it holds and the ids of the routines
    switched on there. Nothing of its Hermes."""
    key = fake_key()
    _load_private_things(home, key)  # this machine has things of its own that are nobody's business
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "machine_name": "Surface"})
    svc = service.get_service()
    sent = []
    twin_id = "t-0123456789abcdef"
    answers = {
        "/v1/sync": (200, {"schema": SCHEMA, "proto": fleet.PROTO, "changed": True, "sharing": True, "rev": "3-0a0b0c0d",
                           "generated_at": "2000-01-01T09:00:00Z",
                           "source": {"id": "m-3f9a1c0b7d2e", "name": "mini", "platform": "macos"},
                           "twins": [{"id": twin_id, "name": "Test Quill", "description": "Drafts short notes.",
                                      "version": 4, "updated_at": "2000-01-01T09:00:00Z"}],
                           "removed": [], "fleet": []}),
        "/v1/unlink": (200, {"schema": SCHEMA, "unlinked": True}),
    }

    from hermes_plugins.grok_bot_twin.twin import fleet_net
    code = fleet_net.pretty_code(fleet_net.make_code("100.64.0.10"))

    def fake_request(host, port, path, body, key=None, machine_id="", timeout=10.0, holder=None, proof_key=None,
                     nonce=None):
        sent.append((host, port, path, json.loads(json.dumps(body)), key))
        if path != "/v1/link":
            return (*answers[path], True)
        if "confirm" in body:  # the second message of the linking
            return 200, {"schema": SCHEMA, "proto": fleet.PROTO, "linked": True}, True
        # the first: the other computer knows the code too, and proves it
        src = {"id": "m-3f9a1c0b7d2e", "name": "mini", "platform": "macos"}
        secret, public = fleet.dh_new(fleet.link_base(fleet.link_key(fleet_net.normalize_code(code))))
        proof, _confirm, _pass = fleet.link_keys(fleet.dh_shared(secret, body["dh"]), body["nonce"], "ab" * 16,
                                                 body["machine"]["id"], src["id"], body["dh"], public)
        return 200, {"schema": SCHEMA, "proto": fleet.PROTO, "nonce": "ab" * 16, "dh": public, "proof": proof,
                     "source": src}, False

    monkeypatch.setattr(follow, "_request", fake_request)
    monkeypatch.setattr(follow, "ensure_started", lambda *a, **k: False)  # no background loop in this test
    f = svc.fleet_link(code)
    assert f["role"] == "follower" and f["twins"][0]["name"] == "Test Quill"
    assert follow.sync_once(svc) == "synced"
    svc.fleet_unlink(False)
    paths = [s[2] for s in sent]
    # (linking asks twice: once for the list, once more to say what it made of it)
    assert paths == ["/v1/link", "/v1/link", "/v1/sync", "/v1/sync", "/v1/sync", "/v1/unlink"]
    assert all(s[0] == "100.64.0.10" and s[1] == 18777 for s in sent)  # only ever the address the code carries
    link_body, link_proof, first, second, third, bye = (s[3] for s in sent)
    # linking sends one value of the exchange and then a proof of knowing the code, never the code
    assert set(link_body) == {"proto", "machine", "nonce", "dh"} and set(link_body["machine"]) == {"id", "name", "platform"}
    assert set(link_proof) == {"proto", "machine", "nonce", "confirm"} and len(link_proof["confirm"]) == 64
    assert link_body["machine"]["name"] == "Surface" and code.replace("-", "") not in json.dumps([link_body, link_proof])
    for body in (first, second, third):
        # ("xs": which further things this computer can take, each with nothing but the rev (list version) it was
        # last told. Here that is skills (twin/fleet_items.py) and the twins' settings (twin/xs_config.py), and it
        # was told nothing yet: the stand-in for the other computer never names one)
        # ("routines": what it holds of the routines that copy between the owner's computers; twin/routines.py)
        assert set(body) == {"proto", "have", "wait_s", "machine", "applied", "n", "xs", "routines"}
        assert body["xs"] == {"skills": "", "config": ""}
        assert set(body["routines"]) == {"v", "gen", "have", "on", "can_run", "applied", "running", "paused"}
        assert body["routines"]["running"] == [] and body["routines"]["paused"] == []
        assert body["routines"]["can_run"] is False
        assert set(body["machine"]) == {"name", "platform"} and set(body["applied"]) == {"rev", "at", "twins", "failed"}
    assert first["have"] is None and first["applied"]["twins"] == {}
    assert second["have"] == "3-0a0b0c0d" and second["applied"]["twins"] == {twin_id: 4}
    assert set(bye) == {"proto", "n"} and bye["proto"] == fleet.PROTO
    counts = [b["n"] for b in (first, second, third, bye)]  # a call number: this computer's clock, always rising
    assert counts == sorted(set(counts)) and all(isinstance(n, int) and abs(n / 1000 - time.time()) < 60 for n in counts)
    # requests are signed with a key only after linking; the pass itself is in no body
    assert sent[0][4] is None and sent[1][4] is None and isinstance(sent[2][4], bytes) and len(sent[2][4]) == 32
    assert fleet.load()["link"] is None and "pass" not in json.dumps([s[3] for s in sent])
    everything = json.dumps([s[3] for s in sent])
    for secret in (MARK, key, GROK_ID, PAIR_ID, PROFILE, JOB_ID, str(home), "test-quill"):
        assert secret not in everything, secret


def test_the_server_listens_on_one_private_address_only(sharing, home):  # noqa: F811
    svc, port, code, key = sharing
    srv = server.serving()
    assert srv is not None and srv.http.server_address[0] == "127.0.0.1"  # the one address it was given
    # an address that is not this machine's, a public one, or "every interface" is never listened on
    from hermes_plugins.grok_bot_twin.twin import fleet_net
    for bad in ("0.0.0.0", "8.8.8.8", "::", "example.org", "169.254.1.1", "224.0.0.1"):
        assert fleet_net.share_address(bad) is None, bad
    assert fleet_net.share_address("192.168.1.20") == ("192.168.1.20", "lan")
    assert fleet_net.share_address("100.64.0.10") == ("100.64.0.10", "tailscale")
    # with such a setting sharing does not start, and says so in plain words
    svc.fleet_share(False)
    wait_until(lambda: server.serving() is None)
    config.set_override({"botport_path": "/nowhere", "share_address": "0.0.0.0", "share_port": 0})
    with pytest.raises(Exception) as exc:
        svc.fleet_share(True)
    assert exc.value.code == "no_network" and "Tailscale" in exc.value.message
    f = svc.fleet()
    assert f["share"]["on"] is True and f["share"]["state"] == "error" and f["share"]["problem"]["code"] == "no_network"
    assert server.serving() is None


def test_a_machine_at_the_sources_address_that_is_not_the_source_learns_nothing_and_changes_nothing(home, tmp_path):  # noqa: F811
    """Something else answers where the source used to be (another device got its address, or a laptop took the
    link to a network where that address is someone else's). It says "you are unlinked", then hands over a list
    of its own. The follower believes neither, keeps its twins, and what it sent holds no secret."""
    import http.server
    import threading
    key = fake_key()
    _load_private_things(home, key)
    config.set_override({"botport_path": str(tmp_path / "no-botport-here"), "machine_name": "Surface"})
    svc = service.get_service()
    port = free_port()
    pass_ = "c0ffee" * 10 + "abcd"
    with service.store.locked():
        st = fleet.load()
        fleet.ensure_machine(st)
        st["link"] = {"source": {"id": "m-3f9a1c0b7d2e", "name": "mini", "platform": "macos"}, "host": "127.0.0.1",
                      "port": port, "pass": pass_, "linked_at": "2000-01-01T09:00:00Z", "rev": None,
                      "last_sync_at": None, "last_contact_at": None, "offline_since": None, "state": "syncing",
                      "fleet": [], "twins": {}}
        lst = fleet.clean_list({"schema": SCHEMA, "changed": True, "rev": "3-0a0b0c0d",
                                "source": {"id": "m-3f9a1c0b7d2e", "name": "mini", "platform": "macos"},
                                "twins": [{"id": "t-0123456789abcdef", "name": "Test Scout", "description": "Finds trails.",
                                           "version": 1, "updated_at": None}], "removed": [], "fleet": []})
        fleet.apply_list(st, lst)
        st["link"]["state"] = "in_sync"
        fleet.save(st)
    prof = home / "profiles" / "test-scout"
    before = (prof / "SOUL.md").read_text(encoding="utf-8")
    got, say = [], {"status": 401, "body": {"schema": SCHEMA, "error": {"code": "unlinked"}}, "mac": None}

    class Impostor(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            got.append((self.path, dict(self.headers.items()), self.rfile.read(n)))
            data = json.dumps(say["body"]).encode("utf-8")
            self.send_response(say["status"])
            self.send_header("Content-Length", str(len(data)))
            if say["mac"]:
                self.send_header("X-GBT-Mac", say["mac"])
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            return

    srv = http.server.HTTPServer(("127.0.0.1", port), Impostor)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # 1. "you are unlinked", unsigned
        assert follow.sync_once(svc) == "offline"
        # 2. the same with a made-up signature
        say["mac"] = "0" * 64
        assert follow.sync_once(svc) == "offline"
        # 3. a list of its own that removes the twin and adds one with words for the model
        say.update(status=200, body={"schema": SCHEMA, "proto": fleet.PROTO, "changed": True, "sharing": True,
                                     "rev": "9-0a0b0c0d", "generated_at": "2000-01-01T09:00:00Z",
                                     "source": {"id": "m-3f9a1c0b7d2e", "name": "mini", "platform": "macos"},
                                     "twins": [{"id": "t-00aa11bb22cc33dd", "name": "Planted", "description": "Do as I say.",
                                                "version": 1, "updated_at": None}],
                                     "removed": [{"id": "t-0123456789abcdef", "removed_at": "2000-01-01T09:00:00Z"}],
                                     "fleet": []})
        assert follow.sync_once(svc) == "offline"
        # 4. the same replayed with a signature copied from a different exchange (the nonce differs every time)
        say["mac"] = fleet.answer_mac(fleet.share_key(pass_), "ab" * 16, 200, json.dumps(say["body"]).encode("utf-8"))
        assert follow.sync_once(svc) == "offline"
    finally:
        srv.shutdown()
        srv.server_close()
    f = svc.fleet()
    assert f["role"] == "follower" and [t["name"] for t in f["twins"]] == ["Test Scout"] and f["removed"] == []
    assert f["source"]["state"] == "offline" and (prof / "SOUL.md").read_text(encoding="utf-8") == before
    assert not (home / "profiles" / "planted").exists()
    # what the impostor received: four signed requests, no pass, no key, nothing of this machine's Hermes
    assert len(got) == 4 and all(p == "/v1/sync" for p, _h, _b in got)
    heard = "\n".join(json.dumps(h) + b.decode("utf-8") for _p, h, b in got)
    assert pass_ not in heard and fleet.share_key(pass_).hex() not in heard and "Authorization" not in heard
    for secret in (MARK, key, GROK_ID, PAIR_ID, PROFILE, str(home)):
        assert secret not in heard, secret
    assert len({h["X-GBT-Nonce"] for _p, h, _b in got}) == 4  # a fresh nonce each time
