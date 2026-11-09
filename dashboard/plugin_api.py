"""REST backend for the Twins page, mounted by Hermes at /api/plugins/grok-bot-twin/.

Every handler validates its input (400), maps an unknown twin to 404, runs blocking work in a worker
thread (asyncio.to_thread) and answers engine problems as HTTP 200 {"error": {"code", "message"}}.
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
    except service.PairNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.as_dict())
    except service.BadInput as exc:
        raise HTTPException(status_code=400, detail=exc.as_dict())
    except engine.EngineError as exc:
        return {"error": exc.as_dict()}
    except Exception as exc:  # never leak a traceback to the UI
        log.exception("grok-bot-twin: REST call failed")
        return {"error": {"code": "internal", "message": f"Something went wrong ({exc.__class__.__name__})"}}


def _pid(pid: str) -> str:
    if not _ID_RE.match(pid or "") or pid in (".", ".."):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "twin ids look like acme-helper"})
    return pid


def _body(body: Any) -> dict:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": "the body must be a JSON object"})
    return body


def _bool(body: dict, key: str) -> bool:
    v = body.get(key, False)
    if not isinstance(v, bool):
        raise HTTPException(status_code=400, detail={"code": "bad_input", "message": f"{key} must be true or false"})
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


@router.get("/doctor")
async def doctor() -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.doctor)


@router.get("/engine/trust")
async def engine_trust_status() -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.trust_info)


@router.post("/engine/trust")
async def engine_trust(body: Any = Body(None)) -> Any:
    b = _body(body)
    # the attestation is mandatory; {"allow_unattested": true} is the explicit, recorded opt-out (a local build).
    # {"tag": "vX.Y.Z"} overrides the expected release tag (validated by the service).
    allow = _bool(b, "allow_unattested")
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.trust_botport, allow, b.get("tag"))


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
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.switch, _pid(pid), b.get("job_ids"))


@router.post("/pairs/{pid}/send-to-grok")
async def send_to_grok(pid: str) -> Any:
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.send_to_grok, _pid(pid))


@router.post("/pairs/{pid}/approve")
async def approve(pid: str, body: Any = Body(None)) -> Any:
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.approve, _pid(pid), b.get("item"))


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


@router.post("/demo")
async def demo(body: Any = Body(None)) -> Any:
    b = _body(body)
    svc = await asyncio.to_thread(_svc)
    return await _call(svc.demo_action, b.get("action"), b.get("profile"))
