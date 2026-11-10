"""The workflow's moving parts: file snapshots, the independent verifier, and waking the agent.

**Files.** A write-up names the files that make up the work. They are read through Hermes's own
``read_file`` tool, so they come from wherever the agent works (local, Docker, SSH…), and kept as
they were at that moment: the operator reviews what was delivered, not what is there now.

**The verifier.** When the operator sends a board to verification, the plugin launches a fresh
subagent through Hermes's public lifecycle API (``ctx.subagent_lifecycle``), the same host path
``delegate_task`` uses: a child session with terminal and file tools that runs the goal test and
checks the work on the machine. It is told not to trust the write-up, and is given only the board,
the write-up, the files as delivered, the review and the findings with the tool output they cite —
nothing of the agent's conversation. Its report comes back to the plugin through ``subagent_stop``,
not into the agent's context, and is held from the agent until the agent has filed its own check.
Hermes lets a plugin launch a child only during an agent turn, so it starts at the main agent's
next turn (the wake-up below starts one).

**Waking.** A move that needs the agent (sent back, sent to verification, the verifier's report
once the agent may see it) starts a turn in the session that last worked on the board, through
``ctx.inject_message``. Hermes allows that only with ``allow_gateway_injection: true`` for this
plugin; otherwise, and whenever no session takes it, the agent hears with its next turn.

A small background thread delivers the wake-ups and notices verifiers lost to a restart. It reads
and writes only this plugin's database.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Callable, Optional

from .board_store import MAX_FILE_CHARS, MAX_FILES, MAX_FILES_CHARS, Refused, Store
from .board_tools import VERIFIER_SESSIONS

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

MARKER = "[pwn-board verifier {token}]"
MARKER_RE = re.compile(r"\[pwn-board verifier ([0-9a-f]{16})\]")


def verifier_task(board, token: str) -> str:
    """The verifier's goal: the board, where the work is, what to check, and the shape of its report.
    It knows nothing else of the work, and nothing of the agent's conversation."""
    return f"""{MARKER.format(token=token)}
Verify board '{board.name}' for the operator, independently.

An agent finished this piece of work and wrote it up. You check it, on your own: do not trust the
write-up, check each of its claims yourself, on the machine.

Board: {board.name} — {board.title}
Goal: {board.goal}
Goal test ({board.goal_test_kind}): {board.goal_test}

The context holds the write-up, the files it delivered (as they were delivered, with the paths they
were taken from), the operator's review notes with the agent's answers, and the findings with the tool
output each cites. Read the write-up. Run the goal test if it is a command. Read the delivered files
on the machine and check they are what was delivered and do what the write-up says: build them, run
their tests, try them. Look for what is missing, wrong, untested or overstated. Change nothing outside
/tmp. You have no part in the board itself: do not try to change it.

End your reply with your verification report, in this form:
VERDICT: passed (or: VERDICT: failed)
then what you checked, how, and what you found, claim by claim, and what you could not check."""


def verdict_of(report: str) -> str:
    """`VERDICT: passed` (or failed) in a verifier's report."""
    lower = (report or "").lower()
    at = lower.rfind("verdict:")
    if at < 0:
        return "unclear"
    rest = lower[at + len("verdict:"):].lstrip().lstrip("*").lstrip()
    if rest.startswith("pass"):
        return "passed"
    if rest.startswith("fail"):
        return "failed"
    return "unclear"


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


TOOLSET_CHOICES = (("terminal", "file"), ("file",), None)


class Verifiers:
    """Launches verifier subagents through Hermes's public lifecycle API, and takes their reports.

    Hermes lets a plugin launch a child only while an agent turn is active, so launching happens in
    the main agent's turns (``in_turn``, from its hooks and the board's tools). The report comes back
    through the ``subagent_stop`` hook, matched by the child's session; it goes to the board, never
    into the agent's context, and is held from the agent until it has filed its own check.
    """

    def __init__(self, store_of: Callable[[], Store], service_of: Callable[[], object], proc: str):
        self._store_of = store_of
        self._service_of = service_of
        self.proc = proc
        self._lock = threading.Lock()

    def in_turn(self) -> None:
        """Launch wanted verifiers, cancel dropped ones. Never raises."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            store = self._store_of()
            service = self._service_of()
            if service is None:
                return
            for job in store.take_cancels():
                try:
                    service.cancel(json.loads(job), reason="pwn-board: the board moved on")
                except Exception as error:
                    log.debug("pwn-board: verifier not cancelled: %s", error)
            for board_id, token in store.verifiers_wanted():
                self._launch(store, service, board_id, token)
        except Exception as error:
            log.debug("pwn-board: verifiers: %s", error)
        finally:
            self._lock.release()

    def _launch(self, store: Store, service, board_id: str, token: str) -> None:
        material = store.verifier_material(board_id)
        goal = verifier_task(material["board"], token)
        context = verifier_input(material, budget=30_000)
        last_error = "no toolset choice worked"
        for toolsets in TOOLSET_CHOICES:
            request = {"goal": goal, "context": context, "role": "leaf",
                       "correlation_id": f"pwn-board-{board_id}-{token}",
                       "metadata": {"plugin": "pwn-board", "board_id": board_id}}
            if toolsets:
                request["allowed_toolsets"] = toolsets
            try:
                handle = service.launch(request)
            except Exception as error:
                last_error = f"{type(error).__name__}: {error}"
                text = str(error).lower()
                if "no active hermes parent" in text:
                    return  # not in a turn: try again in the next one, without counting it
                if "toolset" in text or "broaden" in text:
                    continue  # narrower tools, then the parent's own
                break
            job = json.dumps(handle.to_dict() if hasattr(handle, "to_dict") else dict(handle))
            if not store.verifier_launched(board_id, token, job, self.proc):
                try:
                    service.cancel(handle, reason="pwn-board: the board moved on")
                except Exception:
                    pass
            return
        store.verifier_not_launched(board_id, token, last_error)

    def on_child_start(self, child_session_id=None, child_goal=None, **_kwargs) -> None:
        match = MARKER_RE.search(str(child_goal or ""))
        if match and child_session_id:
            VERIFIER_SESSIONS.add(str(child_session_id))
            self._store_of().verifier_child(match.group(1), str(child_session_id))

    def on_child_stop(self, child_session_id=None, child_summary=None, child_status=None, **_kwargs) -> bool:
        """A verifier finished: its report goes on the board (and to the agent, if it filed its own
        check already). True when the child was one of ours."""
        if not child_session_id:
            return False
        store = self._store_of()
        found = store.verifier_of_child(str(child_session_id))
        if found is None:
            return False
        board_id, token = found
        summary = str(child_summary or "").strip()
        ok = str(child_status or "") == "completed" and bool(summary)
        verdict = verdict_of(summary) if ok else None
        note = None if ok else f"the verifier ended {child_status or 'without a status'}"
        if ok and verdict == "unclear":
            note = "the verifier's report has no VERDICT line"
        store.verifier_finish(board_id, token, ok=ok, verdict=verdict, report=summary, note=note)
        return True


# ---------------------------------------------------------------- waking and the poller


class Poller:
    """Delivers wake-ups and revives verifiers whose process went away, every few seconds."""

    def __init__(self, store_of: Callable[[], Store], inject: Optional[Callable[[str, Optional[str]], bool]],
                 proc: str):
        self._store_of = store_of
        self._inject = inject
        self._proc = proc
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="pwn-board-workflow", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.tick()
            except Exception as error:  # keep polling: one bad pass must not end the workflow
                log.debug("pwn-board poller: %s", error)

    def tick(self) -> None:
        with self._lock:
            store = self._store_of()
            store.revive_orphans(self._proc)
            self.deliver(store)

    def deliver(self, store: Store) -> None:
        if self._inject is None:
            return
        for notice_id, session_key, text in store.wake_due():
            try:
                ok = bool(self._inject(text, session_key))
            except Exception as error:
                log.debug("pwn-board: wake-up not delivered: %s", error)
                ok = False
            store.mark_woken(notice_id, ok)  # not taken: it goes with the agent's next turn


def injector(ctx) -> Callable[[str, Optional[str]], bool]:
    def inject(text: str, session_key: Optional[str]) -> bool:
        if not session_key:
            return False
        content = f"[pwn-board, rendered by the plugin]\n{text}"
        return bool(ctx.inject_message(content, role="user", session_key=session_key))

    return inject
