"""The fleet half of the service: what the Twins page and `hermes twin` call.

`FleetMixin` adds to TwinService: the whole picture (`fleet`), the sharing switch and link code (source), link and
unlink (follower), removing one linked machine, this machine's name, "check now", bringing a kept Hermes bot back,
and the hooks the rest of the service calls (a twin was added, renamed or removed; the background check ran; the
plugin is being removed). Every problem is a FleetError in the contract's words.
"""
from __future__ import annotations

import contextlib
import logging
import time
from typing import Any, Optional

from . import fleet, fleet_follow as follow, fleet_io as fio, fleet_net as net, fleet_profiles as fp
from . import fleet_server as server
from . import fleet_skills as skills
from . import store, tasks_run
from . import xs_memory
from .connect import BadInput
from .model import now_dt, now_iso, parse_iso, plural

log = logging.getLogger(__name__)

BAD_REQUEST = "That request was not understood."
GOES_SHARING = "Sharing with your other computers."


def _get_service() -> Any:
    from .service import get_service
    return get_service()


def _machine_state(f: dict, rev: str) -> str:
    """How a linked machine is doing, as the source sees it."""
    applied = f.get("applied") if isinstance(f.get("applied"), dict) else {}
    seen = parse_iso(f.get("last_seen_at"))
    recent = seen is not None and (now_dt() - seen).total_seconds() <= fleet.OFFLINE_AFTER_S
    if not applied.get("rev"):
        return "never_synced" if recent or not applied.get("at") else "offline"
    if not recent:
        return "offline"
    if applied.get("rev") == rev and not applied.get("failed"):
        return "in_sync"
    return "behind"


def _twin_fail(code: Any, hint: Any = None) -> dict:
    err = fleet.message(code if code in fleet.FAIL_CODES else "write_failed")
    if isinstance(hint, str) and hint.strip():
        err["hint"] = hint.strip()[:300]
    return err


class FleetMixin:
    """Mixed into TwinService."""

    # ---- read ----------------------------------------------------------------------------------------------------
    def fleet(self) -> dict:
        st = fleet.load()
        r = fleet.role(st)
        me = fleet.this_machine(st)
        if not me["id"]:
            me["id"] = "m-000000000000"  # shown until this machine first shares or links (then it gets its own)
        share = server.state_view(st)
        if share["on"] and share["state"] == "starting":
            # nobody on this machine listens right now (the Hermes process that did has ended): take over
            server.ensure(_get_service)
            share = server.state_view(st)
        out = {"role": r, "this_machine": me, "can_share": fleet.real_twin_count(self) > 0 and r != "follower",
               "share": share, "source": None, "machines": [], "twins": [],
               "removed": self._fleet_kept(st), "last_unlink": None}
        if r == "source":
            self._fleet_source_view(st, me, out)
        elif r == "follower":
            self._fleet_follower_view(st, me, out)
        else:
            lu = st.get("last_unlink")
            if isinstance(lu, dict) and lu.get("by") in ("this", "source"):
                out["last_unlink"] = {"at": lu.get("at") if parse_iso(lu.get("at")) else None, "by": lu["by"],
                                      "source_name": fp.machine_name(lu.get("source_name"))}
        return out

    @staticmethod
    def _fleet_kept(st: dict) -> list:
        out = []
        for k in sorted(st.get("kept", []), key=lambda k: str(k.get("removed_at")), reverse=True)[:100]:
            out.append({"rid": k["rid"], "id": k.get("id") if isinstance(k.get("id"), str) else None,
                        "name": fleet.clean_text(k.get("name"), fleet.NAME_CAP, True) or "Twin",
                        "profile": k.get("profile") if isinstance(k.get("profile"), str) else None,
                        "removed_at": k.get("removed_at") if parse_iso(k.get("removed_at")) else None,
                        "kept_until": k.get("kept_until") if parse_iso(k.get("kept_until")) else None,
                        "reason": k.get("reason") if k.get("reason") in ("removed_on_source", "unlinked",
                                                                         "unlinked_by_source") else "unlinked"})
        return out

    @staticmethod
    def _fleet_source_view(st: dict, me: dict, out: dict) -> None:
        sh = st["share"]
        lst = sh["list"]
        rev = lst["rev"]
        followers = [f for f in sh["followers"].values() if fleet.clean_machine(f)]
        followers.sort(key=lambda f: (str(f.get("name")).lower(), f["id"]))
        out["machines"].append({"id": me["id"], "name": me["name"], "platform": me["platform"], "role": "source",
                                "this": True, "state": "in_sync", "linked_at": None, "last_seen_at": None,
                                "last_sync_at": None, "twins": len(lst["twins"]), "failed": 0})
        rows = []
        for f in followers:
            m = fleet.clean_machine(f)
            applied = f.get("applied") if isinstance(f.get("applied"), dict) else {}
            state = _machine_state(f, rev)
            row = {"id": m["id"], "name": m["name"], "platform": m["platform"], "role": "follower", "this": False,
                   "state": state, "linked_at": f.get("linked_at") if parse_iso(f.get("linked_at")) else None,
                   "last_seen_at": f.get("last_seen_at") if parse_iso(f.get("last_seen_at")) else None,
                   "last_sync_at": (f.get("last_seen_at") if state == "in_sync"
                                    else f.get("in_sync_at") if parse_iso(f.get("in_sync_at")) else applied.get("at"))
                   if applied.get("rev") else None,
                   "twins": len(applied.get("twins") or {}) if applied.get("rev") else None,
                   "failed": len(applied.get("failed") or {}) if applied.get("rev") else None}
            if row["last_sync_at"] is not None and not parse_iso(row["last_sync_at"]):
                row["last_sync_at"] = None
            out["machines"].append(row)
            rows.append((f, row))
        for tid, t in sorted(lst["twins"].items(), key=lambda kv: (str(kv[1].get("name")).lower(), kv[0])):
            machines = [{"id": me["id"], "name": me["name"], "role": "source", "this": True, "state": "in_sync",
                         "last_sync_at": None}]
            for f, row in rows:
                applied = f.get("applied") if isinstance(f.get("applied"), dict) else {}
                entry = {"id": row["id"], "name": row["name"], "role": "follower", "this": False,
                         "state": row["state"], "last_sync_at": row["last_sync_at"]}
                failed = (applied.get("failed") or {}).get(tid)
                if failed:
                    entry["state"] = "behind" if row["state"] != "offline" else "offline"
                    entry["error"] = _twin_fail(failed)
                elif tid not in (applied.get("twins") or {}):
                    # That computer has never said it holds this twin (made while it was off, or while sharing
                    # was off, or a moment ago): it is on its way there, not there. A row must not say "also on
                    # Studio" for a twin Studio never got.
                    entry["state"] = "pending"
                    entry["last_sync_at"] = None
                elif (applied.get("twins") or {}).get(tid) == t["version"]:
                    if row["state"] == "behind":
                        entry["state"] = "in_sync"  # the machine is behind on another twin, not on this one
                elif row["state"] == "in_sync":
                    entry["state"] = "behind"
                machines.append(entry)
            out["twins"].append({"id": tid, "name": t["name"], "description": t["description"],
                                 "version": t["version"], "updated_at": t.get("updated_at"),
                                 "pair_id": t.get("pair_id"), "profile": t.get("profile"), "state": "in_sync",
                                 "synced_at": None, "error": None, "machines": machines})

    @staticmethod
    def _fleet_follower_view(st: dict, me: dict, out: dict) -> None:
        ln = st["link"]
        src = fleet.clean_machine(ln["source"]) or {"id": "m-000000000000", "name": "computer", "platform": "other"}
        state = ln.get("state") if ln.get("state") in ("syncing", "in_sync", "offline", "sharing_off", "error") \
            else "syncing"
        problem = None
        since = ln.get("offline_since") if parse_iso(ln.get("offline_since")) else None
        if state == "offline":
            problem = fleet.message("source_offline", name=src["name"], time=fleet._clock(since))
        elif state == "sharing_off":
            problem = fleet.message("sharing_off", name=src["name"])
        elif state == "error" and ln.get("why") == "forgotten":
            problem = fleet.message("source_forgot", name=src["name"])
        elif state == "error" and ln.get("why") == "removed":
            problem = fleet.message("source_removed", name=src["name"])
        elif state == "error":
            side = ln.get("older")  # which of the two has the older plugin, when that could be told
            problem = fleet.message({"this": "update_here", "source": "update_there"}.get(side, "version_mismatch"),
                                    name=src["name"])
            problem["code"] = "version_mismatch"
        # The time of the last answer is written at most once a minute while all is well, the time of the last
        # change at once: the newer of the two is when this computer last heard from the other one. (The page
        # showed "in sync 31 sec ago" three seconds after a twin had arrived.)
        stamps = [x for x in (ln.get("last_contact_at"), ln.get("last_sync_at")) if parse_iso(x)]
        contact = max(stamps, key=parse_iso) if stamps else None
        out["source"] = {**src, "address": ln.get("host"), "port": ln.get("port"), "state": state,
                         "linked_at": ln.get("linked_at"), "last_sync_at": ln.get("last_sync_at"),
                         "last_contact_at": contact,
                         "offline_since": since if state in ("offline", "sharing_off") else None, "problem": problem}
        # (a twin noted as "being made" is not shown before its Hermes bot is there: a moment, or after this
        # machine was cut short part-way, until its next sync finishes the work)
        held = {tid: e for tid, e in ln["twins"].items()
                if isinstance(e, dict) and not (e.get("making") and e.get("state") != "error")}
        n_ok = len([e for e in held.values() if e.get("state") != "error"])
        n_bad = len(held) - n_ok
        own_state = "in_sync" if state == "in_sync" and not n_bad else "behind"
        members = [m for m in ln.get("fleet", []) if isinstance(m, dict) and fleet.clean_machine(m)]
        if not any(m.get("id") == src["id"] for m in members):
            members.insert(0, {**src, "role": "source"})
        if not any(m.get("id") == me["id"] for m in members):
            members.append({**me, "role": "follower"})
        members.sort(key=lambda m: (m.get("role") != "source", str(m.get("name")).lower()))
        for m in members:
            cm = fleet.clean_machine(m, "source" if m.get("role") == "source" else "follower")
            mine = cm["id"] == me["id"]
            out["machines"].append({**cm, "name": me["name"] if mine else cm["name"], "this": mine,
                                    "state": own_state if mine else "unknown",
                                    "linked_at": ln.get("linked_at") if mine else None,
                                    "last_seen_at": contact if mine else None,
                                    "last_sync_at": ln.get("last_sync_at") if mine else None,
                                    "twins": n_ok if mine else None, "failed": n_bad if mine else None})
        for tid, e in sorted(held.items(), key=lambda kv: (str(kv[1].get("name")).lower(), kv[0])):
            t_state = e.get("state") if e.get("state") in ("in_sync", "error", "not_listed") else "in_sync"
            err = e.get("error") if isinstance(e.get("error"), dict) else None
            machines = [{"id": row["id"], "name": row["name"], "role": row["role"], "this": row["this"],
                         "state": ("in_sync" if t_state == "in_sync" and state == "in_sync" else "behind")
                         if row["this"] else "unknown",
                         "last_sync_at": e.get("synced_at") if row["this"] else None} for row in out["machines"]]
            out["twins"].append({"id": tid, "name": e.get("name") or "Twin", "description": e.get("description") or "",
                                 "version": e.get("version") if isinstance(e.get("version"), int) else 1,
                                 "updated_at": e.get("updated_at"), "pair_id": None, "profile": e.get("profile"),
                                 "state": t_state, "synced_at": e.get("synced_at"),
                                 "error": _twin_fail(err.get("code"), err.get("hint")) if t_state == "error" and err
                                 else None, "machines": machines})
            # (worked out now, not remembered: said for as long as the twin sits under another name than its own)
            said = fleet.clash_note(e.get("profile"), e.get("name") or "") if t_state != "error" else None
            if said:
                out["twins"][-1]["note"] = said[:300]
            # optional: when its name or description last changed here, or ("new") when its first copy arrived
            if parse_iso(e.get("changed_at")):
                out["twins"][-1]["changed_at"] = e["changed_at"]
                out["twins"][-1]["changed"] = e["changed"] if e.get("changed") in ("name", "new") else "description"

    def fleet_brief(self) -> dict:
        """The small `fleet` object of GET /status. Reads one small file; makes nothing."""
        try:
            if not fleet.exists():
                return {"role": "none", "sharing": False, "machines": 0, "source": None}
            st = fleet.load()
            r = fleet.role(st)
            out = {"role": r, "sharing": bool(r == "source" and st["share"].get("on")), "machines": 0, "source": None}
            if r == "source":
                out["machines"] = len(st["share"]["followers"])
            elif r == "follower":
                ln = st["link"]
                me = st["machine"]["id"]
                out["machines"] = max(1, len([m for m in ln.get("fleet", []) if isinstance(m, dict)
                                              and m.get("id") != me]))
                state = ln.get("state") if isinstance(ln.get("state"), str) else "syncing"
                out["source"] = {"name": fp.machine_name(ln["source"].get("name")), "state": state,
                                 "offline_since": ln.get("offline_since") if state in ("offline", "sharing_off")
                                 else None}
            return out
        except Exception:
            log.debug("grok-bot-twin: fleet brief failed", exc_info=True)
            return {"role": "none", "sharing": False, "machines": 0, "source": None}

    def fleet_pairs(self) -> dict:
        """pair id -> the `fleet` object of that pair in GET /status (source only; {} otherwise)."""
        try:
            if not fleet.exists():
                return {}
            st = fleet.load()
            if fleet.role(st) != "source":
                return {}
            out: dict = {"role": "source", "machines": [], "twins": []}
            self._fleet_source_view(st, fleet.this_machine(st), out)
            return {t["pair_id"]: {"id": t["id"], "machines": t["machines"]} for t in out["twins"] if t.get("pair_id")}
        except Exception:
            log.debug("grok-bot-twin: fleet pairs failed", exc_info=True)
            return {}

    # ---- source: share, code, remove a machine ---------------------------------------------------------------------
    def fleet_share(self, on: Any) -> dict:
        if not isinstance(on, bool):
            raise BadInput(BAD_REQUEST)
        if not on:
            with fleet.locked():
                st = fleet.load()
                sh = st.get("share")
                if sh and sh.get("on"):
                    sh["on"] = False
                    sh["since"] = None
                    sh["code"] = None
                    fleet.save(st)
                    was_on = True
                else:
                    was_on = False
            if was_on:
                fleet.announce()  # another process that serves is told; waiting machines hear "sharing is off"
                server.ensure(_get_service)
                self.emit("fleet", None, {"what": "share_off", "count": 1})
            return self.fleet()
        with fleet.locked():
            st = fleet.load()
            if st.get("link"):
                raise fleet.FleetError("is_follower", name=fp.machine_name(st["link"]["source"].get("name")))
            if fleet.real_twin_count(self) == 0:
                raise fleet.FleetError("no_twins")
            fleet.ensure_machine(st)
            sh = st.get("share") or fleet._blank_share()
            was_on = bool(sh.get("on"))
            if not was_on:
                sh["on"] = True
                sh["since"] = now_iso()
            sh["leaving"] = False
            st["share"] = sh
            fleet.rebuild_list(self, st)
            fleet.save(st)
        problem = server.ensure(_get_service)
        if problem:
            raise fleet.FleetError(problem, port=server._port(), program=server.holder_name(server._port())
                                   if problem == "port_busy" else "")
        self._fleet_new_code()
        fleet.tend_source_twins(self)
        if not was_on:
            self.emit("fleet", None, {"what": "share_on", "count": 1})
        return self.fleet()

    def _fleet_new_code(self) -> dict:
        srv = server.serving()
        with fleet.locked():
            st = fleet.load()
            sh = st.get("share")
            if not sh or not sh.get("on"):
                raise fleet.FleetError("not_sharing", name="this computer")
            inst = sh.get("instance") if isinstance(sh.get("instance"), dict) else {}
            # the address a link code carries: this process's server, else the serving process's, else the one a
            # short terminal command just found usable (fleet_server.one_shot: it serves nothing itself)
            address = srv.address if srv is not None else (inst.get("address") or sh.get("address"))
            if not net.kind_of(address):
                raise fleet.FleetError(server.problem() or "no_network", port=server._port())
            fleet.new_code(st, address)
            fleet.save(st)
        return fleet.code_view(st) or {}

    def fleet_code(self) -> dict:
        st = fleet.load()
        if not (st.get("share") or {}).get("on"):
            raise fleet.FleetError("not_sharing", name="this computer")
        problem = server.ensure(_get_service)
        if problem:
            raise fleet.FleetError(problem, port=server._port(), program=server.holder_name(server._port())
                                   if problem == "port_busy" else "")
        return {"code": self._fleet_new_code()}

    def fleet_remove_machine(self, mid: Any, dry_run: Any = False) -> dict:
        if not isinstance(dry_run, bool) or not isinstance(mid, str):
            raise BadInput(BAD_REQUEST)
        with fleet.locked():
            st = fleet.load()
            followers = (st.get("share") or {}).get("followers") or {}
            f = followers.get(mid) or next((x for x in followers.values() if isinstance(x, dict)
                                            and str(x.get("name")).lower() == mid.strip().lower()), None)
            if not isinstance(f, dict):
                raise fleet.FleetError("unknown_machine")
            name = fp.machine_name(f.get("name"))
            applied = f.get("applied") if isinstance(f.get("applied"), dict) else {}
            n = len(applied.get("twins") or {}) or len(st["share"]["list"]["twins"])
            its = f"its {plural(n, 'twin')} {'leaves' if n == 1 else 'leave'} {name} only" if n else \
                "nothing else changes"
            # (a computer that is off hears of it, and loses its copies, at its next contact; of one that is
            # answering right now "when it is next on" would be untrue: it is on)
            on_now = bool(st["share"].get("on")) and _machine_state(f, st["share"]["list"]["rev"]) in ("in_sync",
                                                                                                    "behind")
            out = {"dry_run": dry_run, "unlinked": not dry_run, "machine": {"id": f["id"], "name": name},
                   "sentence": f"{name} is unlinked; {its}.",
                   "stays": ["Your twins on this computer and on your other computers.",
                             f"Everything else in Hermes on {name}."],
                   "goes": [f"The {plural(n, 'twin')} on {name}{'' if on_now else ', when it is next on'}. "
                            + ("It is kept there for 30 days with its chats." if n == 1
                               else "They are kept there for 30 days with their chats.") if n
                            else f"The link to {name}."],
                   "undo": f"Link {name} again with a new code within 30 days and its twins come back with their "
                           "chats."}
            if dry_run:
                return out
            fleet.forget_follower(st, f["id"])  # (its key is kept a while, only to sign "you are unlinked")
            fleet.rebuild_list(self, st)
            fleet.save(st)
        fleet.tend_source_twins(self, st)
        fleet.announce()  # that machine's waiting request is answered "unlinked" at once
        self.emit("fleet", None, {"what": "machine_removed", "count": 1})
        return out

    # ---- follower: link, unlink --------------------------------------------------------------------------------------
    def fleet_link(self, code: Any, host: Any = None, port: Any = None) -> dict:
        if host is not None and not isinstance(host, str):
            raise BadInput(BAD_REQUEST)
        if port is not None and (not isinstance(port, int) or isinstance(port, bool)):
            raise BadInput(BAD_REQUEST)
        if not isinstance(code, str):
            raise fleet.FleetError("bad_code")
        follow.link(self, code, host, port)
        return self.fleet()

    def fleet_unlink(self, dry_run: Any = False) -> dict:
        if not isinstance(dry_run, bool):
            raise BadInput(BAD_REQUEST)
        from . import bgagent
        return bgagent.after_unlink(follow.unlink(self, dry_run), dry_run)

    # ---- both --------------------------------------------------------------------------------------------------------
    def fleet_name(self, name: Any) -> dict:
        if not fp.valid_machine_name(name):
            raise fleet.FleetError("bad_name")
        with fleet.locked():
            st = fleet.load()
            fleet.ensure_machine(st)
            st["machine"]["name"] = name
            changed = bool(st.get("share")) and fleet.role(st) == "source" and fleet.rebuild_list(self, st)
            fleet.save(st)
        if changed:
            fleet.tend_source_twins(self, st)
            fleet.announce()
        elif st.get("link"):
            follow.ask_again()  # the source hears the new name with the next request, which goes out now
            follow.kick()
        return self.fleet()

    def fleet_sync(self) -> dict:
        st = fleet.load()
        r = fleet.role(st)
        if r == "none":
            raise fleet.FleetError("not_linked")
        if r == "follower":
            follow.sync_once(self, wait_s=0)
            follow.ensure_started(_get_service)
            follow.kick()
        else:
            fleet.publish(self, force=True)
            server.ensure(_get_service)
        return self.fleet()

    def fleet_restore(self, rid: Any) -> dict:
        if not isinstance(rid, str) or not fleet.RID_RE.match(rid):
            raise fleet.FleetError("unknown_kept")
        with fleet.locked():
            st = fleet.load()
            k = next((x for x in st["kept"] if x["rid"] == rid), None)
            if k is None:
                raise fleet.FleetError("unknown_kept")
            prof = k.get("profile") if isinstance(k.get("profile"), str) else ""
            try:
                if fp.PROFILE_ID_RE.match(prof) and fp.name_taken(prof):
                    # what a running Hermes made again at its old name, with nothing of the owner's in it, is
                    # moved aside (kept, not deleted) so the bot can go back where it was
                    fp.set_aside(prof, store.central_dir() / fleet.KEPT_DIR)
                home = fp.bring_back(store.central_dir() / fleet.KEPT_DIR / rid, prof)
                was = fp.twin_text(home)
                with contextlib.suppress(fp.ProfileBlocked, OSError):
                    fp.write_soul(home, release=True)  # an ordinary Hermes bot now: it follows nothing
                if isinstance(k.get("id"), str) and fleet.REF_RE.match(k["id"]):
                    # remembered, so that linking this computer again makes it that twin's bot again (same folder,
                    # same chats) instead of meeting it as a stranger with the twin's name
                    st["released"][k["id"]] = {"profile": prof, "text": was, "at": now_iso()}
                    st["released"] = dict(list(st["released"].items())[-200:])
            except fp.ProfileBlocked as exc:
                raise fleet.FleetError(exc.code if exc.code in fleet.MESSAGES else "profile_blocked",
                                       hint=exc.hint) from None
            st["kept"] = [x for x in st["kept"] if x["rid"] != rid]
            fleet.save(st)
        self.emit("fleet", None, {"what": "twin_added", "count": 1})
        return self.fleet()

    # ---- hooks for the rest of the service -----------------------------------------------------------------------------
    def fleet_registry_changed(self) -> None:
        """A real twin was added, renamed, described anew or removed on this machine: publish at once. On a
        machine that does not share, the twins' own notes still follow (a renamed twin's note says its new name)."""
        try:
            if not fleet.publish(self):
                fleet.tend_source_twins(self)
        except Exception:
            log.debug("grok-bot-twin: could not publish the twin list", exc_info=True)

    def fleet_twin_started(self, pid: str) -> None:
        """A real twin was just made here: its Hermes bot gets the fleet note and the plugin switched on."""
        fleet.tend_source_twins(self, only=pid)

    def fleet_tick(self) -> None:
        """With the background check (and at start): the safety net. Serve when sharing is on, follow when linked,
        publish what another process changed, drop kept Hermes bots older than 30 days. Never raises."""
        try:
            updated = fp.install_changed()  # (also remembers what the installed plugin looks like now)
            if updated or not getattr(self, "_fleet_tended", False):
                # once per Hermes process: twins made before this version get their note and the plugin's switch
                # too (also on a machine that never shares). And again whenever the installed plugin changed on
                # disk (an update while Hermes runs): every twin's copy follows within one background check, so
                # the restart that finishes the update finds the new page and code in whichever bot is open.
                self._fleet_tended = True
                fleet.tend_source_twins(self)
                fleet.refresh_mirrored_copies()
            if not fleet.exists():
                return
            st = fleet.load()
            r = fleet.role(st)
            if st.get("goodbye"):
                follow.retry_goodbye()  # an unlink the other computer has not heard of yet (rare; backs off)
            if r == "source":
                server.ensure(_get_service)
                fleet.publish(self)
                skills.source_tick()  # the twins' skills, when the owner switched that on
            else:
                server.ensure(_get_service)  # stops a server left from before sharing was switched off
                if r == "follower":
                    follow.ensure_started(_get_service)
            tasks_run.ensure_started(_get_service)  # only on a computer whose owner used the requests switch
            xs_memory.tick(_get_service)  # only on a linked computer with a twin's memory switched on
            from . import routines as _routines
            _routines.ensure_started()  # (no thread unless routines were switched on or heard of on this computer)
            if st.get("kept"):
                with fleet.locked():
                    st = fleet.load()
                    if fleet.prune_kept(st):
                        fleet.save(st)
        except Exception:
            log.debug("grok-bot-twin: fleet check failed", exc_info=True)

    @staticmethod
    def _and(names: list) -> str:
        names = [n for n in names if n]
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""

    def fleet_linked_names(self) -> list:
        """The names of the computers linked to this one (empty unless it shares)."""
        try:
            if not fleet.exists():
                return []
            st = fleet.load()
            if fleet.role(st) != "source":
                return []
            return [m["name"] for m in fleet.fleet_members(st)[1:]]
        except Exception:
            return []

    def fleet_copies_line(self, pid: Optional[str] = None) -> str:
        """For "Stop twinning" on a computer that shares: where else that twin's copies go. With `pid`, only the
        computers that really hold that twin are named (none: no line), and "when it is next on" is said only of
        a computer that is not answering right now. Without it, every linked computer is named."""
        names = self.fleet_linked_names()
        if not names:
            return ""
        now_on = False
        if pid is not None:
            try:
                st = fleet.load()
                sh = st["share"]
                tid = next((t for t, e in sh["list"]["twins"].items() if e.get("pair_id") == pid), None)
                holders = [f for f in sh["followers"].values() if isinstance(f, dict) and fleet.clean_machine(f)
                           and tid is not None and tid in ((f.get("applied") or {}).get("twins") or {})]
                holders.sort(key=lambda f: (str(f.get("name")).lower(), str(f.get("id"))))
                names = [fleet.clean_machine(f)["name"] for f in holders]
                now_on = bool(sh.get("on")) and all(_machine_state(f, sh["list"]["rev"]) in ("in_sync", "behind")
                                                    for f in holders)
            except Exception:
                log.debug("grok-bot-twin: could not tell which computers hold this twin", exc_info=True)
            if not names:
                return ""
        when = "" if now_on else ", when it is next on" if len(names) == 1 else ", when each is next on"
        return f"Its copies on {self._and(names)}{when}. Kept there 30 days, with chats."

    def fleet_staying_lines(self) -> list:
        """For "Remove Grok Bot Twin" on a computer that shares: what becomes of the twins elsewhere."""
        names = self.fleet_linked_names()
        if names:
            return [f"The twins on {self._and(names)}; they stop following."]
        try:  # on a linked computer: its twins stay, as ordinary Hermes bots (this was under "Removed")
            ln = fleet.load().get("link") if fleet.exists() else None
            n = len(ln["twins"]) if ln else 0
            if n:
                return [f"The {plural(n, 'twin')} on this computer, as "
                        f"{'an ordinary Hermes bot' if n == 1 else 'ordinary Hermes bots'}."]
        except Exception:
            pass
        return []

    def fleet_leaving_lines(self) -> list:
        """What "Remove Grok Bot Twin" adds to its `goes` list on this machine."""
        try:
            if not fleet.exists():
                return []
            st = fleet.load()
            r = fleet.role(st)
            if r == "source":
                return [GOES_SHARING]
            lines = []
            if r == "follower":
                ln = st["link"]
                n = len(ln["twins"])
                name = fp.machine_name(ln["source"].get("name"))
                lines.append(f"The link to {name}." + (f" Its {plural(n, 'twin')} here "
                                                       f"{'stops' if n == 1 else 'stop'} following."
                                                       if n else ""))
            if st.get("kept"):
                lines.append(f"The {plural(len(st['kept']), 'Hermes bot')} kept after a removal.")
            return lines
        except Exception:
            return []

    def fleet_shutdown_for_uninstall(self) -> None:
        """Before the plugin's data is deleted: a source says "sharing is off" to the waiting machines and stops
        listening; a follower tells its source goodbye and leaves its twins behind as ordinary Hermes bots (the
        plugin's notes and its switch are taken out of them). Never raises."""
        try:
            if not fleet.exists():
                return
            st = fleet.load()
            r = fleet.role(st)
            if r == "source":
                elsewhere = False
                with contextlib.suppress(Exception):
                    elsewhere = bool(st["share"].get("on")) and server.other_serving(st, fresh=True)
                with contextlib.suppress(Exception):
                    with fleet.locked():  # the last answer to each waiting computer says "removed", not just "off"
                        leaving = fleet.load()
                        if leaving.get("share"):
                            leaving["share"]["leaving"] = True
                            fleet.save(leaving)
                with contextlib.suppress(Exception):
                    self.fleet_share(False)
                if elsewhere:
                    # another Hermes process on this computer is the one that listens (this one is a terminal
                    # command, say). It has been told; it gets a moment to give its waiting computers that last
                    # answer before the data it reads is gone
                    time.sleep(0.7)
                for t in st["share"]["list"]["twins"].values():
                    fleet.untend_profile(t.get("profile"))
            elif r == "follower":
                # the link is forgotten HERE first: the source's answer to this machine's own waiting request
                # ("you are unlinked") must find nothing to take away. The twins stay as ordinary Hermes bots.
                stopped = True
                with fleet.locked():
                    st = fleet.load()
                    ln = st.get("link") or {}
                    if st.get("link"):
                        # a routine this computer runs for the others is paused before the goodbye: the bots
                        # stay here as ordinary ones, and the goodbye lets the routine start elsewhere
                        from . import routines as _routines
                        stopped = _routines.link_dropped(st)
                    st["link"] = None
                    fleet.save(st)
                follow._abort()
                for e in (ln.get("twins") or {}).values():
                    if isinstance(e, dict):
                        fleet.untend_profile(e.get("profile"), release=True)
                if stopped:  # (not seen paused: no goodbye, so nothing this computer ran is started elsewhere)
                    follow.say_goodbye(ln)
            fleet.forget_marks(fleet.load())  # (the kept bots go with the data: so do the notes at their names)
            server.stop()
        except Exception:
            log.debug("grok-bot-twin: fleet shutdown failed", exc_info=True)


def mirrored_summary(profile: Optional[str] = None) -> Optional[dict]:
    """On a machine that follows another one: how its mirrored twins are doing, in fixed words and numbers only
    (no bot name, no computer name), for the model-facing tool. None on any other machine."""
    try:
        if not fleet.exists():
            return None
        ln = fleet.load().get("link")
        if not ln:
            return None
        from .model import ago
        held = [e for e in ln["twins"].values() if isinstance(e, dict)]
        state = ln.get("state")
        words = {"in_sync": "in step", "syncing": "being set up",
                 "offline": "the other computer cannot be reached right now; the copies are as they were",
                 "sharing_off": "sharing was switched off on the other computer; the copies are as they were",
                 "error": "the two computers run versions that cannot work together"}
        said = words.get(state, "being set up")
        if state == "error" and ln.get("why") == "forgotten":
            said = ("the other computer no longer answers as linked to this one; if Grok Bot Twin was set up again "
                    "there, the owner has to unlink this computer and link it again; the copies are as they were")
        elif state == "error" and ln.get("why") == "removed":
            said = ("Grok Bot Twin was removed on the other computer, so the copies here no longer follow it; they "
                    "are as they were")
        return {"kind": "copies kept in step from another of the owner's computers (the one with the Grok app)",
                "copies_on_this_computer": len(held),
                "this_profile_is_one": bool(profile) and any(e.get("profile") == profile for e in held),
                "state": said, "last_answer_from_that_computer": ago(ln.get("last_contact_at")),
                "could_not_be_made_here": len([e for e in held if e.get("state") == "error"]),
                "what_follows": _what_follows()}
    except Exception:
        return None


def _what_follows() -> str:
    """For the model-facing summary: fixed words about what follows a twin to this computer."""
    try:
        from . import xs_config
        v = xs_config.view()
        on = [i["id"] for i in v["items"] if i["on"]]
        if v["state"] == "on" and on:
            return ("name and description, and these settings of the twin: " + ", ".join(on)
                    + "; sign-ins, keys and how a connector is set up are never copied")
    except Exception:
        pass
    return "name and description only; instructions are pasted on each computer"


def ensure_started(get_service: Any) -> None:
    """Called with the background check's own start (first hook or page visit): begin serving or following when
    this machine has that role. One small file read when it has none."""
    try:
        get_service().fleet_tick()
    except Exception:
        log.debug("grok-bot-twin: fleet start failed", exc_info=True)


def stop() -> None:
    xs_memory.stop()
    from . import routines as _routines
    _routines.stop()
    tasks_run.stop()
    follow.stop()
    server.stop()


def _reset_for_tests() -> None:
    xs_memory._reset_for_tests()
    from . import routines as _routines
    _routines._reset_for_tests()
    tasks_run._reset_for_tests()
    server.stop()
    follow._reset_for_tests()
    fleet.hub.closing = False
    from . import xs_config
    xs_config._reset_for_tests()
    server._problem = None
    server._other = (0.0, False)
