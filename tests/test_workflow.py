"""The workflow: write-up → the operator's review → verification → complete, and every way back."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if "search_board" not in sys.modules:
    spec = importlib.util.spec_from_file_location("search_board", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["search_board"] = module
    spec.loader.exec_module(module)

from search_board import board_commands as cmd, board_flow as flowrt, board_hooks as hooks, board_tools as tools  # noqa: E402
from search_board.board_store import Store  # noqa: E402

MAIN = {"session_id": "s-main", "conversation_id": "c-1", "task_id": "t-1"}
REPORT = "Fixed the flush: the container's /tmp was a 64 MB tmpfs. Raised it to 1 GB in docker-compose.yml; fifty runs pass."


class Files:
    """A fake read_file: a dict of path -> content, anything else is an error."""

    def __init__(self, files):
        self.files = files
        self.read = []

    def __call__(self, kwargs):
        def read(path):
            self.read.append(path)
            if path not in self.files:
                return json.dumps({"error": f"File not found: {path}"})
            text = "\n".join(f"{i + 1:>6}|{line}" for i, line in enumerate(self.files[path].split("\n")))
            return json.dumps({"content": text, "total_lines": self.files[path].count("\n") + 1})
        return read


class Lifecycle:
    """A fake ctx.subagent_lifecycle: records launches; the test plays the child's start and stop."""

    def __init__(self):
        self.requests, self.cancelled, self.fail_with = [], [], None

    def launch(self, request):
        if self.fail_with:
            raise self.fail_with
        self.requests.append(request)
        return SimpleNamespace(to_dict=lambda n=len(self.requests): {"subagent_id": f"sa-{n}"})

    def cancel(self, handle, *, reason):
        self.cancelled.append(handle)


@pytest.fixture
def env(tmp_path):
    store = Store.at(tmp_path / "boards.db")
    runs = []
    files = Files({"/work/docker-compose.yml": "services:\n  test:\n    tmpfs: /tmp:size=1g", "/work/NOTES.md": "notes"})
    t = tools.Tools(lambda: store, lambda: (lambda c, to, kw: runs.pop(0)), reader=files,
                    session_key_of=lambda kwargs: "ses_main")
    injected = []
    service = Lifecycle()
    poller = flowrt.Poller(lambda: store, lambda text, key: injected.append((key, text)) or True, "proc-1")
    verifiers = flowrt.Verifiers(lambda: store, lambda: service, "proc-1")
    tools.SUBAGENT_SESSIONS.discard("s-sub")
    return SimpleNamespace(store=store, t=t, runs=runs, files=files, injected=injected, service=service,
                           poller=poller, verifiers=verifiers)


def call(env, name, args, **kwargs):
    return json.loads(env.t.handler(name)(args, **{**MAIN, **kwargs}))


def solved_board(env, name="flaky-flush"):
    call(env, "search_board_open", {"name": name, "title": "Find why test_flush fails one run in fifty",
                                    "origin": "CI fails about 2%", "goal": "fifty green runs in a row",
                                    "goal_test": "pytest --count 50", "goal_test_kind": "command"})
    hook = hooks.make_tool_result_hook(lambda: env.store)
    tagged = json.loads(hook(tool_name="terminal", args={"command": "df /tmp"}, result=json.dumps({"output": "tmpfs 64M 64M 0 100% /tmp"})))
    call(env, "search_board_finding", {"name": name, "statement": "The container's /tmp is a full 64 MB tmpfs",
                                       "polarity": "supports", "basis": "observed", "evidence": [tagged["_evidence"]]})
    env.runs.append(("ran", 0, "50 passed", None))
    result = call(env, "search_board_test", {"name": name})
    assert result["board_status"] == "solved" and "search_board_report" in result["write_up"]
    return result


def report(env, name="flaky-flush", files=("/work/docker-compose.yml",)):
    return call(env, "search_board_report", {"name": name, "report": REPORT, "files": list(files)})


def run_poller(env):
    env.poller.tick()


def run_verifier(env, report="VERDICT: passed\n\nRan the goal test: fifty runs pass. The compose file sets 1 GB.",
                 status="completed"):
    """A main-agent turn launches the wanted verifier; the child starts, reports and stops."""
    before = len(env.service.requests)
    env.verifiers.in_turn()
    assert len(env.service.requests) == before + 1, "no verifier was launched"
    request = env.service.requests[-1]
    child = f"child-{len(env.service.requests)}"
    env.verifiers.on_child_start(child_session_id=child, child_goal=request["goal"])
    assert child in tools.VERIFIER_SESSIONS
    assert env.verifiers.on_child_stop(child_session_id=child, child_summary=report, child_status=status)
    env.poller.deliver(env.store)
    return request


# ---------------------------------------------------------------- write-up and review


def test_a_finished_board_owes_a_write_up(env):
    solved_board(env)
    read = call(env, "search_board_read", {"name": "flaky-flush"})
    assert "write_up_due" in read["workflow"] and read["workflow"]["stage"] == "working"
    assert "write-up due" in hooks.make_context_hook(lambda: env.store)(session_id="s-main")["context"]


def test_an_open_board_cannot_be_written_up(env):
    call(env, "search_board_open", {"name": "open-one", "title": "Something still being searched",
                                    "origin": "an origin here", "goal": "a goal to reach",
                                    "goal_test": "true", "goal_test_kind": "command"})
    result = report(env, "open-one")
    assert not result["ok"] and "close it" in result["error"]
    assert env.files.read == []  # refused before any file was read


def test_the_write_up_keeps_the_files_as_delivered(env):
    solved_board(env)
    result = report(env, files=["/work/docker-compose.yml", "/work/missing.txt"])
    assert result["ok"] and result["stage"] == "review" and result["files_kept"] == 1
    assert result["not_kept"] == ["/work/missing.txt"]
    meta, content = env.store.delivered_file("flaky-flush", "/work/docker-compose.yml")
    assert content == "services:\n  test:\n    tmpfs: /tmp:size=1g"  # line numbers stripped
    again = report(env)
    assert not again["ok"] and "review already" in again["error"]
    review = cmd.run(env.store, "review flaky-flush")
    assert "write-up, round 1" in review and "/work/docker-compose.yml" in review and "not kept" in review


def test_dot_dot_paths_are_refused(env):
    solved_board(env)
    result = report(env, files=["/work/../etc/passwd"])
    assert not result["ok"] and "'..'" in result["error"]


def test_sent_back_with_notes_every_note_needs_an_answer(env):
    solved_board(env)
    report(env)
    assert "Review note 1 added" in cmd.run(env.store, "comment flaky-flush Did you check it on arm64?")
    cmd.run(env.store, "comment flaky-flush Why 1 GB and not 256 MB?")
    assert "round 2" in cmd.run(env.store, "back flaky-flush")
    board = env.store.board("flaky-flush")
    assert board.status == "open" and board.outcome is None

    # The agent is woken in the board's own session, with the notes listed.
    run_poller(env)
    key, text = env.injected[-1]
    assert key == "ses_main" and "R1: Did you check it on arm64?" in text and "R2" in text

    read = call(env, "search_board_read", {"name": "flaky-flush"})
    assert [n["id"] for n in read["workflow"]["review"]] == ["R1", "R2"] and "answer_first" in read["workflow"]
    env.runs.append(("ran", 0, "50 passed", None))
    call(env, "search_board_test", {"name": "flaky-flush"})
    refused = report(env)
    assert not refused["ok"] and "R1, R2" in refused["error"]
    assert call(env, "search_board_answer", {"name": "flaky-flush", "note": "R1", "answer": "Yes, the arm64 runner passes too."})["unanswered"] == ["R2"]
    call(env, "search_board_answer", {"name": "flaky-flush", "note": "r2", "answer": "256 MB fails at 40 runs."})
    result = report(env)
    assert result["ok"] and result["round"] == 2
    review = cmd.run(env.store, "review flaky-flush")
    assert "round 2" in review and "rounds before: 1" in review and "agent: Yes, the arm64 runner passes too." in review


def test_review_notes_only_in_review_and_unanswered_ones_can_be_taken_back(env):
    solved_board(env)
    assert "not in review" in cmd.run(env.store, "comment flaky-flush too early")
    report(env)
    cmd.run(env.store, "comment flaky-flush first thought")
    assert "Review note 1 taken back" in cmd.run(env.store, "uncomment flaky-flush Review note 1")
    assert env.store.workflow("flaky-flush").notes == []


def test_answers_only_while_working(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "comment flaky-flush a question for you")
    result = call(env, "search_board_answer", {"name": "flaky-flush", "note": "R1", "answer": "an answer"})
    assert not result["ok"] and "nothing to answer now" in result["error"]


# ---------------------------------------------------------------- verification


def test_the_verifiers_report_is_held_until_the_agent_files_its_own(env):
    solved_board(env)
    report(env)
    assert "in verification" in cmd.run(env.store, "verify flaky-flush")
    run_poller(env)  # the agent is woken to do its own check
    run_verifier(env)  # and its next turn launches the verifier, which reports
    assert any("Check it yourself now" in text for _, text in env.injected)
    assert not any("verifier's report" in text for _, text in env.injected)

    read = call(env, "search_board_read", {"name": "flaky-flush"})
    assert read["workflow"]["verifier"] == "held until you file your own check"
    early = call(env, "search_board_verification", {"name": "flaky-flush", "verdict": "passed", "report": "x" * 50})
    assert not early["ok"] and "own check first" in early["error"]

    own = call(env, "search_board_verify", {"name": "flaky-flush", "verdict": "passed",
                                            "findings": "Re-ran the goal test: fifty runs pass. Read the compose file: tmpfs is 1 GB."})
    assert own["verifier"]["state"] == "done" and own["verifier"]["verdict"] == "passed"
    assert own["verifier"]["report"].startswith("VERDICT: passed") and "compose file sets 1 GB" in own["verifier"]["report"]
    assert not call(env, "search_board_verify", {"name": "flaky-flush", "verdict": "passed", "findings": "y" * 50})["ok"]

    done = call(env, "search_board_verification", {"name": "flaky-flush", "verdict": "passed",
                                                   "report": "Both checks agree: the goal test passes and the fix is the tmpfs size."})
    assert done["ok"]
    assert "verified (passed): accept or send back" in cmd.run(env.store, "")
    assert "complete" in cmd.run(env.store, "accept flaky-flush")
    board = env.store.board("flaky-flush")
    assert board.status == "solved" and board.outcome.endswith("[verified (passed); accepted by the operator]")
    assert env.store.workflow("flaky-flush").flow.stage == "deliverable"
    assert any("accepted 'flaky-flush' as delivered" in text for text in env.store.take_notices())


def test_the_verifier_is_a_subagent_that_sees_the_work_not_the_agents_word(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    call(env, "search_board_verify", {"name": "flaky-flush", "verdict": "passed", "findings": "MY SECRET OWN CHECK " * 5})
    request = run_verifier(env)
    assert request["role"] == "leaf" and request["allowed_toolsets"] == ("terminal", "file")
    assert "Run the goal test if it is a command" in request["goal"] and "pytest --count 50" in request["goal"]
    shown = request["context"]
    assert REPORT in shown and "tmpfs: /tmp:size=1g" in shown
    assert "tmpfs 64M 64M 0 100% /tmp" in shown  # the tool output a finding cites
    assert "MY SECRET OWN CHECK" not in shown + request["goal"]
    # The agent filed first, so the report wakes it the moment it lands.
    assert any("independent verifier's report" in text and "VERDICT: passed" in text for _, text in env.injected)


def test_a_verifier_only_reads_the_board(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    env.verifiers.in_turn()
    env.verifiers.on_child_start(child_session_id="child-v", child_goal=env.service.requests[-1]["goal"])
    tools.note_subagent("child-v")
    assert call(env, "search_board_review", {"name": "flaky-flush"}, session_id="child-v")["ok"]
    refused = call(env, "search_board_leads", {"name": "flaky-flush", "leads": [{"direction": "something else to try", "rationale": "because of reasons"}]},
                   session_id="child-v")
    assert not refused["ok"] and "only reads the board" in refused["error"]


def test_launching_outside_a_turn_waits_for_one(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    env.service.fail_with = RuntimeError("No active Hermes parent session is available.")
    env.verifiers.in_turn()
    flow = env.store.workflow("flaky-flush").flow
    assert flow.verifier_state == "wanted" and flow.verifier_note is None  # not counted as a failure
    env.service.fail_with = None
    run_verifier(env)


def test_toolsets_narrow_until_the_parent_allows_them(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    seen = []

    def launch(request):
        seen.append(request.get("allowed_toolsets"))
        if request.get("allowed_toolsets") == ("terminal", "file"):
            raise ValueError("Requested toolsets would broaden parent permissions.")
        return SimpleNamespace(to_dict=lambda: {"subagent_id": "sa-x"})

    env.service.launch = launch
    env.verifiers.in_turn()
    assert seen == [("terminal", "file"), ("file",)]
    assert env.store.workflow("flaky-flush").flow.verifier_state == "running"


def test_a_verifier_that_cannot_launch_says_why_and_is_tried_again(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    env.service.fail_with = RuntimeError("Delegation spawning is paused.")
    env.verifiers.in_turn()
    flow = env.store.workflow("flaky-flush").flow
    assert flow.verifier_state == "wanted" and "spawning is paused" in flow.verifier_note
    assert env.store.verifiers_wanted() == []  # not again for a minute
    assert len(env.store.verifiers_wanted(retry_seconds=-5)) == 1


def test_a_failed_verifier_can_be_reconciled_or_run_again(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    run_verifier(env, report="", status="failed")
    flow = env.store.workflow("flaky-flush").flow
    assert flow.verifier_state == "failed" and "ended failed" in flow.verifier_note
    assert "will run again" in cmd.run(env.store, "reverify flaky-flush")
    run_verifier(env, report="It does not hold up.\nVERDICT: failed")
    assert env.store.workflow("flaky-flush").flow.verifier_verdict == "failed"


def test_verdicts_are_read_from_the_report():
    assert flowrt.verdict_of("blah\nVERDICT: passed\nmore") == "passed"
    assert flowrt.verdict_of("**Verdict:** FAILED") == "failed"
    assert flowrt.verdict_of("no verdict here") == "unclear"


def test_sent_back_from_verification_drops_and_cancels_the_running_verifier(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    env.verifiers.in_turn()
    env.verifiers.on_child_start(child_session_id="child-late", child_goal=env.service.requests[-1]["goal"])
    cmd.run(env.store, "back flaky-flush the arm64 run still fails")
    assert not env.verifiers.on_child_stop(child_session_id="child-late", child_summary="VERDICT: passed", child_status="completed")
    env.verifiers.in_turn()  # the next turn cancels it
    assert env.service.cancelled == [{"subagent_id": "sa-1"}]
    snap = env.store.snapshot("flaky-flush")
    assert snap.board.status == "open" and "Sent back from verification: the arm64 run still fails" in snap.notes[-1].text
    assert env.store.workflow("flaky-flush").flow.round == 2


def test_back_to_review_and_verified_again_from_complete(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    assert "your review again" in cmd.run(env.store, "toreview flaky-flush")
    assert env.store.workflow("flaky-flush").flow.verifier_state is None
    cmd.run(env.store, "verify flaky-flush")
    run_verifier(env)
    assert call(env, "search_board_verify", {"name": "flaky-flush", "verdict": "passed",
                                             "findings": "Checked it all again: fifty runs pass, compose file is right."})["ok"]
    assert call(env, "search_board_verification", {"name": "flaky-flush", "verdict": "passed",
                                                   "report": "Both agree: the test passes and the fix is the tmpfs size."})["ok"]
    assert "complete" in cmd.run(env.store, "accept flaky-flush")
    assert "in verification" in cmd.run(env.store, "verify flaky-flush")  # verified again, from complete
    flow = env.store.workflow("flaky-flush").flow
    assert flow.own_report is None and flow.verification is None and flow.verifier_state == "wanted"


def test_accept_needs_a_reconciled_report(env):
    solved_board(env)
    report(env)
    assert "not in verification" in cmd.run(env.store, "accept flaky-flush")
    cmd.run(env.store, "verify flaky-flush")
    assert "no verification report yet" in cmd.run(env.store, "accept flaky-flush")


# ---------------------------------------------------------------- exhausted, discarded, confirmed


def test_an_exhausted_proposal_is_reviewed_then_closed_by_the_operator(env):
    call(env, "search_board_open", {"name": "ghost", "title": "Find the memory leak in the worker",
                                    "origin": "RSS grows 10 MB an hour", "goal": "RSS flat for a day",
                                    "goal_test": "judge the graph", "goal_test_kind": "judgment"})
    closed = call(env, "search_board_close", {"name": "ghost", "status": "proposed_exhausted",
                                              "rationale": "Every allocation site was profiled and ruled out."})
    assert "what was tried" in closed["write_up"]
    assert report(env, "ghost", files=())["ok"]
    assert "nothing to verify" in cmd.run(env.store, "verify ghost")
    assert "complete" in cmd.run(env.store, "exhaust ghost")
    assert env.store.board("ghost").status == "exhausted"
    assert env.store.workflow("ghost").flow.stage == "deliverable"


def test_an_exhausted_proposal_sent_back_is_open_again(env):
    call(env, "search_board_open", {"name": "ghost", "title": "Find the memory leak in the worker",
                                    "origin": "RSS grows 10 MB an hour", "goal": "RSS flat for a day",
                                    "goal_test": "judge the graph", "goal_test_kind": "judgment"})
    call(env, "search_board_close", {"name": "ghost", "status": "proposed_exhausted", "rationale": "Every site was ruled out."})
    report(env, "ghost", files=())
    cmd.run(env.store, "comment ghost What about the thread-local caches?")
    cmd.run(env.store, "back ghost")
    assert env.store.board("ghost").status == "open"


def test_discard_from_anywhere(env):
    solved_board(env)
    report(env)
    assert "discarded" in cmd.run(env.store, "discard flaky-flush not worth it")
    board = env.store.board("flaky-flush")
    assert board.status == "abandoned" and "[discarded by the operator] not worth it" in board.outcome
    assert "discarded already" in cmd.run(env.store, "discard flaky-flush")
    assert "discarded already" in cmd.run(env.store, "abandon flaky-flush")  # the same move


def test_confirm_without_the_workflow_still_completes(env):
    call(env, "search_board_open", {"name": "taste", "title": "Pick a better colour palette",
                                    "origin": "the old one is ugly", "goal": "the operator likes it",
                                    "goal_test": "the operator looks", "goal_test_kind": "judgment"})
    call(env, "search_board_close", {"name": "taste", "status": "proposed_solved", "rationale": "Palette three reads well."})
    cmd.run(env.store, "confirm taste")
    assert env.store.workflow("taste").flow.stage == "deliverable"
    assert not report(env, "taste", files=())["ok"]


# ---------------------------------------------------------------- the agent, subagents, waking


def test_subagents_have_no_part_in_the_workflow(env):
    solved_board(env)
    tools.note_subagent("s-sub")
    sub = {"session_id": "s-sub"}
    assert "main agent's" in call(env, "search_board_report", {"name": "flaky-flush", "report": REPORT}, **sub)["error"]
    assert "main agent's" in call(env, "search_board_verify", {"name": "flaky-flush", "verdict": "passed", "findings": "x" * 50}, **sub)["error"]
    report(env)
    assert call(env, "search_board_review", {"name": "flaky-flush"}, **sub)["ok"]  # reading what was delivered is fine


def test_a_subagents_passing_test_wakes_the_main_agent(env):
    call(env, "search_board_open", {"name": "flaky-flush", "title": "Find why test_flush fails one run in fifty",
                                    "origin": "CI fails about 2%", "goal": "fifty green runs in a row",
                                    "goal_test": "pytest --count 50", "goal_test_kind": "command"})
    tools.note_subagent("s-sub")
    env.runs.append(("ran", 0, "50 passed", None))
    result = call(env, "search_board_test", {"name": "flaky-flush"}, session_id="s-sub")
    assert result["passed"] and "write_up" not in result
    run_poller(env)
    assert any("run by a subagent" in text for _, text in env.injected)


def test_a_wake_that_cannot_start_a_turn_goes_with_the_next_one(env):
    solved_board(env)
    report(env)
    env.poller._inject = lambda text, key: False
    cmd.run(env.store, "back flaky-flush")
    run_poller(env)
    block = hooks.make_context_hook(lambda: env.store)(session_id="s-main")["context"]
    assert "sent it back to you" in block
    env.poller._inject = lambda text, key: pytest.fail("a told notice must not be injected again")
    run_poller(env)


def test_review_tool_shows_what_was_delivered(env):
    solved_board(env)
    report(env)
    shown = call(env, "search_board_review", {"name": "flaky-flush"})
    assert shown["report"] == REPORT and shown["files"][0]["path"] == "/work/docker-compose.yml"
    one = call(env, "search_board_review", {"name": "flaky-flush", "file": "/work/docker-compose.yml"})
    assert "size=1g" in one["content"]


def test_a_verifier_lost_with_its_process_is_wanted_again(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    env.verifiers.in_turn()
    [(board_id, token, job)] = env.store.verifiers_running("proc-1")
    assert env.store.revive_orphans("proc-1") == 0  # still ours, still young
    assert env.store.revive_orphans("proc-2") == 1  # a new process: the child died with the old one
    flow = env.store.workflow("flaky-flush").flow
    assert flow.verifier_state == "wanted" and flow.verifier_token != token and "lost" in flow.verifier_note
    assert env.store.verifier_finish(board_id, token, ok=True, verdict="passed", report="old") is None


def test_snapshot_limits():
    many = [f"/f{i}" for i in range(31)]
    with pytest.raises(Exception, match="at most 30"):
        flowrt.snapshot_files(lambda p: "{}", many)
    big = flowrt.snapshot_files(lambda p: json.dumps({"content": "x" * 150_000}), ["/big"])
    assert big[0]["chars"] == 100_000 and big[0]["truncated"]


def test_no_reconciling_before_the_verifier_is_done(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    assert call(env, "search_board_verify", {"name": "flaky-flush", "verdict": "passed",
                                             "findings": "Re-ran the goal test: fifty runs pass, compose file checked."})["ok"]
    early = call(env, "search_board_verification", {"name": "flaky-flush", "verdict": "passed", "report": "z" * 60})
    assert not early["ok"] and "has not finished" in early["error"]


def test_an_old_verifiers_report_never_lands_on_a_new_verification(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    env.verifiers.in_turn()
    env.verifiers.on_child_start(child_session_id="child-old", child_goal=env.service.requests[-1]["goal"])
    cmd.run(env.store, "toreview flaky-flush")
    cmd.run(env.store, "verify flaky-flush")
    env.verifiers.in_turn()
    env.verifiers.on_child_start(child_session_id="child-new", child_goal=env.service.requests[-1]["goal"])
    assert not env.verifiers.on_child_stop(child_session_id="child-old", child_summary="VERDICT: passed", child_status="completed")
    flow = env.store.workflow("flaky-flush").flow
    assert flow.verifier_state == "running" and flow.verifier_report is None
    assert env.verifiers.on_child_stop(child_session_id="child-new", child_summary="VERDICT: failed", child_status="completed")
    assert env.store.workflow("flaky-flush").flow.verifier_verdict == "failed"


def test_a_report_under_an_old_token_is_dropped_by_the_store_itself(env):
    """Second line of defence: even if an old child were matched, its token no longer counts."""
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    env.verifiers.in_turn()
    old = env.store.workflow("flaky-flush").flow.verifier_token
    cmd.run(env.store, "toreview flaky-flush")
    cmd.run(env.store, "verify flaky-flush")
    env.verifiers.in_turn()
    flow = env.store.workflow("flaky-flush").flow
    assert flow.verifier_state == "running" and flow.verifier_token != old
    board_id = env.store.board("flaky-flush").id
    assert env.store.verifier_finish(board_id, old, ok=True, verdict="passed", report="VERDICT: passed") is None
    flow = env.store.workflow("flaky-flush").flow
    assert flow.verifier_state == "running" and flow.verifier_report is None
