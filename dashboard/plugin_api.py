"""REST backend for the Twins page, mounted by Hermes at /api/plugins/grok-bot-twin/.

Every handler checks the shape of its input (400 for a body or id that is not even well formed), runs blocking
work in a worker thread (asyncio.to_thread) and answers every problem the service reports, bad input and an
unknown twin included, as HTTP 200 {"error": {"code", "message", "hint"?}} in plain words, so the page has one
failure path and never shows transport text.
Hermes' dashboard/gateway auth sits in front of these routes.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Query

log = logging.getLogger(__name__)
router = APIRouter()

_ROOT = Path(__file__).resolve().parent.parent
_LOADED_RE = re.compile(r"^hermes_plugins\.grok_bot_twin(?:__home_[0-9a-f]{12})?$")
_PRIVATE_PKG = "_grok_bot_twin_rest"
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MACHINE_RE = re.compile(r"^m-[0-9a-f]{12}\Z")
_KEPT_RE = re.compile(r"^r-[0-9a-f]{12}\Z")
# What a request must carry before /uninstall deletes anything (the Twins page sends it after its confirmation).
UNINSTALL_CONFIRM = "remove-everything"
_pkg: Any = None


def _package() -> Any:
    """The plugin package: reuse the copy Hermes loaded (same service, same timer), else load our own
    copy under a private name (e.g. when the API runs in an isolated plugin host)."""
    global _pkg
    if _pkg is not None:
        return _pkg
    for name, mod in list(sys.modules.items()):
        if _LOADED_RE.match(name) and Path(getattr(mod, "__file__", "") or "/").resolve().parent == _ROOT:
            _pkg = importlib.import_module(name + ".twin")
            return _pkg
    if _PRIVATE_PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(_PRIVATE_PKG, _ROOT / "__init__.py",
                                                      submodule_search_locations=[str(_ROOT)])
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load the grok-bot-twin package")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_PRIVATE_PKG] = mod
        spec.loader.exec_module(mod)
    _pkg = importlib.import_module(_PRIVATE_PKG + ".twin")
    return _pkg


def _svc() -> Any:
    pkg = _package()
    service = importlib.import_module(pkg.__name__ + ".service")
    timer = importlib.import_module(pkg.__name__ + ".timer")
    timer.ensure_started(service.get_service)  # lazy: the first request starts the background check
    return service.get_service()


async def _call(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking service call off the event loop and map errors to HTTP."""
    pkg = _package()
    engine = importlib.import_module(pkg.__name__ + ".engine")
    service = importlib.import_module(pkg.__name__ + ".service")
    try:
        return await asyncio.to_thread(fn, *args)
    except engine.EngineError as exc:  # PairNotFound and BadInput are EngineErrors too
        return {"error": exc.as_dict()}
    except Exception:  # never leak a traceback or a class name to the UI; the log has the details
        log.exception("grok-bot-twin: REST call failed")
        return {"error": {"code": "internal", "message": "Something went wrong inside Grok Bot Twin. Try again."}}


def _pid(pid: str) -> str:
    if not _ID_RE.match(pid or "") or pid in (".", ".."):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That is not one of your twins."})
    return pid


def _body(body: Any) -> dict:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    return body


def _bool(body: dict, key: str) -> bool:
    v = body.get(key, False)
    if not isinstance(v, bool):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    return v


# ---- read -------------------------------------------------------------------------------------------
@router.get("/status")
async def status() -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.status)


@router.get("/grok/bots")
async def grok_bots() -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.grok_list)


@router.get("/pairs/{pid}/conflicts")
async def conflicts(pid: str) -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.conflicts, _pid(pid))


@router.get("/pairs/{pid}/restore-points")
async def restore_points(pid: str) -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.restore_points, _pid(pid))


@router.get("/pairs/{pid}/recent")
async def recent(pid: str, limit: int = Query(10, ge=1, le=50)) -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.recent, _pid(pid), limit)


@router.get("/pairs/{pid}/parity")
async def parity(pid: str) -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.parity, _pid(pid))


@router.get("/pairs/{pid}/checklist")
async def checklist(pid: str) -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.checklist, _pid(pid))


@router.get("/pairs/{pid}/digest")
async def digest_preview(pid: str) -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.digest_preview, _pid(pid))


@router.get("/pairs/{pid}/skill-text")
async def skill_text(pid: str, item: str = Query("", max_length=140)) -> Any:
    """The Hermes version of one skill the owner copies into Grok by hand (text only; never logged)."""
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.skill_text, _pid(pid), item)


@router.get("/doctor")
async def doctor() -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.doctor)


@router.get("/setup")
async def setup_state() -> Any:
    """The three facts "Connect your Grok bots" shows (Grok app, helper, checked as genuine). Polled while that
    sheet is open: cheap, and the helper never runs here."""
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.setup_state)


@router.post("/engine/path")
async def engine_path(body: Any = Body(None)) -> Any:
    """The owner picked the downloaded botport file in the native file picker ({"path"}), or pressed "Use it" on
    the download Hermes found in Downloads ({"from_downloads": true}). The file is verified against the pinned
    release before anything is kept or run; the plugin keeps its own copy. Nothing is downloaded here."""
    b = _body(body)
    from_downloads = _bool(b, "from_downloads")
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.choose_helper, b.get("path"), from_downloads)


@router.get("/engine/trust")
async def engine_trust_status() -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.trust_info)


@router.post("/engine/trust")
async def engine_trust(body: Any = Body(None)) -> Any:
    b = _body(body)
    # {}: the pinned release table first, no GitHub tool needed. {"method": "gh"} asks for GitHub's build
    # attestation on purpose. {"allow_unattested": true} is the explicit, recorded opt-out (a local build).
    # {"tag": "vX.Y.Z"} overrides the expected release tag (validated by the service).
    allow = _bool(b, "allow_unattested")
    svc = await asyncio.to_thread(_svc)
    # (the page's plain ask answers in seconds and in the owner's words: `not_pinned` when this plugin version has
    # no table for the release, never a developer tool's name; {"method": "gh"} keeps that tool's own errors)
    return await _call(svc.trust_botport, allow, b.get("tag"), b.get("method"), True)


@router.post("/engine/attest")
async def engine_attest(body: Any = Body(None)) -> Any:
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.attest_botport, b.get("tag"))


@router.get("/events")
async def events(since: str | None = Query(None, max_length=40)) -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.events, since)


# ---- write ------------------------------------------------------------------------------------------
@router.post("/sync")
async def sync(body: Any = Body(None)) -> Any:
    b = _body(body)
    pair = b.get("pair_id")
    if pair is not None:
        _pid(pair if isinstance(pair, str) else "")
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.sync, pair)


@router.post("/twins/preview")
async def twins_preview(body: Any = Body(None)) -> Any:
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.preview, b.get("grok_ids"), b.get("profiles"))


@router.post("/twins/create")
async def twins_create(body: Any = Body(None)) -> Any:
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.create, b.get("grok_ids"), b.get("profiles"))


@router.post("/pairs/{pid}/resolve")
async def resolve(pid: str, body: Any = Body(None)) -> Any:
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.resolve, _pid(pid), b.get("conflict_id"), b.get("keep"))


@router.post("/pairs/{pid}/restore")
async def restore(pid: str, body: Any = Body(None)) -> Any:
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.restore, _pid(pid), b.get("point_id"))


@router.post("/pairs/{pid}/switch")
async def switch(pid: str, body: Any = Body(None)) -> Any:
    b = _body(body)
    undo = _bool(b, "undo")
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.switch, _pid(pid), b.get("job_ids"), undo)


@router.post("/pairs/{pid}/skip")
async def skip(pid: str, body: Any = Body(None)) -> Any:
    """"Keep it in Hermes only": the item stops asking to be sent to Grok ({"undo": true} offers it again).
    {"copied": true}: the owner pasted this version into Grok by hand; it stops asking until it changes again."""
    b = _body(body)
    undo, copied = _bool(b, "undo"), _bool(b, "copied")
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.skip, _pid(pid), b.get("item"), undo, copied)


@router.post("/pairs/{pid}/instructions")
async def instructions(pid: str, body: Any = Body(None)) -> Any:
    """The owner brings a twin's instructions over: {"text"} (pasted; saved on this computer only, never sent to
    Grok, never logged), or {"none": true} ("this bot has no instructions"). {"accept_lines": [n]} with a text:
    the owner looked at exactly those lines and says they are not a password."""
    b = _body(body)
    none = _bool(b, "none")
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.set_instructions, _pid(pid), b.get("text"), none, b.get("accept_lines"))


# The largest setup export the route reads (characters of its text; the service checks the same number again).
_EXPORT_TEXT_CAP = 2_000_000


@router.get("/pairs/{pid}/setup-export")
async def setup_export_info(pid: str) -> Any:
    """What the import sheet shows before a file is there: the request to paste into the bot's own Grok chat, the
    sentence about what is left out, and the connector names the last import left to set up on this computer."""
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.setup_export_info, _pid(pid))


@router.post("/pairs/{pid}/setup-export")
async def setup_export(pid: str, body: Any = Body(None)) -> Any:
    """{"text"}: the setup export the bot wrote about itself, as the owner dropped it. {"apply": false} (the
    default) only says what an import would do; {"apply": true} does it: instructions saved on this computer,
    routines added switched off, connector names noted. Memories and facts about the owner in the file are
    never read into anything. The text is not stored, not logged and not sent anywhere."""
    b = _body(body)
    apply = _bool(b, "apply")
    text = b.get("text")
    if isinstance(text, str) and len(text) > _EXPORT_TEXT_CAP:
        return {"error": {"code": "too_large", "message": "That file is too large to be a setup export.",
                          "hint": "Ask the bot for its setup only: instructions, routines and connector names."}}
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.import_setup, _pid(pid), text, apply, b.get("bot"), b.get("skip"))


# ---- several twins at once: their model and their setup exports ---------------------------------------------
_BATCH_FILES_MAX = 40


def _pid_list(value: Any) -> Any:
    """A list of twin ids from a body (None stays None: every twin). Anything else is refused before the service."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 200 or not all(isinstance(p, str) for p in value):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    return [_pid(p) for p in value]


def _too_many_files(files: Any) -> Any:
    """The error envelope for a drop the service would refuse anyway (checked here before any text is passed on)."""
    if isinstance(files, list) and (len(files) > _BATCH_FILES_MAX or any(
            isinstance(f, dict) and isinstance(f.get("text"), str) and len(f["text"]) > _EXPORT_TEXT_CAP
            for f in files)):
        return {"error": {"code": "too_large", "message": "Drop at most 40 setup export files, each under 2 MB.",
                          "hint": "Ask each bot for its setup only: instructions, routines and connector names."}}
    return None


@router.get("/batch")
async def batch_info(pair_ids: str | None = Query(None, max_length=20000)) -> Any:
    """What the sheet for several new twins shows: the request to paste into each bot's chat, the models in use,
    each twin's model and what it still needs, whether Downloads is there (never its path)."""
    pids = None if pair_ids is None else _pid_list([p for p in pair_ids.split(",") if p])
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.batch_info, pids)


@router.post("/batch/model")
async def batch_model(body: Any = Body(None)) -> Any:
    """{"pair_ids", "model"}: the same model for every ticked twin (each in its own config.yaml)."""
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.batch_models, _pid_list(b.get("pair_ids")), b.get("model"), b.get("endpoint"))


@router.post("/pairs/{pid}/model")
async def pair_model(pid: str, body: Any = Body(None)) -> Any:
    """{"model"}: this twin's model, written into its own config.yaml (never the default profile's)."""
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.set_model, _pid(pid), b.get("model"), "owner", None, b.get("endpoint"))


@router.post("/batch/match")
async def batch_match(body: Any = Body(None)) -> Any:
    """{"files": [{"name", "text"}], "pair_ids"}: which twin each dropped file is for. Nothing is kept."""
    b = _body(body)
    too = _too_many_files(b.get("files"))
    if too:
        return too
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.batch_match, b.get("files"), _pid_list(b.get("pair_ids")))


@router.post("/batch/import")
async def batch_import(body: Any = Body(None)) -> Any:
    """{"files": [{"name", "text"}], "pair_ids"}: import each file into the twin it is for. The texts are not
    stored, logged or sent anywhere; memories and facts about the owner are left out."""
    b = _body(body)
    too = _too_many_files(b.get("files"))
    if too:
        return too
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.batch_import, b.get("files"), _pid_list(b.get("pair_ids")))


@router.get("/pairs/{pid}/instructions-sync")
async def instructions_sync_info(pid: str) -> Any:
    """Grok's instructions kept in step for one twin: on or off, the last re-read of its setup export, and the
    conflict card when the instructions changed both in Grok and in Hermes."""
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.instructions_sync_info, _pid(pid))


@router.post("/pairs/{pid}/instructions-sync")
async def instructions_sync_set(pid: str, body: Any = Body(None)) -> Any:
    """{"on": bool}: follow Grok's instructions for this twin, or stop."""
    b = _body(body)
    if not isinstance(b.get("on"), bool):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.instructions_sync_set, _pid(pid), b["on"])


@router.post("/pairs/{pid}/instructions-sync/resolve")
async def instructions_sync_resolve(pid: str, body: Any = Body(None)) -> Any:
    """{"choice": "grok"|"hermes"|"text", "text", "grok_digest"}: the owner's answer to the conflict card;
    `grok_digest` (the card's own, needed for "grok") names the version of Grok's instructions the owner read."""
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.instructions_conflict_resolve, _pid(pid), b.get("choice"), b.get("text"),
                       b.get("grok_digest"))


@router.post("/instructions-sync/check")
async def instructions_sync_check(body: Any = Body(None)) -> Any:
    """{"pair_id"} (optional): re-read the newest setup exports now instead of at the next background check."""
    b = _body(body)
    pid = b.get("pair_id")
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.instructions_sync_now, None if pid is None else _pid(pid if isinstance(pid, str) else ""))


@router.post("/instructions-sync/settings")
async def instructions_sync_settings(body: Any = Body(None)) -> Any:
    """{"downloads": bool}: also look for setup exports in Downloads in the background (off by default)."""
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.instructions_sync_settings, b.get("downloads"))


@router.post("/batch/downloads")
async def batch_downloads(body: Any = Body(None)) -> Any:
    """{"since", "pair_ids", "apply"}: setup exports saved to Downloads since then, imported into the ticked twins
    (apply defaults to true). The page asks every 4 s while it watches."""
    b = _body(body)
    apply = b.get("apply", True)
    if not isinstance(apply, bool):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.downloads_scan, b.get("since"), _pid_list(b.get("pair_ids")), apply)


@router.post("/pairs/{pid}/send-to-grok")
async def send_to_grok(pid: str) -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.send_to_grok, _pid(pid))


@router.post("/pairs/{pid}/approve")
async def approve(pid: str, body: Any = Body(None)) -> Any:
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.approve, _pid(pid), b.get("item"))


@router.get("/pairs/{pid}/skill-sync")
async def skill_sync_status(pid: str) -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.skill_sync_status, _pid(pid))


@router.post("/pairs/{pid}/skill-sync")
async def skill_sync_setup(pid: str, body: Any = Body(None)) -> Any:
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.skill_sync_setup, _pid(pid), b.get("folder"), _bool(b, "remake"))


@router.post("/pairs/{pid}/skill-sync/take")
async def skill_sync_take(pid: str, body: Any = Body(None)) -> Any:
    """Which skills made in Grok come back to this twin: {"add": [names], "remove": [names], "every": true|false}."""
    b = _body(body)
    every = b.get("every")
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.skill_sync_take, _pid(pid), b.get("add") or [], b.get("remove") or [],
                       every if isinstance(every, bool) else None)


@router.post("/pairs/{pid}/mirror")
async def mirror(pid: str, body: Any = Body(None)) -> Any:
    b = _body(body)
    off, rotate = _bool(b, "off"), _bool(b, "rotate_key")
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.mirror, _pid(pid), b.get("pair_folder"), off, rotate)


@router.post("/pairs/{pid}/remove")
async def remove(pid: str, body: Any = Body(None)) -> Any:
    b = _body(body)
    dry = _bool(b, "dry_run")
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.remove, _pid(pid), dry)


@router.post("/pairs/{pid}/digest")
async def digest_set(pid: str, body: Any = Body(None)) -> Any:
    b = _body(body)
    enabled, titles = _bool(b, "enabled"), _bool(b, "titles")
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.set_digest, _pid(pid), enabled, titles)


@router.post("/uninstall")
async def uninstall(body: Any = Body(None)) -> Any:
    """Remove Grok Bot Twin: every twin's record, earlier versions, the sample and Hermes' own copy of botport. Grok
    bots, Hermes bots and their chats stay. {"dry_run": true} only reports what would go. Removing for real needs
    {"confirm": "remove-everything"}: an empty or half-made request never deletes anything."""
    b = _body(body)
    dry = _bool(b, "dry_run")
    if not dry and b.get("confirm") != UNINSTALL_CONFIRM:
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.uninstall, dry)


# ---- your other machines (the sharing contract, local routes) ------------------------------------------------
@router.get("/fleet")
async def fleet_get() -> Any:
    """This machine's place among the owner's machines: sharing, the link code, linked machines, mirrored twins.
    Reads one small file; no network, no botport."""
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.fleet)


@router.post("/fleet/share")
async def fleet_share(body: Any = Body(None)) -> Any:
    """{"on": true}: share this machine's twins with the owner's other machines and show a link code.
    {"on": false}: stop listening (linked machines stay linked)."""
    b = _body(body)
    if "on" not in b:
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    on = _bool(b, "on")
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.fleet_share, on)


@router.post("/fleet/code")
async def fleet_code() -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.fleet_code)


@router.post("/fleet/link")
async def fleet_link(body: Any = Body(None)) -> Any:
    """{"code"}: link this machine to the one the code came from (optional "host", "port")."""
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.fleet_link, b.get("code"), b.get("host"), b.get("port"))


@router.post("/fleet/unlink")
async def fleet_unlink(body: Any = Body(None)) -> Any:
    b = _body(body)
    dry = _bool(b, "dry_run")
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.fleet_unlink, dry)


@router.post("/fleet/machines/{mid}/remove")
async def fleet_machine_remove(mid: str, body: Any = Body(None)) -> Any:
    b = _body(body)
    dry = _bool(b, "dry_run")
    if not _MACHINE_RE.match(mid or ""):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That computer is not linked."})
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.fleet_remove_machine, mid, dry)


@router.post("/fleet/name")
async def fleet_name(body: Any = Body(None)) -> Any:
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.fleet_name, b.get("name"))


@router.post("/fleet/sync")
async def fleet_sync() -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.fleet_sync)


@router.post("/fleet/removed/{rid}/restore")
async def fleet_restore(rid: str) -> Any:
    if not _KEPT_RE.match(rid or ""):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That Hermes bot is no longer kept."})
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.fleet_restore, rid)


@router.get("/fleet/background")
async def fleet_background_get() -> Any:
    """The background helper that keeps a linked computer following while Hermes is closed: is it available here,
    set up, running. Asks systemd / launchd once; no network."""
    bg = importlib.import_module(_package().__name__ + ".bgagent")
    return await _call(bg.status)


@router.post("/fleet/background")
async def fleet_background_set(body: Any = Body(None)) -> Any:
    """{"on": true}: set the helper up and start it (adds one file). {"on": false}: stop it and delete that file."""
    b = _body(body)
    if "on" not in b:
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    on = _bool(b, "on")
    bg = importlib.import_module(_package().__name__ + ".bgagent")
    return await _call(bg.switch, on)


# ---- the settings that follow a twin to the owner's other computers (twin/xs_config.py) ---------------------
def _xs_config() -> Any:
    return importlib.import_module(_package().__name__ + ".xs_config")


@router.get("/fleet/settings")
async def fleet_settings_get() -> Any:
    """Which settings follow a twin (tools, preferences, persona; the model only when switched on), this
    computer's switch for each, and on a linked computer how each twin's settings are doing. No network."""
    svc = await asyncio.to_thread(_svc)
    return await _call(_xs_config().view, svc)


@router.post("/fleet/settings")
async def fleet_settings_set(body: Any = Body(None)) -> Any:
    """{"item": "tools" | "preferences" | "persona" | "model", "on": true|false}: this computer's switch."""
    b = _body(body)
    if not isinstance(b.get("item"), str) or not isinstance(b.get("on"), bool):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(_xs_config().switch, b["item"], b["on"], svc)


@router.post("/fleet/settings/restore")
async def fleet_settings_restore(body: Any = Body(None)) -> Any:
    """{"twin": "t-...", "point"?: "p-..."}: put back what that twin's Hermes bot had before settings arrived."""
    b = _body(body)
    if not isinstance(b.get("twin"), str) or not (b.get("point") is None or isinstance(b.get("point"), str)):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(_xs_config().restore, b["twin"], b.get("point"), svc)


@router.post("/fleet/settings/follow")
async def fleet_settings_follow(body: Any = Body(None)) -> Any:
    """{"twin": "t-..."}: end the hold that "Put back" left; the twin takes the current settings again."""
    b = _body(body)
    if not isinstance(b.get("twin"), str):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(_xs_config().follow_again, b["twin"], svc)


# ---- routines on the owner's other computers (twin/routines.py) --------
_ROUTINE_RE = re.compile(r"^j-[0-9a-f]{16}$")
_ROUTINE_UNDO_RE = re.compile(r"^u-[0-9a-f]{12}$")


@router.get("/fleet/routines")
async def fleet_routines_get() -> Any:
    """What copies between the owner's computers, and where each routine runs. Reads small files; no network."""
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.routines)


@router.post("/fleet/routines")
async def fleet_routines_set(body: Any = Body(None)) -> Any:
    """{"on": bool}: copy routines (the sharing computer) / keep copies here (a linked computer).
    {"can_run": bool}: routines may run on this linked computer."""
    b = _body(body)
    if not ("on" in b or "can_run" in b):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    on = _bool(b, "on") if "on" in b else None
    can_run = _bool(b, "can_run") if "can_run" in b else None
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.routines_switch, on, can_run)


@router.post("/fleet/routines/{rid}/move")
async def fleet_routines_move(rid: str, body: Any = Body(None)) -> Any:
    """{"to": "m-…"}: run this routine on that computer. It starts there once the computer it ran on stopped it."""
    b = _body(body)
    if not _ROUTINE_RE.match(rid):
        return {"error": {"code": "unknown_routine", "message": "That routine is no longer there."}}
    if not isinstance(b.get("to"), str):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    force = _bool(b, "force") if "force" in b else False
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.routines_move, rid, b.get("to"), force)


@router.post("/fleet/routines/restore/{uid}")
async def fleet_routines_restore(uid: str) -> Any:
    """Put one routine back in Hermes as it was before the plugin changed or removed it here (paused)."""
    if not _ROUTINE_UNDO_RE.match(uid):
        return {"error": {"code": "unknown_restore", "message": "That restore point is no longer kept."}}
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.routines_restore, uid)


@router.post("/demo")
async def demo(body: Any = Body(None)) -> Any:
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.demo_action, b.get("action"), b.get("profile"))


# ---- requests between the owner's computers ("do X on Surface") -----------------------------------------------
_TASK_RE = re.compile(r"^k-[0-9a-f]{16}$")


@router.get("/tasks")
async def tasks_get() -> Any:
    """The switch "Let my other computers ask this one to do things" (off by default), the other computers, and
    this computer's log of requests (asked from here, asked of this computer)."""
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.tasks)


@router.post("/tasks/switch")
async def tasks_switch(body: Any = Body(None)) -> Any:
    b = _body(body)
    if not isinstance(b.get("on"), bool):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.tasks_switch, b["on"])


@router.post("/tasks/ask")
async def tasks_ask(body: Any = Body(None)) -> Any:
    """Ask a twin's copy on another computer: {"machine", "text", "twin"?, "wait_s"?}. Waits for the answer."""
    b = _body(body)
    for key in ("machine", "text"):
        if not isinstance(b.get(key), str):
            raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    if b.get("twin") is not None and not isinstance(b.get("twin"), str):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.tasks_ask, b["machine"], b["text"], b.get("twin"), b.get("wait_s"))


@router.post("/tasks/{tid}/approve")
async def tasks_approve(tid: str, body: Any = Body(None)) -> Any:
    """Answer the step of a request that waits for approval ON THIS COMPUTER: {"choice": "once" | "deny",
    "q"?: the mark of the step that was shown (waiting[i].q)}. Only these two choices; only this computer's own
    page can call it (Hermes' own auth sits in front, as for every route). With "q" the answer counts only for
    the step the owner saw."""
    b = _body(body)
    if not _TASK_RE.match(tid or "") or b.get("choice") not in ("once", "deny") \
            or not (b.get("q") is None or (isinstance(b.get("q"), str) and len(b["q"]) <= 16)):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    # (True: this is the owner's page, not a chat; see tasks.decide)
    return await _call(svc.tasks_approve, tid, b["choice"], b.get("q"), True)


@router.post("/tasks/mode")
async def tasks_mode(body: Any = Body(None)) -> Any:
    """What happens on THIS computer when a step of a request needs approval: {"mode": "here" | "safe"}.
    Only this computer's own page can call it (Hermes' own auth sits in front, as for every route); nothing another
    computer sends reaches it. A request for "linked" (approving from another computer, removed) is refused."""
    b = _body(body)
    if b.get("mode") not in ("here", "safe"):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.tasks_mode, b["mode"], True)


@router.post("/tasks/check")
async def tasks_check(body: Any = Body(None)) -> Any:
    b = _body(body)
    if not isinstance(b.get("id"), str) or not _TASK_RE.match(b["id"]):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.tasks_check, b["id"], b.get("wait_s") or 0)


# ---- a twin's memory between your own linked computers (twin/xs_memory.py; off for every twin until switched on) ----
def _xs_memory() -> Any:
    _svc()  # (the service sets the plugin up; the memory module then works from this computer's own files)
    return importlib.import_module(_package().__name__ + ".xs_memory")


def _twin_word(b: dict) -> str:
    if not isinstance(b.get("twin"), str) or not 0 < len(b["twin"]) <= 120:
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    return b["twin"]


@router.get("/fleet/memory")
async def fleet_memory_get() -> Any:
    """Which twins keep their memory the same on the owner's linked computers, in words ready to show. Reads this
    computer's own files; no network call."""
    m = await asyncio.to_thread(_xs_memory)
    return await _call(m.view)


@router.post("/fleet/memory")
async def fleet_memory_set(body: Any = Body(None)) -> Any:
    """{"twin": id, "on": bool}: that twin's memory switch on this computer."""
    b = _body(body)
    twin = _twin_word(b)
    if not isinstance(b.get("on"), bool):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    m = await asyncio.to_thread(_xs_memory)
    return await _call(m.switch, twin, b["on"], _svc)


@router.post("/fleet/memory/forget")
async def fleet_memory_forget(body: Any = Body(None)) -> Any:
    """{"twin": id}: take out of this twin's memory here what came from the other computers; restore points go."""
    twin = _twin_word(_body(body))
    m = await asyncio.to_thread(_xs_memory)
    return await _call(m.forget, twin)


@router.post("/fleet/memory/restore")
async def fleet_memory_restore(body: Any = Body(None)) -> Any:
    """{"twin": id, "point"?: "p-..."}: put the memory files back as a restore point has them; the switch goes off."""
    b = _body(body)
    twin = _twin_word(b)
    if not (b.get("point") is None or (isinstance(b.get("point"), str) and len(b["point"]) <= 20)):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    m = await asyncio.to_thread(_xs_memory)
    return await _call(m.restore, twin, b.get("point"))


@router.post("/fleet/memory/resolve")
async def fleet_memory_resolve(body: Any = Body(None)) -> Any:
    """{"twin": id, "conflict": "c-...", "keep": "both" | a version's key}: settle a conflict."""
    b = _body(body)
    twin = _twin_word(b)
    for key in ("conflict", "keep"):
        if not isinstance(b.get(key), str) or not 0 < len(b[key]) <= 40:
            raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    m = await asyncio.to_thread(_xs_memory)
    return await _call(m.resolve, twin, b["conflict"], b["keep"])


@router.post("/fleet/memory/computer")
async def fleet_memory_computer(body: Any = Body(None)) -> Any:
    """{"twin": id, "machine": "m-...", "on": bool} on the computer that shares: whether that linked computer may
    take part in this twin's memory."""
    b = _body(body)
    twin = _twin_word(b)
    if not isinstance(b.get("on"), bool) or not isinstance(b.get("machine"), str) or not _MACHINE_RE.match(b["machine"]):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    m = await asyncio.to_thread(_xs_memory)
    return await _call(m.computer, twin, b["machine"], b["on"])


# ---- skills between the owner's computers (twin/fleet_skills.py) -----------------------------------------------------
_SKILL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,63})?\Z")
_SKILL_VID_RE = re.compile(r"^(?:k-[0-9a-f]{12}|v[0-9]{1,9})\Z")
_TWIN_REF_RE = re.compile(r"^t-[0-9a-f]{16}\Z")


def _skills() -> Any:
    return importlib.import_module(_package().__name__ + ".fleet_skills")


def _skill_body(body: Any, *extra: str) -> dict:
    b = _body(body)
    ok = isinstance(b.get("twin"), str) and _TWIN_REF_RE.match(b["twin"]) and isinstance(b.get("skill"), str) \
        and _SKILL_KEY_RE.match(b["skill"]) and all(k in b for k in extra)
    if not ok:
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    return b


@router.get("/fleet/skills")
async def fleet_skills_get() -> Any:
    """Which of each twin's skills are shared with (or taken from) the owner's other computers. No network."""
    svc = await asyncio.to_thread(_svc)
    return await _call(_skills().view, svc)


@router.post("/fleet/skills/switch")
async def fleet_skills_switch(body: Any = Body(None)) -> Any:
    """{"on": bool}: this computer's switch for skills (the sharing computer sends them, a linked one takes them)."""
    b = _body(body)
    if "on" not in b:
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    on = _bool(b, "on")
    svc = await asyncio.to_thread(_svc)
    return await _call(_skills().switch, svc, on)


@router.post("/fleet/skills/item")
async def fleet_skills_item(body: Any = Body(None)) -> Any:
    """{"twin", "skill", "on": bool}: one skill's own switch (not shared / only on this computer when off)."""
    b = _skill_body(body, "on")
    on = _bool(b, "on")
    svc = await asyncio.to_thread(_svc)
    return await _call(_skills().set_item, svc, b["twin"], b["skill"], on)


@router.post("/fleet/skills/keep")
async def fleet_skills_keep(body: Any = Body(None)) -> Any:
    """{"twin", "skill", "keep": "mine" | "theirs"}: settle a skill that differs from the sharing computer's."""
    b = _skill_body(body, "keep")
    if b.get("keep") not in ("mine", "theirs"):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(_skills().keep, svc, b["twin"], b["skill"], b["keep"])


@router.post("/fleet/skills/restore")
async def fleet_skills_restore(body: Any = Body(None)) -> Any:
    """{"twin", "skill", "vid"}: put an earlier version of a skill back on this computer."""
    b = _skill_body(body, "vid")
    if not isinstance(b.get("vid"), str) or not _SKILL_VID_RE.match(b["vid"]):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(_skills().restore, svc, b["twin"], b["skill"], b["vid"])


# ---- what syncs between a Grok bot and its twin (twin/sync_panel.py) -----------------
_SYNC_WORD_RE = re.compile(r"^[a-z_]{1,20}$")


def _sync_panel() -> Any:
    return importlib.import_module(_package().__name__ + ".sync_panel")


@router.get("/sync/panel")
async def sync_panel_get() -> Any:
    """Every twin on this computer: its eight rows, which way each goes, when it last synced. Local files only."""
    svc = await asyncio.to_thread(_svc)
    return await _call(_sync_panel().view, svc)


@router.post("/sync/panel")
async def sync_panel_set(body: Any = Body(None)) -> Any:
    """{"twin": id, "item": key, "direction": d}: the owner's choice for one item of one twin."""
    b = _body(body)
    if not isinstance(b.get("twin"), str) or not _ID_RE.match(b["twin"]) or \
            not all(isinstance(b.get(k), str) and _SYNC_WORD_RE.match(b[k]) for k in ("item", "direction")):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(_sync_panel().choose, svc, b["twin"], b["item"], b["direction"])


# ---- session briefs (twin/briefs.py) ---------------------------------------------------------------
_BRIEF_WAYS = ("grok_to_hermes", "hermes_to_grok", "both", "off")


@router.get("/briefs")
async def briefs_get() -> Any:
    """Every twin's session-brief direction, counts and the latest briefs from Grok (already filtered)."""
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.briefs_info)


@router.post("/briefs")
async def briefs_set(body: Any = Body(None)) -> Any:
    """{"twin": id, "direction": grok_to_hermes | hermes_to_grok | both | off}; off by default for every twin."""
    b = _body(body)
    twin = _twin_word(b)
    if b.get("direction") not in _BRIEF_WAYS:
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.briefs_set, twin, b["direction"])


@router.post("/briefs/now")
async def briefs_now(body: Any = Body(None)) -> Any:
    """{"twin"?: id}: one round now (counts only, never a brief's text)."""
    b = _body(body)
    twin = _twin_word(b) if b.get("twin") is not None else None
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.briefs_now, twin)


@router.post("/briefs/forget")
async def briefs_forget(body: Any = Body(None)) -> Any:
    """{"twin": id}: delete every brief kept for this twin; its briefs go off."""
    twin = _twin_word(_body(body))
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.briefs_forget, twin)


# ---- Hermes -> Grok through the Mirror bot (twin/h2g.py, twin/h2g_service.py) ----------------------------------------
_H2G_ITEMS = ("name", "description", "instructions", "routines")
_H2G_CHANGE_RE = re.compile(r"^c-[0-9a-f]{16}$")
_H2G_RKEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _h2g_bad() -> HTTPException:
    return HTTPException(status_code=400, detail={"code": "bad_input", "message": "That request was not understood."})


def _h2g_items(b: dict) -> Any:
    items = b.get("items", "all")
    if items == "all":
        return "all"
    if not isinstance(items, list) or not items or len(items) > 4 or not all(i in _H2G_ITEMS for i in items):
        raise _h2g_bad()
    return items


@router.get("/pairs/{pid}/to-grok")
async def to_grok_get(pid: str) -> Any:
    """What the Mirror may change in Grok for this twin, its recent changes, and the preview of what would go now."""
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.to_grok_info, _pid(pid))


_H2G_STATUS = {"refused": 403, "not_allowed": 403, "no_permission": 403, "no_bot": 409, "stale": 409,
               "pending": 409, "changed_in_grok": 409, "undone": 409, "is_move": 409, "empty": 409, "no_change": 404,
               "not_found": 404, "internal": 500}


async def _h2g_call(fn: Callable[..., Any], *args: Any) -> Any:
    """A Hermes -> Grok change: an error is an HTTP 4xx (or 500) with {"detail": {"code", "message", "hint"?}}."""
    out = await _call(fn, *args)
    if isinstance(out, dict) and set(out) == {"error"} and isinstance(out["error"], dict):
        raise HTTPException(status_code=_H2G_STATUS.get(out["error"].get("code"), 400), detail=out["error"])
    return out


@router.post("/pairs/{pid}/to-grok/{action}")
async def to_grok_post(pid: str, action: str, body: Any = Body(None)) -> Any:
    """allow / disallow {"items": [..] | "all"}; apply / undo {"change_id": "c-…"};
    resolve {"item": "name"|"description"|"instructions", "keep": "hermes"|"grok"}; runner {"routine": key, "to": "grok"|"hermes"};
    give-up {} (stop waiting for the Mirror's answer). Every change refuses inside a request from another computer
    (tasks.asked_turn); nothing is sent for a twin the owner never allowed, and apply / undo take only the exact
    change id the owner saw (a stale one is refused). Disallow (which only takes changes back) is always accepted."""
    b = _body(body)
    p = _pid(pid)
    svc = await asyncio.to_thread(_svc)
    if action == "allow":  # the page's own route: never a request from another computer (tasks.asked_turn)
        return await _h2g_call(svc.to_grok_allow, p, _h2g_items(b), True)
    if action == "disallow":
        return await _h2g_call(svc.to_grok_disallow, p, _h2g_items(b))
    if action in ("apply", "undo"):
        if not isinstance(b.get("change_id"), str) or not _H2G_CHANGE_RE.match(b["change_id"]):
            raise _h2g_bad()
        return await _h2g_call(getattr(svc, f"to_grok_{action}"), p, b["change_id"], True)
    if action == "resolve":
        if b.get("item") not in _H2G_ITEMS[:3] or b.get("keep") not in ("hermes", "grok"):
            raise _h2g_bad()
        return await _h2g_call(svc.to_grok_resolve, p, b["item"], b["keep"], True)
    if action == "runner":
        if not isinstance(b.get("routine"), str) or not _H2G_RKEY_RE.match(b["routine"]) or \
                b.get("to") not in ("grok", "hermes"):
            raise _h2g_bad()
        return await _h2g_call(svc.to_grok_runner, p, b["routine"], b["to"], True)
    if action == "give-up":
        return await _h2g_call(svc.to_grok_give_up, p, True)
    raise HTTPException(status_code=404, detail={"code": "not_found", "message": "That request was not understood."})
