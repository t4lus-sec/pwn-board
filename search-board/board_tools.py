"""The model's side of a board: tool schemas and handlers.

Every handler returns a JSON string and never raises: a request the board
refuses comes back as ``{"ok": false, "error": ...}`` the model can act on.

Who is calling is decided here, never by the model: a worker that could name
itself could steal another worker's claim. The main agent is keyed by its
conversation (stable across compression); a subagent by its own session, which
the ``subagent_start`` hook reported.
"""

from __future__ import annotations

import json
import threading
from typing import Callable, Optional

from . import board_render as render
from .board_store import (
    MAX_EVIDENCE_PER_FINDING, MAX_LEADS_PER_CALL, MAX_STEP_BUDGET, Refused, Store, label, labels, now_utc,
    parse_ref,
)

TOOLSET = "search_board"

# Sessions of subagents, reported by the subagent_start hook; the verifiers among them.
SUBAGENT_SESSIONS: set[str] = set()
VERIFIER_SESSIONS: set[str] = set()
READ_ONLY = ("search_board_list", "search_board_read", "search_board_review")
_SUB_LOCK = threading.Lock()

# Boards whose goal test is running in this process.
_TESTS_RUNNING: set[str] = set()
_TEST_LOCK = threading.Lock()

TEST_TIMEOUT_SECS = 600

# A goal-test runner: (command, timeout, call kwargs) -> (outcome, exit_code, output, error), where
# outcome is "ran" (exit_code is the verdict), "timed_out", or "not_run" (no verdict either way).
Runner = Callable[[str, int, dict], tuple]


def note_subagent(child_session_id: Optional[str]) -> None:
    if child_session_id:
        with _SUB_LOCK:
            SUBAGENT_SESSIONS.add(str(child_session_id))


def is_subagent(session_id: Optional[str]) -> bool:
    return bool(session_id) and str(session_id) in SUBAGENT_SESSIONS


def worker_of(kwargs: dict) -> tuple[str, bool]:
    session_id = str(kwargs.get("session_id") or "")
    if is_subagent(session_id):
        return f"sub:{session_id}", True
    conversation = str(kwargs.get("conversation_id") or session_id or kwargs.get("task_id") or "local")
    return f"main:{conversation}", False


def _ok(**fields) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False)


def _no(error: str, **fields) -> str:
    return json.dumps({"ok": False, "error": error, **fields}, ensure_ascii=False)


def _board_view(board, counts: Optional[dict] = None) -> dict:
    view = {
        "name": board.name, "title": board.title, "status": board.status,
        "origin": board.origin, "goal": board.goal, "goal_test": board.goal_test, "goal_test_kind": board.goal_test_kind,
        "steps": f"{board.steps_used}/{board.step_budget}",
    }
    if board.outcome:
        view["outcome"] = board.outcome
    if counts:
        view.update(counts)
    return view


def _lead_view(lead, at) -> dict:
    view = {"id": lead.id, "label": label(lead.id), "direction": lead.direction, "rationale": lead.rationale,
            "priority": lead.priority, "state": lead.state}
    if lead.from_findings:
        view["from_findings"] = [label(x) for x in lead.from_findings]
    if lead.state == "claimed":
        view["state"] = "open (claim went stale)" if lead.claim_expired(at) else "claimed"
        view["claimed"] = f"by {render.holder(lead.claimed_by)}, {render.ago(lead.claimed_at, at)}"
    return view


def _finding_view(f, at) -> dict:
    view = {"id": f.id, "label": label(f.id), "statement": f.statement, "polarity": f.polarity, "basis": f.basis,
            "evidence": [f"e{x}" for x in f.evidence], "recorded": render.ago(f.created_at, at)}
    if f.author != "model":
        view["by"] = "the goal test" if f.author == "plugin" else f.author
    if f.from_lead:
        view["from_lead"] = f.from_lead
    if f.from_findings:
        view["from_findings"] = f.from_findings
    return view


NAME = {"type": "string", "description": "The board's name, e.g. flaky-flush."}

SCHEMAS = {
    "search_board_list": {
        "name": "search_board_list",
        "description": (
            "List your search boards: name, title, status, how big the frontier is, how much of the "
            "step budget is spent, and whether the operator left new notes. Cheap orientation: use it "
            "to see what searches are live before committing to one."
        ),
        "parameters": {"type": "object", "properties": {
            "all": {"type": "boolean", "description": "Include closed boards. Default false."}}},
    },
    "search_board_open": {
        "name": "search_board_open",
        "description": (
            "Open a search board for a problem whose origin you know, whose goal you can test, and whose "
            "path you don't know: an intermittent failure, a regression hiding among many candidates, a "
            "number that has to cross a threshold. Not for plain to-do lists. Before building any "
            "structure, try the problem directly once: most problems are not actually hard. If that "
            "works, run search_board_test and you are done in one step; if it doesn't, what you learned "
            "is your first findings and leads. goal_test_kind decides who says you succeeded: prefer "
            "'command', because a shell command's exit status is a verdict you don't author. The test "
            "is fixed once the board opens. 'judgment' and 'observation' boards can only reach "
            "proposed_solved until the operator confirms."
        ),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Short stable handle, lowercase-with-hyphens, unique among open boards."},
            "title": {"type": "string", "description": "One line of prose."},
            "origin": {"type": "string", "description": "The starting state: what is known at the outset."},
            "goal": {"type": "string", "description": "What success is."},
            "goal_test": {"type": "string", "description": "How success gets checked. For 'command', the exact shell command: exit 0 means solved."},
            "goal_test_kind": {"type": "string", "enum": ["command", "observation", "judgment"]},
            "step_budget": {"type": "integer", "minimum": 1, "maximum": MAX_STEP_BUDGET,
                            "description": "Explorations allowed before the board suspends itself. Default 60."},
        }, "required": ["name", "title", "origin", "goal", "goal_test", "goal_test_kind"]},
    },
    "search_board_read": {
        "name": "search_board_read",
        "description": (
            "Read one board: the operator's notes first, then the frontier (open leads, highest "
            "priority first), then the most recent findings. This is your observe step: read before "
            "deciding anything. Findings are paged and the result says how many were left out, so "
            "never read absence into a short list."
        ),
        "parameters": {"type": "object", "properties": {
            "name": NAME,
            "offset": {"type": "integer", "minimum": 0, "description": "Skip this many findings, newest first. Default 0."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Findings to return. Default 20."},
        }, "required": ["name"]},
    },
    "search_board_leads": {
        "name": "search_board_leads",
        "description": (
            "Declare directions worth trying that nobody has tried yet. Each needs a rationale naming "
            "the findings that suggest it: a direction you can't justify from what you know is a guess "
            "with structure around it. Declaring costs no steps; exploring does. Rule out what the "
            "newest findings made pointless before declaring more."
        ),
        "parameters": {"type": "object", "properties": {
            "name": NAME,
            "leads": {"type": "array", "minItems": 1, "maxItems": MAX_LEADS_PER_CALL, "items": {
                "type": "object", "properties": {
                    "direction": {"type": "string", "description": "What to try, concrete enough to act on."},
                    "rationale": {"type": "string", "description": "Why this looks worth trying, naming the findings that suggest it."},
                    "from_findings": {"type": "array", "items": {"type": "string"},
                                      "description": "Findings this descends from, e.g. ['Finding 3'] (F3 also works). Empty only for leads seeded at the start."},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 9, "description": "Higher explores first. Default 5."},
                }, "required": ["direction", "rationale"]}},
        }, "required": ["name", "leads"]},
    },
    "search_board_claim": {
        "name": "search_board_claim",
        "description": (
            "Claim one open lead before exploring it, so two workers never explore the same branch. "
            "This can fail: if somebody else holds it you get ok:false and pick another rather than "
            "retrying. A claim goes stale after half an hour and returns to the frontier."
        ),
        "parameters": {"type": "object", "properties": {
            "name": NAME, "lead": {"type": "string", "description": "e.g. Intent 4 (L4 also works)."}}, "required": ["name", "lead"]},
    },
    "search_board_finding": {
        "name": "search_board_finding",
        "description": (
            "Record exactly one thing an exploration showed, citing the tool results that show it. While "
            "a board is open every tool result you receive carries an evidence id ('_evidence': 'e12' in "
            "JSON results, or a final '[evidence e12]' line); cite those. A finding citing an id that no "
            "tool result carries is refused. One finding per exploration: a finding written against your "
            "claimed lead marks it explored and spends a step. A negative result is a finding and matters "
            "more than it sounds: use polarity 'rules_out' for it."
        ),
        "parameters": {"type": "object", "properties": {
            "name": NAME,
            "lead": {"type": "string", "description": "The lead you claimed. Omit only for something learned outside any claimed lead."},
            "statement": {"type": "string", "description": "One claim, in prose."},
            "polarity": {"type": "string", "enum": ["supports", "rules_out"]},
            "basis": {"type": "string", "enum": ["observed", "inferred"],
                      "description": "'inferred' requires the findings it came from in from_findings."},
            "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_EVIDENCE_PER_FINDING,
                         "description": "Evidence ids of the tool results that show this, e.g. ['e12', 'e15']."},
            "from_findings": {"type": "array", "items": {"type": "string"},
                              "description": "For basis 'inferred', the findings this was reasoned from."},
        }, "required": ["name", "statement", "polarity", "basis", "evidence"]},
    },
    "search_board_rule_out": {
        "name": "search_board_rule_out",
        "description": (
            "Mark a lead ruled out because a finding eliminated it. Requires that finding (polarity "
            "'rules_out'): a branch you merely lost interest in stays on the frontier, and a branch ruled "
            "out by evidence is never re-explored."
        ),
        "parameters": {"type": "object", "properties": {
            "name": NAME, "lead": {"type": "string"},
            "finding": {"type": "string", "description": "A 'rules_out' finding on this board that eliminates it."},
        }, "required": ["name", "lead", "finding"]},
    },
    "search_board_test": {
        "name": "search_board_test",
        "description": (
            "Ask the plugin to run this board's goal test. It runs the command through Hermes's terminal "
            "tool (normal approvals apply) and writes the verdict as a finding itself; you never report "
            "the verdict, and you cannot mark a board solved. The findings you gathered mean something "
            "only because success wasn't scored by the thing that gathered them. Costs a step."
        ),
        "parameters": {"type": "object", "properties": {"name": NAME}, "required": ["name"]},
    },
    "search_board_close": {
        "name": "search_board_close",
        "description": (
            "Close a board without a passing test. 'solved' is not available to you: a passing goal "
            "test does that, and on a judgment or observation board the most you can reach is "
            "proposed_solved. Nor is 'exhausted': the most you can reach is proposed_exhausted, meaning "
            "the frontier is empty and your rationale argues it is genuinely empty rather than that you "
            "stopped thinking. The operator confirms or sends it back. 'abandoned' means the search "
            "isn't worth continuing."
        ),
        "parameters": {"type": "object", "properties": {
            "name": NAME,
            "status": {"type": "string", "enum": ["proposed_solved", "proposed_exhausted", "abandoned"]},
            "rationale": {"type": "string", "description": "Why it closes this way. For proposed_exhausted, the argument that nothing is left to try."},
        }, "required": ["name", "status", "rationale"]},
    },
    # ---- the workflow: write-up → the operator's review → verification → complete
    "search_board_report": {
        "name": "search_board_report",
        "description": (
            "Write up a finished board for the operator's review: after search_board_close proposed_solved or "
            "proposed_exhausted, or a passing search_board_test. The write-up is markdown: what was done, how it "
            "was checked, what is left or uncertain (for proposed_exhausted: what was tried, what each attempt "
            "ruled out, and why nothing is left). `files` are the paths of the files that make up the work "
            "(code, configs, outputs, logs): they are read now and kept as delivered, and the operator reviews "
            "them with the write-up. The board then waits in the operator's review: they send it back with "
            "notes, or on to verification. When a board comes back with notes, answer every one "
            "(search_board_answer) before writing it up again."
        ),
        "parameters": {"type": "object", "properties": {
            "name": NAME,
            "report": {"type": "string", "description": "The write-up, in markdown."},
            "files": {"type": "array", "items": {"type": "string"}, "maxItems": 30,
                      "description": "Paths of the files that make up the work (files, not folders)."},
        }, "required": ["name", "report"]},
    },
    "search_board_answer": {
        "name": "search_board_answer",
        "description": (
            "Answer one of the operator's review notes on a board they sent back (search_board_read shows them "
            "under workflow.review): what they asked, or what you did with what they suggested and why. Every "
            "note needs an answer before the board can be written up again."
        ),
        "parameters": {"type": "object", "properties": {
            "name": NAME, "note": {"type": "string", "description": "e.g. Review note 2 (R2 also works)."},
            "answer": {"type": "string"},
        }, "required": ["name", "note", "answer"]},
    },
    "search_board_review": {
        "name": "search_board_review",
        "description": (
            "Read a board's write-up as delivered: the latest report, the delivered files (or one file's "
            "content as it was delivered, with `file`), and the operator's review notes with your answers. "
            "Use it for your own check in verification: check what was delivered, not what you remember."
        ),
        "parameters": {"type": "object", "properties": {
            "name": NAME, "file": {"type": "string", "description": "A delivered file's path, to read it."},
        }, "required": ["name"]},
    },
    "search_board_verify": {
        "name": "search_board_verify",
        "description": (
            "Your own check of a board the operator sent to verification, made independently: from the goal, the "
            "test and the delivered files (search_board_review), not from memory of having done it. Run the test, "
            "read the files, try the claims. An independent verifier checks it too; its report is held from you "
            "until you file this, and comes back in this call's answer if it is done."
        ),
        "parameters": {"type": "object", "properties": {
            "name": NAME, "verdict": {"type": "string", "enum": ["passed", "failed"]},
            "findings": {"type": "string", "description": "What you checked, how, and what you found, claim by claim."},
        }, "required": ["name", "verdict", "findings"]},
    },
    "search_board_verification": {
        "name": "search_board_verification",
        "description": (
            "The verification report for the operator, reconciling your own check (search_board_verify) with the "
            "independent verifier's: where they agree, where they differ and which is right, and the verdict. It "
            "goes to the operator, who accepts the board as delivered or sends it back to work."
        ),
        "parameters": {"type": "object", "properties": {
            "name": NAME, "verdict": {"type": "string", "enum": ["passed", "failed"]},
            "report": {"type": "string", "description": "The reconciled report, in markdown."},
        }, "required": ["name", "verdict", "report"]},
    },
}

# What a subagent may do: work a search the main agent opened. Opening, closing and the workflow stay with it.
WORKER_TOOLS = ("search_board_list", "search_board_read", "search_board_leads", "search_board_claim",
                "search_board_finding", "search_board_rule_out", "search_board_test", "search_board_review")


WRITES = {"search_board_open", "search_board_leads", "search_board_claim", "search_board_finding",
          "search_board_rule_out", "search_board_test", "search_board_close", "search_board_report",
          "search_board_answer", "search_board_verify", "search_board_verification"}


def write_up_text(board) -> str:
    if board.status == "proposed_exhausted":
        return ("Write it up for the operator with search_board_report: what was tried, what each attempt ruled "
                "out, why nothing is left to try, and the files that show it. It goes to their review: they close "
                "it exhausted, or send it back.")
    return ("Write it up for the operator with search_board_report: what was done, how it was checked, what is "
            "left or uncertain, and the files that make up the work. It goes to their review, then to "
            "verification.")


REF_ARGS = ("lead", "finding", "note")
REF_LIST_ARGS = ("from_findings",)


def with_refs(args: dict) -> dict:
    """The board shows 'Intent 3', 'Finding 2', 'Review note 1'; the agent may pass those or L3/F2/R1."""
    out = dict(args)
    for key in REF_ARGS:
        if isinstance(out.get(key), str) and out[key].strip():
            out[key] = parse_ref(out[key])
    for key in REF_LIST_ARGS:
        if isinstance(out.get(key), list):
            out[key] = [parse_ref(x) if isinstance(x, str) else x for x in out[key]]
    leads = out.get("leads")
    if isinstance(leads, list):
        out["leads"] = [dict(x, from_findings=[parse_ref(f) if isinstance(f, str) else f
                                               for f in x.get("from_findings") or []])
                        if isinstance(x, dict) and isinstance(x.get("from_findings"), list) else x
                        for x in leads]
    return out


class Tools:
    def __init__(self, store_of: Callable[[], Store], runner: Callable[[], Optional[Runner]],
                 changed: Optional[Callable[[str], None]] = None,
                 reader: Optional[Callable[[dict], Callable[[str], str]]] = None,
                 session_key_of: Optional[Callable[[dict], str]] = None):
        self._store_of = store_of
        self._runner = runner
        self._changed = changed or (lambda name: None)
        self._reader = reader
        self._session_key_of = session_key_of or (lambda kwargs: "")

    def handler(self, name: str) -> Callable[..., str]:
        method = getattr(self, name.replace("search_board_", "do_"))

        def handle(args: dict, **kwargs) -> str:
            args = with_refs(args if isinstance(args, dict) else {})
            worker, subagent = worker_of(kwargs)
            if str(kwargs.get("session_id") or "") in VERIFIER_SESSIONS and name not in READ_ONLY:
                return _no("a verifier only reads the board; put what you found in your report")
            if subagent and name not in WORKER_TOOLS:
                return _no("opening and closing boards is the main agent's; say what you found in your report")
            try:
                store = self._store_of()
                result = method(store, args, worker=worker, subagent=subagent, kwargs=kwargs)
                if not subagent and args.get("name"):
                    # The session that works on a board is where a turn starts when the operator moves it.
                    try:
                        store.remember_session(str(args.get("name")), self._session_key_of(kwargs))
                    except Exception:
                        pass
                if name in WRITES:
                    self._changed(str(args.get("name") or ""))
                return result
            except Refused as refused:
                text = str(refused)
                if text.startswith("already_claimed"):
                    return _no(text.split(": ", 1)[-1], reason="already_claimed",
                               next="Pick another lead from the frontier rather than retrying this one.")
                return _no(text)
            except Exception as error:  # never raise into the agent loop
                return _no(f"search-board failed: {type(error).__name__}: {error}")

        return handle

    # -- handlers

    def do_list(self, store: Store, args: dict, **_) -> str:
        at = now_utc()
        listed = []
        for board in store.boards(include_closed=bool(args.get("all"))):
            counts = store.counts(board, at)
            listed.append({
                "name": board.name, "title": board.title, "status": board.status,
                "frontier": counts["frontier"], "findings": counts["findings"],
                "steps": f"{board.steps_used}/{board.step_budget}",
                "opened": render.ago(board.created_at, at),
                **({"new_notes_from_operator": counts["unread_notes"]} if counts["unread_notes"] else {}),
            })
        return _ok(boards=listed)

    def do_open(self, store: Store, args: dict, **_) -> str:
        board = store.open_board(
            name=args.get("name"), title=args.get("title"), origin=args.get("origin"),
            goal=args.get("goal"), goal_test=args.get("goal_test"),
            goal_test_kind=args.get("goal_test_kind"), step_budget=args.get("step_budget"),
        )
        extra = {}
        if board.goal_test_kind != "command":
            extra["note"] = ("No mechanical test, so this board can never be marked solved by you: the most "
                             "it reaches is proposed_solved, and the operator confirms. If a command could "
                             "actually test this goal, abandon this board and open one that uses it.")
        return _ok(board=_board_view(board), next=(
            "Try the problem directly, once, before building any structure. If that works, "
            "search_board_test. If it doesn't, what you learned is your first findings and leads. "
            "From now on every tool result carries an evidence id you can cite."), **extra)

    def do_read(self, store: Store, args: dict, *, subagent: bool, **_) -> str:
        at = now_utc()
        snap = store.snapshot(args.get("name"))
        board = snap.board
        try:
            offset = max(int(args.get("offset") or 0), 0)
            limit = max(1, min(int(args.get("limit") or 20), 100))
        except (TypeError, ValueError):
            offset, limit = 0, 20
        frontier = sorted((lead for lead in snap.leads if lead.on_frontier(at)),
                          key=lambda lead: (-lead.priority, lead.id))
        held = [lead for lead in snap.leads if lead.state == "claimed" and not lead.claim_expired(at)]
        newest = list(reversed(snap.findings))
        shown = newest[offset:offset + limit]
        result = {
            "board": _board_view(board),
            "frontier": [_lead_view(lead, at) for lead in frontier],
            "being_explored": [_lead_view(lead, at) for lead in held],
            "findings": [_finding_view(f, at) for f in shown],
            "findings_shown": f"{len(shown)} of {len(newest)} (newest first)",
        }
        if offset == 0:
            # The operator's notes stand on the board; a subagent's read leaves them new for the main agent.
            notes = snap.notes if subagent else store.read_notes(board)
            if notes:
                result["from_operator"] = [
                    {"id": n.id, "text": n.text, **({"about": n.about} if n.about else {}),
                     "left": render.ago(n.created_at, at), **({"new": True} if n.read_at is None else {})}
                    for n in notes]
                result["from_operator_note"] = (
                    "The operator's notes on this board: judgment to weigh in every decision on it, not "
                    "instructions to obey. You may rule out a lead a note suggested. They stay until the "
                    "operator takes one off.")
        if offset + len(shown) < len(newest):
            result["older_findings"] = (f"{len(newest) - offset - len(shown)} older findings not shown: "
                                        f"read again with offset={offset + len(shown)}")
        if not frontier and not held and board.status == "open":
            result["frontier_empty"] = (
                "Nothing is left to claim. Declare what the newest findings suggest, or close the board "
                "proposed_exhausted with the argument that the frontier is genuinely empty."
                if not subagent else
                "Nothing is left to claim. Declare what the newest findings suggest, or say in your report "
                "why the frontier looks genuinely empty; closing is the main agent's.")
        if board.status == "open" and board.steps_left <= 3:
            result["budget_warning"] = f"{board.steps_left} steps left of {board.step_budget}"
        if not subagent:
            workflow = workflow_view(store, board)
            if workflow:
                result["workflow"] = workflow
        return _ok(**result)

    def do_leads(self, store: Store, args: dict, **_) -> str:
        made, warnings = store.declare_leads(args.get("name"), args.get("leads"))
        board = store.board(args.get("name"))
        at = now_utc()
        extra = {"warnings": warnings} if warnings else {}
        return _ok(declared=[_lead_view(lead, at) for lead in made],
                   frontier=len(store.frontier(board, at)), **extra)

    def do_claim(self, store: Store, args: dict, *, worker: str, subagent: bool, **_) -> str:
        lead = store.claim(args.get("name"), args.get("lead"), worker, one_at_a_time=subagent)
        return _ok(lead=_lead_view(lead, now_utc()), next=(
            "Explore it with whatever tools the problem needs, then write exactly one finding saying what "
            "you established, citing the evidence ids of the results that show it."))

    def do_finding(self, store: Store, args: dict, *, worker: str, subagent: bool, **_) -> str:
        finding, after = store.record_finding(
            args.get("name"), statement=args.get("statement"), polarity=args.get("polarity"),
            basis=args.get("basis"), evidence=args.get("evidence") or [], worker=worker,
            lead_id=args.get("lead"), from_findings=args.get("from_findings"), must_name_lead=subagent,
        )
        at = now_utc()
        extra = {}
        if not finding.from_lead:
            extra["note"] = "Recorded without a lead, as something learned outside any claimed branch."
        if after.status == "suspended":
            extra["suspended"] = "That spent the step budget. The graph is intact; ask the operator to extend it."
        return _ok(finding=_finding_view(finding, at), steps=f"{after.steps_used}/{after.step_budget}",
                   frontier=len(store.frontier(after, at)), **extra)

    def do_rule_out(self, store: Store, args: dict, *, worker: str, subagent: bool, **_) -> str:
        lead = store.rule_out(args.get("name"), args.get("lead"), args.get("finding"), worker,
                              respect_claims=subagent)
        board = store.board(args.get("name"))
        at = now_utc()
        return _ok(lead=_lead_view(lead, at), frontier=len(store.frontier(board, at)))

    def do_test(self, store: Store, args: dict, *, worker: str, subagent: bool, kwargs: dict, **_) -> str:
        board = store.board(str(args.get("name") or "").strip())
        if board is None:
            return _no(f"no board named '{args.get('name')}' (see search_board_list)")
        if board.status != "open":
            return _no(f"'{board.name}' is {board.status}; the goal test runs only on an open board")
        if board.goal_test_kind == "judgment":
            return _no("this board's goal test is 'judgment': there is nothing mechanical to run. Close it "
                       "proposed_solved with your argument, and the operator confirms or doesn't.")
        if board.goal_test_kind == "observation":
            return _no("this board's goal test is an observation to produce and record as a finding, not a "
                       "command the plugin can run. Closing stays proposed_solved until the operator confirms.",
                       goal_test=board.goal_test)
        runner = self._runner()
        if runner is None:
            return _no("the terminal tool isn't available in this session, so the goal test can't run")
        with _TEST_LOCK:
            if board.id in _TESTS_RUNNING:
                return _no(f"the goal test for '{board.name}' is already running; its verdict will be a "
                           "finding on the board (search_board_read shows it)")
            _TESTS_RUNNING.add(board.id)
        try:
            outcome, exit_code, output, error = runner(board.goal_test, TEST_TIMEOUT_SECS, kwargs)
        finally:
            with _TEST_LOCK:
                _TESTS_RUNNING.discard(board.id)
        if outcome == "not_run":
            # Refused by an approval, blocked by a guard, sent to the background, or the backend
            # failed: no verdict either way, so nothing is recorded and no step is spent.
            return _no(f"the goal test did not run: {error or 'no exit status'}. Nothing was recorded.",
                       goal_test=board.goal_test)
        passed = outcome == "ran" and exit_code == 0
        if outcome == "timed_out":
            verdict = "timed out"
        else:
            verdict = "passed (exit 0)" if passed else f"failed (exit {exit_code})"
        finding, after = store.record_test(
            board.name, passed=passed, verdict=verdict, command=board.goal_test,
            output=str(output or "") + (f"\nerror: {error}" if error else ""), worker=worker,
            conversation_id=str(kwargs.get("conversation_id") or ""),
            session_id=str(kwargs.get("session_id") or ""),
        )
        tail = str(output or "")[-2000:]
        extra = {}
        if passed and after.status == "solved":
            if subagent:
                store.add_notice(f"'{after.name}' was closed solved by its goal test, run by a subagent. "
                                 + write_up_text(after), wake=True, board=after)
            else:
                extra["write_up"] = write_up_text(after)
        return _ok(verdict=verdict, passed=passed, finding=_finding_view(finding, now_utc()),
                   board_status=after.status, steps=f"{after.steps_used}/{after.step_budget}",
                   output_tail=tail, **extra,
                   note=("The plugin ran the test and closed the board solved. You did not score this."
                         if passed else
                         "Not solved. The failure is now a finding on the board; let it suggest the next leads."))

    def do_close(self, store: Store, args: dict, **_) -> str:
        board = store.close(args.get("name"), str(args.get("status") or ""), args.get("rationale"))
        extra = {}
        if board.status in ("proposed_solved", "proposed_exhausted"):
            word = "solved" if board.status == "proposed_solved" else "exhausted"
            extra["awaiting"] = f"It counts as {word} only once the operator agrees."
            extra["write_up"] = write_up_text(board)
        return _ok(board=_board_view(board), **extra)

    # -- the workflow

    def do_report(self, store: Store, args: dict, *, kwargs: dict, **_) -> str:
        name = str(args.get("name") or "").strip()
        board, flow = store.flow(name)  # refuse early, before reading any file
        if not store.write_up_due(board, flow):
            if flow.stage != "working":
                return _no(f"'{board.name}' is in the operator's {flow.stage} already")
            if board.status not in ("solved", "proposed_solved", "proposed_exhausted"):
                return _no(f"'{board.name}' is {board.status}: close it proposed_solved or proposed_exhausted (or "
                           "pass its goal test) before writing it up")
        paths = args.get("files") or []
        if paths and self._reader is None:
            return _no("delivered files can't be read in this session (no read_file tool)")
        from .board_flow import snapshot_files  # here: board_flow imports this module

        files = snapshot_files(self._reader(kwargs), paths) if paths else []
        board, flow = store.submit_report(name, str(args.get("report") or ""), files)
        unread = [f["path"] for f in files if f["error"]]
        extra = {"not_kept": unread} if unread else {}
        return _ok(stage="review", round=flow.round, files_kept=len(files) - len(unread), **extra,
                   note=("It is in the operator's review now. They close it exhausted, or send it back with "
                         "questions and ideas." if board.status == "proposed_exhausted" else
                         "It is in the operator's review now. They may send it back to you with questions and "
                         "ideas, or on to verification."))

    def do_answer(self, store: Store, args: dict, **_) -> str:
        left = store.answer(args.get("name"), args.get("note"), str(args.get("answer") or ""))
        return _ok(unanswered=left, note=(
            "All of the operator's notes are answered. When the work is done, close the board (or pass its test) "
            "and write it up again with search_board_report." if not left else "Answer the rest too."))

    def do_review(self, store: Store, args: dict, **_) -> str:
        name = str(args.get("name") or "").strip()
        if args.get("file"):
            meta, content = store.delivered_file(name, str(args.get("file")))
            return _ok(path=meta.path, content=content, truncated=meta.truncated,
                       **({"error": meta.error} if meta.error else {}))
        wf = store.workflow(name)
        latest = wf.reports[-1] if wf.reports else None
        return _ok(stage=wf.flow.stage, round=wf.flow.round,
                   report=(latest or {}).get("text"), report_round=(latest or {}).get("round"),
                   files=[{"path": f.path, "chars": f.chars, **({"cut": True} if f.truncated else {}),
                           **({"not_kept": f.error} if f.error else {})} for f in wf.files],
                   review=[{"id": n.id, "round": n.round, "from_operator": n.text, "your_answer": n.answer}
                           for n in wf.notes])

    def do_verify(self, store: Store, args: dict, **_) -> str:
        flow = store.own_check(args.get("name"), str(args.get("verdict") or ""), str(args.get("findings") or ""))
        if flow.verifier_state in ("done", "failed"):
            return _ok(verifier={"state": flow.verifier_state, "verdict": flow.verifier_verdict,
                                 "report": flow.verifier_report or flow.verifier_note or ""},
                       note="Reconcile the two with search_board_verification.")
        return _ok(verifier={"state": flow.verifier_state},
                   note="The independent verifier is still at it; its report comes to you when it finishes. "
                        "Then reconcile with search_board_verification.")

    def do_verification(self, store: Store, args: dict, **_) -> str:
        store.reconcile(args.get("name"), str(args.get("verdict") or ""), str(args.get("report") or ""))
        return _ok(note="It is with the operator: they accept the board as delivered, or send it back to work.")


def workflow_view(store: Store, board) -> Optional[dict]:
    """Where a board stands in the workflow, as the agent may see it: the verifier's report stays out
    until the agent has filed its own check."""
    wf = store.workflow(board.name)
    flow = wf.flow
    due = store.write_up_due(board, flow)
    if flow.stage == "working" and not due and not wf.notes and flow.report is None:
        return None
    view = {"stage": flow.stage, "round": flow.round}
    if due:
        view["write_up_due"] = write_up_text(board)
    if wf.notes:
        view["review"] = [{"id": n.id, "round": n.round, "from_operator": n.text, "your_answer": n.answer}
                          for n in wf.notes]
        open_notes = [n.id for n in wf.notes if n.answer is None]
        if open_notes and flow.stage == "working":
            view["answer_first"] = f"Answer {', '.join(open_notes)} with search_board_answer before writing it up again."
    if flow.stage == "verification":
        view["your_check"] = flow.own_verdict or "not filed: search_board_verify"
        if flow.own_report is None:
            view["verifier"] = "held until you file your own check"
        else:
            view["verifier"] = {"state": flow.verifier_state, "verdict": flow.verifier_verdict,
                                "report": flow.verifier_report or flow.verifier_note}
        if flow.verification is not None:
            view["reconciled"] = flow.verdict
    return view


def terminal_runner(dispatch: Callable[..., str]) -> Runner:
    """Run a goal test through Hermes's own terminal tool, so the user's approval rules apply."""

    def run(command: str, timeout: int, kwargs: dict) -> tuple:
        forward = {key: kwargs[key] for key in ("task_id", "parent_agent", "session_id") if kwargs.get(key)}
        try:
            raw = dispatch("terminal", {"command": command, "timeout": timeout}, **forward)
        except Exception as error:  # the tool isn't registered, or dispatch failed
            return "not_run", None, "", f"{type(error).__name__}: {error}"
        try:
            body = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError):
            return "not_run", None, str(raw), "the terminal tool returned something that isn't JSON"
        if not isinstance(body, dict):
            return "not_run", None, str(raw), "the terminal tool returned something unexpected"
        output = str(body.get("output") or "")
        error = body.get("error")
        status = body.get("status")
        exit_code = body.get("exit_code")
        try:
            exit_code = None if exit_code is None else int(exit_code)
        except (TypeError, ValueError):
            exit_code = None
        if status:  # blocked by a guard, refused at approval, yielded to the background, …
            return "not_run", exit_code, output, f"{status}: {error}" if error else str(status)
        if error and "timed out" in str(error).lower():
            return "timed_out", exit_code, output, str(error)
        if error or exit_code is None or exit_code < 0:
            return "not_run", exit_code, output, str(error or "no exit status")
        return "ran", exit_code, output, None

    return run
