"""Hooks: the evidence ledger, the standing block, and who is a subagent.

**Evidence.** While any board is open, every tool result is recorded (an
excerpt, its length and a SHA-256 of the whole) and tagged with an id the model
can cite: ``"_evidence": "e12"`` added to a JSON object result, or a last line
``[evidence e12]`` otherwise. A finding citing an id nobody recorded is refused,
so a finding is always backed by something a tool actually returned. Results of
the board's own tools are not recorded: they say what the model wrote, not what
the world showed. Nothing is recorded while no board is open.

**The standing block.** Before each turn of the main agent, one line per board
that is live or waiting for the operator, plus anything the operator decided
since. It is injected into the turn's user message (Hermes's ``pre_llm_call``),
never persisted, and absent when there is nothing to say. A search nobody looks
at after a compaction is a search that quietly stops; this keeps it visible.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable

from . import board_render as render
from .board_store import Store
from .board_tools import SUBAGENT_SESSIONS, note_subagent

log = logging.getLogger(__name__)

PRUNE_EVERY = 200
_counter = 0
_counter_lock = threading.Lock()

BLOCK_HEADER = "[search boards: current state, rendered by the search-board plugin]"


def tag_result(result: str, evidence_id: int) -> str:
    """Add the evidence id to a tool result without breaking JSON for whatever reads it next."""
    tag = f"e{evidence_id}"
    stripped = result.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            body = json.loads(stripped)
        except ValueError:
            body = None
        if isinstance(body, dict) and "_evidence" not in body:
            body["_evidence"] = tag
            return json.dumps(body, ensure_ascii=False)
    return f"{result}\n[evidence {tag}]"


def make_tool_result_hook(store_of: Callable[[], Store]):
    def on_tool_result(tool_name: str = "", args=None, result=None, **kwargs):
        global _counter
        try:
            if not isinstance(result, str) or not tool_name or tool_name.startswith("search_board_"):
                return None
            store = store_of()
            if not store.any_live():
                return None
            evidence_id = store.record_evidence(
                tool_name=tool_name, args=args, result=result,
                conversation_id=str(kwargs.get("conversation_id") or ""),
                session_id=str(kwargs.get("session_id") or ""),
                tool_call_id=str(kwargs.get("tool_call_id") or ""),
            )
            with _counter_lock:
                _counter += 1
                due = _counter % PRUNE_EVERY == 0
            if due:
                store.prune_evidence()
            return tag_result(result, evidence_id)
        except Exception as error:  # fail open: never break a tool result
            log.debug("search-board evidence hook failed: %s", error)
            return None

    return on_tool_result


def make_context_hook(store_of: Callable[[], Store]):
    def on_pre_llm_call(session_id: str = "", parent_session_id: str = "", **kwargs):
        try:
            if parent_session_id or (session_id and str(session_id) in SUBAGENT_SESSIONS):
                return None  # a subagent works from its goal; the block is the main agent's
            store = store_of()
            boards = store.boards()
            notices = store.take_notices()
            if not boards and not notices:
                return None
            lines = [BLOCK_HEADER]
            if boards:
                for board in boards[:6]:
                    lines.append(render.board_line(board, store.counts(board)))
                if len(boards) > 6:
                    lines.append(f"· …and {len(boards) - 6} more (search_board_list)")
                lines.append("search_board_read <name> for the graph. Nothing here advances on its own. "
                             "While a board is open, tool results carry evidence ids to cite in findings.")
            if notices:
                lines.append("The operator, since your last turn:")
                lines.extend(notices)
            return {"context": "\n".join(lines)}
        except Exception as error:
            log.debug("search-board context hook failed: %s", error)
            return None

    return on_pre_llm_call


def on_subagent_start(child_session_id=None, **kwargs):
    note_subagent(child_session_id)
