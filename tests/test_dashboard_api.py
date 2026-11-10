"""The Desktop pane's backend routes, against a temporary database."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def api(tmp_path):
    spec = importlib.util.spec_from_file_location("pwn_board_api_under_test", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store = module._m.Store.at(tmp_path / "boards.db")
    module._store = store
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/pwn-board")
    return TestClient(app), store, module


def seed(store, m):
    store.open_board(name="flaky-flush", title="Find why flush fails one run in fifty", origin="CI fails about 2%",
                     goal="fifty green runs in a row", goal_test="pytest --count 50", goal_test_kind="judgment")
    store.declare_leads("flaky-flush", [{"direction": "Reproduce with fifty runs", "rationale": "Need a repro first"}])
    store.claim("flaky-flush", "L1", "main:c")
    e = store.record_evidence(tool_name="terminal", args={"command": "pytest"}, result='{"output": "ENOSPC"}')
    store.record_finding("flaky-flush", statement="Fails 3 of 50 with ENOSPC", polarity="supports", basis="observed",
                         evidence=[e], worker="main:c", lead_id="L1")
    return e


def test_list_and_read(api):
    client, store, m = api
    seed(store, m)
    boards = client.get("/api/plugins/pwn-board/boards").json()["boards"]
    assert boards[0]["name"] == "flaky-flush" and boards[0]["findings"] == 1
    snap = client.get("/api/plugins/pwn-board/boards/flaky-flush").json()
    assert snap["leads"][0]["state"] == "explored" and snap["findings"][0]["evidence"] == [1]
    ev = client.get("/api/plugins/pwn-board/evidence?ids=1").json()["evidence"][0]
    assert ev["tool_name"] == "terminal" and "ENOSPC" in ev["excerpt"]
    assert client.get("/api/plugins/pwn-board/boards/nope").status_code == 404


def test_operator_actions(api):
    client, store, m = api
    seed(store, m)
    base = "/api/plugins/pwn-board/boards/flaky-flush"
    note = client.post(f"{base}/note", json={"text": "F1: the container has a 64 MB /tmp"}).json()
    assert note["note"] == {"id": "N1", "about": "F1"}
    assert client.post(f"{base}/unnote", json={"id": "N1"}).json()["ok"]
    assert client.post(f"{base}/decide", json={"decision": "confirm"}).status_code == 400  # not proposed
    store.close("flaky-flush", "proposed_solved", "the fix makes fifty runs pass")
    assert client.post(f"{base}/decide", json={"decision": "nonsense"}).status_code == 400
    sent = client.post(f"{base}/decide", json={"decision": "reject", "why": "still fails on arm64"}).json()
    assert sent["board"]["status"] == "open"
    assert any("arm64" in n.text for n in store.snapshot("flaky-flush").notes)
    assert any("sent 'flaky-flush' back" in text for text in store.take_notices())


def test_nothing_here_writes_the_search(api):
    client, store, m = api
    paths = {route.path for route in m.router.routes}
    assert not any(word in path for path in paths
                   for word in ("lead", "finding", "claim", "test", "open", "report", "answer", "verification"))


def test_pane_fixture(api, tmp_path):
    """Write the snapshot the JavaScript test renders, from the real routes."""
    client, store, m = api
    seed(store, m)
    store.declare_leads("flaky-flush", [{"direction": "Measure /tmp inside the container", "rationale": "F1 says ENOSPC",
                                         "from_findings": ["F1"], "priority": 7}])
    store.add_note("flaky-flush", "L2: try a 1 GB tmpfs")
    # Through the workflow, as far as a reconciled verification waiting for the operator.
    store.close("flaky-flush", "proposed_solved", "the tmpfs fix makes fifty runs pass")
    store.submit_report("flaky-flush", "Raised the container's tmpfs to 1 GB; fifty runs pass on the goal test.",
                        [{"path": "/w/docker-compose.yml", "content": "tmpfs: /tmp:size=1g", "chars": 19}])
    store.comment("flaky-flush", "Did you try arm64?")
    store.to_verification("flaky-flush")
    [(board_id, token)] = store.verifiers_wanted()
    store.verifier_launched(board_id, token, "{}", "proc")
    store.verifier_finish(board_id, token, ok=True, verdict="passed", report="VERDICT: passed\n\nThe compose file shows 1 GB.")
    store.own_check("flaky-flush", "passed", "Re-ran the goal test: fifty runs pass. The compose file is right.")
    store.reconcile("flaky-flush", "passed", "Both checks agree: the fix is the tmpfs size, and the test passes.")
    target = os.environ.get("PWN_BOARD_PANE_FIXTURE")
    if not target:
        pytest.skip("set PWN_BOARD_PANE_FIXTURE to write the pane fixture")
    Path(target).write_text(json.dumps({
        "boards": client.get("/api/plugins/pwn-board/boards?all=true").json(),
        "board": client.get("/api/plugins/pwn-board/boards/flaky-flush").json(),
        "evidence": client.get("/api/plugins/pwn-board/evidence?ids=1").json(),
        "workflow": client.get("/api/plugins/pwn-board/boards/flaky-flush/workflow").json(),
        "file": client.get("/api/plugins/pwn-board/boards/flaky-flush/file", params={"path": "/w/docker-compose.yml"}).json(),
    }, indent=1))


def test_workflow_routes(api):
    client, store, m = api
    seed(store, m)
    base = "/api/plugins/pwn-board/boards/flaky-flush"
    store.close("flaky-flush", "proposed_solved", "the fix makes fifty runs pass")
    store.submit_report("flaky-flush", "A write-up long enough to be accepted by the store, about the fix.",
                        [{"path": "/w/a.py", "content": "print(1)", "chars": 8}])
    wf = client.get(f"{base}/workflow").json()
    assert wf["flow"]["stage"] == "review" and wf["state"] == "in review" and wf["files"][0]["path"] == "/w/a.py"
    assert client.get(f"{base}/file", params={"path": "/w/a.py"}).json()["content"] == "print(1)"
    assert client.get(f"{base}/file", params={"path": "/nope"}).status_code == 404
    assert client.post(f"{base}/comment", json={"text": "why this way?"}).json()["note"] == "R1"
    assert client.post(f"{base}/accept").status_code == 400
    assert client.post(f"{base}/verify").json()["ok"]
    assert client.get(f"{base}/workflow").json()["flow"]["verifier_state"] == "wanted"
    assert client.post(f"{base}/toreview", json={}).json()["ok"]
    assert client.post(f"{base}/back", json={"why": ""}).json()["round"] == 2
    assert client.post(f"{base}/discard", json={"why": "enough"}).json()["ok"]
    assert store.board("flaky-flush").status == "abandoned"


def test_build_says_what_each_side_runs(api):
    import re

    client, store, m = api
    version = re.search(r"^version:\s*(\S+)", (ROOT / "plugin.yaml").read_text(), re.M).group(1)
    assert client.get("/api/plugins/pwn-board/build").json() == {"api": version, "agent": None}
    store.record_build("agent", "0.1.0")
    assert client.get("/api/plugins/pwn-board/build").json() == {"api": version, "agent": "0.1.0"}


def test_the_pane_and_the_manifest_are_the_same_build():
    import re

    version = re.search(r"^version:\s*(\S+)", (ROOT / "plugin.yaml").read_text(), re.M).group(1)
    pane = re.search(r"^export const BUILD = '([^']+)'", (ROOT / "desktop" / "plugin.js").read_text(), re.M).group(1)
    assert pane == version, "bump desktop/plugin.js BUILD with plugin.yaml's version"
