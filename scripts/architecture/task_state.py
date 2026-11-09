"""Recoverable task ledger for the unattended development plan.

The ledger is deliberately local and file-based: no cloud scheduler, no queue
and no automatic replay of side-effecting CAD work.  It enforces:

* dependency checks (all dependencies DONE before a claim),
* one owner per task and at most two non-overlapping active tasks,
* an attempt budget (initial attempt + two repairs),
* atomic state replacement and monotonic event sequence,
* DONE only with passing acceptance and gate evidence bound to the current
  source fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

STATES = (
    "PENDING", "READY", "RUNNING", "VERIFYING", "DONE",
    "BLOCKED_ENV", "BLOCKED_BASELINE", "BLOCKED_CONTRACT", "FAILED",
)
TERMINAL = {"DONE", "BLOCKED_ENV", "BLOCKED_BASELINE", "BLOCKED_CONTRACT", "FAILED"}
CLAIMABLE = {"PENDING", "READY"}
TRANSITIONS = {
    "PENDING": {"READY", "RUNNING", "BLOCKED_ENV", "BLOCKED_BASELINE", "BLOCKED_CONTRACT"},
    "READY": {"RUNNING", "PENDING", "BLOCKED_ENV", "BLOCKED_BASELINE", "BLOCKED_CONTRACT"},
    "RUNNING": {"VERIFYING", "FAILED", "BLOCKED_ENV", "BLOCKED_BASELINE", "BLOCKED_CONTRACT"},
    "VERIFYING": {"DONE", "FAILED", "BLOCKED_ENV", "BLOCKED_BASELINE", "BLOCKED_CONTRACT"},
    "DONE": set(),
    "BLOCKED_ENV": {"READY", "PENDING"},
    "BLOCKED_BASELINE": {"READY", "PENDING"},
    "BLOCKED_CONTRACT": {"READY", "PENDING"},
    "FAILED": {"READY", "PENDING"},
}
MAX_PARALLEL_DEFAULT = 2
MAX_ATTEMPTS_DEFAULT = 3


class LedgerError(RuntimeError):
    """A claim or transition is not allowed; the ledger is unchanged."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def source_fingerprint(repo_root: str, git: str = "git") -> str:
    """SHA-256 over HEAD, porcelain status, the index and untracked file bytes.

    Untracked directories appear as a single ``?? dir/`` status line, so the
    status text alone cannot detect edits inside a newly created directory.
    The content digest of every untracked (non-ignored) file is therefore
    included, which is what makes a report produced before the current edit
    unable to certify it.  Ignored execution artifacts do not participate.
    """

    def run(*args: str) -> bytes:
        proc = subprocess.run([git, *args], cwd=repo_root, capture_output=True, check=True)
        return proc.stdout

    head = run("rev-parse", "HEAD")
    status = run("status", "--porcelain=v1")
    index = run("ls-files", "-s")
    untracked = [item for item in run("ls-files", "-o", "--exclude-standard", "-z").split(b"\0") if item]
    digests = bytearray()
    for raw in sorted(untracked):
        relative = raw.decode("utf-8", "surrogateescape")
        path = os.path.join(repo_root, relative)
        digests += raw + b"\0"
        try:
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digests += hashlib.sha256(chunk).digest()
        except OSError:
            digests += b"<unreadable>"
        digests += b"\0"
    return hashlib.sha256(head + b"\0" + status + b"\0" + index + b"\0" + bytes(digests)).hexdigest()


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _overlap(left: Iterable[str], right: Iterable[str]) -> bool:
    left_set = {item.replace("\\", "/").lower() for item in left}
    right_set = {item.replace("\\", "/").lower() for item in right}
    return bool(left_set & right_set)


class TaskLedger:
    def __init__(self, state_path: str, tasks: list[dict[str, Any]], *,
                 core_only: bool = True, max_parallel: int = MAX_PARALLEL_DEFAULT,
                 max_attempts: int = MAX_ATTEMPTS_DEFAULT,
                 state: dict[str, Any] | None = None) -> None:
        self.state_path = os.path.abspath(state_path)
        self.tasks = {task["id"]: task for task in tasks}
        self.core_only = core_only
        self.max_parallel = max_parallel
        self.max_attempts = max_attempts
        self.state = state if state is not None else self._new_state()

    # -- persistence ------------------------------------------------------
    @classmethod
    def load(cls, state_path: str, tasks_json_path: str, **kwargs: Any) -> "TaskLedger":
        with open(tasks_json_path, encoding="utf-8") as handle:
            plan = json.load(handle)
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
        return cls(state_path, plan["tasks"], state=state, **kwargs)

    def _new_state(self) -> dict[str, Any]:
        return {
            "schema_version": "catiaagent-development-state/1",
            "plan_version": "1.0",
            "run_id": None,
            "sequence": 0,
            "active_tasks": [],
            "gate_status": {gate: "NOT_RUN" for gate in ("G0", "G1", "G2", "G3", "G4", "G5", "G6")},
            "delivery_status": {
                "offline": "NOT_VERIFIED", "target_platform": "NOT_VERIFIED",
                "product": "NOT_VERIFIED", "extension": "NOT_ENABLED",
            },
            "events": [],
            "tasks": {
                task_id: {"state": "PENDING", "attempts": 0, "owner": None,
                          "result_path": None, "reason": None,
                          "source_fingerprint": None}
                for task_id in self.tasks
            },
        }

    def save(self) -> None:
        atomic_write_json(self.state_path, self.state)

    # -- events -----------------------------------------------------------
    def _event(self, task_id: str, old: str | None, new: str, owner: str | None,
               reason: str | None, at: str | None) -> None:
        self.state["sequence"] = int(self.state.get("sequence", 0)) + 1
        self.state.setdefault("events", []).append({
            "seq": self.state["sequence"], "at": at or _utc_now(), "task": task_id,
            "from": old, "to": new, "owner": owner, "reason": reason,
        })

    # -- queries ----------------------------------------------------------
    def task_state(self, task_id: str) -> str:
        return self.state["tasks"][task_id]["state"]

    def dependencies_done(self, task_id: str) -> bool:
        task = self.tasks[task_id]
        return all(self.task_state(dep) == "DONE" for dep in task.get("dependencies", []))

    def blocked_dependencies(self, task_id: str) -> list[str]:
        return [dep for dep in self.tasks[task_id].get("dependencies", [])
                if self.task_state(dep) != "DONE"]

    def active_claims(self) -> list[dict[str, Any]]:
        return list(self.state.get("active_tasks", []))

    # -- claim ------------------------------------------------------------
    def claim(self, task_id: str, owner: str, *, files: Iterable[str] = (),
              at: str | None = None) -> dict[str, Any]:
        if not owner or owner.strip() != owner:
            raise LedgerError("owner must be a non-empty trimmed string")
        task = self.tasks.get(task_id)
        if task is None:
            raise LedgerError(f"unknown task: {task_id}")
        if self.core_only and task.get("activation", "core") != "core":
            raise LedgerError(f"{task_id} is a conditional extension task")
        if self.state["tasks"][task_id]["state"] == "RUNNING":
            raise LedgerError(f"{task_id} is already held by "
                              f"{self.state['tasks'][task_id].get('owner')!r}")
        if self.state["tasks"][task_id]["state"] not in CLAIMABLE:
            raise LedgerError(f"{task_id} is {self.state['tasks'][task_id]['state']}, not claimable")
        missing = self.blocked_dependencies(task_id)
        if missing:
            raise LedgerError(f"{task_id} dependencies not DONE: {missing}")
        attempts = int(self.state["tasks"][task_id].get("attempts", 0))
        if attempts >= self.max_attempts:
            self.state["tasks"][task_id]["state"] = "FAILED"
            self.state["tasks"][task_id]["reason"] = "attempt budget exhausted"
            self._event(task_id, "PENDING", "FAILED", owner, "attempt budget exhausted", at)
            self.save()
            raise LedgerError(f"{task_id} attempt budget exhausted")
        active = self.active_claims()
        if len(active) >= self.max_parallel:
            raise LedgerError(f"parallel limit reached ({self.max_parallel})")
        file_list = [item.replace("\\", "/") for item in files]
        for claim in active:
            if _overlap(file_list, claim.get("files", [])):
                raise LedgerError(f"file overlap with active task {claim['task_id']}")

        old = self.state["tasks"][task_id]["state"]
        entry = self.state["tasks"][task_id]
        entry.update({"state": "RUNNING", "attempts": attempts + 1, "owner": owner,
                      "reason": None, "claimed_at": at or _utc_now()})
        active.append({"task_id": task_id, "owner": owner, "files": file_list,
                       "claimed_at": entry["claimed_at"]})
        self.state["active_tasks"] = active
        self._event(task_id, old, "RUNNING", owner, "claimed", at)
        self.save()
        return dict(entry)

    # -- transitions ------------------------------------------------------
    def _transition(self, task_id: str, new: str, owner: str, reason: str | None,
                    at: str | None) -> None:
        if new not in STATES:
            raise LedgerError(f"unknown state {new}")
        old = self.task_state(task_id)
        if new not in TRANSITIONS.get(old, set()):
            raise LedgerError(f"illegal transition {task_id}: {old} -> {new}")
        entry = self.state["tasks"][task_id]
        if old == "RUNNING" and entry.get("owner") != owner:
            raise LedgerError(f"{task_id} is held by {entry.get('owner')!r}, not {owner!r}")
        entry.update({"state": new, "reason": reason})
        if new != "RUNNING":
            entry["owner"] = None
            self.state["active_tasks"] = [
                claim for claim in self.state.get("active_tasks", [])
                if claim["task_id"] != task_id
            ]
        self._event(task_id, old, new, owner, reason, at)
        self.save()

    def mark_verifying(self, task_id: str, owner: str, *, at: str | None = None) -> None:
        self._transition(task_id, "VERIFYING", owner, None, at)

    def finish(self, task_id: str, result: dict[str, Any], *, owner: str,
               fingerprint: str, at: str | None = None) -> None:
        """Finish a task only when its own evidence says every gate passed."""
        entry = self.state["tasks"][task_id]
        if entry["state"] == "RUNNING":
            self.mark_verifying(task_id, owner, at=at)
        elif entry["state"] != "VERIFYING":
            raise LedgerError(f"{task_id} is {entry['state']}, not VERIFYING")
        if result.get("state") != "DONE":
            target = result.get("state") if result.get("state") in TERMINAL else "FAILED"
            self.state["tasks"][task_id]["result_path"] = result.get("result_path")
            self._transition(task_id, target, owner, result.get("blocker") or result.get("state"), at)
            return
        gate_results = result.get("gate_results") or []
        acceptance = result.get("acceptance_results") or []
        if not gate_results or any(item.get("result") != "PASS" for item in gate_results):
            raise LedgerError("DONE requires every gate result to be PASS")
        if not acceptance or any(item.get("result") != "PASS" for item in acceptance):
            raise LedgerError("DONE requires every acceptance assertion to be PASS")
        if not fingerprint or fingerprint != result.get("working_tree_fingerprint"):
            raise LedgerError("report fingerprint does not match the finishing tree")
        result_path = result.get("result_path")
        if not result_path or not os.path.isfile(result_path):
            raise LedgerError("DONE requires an existing task-result.json")
        self.state["tasks"][task_id].update({
            "result_path": result_path, "source_fingerprint": fingerprint,
        })
        self._transition(task_id, "DONE", owner, None, at)

    def block(self, task_id: str, owner: str, state: str, reason: str, *,
              at: str | None = None) -> None:
        if state not in {"BLOCKED_ENV", "BLOCKED_BASELINE", "BLOCKED_CONTRACT", "FAILED"}:
            raise LedgerError("not a blocking state")
        self._transition(task_id, state, owner, reason, at)

    # -- recovery ---------------------------------------------------------
    def recover(self, owner_alive: Callable[[str], bool | None], *,
                at: str | None = None) -> dict[str, str]:
        """Return RUNNING tasks to PENDING only when the owner is confirmed gone.

        ``owner_alive`` returns True (still working: leave untouched), False
        (confirmed stopped: make claimable again) or None (unknown: leave
        fenced).  A CAD operation is never replayed by recovery.
        """
        changed: dict[str, str] = {}
        for claim in list(self.state.get("active_tasks", [])):
            task_id = claim["task_id"]
            owner = claim.get("owner") or ""
            alive = owner_alive(owner)
            if alive is False:
                entry = self.state["tasks"][task_id]
                entry.update({"state": "PENDING", "owner": None,
                              "reason": "owner confirmed stopped; review saved evidence before reclaiming"})
                changed[task_id] = "PENDING"
                self._event(task_id, "RUNNING", "PENDING", owner, "recovered from stopped owner", at)
        if changed:
            self.state["active_tasks"] = [
                claim for claim in self.state.get("active_tasks", [])
                if claim["task_id"] in self.state["tasks"]
                and self.state["tasks"][claim["task_id"]]["state"] == "RUNNING"
            ]
            self.save()
        return changed
