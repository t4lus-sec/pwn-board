"""Text views of a board: the tree, one node in full, and the standing block.

A board is already a tree, written as it was searched: a lead is explored
into findings (each naming the lead it came from), and a finding opens new
leads (each naming the findings it came from). So the view draws
goal → leads → what each found → the leads that opened, one row each.

- A node with more than one parent sits under the first and says where else it
  came from ("also from Finding 2").
- Leads being explored come first, then open ones by priority, then explored;
  ruled-out leads go last and, unless asked for, are folded into one row.
- The operator's notes stand under the lead or finding they are about, after its
  own children; a note about nothing in particular stands under the board.

Markers follow the usual convention: [+] done or for, [*] under way, [-] ruled out or failed,
[?] open, [!] wants the operator.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .board_store import Board, Finding, Lead, Note, Snapshot, ago, label, labels, now_utc, parse_ref

STATUS_MARK = {
    "open": "[*]", "suspended": "[!]", "proposed_solved": "[!]", "proposed_exhausted": "[!]",
    "solved": "[+]", "exhausted": "[-]", "abandoned": "[-]",
}


def lead_mark(lead: Lead, at: datetime) -> str:
    if lead.state == "claimed" and not lead.claim_expired(at):
        return "[*]"
    if lead.state == "explored":
        return "[+]"
    if lead.state == "ruled_out":
        return "[-]"
    return "[?]"


def finding_mark(finding: Finding) -> str:
    return "[+]" if finding.polarity == "supports" else "[-]"


def _lead_rank(lead: Lead, at: datetime) -> tuple:
    if lead.state == "claimed" and not lead.claim_expired(at):
        rank = 0
    elif lead.on_frontier(at):
        rank = 1
    elif lead.state == "explored":
        rank = 2
    else:
        rank = 3
    return (rank, -lead.priority, int(lead.id[1:]) if lead.id[1:].isdigit() else 0)


def _clip(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: max(width - 1, 1)] + "…"


def holder(worker: Optional[str]) -> str:
    if not worker:
        return "someone"
    if worker.startswith("sub:"):
        return "a subagent"
    return "the main agent"


ORIGIN_MARK = "[o]"


class _Tree:
    """Nodes keyed 'board', 'L3', 'F2', 'N1', with children in display order."""

    def __init__(self, snap: Snapshot, at: datetime):
        self.snap = snap
        self.at = at
        self.leads = {lead.id: lead for lead in snap.leads}
        self.findings = {f.id: f for f in snap.findings}
        self.notes = {n.id: n for n in snap.notes}
        self.children: dict[str, list[str]] = {"board": []}
        self.also: dict[str, list[str]] = {}
        placed: set[str] = set()

        for key in list(self.leads) + list(self.findings) + list(self.notes):
            self.children.setdefault(key, [])

        # Leads under the first finding they came from, or under the board.
        for lead in sorted(snap.leads, key=lambda lead: _lead_rank(lead, at)):
            parents = [f for f in lead.from_findings if f in self.findings]
            parent = parents[0] if parents else "board"
            self.children[parent].append(lead.id)
            if len(parents) > 1:
                self.also[lead.id] = parents[1:]
            placed.add(lead.id)

        # Findings under the lead they explored; one outside any lead (a goal test) under the board,
        # after the leads.
        for finding in snap.findings:
            parent = finding.from_lead if finding.from_lead in self.leads else "board"
            self.children[parent].append(finding.id)
            placed.add(finding.id)

        # Notes last under what they are about.
        for note in snap.notes:
            parent = note.about if note.about in self.children and note.about != note.id else "board"
            self.children[parent].append(note.id)

    def line(self, key: str, width: int) -> str:
        at = self.at
        if key in self.leads:
            lead = self.leads[key]
            parts = [f"{lead_mark(lead, at)} {label(lead.id)}", f"p{lead.priority}"]
            if lead.state == "claimed" and not lead.claim_expired(at):
                parts.append(f"exploring ({holder(lead.claimed_by)}, {ago(lead.claimed_at, at)})")
            elif lead.state == "explored":
                parts.append("explored")
            elif lead.state == "ruled_out":
                parts.append(f"ruled out by {label(lead.closed_by)}")
            text = " · ".join(parts) + f" — {lead.direction}"
        elif key in self.findings:
            f = self.findings[key]
            kind = {"plugin": "goal test", "operator": "by you"}.get(f.author)
            what = ("passed" if f.polarity == "supports" else "failed") if f.author == "plugin" else (
                "supports" if f.polarity == "supports" else "rules out")
            parts = [f"{finding_mark(f)} {label(f.id)}"] + ([kind] if kind else []) + [what]
            cites = ", ".join(f"e{x}" for x in f.evidence)
            text = " · ".join(parts) + f" — {f.statement} ({cites})"
        else:
            n = self.notes[key]
            text = f"{'[!]' if n.read_at is None else '[ ]'} {label(n.id)}{' · new' if n.read_at is None else ''} — {n.text}"
        also = self.also.get(key)
        if also:
            text += f" (also from {labels(also)})"
        return _clip(text, width)


def tree(snap: Snapshot, *, width: int = 110, fold_ruled_out: bool = True,
         at: Optional[datetime] = None) -> str:
    at = at or now_utc()
    t = _Tree(snap, at)
    b = snap.board
    frontier = sum(1 for lead in snap.leads if lead.on_frontier(at))
    lines = [
        _clip(f"{STATUS_MARK.get(b.status, '[?]')} {b.name} — {b.title}", width),
        "│  " + _clip(f"{b.status} · steps {b.steps_used}/{b.step_budget} · frontier {frontier} · "
                     f"{len(snap.findings)} findings · opened {ago(b.created_at, at)}", width - 3),
        "│  " + _clip(f"goal: {b.goal}", width - 3),
        "│  " + _clip(f"test ({b.goal_test_kind}): {b.goal_test}", width - 3),
    ]
    if b.outcome:
        lines.append("│  " + _clip(f"outcome: {b.outcome}", width - 3))
    # The origin is the tree's root: where the search started, and what every seed intent grew from.
    lines.append("└─ " + _clip(f"{ORIGIN_MARK} Origin — {b.origin}", width - 3))

    def walk(key: str, prefix: str) -> None:
        kids = list(t.children.get(key, []))
        folded: list[str] = []
        if fold_ruled_out:
            folded = [k for k in kids if k in t.leads and t.leads[k].state == "ruled_out"
                      and not t.children.get(k)]
            kids = [k for k in kids if k not in folded]
        rows: list[tuple[str, Optional[str]]] = [(k, None) for k in kids]
        if folded:
            rows.append(("", f"[-] {len(folded)} ruled out: {labels(folded)}"))
        for index, (child, summary) in enumerate(rows):
            last = index == len(rows) - 1
            branch = "└─ " if last else "├─ "
            room = max(width - len(prefix) - len(branch), 20)
            lines.append(prefix + branch + (summary and _clip(summary, room) or t.line(child, room)))
            if child:
                walk(child, prefix + ("   " if last else "│  "))

    walk("board", "   ")
    if not snap.leads and not snap.findings and not snap.notes:
        lines.append("   └─ (nothing yet: no intents declared, nothing found)")
    return "\n".join(lines)


def detail(snap: Snapshot, key: str, evidence: Optional[list] = None,
           at: Optional[datetime] = None) -> str:
    """One node in full: what the tree can only show cut."""
    at = at or now_utc()
    key = parse_ref(key)
    b = snap.board
    if key in ("", "BOARD", "ORIGIN", b.name.upper()):
        return "\n".join([
            f"{ORIGIN_MARK} Origin of {b.name} — {b.title}", f"status: {b.status} · steps {b.steps_used}/{b.step_budget}",
            f"origin: {b.origin}", f"goal: {b.goal}", f"test ({b.goal_test_kind}): {b.goal_test}",
        ] + ([f"outcome: {b.outcome}"] if b.outcome else []))
    for lead in snap.leads:
        if lead.id == key:
            out = [f"{lead_mark(lead, at)} {label(lead.id)} — {lead.state.replace('_', ' ')}, priority {lead.priority}",
                   f"direction: {lead.direction}", f"rationale: {lead.rationale}"]
            if lead.from_findings:
                out.append(f"came from: {labels(lead.from_findings)}")
            if lead.state == "claimed":
                out.append(f"claimed by {holder(lead.claimed_by)}, {ago(lead.claimed_at, at)}")
            if lead.closed_by:
                out.append(f"{'explored in' if lead.state == 'explored' else 'ruled out by'} {label(lead.closed_by)}")
            return "\n".join(out)
    for f in snap.findings:
        if f.id == key:
            out = [f"{finding_mark(f)} {label(f.id)} — {f.polarity.replace('_', ' ')}, {f.basis}, by {'the goal test' if f.author == 'plugin' else f.author}, {ago(f.created_at, at)}",
                   f"statement: {f.statement}"]
            if f.from_lead:
                out.append(f"explored: {label(f.from_lead)}")
            if f.from_findings:
                out.append(f"inferred from: {labels(f.from_findings)}")
            for ev in evidence or []:
                out.append(f"--- e{ev.id}: {ev.tool_name} {ev.args_excerpt[:200]} "
                           f"({ev.chars} chars, sha256 {ev.sha256[:12]}, {ago(ev.created_at, at)})")
                out.append(ev.excerpt[:1500] + ("…" if len(ev.excerpt) > 1500 else ""))
            return "\n".join(out)
    for n in snap.notes:
        if n.id == key:
            return "\n".join([f"{label(n.id)} — your note, {ago(n.created_at, at)}"
                              + (f", about {label(n.about)}" if n.about else ""),
                              n.text, "read by the agent" if n.read_at else "not read yet"])
    return f"no {label(key)} on '{b.name}'"


def board_line(board: Board, counts: dict) -> str:
    stage = counts.get("workflow")
    line = (f"· {board.name} — {board.title} [{board.status}{', ' + stage if stage else ''}, "
            f"frontier {counts['frontier']}, steps {board.steps_used}/{board.step_budget}]")
    if board.status == "suspended":
        line += " — budget spent; the operator can extend it"
    if counts.get("unread_notes"):
        line += f" — {counts['unread_notes']} new note(s) from the operator"
    return line


# ---------------------------------------------------------------- the workflow's views


def review(wf) -> str:
    """The write-up as delivered, the files, and the review notes with the agent's answers."""
    b, flow = wf.board, wf.flow
    lines = [f"{b.name} — {b.title}", f"stage: {flow.stage} · round {flow.round} · board {b.status}"]
    latest = wf.reports[-1] if wf.reports else None
    if latest is None:
        lines.append("\nNo write-up yet.")
    else:
        older = f" (rounds before: {', '.join(str(r['round']) for r in wf.reports[:-1])})" if len(wf.reports) > 1 else ""
        lines += [f"\n— write-up, round {latest['round']}, {ago(latest['at'])}{older} —", latest["text"]]
    if wf.files:
        lines.append("\n— files as delivered —")
        for f in wf.files:
            extra = " (cut)" if f.truncated else ""
            extra += f" — not kept: {f.error}" if f.error else ""
            lines.append(f"  {f.path}  {f.chars} chars{extra}")
    lines.append("\n— review notes —" if wf.notes else "\nNo review notes.")
    for n in wf.notes:
        answer = f"\n      agent: {n.answer}" if n.answer else "\n      (not answered yet)"
        lines.append(f"  [{'+' if n.answer else '?'}] {label(n.id)} (round {n.round}): {n.text}{answer}")
    return "\n".join(lines)


def verification(wf) -> str:
    """The two checks and the reconciled report, as far as they have got."""
    b, flow = wf.board, wf.flow
    if flow.stage != "verification" and flow.verification is None:
        return f"'{b.name}' is {flow.stage}: no verification under way."
    mark = {"passed": "[+]", "failed": "[-]"}
    lines = [f"{b.name} — verification (round {flow.round})"]
    lines.append(f"\n— the independent verifier: {flow.verifier_state or 'stopped'}"
                 + (f", {mark.get(flow.verifier_verdict, '')} {flow.verifier_verdict}" if flow.verifier_verdict else "") + " —")
    lines.append(flow.verifier_report or flow.verifier_note or "(no report yet)")
    lines.append(f"\n— the agent's own check: {mark.get(flow.own_verdict, '[*]')} {flow.own_verdict or 'not filed yet'} —")
    if flow.own_report:
        lines.append(flow.own_report)
    lines.append(f"\n— reconciled: {mark.get(flow.verdict, '[*]')} {flow.verdict or 'not yet'} —")
    if flow.verification:
        lines.append(flow.verification)
    return "\n".join(lines)
