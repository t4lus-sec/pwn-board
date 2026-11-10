"""The operator's side of a board: the ``/sboard`` command and the actions behind it.

Notes land here and only here: nothing in the tool surface writes one, which is
what makes the direction one-way by construction rather than by rule. The same
store actions back the Desktop pane's buttons (``dashboard/plugin_api.py``).

The workflow's moves are here too: review a finished board (its write-up and the
files as delivered), leave review notes, send it back for another round, send it to
verification, accept it as complete, discard it. A move that needs the agent starts a
turn in the board's session where Hermes allows it, and otherwise reaches the agent
with its next turn.
"""

from __future__ import annotations

import shlex
from typing import Optional

from . import board_render as render
from .board_store import Refused, Store, label, parse_ref

HELP = """/sboard — search boards
  /sboard                      boards that are live or waiting for you
  /sboard all                  every board, closed ones too
  /sboard show <name> [all]    the board as a tree (all: unfold ruled-out intents)
  /sboard detail <name> <ref>  one intent, finding or note in full: "Finding 3", "Intent 2" or F3, I2
  /sboard note <name> [Intent 3:] <text>   leave a note (about an intent or finding if you start with it)
  /sboard unnote <name> <Note 2>     take a note off
  /sboard extend <name> <steps>      give a suspended board more steps
  /sboard confirm <name>       accept a proposed_solved board
  /sboard exhaust <name>       accept a proposed_exhausted board
  /sboard reject <name> [why]  send a proposal back; your reason becomes a note
  /sboard abandon <name> [why] stop a search

 the workflow (a finished board: write-up → your review → verification → complete)
  /sboard review <name>        the write-up, the files as delivered, your review notes and the answers
  /sboard file <name> <path>   one delivered file, as it was delivered
  /sboard comment <name> <text>      leave a review note (the agent answers each before writing up again)
  /sboard uncomment <name> <Review note 2>  take back a review note the agent hasn't answered
  /sboard back <name> [why]    send it back to work: open again, a new round
  /sboard verify <name>        send it to verification: an independent verifier and the agent check it
  /sboard verification <name>  both checks and the reconciled report
  /sboard reverify <name>      run a verifier that failed again
  /sboard accept <name>        accept the verified board: complete
  /sboard toreview <name> [why]      take it back to review, from verification or complete
  /sboard discard <name> [why] throw it away, wherever it is"""

LOCAL_SURFACES = {"cli", "tui", "local", "desktop", ""}

WAKES = ("The agent is told now if this plugin may start a turn (allow_gateway_injection), "
         "otherwise with its next turn.")


# ---------------------------------------------------------------- the command


def _list(store: Store, include_closed: bool) -> str:
    boards = store.boards(include_closed=include_closed)
    if not boards:
        return "No boards" + ("" if include_closed else " live or waiting for you") + ". The agent opens one with search_board_open."
    lines = [f"{len(boards)} board(s):"]
    for board in boards:
        lines.append("  " + render.board_line(board, store.counts(board)))
    waiting = []
    for board in boards:
        state = store.counts(board).get("workflow") or ""
        if state == "in review" or state.startswith("verified"):
            waiting.append(f"{board.name} ({state})")
    if waiting:
        lines.append(f"[!] waiting for you: {', '.join(waiting)} (/sboard review <name>)")
    return "\n".join(lines)


def run(store: Store, raw_args: str) -> str:
    try:
        parts = shlex.split(raw_args or "")
    except ValueError:
        parts = (raw_args or "").split()
    if not parts or parts[0] in ("list", "ls"):
        return _list(store, False)
    verb, rest = parts[0].lower(), parts[1:]
    try:
        if verb == "all":
            return _list(store, True)
        if verb in ("help", "-h", "--help"):
            return HELP
        if verb == "show" and rest:
            snap = store.snapshot(rest[0])
            return render.tree(snap, fold_ruled_out=not (len(rest) > 1 and rest[1] == "all"))
        if verb == "detail" and len(rest) >= 2:
            snap = store.snapshot(rest[0])
            key = parse_ref(" ".join(rest[1:]))
            evidence = []
            for finding in snap.findings:
                if finding.id == key:
                    evidence = store.evidence(finding.evidence)
            return render.detail(snap, key, evidence)
        if verb in ("note", "hint") and len(rest) >= 2:
            note, board = store.add_note(rest[0], " ".join(rest[1:]))
            about = f" about {label(note.about)}" if note.about else ""
            return (f"{label(note.id)} left on '{board.name}'{about}. It stays on the board; the agent sees it on "
                    "every read, marked new on its next. The current exploration is not interrupted.")
        if verb in ("unnote", "unhint") and len(rest) >= 2:
            board = store.remove_note(rest[0], " ".join(rest[1:]))
            return f"{label(parse_ref(' '.join(rest[1:])))} taken off '{board.name}'."
        if verb == "extend" and len(rest) == 2:
            board = store.operator_extend(rest[0], rest[1])
            return f"'{board.name}' now at steps {board.steps_used}/{board.step_budget} [{board.status}]"
        if verb in ("confirm", "exhaust") and len(rest) == 1:
            board = store.operator_decide(rest[0], verb)
            return f"'{board.name}' is {board.status}, and complete. The agent hears with its next turn."
        if verb in ("reject", "abandon") and rest:
            board = store.operator_decide(rest[0], verb, " ".join(rest[1:]))
            return f"'{board.name}' is {board.status}. {WAKES if board.live else 'The agent hears with its next turn.'}"
        # -- the workflow
        if verb == "review" and len(rest) == 1:
            return render.review(store.workflow(rest[0]))
        if verb == "file" and len(rest) >= 2:
            meta, content = store.delivered_file(rest[0], " ".join(rest[1:]))
            head = f"{meta.path} as delivered" + (" (cut)" if meta.truncated else "")
            return head + (f" — not kept: {meta.error}" if meta.error else "") + "\n" + content
        if verb == "comment" and len(rest) >= 2:
            note = store.comment(rest[0], " ".join(rest[1:]))
            return (f"{label(note.id)} added to '{rest[0]}' (round {note.round}). The agent sees your notes when you send "
                    "the board back, and must answer each before writing it up again.")
        if verb == "uncomment" and len(rest) >= 2:
            ref = " ".join(rest[1:])
            store.uncomment(rest[0], ref)
            return f"{label(parse_ref(ref) if not ref.isdigit() else 'R' + ref)} taken back."
        if verb == "back" and rest:
            board, flow = store.send_back(rest[0], " ".join(rest[1:]))
            return f"'{board.name}' is back with the agent, {board.status} again (round {flow.round}). {WAKES}"
        if verb == "verify" and len(rest) == 1:
            board, flow = store.to_verification(rest[0])
            return (f"'{board.name}' is in verification: the independent verifier and the agent check it, each on "
                    f"its own; the verifier's report is held from the agent until it files its check. {WAKES}")
        if verb == "verification" and len(rest) == 1:
            return render.verification(store.workflow(rest[0]))
        if verb == "reverify" and len(rest) == 1:
            store.retry_verifier(rest[0])
            return f"The verifier of '{rest[0]}' will run again."
        if verb == "accept" and len(rest) == 1:
            board, flow = store.accept(rest[0])
            return f"'{board.name}' is complete: {board.status}, verified ({flow.verdict}), accepted by you."
        if verb == "toreview" and rest:
            board, _ = store.to_review(rest[0], " ".join(rest[1:]))
            return f"'{board.name}' is in your review again."
        if verb == "discard" and rest:
            board, _ = store.discard(rest[0], " ".join(rest[1:]))
            return f"'{board.name}' is discarded (abandoned). The agent hears with its next turn."
    except Refused as refused:
        return f"search-board: {refused}"
    return HELP


def format_for(platform: Optional[str], text: str) -> str:
    """Monospace on messaging platforms, where a tree only lines up inside a code block."""
    if (platform or "") in LOCAL_SURFACES:
        return text
    return f"```\n{text}\n```"
