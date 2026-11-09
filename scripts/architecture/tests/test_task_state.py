"""Ledger self-tests: dependencies, exclusivity, budgets, recovery, evidence."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import task_state  # noqa: E402

FINGERPRINT = "a" * 64


def make_plan(path: Path, tasks: list[dict]) -> Path:
    path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    return path


def task(task_id: str, deps: list[str], files: list[str] | None = None) -> dict:
    return {"id": task_id, "dependencies": deps, "activation": "core",
            "allowed_write_paths": files or [f"{task_id}.py"]}


@pytest.fixture
def setup(tmp_path):
    plan_path = make_plan(tmp_path / "tasks.json", [
        task("T00", []), task("T01", ["T00"]), task("T02", ["T01"]),
        task("T03", ["T01"]), task("T04", ["T02", "T03"]),
        {"id": "T30", "dependencies": [], "activation": "extension", "allowed_write_paths": []},
    ])
    state_path = tmp_path / "state.json"
    ledger = task_state.TaskLedger(str(state_path), json.loads(plan_path.read_text())["tasks"])
    ledger.save()
    return ledger, state_path, plan_path


def done(ledger, task_id, at="2026-11-09T00:00:00Z"):
    ledger.state["tasks"][task_id]["state"] = "DONE"
    ledger.save()


def result(path: Path, *, gate="PASS", acceptance="PASS", fingerprint=FINGERPRINT) -> dict:
    path.write_text("{}", encoding="utf-8")
    return {"state": "DONE", "result_path": str(path), "working_tree_fingerprint": fingerprint,
            "gate_results": [{"gate": "G1", "result": gate}],
            "acceptance_results": [{"assertion": "x", "result": acceptance}]}


# -- dependencies and exclusivity --------------------------------------------


def test_dependency_must_be_done(setup):
    ledger, _, _ = setup
    with pytest.raises(task_state.LedgerError, match="dependencies"):
        ledger.claim("T01", "agent-a")
    assert ledger.task_state("T01") == "PENDING"


def test_claim_after_dependency_done(setup):
    ledger, _, _ = setup
    done(ledger, "T00")
    claimed = ledger.claim("T01", "agent-a")
    assert claimed["state"] == "RUNNING" and claimed["attempts"] == 1
    assert ledger.active_claims()[0]["task_id"] == "T01"


def test_second_owner_cannot_claim_same_task(setup):
    ledger, _, _ = setup
    done(ledger, "T00")
    ledger.claim("T01", "agent-a")
    with pytest.raises(task_state.LedgerError, match="already held"):
        ledger.claim("T01", "agent-b")


def test_file_overlap_blocks_parallel_claims(setup):
    ledger, _, _ = setup
    done(ledger, "T00")
    ledger.tasks["T02"]["dependencies"] = []
    ledger.claim("T01", "agent-a", files=["shared.py"])
    with pytest.raises(task_state.LedgerError, match="overlap"):
        ledger.claim("T02", "agent-b", files=["shared.py"])


def test_parallel_limit_is_two(setup):
    ledger, _, _ = setup
    done(ledger, "T00")
    ledger.tasks["T02"]["dependencies"] = []
    ledger.tasks["T03"]["dependencies"] = []
    ledger.claim("T02", "agent-a", files=["a.py"])
    ledger.claim("T03", "agent-b", files=["b.py"])
    with pytest.raises(task_state.LedgerError, match="parallel limit"):
        ledger.claim("T01", "agent-c", files=["c.py"])


def test_extension_task_requires_opt_in(setup):
    ledger, _, _ = setup
    with pytest.raises(task_state.LedgerError, match="extension"):
        ledger.claim("T30", "agent-a")


# -- state machine -----------------------------------------------------------


def test_illegal_transition_is_refused(setup):
    ledger, _, _ = setup
    with pytest.raises(task_state.LedgerError, match="illegal transition"):
        ledger.mark_verifying("T00", "agent-a")
    ledger.state["tasks"]["T00"]["state"] = "RUNNING"
    ledger.state["tasks"]["T00"]["owner"] = "agent-a"
    ledger.save()
    ledger.mark_verifying("T00", "agent-a")
    with pytest.raises(task_state.LedgerError, match="illegal transition"):
        ledger.mark_verifying("T00", "agent-a")


def test_done_requires_passing_gates(tmp_path, setup):
    ledger, _, _ = setup
    ledger.state["tasks"]["T00"]["state"] = "RUNNING"
    ledger.state["tasks"]["T00"]["owner"] = "agent-a"
    ledger.state["active_tasks"] = [{"task_id": "T00", "owner": "agent-a", "files": []}]
    ledger.save()
    with pytest.raises(task_state.LedgerError, match="gate result"):
        ledger.finish("T00", result(tmp_path / "r.json", gate="FAIL"),
                      owner="agent-a", fingerprint=FINGERPRINT)


def test_done_requires_acceptance(tmp_path, setup):
    ledger, _, _ = setup
    ledger.state["tasks"]["T00"]["state"] = "RUNNING"
    ledger.state["tasks"]["T00"]["owner"] = "agent-a"
    ledger.state["active_tasks"] = [{"task_id": "T00", "owner": "agent-a", "files": []}]
    ledger.save()
    with pytest.raises(task_state.LedgerError, match="acceptance"):
        ledger.finish("T00", result(tmp_path / "r.json", acceptance="FAIL"),
                      owner="agent-a", fingerprint=FINGERPRINT)


def test_stale_report_cannot_certify_current_tree(tmp_path, setup):
    ledger, _, _ = setup
    ledger.state["tasks"]["T00"]["state"] = "RUNNING"
    ledger.state["tasks"]["T00"]["owner"] = "agent-a"
    ledger.save()
    with pytest.raises(task_state.LedgerError, match="fingerprint"):
        ledger.finish("T00", result(tmp_path / "r.json", fingerprint="b" * 64),
                      owner="agent-a", fingerprint=FINGERPRINT)


def test_done_with_full_evidence(tmp_path, setup):
    ledger, state_path, _ = setup
    ledger.state["tasks"]["T00"]["state"] = "RUNNING"
    ledger.state["tasks"]["T00"]["owner"] = "agent-a"
    ledger.state["active_tasks"] = [{"task_id": "T00", "owner": "agent-a", "files": []}]
    ledger.save()
    ledger.finish("T00", result(tmp_path / "r.json"), owner="agent-a", fingerprint=FINGERPRINT)
    assert ledger.task_state("T00") == "DONE"
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["tasks"]["T00"]["source_fingerprint"] == FINGERPRINT
    seqs = [event["seq"] for event in saved["events"]]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


def test_result_file_must_exist(tmp_path, setup):
    ledger, _, _ = setup
    ledger.state["tasks"]["T00"]["state"] = "RUNNING"
    ledger.state["tasks"]["T00"]["owner"] = "agent-a"
    ledger.save()
    payload = result(tmp_path / "r.json")
    payload["result_path"] = str(tmp_path / "absent.json")
    with pytest.raises(task_state.LedgerError, match="task-result"):
        ledger.finish("T00", payload, owner="agent-a", fingerprint=FINGERPRINT)


# -- budget and recovery -----------------------------------------------------


def test_attempt_budget_exhausts(tmp_path):
    plan_path = make_plan(tmp_path / "tasks.json", [task("T00", [])])
    ledger = task_state.TaskLedger(str(tmp_path / "state.json"),
                                   json.loads(plan_path.read_text())["tasks"], max_attempts=1)
    ledger.save()
    ledger.claim("T00", "agent-a")
    ledger.recover(lambda _: False, at="2026-11-09T00:00:00Z")
    with pytest.raises(task_state.LedgerError, match="budget"):
        ledger.claim("T00", "agent-b")
    assert ledger.task_state("T00") == "FAILED"


def test_recover_only_when_owner_confirmed_stopped(setup):
    ledger, _, _ = setup
    done(ledger, "T00")
    ledger.claim("T01", "agent-a")
    assert ledger.recover(lambda _: None) == {}
    assert ledger.task_state("T01") == "RUNNING"
    assert ledger.recover(lambda _: True) == {}
    assert ledger.task_state("T01") == "RUNNING"
    changed = ledger.recover(lambda _: False)
    assert changed == {"T01": "PENDING"}
    assert ledger.task_state("T01") == "PENDING"
    assert ledger.active_claims() == []


def test_atomic_replace_leaves_valid_json(setup):
    ledger, state_path, _ = setup
    done(ledger, "T00")
    ledger.claim("T01", "agent-a")
    json.loads(state_path.read_text(encoding="utf-8"))
    assert not list(state_path.parent.glob(".state-*"))


def test_source_fingerprint_changes_with_tree(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one"], cwd=repo, check=True)
    first = task_state.source_fingerprint(str(repo))
    (repo / "a.txt").write_text("two", encoding="utf-8")
    tracked_change = task_state.source_fingerprint(str(repo))
    assert tracked_change != first
    # A new directory shows only a single ``?? dir/`` status line: its file
    # contents must still change the fingerprint.
    (repo / "newdir").mkdir()
    (repo / "newdir" / "one.py").write_text("one", encoding="utf-8")
    untracked_one = task_state.source_fingerprint(str(repo))
    (repo / "newdir" / "one.py").write_text("two", encoding="utf-8")
    assert untracked_one != tracked_change
    assert task_state.source_fingerprint(str(repo)) != untracked_one
