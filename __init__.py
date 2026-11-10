"""search-board: a search structure for problems with an unknown path, and the workflow that
takes a finished one through the operator's review and an independent verification.

Ported from the board in s0ma. Registers the board tools, the evidence ledger, the standing
block, the subagent tracker, the ``/sboard`` command, the bundled skill, and (on first use) the
workflow's background thread. State lives in ``<hermes home>/plugin-data/search-board/boards.db``.
"""

from __future__ import annotations

import logging
import secrets
import threading
from pathlib import Path

log = logging.getLogger(__name__)

PLUGIN = "search-board"
DB_FILE = "boards.db"

_store = None
_store_lock = threading.Lock()
_db_path = None  # fixed at register(), in this plugin's own profile
_poller = None
_verifiers = None


def store():
    """The board store for this plugin's profile."""
    global _store
    with _store_lock:
        if _store is None:
            from .board_store import Store

            path = _db_path
            if path is None:
                from plugins.plugin_storage import plugin_data_dir

                path = plugin_data_dir(PLUGIN) / DB_FILE
            _store = Store.at(path)
        return _store


def changed(board: str = "") -> None:
    """Tell open Desktop panes a board changed (a no-op where no Desktop is attached)."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(PLUGIN, "board.changed", {"board": board})
    except Exception as error:
        log.debug("search-board: change event not sent: %s", error)


def session_key_of(kwargs: dict) -> str:
    """The calling session's routing key: where a turn is started when the operator moves a board."""
    try:
        from gateway.session_context import get_session_env

        key = get_session_env("HERMES_SESSION_KEY")
    except Exception:
        key = ""
    return str(key or kwargs.get("session_key") or kwargs.get("session_id") or "")


def ensure_poller() -> None:
    """Start the workflow's thread on first use (not at load, so validate/doctor start nothing)."""
    if _poller is not None:
        _poller.start()


def register(ctx) -> None:
    # Imported here, not at module level, so this file imports on its own (test collectors do that).
    from . import board_commands, board_flow, board_hooks
    from .board_tools import SCHEMAS, TOOLSET, Tools, is_subagent, terminal_runner

    global _db_path, _poller, _store, _verifiers
    try:
        from plugins.plugin_storage import plugin_data_dir

        _db_path = plugin_data_dir(PLUGIN) / DB_FILE
        _store = None
    except Exception as error:
        log.debug("search-board: data dir not resolved at load: %s", error)

    proc = secrets.token_hex(8)  # this process: a verifier launched by another one is gone
    _poller = board_flow.Poller(store, board_flow.injector(ctx), proc)
    ctx.on_unload(_poller.stop)
    _verifiers = board_flow.Verifiers(store, lambda: getattr(ctx, "subagent_lifecycle", None), proc)

    def main_turn(kwargs: dict) -> bool:
        session = str(kwargs.get("session_id") or "")
        return not kwargs.get("parent_session_id") and not is_subagent(session)

    def in_turn(kwargs: dict) -> None:
        """Anything that can only happen during an agent turn: launching and cancelling verifiers."""
        if main_turn(kwargs):
            _verifiers.in_turn()

    runner = terminal_runner(ctx.dispatch_tool)
    tools = Tools(store, lambda: runner, changed,
                  reader=lambda kwargs: board_flow.file_reader(ctx.dispatch_tool, kwargs),
                  session_key_of=session_key_of)
    for name, schema in SCHEMAS.items():
        handler = tools.handler(name)

        def with_poller(args, _handler=handler, **kwargs):
            ensure_poller()
            result = _handler(args, **kwargs)
            in_turn(kwargs)  # after the call: a verification it asked for can start now
            return result

        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=with_poller,
                          description=schema["description"].split(". ")[0], emoji="🧭")

    context_hook = board_hooks.make_context_hook(store)
    evidence_hook = board_hooks.make_tool_result_hook(store)

    def on_pre_llm_call(**kwargs):
        ensure_poller()
        in_turn(kwargs)
        return context_hook(**kwargs)

    def on_tool_result(**kwargs):
        in_turn(kwargs)
        return evidence_hook(**kwargs)

    def on_subagent_start(**kwargs):
        board_hooks.on_subagent_start(**kwargs)
        _verifiers.on_child_start(**kwargs)

    def on_subagent_stop(**kwargs):
        if _verifiers.on_child_stop(**kwargs):
            changed()
            _poller.deliver(store())  # if the agent filed its check already, it hears now

    ctx.register_hook("transform_tool_result", on_tool_result)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("subagent_start", on_subagent_start)
    ctx.register_hook("subagent_stop", on_subagent_stop)

    def sboard(raw_args: str = "", *, platform: str = "", session_key: str = "", **_kwargs) -> str:
        ensure_poller()
        text = board_commands.run(store(), raw_args)
        changed()
        if _poller is not None:
            _poller.deliver(store())  # a move that wakes the agent does it now, not in 3 s
        return board_commands.format_for(platform, text)

    ctx.register_command("sboard", sboard, description="Search boards: show, review, verify, accept, send back",
                         args_hint="[show|review|comment|back|verify|accept|discard|…] <board> …")

    skill = Path(__file__).parent / "skills" / "search-board" / "SKILL.md"
    if skill.exists():
        try:
            ctx.register_skill("search-board", skill)
        except Exception as error:  # an older Hermes without plugin skills still gets the tools
            log.debug("search-board: skill not registered: %s", error)
