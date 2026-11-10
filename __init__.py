"""search-board: a search structure for problems with an unknown path, and the workflow that
takes a finished one through the operator's review and an independent verification.

Ported from the board in s0ma. Registers the board tools, the evidence ledger, the standing
block, the subagent tracker, the ``/sboard`` command, the bundled skill, and (on first use) the
workflow's background thread. State lives in ``<hermes home>/plugin-data/search-board/boards.db``.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

PLUGIN = "search-board"
DB_FILE = "boards.db"

_store = None
_store_lock = threading.Lock()
_db_path = None  # fixed at register(), in this plugin's own profile
_poller = None


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
    from .board_tools import SCHEMAS, TOOLSET, Tools, terminal_runner

    global _db_path, _poller, _store
    try:
        from plugins.plugin_storage import plugin_data_dir

        _db_path = plugin_data_dir(PLUGIN) / DB_FILE
        _store = None
    except Exception as error:
        log.debug("search-board: data dir not resolved at load: %s", error)

    llm = getattr(ctx, "llm", None)
    complete = getattr(llm, "complete_structured", None) if llm is not None else None
    _poller = board_flow.Poller(store, complete, board_flow.injector(ctx), changed)
    ctx.on_unload(_poller.stop)

    runner = terminal_runner(ctx.dispatch_tool)
    tools = Tools(store, lambda: runner, changed,
                  reader=lambda kwargs: board_flow.file_reader(ctx.dispatch_tool, kwargs),
                  session_key_of=session_key_of)
    for name, schema in SCHEMAS.items():
        handler = tools.handler(name)

        def with_poller(args, _handler=handler, **kwargs):
            ensure_poller()
            return _handler(args, **kwargs)

        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=with_poller,
                          description=schema["description"].split(". ")[0], emoji="🧭")

    context_hook = board_hooks.make_context_hook(store)

    def on_pre_llm_call(**kwargs):
        ensure_poller()
        return context_hook(**kwargs)

    ctx.register_hook("transform_tool_result", board_hooks.make_tool_result_hook(store))
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("subagent_start", board_hooks.on_subagent_start)

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
