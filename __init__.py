"""Grok Bot Twin, a Hermes plugin: keep a Grok bot and its Hermes twin in step.

register() is fast and pure: it only records tools, hooks and commands. No disk I/O, no threads, no
subprocesses, no ctx.state / ctx.llm. The background check starts lazily on the first session hook.
"""
from __future__ import annotations

import logging
from typing import Any

from .twin import awareness, cli, slash, timer, tools

log = logging.getLogger(__name__)

_ctx: Any = None


def _service():
    from .twin.service import get_service
    svc = get_service()
    if _ctx is not None and svc.ctx is None:
        svc.ctx = _ctx
    return svc


def _profile(svc) -> str:
    return svc.profile()


def _on_pre_llm_call(**kwargs: Any):
    """Adds a short, data-only note to the turn when this profile is a Grok twin (else nothing)."""
    try:
        svc = _service()
        timer.ensure_started(_service)
        block = awareness.build_block(_profile(svc), svc)
        return {"context": block} if block else None
    except Exception:
        log.debug("grok-bot-twin: pre_llm_call skipped", exc_info=True)
        return None


def _on_session_start(**kwargs: Any) -> None:
    try:
        svc = _service()
        timer.ensure_started(_service)
        if svc.on_session_start():
            timer.kick()  # debounced (30 s) check + sync for a twinned profile
    except Exception:
        log.debug("grok-bot-twin: on_session_start skipped", exc_info=True)


def _on_session_end(**kwargs: Any) -> None:
    """Fires every turn: records only a debounced timestamp marker, never content."""
    try:
        _service().record_activity()
    except Exception:
        log.debug("grok-bot-twin: on_session_end skipped", exc_info=True)


def _unload() -> None:
    timer.stop()


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_tool("twin_status", tools.TOOLSET, tools.TWIN_STATUS_SCHEMA, tools.twin_status,
                      description="Status of this profile's Grok bot twin", emoji="🪞")
    ctx.register_tool("twin_recent", tools.TOOLSET, tools.TWIN_RECENT_SCHEMA, tools.twin_recent,
                      description="Recent activity of a Grok bot twin (metadata only)", emoji="🪞")
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_cli_command(name="twin", help="Grok Bot Twin: keep a Grok bot and its Hermes twin in step",
                             setup_fn=cli.setup, handler_fn=cli.handle,
                             description="Try it without Grok: hermes twin demo")
    ctx.register_command("twin", handler=slash.handle, description="Grok Bot Twin status, sync and demo",
                         args_hint="[status|sync|conflicts|recent|demo]")
    ctx.on_unload(_unload)
