"""The workflow's moving parts: file snapshots, the independent verifier, and waking the agent.

**Files.** A write-up names the files that make up the work. They are read through Hermes's own
``read_file`` tool, so they come from wherever the agent works (local, Docker, SSH…), and kept as
they were at that moment: the operator reviews what was delivered, not what is there now.

**The verifier.** When the operator sends a board to verification, the plugin asks a model, in a
call of its own, to check the work. It sees only the board, the write-up, the delivered files, the
review, and every finding with the tool output that finding cites — nothing of the agent's
conversation. It is told not to trust the write-up. It runs off the agent's turn (``ctx.llm``),
so it cannot run commands; the goal test's verdicts it reads were recorded by the plugin, not
written by the agent. Its report is held from the agent until the agent has filed its own check.

**Waking.** A move that needs the agent (sent back, sent to verification, the verifier's report
once the agent may see it) starts a turn in the session that last worked on the board, through
``ctx.inject_message``. Hermes allows that only with ``allow_gateway_injection: true`` for this
plugin; otherwise, and whenever no session takes it, the agent hears with its next turn.

A small background thread does both: it starts verifiers the operator asked for and delivers
wake-ups. It reads and writes only this plugin's database.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Callable, Optional

from .board_store import MAX_FILE_CHARS, MAX_FILES, MAX_FILES_CHARS, Refused, Store

log = logging.getLogger(__name__)

POLL_SECONDS = 3.0
LINE_NUMBER = re.compile(r"^\s*\d+\|", re.M)


# ---------------------------------------------------------------- delivered files


def snapshot_files(read: Callable[[str], str], paths: list) -> list:
    """Read each file through ``read`` (``read_file``'s JSON result); keep content, or why not."""
    if not isinstance(paths, list):
        raise Refused("files is a list of paths")
    clean = []
    for path in paths:
        path = str(path or "").strip()
        if not path:
            continue
        if ".." in path.replace("\\", "/").split("/"):
            raise Refused(f"'{path}': no '..' in delivered paths")
        if path not in clean:
            clean.append(path)
    if len(clean) > MAX_FILES:
        raise Refused(f"at most {MAX_FILES} files")
    kept, total = [], 0
    for path in clean:
        entry = {"path": path, "content": "", "chars": 0, "truncated": False, "error": None}
        try:
            raw = read(path)
            body = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception as error:
            entry["error"] = f"could not be read: {type(error).__name__}: {error}"
            kept.append(entry)
            continue
        if not isinstance(body, dict) or body.get("error"):
            entry["error"] = str(body.get("error") if isinstance(body, dict) else "unexpected result")[:500]
            kept.append(entry)
            continue
        content = LINE_NUMBER.sub("", str(body.get("content") or ""))
        truncated = bool(body.get("truncated")) or body.get("next_offset") is not None
        if len(content) > MAX_FILE_CHARS:
            content, truncated = content[:MAX_FILE_CHARS], True
        if total + len(content) > MAX_FILES_CHARS:
            content, truncated = content[: max(MAX_FILES_CHARS - total, 0)], True
            entry["error"] = "cut: the delivery's size limit was reached"
        total += len(content)
        entry.update(content=content, chars=len(content), truncated=truncated)
        kept.append(entry)
    return kept


def file_reader(dispatch: Callable[..., str], kwargs: dict) -> Callable[[str], str]:
    forward = {key: kwargs[key] for key in ("task_id", "parent_agent", "session_id") if kwargs.get(key)}
    return lambda path: dispatch("read_file", {"path": path, "limit": 2000}, **forward)


# ---------------------------------------------------------------- the verifier

VERIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["passed", "failed"]},
        "claims": {"type": "array", "items": {"type": "object", "properties": {
            "claim": {"type": "string"},
            "status": {"type": "string", "enum": ["supported", "unsupported", "contradicted", "unverifiable"]},
            "why": {"type": "string"},
        }, "required": ["claim", "status", "why"]}},
        "missing": {"type": "array", "items": {"type": "string"}},
        "report": {"type": "string"},
    },
    "required": ["verdict", "claims", "report"],
}

VERIFIER_INSTRUCTIONS = """You verify a finished piece of work for its operator, independently.

An agent searched for a solution, finished, and wrote it up. You did not do the work and you do not
trust the write-up. Check each of its claims against what is in front of you:
- the board: its origin, goal and goal test;
- the findings: each cites tool output, shown under it — the output is real, the finding's wording is the agent's;
- the goal test runs: recorded by the plugin, which ran them; the agent did not write their verdicts;
- the files as delivered, and the operator's review notes with the agent's answers.

For every claim in the write-up: supported (the material shows it), unsupported (nothing shows it),
contradicted (the material shows otherwise), or unverifiable (it would need running something).
Look for what is missing, wrong, untested or overstated. A command goal test that has not passed
since the work was last changed is a reason to fail. Be specific: name the finding, the evidence id,
the file and line.

verdict: passed only if the goal is met and the central claims are supported; otherwise failed.
report: markdown, starting with the verdict, then what you checked and found, claim by claim, and
what you could not check."""


def verifier_input(material: dict, budget: int = 120_000) -> str:
    board = material["board"]
    parts = [
        f"# Board {board.name} — {board.title}",
        f"Origin: {board.origin}", f"Goal: {board.goal}", f"Goal test ({board.goal_test_kind}): {board.goal_test}",
        "", "# The write-up", material["report"] or "(none)",
        "", "# Findings, with the tool output each cites",
    ]
    evidence = material["evidence"]
    for f in material["findings"]:
        who = {"plugin": "recorded by the plugin's goal test run", "operator": "by the operator"}.get(f.author, "by the agent")
        parts.append(f"\n## {f.id} ({f.polarity}, {f.basis}, {who}){' exploring ' + f.from_lead if f.from_lead else ''}")
        parts.append(f.statement)
        for e in f.evidence:
            ev = evidence.get(e)
            if ev is not None:
                parts.append(f"--- e{ev.id}: {ev.tool_name} {ev.args_excerpt[:300]}\n{ev.excerpt[:3000]}")
    leads = material.get("leads") or []
    if leads:
        parts.append("\n# Leads")
        parts.extend(f"- {lead.id} [{lead.state}] {lead.direction}" for lead in leads)
    notes = material.get("notes") or []
    if notes:
        parts.append("\n# The operator's review notes and the agent's answers")
        parts.extend(f"- {n.id} (round {n.round}): {n.text}\n  answer: {n.answer or '(none)'}" for n in notes)
    parts.append("\n# Files as delivered")
    for f in material["files"]:
        head = f"\n## {f['path']}" + (" (cut)" if f["truncated"] else "") + (f" — {f['error']}" if f["error"] else "")
        parts.append(head)
        if f["content"]:
            parts.append(f["content"])
    text = "\n".join(parts)
    if len(text) > budget:
        text = text[:budget] + f"\n\n[… {len(text) - budget} more characters of material not shown]"
    return text


def run_verifier(store: Store, complete: Callable[..., object], board_id: str, token: str) -> Optional[str]:
    """One verification, start to finish. Returns the wake text when the agent may now see it."""
    if not store.verifier_start(board_id, token):
        return None
    try:
        material = store.verifier_material(board_id)
        result = complete(instructions=VERIFIER_INSTRUCTIONS, input=[{"type": "text", "text": verifier_input(material)}],
                          json_schema=VERIFIER_SCHEMA, schema_name="board_verification",
                          purpose="search-board.verify", temperature=0.0)
        parsed = getattr(result, "parsed", None)
        if not isinstance(parsed, dict) or parsed.get("verdict") not in ("passed", "failed"):
            raw = str(getattr(result, "text", "") or "")[:2000]
            return store.verifier_finish(board_id, token, ok=False, verdict=None, report=raw,
                                         note="the verifier's answer could not be read")
        return store.verifier_finish(board_id, token, ok=True, verdict=parsed["verdict"],
                                     report=format_verifier_report(parsed))
    except Exception as error:
        log.warning("search-board: verifier for %s failed: %s", board_id, error)
        return store.verifier_finish(board_id, token, ok=False, verdict=None, report="",
                                     note=f"{type(error).__name__}: {error}"[:500])


def format_verifier_report(parsed: dict) -> str:
    lines = [f"VERDICT: {parsed['verdict']}", "", str(parsed.get("report") or "").strip()]
    claims = parsed.get("claims") or []
    if claims:
        lines += ["", "## Claim by claim"]
        lines += [f"- [{c.get('status', '?')}] {c.get('claim', '')} — {c.get('why', '')}" for c in claims]
    missing = parsed.get("missing") or []
    if missing:
        lines += ["", "## Missing or not checked"] + [f"- {m}" for m in missing]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- waking and the poller


class Poller:
    """Starts wanted verifiers and delivers wake-ups, every few seconds, until stopped."""

    def __init__(self, store_of: Callable[[], Store], complete: Optional[Callable[..., object]],
                 inject: Optional[Callable[[str, Optional[str]], bool]], changed: Callable[[str], None]):
        self._store_of = store_of
        self._complete = complete
        self._inject = inject
        self._changed = changed
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="search-board-workflow", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.tick()
            except Exception as error:  # keep polling: one bad pass must not end the workflow
                log.debug("search-board poller: %s", error)

    def tick(self) -> None:
        """One pass. Verifiers run in threads of their own so a slow model call holds up nothing."""
        with self._lock:
            store = self._store_of()
            store.revive_stale_verifiers()
            if self._complete is not None:
                for board_id, token in store.verifiers_wanted():
                    threading.Thread(target=self._verify, args=(store, board_id, token),
                                     name=f"search-board-verifier-{board_id}", daemon=True).start()
            self.deliver(store)

    def _verify(self, store: Store, board_id: str, token: str) -> None:
        run_verifier(store, self._complete, board_id, token)
        self._changed(board_id)
        self.deliver(store)

    def deliver(self, store: Store) -> None:
        if self._inject is None:
            return
        for notice_id, session_key, text in store.wake_due():
            try:
                ok = bool(self._inject(text, session_key))
            except Exception as error:
                log.debug("search-board: wake-up not delivered: %s", error)
                ok = False
            store.mark_woken(notice_id, ok)  # not taken: it goes with the agent's next turn


def injector(ctx) -> Callable[[str, Optional[str]], bool]:
    def inject(text: str, session_key: Optional[str]) -> bool:
        if not session_key:
            return False
        content = f"[search-board, rendered by the plugin]\n{text}"
        return bool(ctx.inject_message(content, role="user", session_key=session_key))

    return inject
