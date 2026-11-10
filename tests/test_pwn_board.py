"""Tests for pwn-board, run without Hermes: a fake ctx and a temporary database."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "pwn_board", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["pwn_board"] = module
    spec.loader.exec_module(module)
    return module


sb = _load()
from pwn_board import board_commands as operator, board_hooks as hooks, board_render as render, board_tools as tools  # noqa: E402
from pwn_board.board_store import Refused, Store, now_utc, stamp  # noqa: E402


# ---------------------------------------------------------------- fixtures


class Runner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, command, timeout, kwargs):
        self.calls.append((command, timeout, dict(kwargs)))
        return self.results.pop(0)


@pytest.fixture
def store(tmp_path):
    return Store.at(tmp_path / "boards.db")


@pytest.fixture
def runner():
    return Runner()


@pytest.fixture
def t(store, runner):
    return tools.Tools(lambda: store, lambda: runner)


MAIN = {"session_id": "s-main", "conversation_id": "c-1", "task_id": "t-1"}


def call(t, name, args, **kwargs):
    return json.loads(t.handler(name)(args, **{**MAIN, **kwargs}))


def open_board(t, name="flaky-flush", kind="command", budget=None):
    args = {"name": name, "title": "Find why flush fails one run in fifty",
            "origin": "test_flush fails intermittently on CI", "goal": "test_flush passes 50 runs in a row",
            "goal_test": "pytest tests/test_flush.py --count 50", "goal_test_kind": kind}
    if budget:
        args["step_budget"] = budget
    result = call(t, "pwn_board_open", args)
    assert result["ok"], result
    return result


def evidence(store, text="exit 1: ENOSPC while flushing", tool="terminal"):
    hook = hooks.make_tool_result_hook(lambda: store)
    tagged = hook(tool_name=tool, args={"command": "pytest"}, result=json.dumps({"output": text, "exit_code": 1}),
                  conversation_id="c-1", session_id="s-main", tool_call_id="call-1")
    assert tagged is not None
    return json.loads(tagged)["_evidence"]


# ---------------------------------------------------------------- the search loop


def test_open_declare_claim_find(t, store):
    open_board(t)
    declared = call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [
        {"direction": "Reproduce the failure locally with 50 runs", "rationale": "Need a reliable repro first", "priority": 7},
        {"direction": "Check disk space on the CI runner", "rationale": "ENOSPC appears in the CI log tail"},
    ]})
    assert declared["ok"] and [lead["id"] for lead in declared["declared"]] == ["L1", "L2"]
    assert declared["frontier"] == 2

    claimed = call(t, "pwn_board_claim", {"name": "flaky-flush", "lead": "L1"})
    assert claimed["ok"] and claimed["lead"]["state"] == "claimed"

    e = evidence(store)
    found = call(t, "pwn_board_finding", {"name": "flaky-flush", "lead": "L1", "statement": "Fails 3 of 50 runs with ENOSPC",
                                             "polarity": "supports", "basis": "observed", "evidence": [e]})
    assert found["ok"], found
    assert found["finding"]["id"] == "F1" and found["steps"] == "1/60"

    read = call(t, "pwn_board_read", {"name": "flaky-flush"})
    assert [lead["id"] for lead in read["frontier"]] == ["L2"]
    assert read["findings"][0]["evidence"] == [e]


def test_tools_take_the_labels_the_board_shows(t, store):
    """The board reads 'Intent 1', 'Finding 1'; an agent copying those must not be refused."""
    open_board(t)
    call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [
        {"direction": "Reproduce with fifty runs", "rationale": "Need a repro first"},
        {"direction": "Check CI runner disk space", "rationale": "ENOSPC in the CI log"}]})
    assert call(t, "pwn_board_claim", {"name": "flaky-flush", "lead": "Intent 1"})["ok"]
    found = call(t, "pwn_board_finding", {"name": "flaky-flush", "lead": "intent 1", "statement": "Fails 3 of 50 with ENOSPC",
                                             "polarity": "supports", "basis": "observed", "evidence": [evidence(store)]})
    assert found["ok"], found
    assert found["finding"]["label"] == "Finding 1"
    more = call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [
        {"direction": "Measure /tmp inside the container", "rationale": "ENOSPC on flush", "from_findings": ["Finding 1"]}]})
    assert more["ok"], more
    assert more["declared"][0]["label"] == "Intent 3" and more["declared"][0]["from_findings"] == ["Finding 1"]


def test_a_finding_must_cite_recorded_evidence(t, store):
    open_board(t)
    call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [{"direction": "Reproduce with fifty runs", "rationale": "Need a repro first"}]})
    call(t, "pwn_board_claim", {"name": "flaky-flush", "lead": "L1"})
    base = {"name": "flaky-flush", "lead": "L1", "statement": "It fails with ENOSPC", "polarity": "supports", "basis": "observed"}

    none = call(t, "pwn_board_finding", {**base, "evidence": []})
    assert not none["ok"] and "needs evidence" in none["error"]

    made_up = call(t, "pwn_board_finding", {**base, "evidence": ["e999"]})
    assert not made_up["ok"] and "e999" in made_up["error"]

    e = evidence(store)
    assert call(t, "pwn_board_finding", {**base, "evidence": [e]})["ok"]
    again = call(t, "pwn_board_finding", {**base, "evidence": [e]})
    assert not again["ok"] and "already been explored" in again["error"]


def test_a_finding_against_an_unclaimed_or_foreign_lead_is_refused(t, store):
    open_board(t)
    call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [{"direction": "Reproduce with fifty runs", "rationale": "Need a repro first"}]})
    e = evidence(store)
    base = {"name": "flaky-flush", "lead": "L1", "statement": "It fails with ENOSPC", "polarity": "supports", "basis": "observed", "evidence": [e]}
    assert "claim L1" in call(t, "pwn_board_finding", base)["error"]
    call(t, "pwn_board_claim", {"name": "flaky-flush", "lead": "L1"}, conversation_id="c-other")
    assert "held by" in call(t, "pwn_board_finding", base)["error"]


def test_inferred_needs_sources(t, store):
    open_board(t)
    e = evidence(store)
    result = call(t, "pwn_board_finding", {"name": "flaky-flush", "statement": "So the disk fills up",
                                              "polarity": "supports", "basis": "inferred", "evidence": [e]})
    assert not result["ok"] and "from_findings" in result["error"]


def test_rule_out_needs_a_rules_out_finding(t, store):
    open_board(t)
    call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [
        {"direction": "Check disk space on the runner", "rationale": "ENOSPC in the log"},
        {"direction": "Check the fsync ordering in writer", "rationale": "Writes might race"}]})
    e = evidence(store)
    call(t, "pwn_board_finding", {"name": "flaky-flush", "statement": "The runner has 40 GB free",
                                     "polarity": "rules_out", "basis": "observed", "evidence": [e]})
    call(t, "pwn_board_finding", {"name": "flaky-flush", "statement": "Writes go through one thread",
                                     "polarity": "supports", "basis": "observed", "evidence": [e]})
    assert "'supports'" in call(t, "pwn_board_rule_out", {"name": "flaky-flush", "lead": "L1", "finding": "F2"})["error"]
    ruled = call(t, "pwn_board_rule_out", {"name": "flaky-flush", "lead": "L1", "finding": "F1"})
    assert ruled["ok"] and ruled["lead"]["state"] == "ruled_out" and ruled["frontier"] == 1


def test_frontier_cap_and_similarity_warning(t):
    open_board(t)
    first = call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [
        {"direction": "Check disk space on the runner", "rationale": "ENOSPC in the log"}]})
    assert "warnings" not in first
    similar = call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [
        {"direction": "Check the disk space on that runner", "rationale": "ENOSPC appeared"}]})
    assert similar["ok"] and "reads a lot like L1" in similar["warnings"][0]


def test_claims_go_stale(store, t):
    open_board(t)
    call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [{"direction": "Reproduce with fifty runs", "rationale": "Need a repro first"}]})
    call(t, "pwn_board_claim", {"name": "flaky-flush", "lead": "L1"}, conversation_id="c-other")
    taken = call(t, "pwn_board_claim", {"name": "flaky-flush", "lead": "L1"})
    assert not taken["ok"] and taken["reason"] == "already_claimed"
    later = now_utc() + timedelta(minutes=31)
    lead = store.claim("flaky-flush", "L1", "main:c-1", at=later)
    assert lead.claimed_by == "main:c-1" and lead.prior_claims == ["main:c-other"]


# ---------------------------------------------------------------- the goal test


def test_a_passing_test_solves_the_board(t, runner):
    open_board(t)
    runner.results.append(("ran", 0, "50 passed", None))
    result = call(t, "pwn_board_test", {"name": "flaky-flush"})
    assert result["passed"] and result["board_status"] == "solved"
    assert result["finding"]["by"] == "the goal test"
    command, timeout, kwargs = runner.calls[0]
    assert command.startswith("pytest") and timeout == 600 and kwargs["task_id"] == "t-1"
    assert not call(t, "pwn_board_test", {"name": "flaky-flush"})["ok"]


def test_a_failing_test_is_a_finding(t, runner):
    open_board(t)
    runner.results.append(("ran", 1, "3 failed, 47 passed", None))
    result = call(t, "pwn_board_test", {"name": "flaky-flush"})
    assert result["ok"] and not result["passed"] and result["board_status"] == "open"
    assert result["finding"]["polarity"] == "rules_out" and result["steps"] == "1/60"


def test_a_test_that_did_not_run_records_nothing(t, runner, store):
    open_board(t)
    runner.results.append(("not_run", 1, "", "denied: the user refused"))
    result = call(t, "pwn_board_test", {"name": "flaky-flush"})
    assert not result["ok"] and "did not run" in result["error"]
    snap = store.snapshot("flaky-flush")
    assert snap.findings == [] and snap.board.steps_used == 0


def test_judgment_boards_cannot_be_tested_or_solved(t):
    open_board(t, kind="judgment")
    assert "judgment" in call(t, "pwn_board_test", {"name": "flaky-flush"})["error"]
    assert "cannot close a board solved" in call(t, "pwn_board_close", {"name": "flaky-flush", "status": "solved", "rationale": "it looks right to me"})["error"]


def test_terminal_runner_reads_every_shape():
    def dispatch(body):
        return lambda name, args, **kw: json.dumps(body)

    assert tools.terminal_runner(dispatch({"output": "ok", "exit_code": 0, "error": None}))("x", 1, {}) == ("ran", 0, "ok", None)
    assert tools.terminal_runner(dispatch({"output": "", "exit_code": 2, "error": None}))("x", 1, {})[:2] == ("ran", 2)
    assert tools.terminal_runner(dispatch({"output": "", "exit_code": 124, "error": "Command timed out after 600 seconds"}))("x", 1, {})[0] == "timed_out"
    assert tools.terminal_runner(dispatch({"output": "", "exit_code": 1, "error": "blocked", "status": "blocked"}))("x", 1, {})[0] == "not_run"
    assert tools.terminal_runner(dispatch({"output": "", "exit_code": None, "error": None, "status": "yielded_to_background"}))("x", 1, {})[0] == "not_run"
    assert tools.terminal_runner(dispatch({"output": "", "exit_code": -1, "error": "Failed to execute"}))("x", 1, {})[0] == "not_run"

    def broken(name, args, **kw):
        raise KeyError("terminal")
    assert tools.terminal_runner(broken)("x", 1, {})[0] == "not_run"


# ---------------------------------------------------------------- closing and the operator


def test_exhausted_is_the_operators_word(t, store):
    open_board(t)
    call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [{"direction": "Reproduce with fifty runs", "rationale": "Need a repro first"}]})
    assert "cannot close a board exhausted" in call(t, "pwn_board_close", {"name": "flaky-flush", "status": "exhausted", "rationale": "nothing left"})["error"]
    refused = call(t, "pwn_board_close", {"name": "flaky-flush", "status": "proposed_exhausted", "rationale": "nothing left to try"})
    assert not refused["ok"] and "still on the frontier" in refused["error"]

    e = evidence(store)
    call(t, "pwn_board_finding", {"name": "flaky-flush", "statement": "It never fails locally in 500 runs",
                                     "polarity": "rules_out", "basis": "observed", "evidence": [e]})
    call(t, "pwn_board_rule_out", {"name": "flaky-flush", "lead": "L1", "finding": "F1"})
    proposed = call(t, "pwn_board_close", {"name": "flaky-flush", "status": "proposed_exhausted", "rationale": "every lead was ruled out by evidence"})
    assert proposed["ok"] and "awaiting" in proposed

    # The operator closes it, and the agent hears with its next turn, once.
    assert "exhausted" in operator.run(store, "exhaust flaky-flush")
    context = hooks.make_context_hook(lambda: store)
    block = context(session_id="s-main")["context"]
    assert "closed 'flaky-flush' exhausted" in block
    assert context(session_id="s-main") is None


def test_reject_reopens_with_the_reason_as_a_note(t, store):
    open_board(t, kind="judgment")
    call(t, "pwn_board_close", {"name": "flaky-flush", "status": "proposed_solved", "rationale": "the fix looks right"})
    assert "open" in operator.run(store, "reject flaky-flush it still fails on arm64")
    read = call(t, "pwn_board_read", {"name": "flaky-flush"})
    note = read["from_operator"][0]
    assert note["new"] and "arm64" in note["text"] and read["board"]["status"] == "open"
    again = call(t, "pwn_board_read", {"name": "flaky-flush"})
    assert "new" not in again["from_operator"][0]


def test_a_subagent_read_leaves_notes_new(t, store):
    open_board(t)
    operator.run(store, "note flaky-flush try the arm64 runner first")
    tools.note_subagent("s-child")
    sub = call(t, "pwn_board_read", {"name": "flaky-flush"}, session_id="s-child")
    assert sub["from_operator"][0]["new"]
    main = call(t, "pwn_board_read", {"name": "flaky-flush"})
    assert main["from_operator"][0]["new"]
    assert "new" not in call(t, "pwn_board_read", {"name": "flaky-flush"})["from_operator"][0]


def test_notes_about_a_lead_and_taking_them_off(t, store):
    open_board(t)
    call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [{"direction": "Reproduce with fifty runs", "rationale": "Need a repro first"}]})
    assert "about Intent 1" in operator.run(store, "note flaky-flush L1: use the arm64 runner")
    note = store.snapshot("flaky-flush").notes[0]
    assert note.about == "L1" and note.text == "use the arm64 runner"
    assert "about Intent 1" in operator.run(store, 'note flaky-flush "Intent 1: try it on arm64"')
    assert "Note 1 taken off" in operator.run(store, "unnote flaky-flush Note 1")
    assert "Note 2 taken off" in operator.run(store, "unnote flaky-flush N2")
    assert store.snapshot("flaky-flush").notes == []


def test_refs_read_as_words_and_short_ids():
    from pwn_board.board_store import label, parse_ref
    for text, ref in [("Intent 3", "L3"), ("intent3", "L3"), ("I3", "L3"), ("L3", "L3"), ("lead 3", "L3"),
                      ("Finding 12", "F12"), ("f2", "F2"), ("Note 1", "N1"), ("Review note 4", "R4"), ("r4", "R4"),
                      ("board", "BOARD"), ("Intent 3 extra", "INTENT 3 EXTRA")]:
        assert parse_ref(text) == ref, text
    assert label("L3") == "Intent 3" and label("F12") == "Finding 12" and label("R4") == "Review note 4"
    assert label("board") == "board"


def test_budget_suspends_and_extend_reopens(t, store):
    open_board(t, budget=2)
    e = evidence(store)
    for statement in ("First thing that was seen", "Second thing that was seen"):
        call(t, "pwn_board_finding", {"name": "flaky-flush", "statement": statement, "polarity": "supports",
                                         "basis": "observed", "evidence": [e]})
    assert store.board("flaky-flush").status == "suspended"
    refused = call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [{"direction": "Something else to try", "rationale": "Because reasons"}]})
    assert "suspended" in refused["error"]
    assert "2/4 [open]" in operator.run(store, "extend flaky-flush 2") and store.board("flaky-flush").status == "open"


# ---------------------------------------------------------------- subagents


def test_subagents_work_a_board_but_dont_own_it(t, store):
    open_board(t)
    call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [
        {"direction": "Reproduce with fifty runs", "rationale": "Need a repro first"},
        {"direction": "Check disk space on runner", "rationale": "ENOSPC in the log"}]})
    tools.note_subagent("s-worker")
    sub = {"session_id": "s-worker"}
    assert "main agent's" in call(t, "pwn_board_open", {"name": "other"}, **sub)["error"]
    assert "main agent's" in call(t, "pwn_board_close", {"name": "flaky-flush", "status": "abandoned", "rationale": "meh meh meh"}, **sub)["error"]
    assert call(t, "pwn_board_claim", {"name": "flaky-flush", "lead": "L1"}, **sub)["ok"]
    assert "already hold L1" in call(t, "pwn_board_claim", {"name": "flaky-flush", "lead": "L2"}, **sub)["error"]
    e = evidence(store)
    unnamed = call(t, "pwn_board_finding", {"name": "flaky-flush", "statement": "Fails 3 of 50", "polarity": "supports",
                                               "basis": "observed", "evidence": [e]}, **sub)
    assert "name the lead" in unnamed["error"]
    assert call(t, "pwn_board_finding", {"name": "flaky-flush", "lead": "L1", "statement": "Fails 3 of 50", "polarity": "rules_out",
                                            "basis": "observed", "evidence": [e]}, **sub)["ok"]
    # The main agent holds L2; the subagent can't rule it out from under it.
    call(t, "pwn_board_claim", {"name": "flaky-flush", "lead": "L2"})
    assert "being explored" in call(t, "pwn_board_rule_out", {"name": "flaky-flush", "lead": "L2", "finding": "F1"}, **sub)["error"]


# ---------------------------------------------------------------- the evidence hook


def test_evidence_is_recorded_only_while_a_board_is_open(t, store):
    hook = hooks.make_tool_result_hook(lambda: store)
    assert hook(tool_name="terminal", args={}, result='{"output": "x"}') is None
    open_board(t)
    tagged = hook(tool_name="read_file", args={"path": "a.py"}, result="plain text result")
    assert tagged.endswith("[evidence e1]")
    assert json.loads(hook(tool_name="terminal", args={}, result='{"output": "é", "exit_code": 0}'))["_evidence"] == "e2"
    assert hook(tool_name="pwn_board_read", args={}, result='{"ok": true}') is None
    assert hook(tool_name="terminal", args={}, result={"not": "a string"}) is None
    [ev] = store.evidence([1])
    assert ev.tool_name == "read_file" and ev.excerpt == "plain text result" and len(ev.sha256) == 64


def test_long_results_keep_head_and_tail(t, store):
    open_board(t)
    hook = hooks.make_tool_result_hook(lambda: store)
    hook(tool_name="terminal", args={}, result="A" * 3000 + "B" * 3000)
    [ev] = store.evidence([1])
    assert ev.chars == 6000 and ev.excerpt.startswith("A" * 2000) and ev.excerpt.endswith("B" * 2000)


def test_uncited_evidence_is_pruned(t, store):
    open_board(t)
    cited = evidence(store)
    loose = evidence(store, text="something else")
    call(t, "pwn_board_finding", {"name": "flaky-flush", "statement": "Saw the ENOSPC failure", "polarity": "supports",
                                     "basis": "observed", "evidence": [cited]})
    with store._db(write=True) as db:
        db.execute("UPDATE evidence SET created_at=?", (stamp(now_utc() - timedelta(days=30)),))
    assert store.prune_evidence() == 1
    assert [e.id for e in store.evidence([1, 2])] == [int(cited[1:])]
    assert loose == "e2"


def test_context_block_is_quiet_without_boards_and_skips_subagents(t, store):
    context = hooks.make_context_hook(lambda: store)
    assert context(session_id="s-main") is None
    open_board(t)
    block = context(session_id="s-main")["context"]
    assert "flaky-flush" in block and "frontier 0" in block
    assert context(session_id="s-x", parent_session_id="s-main") is None


# ---------------------------------------------------------------- the view


def test_the_tree_reads_as_leads_and_what_they_found(t, store):
    open_board(t)
    call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [
        {"direction": "Reproduce with fifty runs", "rationale": "Need a repro first", "priority": 8},
        {"direction": "Check disk space on runner", "rationale": "ENOSPC in the log"}]})
    call(t, "pwn_board_claim", {"name": "flaky-flush", "lead": "L1"})
    e = evidence(store)
    call(t, "pwn_board_finding", {"name": "flaky-flush", "lead": "L1", "statement": "Fails 3 of 50 with ENOSPC",
                                     "polarity": "supports", "basis": "observed", "evidence": [e]})
    call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [
        {"direction": "Look at tmpfs size in the container", "rationale": "F1 shows ENOSPC", "from_findings": ["F1"]}]})
    operator.run(store, "note flaky-flush F1: the container has a 64 MB /tmp")
    view = operator.run(store, "show flaky-flush")
    lines = view.splitlines()
    assert lines[0].startswith("[*] flaky-flush")
    l1 = next(i for i, line in enumerate(lines) if "[+] Intent 1 · p8 · explored — Reproduce" in line)
    f1 = next(i for i, line in enumerate(lines) if "[+] Finding 1 · supports — Fails 3 of 50" in line)
    l3 = next(i for i, line in enumerate(lines) if "[?] Intent 3 · p5 — Look at tmpfs" in line)
    n1 = next(i for i, line in enumerate(lines) if "[!] Note 1 · new — the container has a 64 MB /tmp" in line)
    assert l1 < f1 < l3 < n1
    assert lines[f1].index("Finding 1") > lines[l1].index("Intent 1")
    assert lines[l3].index("Intent 3") > lines[f1].index("Finding 1")
    for ref in ("F1", "Finding 1", "finding 1"):
        detail = operator.run(store, f"detail flaky-flush {ref}")
        assert detail.startswith("[+] Finding 1") and "explored: Intent 1" in detail and "--- e1: terminal" in detail


def test_the_origin_is_the_root_of_the_board(t, store):
    """The origin is an entry of its own: the tree's root, which the seed intents grow from."""
    open_board(t)
    call(t, "pwn_board_leads", {"name": "flaky-flush", "leads": [
        {"direction": "Reproduce with fifty runs", "rationale": "Need a repro first"}]})
    lines = operator.run(store, "show flaky-flush").splitlines()
    origin = next(i for i, line in enumerate(lines) if "[o] Origin — test_flush fails intermittently on CI" in line)
    l1 = next(i for i, line in enumerate(lines) if "Intent 1" in line)
    assert origin < l1
    assert lines[l1].index("Intent 1") > lines[origin].index("Origin"), "seed intents sit under the origin"
    for ref in ("origin", "Origin"):
        assert "origin: test_flush fails intermittently on CI" in operator.run(store, f"detail flaky-flush {ref}")
    assert call(t, "pwn_board_read", {"name": "flaky-flush"})["board"]["origin"] == "test_flush fails intermittently on CI"


def test_messaging_platforms_get_a_code_block():
    assert operator.format_for("telegram", "x") == "```\nx\n```"
    assert operator.format_for("cli", "x") == "x"


# ---------------------------------------------------------------- registration


class FakeCtx:
    def __init__(self):
        self.tools, self.hooks, self.commands, self.skills = {}, {}, {}, {}

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        assert toolset == "pwn_board" and schema["name"] == name
        self.tools[name] = handler

    def register_hook(self, name, callback, **kwargs):
        self.hooks[name] = callback

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_skill(self, name, path):
        assert Path(path).exists()
        self.skills[name] = path

    def dispatch_tool(self, name, args, **kwargs):
        return json.dumps({"output": "ok", "exit_code": 0, "error": None})

    def on_unload(self, callback):
        self.unload = callback

    def inject_message(self, content, role="user", *, session_key=None, origin=None):
        return False


def test_register_matches_the_manifest(monkeypatch, store):
    import yaml

    monkeypatch.setattr(sb, "store", lambda: store)
    ctx = FakeCtx()
    sb.register(ctx)
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert sorted(ctx.tools) == sorted(manifest["provides_tools"])
    assert sorted(ctx.hooks) == sorted(manifest["provides_hooks"])
    assert list(ctx.commands) == ["pwnboard"] and list(ctx.skills) == ["pwn-board"]
    assert "No boards" in ctx.commands["pwnboard"]("")
    assert store.build("agent") == str(manifest["version"]), "first use says which build the agent side runs"


def test_loading_creates_no_files_but_stamps_an_existing_database(monkeypatch, tmp_path):
    import types

    import yaml

    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda plugin: tmp_path / plugin
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    monkeypatch.setattr(sb, "_store", None)
    sb.register(FakeCtx())
    assert not (tmp_path / "pwn-board").exists(), "validate/doctor load the plugin: no database for that"

    (tmp_path / "pwn-board").mkdir()
    Store.at(tmp_path / "pwn-board" / "boards.db").record_build("agent", "0.1.0")
    monkeypatch.setattr(sb, "_store", None)
    sb.register(FakeCtx())
    version = str(yaml.safe_load((ROOT / "plugin.yaml").read_text())["version"])
    assert Store.at(tmp_path / "pwn-board" / "boards.db").build("agent") == version
