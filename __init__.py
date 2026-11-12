"""Grok Bot Twin, a Hermes plugin: give a Grok bot a twin in Hermes that keeps up with it.

register() is fast and pure: it only records tools, hooks and commands. No disk I/O, no threads, no
subprocesses, no ctx.state / ctx.llm. The background check starts lazily on the first session hook.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .twin import awareness, cli, slash, tasks_api, tasks_run, timer, tools

log = logging.getLogger(__name__)

_ctx: Any = None
# The one skill this plugin ships (read-only text Hermes shows the model on request): setting the plugin up in chat.
SKILL_NAME = "set-up-grok-bot-twin"
SKILL_DESCRIPTION = "Set up Grok Bot Twin and link your computers."
# The skill Hermes follows when the owner picks "Let Hermes do the Grok part" (twin/grok_part.py guards it).
GROK_PART_SKILL = "finish-twins-in-grok"
GROK_PART_DESCRIPTION = "Ask new twins' Grok bots for their setup export."


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
        profile = _profile(svc)
        asked = tasks_run.is_sent_turn(kwargs.get("user_message"))  # a request another computer sent here
        blocks = [b for b in (awareness.build_block(profile, svc), tasks_api.context_block(profile, asked)) if b]
        return {"context": "\n".join(blocks)} if blocks else None
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


def _on_pre_tool_call(**kwargs: Any):
    """Says nothing unless "Let Hermes do the Grok part" is on and the call is computer_use (twin/grok_part.py)."""
    if kwargs.get("tool_name") != "computer_use":
        return None
    try:
        from .twin import grok_part
        return grok_part.guard(kwargs.get("tool_name"), kwargs.get("args"), kwargs.get("session_id"))
    except Exception:  # guard() never raises (an error while the flow is on blocks there); this is a broken install
        log.debug("grok-bot-twin: pre_tool_call guard failed", exc_info=True)
        return _guard_broken()


def _guard_broken():
    """The guard could not run: refuse the step while the Grok part flow may be on (its file is there), else no say."""
    import os
    try:
        env = os.environ.get("HERMES_HOME", "").strip()
        roots = {Path(env).expanduser() if env else Path.home() / ".hermes"}
        try:
            from hermes_constants import get_default_hermes_root
            roots.add(Path(get_default_hermes_root()))
        except Exception:
            pass
        on = any((r / "plugin-data" / "grok-bot-twin" / "grok-part.json").exists() for r in roots)
    except Exception:
        on = True
    if not on:
        return None
    return {"action": "block", "message": "Grok Bot Twin (Let Hermes do the Grok part): the step could not be checked, "
            "so it was not done. Stop here and tell the owner in one sentence what you wanted to do."}


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
                      description="Recent activity of a Grok bot twin (when and what kind; never content)", emoji="🪞")
    ctx.register_tool(tasks_api.TOOL_NAME, tools.TOOLSET, tasks_api.TOOL_SCHEMA, tasks_api.twin_ask_machine,
                      description="Ask this twin's copy on another of your computers to do something there",
                      emoji="🪞")
    ctx.register_tool("twin_transcripts_search", tools.TOOLSET, tools.TWIN_TRANSCRIPTS_SCHEMA,
                      tools.twin_transcripts_search,
                      description="Search this twin's kept conversations (only when full transcripts are on)",
                      emoji="🪞")
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_cli_command(name="twin", help="Grok Bot Twin: give your Grok bots a twin in Hermes",
                             setup_fn=cli.setup, handler_fn=cli.handle,
                             description="Your twins and the next step: hermes twin")
    ctx.register_command("twin", handler=slash.handle, description="Your Grok bot twins: status, check now, sample",
                         args_hint="[status|sync|conflicts|recent|ask|requests|demo]")
    ctx.on_unload(_unload)
    _register_skill(ctx)


def _register_skill(ctx) -> None:
    """Hands Hermes the place of the plugin's two skills (skills/NAME/SKILL.md). The plugin opens no file here; a
    Hermes that has no plugin skills simply gets none."""
    add = getattr(ctx, "register_skill", None)
    if not callable(add):
        return
    for name, description in ((SKILL_NAME, SKILL_DESCRIPTION), (GROK_PART_SKILL, GROK_PART_DESCRIPTION)):
        try:
            add(name, Path(__file__).resolve().parent / "skills" / name / "SKILL.md", description,
                {"name": name, "description": description})
        except Exception:
            log.debug("grok-bot-twin: the skill %s was not registered", name, exc_info=True)
