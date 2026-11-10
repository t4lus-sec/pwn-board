"""Backend routes for the Desktop pane, mounted at ``/api/plugins/search-board/``.

They read and write the same database as the agent's tools and ``/sboard``. Only
the operator's actions are offered here: notes, extend, confirm, exhaust, reject,
abandon, and the workflow's moves (review notes, send back, to verification, to
review, accept, discard, run a failed verifier again). Nothing here writes a lead,
a finding, a write-up or a check, or runs a test. A move that needs the agent is
queued in the database; the plugin's thread in the agent's process delivers it.

Handlers are plain ``def``s: FastAPI runs them in a worker thread, so a SQLite
call never blocks the gateway's event loop.
"""

import importlib.util
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

PLUGIN = "search-board"
DB_FILE = "boards.db"

router = APIRouter()


def _load_store_module():
    # board_store.py has no imports of its own package, so it loads on its own from beside us.
    path = Path(__file__).resolve().parents[1] / "board_store.py"
    spec = importlib.util.spec_from_file_location("search_board_dashboard_store", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_m = _load_store_module()
_store = None


def _db():
    global _store
    if _store is None:
        from plugins.plugin_storage import plugin_db

        _store = _m.Store(lambda: plugin_db(PLUGIN, DB_FILE))
    return _store


def _changed(board: str) -> None:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(PLUGIN, "board.changed", {"board": board})
    except Exception:
        pass


def _refused(error: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


def _board_json(board, counts=None) -> dict:
    out = {
        "name": board.name, "title": board.title, "origin": board.origin, "goal": board.goal,
        "goal_test": board.goal_test, "goal_test_kind": board.goal_test_kind, "status": board.status,
        "step_budget": board.step_budget, "steps_used": board.steps_used,
        "created_at": board.created_at, "closed_at": board.closed_at, "outcome": board.outcome,
    }
    if counts is not None:
        out.update(counts)
    return out


def _build_version() -> str:
    import re

    try:
        text = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8")
        found = re.search(r"^version:\s*['\"]?([^'\"\s]+)", text, re.M)
        return found.group(1) if found else "unknown"
    except OSError:
        return "unknown"


# Read at import: what this backend process actually loaded, not what is on disk now.
BUILD = _build_version()


@router.get("/build")
def build():
    """Which search-board build this backend runs, and which one the agent side last said it runs.
    The pane compares both with its own, to tell when Hermes still has an older build loaded."""
    try:
        agent = _db().build("agent")
    except Exception:
        agent = None
    return {"api": BUILD, "agent": agent}


@router.get("/boards")
def list_boards(all: bool = False) -> dict:
    store = _db()
    return {"boards": [_board_json(b, store.counts(b)) for b in store.boards(include_closed=all)]}


@router.get("/boards/{name}")
def get_board(name: str) -> dict:
    store = _db()
    try:
        snap = store.snapshot(name)
    except _m.Refused as error:
        raise HTTPException(status_code=404, detail=str(error))
    at = _m.now_utc()
    return {
        "board": _board_json(snap.board, store.counts(snap.board, at)),
        "leads": [{
            "id": lead.id, "direction": lead.direction, "rationale": lead.rationale,
            "from_findings": lead.from_findings, "priority": lead.priority,
            "state": "open" if lead.claim_expired(at) else lead.state,
            "claimed_by": "subagent" if (lead.claimed_by or "").startswith("sub:") else ("main agent" if lead.claimed_by else None),
            "claimed_at": lead.claimed_at, "closed_by": lead.closed_by, "created_at": lead.created_at,
        } for lead in snap.leads],
        "findings": [{
            "id": f.id, "statement": f.statement, "polarity": f.polarity, "basis": f.basis,
            "author": f.author, "from_lead": f.from_lead, "evidence": f.evidence,
            "from_findings": f.from_findings, "created_at": f.created_at,
        } for f in snap.findings],
        "notes": [{
            "id": n.id, "text": n.text, "about": n.about, "created_at": n.created_at, "read": n.read_at is not None,
        } for n in snap.notes],
    }


@router.get("/evidence")
def get_evidence(ids: str = "") -> dict:
    try:
        wanted = [int(x) for x in ids.split(",") if x.strip()][:20]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids are numbers, comma-separated")
    return {"evidence": [{
        "id": e.id, "tool_name": e.tool_name, "args": e.args_excerpt, "excerpt": e.excerpt,
        "chars": e.chars, "sha256": e.sha256, "created_at": e.created_at,
    } for e in _db().evidence(wanted)]}


@router.post("/boards/{name}/note")
def leave_note(name: str, body: dict = Body(...)) -> dict:
    try:
        note, board = _db().add_note(name, str(body.get("text") or ""))
    except _m.Refused as error:
        raise _refused(error)
    _changed(board.name)
    return {"ok": True, "note": {"id": note.id, "about": note.about}}


@router.post("/boards/{name}/unnote")
def remove_note(name: str, body: dict = Body(...)) -> dict:
    try:
        board = _db().remove_note(name, str(body.get("id") or ""))
    except _m.Refused as error:
        raise _refused(error)
    _changed(board.name)
    return {"ok": True}


@router.post("/boards/{name}/extend")
def extend(name: str, body: dict = Body(...)) -> dict:
    try:
        board = _db().operator_extend(name, body.get("steps"))
    except _m.Refused as error:
        raise _refused(error)
    _changed(board.name)
    return {"ok": True, "board": _board_json(board)}


@router.post("/boards/{name}/decide")
def decide(name: str, body: dict = Body(...)) -> dict:
    decision = str(body.get("decision") or "")
    if decision not in ("confirm", "exhaust", "reject", "abandon"):
        raise HTTPException(status_code=400, detail="decision is confirm, exhaust, reject or abandon")
    try:
        board = _db().operator_decide(name, decision, str(body.get("why") or ""))
    except _m.Refused as error:
        raise _refused(error)
    _changed(board.name)
    return {"ok": True, "board": _board_json(board)}


# ---------------------------------------------------------------- the workflow


def _flow_json(flow) -> dict:
    return {key: getattr(flow, key) for key in (
        "stage", "round", "report_round", "report_at", "entered_at", "verifier_state", "verifier_verdict",
        "verifier_report", "verifier_note", "verifier_started_at", "own_verdict", "own_report", "verdict",
        "verification", "verified_at", "accepted_at")}


@router.get("/boards/{name}/workflow")
def get_workflow(name: str) -> dict:
    store = _db()
    try:
        wf = store.workflow(name)
    except _m.Refused as error:
        raise HTTPException(status_code=404, detail=str(error))
    return {
        "flow": _flow_json(wf.flow),
        "state": store.workflow_state(wf.board, wf.flow),
        "write_up_due": store.write_up_due(wf.board, wf.flow),
        "reports": wf.reports,
        "notes": [{"id": n.id, "round": n.round, "text": n.text, "created_at": n.created_at,
                   "answer": n.answer, "answered_at": n.answered_at} for n in wf.notes],
        "files": [{"path": f.path, "chars": f.chars, "truncated": f.truncated, "error": f.error} for f in wf.files],
    }


@router.get("/boards/{name}/file")
def get_file(name: str, path: str) -> dict:
    try:
        meta, content = _db().delivered_file(name, path)
    except _m.Refused as error:
        raise HTTPException(status_code=404, detail=str(error))
    return {"path": meta.path, "content": content, "truncated": meta.truncated, "error": meta.error}


def _move(name: str, run) -> dict:
    try:
        result = run(_db())
    except _m.Refused as error:
        raise _refused(error)
    _changed(name)
    return {"ok": True, **(result or {})}


@router.post("/boards/{name}/comment")
def comment(name: str, body: dict = Body(...)) -> dict:
    return _move(name, lambda s: {"note": s.comment(name, str(body.get("text") or "")).id})


@router.post("/boards/{name}/uncomment")
def uncomment(name: str, body: dict = Body(...)) -> dict:
    return _move(name, lambda s: s.uncomment(name, str(body.get("id") or "")) and None)


@router.post("/boards/{name}/back")
def send_back(name: str, body: dict = Body(default={})) -> dict:
    return _move(name, lambda s: {"round": s.send_back(name, str(body.get("why") or ""))[1].round})


@router.post("/boards/{name}/verify")
def to_verification(name: str) -> dict:
    return _move(name, lambda s: s.to_verification(name) and None)


@router.post("/boards/{name}/reverify")
def reverify(name: str) -> dict:
    return _move(name, lambda s: s.retry_verifier(name) and None)


@router.post("/boards/{name}/toreview")
def to_review(name: str, body: dict = Body(default={})) -> dict:
    return _move(name, lambda s: s.to_review(name, str(body.get("why") or "")) and None)


@router.post("/boards/{name}/accept")
def accept(name: str) -> dict:
    return _move(name, lambda s: s.accept(name) and None)


@router.post("/boards/{name}/discard")
def discard(name: str, body: dict = Body(default={})) -> dict:
    return _move(name, lambda s: s.discard(name, str(body.get("why") or "")) and None)
