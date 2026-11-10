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


class Verifier:
    """A fake ctx.llm.complete_structured: records what it was shown, answers as told."""

    def __init__(self, verdict="passed", fail=False):
        self.verdict, self.fail, self.inputs = verdict, fail, []

    def __call__(self, **kwargs):
        self.inputs.append(kwargs["input"][0]["text"])
        if self.fail:
            raise TimeoutError("provider timed out")
        return SimpleNamespace(parsed={"verdict": self.verdict, "report": "Checked the compose file and the test runs.",
                                       "claims": [{"claim": "tmpfs raised to 1 GB", "status": "supported", "why": "docker-compose.yml line 3"}],
                                       "missing": ["did not re-run the test"]}, text="")


@pytest.fixture
def env(tmp_path):
    store = Store.at(tmp_path / "boards.db")
    runs = []
    files = Files({"/work/docker-compose.yml": "services:\n  test:\n    tmpfs: /tmp:size=1g", "/work/NOTES.md": "notes"})
    t = tools.Tools(lambda: store, lambda: (lambda c, to, kw: runs.pop(0)), reader=files,
                    session_key_of=lambda kwargs: "ses_main")
    injected = []
    verifier = Verifier()
    poller = flowrt.Poller(lambda: store, verifier, lambda text, key: injected.append((key, text)) or True, lambda b: None)
    tools.SUBAGENT_SESSIONS.discard("s-sub")
    return SimpleNamespace(store=store, t=t, runs=runs, files=files, injected=injected, verifier=verifier, poller=poller)


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
    import threading
    for thread in threading.enumerate():
        if thread.name.startswith("search-board-verifier"):
            thread.join(5)
    env.poller.deliver(env.store)


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
    assert "R1 added" in cmd.run(env.store, "comment flaky-flush Did you check it on arm64?")
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
    assert "taken back" in cmd.run(env.store, "uncomment flaky-flush R1")
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
    run_poller(env)  # the verifier runs; the agent is woken to do its own check
    assert any("Check it yourself now" in text for _, text in env.injected)
    assert not any("verifier's report" in text for _, text in env.injected)

    read = call(env, "search_board_read", {"name": "flaky-flush"})
    assert read["workflow"]["verifier"] == "held until you file your own check"
    early = call(env, "search_board_verification", {"name": "flaky-flush", "verdict": "passed", "report": "x" * 50})
    assert not early["ok"] and "own check first" in early["error"]

    own = call(env, "search_board_verify", {"name": "flaky-flush", "verdict": "passed",
                                            "findings": "Re-ran the goal test: fifty runs pass. Read the compose file: tmpfs is 1 GB."})
    assert own["verifier"]["state"] == "done" and own["verifier"]["report"].startswith("VERDICT: passed")
    assert "[supported] tmpfs raised to 1 GB" in own["verifier"]["report"]
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


def test_the_verifier_sees_the_work_not_the_agents_word(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    call(env, "search_board_verify", {"name": "flaky-flush", "verdict": "passed", "findings": "MY SECRET OWN CHECK " * 5})
    run_poller(env)
    shown = env.verifier.inputs[0]
    assert REPORT in shown and "tmpfs: /tmp:size=1g" in shown
    assert "tmpfs 64M 64M 0 100% /tmp" in shown  # the tool output a finding cites
    assert "recorded by the plugin's goal test run" in shown
    assert "MY SECRET OWN CHECK" not in shown
    # The agent filed first, so the report wakes it the moment it lands.
    assert any("independent verifier's report" in text and "VERDICT: passed" in text for _, text in env.injected)


def test_a_failed_verifier_can_be_reconciled_or_run_again(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    env.poller._complete = Verifier(fail=True)
    run_poller(env)
    flow = env.store.workflow("flaky-flush").flow
    assert flow.verifier_state == "failed" and "provider timed out" in flow.verifier_note
    assert "will run again" in cmd.run(env.store, "reverify flaky-flush")
    env.poller._complete = Verifier(verdict="failed")
    run_poller(env)
    assert env.store.workflow("flaky-flush").flow.verifier_verdict == "failed"


def test_sent_back_from_verification_drops_the_running_verifier(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    [(board_id, token)] = env.store.verifiers_wanted()
    assert env.store.verifier_start(board_id, token)
    cmd.run(env.store, "back flaky-flush the arm64 run still fails")
    assert env.store.verifier_finish(board_id, token, ok=True, verdict="passed", report="late") is None
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
    run_poller(env)
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


def test_stale_running_verifiers_are_wanted_again(env):
    solved_board(env)
    report(env)
    cmd.run(env.store, "verify flaky-flush")
    [(board_id, token)] = env.store.verifiers_wanted()
    env.store.verifier_start(board_id, token)
    with env.store._db(write=True) as db:
        db.execute("UPDATE flow SET verifier_started_at='2020-01-01T00:00:00+00:00'")
    assert env.store.revive_stale_verifiers() == 1
    [(_, new_token)] = env.store.verifiers_wanted()
    assert new_token != token
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
    [(board_id, old)] = env.store.verifiers_wanted()
    env.store.verifier_start(board_id, old)
    cmd.run(env.store, "toreview flaky-flush")
    cmd.run(env.store, "verify flaky-flush")
    [(_, new)] = env.store.verifiers_wanted()
    env.store.verifier_start(board_id, new)
    assert env.store.verifier_finish(board_id, old, ok=True, verdict="passed", report="from the old one") is None
    flow = env.store.workflow("flaky-flush").flow
    assert flow.verifier_state == "running" and flow.verifier_report is None
