"""Deterministic edit coordinator: bind, validate, mutate, verify, save, recover."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catia_mcp.editing import report as report_mod
from catia_mcp.editing.journal import Journal
from catia_mcp.editing.probe import approx_equal, read_model
from catia_mcp.editing.session import (
    _decorate_holes,
    build_snapshot,
    create_working_copy,
    get_param,
    now,
    norm,
    resolve_document,
    sha256_file,
    unit_of,
    assign_value,
)
from catia_mcp.editing.validation import plan_hash, validate_plan
from catia_mcp.results import operation_result

TOOL = "catia_edit"
# Any session that still owns an open working copy keeps the generic write
# tools locked, including failed/rolled_back sessions (contract §7). A failed
# session is released only by catia_recover_edit, which can abandon it.
_ACTIVE = {
    "ready",
    "verified",
    "executing",
    "saving",
    "recovering",
    "unknown",
    "failed",
    "rolled_back",
}
_OPERATION_STATES = ("intent", "applied", "verified", "failed", "unknown", "recovered", "rejected")
_REPLAY_STATES = ("verified", "failed", "unknown", "recovered", "rejected")
# Clarifications that need the *user's* answer arm the session gate. A missing
# or unknown handle is model-fixable from the candidate list and must stay
# retryable without a user turn; otherwise every first draft with a null target
# deadlocks the session.
_GATE_PATHS = (
    "text.provenance",
    "operation.unit",
    "reference.basis",
    "reference.direction",
    "reference.directionText",
    "reference.frame",
    "reference.axis",
    "reference.revision",
    "target.holeCount",
    "target.holeCount.provenance",
    "target.kind",
    "target.kind.provenance",
    "target.sketch.provenance",
    "preserve",
)


def _needs_gate(unresolved: list[Any]) -> bool:
    for path in unresolved:
        text = str(path)
        if any(text == gate or text.startswith(gate + ".") for gate in _GATE_PATHS):
            return True
    return False


def _text_key(text: str) -> str:
    """Whitespace-insensitive key used by the clarification gate."""
    return "".join(str(text or "").split()).lower()


def _measured_holes(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Only holes whose role was measured; older snapshots keep every circle."""
    holes = model.get("holes") or []
    if any(hole.get("role") for hole in holes):
        return [hole for hole in holes if hole.get("role") == "hole"]
    return list(holes)


def _acceptance_from_baseline(baseline: dict[str, Any]) -> list[dict[str, Any]]:
    """Assertions that prove a revert restored the recorded before-state."""
    acceptance: list[dict[str, Any]] = []
    volume = baseline.get("volumeMm3")
    if isinstance(volume, (int, float)) and not isinstance(volume, bool):
        acceptance.append({"type": "volume", "expected": round(float(volume), 6)})
    bbox = baseline.get("bbox") if isinstance(baseline.get("bbox"), dict) else None
    if bbox and isinstance(bbox.get("min"), dict) and isinstance(bbox.get("max"), dict):
        for axis in ("x", "y", "z"):
            try:
                acceptance.append(
                    {
                        "type": "bbox_range",
                        "axis": axis,
                        "min": round(float(bbox["min"][axis]), 6),
                        "max": round(float(bbox["max"][axis]), 6),
                    }
                )
            except Exception:
                continue
    holes = baseline.get("holes")
    if isinstance(holes, list) and holes:
        measured = holes
        if any(hole.get("role") for hole in holes):
            measured = [hole for hole in holes if hole.get("role") == "hole"]
        if measured:
            acceptance.append(
                {
                    "type": "hole_positions",
                    "holes": [
                        {
                            "x": round(float(hole["center"]["x"]), 6),
                            "y": round(float(hole["center"]["y"]), 6),
                        }
                        for hole in measured
                    ],
                }
            )
            if all(isinstance(hole.get("centerWorld"), dict) for hole in measured):
                acceptance.append(
                    {
                        "type": "hole_world_positions",
                        "holes": [
                            {
                                "x": round(float(hole["centerWorld"]["x"]), 6),
                                "y": round(float(hole["centerWorld"]["y"]), 6),
                                "z": round(float(hole["centerWorld"]["z"]), 6),
                            }
                            for hole in measured
                        ],
                    }
                )
    return acceptance


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _mtime_utc(path: str) -> str:
    """Real file mtime as a UTC ISO timestamp (never the wall clock)."""
    return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat(
        timespec="milliseconds"
    )


def _hole_signature(holes: list[dict[str, Any]]) -> list[tuple[float, float, float, float]]:
    signature = []
    for hole in holes:
        center = hole.get("center") or {}
        signature.append(
            (
                float(center.get("x", 0.0)),
                float(center.get("y", 0.0)),
                float(center.get("z", 0.0)),
                float(hole.get("radius", 0.0)),
            )
        )
    return sorted(signature)


def _work_root(source_path: str, working_dir: str | None, session_id: str) -> Path:
    """Where the server-owned working copy lives.

    Default: next to the source, inside the same project (`<source dir>/.catia-edit/<session>`),
    so the working copy and the delivery stay with the project instead of a machine-level cache.
    `workingDir` overrides it; `CATIA_MCP_EDIT_DIR` sets a machine-wide root.
    """
    if working_dir:
        return Path(working_dir)
    env_root = os.environ.get("CATIA_MCP_EDIT_DIR")
    if env_root:
        return Path(env_root) / session_id
    return Path(source_path).parent / ".catia-edit" / session_id


def delivery_conflict(delivery_path: str, open_document: str | None) -> dict[str, Any] | None:
    """Refuse a delivery target that CATIA already has open (踩坑 69).

    CATIA cannot SaveAs onto a path whose document is open; the COM error is a bare E_FAIL
    that looks exactly like a permission problem, so the check has to happen here, before
    SaveAs, and say what to do about it.
    """
    if not open_document:
        return None
    return _envelope(
        False,
        "SAVE_CONFLICT",
        f"delivery path is already open in CATIA as '{open_document}': {delivery_path}; "
        f"close that document (or the working copy) and retry with the same path, "
        f"or deliver to a different path",
        modified=False,
        data={"deliveryPath": delivery_path, "openDocument": open_document},
    )


def close_document_check(doc: Any) -> dict[str, Any]:
    """Close a document and report the outcome instead of swallowing it (踩坑 69)."""
    try:
        doc.Close()
    except Exception as exc:  # noqa: BLE001 - COM raises bare Exception
        return {"id": "close", "status": "fail", "evidence": {"error": str(exc)}}
    return {"id": "close", "status": "pass", "evidence": {}}


def _envelope(
    ok: bool,
    code: str,
    message: str,
    *,
    modified: bool | None,
    data: dict[str, Any],
    operation_id: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    result = operation_result(
        tool=TOOL,
        ok=ok,
        code=code,
        message=message,
        data=data,
        modified=modified,
        operation_id=operation_id,
        warnings=warnings,
    )
    result["data"] = data
    return result


class EditCoordinator:
    def __init__(self, connection: Any, journal: Journal | None = None) -> None:
        self.conn = connection
        self.journal = journal or Journal()

    # ------------------------------------------------------------------
    # session helpers
    # ------------------------------------------------------------------
    def _load(self, session_id: str) -> dict[str, Any]:
        session = self.journal.load_session(session_id)
        if session is None:
            raise LookupError(session_id)
        return session

    def _save(self, session: dict[str, Any]) -> None:
        self.journal.save_session(session)

    def _doc(self, session: dict[str, Any]):
        app = self.conn.app
        try:
            doc = resolve_document(app, session["workingCopy"]["path"])
        except RuntimeError as exc:
            raise RuntimeError(
                f"DOCUMENT_CLOSED: cannot prove which documents are open ({exc})"
            ) from exc
        if doc is None:
            raise RuntimeError(
                f"DOCUMENT_CLOSED: the working copy {session['workingCopy']['path']} is not open; "
                "begin a new edit session instead of rebinding by name"
            )
        return doc

    def _fresh_model(self, session: dict[str, Any]) -> dict[str, Any]:
        """Re-read the model and re-attach the measured circle roles.

        ``read_model`` cannot classify a circle on its own (that needs the
        perturbation evidence). The session already stores it, so every
        acceptance/preserve comparison sees the same hole/boss split as the
        snapshot the plan was compiled from.
        """
        doc = self._doc(session)
        model = read_model(self.conn.app, doc, doc.Part)
        impacts = session.get("impacts") or {}
        if impacts:
            model = {**model, "holes": _decorate_holes(model.get("holes") or [], impacts)}
        return model

    # ------------------------------------------------------------------
    # begin
    # ------------------------------------------------------------------
    def begin_edit(
        self, source_path: str, operation_id: str | None = None, working_dir: str | None = None
    ) -> dict[str, Any]:
        if not source_path or not os.path.isabs(source_path):
            return _envelope(False, "INVALID_PLAN", "sourcePath must be an absolute path", modified=False, data={})
        if not source_path.lower().endswith(".catpart"):
            return _envelope(
                False, "UNSUPPORTED_CAPABILITY", "only .CATPart documents are supported in this phase",
                modified=False, data={},
            )
        if not os.path.exists(source_path):
            return _envelope(
                False,
                "NOT_FOUND",
                f"source does not exist: {source_path} (the path may have been moved or renamed; "
                f"check the file and retry with its current absolute path)",
                modified=False,
                data={"sourcePath": os.path.abspath(source_path)},
            )
        for existing in self.journal.list_sessions():
            if existing["status"] in _ACTIVE:
                record = self.journal.load_session(existing["session_id"])
                if not record or norm(record["source"]["path"]) != norm(source_path):
                    continue
                try:
                    bound_doc = resolve_document(self.conn.app, record["workingCopy"]["path"])
                    scan_ok = True
                except RuntimeError:
                    bound_doc, scan_ok = None, False
                if bound_doc is not None and existing["status"] in ("ready", "verified"):
                    return _envelope(
                        True,
                        "ALREADY_BOUND",
                        f"this source already has an open edit session: {existing['session_id']}",
                        modified=None,
                        data={
                            "editSessionId": existing["session_id"],
                            "businessState": existing["status"],
                            "sessionState": existing["status"],
                            "modelRevision": existing["revision"],
                            "snapshot": record.get("snapshot"),
                            "workingCopyPath": record["workingCopy"]["path"],
                        },
                    )
                if bound_doc is None and scan_ok:
                    # The working document is gone: no writes can be pending on it.
                    record["status"] = "abandoned"
                    self._save(record)
                    self.journal.append_event(
                        "session_abandoned",
                        {"reason": "working copy is no longer open"},
                        session_id=existing["session_id"],
                    )
                    continue
                return _envelope(
                    False,
                    "BUSY",
                    f"an active edit session already owns this source: {existing['session_id']}"
                    f" (status {existing['status']}); resolve it with catia_recover_edit",
                    modified=None,
                    data={
                        "editSessionId": existing["session_id"],
                        "businessState": existing["status"],
                        "sessionState": existing["status"],
                    },
                )
        app = self.conn.app
        # A source with unsaved edits lives only in CATIA's memory: copying it from
        # disk would silently discard the user's work, and SaveAs from it would
        # fight the open document. Refuse before any file is created.
        try:
            open_source = resolve_document(app, source_path)
        except RuntimeError as exc:
            return _envelope(
                False,
                "BUSY",
                f"cannot prove whether the source is already open in CATIA ({exc}); retry "
                f"after the document scan succeeds",
                modified=False,
                data={"sourcePath": os.path.abspath(source_path)},
            )
        except Exception as exc:  # noqa: BLE001 - an unusable connection cannot prove the state
            return _envelope(
                False,
                "BUSY",
                f"cannot prove whether the source is already open in CATIA "
                f"({type(exc).__name__}: {exc}); retry after the connection is usable",
                modified=False,
                data={"sourcePath": os.path.abspath(source_path)},
            )
        if open_source is not None:
            try:
                source_saved = bool(open_source.Saved)
            except Exception as exc:  # noqa: BLE001 - unreadable Saved is not proof
                return _envelope(
                    False,
                    "BUSY",
                    f"the source is open in CATIA but its Saved state cannot be read "
                    f"({type(exc).__name__}: {exc}); save and close it, then retry",
                    modified=False,
                    data={"sourcePath": os.path.abspath(source_path)},
                )
            if not source_saved:
                return _envelope(
                    False,
                    "EXTERNAL_CHANGE",
                    f"the source {source_path} is open in CATIA with unsaved changes (Saved=False); "
                    f"save or close that document before beginning an edit session so the working "
                    f"copy starts from bytes on disk, not from memory",
                    modified=False,
                    data={
                        "sourcePath": os.path.abspath(source_path),
                        "openDocument": str(getattr(open_source, "Name", "document")),
                    },
                )
        source_sha = sha256_file(source_path)
        source_mtime = _mtime_utc(source_path)
        source_size = os.path.getsize(source_path)
        session_id = uuid.uuid4().hex[:12]
        work_root = _work_root(source_path, working_dir, session_id)
        work_root_existed = os.path.isdir(str(work_root))
        try:
            doc, working_path, copied_by_server = create_working_copy(
                app, source_path, str(work_root), session_id
            )
        except Exception as exc:
            return _envelope(
                False,
                "SAVE_FAILED",
                f"could not create the working copy (CATIA SaveAs was refused and the "
                f"byte-copy fallback failed): {exc}",
                modified=False,
                data={},
            )
        part = doc.Part
        session: dict[str, Any] = {
            "schema": "catia-edit-session/1",
            "editSessionId": session_id,
            "modelRevision": 0,
            "createdUtc": now(),
            "status": "ready",
            "source": {
                "path": os.path.abspath(source_path),
                "sha256": source_sha,
                "sizeBytes": source_size,
                "mtimeUtc": source_mtime,
            },
            "workingCopy": {
                "path": os.path.abspath(working_path),
                "sha256": sha256_file(working_path),
                "createdUtc": now(),
            },
            "impacts": {},
            "results": [],
        }
        self.journal.save_session(session)
        self.journal.append_event("session_begin", {"source": session["source"]}, session_id=session_id)
        snapshot = build_snapshot(app, doc, part, session, with_impacts=True)
        capabilities = snapshot.get("capabilities") or {}
        if not capabilities.get("writableDrivers"):
            # Nothing in this model can be driven by a parameter: presenting the
            # session as editable would only produce NO_EFFECT later. Close the
            # copy this call created and remove only what this call created.
            session["status"] = "failed"
            session["snapshot"] = snapshot
            self._save(session)
            self.journal.append_event(
                "unsupported_capability",
                {
                    "reason": "no writable driver parameter",
                    "capabilities": capabilities,
                    "limitations": snapshot.get("limitations"),
                },
                session_id=session_id,
            )
            close_error = None
            try:
                doc.Close()
            except Exception as exc:  # noqa: BLE001 - report, do not hide the refusal
                close_error = f"{type(exc).__name__}: {exc}"
            cleanup_error = None
            try:
                if work_root_existed:
                    os.remove(working_path)
                else:
                    shutil.rmtree(str(work_root), ignore_errors=True)
            except Exception as exc:  # noqa: BLE001 - report, do not hide the refusal
                cleanup_error = f"{type(exc).__name__}: {exc}"
            return _envelope(
                False,
                "UNSUPPORTED_CAPABILITY",
                "this model has no editable driver parameter (writableDrivers is empty; "
                "it may be a non-parametric solid), so the working copy was closed and "
                "removed without any model change",
                modified=False,
                data={
                    "editSessionId": session_id,
                    "businessState": "failed",
                    "sessionState": "failed",
                    "snapshot": snapshot,
                    "closeError": close_error,
                    "cleanupError": cleanup_error,
                },
            )
        unrestored = [
            handle for handle, impact in snapshot["impacts"].items() if impact.get("restored") is not True
        ]
        if unrestored:
            # The dependency probe could not be proven reversible: refuse to present
            # this working copy as clean.
            session["status"] = "failed"
            session["snapshot"] = snapshot
            self._save(session)
            self.journal.append_event(
                "probe_not_restored", {"drivers": unrestored}, session_id=session_id
            )
            try:
                doc.Close()
            except Exception:
                pass
            return _envelope(
                False,
                "RECOVERY_UNPROVEN",
                "the dependency probe could not be proven reversible for: " + ", ".join(unrestored),
                modified=True,
                data={"editSessionId": session_id, "businessState": "failed", "snapshot": snapshot},
            )
        session["impacts"] = snapshot["impacts"]
        # The state at begin_edit is the only "original" this session can prove.
        # It is not a history of earlier files; reference.basis=original uses it
        # and asks for clarification when it is missing.
        session["baselineParameters"] = {
            p["handle"]: {"value": p.get("value"), "unit": p.get("unit")}
            for p in snapshot.get("parameters", [])
            if p.get("kind") == "driver" and _is_number(p.get("value"))
        }
        session["snapshot"] = snapshot
        self._save(session)
        self.journal.append_event(
            "snapshot",
            {
                "revision": 0,
                "drivers": snapshot["drivers"],
                "capabilities": snapshot["capabilities"],
            },
            session_id=session_id,
        )
        data = {
            "editSessionId": session_id,
            "businessState": "ready",
            "sessionState": "ready",
            "modelRevision": 0,
            "snapshot": snapshot,
            "workingCopyPath": session["workingCopy"]["path"],
            "sourceUnchanged": sha256_file(source_path) == source_sha,
        }
        if operation_id:
            data["clientOperationId"] = operation_id
        warnings: list[str] = []
        if copied_by_server:
            data["canDeliver"] = False
            warnings.append(
                "working copy was produced by a byte copy: CATIA refused SaveAs, so this "
                "session can edit and verify but not deliver (catia_finalize_edit needs a "
                "CATIA instance with save permission). data.canDeliver=false"
            )
        else:
            data["canDeliver"] = True
        return _envelope(
            True,
            "READY",
            "edit session bound to the working copy",
            modified=False,
            data=data,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # inspect / status
    # ------------------------------------------------------------------
    def inspect_edit(self, session_id: str) -> dict[str, Any]:
        try:
            session = self._load(session_id)
        except LookupError:
            return _envelope(False, "NOT_FOUND", f"unknown edit session {session_id}", modified=None, data={})
        refreshed = dict(session)
        doc = resolve_document(self.conn.app, session["workingCopy"]["path"])
        if doc is None:
            return _envelope(
                True,
                "DOCUMENT_CLOSED",
                "the working copy is not open in CATIA; status is from the journal only",
                modified=None,
                data={
                    "editSessionId": session_id,
                    "businessState": session["status"],
                    "sessionState": session["status"],
                    "modelRevision": session["modelRevision"],
                    "snapshot": session.get("snapshot"),
                    "documentOpen": False,
                },
            )
        snapshot = build_snapshot(self.conn.app, doc, doc.Part, refreshed, with_impacts=False)
        return _envelope(
            True,
            session["status"].upper(),
            "current model inspected",
            modified=None,
            data={
                "editSessionId": session_id,
                "businessState": session["status"],
                "sessionState": session["status"],
                "modelRevision": refreshed["modelRevision"],
                "snapshot": snapshot,
                "documentOpen": True,
                **(
                    {"pendingClarification": session["pendingClarification"]}
                    if isinstance(session.get("pendingClarification"), dict)
                    else {}
                ),
                **(
                    {"openAmbiguity": session["openAmbiguity"]}
                    if isinstance(session.get("openAmbiguity"), dict)
                    else {}
                ),
            },
        )

    def get_status(self, session_id: str) -> dict[str, Any]:
        try:
            session = self._load(session_id)
        except LookupError:
            return _envelope(False, "NOT_FOUND", f"unknown edit session {session_id}", modified=None, data={})
        last = (session.get("results") or [None])[-1]
        return _envelope(
            True,
            "STATUS",
            f"session is {session['status']}",
            modified=None,
            data={
                "editSessionId": session_id,
                "businessState": session["status"],
                "sessionState": session["status"],
                "modelRevision": session["modelRevision"],
                "operations": [
                    {
                        "operationId": op["operation_id"],
                        "state": op["state"],
                        "planHash": op["plan_hash"],
                        "updatedUtc": op["updated_utc"],
                    }
                    for op in self.journal.operations_in_state(session_id, _OPERATION_STATES)
                ],
                "lastResult": last,
                "events": self.journal.events(session_id, limit=20),
                **(
                    {"pendingClarification": session["pendingClarification"]}
                    if isinstance(session.get("pendingClarification"), dict)
                    else {}
                ),
                **(
                    {"openAmbiguity": session["openAmbiguity"]}
                    if isinstance(session.get("openAmbiguity"), dict)
                    else {}
                ),
            },
        )

    # ------------------------------------------------------------------
    # intent compilation (read-only)
    # ------------------------------------------------------------------
    def compile_intent(
        self,
        session_id: str,
        intent: dict[str, Any],
        operation_id: str | None = None,
        *,
        user_text: str | None = None,
        user_confirmed: bool = False,
        confirmation_text: str | None = None,
    ) -> dict[str, Any]:
        """Compile a structured intent into a plan without touching the model.

        This is a read-only helper: it re-reads the current snapshot and its
        stored perturbation evidence, then returns a decision and, when the
        intent is well determined, a plan that still has to go through the
        normal ``catia_apply_edit_plan`` validation and execution.

        ``user_text`` is injected by the host from the current user message and
        is authoritative. A ``needs_clarification`` decision arms a session gate:
        the same session cannot produce another plan until the user answers
        (``userConfirmed`` plus their verbatim reply). The gate is also enforced
        by ``apply_edit_plan``, so a hand-written plan cannot bypass it.
        """
        from catia_mcp.editing.intent import compile_intent as compile_draft

        try:
            session = self._load(session_id)
        except LookupError:
            return _envelope(False, "NOT_FOUND", f"unknown edit session {session_id}", modified=None, data={})
        if session["status"] not in ("ready", "verified"):
            return _envelope(
                True,
                "INTENT_UNSUPPORTED",
                f"session state is {session['status']}; it cannot accept a new plan. "
                f"Re-run catia_begin_edit before compiling another intent.",
                modified=False,
                data={
                    "schema": "cad-intent-compile/1",
                    "decision": "unsupported",
                    "plan": None,
                    "editSessionId": session_id,
                    "businessState": session["status"],
                    "modelRevision": session["modelRevision"],
                    "message": f"session state is {session['status']}",
                    "unresolved": ["session.state"],
                    "checks": [],
                    "candidates": [],
                    "notes": [],
                },
            )
        pending = session.get("pendingClarification")
        effective_text = user_text.strip() if isinstance(user_text, str) and user_text.strip() else None
        # A clarification can only be cleared by a *new* user message that
        # contains the reply the model quotes. Repeating the same message, or
        # paraphrasing, is not an answer.
        confirmation_ok = False
        if isinstance(pending, dict) and effective_text is not None:
            same_message = _text_key(effective_text) == _text_key(str(pending.get("userText") or ""))
            reply = _text_key(confirmation_text or "")
            confirmation_ok = bool(
                user_confirmed
                and not same_message
                and reply
                and reply in _text_key(effective_text)
            )
        try:
            doc = self._doc(session)
        except Exception as exc:  # noqa: BLE001 - a closed/relocated document is not a write error
            return _envelope(
                False,
                "DOCUMENT_CLOSED",
                f"the working copy for {session_id} is not open; re-run catia_begin_edit ({exc})",
                modified=False,
                data={"editSessionId": session_id},
            )
        snapshot = build_snapshot(self.conn.app, doc, doc.Part, session, with_impacts=False)
        compiled = compile_draft(
            intent,
            snapshot,
            session_id=session_id,
            operation_id=operation_id,
            baseline_parameters=session.get("baselineParameters"),
            user_text=effective_text,
        )
        decision = compiled.get("decision")
        if decision == "needs_clarification":
            unresolved = list(compiled.get("unresolved") or [])
            should_arm = compiled.get("clarificationSource") == "model" or _needs_gate(unresolved)
            if not should_arm:
                # Model-fixable (missing handle, a candidate that needs a
                # sketch/kind filter, ...): do not arm the user gate, so the
                # model may retry with a structural discriminator from the same
                # user turn. A hand-written plan is still blocked by apply until
                # a compile actually returns planned.
                ambiguity = {
                    "userText": effective_text if effective_text is not None else str(intent.get("text") or ""),
                    "textSource": compiled.get("textSource"),
                    "unresolved": unresolved,
                    "message": str(compiled.get("message") or ""),
                    "modelRevision": session["modelRevision"],
                    "createdUtc": now(),
                }
                if not isinstance(session.get("pendingClarification"), dict):
                    session["openAmbiguity"] = ambiguity
                    self._save(session)
                    self.journal.append_event(
                        "intent_ambiguity_open",
                        {"unresolved": unresolved, "textSource": ambiguity["textSource"]},
                        session_id=session_id,
                    )
                compiled["openAmbiguity"] = ambiguity
                compiled["clarificationGate"] = {
                    "armed": False,
                    "openAmbiguity": True,
                    "reason": "clarification is resolvable from the snapshot candidates; retry with an exact handle/sketch/kind",
                    "unresolved": unresolved,
                }
            else:
                # Arm/refresh the gate. The stored text is the host-injected
                # message, never the model's paraphrase.
                gate = {
                    "userText": effective_text if effective_text is not None else str(intent.get("text") or ""),
                    "textSource": compiled.get("textSource"),
                    "unresolved": unresolved,
                    "message": str(compiled.get("message") or ""),
                    "modelRevision": session["modelRevision"],
                    "createdUtc": now(),
                }
                session["pendingClarification"] = gate
                self._save(session)
                self.journal.append_event(
                    "intent_clarification_pending",
                    {"unresolved": gate["unresolved"], "textSource": gate["textSource"]},
                    session_id=session_id,
                )
                compiled["pendingClarification"] = gate
                compiled["clarificationGate"] = {"armed": True, "unresolved": unresolved}
        elif decision == "planned" and isinstance(pending, dict) and not confirmation_ok:
            previous = compiled.get("plan")
            compiled.update(
                {
                    "decision": "needs_clarification",
                    "plan": None,
                    "operationId": None,
                    "unresolved": list(pending.get("unresolved") or ["clarification.pending"]),
                    "message": (
                        "a clarification is pending for this session: answer the user's question "
                        "first, then call catia_compile_intent with userConfirmed=true and "
                        "confirmationText copied from the user's reply. Repeating or paraphrasing "
                        "the same request is not an answer."
                    ),
                    "pendingClarification": pending,
                }
            )
            checks = compiled.setdefault("checks", [])
            checks.append(
                {
                    "id": "clarification-gate",
                    "status": "fail",
                    "detail": "a plan was withheld until the pending clarification is answered",
                    "evidence": {
                        "blockedNewValue": ((previous or {}).get("changes") or [{}])[0].get("newValue"),
                        "pendingUnresolved": pending.get("unresolved"),
                        "userConfirmed": user_confirmed,
                        "confirmationMatched": False,
                    },
                }
            )
        elif decision in ("planned", "unsupported"):
            had_open = isinstance(session.get("openAmbiguity"), dict)
            session.pop("openAmbiguity", None)
            if had_open and not isinstance(pending, dict):
                self._save(session)
            if isinstance(pending, dict):
                cleared = {
                    "clearedUtc": now(),
                    "userConfirmed": bool(user_confirmed),
                    "confirmationMatched": confirmation_ok,
                    "confirmationText": confirmation_text,
                    "decision": decision,
                }
                session.pop("pendingClarification", None)
                self._save(session)
                self.journal.append_event(
                    "intent_clarification_cleared",
                    {
                        "decision": decision,
                        "userConfirmed": bool(user_confirmed),
                        "confirmationMatched": confirmation_ok,
                    },
                    session_id=session_id,
                )
                compiled["clarificationCleared"] = cleared
        decision = compiled.get("decision")
        code = {
            "planned": "INTENT_PLANNED",
            "needs_clarification": "INTENT_NEEDS_CLARIFICATION",
            "unsupported": "INTENT_UNSUPPORTED",
            "invalid": "INVALID_INTENT",
        }.get(str(decision), "INVALID_INTENT")
        data = dict(compiled)
        data.update(
            {
                "editSessionId": session_id,
                "businessState": session["status"],
                "modelRevision": session["modelRevision"],
            }
        )
        return _envelope(
            decision != "invalid",
            code,
            str(compiled.get("message") or code),
            modified=False,
            data=data,
            operation_id=compiled.get("operationId"),
        )

    # ------------------------------------------------------------------
    # task-scoped context, cases and multi-turn history (P2)
    # ------------------------------------------------------------------
    def get_task_context(
        self,
        session_id: str,
        *,
        attribute: str | None = None,
        text: str | None = None,
        operation_type: str | None = None,
        include_cases: bool = False,
    ) -> dict[str, Any]:
        """Read-only, task-scoped selection from the current snapshot."""
        from catia_mcp.editing.cases import compatible_cases
        from catia_mcp.editing.context import build_task_context

        try:
            session = self._load(session_id)
        except LookupError:
            return _envelope(False, "NOT_FOUND", f"unknown edit session {session_id}", modified=None, data={})
        if session["status"] not in ("ready", "verified"):
            return _envelope(
                False,
                "UNSUPPORTED_CAPABILITY",
                f"session state is {session['status']}; re-run catia_begin_edit",
                modified=False,
                data={"editSessionId": session_id},
            )
        try:
            doc = self._doc(session)
        except Exception as exc:  # noqa: BLE001
            return _envelope(False, "DOCUMENT_CLOSED", str(exc), modified=False, data={"editSessionId": session_id})
        snapshot = build_snapshot(self.conn.app, doc, doc.Part, session, with_impacts=False)
        cases = (
            compatible_cases(snapshot, attribute=attribute, operation_type=operation_type)
            if include_cases
            else None
        )
        data = build_task_context(
            snapshot,
            attribute=attribute,
            text=text,
            operation_type=operation_type,
            cases=cases,
        )
        data["state"] = self._history_summary(session)
        return _envelope(True, "TASK_CONTEXT", "task-scoped context built from the current snapshot", modified=False, data=data)

    def find_cases(
        self,
        session_id: str,
        *,
        attribute: str | None = None,
        operation_type: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        from catia_mcp.editing.cases import compatible_cases, snapshot_conditions

        try:
            session = self._load(session_id)
        except LookupError:
            return _envelope(False, "NOT_FOUND", f"unknown edit session {session_id}", modified=None, data={})
        try:
            doc = self._doc(session)
        except Exception as exc:  # noqa: BLE001
            return _envelope(False, "DOCUMENT_CLOSED", str(exc), modified=False, data={"editSessionId": session_id})
        snapshot = build_snapshot(self.conn.app, doc, doc.Part, session, with_impacts=False)
        rows = compatible_cases(
            snapshot,
            attribute=attribute,
            operation_type=operation_type,
            limit=max(1, min(int(limit), 20)),
        )
        return _envelope(
            True,
            "CASES",
            f"{len(rows)} compatible case(s); templates only, recompile against this snapshot",
            modified=False,
            data={
                "editSessionId": session_id,
                "modelRevision": session["modelRevision"],
                "conditions": snapshot_conditions(snapshot),
                "cases": rows,
            },
        )

    def get_history(self, session_id: str) -> dict[str, Any]:
        try:
            session = self._load(session_id)
        except LookupError:
            return _envelope(False, "NOT_FOUND", f"unknown edit session {session_id}", modified=None, data={})
        try:
            current = self._fresh_model(session)
        except Exception as exc:  # noqa: BLE001
            return _envelope(False, "DOCUMENT_CLOSED", str(exc), modified=False, data={"editSessionId": session_id})
        operations = []
        for result in session.get("results") or []:
            if not result.get("operationId"):
                continue
            before = self._baseline_from_payload(result.get("before") or {})
            after = self._baseline_from_payload(result.get("after") or {})
            operations.append(
                {
                    "operationId": result.get("operationId"),
                    "verdict": result.get("verdict"),
                    "code": result.get("code"),
                    "revision": result.get("modelRevision"),
                    "utc": result.get("utc"),
                    "intentText": (result.get("intent") or {}).get("text"),
                    "changes": (result.get("plan") or {}).get("changes"),
                    "revertOf": (result.get("intent") or {}).get("revertOf"),
                    "before": {"parameters": before.get("parameters"), "volumeMm3": before.get("volumeMm3")},
                    "after": {"parameters": after.get("parameters"), "volumeMm3": after.get("volumeMm3")},
                }
            )
        return _envelope(
            True,
            "HISTORY",
            f"{len(operations)} recorded operation(s)",
            modified=False,
            data={
                "editSessionId": session_id,
                "businessState": session["status"],
                "modelRevision": session["modelRevision"],
                "baselineParameters": session.get("baselineParameters"),
                "currentParameters": {
                    param["handle"]: param.get("value")
                    for param in current.get("parameters") or []
                    if param.get("kind") == "driver"
                },
                "operations": operations,
                "canRevertLast": self._can_revert(session),
                "pendingClarification": session.get("pendingClarification"),
            },
        )

    def revert_last_edit(self, session_id: str, confirm: bool = False) -> dict[str, Any]:
        """Revert the last verified operation through the normal apply path.

        The revert is itself a plan with a new operation id, so validation,
        revision, idempotency, rollback and readback all keep working. It is
        refused unless the session is verified and the last result is a PASS.
        """
        if confirm is not True:
            return _envelope(
                False,
                "INVALID_PLAN",
                "revert_last_edit requires confirm=true; the user must have asked to undo",
                modified=False,
                data={"editSessionId": session_id},
            )
        try:
            session = self._load(session_id)
        except LookupError:
            return _envelope(False, "NOT_FOUND", f"unknown edit session {session_id}", modified=None, data={})
        if session["status"] != "verified":
            return _envelope(
                False,
                "UNKNOWN_STATE",
                f"session state is {session['status']}; only a verified session can be reverted",
                modified=False,
                data={"editSessionId": session_id, "businessState": session["status"]},
            )
        if not self._can_revert(session):
            return _envelope(
                False,
                "ALREADY_REVERTED",
                "the last operation is not a reversible PASS (or it is already a revert)",
                modified=False,
                data={"editSessionId": session_id},
            )
        last = (session.get("results") or [])[-1]
        plan = last.get("plan") or {}
        try:
            current = self._fresh_model(session)
        except Exception as exc:  # noqa: BLE001
            return _envelope(False, "DOCUMENT_CLOSED", str(exc), modified=False, data={"editSessionId": session_id})
        changes = []
        for change in plan.get("changes") or []:
            handle = change.get("parameter")
            current_value = model_value(current, handle)
            if not isinstance(current_value, (int, float)):
                return _envelope(
                    False,
                    "PARAM_UNKNOWN",
                    f"the current value of {handle} is unreadable; cannot revert",
                    modified=False,
                    data={"editSessionId": session_id},
                )
            changes.append(
                {
                    "parameter": handle,
                    "oldValue": current_value,
                    "newValue": change.get("oldValue"),
                    "unit": change.get("unit"),
                    "reason": f"revert {last.get('operationId')}",
                }
            )
        if not changes:
            return _envelope(
                False,
                "INVALID_PLAN",
                "the last operation has no recorded changes",
                modified=False,
                data={"editSessionId": session_id},
            )
        before = self._baseline_from_payload(last.get("before") or {})
        acceptance = _acceptance_from_baseline(before)
        seed = json.dumps(
            {"session": session_id, "revision": session["modelRevision"], "revert": last.get("operationId")},
            sort_keys=True,
        )
        operation_id = "revert-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
        revert_plan = {
            "schema": "catia-edit-plan/1",
            "editSessionId": session_id,
            "operationId": operation_id,
            "expectedRevision": session["modelRevision"],
            "intent": {
                "text": "恢复上一步",
                "resolved": f"revert {last.get('operationId')} to its before-state",
                "revertOf": last.get("operationId"),
            },
            "changes": changes,
            "preserve": [],
            "acceptance": acceptance,
        }
        result = self.apply_edit_plan(session_id, revert_plan)
        data = result.setdefault("data", {})
        data["revertedOperationId"] = last.get("operationId")
        data["revertAcceptance"] = acceptance
        data["revertPlan"] = revert_plan
        return result

    @staticmethod
    def _can_revert(session: dict[str, Any]) -> bool:
        if session.get("status") != "verified":
            return False
        results = [row for row in session.get("results") or [] if row.get("operationId")]
        if not results:
            return False
        last = results[-1]
        if last.get("verdict") != "PASS":
            return False
        return not (last.get("intent") or {}).get("revertOf")

    @staticmethod
    def _history_summary(session: dict[str, Any]) -> dict[str, Any]:
        rows = [
            {
                "operationId": row.get("operationId"),
                "verdict": row.get("verdict"),
                "revision": row.get("modelRevision"),
                "intentText": (row.get("intent") or {}).get("text"),
                "revertOf": (row.get("intent") or {}).get("revertOf"),
            }
            for row in (session.get("results") or [])[-5:]
            if row.get("operationId")
        ]
        return {
            "modelRevision": session.get("modelRevision"),
            "businessState": session.get("status"),
            "baselineParameters": session.get("baselineParameters"),
            "recentOperations": rows,
            "canRevertLast": EditCoordinator._can_revert(session),
        }

    # ------------------------------------------------------------------
    # apply
    # ------------------------------------------------------------------
    def apply_edit_plan(self, session_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        try:
            session = self._load(session_id)
        except LookupError:
            return _envelope(False, "NOT_FOUND", f"unknown edit session {session_id}", modified=None, data={})
        if not isinstance(plan, dict):
            return _envelope(False, "INVALID_PLAN", "plan must be a JSON object", modified=False, data={})
        operation_id = str(plan.get("operationId") or uuid.uuid4().hex)
        digest = plan_hash(plan)
        existing_op = self.journal.get_operation(operation_id, session_id=session_id)
        if existing_op is not None:
            if existing_op["plan_hash"] != digest:
                return _envelope(
                    False,
                    "OPERATION_CONFLICT",
                    f"operationId {operation_id} was used with a different plan",
                    modified=None,
                    data={"editSessionId": session_id, "operationId": operation_id},
                )
            stored = existing_op["payload"].get("result")
            if stored and existing_op["state"] in _REPLAY_STATES:
                replay = dict(stored)
                replay["data"] = {**replay.get("data", {}), "idempotentReplay": True}
                return replay
            if existing_op["state"] == "recovered":
                # Reconciled without a stored result (nothing had been applied):
                # never run the plan a second time.
                return _envelope(
                    True,
                    "RECOVERED",
                    "this operation was reconciled by catia_recover_edit; it is not executed again",
                    modified=False,
                    data={
                        "editSessionId": session_id,
                        "operationId": operation_id,
                        "businessState": session["status"],
                        "sessionState": session["status"],
                        "planHash": "sha256:" + digest,
                        "idempotentReplay": True,
                    },
                )
            if existing_op["state"] in ("intent", "applied"):
                return _envelope(
                    False,
                    "UNKNOWN_STATE",
                    f"operation {operation_id} recorded an intent without evidence; run catia_recover_edit",
                    modified=None,
                    data={"editSessionId": session_id, "operationId": operation_id},
                )

        # A pending clarification blocks every *new* write, including a plan the
        # model wrote by hand instead of compiling it. An exact retry of a
        # finished operation was already answered above with its stored result.
        pending = session.get("pendingClarification")
        if isinstance(pending, dict):
            return _envelope(
                False,
                "CLARIFICATION_PENDING",
                "a clarification is pending for this session; no plan may be applied until the "
                "user answers and catia_compile_intent clears the gate with userConfirmed and "
                "confirmationText",
                modified=False,
                data={
                    "editSessionId": session_id,
                    "pendingClarification": pending,
                    "businessState": session["status"],
                },
            )

        open_ambiguity = session.get("openAmbiguity")
        if isinstance(open_ambiguity, dict):
            return _envelope(
                False,
                "AMBIGUITY_OPEN",
                "an earlier compile returned an unresolved binding; retry catia_compile_intent with "
                "an exact handle/sketch/kind discriminator until it returns planned. A hand-written "
                "plan is not accepted while the binding is unresolved.",
                modified=False,
                data={
                    "editSessionId": session_id,
                    "openAmbiguity": open_ambiguity,
                    "businessState": session["status"],
                },
            )

        # Only a new write is blocked by the session state; an exact retry of a
        # finished operation was answered above with its stored result.
        if session["status"] in ("executing", "saving", "recovering", "unknown"):
            return _envelope(
                False,
                "UNKNOWN_STATE" if session["status"] == "unknown" else "BUSY",
                f"session {session_id} is {session['status']}; resolve it with catia_recover_edit",
                modified=None,
                data={
                    "editSessionId": session_id,
                    "businessState": session["status"],
                    "sessionState": session["status"],
                },
            )
        if session["status"] in ("completed",):
            return _envelope(
                False, "INVALID_PLAN", "session is completed; begin a new edit session",
                modified=False,
                data={
                    "editSessionId": session_id,
                    "businessState": "completed",
                    "sessionState": "completed",
                },
            )

        try:
            doc = self._doc(session)
        except RuntimeError as exc:
            return _envelope(False, "DOCUMENT_CLOSED", str(exc), modified=None, data={"editSessionId": session_id})

        before_model = self._fresh_model(session)
        # One normalized baseline for the intent row, the journal evidence and the
        # rollback: never hand a read_model() result (parameters is a list) to
        # something that expects {handle: value}.
        baseline = self._baseline_from_model(before_model)
        base_snapshot = dict(session.get("snapshot") or {})
        current_snapshot = self._snapshot_from(session, before_model)
        drift = self._drift(base_snapshot, before_model)
        if drift:
            # Someone changed the model outside this workflow. Nothing of ours is
            # in flight (an intent row would exist), so the session is failed, not
            # unknown: the baseline is refreshed and old plans become stale.
            session["modelRevision"] = int(session["modelRevision"]) + 1
            session["status"] = "failed"
            session["snapshot"] = self._snapshot_from(session, before_model)
            self._save(session)
            self.journal.append_event("external_change", {"drift": drift}, session_id=session_id)
            return _envelope(
                False,
                "EXTERNAL_CHANGE",
                "the working model no longer matches the last verified snapshot; the baseline was refreshed, "
                "re-plan against the new revision",
                modified=None,
                data={
                    "editSessionId": session_id,
                    "drift": drift,
                    "businessState": "failed",
                    "sessionState": "failed",
                    "modelRevision": session["modelRevision"],
                },
            )

        problems = validate_plan(plan, current_snapshot, session_id=session_id)
        target_already_met = self._all_targets_met(plan, before_model)
        if problems and target_already_met:
            problems = [p for p in problems if p["code"] not in ("STALE_PLAN", "STALE_REVISION")]
        if problems:
            # Static rejection happens before any write. It is a result state,
            # not a session state: the session keeps whatever status it had so a
            # corrected plan can still run.
            result = self._result(
                session,
                plan,
                verdict="REJECTED",
                code=problems[0]["code"],
                message="; ".join(p["message"] for p in problems),
                modified=False,
                before=before_model,
                after=before_model,
                steps=[],
                checks=[
                    {
                        "id": f"reject:{problem['code']}",
                        "status": "fail",
                        "evidence": {"path": problem["path"], "message": problem["message"]},
                    }
                    for problem in problems
                ],
                business_state="rejected",
            )
            result["data"]["problems"] = problems
            # No write happened: there is no after-state. Leaving one would make
            # the delivery report treat this rejected attempt as the request that
            # produced the delivered model.
            result["data"]["after"] = None
            self._record_result(session, result, plan=plan)
            self.journal.upsert_operation(
                operation_id,
                session_id,
                digest,
                "rejected",
                {
                    "plan": plan,
                    "intent": plan.get("intent"),
                    "before": baseline,
                    "result": result,
                },
            )
            self.journal.append_event(
                "rejected",
                {"operationId": operation_id, "problems": problems},
                session_id=session_id,
                operation_id=operation_id,
            )
            return result

        if target_already_met:
            verdict, checks, notes = self._evaluate(
                plan, before_model, before_model, session, expect_no_change=True
            )
            if verdict == "PASS":
                verdict = "ALREADY_SATISFIED"
            ok = verdict == "ALREADY_SATISFIED"
            session["status"] = "verified" if ok else "failed"
            session["snapshot"] = self._snapshot_from(session, before_model)
            result = self._result(
                session,
                plan,
                verdict=verdict,
                code=verdict,
                message=(
                    "every requested value is already in place; no write was performed"
                    if ok
                    else "; ".join(notes) or "the current model does not satisfy the plan"
                ),
                modified=False,
                before=before_model,
                after=before_model,
                steps=[],
                checks=checks,
            )
            self._record_result(session, result, plan=plan)
            self.journal.upsert_operation(
                operation_id,
                session_id,
                digest,
                "verified" if ok else "failed",
                {
                    "plan": plan,
                    "intent": plan.get("intent"),
                    "before": baseline,
                    "result": result,
                },
            )
            return result

        session["status"] = "executing"
        self._save(session)
        self.journal.upsert_operation(
            operation_id,
            session_id,
            digest,
            "intent",
            {
                "plan": plan,
                "intent": plan.get("intent"),
                "before": baseline,
                "revision": session["modelRevision"],
            },
        )
        self.journal.append_event(
            "intent", {"operationId": operation_id, "changes": plan.get("changes")}, session_id=session_id,
            operation_id=operation_id,
        )

        steps: list[dict[str, Any]] = []
        failure: Exception | None = None
        try:
            for index, change in enumerate(plan["changes"], start=1):
                handle = change["parameter"]
                param = get_param(doc.Part, handle)
                unit = unit_of(param)
                old_value = float(param.Value)
                assign_value(param, float(change["newValue"]), unit if unit else "none")
                part_update_ok = False
                _update_sketches(doc.Part)
                doc.Part.Update()
                part_update_ok = True
                readback = float(param.Value)
                steps.append(
                    {
                        "index": index,
                        "parameter": handle,
                        "oldValue": old_value,
                        "newValue": float(change["newValue"]),
                        "unit": unit or "none",
                        "readback": readback,
                        "status": "applied" if part_update_ok else "failed",
                    }
                )
                self.journal.append_event(
                    "step", steps[-1], session_id=session_id, operation_id=operation_id
                )
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed silently
            failure = exc

        after_model = self._fresh_model(session)
        if failure is not None:
            recovery = self._bounded_rollback(doc, plan, baseline, operation_id, session_id)
            restored = bool(recovery.get("restored"))
            if restored:
                # Prove the state the caller is actually left with; data.after must
                # not be the partially written model when rollback succeeded.
                try:
                    after_model = self._fresh_model(session)
                except Exception:
                    after_model = before_model
            code = "EXECUTION_FAILED" if restored else "RECOVERY_UNPROVEN"
            session["status"] = "rolled_back" if restored else "unknown"
            if restored:
                session["snapshot"] = self._snapshot_from(session, after_model)
            else:
                session["modelRevision"] = int(session["modelRevision"]) + 1
                session["snapshot"] = self._snapshot_from(session, after_model)
            result = self._result(
                session,
                plan,
                verdict="INCONCLUSIVE",
                code=code,
                message=(
                    f"execution failed: {failure}; "
                    + (
                        "changes were rolled back and the rollback is proven by readback"
                        if restored
                        else "rollback could not be proven, the model state is unknown"
                    )
                ),
                modified=not restored,
                before=before_model,
                after=after_model,
                steps=steps,
                checks=[{"id": "execution", "status": "fail", "evidence": {"error": str(failure)}}],
                recovery=recovery,
                business_state="failed" if restored else "unknown",
            )
            self._record_result(session, result, plan=plan)
            self.journal.upsert_operation(
                operation_id,
                session_id,
                digest,
                "failed",
                {
                    "plan": plan,
                    "intent": plan.get("intent"),
                    "before": baseline,
                    "result": result,
                },
            )
            return result

        verdict, checks, notes = self._evaluate(plan, before_model, after_model, session)
        ok = verdict in ("PASS", "ALREADY_SATISFIED")
        session["modelRevision"] = int(session["modelRevision"]) + 1
        session["status"] = "verified" if ok else "failed"
        session["snapshot"] = self._snapshot_from(session, after_model)
        result = self._result(
            session,
            plan,
            verdict=verdict,
            code="VERIFIED" if ok else verdict,
            message=(
                "all checks passed"
                if ok
                else "; ".join(notes) or "verification did not meet the plan"
            ),
            modified=True,
            before=before_model,
            after=after_model,
            steps=steps,
            checks=checks,
        )
        self._record_result(session, result, plan=plan)
        self.journal.upsert_operation(
            operation_id,
            session_id,
            digest,
            "verified" if ok else "failed",
            {
                "plan": plan,
                "intent": plan.get("intent"),
                "before": baseline,
                "result": result,
            },
        )
        return result

    # ------------------------------------------------------------------
    # finalize
    # ------------------------------------------------------------------
    def finalize_edit(self, session_id: str, delivery_path: str) -> dict[str, Any]:
        try:
            session = self._load(session_id)
        except LookupError:
            return _envelope(False, "NOT_FOUND", f"unknown edit session {session_id}", modified=None, data={})
        if session["status"] != "verified":
            return _envelope(
                False,
                "INVALID_PLAN",
                f"session must be verified before saving (current: {session['status']})",
                modified=None,
                data={"editSessionId": session_id, "businessState": session["status"]},
            )
        if not delivery_path or not os.path.isabs(delivery_path):
            return _envelope(False, "INVALID_PLAN", "deliveryPath must be absolute", modified=False, data={})
        # CATIA SaveAs fails with a bare E_FAIL when the path uses forward
        # slashes on Windows. Normalize before the conflict checks and SaveAs
        # so callers may use either separator.
        delivery_path = os.path.normpath(delivery_path)
        if os.path.exists(delivery_path):
            return _envelope(
                False, "SAVE_CONFLICT", f"delivery path already exists: {delivery_path}", modified=False, data={}
            )
        try:
            open_document = resolve_document(self.conn.app, delivery_path)
        except RuntimeError as exc:
            return _envelope(
                False,
                "BUSY",
                f"cannot prove whether the delivery path is already open ({exc}); retry after the "
                f"document scan succeeds",
                modified=False,
                data={"deliveryPath": delivery_path},
            )
        conflict = delivery_conflict(
            delivery_path, None if open_document is None else str(getattr(open_document, "Name", "document"))
        )
        if conflict is not None:
            return conflict
        try:
            doc = self._doc(session)
        except RuntimeError as exc:
            return _envelope(False, "DOCUMENT_CLOSED", str(exc), modified=None, data={"editSessionId": session_id})
        # Measured before SaveAs and again after it (see below): the report must
        # carry a measured sourceUnchanged value, never a default.
        source_sha_before = sha256_file(session["source"]["path"])
        if source_sha_before != session["source"]["sha256"]:
            self.journal.append_event("source_changed", {"path": session["source"]["path"]}, session_id=session_id)
            return _envelope(
                False,
                "EXTERNAL_CHANGE",
                "the source file changed during the session; delivery is blocked",
                modified=None,
                data={"editSessionId": session_id},
            )
        session["status"] = "saving"
        self._save(session)
        expected = self._fresh_model(session)
        drift = self._drift(session.get("snapshot") or {}, expected)
        if drift:
            session["modelRevision"] = int(session["modelRevision"]) + 1
            session["status"] = "failed"
            session["snapshot"] = self._snapshot_from(session, expected)
            self._save(session)
            self.journal.append_event("external_change_before_save", {"drift": drift}, session_id=session_id)
            return _envelope(
                False,
                "EXTERNAL_CHANGE",
                "the model changed after the last verification; saving would deliver an unverified model",
                modified=None,
                data={
                    "editSessionId": session_id,
                    "drift": drift,
                    "businessState": "failed",
                    "sessionState": "failed",
                },
            )
        app = self.conn.app
        try:
            os.makedirs(os.path.dirname(delivery_path), exist_ok=True)
            app.DisplayFileAlerts = False
            doc.SaveAs(delivery_path)
        except Exception as exc:
            session["status"] = "verified"
            self._save(session)
            return _envelope(
                False,
                "SAVE_FAILED",
                f"the model is verified but saving failed: {exc}",
                modified=True,
                data={"editSessionId": session_id, "businessState": "verified"},
            )
        finally:
            try:
                app.DisplayFileAlerts = True
            except Exception:
                pass
        try:
            delivery_sha = sha256_file(delivery_path)
            delivered_bytes = os.path.getsize(delivery_path)
        except Exception as exc:
            session["status"] = "unknown"
            self._save(session)
            return _envelope(
                False,
                "SAVE_FAILED",
                f"CATIA reported a save but the delivery file is unreadable: {exc}",
                modified=True,
                data={"editSessionId": session_id},
            )
        if norm(str(doc.FullName)) != norm(delivery_path):
            session["status"] = "unknown"
            self._save(session)
            return _envelope(
                False,
                "SAVE_FAILED",
                f"CATIA saved to '{doc.FullName}' instead of '{delivery_path}'",
                modified=True,
                data={"editSessionId": session_id},
            )
        close_error = None
        try:
            doc.Close()
        except Exception as exc:
            close_error = str(exc)
        try:
            still_open = resolve_document(app, delivery_path) is not None
        except RuntimeError:
            still_open = True  # cannot prove it closed: do not claim a fresh disk load
        verification = self._verify_delivery(delivery_path, expected, session, still_open=still_open)
        if close_error:
            verification["closeError"] = close_error
        # The source must not move while we deliver. Measure it again after SaveAs
        # and carry the measured answer into the report instead of defaulting it.
        try:
            source_sha_after = sha256_file(session["source"]["path"])
            source_sha_error = None
        except Exception as exc:  # noqa: BLE001 - an unreadable source is not proof of equality
            source_sha_after = None
            source_sha_error = f"{type(exc).__name__}: {exc}"
        source_unchanged = (
            source_sha_before is not None
            and source_sha_after is not None
            and source_sha_before == source_sha_after
        )
        verification["sourceUnchanged"] = source_unchanged
        verification["sourceSha256Before"] = source_sha_before
        verification["sourceSha256After"] = source_sha_after
        if source_sha_error:
            verification["sourceShaError"] = source_sha_error
        verification.setdefault("checks", []).append(
            {
                "id": "source-unchanged",
                "status": "pass" if source_unchanged else "fail",
                "evidence": {
                    "before": source_sha_before,
                    "after": source_sha_after,
                    "error": source_sha_error,
                },
            }
        )
        verification["passed"] = bool(verification.get("passed")) and source_unchanged
        try:
            report_paths = report_mod.write_reports(
                session,
                delivery_path,
                delivery_sha,
                delivered_bytes,
                expected,
                verification,
                journal=self.journal,
            )
            report_error = None
        except Exception as exc:  # noqa: BLE001 - the delivered file already exists
            report_paths = {"json": None, "markdown": None}
            report_error = f"{type(exc).__name__}: {exc}"
        verified = bool(verification["passed"])
        ok = verified and report_error is None
        session["status"] = "completed" if ok else "unknown"
        session["delivery"] = {
            "path": delivery_path,
            "sha256": delivery_sha,
            "sizeBytes": delivered_bytes,
            "reportJson": report_paths["json"],
            "reportMarkdown": report_paths["markdown"],
            "reportError": report_error,
        }
        if not verified:
            code, message = "VERIFY_FAILED", "saved, but the reopened file failed verification"
        elif report_error is not None:
            code, message = (
                "REPORT_FAILED",
                "the delivery file was saved and re-verified from disk, but the report could "
                f"not be written ({report_error}); the session stays unknown until the report exists",
            )
        else:
            code, message = "COMPLETED", "saved and re-verified from disk"
        result = _envelope(
            ok,
            code,
            message,
            modified=True,
            data={
                "editSessionId": session_id,
                "businessState": session["status"],
                "sessionState": session["status"],
                "modelRevision": session["modelRevision"],
                "delivery": session["delivery"],
                "verification": verification,
            },
        )
        self._record_result(session, result)
        self._save(session)
        self.journal.append_event(
            "finalize", {"delivery": session["delivery"], "passed": ok}, session_id=session_id
        )
        return result

    def _verify_delivery(
        self,
        delivery_path: str,
        expected: dict[str, Any],
        session: dict[str, Any],
        *,
        still_open: bool = False,
    ) -> dict[str, Any]:
        app = self.conn.app
        checks: list[dict[str, Any]] = []
        app.DisplayFileAlerts = False
        doc = None
        reopened = False
        model: dict[str, Any] | None = None
        reopen_error: Exception | None = None
        try:
            before_count = app.Documents.Count
            doc = app.Documents.Open(delivery_path)
            reopened = app.Documents.Count > before_count and not still_open
        except Exception as exc:  # noqa: BLE001 - COM raises bare Exception
            reopen_error = exc
        finally:
            try:
                app.DisplayFileAlerts = True
            except Exception:
                pass
        if reopen_error is not None:
            # A document can be open even when the count read or a later COM call
            # raised; never leave it holding the delivery path (踩坑 69).
            checks.append(
                {"id": "reopen", "status": "fail", "evidence": {"error": str(reopen_error)}}
            )
            if doc is not None:
                checks.append(close_document_check(doc))
            return {"passed": False, "checks": checks, "model": None}
        checks.append({"id": "reopen", "status": "pass" if reopened else "warn", "evidence": {"documentCountDelta": reopened}})
        try:
            model = read_model(app, doc, doc.Part)
        except Exception as exc:  # noqa: BLE001 - close what we opened before failing
            checks.append({"id": "readback", "status": "fail", "evidence": {"error": str(exc)}})
            if doc is not None:
                checks.append(close_document_check(doc))
            return {"passed": False, "checks": checks, "model": None}
        volume_expected = expected["geometry"].get("volumeMm3")
        volume_actual = model["geometry"].get("volumeMm3")
        checks.append(
            {
                "id": "volume",
                "status": "pass"
                if volume_expected is not None
                and volume_actual is not None
                and approx_equal(volume_actual, volume_expected, rel=1e-6, abs_tol=0.01)
                else "fail",
                "evidence": {"expected": volume_expected, "actual": volume_actual},
            }
        )
        bbox_expected = expected["geometry"].get("bbox", {})
        bbox_actual = model["geometry"].get("bbox", {})
        same_bbox = (
            "dimensions" in bbox_expected
            and "dimensions" in bbox_actual
            and all(
                approx_equal(bbox_actual["dimensions"][axis], bbox_expected["dimensions"][axis], abs_tol=1e-6)
                for axis in ("x", "y", "z")
            )
        )
        checks.append(
            {
                "id": "bbox",
                "status": "pass" if same_bbox else "fail",
                "evidence": {"expected": bbox_expected.get("dimensions"), "actual": bbox_actual.get("dimensions")},
            }
        )
        holes_expected = expected["holes"]
        holes_actual = model["holes"]
        same_holes = len(holes_expected) == len(holes_actual) and all(
            approx_equal(a["center"]["x"], b["center"]["x"], abs_tol=1e-6)
            and approx_equal(a["center"]["y"], b["center"]["y"], abs_tol=1e-6)
            and approx_equal(a["radius"], b["radius"], abs_tol=1e-6)
            for a, b in zip(holes_expected, holes_actual)
        )
        checks.append(
            {
                "id": "holes",
                "status": "pass" if same_holes else "fail",
                "evidence": {"expected": holes_expected, "actual": holes_actual},
            }
        )
        expected_params = {p["handle"]: p.get("value") for p in expected["parameters"] if p.get("kind") == "driver"}
        actual_params = {p["handle"]: p.get("value") for p in model["parameters"] if p.get("kind") == "driver"}
        diff = [
            {"handle": h, "expected": v, "actual": actual_params.get(h)}
            for h, v in expected_params.items()
            if not isinstance(v, (int, float)) or not approx_equal(float(actual_params.get(h, 0.0)), float(v), abs_tol=1e-6)
        ]
        checks.append({"id": "drivers", "status": "pass" if not diff else "fail", "evidence": {"differences": diff}})
        # Close the reopened document and report the outcome: a document left open here keeps
        # holding the delivery path, so the next SaveAs onto the same path fails with a bare
        # E_FAIL that looks like a permission error (踩坑 69).
        if doc is not None:
            checks.append(close_document_check(doc))
        passed = all(c["status"] == "pass" for c in checks)
        return {"passed": passed, "checks": checks, "model": model}

    # ------------------------------------------------------------------
    # recover
    # ------------------------------------------------------------------
    def recover_edit(self, session_id: str, dry_run: bool = False) -> dict[str, Any]:
        try:
            session = self._load(session_id)
        except LookupError:
            return _envelope(False, "NOT_FOUND", f"unknown edit session {session_id}", modified=None, data={})
        pending = self.journal.operations_in_state(
            session_id, ("intent", "applied", "failed", "unknown")
        )
        if not pending:
            if session["status"] == "saving":
                # SaveAs is not journaled as an operation, so a crashed save cannot
                # be declared clean: the delivery path may hold a partial file.
                return _envelope(
                    False,
                    "UNKNOWN_STATE",
                    "the session was saving when it stopped; verify the delivery path by hand, "
                    "then start a new edit session",
                    modified=None,
                    data={"editSessionId": session_id, "businessState": "saving"},
                )
            # No intent row means no apply of ours can be in flight: the state can
            # be settled without writing.
            if session["status"] in ("executing", "unknown", "recovering"):
                if dry_run:
                    return _envelope(
                        True,
                        "NOTHING_TO_RECOVER",
                        "no operation is in flight; a real recover would reset the session to failed",
                        modified=None,
                        data={
                            "editSessionId": session_id,
                            "businessState": session["status"],
                            "sessionState": session["status"],
                            "wouldResetTo": "failed",
                        },
                    )
                session["status"] = "failed"
                self._save(session)
                return _envelope(
                    True,
                    "RECONCILED_CLEAN",
                    "no operation was in flight; the session was reset to failed",
                    modified=False,
                    data={
                        "editSessionId": session_id,
                        "businessState": "failed",
                        "sessionState": "failed",
                    },
                )
            if session["status"] in ("failed", "rolled_back"):
                # A settled failure keeps holding the working-copy document, which
                # blocks delivery and generic writers. Close it and abandon the
                # session (no model writes) so exclusivity is released. A verified
                # session is never closed here: it may still be finalized.
                if dry_run:
                    return _envelope(
                        True,
                        "NOTHING_TO_RECOVER",
                        "no operation is pending; a real recover would close the working copy "
                        "and abandon this session",
                        modified=None,
                        data={
                            "editSessionId": session_id,
                            "businessState": session["status"],
                            "sessionState": session["status"],
                            "wouldAbandon": True,
                        },
                    )
                close_info: dict[str, Any] = {"closed": False, "error": None}
                doc = None
                if self.conn is not None:
                    try:
                        doc = resolve_document(self.conn.app, session["workingCopy"]["path"])
                    except RuntimeError as exc:
                        return _envelope(
                            False,
                            "BUSY",
                            f"cannot prove whether the working copy is open ({exc}); retry "
                            f"after the document scan succeeds",
                            modified=None,
                            data={
                                "editSessionId": session_id,
                                "businessState": session["status"],
                                "sessionState": session["status"],
                            },
                        )
                    except Exception as exc:  # noqa: BLE001 - "could not look" is not "not open"
                        return _envelope(
                            False,
                            "BUSY",
                            f"cannot prove whether the working copy is open ({type(exc).__name__}: {exc}); "
                            f"retry after the document scan succeeds",
                            modified=None,
                            data={
                                "editSessionId": session_id,
                                "businessState": session["status"],
                                "sessionState": session["status"],
                            },
                        )
                if doc is None:
                    close_info["closed"] = True
                else:
                    app = self.conn.app
                    display_alerts = None
                    try:
                        display_alerts = app.DisplayFileAlerts
                        app.DisplayFileAlerts = False
                    except Exception:
                        display_alerts = None
                    try:
                        doc.Close()
                        close_info["closed"] = True
                    except Exception as exc:  # noqa: BLE001 - reported in data
                        close_info["error"] = str(exc)
                    finally:
                        if display_alerts is not None:
                            try:
                                app.DisplayFileAlerts = display_alerts
                            except Exception:
                                pass
                if not close_info["closed"]:
                    # The document is still open. Abandoning now would release the
                    # write lock while a possibly modified working copy is alive.
                    return _envelope(
                        False,
                        "BUSY",
                        f"the working copy could not be closed ({close_info['error']}); "
                        f"close it manually, then retry catia_recover_edit",
                        modified=None,
                        data={
                            "editSessionId": session_id,
                            "businessState": session["status"],
                            "sessionState": session["status"],
                            "close": close_info,
                        },
                    )
                session["status"] = "abandoned"
                self._save(session)
                self.journal.append_event(
                    "session_abandoned",
                    {"reason": "recover closed the working copy", **close_info},
                    session_id=session_id,
                )
                return _envelope(
                    True,
                    "RECONCILED_CLEAN",
                    "no operation is pending; the working copy was closed and the session "
                    "abandoned without any model write",
                    modified=False,
                    data={
                        "editSessionId": session_id,
                        "businessState": "abandoned",
                        "sessionState": "abandoned",
                        "close": close_info,
                    },
                )
            return _envelope(
                True,
                "NOTHING_TO_RECOVER",
                "no operation needs reconciliation",
                modified=None,
                data={
                    "editSessionId": session_id,
                    "businessState": session["status"],
                    "sessionState": session["status"],
                },
            )
        try:
            doc = self._doc(session)
        except RuntimeError as exc:
            return _envelope(False, "DOCUMENT_CLOSED", str(exc), modified=None, data={"editSessionId": session_id})
        model = read_model(self.conn.app, doc, doc.Part)
        assessments = []
        baselines: dict[str, dict[str, Any]] = {}
        for op in pending:
            # Older journals stored a raw model here (parameters is a list);
            # normalize before any comparison so list.get never happens.
            baseline = self._baseline_from_payload(op["payload"].get("before", {}))
            baselines[op["operation_id"]] = baseline
            assessments.append(self._assess(op, baseline, model))
        response: dict[str, Any] = {
            "editSessionId": session_id,
            "businessState": session["status"],
            "sessionState": session["status"],
            "dryRun": bool(dry_run),
            "assessments": assessments,
        }
        if dry_run:
            return _envelope(True, "ASSESSED", "reconciliation assessed without writing", modified=None, data=response)
        session["status"] = "recovering"
        self._save(session)
        restorations = []
        # Newest operation first: each rollback writes that operation's own before
        # values, so undoing an older change last would resurrect it.
        for op, assessment in reversed(list(zip(pending, assessments))):
            if assessment["state"] == "not_applied":
                restorations.append({"operationId": op["operation_id"], "action": "none", "proven": True})
                self.journal.upsert_operation(
                    op["operation_id"], session_id, op["plan_hash"], "recovered", op["payload"]
                )
                continue
            plan = op["payload"].get("plan") or {}
            baseline = baselines[op["operation_id"]]
            recovery = self._bounded_rollback(
                doc, plan, baseline, op["operation_id"], session_id
            )
            restorations.append(
                {"operationId": op["operation_id"], "action": "rollback", **recovery}
            )
            state = "recovered" if recovery.get("restored") else "unknown"
            self.journal.upsert_operation(op["operation_id"], session_id, op["plan_hash"], state, op["payload"])
            model = read_model(self.conn.app, doc, doc.Part)
        proven = all(r.get("proven", r.get("restored", False)) for r in restorations)
        session["status"] = "rolled_back" if proven else "unknown"
        session["snapshot"] = self._snapshot_from(session, model)
        self._save(session)
        response["restorations"] = restorations
        # businessState must be the state after recovery, not the pre-recovery one.
        response["businessState"] = session["status"]
        response["sessionState"] = session["status"]
        self.journal.append_event("recover", response, session_id=session_id)
        return _envelope(
            proven,
            "ROLLED_BACK" if proven else "RECOVERY_UNPROVEN",
            "recovery proven by readback" if proven else "state remains unknown",
            modified=True,
            data=response,
        )

    def _assess(self, op: dict[str, Any], before: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
        changes = (op["payload"].get("plan") or {}).get("changes") or []
        baseline = self._baseline_from_payload(before)
        before_params = baseline.get("parameters") or {}
        comparable = [
            change
            for change in changes
            if _is_number(before_params.get(change.get("parameter")))
        ]

        def current(handle: Any) -> float | None:
            value = model_value(model, handle)
            return float(value) if _is_number(value) else None

        at_before = bool(comparable) and all(
            current(change["parameter"]) is not None
            and approx_equal(
                current(change["parameter"]),
                float(before_params[change["parameter"]]),
                rel=0.0,
                abs_tol=1e-6,
            )
            for change in comparable
        )
        at_target = bool(changes) and all(
            _is_number(change.get("newValue"))
            and current(change["parameter"]) is not None
            and approx_equal(
                current(change["parameter"]), float(change["newValue"]), rel=0.0, abs_tol=1e-6
            )
            for change in changes
        )
        if at_before:
            state = "not_applied"
        elif at_target:
            state = "applied"
        else:
            # Without baseline values there is no evidence either way; say so
            # instead of claiming "not applied".
            state = "partial_or_unknown"
        return {
            "operationId": op["operation_id"],
            "state": state,
            "beforeValues": before_params,
            "currentValues": {c["parameter"]: model_value(model, c["parameter"]) for c in changes},
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _snapshot_from(self, session: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
        base = dict(session.get("snapshot") or {})
        base.update(
            {
                "schema": "catia-model-snapshot/1",
                "editSessionId": session["editSessionId"],
                "modelRevision": session["modelRevision"],
                "source": session["source"],
                "workingCopy": session["workingCopy"],
                "parameters": self._decorate(model["parameters"], session.get("impacts", {})),
                "relations": model["relations"],
                "geometry": model["geometry"],
                "holes": model["holes"],
                "part": {
                    "name": model["part"],
                    "mainBody": model["features"]["mainBody"],
                    "featureCount": len(model["features"]["shapes"]),
                    "bodyNames": model["features"]["bodies"],
                },
                "drivers": [p["handle"] for p in model["parameters"] if p.get("kind") == "driver"],
                "capturedUtc": now(),
            }
        )
        base.setdefault("impacts", session.get("impacts", {}))
        base.setdefault("capabilities", {})
        base["limitations"] = model["limitations"]
        return base

    @staticmethod
    def _decorate(parameters: list[dict[str, Any]], impacts: dict[str, Any]) -> list[dict[str, Any]]:
        from catia_mcp.editing.session import _decorate_parameter

        return [_decorate_parameter(p, impacts) for p in parameters]

    @staticmethod
    def _drift(previous: dict[str, Any], model: dict[str, Any]) -> list[dict[str, Any]]:
        if not previous:
            return []
        drift = []
        previous_params = {p["handle"]: p.get("value") for p in previous.get("parameters", [])}
        current_handles = {p["handle"] for p in model["parameters"]}
        for handle in sorted(current_handles - set(previous_params)):
            drift.append({"parameter": handle, "change": "added"})
        for handle in sorted(set(previous_params) - current_handles):
            drift.append({"parameter": handle, "change": "removed"})
        for param in model["parameters"]:
            handle = param["handle"]
            if handle in previous_params and isinstance(previous_params[handle], (int, float)):
                current = param.get("value")
                if not isinstance(current, (int, float)):
                    drift.append({"parameter": handle, "change": "unreadable", "actual": current})
                    continue
                if not approx_equal(float(current), float(previous_params[handle]), abs_tol=1e-6):
                    drift.append({"parameter": handle, "expected": previous_params[handle], "actual": current})
        previous_volume = (previous.get("geometry") or {}).get("volumeMm3")
        model_volume = model["geometry"].get("volumeMm3")
        if previous_volume is not None and model_volume is not None and not approx_equal(
            model_volume, previous_volume, abs_tol=1e-6
        ):
            drift.append({"geometry": "volumeMm3", "expected": previous_volume, "actual": model_volume})
        previous_dims = ((previous.get("geometry") or {}).get("bbox") or {}).get("dimensions")
        model_dims = (model["geometry"].get("bbox") or {}).get("dimensions")
        if previous_dims and model_dims:
            for axis in ("x", "y", "z"):
                if not approx_equal(float(model_dims[axis]), float(previous_dims[axis]), abs_tol=1e-6):
                    drift.append(
                        {"geometry": f"bbox.{axis}", "expected": previous_dims[axis], "actual": model_dims[axis]}
                    )
        previous_holes = previous.get("holes")
        if previous_holes is not None:
            previous_sig = [
                (round(h["center"]["x"], 6), round(h["center"]["y"], 6), round(h["radius"], 6))
                for h in previous_holes
            ]
            model_sig = [
                (round(h["center"]["x"], 6), round(h["center"]["y"], 6), round(h["radius"], 6))
                for h in model.get("holes") or []
            ]
            if previous_sig != model_sig:
                drift.append({"geometry": "holes", "expected": previous_sig, "actual": model_sig})
        return drift

    @staticmethod
    def _all_targets_met(plan: dict[str, Any], model: dict[str, Any]) -> bool:
        changes = plan.get("changes")
        if not isinstance(changes, list) or not changes:
            return False
        for change in changes:
            if not isinstance(change, dict):
                return False
            target = change.get("newValue")
            current = model_value(model, change.get("parameter"))
            if not isinstance(target, (int, float)) or isinstance(target, bool) or current is None:
                return False
            if not approx_equal(float(current), float(target), abs_tol=1e-6):
                return False
        return True

    def _checks_for_already(self, plan: dict[str, Any], model: dict[str, Any]) -> list[dict[str, Any]]:
        checks = []
        for change in plan["changes"]:
            checks.append(
                {
                    "id": f"already-{change['parameter']}",
                    "status": "pass",
                    "evidence": {"current": model_value(model, change["parameter"]), "target": change["newValue"]},
                }
            )
        return checks

    def _evaluate(
        self,
        plan: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
        session: dict[str, Any],
        *,
        expect_no_change: bool = False,
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        checks: list[dict[str, Any]] = []
        notes: list[str] = []
        impacts = session.get("impacts", {})

        readback_ok = True
        for change in plan["changes"]:
            current = model_value(after, change["parameter"])
            ok = current is not None and approx_equal(float(current), float(change["newValue"]), abs_tol=1e-6)
            readback_ok = readback_ok and ok
            checks.append(
                {
                    "id": f"param-readback:{change['parameter']}",
                    "status": "pass" if ok else "fail",
                    "evidence": {"expected": change["newValue"], "actual": current},
                }
            )
        if not readback_ok:
            notes.append("parameter readback does not match the requested value")
            return "WRONG_EFFECT", checks, notes

        if not expect_no_change:
            observable = False
            changed_handles = {change["parameter"] for change in plan["changes"]}
            volume_available = (
                (before["geometry"].get("volumeMm3") is not None)
                and (after["geometry"].get("volumeMm3") is not None)
            )
            bbox_available = bool(
                ((before["geometry"] or {}).get("bbox") or {}).get("dimensions")
                and ((after["geometry"] or {}).get("bbox") or {}).get("dimensions")
            )
            if not volume_available and not bbox_available:
                checks.append(
                    {
                        "id": "effect:measurement",
                        "status": "warn",
                        "evidence": {"reason": "volume and bounding box are both unavailable"},
                    }
                )
                notes.append("no usable geometric measurement; the effect cannot be judged")
                return "INCONCLUSIVE", checks, notes
            for change in plan["changes"]:
                impact = impacts.get(change["parameter"], {})
                geometry_changed = volume_available and not approx_equal(
                    before["geometry"].get("volumeMm3") or 0.0,
                    after["geometry"].get("volumeMm3") or 0.0,
                    abs_tol=1e-6,
                )
                bbox_changed = self._bbox_changed(before, after)
                holes_moved = self._edge_signature(before) != self._edge_signature(after)
                # The probe maps include the perturbed parameter itself, so only
                # *other* parameters count as dependent evidence.
                dependent_changed = any(
                    not approx_equal(
                        float(model_value(before, handle) or 0.0),
                        float(model_value(after, handle) or 0.0),
                        abs_tol=1e-6,
                    )
                    for handle in (impact.get("affectedParameters") or [])
                    for handle in [handle.get("handle") if isinstance(handle, dict) else handle]
                    if handle and handle not in changed_handles
                )
                expected_effect = bool(impact.get("geometryChanged"))
                # In a multi-change plan a model-wide delta cannot be attributed to
                # one parameter, so such a change only counts when its own
                # dependent parameters moved.
                if len(plan["changes"]) == 1:
                    observed = geometry_changed or bbox_changed or holes_moved or dependent_changed
                else:
                    observed = dependent_changed
                checks.append(
                    {
                        "id": f"effect:{change['parameter']}",
                        "status": "pass" if (observed or not expected_effect) else "fail",
                        "evidence": {
                            "expectedGeometryChange": expected_effect,
                            "volumeBefore": before["geometry"].get("volumeMm3"),
                            "volumeAfter": after["geometry"].get("volumeMm3"),
                            "bboxChanged": bbox_changed,
                            "holesMoved": holes_moved,
                            "dependentParametersChanged": dependent_changed,
                            "attribution": "single-change" if len(plan["changes"]) == 1 else "dependent-parameters-only",
                        },
                    }
                )
                observable = observable or observed
            if not observable:
                notes.append("the requested parameters changed but nothing measurable moved (NO_EFFECT)")
                return "NO_EFFECT", checks, notes

        sanity, inconclusive = self._sanity_checks(before, after)
        checks.extend(sanity)
        for item in sanity:
            if item["status"] == "fail":
                notes.append(f"{item['id']} failed")

        for index, item in enumerate(plan.get("preserve") or []):
            if item.get("type") == "holes_edge_distance":
                scoped = item.get("holes") if isinstance(item.get("holes"), list) else None
                before_sig = self._edge_signature_for(before, scoped)
                after_sig = self._edge_signature_for(after, scoped)
                deviation = None
                if before_sig and after_sig and len(before_sig) == len(after_sig):
                    deviation = max(
                        abs(a - b)
                        for row_a, row_b in zip(before_sig, after_sig)
                        for a, b in zip(row_a, row_b)
                    )
                if deviation is None:
                    inconclusive = True
                    status = "warn"
                else:
                    status = "pass" if deviation <= 0.01 else "fail"
                checks.append(
                    {
                        "id": f"preserve-{index}:holes_edge_distance",
                        "status": status,
                        "evidence": {
                            "before": before_sig,
                            "after": after_sig,
                            "maxDeviationMm": deviation,
                        },
                    }
                )
                if status == "fail":
                    notes.append("hole edge distance was not preserved")
            elif item.get("type") == "hole_world_position":
                before_holes = _measured_holes(before)
                after_holes = _measured_holes(after)
                expected = item.get("holes") or []
                deviation = None
                if (
                    len(before_holes) == len(after_holes) == len(expected)
                    and all(isinstance(h.get("centerWorld"), dict) for h in after_holes)
                ):
                    deviation = max(
                        abs(float(h["centerWorld"][axis]) - float(e.get(axis, h["centerWorld"][axis])))
                        for h, e in zip(after_holes, expected)
                        for axis in ("x", "y", "z")
                    )
                if deviation is None:
                    inconclusive = True
                    status = "warn"
                else:
                    status = "pass" if deviation <= 0.01 else "fail"
                checks.append(
                    {
                        "id": f"preserve-{index}:hole_world_position",
                        "status": status,
                        "evidence": {
                            "expected": expected,
                            "after": [h.get("centerWorld") for h in after_holes],
                            "maxDeviationMm": deviation,
                        },
                    }
                )
                if status == "fail":
                    notes.append("hole world positions were not preserved")

        for index, item in enumerate(plan.get("acceptance") or []):
            try:
                status, evidence = self._check_acceptance(item, after)
            except Exception as exc:  # noqa: BLE001 - an unusable assertion is not a pass
                status, evidence = "unavailable", {"error": f"{type(exc).__name__}: {exc}"}
            if status == "unavailable":
                inconclusive = True
            checks.append({"id": f"acceptance-{index}:{item.get('type')}", "status": status, "evidence": evidence})
            if status == "fail":
                notes.append(f"acceptance {index} ({item.get('type')}) failed")

        if any(c["status"] == "fail" for c in checks):
            return "WRONG_EFFECT", checks, notes
        if inconclusive:
            return "INCONCLUSIVE", checks, notes
        return "PASS", checks, notes

    def _sanity_checks(
        self, before: dict[str, Any], after: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], bool]:
        """Generic invariants that need no plan data: the model must stay one solid."""
        checks: list[dict[str, Any]] = []
        inconclusive = False
        before_holes = before.get("holes") or []
        after_holes = after.get("holes") or []
        checks.append(
            {
                "id": "sanity:hole-count",
                "status": "pass" if len(before_holes) == len(after_holes) else "fail",
                "evidence": {"before": len(before_holes), "after": len(after_holes)},
            }
        )
        bbox = (after.get("geometry") or {}).get("bbox") or {}
        if not after_holes:
            # No holes is a valid state, not missing evidence.
            checks.append(
                {
                    "id": "sanity:holes-inside",
                    "status": "pass",
                    "evidence": {"reason": "model has no hole geometry"},
                }
            )
        elif "min" in bbox:
            outside = []
            for hole in after_holes:
                x, y = float(hole["center"]["x"]), float(hole["center"]["y"])
                margin = min(
                    x - float(bbox["min"]["x"]),
                    float(bbox["max"]["x"]) - x,
                    y - float(bbox["min"]["y"]),
                    float(bbox["max"]["y"]) - y,
                )
                if margin <= 0:
                    outside.append({"hole": hole, "marginMm": round(margin, 6)})
            checks.append(
                {
                    "id": "sanity:holes-inside",
                    "status": "pass" if not outside else "fail",
                    "evidence": {"outside": outside},
                }
            )
        else:
            inconclusive = True
            checks.append(
                {
                    "id": "sanity:holes-inside",
                    "status": "warn",
                    "evidence": {"reason": "bounding box unavailable", "holes": len(after_holes)},
                }
            )
        before_lumps = ((before.get("geometry") or {}).get("bbox") or {}).get("lumpCount")
        after_lumps = ((after.get("geometry") or {}).get("bbox") or {}).get("lumpCount")
        if before_lumps is None or after_lumps is None:
            # Without an STL tessellation the lump guard cannot run. It is a
            # supplementary invariant, so a skipped guard is recorded but does
            # not by itself make an otherwise verified edit inconclusive.
            checks.append(
                {
                    "id": "sanity:lump-count",
                    "status": "warn",
                    "evidence": {
                        "reason": "STL tessellation unavailable",
                        "before": before_lumps,
                        "after": after_lumps,
                    },
                }
            )
        else:
            # An edit must not split one solid into several: the circles leaving
            # the rectangle get padded as extra cylinders and the bbox grows to
            # cover them, so holes-inside cannot see it (see _lump_count).
            # Only an increase is a defect. A decrease is how an earlier split
            # is repaired (A12 pushes E back inside the plate), and the volume
            # and bbox checks still have to agree for that edit to pass.
            split = after_lumps > before_lumps
            checks.append(
                {
                    "id": "sanity:lump-count",
                    "status": "fail" if split else "pass",
                    "evidence": {
                        "before": before_lumps,
                        "after": after_lumps,
                        "direction": "split" if split else ("merged" if after_lumps < before_lumps else "unchanged"),
                    },
                }
            )
        before_shapes = len((before.get("features") or {}).get("shapes") or [])
        after_shapes = len((after.get("features") or {}).get("shapes") or [])
        checks.append(
            {
                "id": "sanity:feature-count",
                "status": "pass" if before_shapes == after_shapes else "fail",
                "evidence": {"before": before_shapes, "after": after_shapes},
            }
        )
        before_volume = (before.get("geometry") or {}).get("volumeMm3")
        after_volume = (after.get("geometry") or {}).get("volumeMm3")
        positive_ok = True
        if isinstance(before_volume, float) and before_volume > 0 and after_volume is not None:
            positive_ok = float(after_volume) > 0
        checks.append(
            {
                "id": "sanity:volume-positive",
                "status": "pass" if positive_ok else "fail",
                "evidence": {"before": before_volume, "after": after_volume},
            }
        )
        return checks, inconclusive

    def _check_acceptance(self, item: dict[str, Any], model: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        from catia_mcp.editing.validation import acceptance_tolerance

        kind = item.get("type")
        tol = acceptance_tolerance(item)
        geometry = model["geometry"]
        if kind == "volume":
            actual = geometry.get("volumeMm3")
            if actual is None:
                return "unavailable", {"reason": "volume measurement unavailable"}
            expected = float(item["expected"])
            ok = approx_equal(actual, expected, rel=float(item.get("relTolerance", 1e-6)), abs_tol=max(tol, expected * 1e-6))
            return ("pass" if ok else "fail"), {"expected": expected, "actual": actual}
        if kind == "bbox_range":
            bbox = geometry.get("bbox") or {}
            axis = item["axis"]
            if "min" not in bbox:
                return "unavailable", {"reason": "bounding box unavailable"}
            actual_min = bbox["min"][axis]
            actual_max = bbox["max"][axis]
            ok = abs(actual_min - float(item["min"])) <= tol and abs(actual_max - float(item["max"])) <= tol
            return ("pass" if ok else "fail"), {
                "axis": axis,
                "expected": [item["min"], item["max"]],
                "actual": [actual_min, actual_max],
            }
        if kind == "bbox_dimension":
            bbox = geometry.get("bbox") or {}
            axis = item["axis"]
            if "dimensions" not in bbox:
                return "unavailable", {"reason": "bounding box unavailable"}
            actual = bbox["dimensions"][axis]
            ok = abs(actual - float(item["expected"])) <= tol
            return ("pass" if ok else "fail"), {"axis": axis, "expected": item["expected"], "actual": actual}
        if kind == "hole_positions":
            holes = _measured_holes(model)
            expected = item.get("holes") or []
            if not holes:
                return "unavailable", {"reason": "hole readback unavailable"}
            if len(expected) != len(holes):
                return "fail", {"expectedCount": len(expected), "actualCount": len(holes)}
            ok = all(
                abs(h["center"]["x"] - float(e.get("x", h["center"]["x"]))) <= tol
                and abs(h["center"]["y"] - float(e.get("y", h["center"]["y"]))) <= tol
                for h, e in zip(holes, expected)
            )
            return ("pass" if ok else "fail"), {"expected": expected, "actual": holes}
        if kind == "hole_world_positions":
            holes = _measured_holes(model)
            expected = item.get("holes") or []
            if not holes:
                return "unavailable", {"reason": "hole readback unavailable"}
            if any(not isinstance(h.get("centerWorld"), dict) for h in holes):
                return "unavailable", {"reason": "world coordinates unavailable for at least one hole"}
            if len(expected) != len(holes):
                return "fail", {"expectedCount": len(expected), "actualCount": len(holes)}
            ok = all(
                all(
                    abs(h["centerWorld"][axis] - float(e.get(axis, h["centerWorld"][axis]))) <= tol
                    for axis in ("x", "y", "z")
                )
                for h, e in zip(holes, expected)
            )
            return ("pass" if ok else "fail"), {"expected": expected, "actual": [h["centerWorld"] for h in holes]}
        if kind == "hole_radius":
            parameter = item.get("parameter")
            if isinstance(parameter, str):
                # A diameter/radius driver changes only the circles it drives;
                # another hole group (or an additive boss) is not part of this
                # assertion.
                holes = [
                    hole
                    for hole in model.get("holes") or []
                    if parameter in (hole.get("radiusDrivers") or [])
                ]
            else:
                holes = _measured_holes(model)
            if not holes:
                return "unavailable", {"reason": "no circle is measured as driven by this parameter"}
            expected = float(item["expected"])
            ok = all(abs(h["radius"] - expected) <= tol for h in holes)
            return ("pass" if ok else "fail"), {
                "expected": expected,
                "parameter": parameter,
                "actual": [h["radius"] for h in holes],
            }
        if kind == "unchanged_dimension":
            current = model_value(model, item["parameter"])
            if current is None:
                return "unavailable", {"reason": f"parameter {item['parameter']} missing"}
            expected = float(item["expected"])
            ok = abs(float(current) - expected) <= tol
            return ("pass" if ok else "fail"), {"parameter": item["parameter"], "expected": expected, "actual": current}
        if kind == "feature_count":
            actual = len(model["features"]["shapes"])
            ok = actual == int(item["expected"])
            return ("pass" if ok else "fail"), {"expected": item["expected"], "actual": actual}
        return "unavailable", {"reason": f"unsupported acceptance type {kind}"}

    @staticmethod
    def _bbox_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
        try:
            return any(
                abs(
                    float(((after.get("geometry") or {}).get("bbox") or {})["dimensions"][axis])
                    - float(((before.get("geometry") or {}).get("bbox") or {})["dimensions"][axis])
                )
                > 1e-6
                for axis in ("x", "y", "z")
            )
        except Exception:
            return False

    @staticmethod
    def _edge_signature(model: dict[str, Any]) -> list[list[float]]:
        bbox = (model.get("geometry") or {}).get("bbox") or {}
        try:
            x0, x1 = float(bbox["min"]["x"]), float(bbox["max"]["x"])
            y0, y1 = float(bbox["min"]["y"]), float(bbox["max"]["y"])
        except Exception:
            return []
        holes = model.get("holes") or []
        if any(hole.get("role") for hole in holes):
            holes = [hole for hole in holes if hole.get("role") == "hole"]
        out = []
        for hole in holes:
            x, y = float(hole["center"]["x"]), float(hole["center"]["y"])
            out.append([round(min(x - x0, x1 - x), 6), round(min(y - y0, y1 - y), 6)])
        return out

    @classmethod
    def _edge_signature_for(cls, model: dict[str, Any], indices: list[Any] | None) -> list[list[float]]:
        """Margins for a 1-based hole scope, or the global measured holes."""
        if not isinstance(indices, list) or not indices:
            return cls._edge_signature(model)
        bbox = (model.get("geometry") or {}).get("bbox") or {}
        try:
            x0, x1 = float(bbox["min"]["x"]), float(bbox["max"]["x"])
            y0, y1 = float(bbox["min"]["y"]), float(bbox["max"]["y"])
        except Exception:
            return []
        holes = model.get("holes") or []
        has_roles = any(hole.get("role") for hole in holes)
        out = []
        for value in indices:
            try:
                hole = holes[int(value) - 1]
            except Exception:
                continue
            if has_roles and hole.get("role") != "hole":
                continue
            x, y = float(hole["center"]["x"]), float(hole["center"]["y"])
            out.append([round(min(x - x0, x1 - x), 6), round(min(y - y0, y1 - y), 6)])
        return out

    @staticmethod
    def _baseline_from_model(model: dict[str, Any]) -> dict[str, Any]:
        """Normalize a ``read_model()`` result into the shared baseline shape.

        ``read_model()["parameters"]`` is a *list*; every persistence and
        rollback path must use this map instead of calling ``.get()`` on it.
        """
        parameters: dict[str, Any] = {}
        for param in model.get("parameters") or []:
            if not isinstance(param, dict):
                continue
            handle = param.get("handle")
            if isinstance(handle, str) and handle:
                # Keep every readable value; only numeric ones are compared by
                # the rollback (non-numeric system parameters are not writable).
                parameters[handle] = param.get("value")
        geometry = model.get("geometry") if isinstance(model.get("geometry"), dict) else {}
        volume = geometry.get("volumeMm3")
        if volume is None:
            volume = model.get("volumeMm3")
        bbox = geometry.get("bbox")
        if bbox is None:
            bbox = model.get("bbox")
        holes = model.get("holes")
        return {
            "parameters": parameters,
            "volumeMm3": volume if _is_number(volume) else None,
            "bbox": bbox if isinstance(bbox, dict) else None,
            "holes": holes if isinstance(holes, list) else None,
        }

    @staticmethod
    def _baseline_from_payload(data: Any) -> dict[str, Any]:
        """Accept either a model-shaped dict or an already-normalized baseline.

        Journals written by older builds stored a raw model (parameters list);
        the recover path must normalize those before comparing anything.
        """
        if not isinstance(data, dict):
            return {"parameters": {}, "volumeMm3": None, "bbox": None, "holes": None}
        parameters = data.get("parameters")
        if isinstance(parameters, list):
            return EditCoordinator._baseline_from_model(data)
        if not isinstance(parameters, dict):
            parameters = {}
        normalized = {
            handle: value
            for handle, value in parameters.items()
            if isinstance(handle, str) and handle
        }
        volume = data.get("volumeMm3")
        bbox = data.get("bbox")
        geometry = data.get("geometry")
        if isinstance(geometry, dict):
            if volume is None:
                volume = geometry.get("volumeMm3")
            if bbox is None:
                bbox = geometry.get("bbox")
        holes = data.get("holes")
        return {
            "parameters": normalized,
            "volumeMm3": volume if _is_number(volume) else None,
            "bbox": bbox if isinstance(bbox, dict) else None,
            "holes": holes if isinstance(holes, list) else None,
        }

    def _bounded_rollback(
        self,
        doc: Any,
        plan: dict[str, Any],
        baseline: dict[str, Any],
        operation_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Reverse the applied changes once, then prove the reversal by readback.

        ``baseline`` is the shared contract shape
        ``{"parameters": {handle: value}, "volumeMm3": ..., "bbox": ..., "holes": ...}``.
        ``restored`` is true only when every piece of *available* evidence
        (parameters, volume, bbox dimensions, holes) matches; evidence that the
        model cannot provide is listed in ``unavailable`` instead of being
        treated as a pass.
        """
        baseline = self._baseline_from_payload(baseline)
        recorded: list[dict[str, Any]] = []
        after: dict[str, Any] | None = None
        write_error: str | None = None
        try:
            for change in reversed(plan.get("changes") or []):
                handle = change["parameter"]
                before_value = (baseline.get("parameters") or {}).get(handle)
                if not _is_number(before_value):
                    continue
                param = get_param(doc.Part, handle)
                unit = unit_of(param)
                assign_value(param, float(before_value), unit)
                _update_sketches(doc.Part)
                doc.Part.Update()
                readback = float(param.Value)
                recorded.append(
                    {
                        "parameter": handle,
                        "restoredValue": before_value,
                        "readback": readback,
                        "matched": approx_equal(readback, float(before_value), rel=0.0, abs_tol=1e-6),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - COM raises bare Exception
            write_error = f"{type(exc).__name__}: {exc}"
        if after is None:
            try:
                after = read_model(self.conn.app, doc, doc.Part)
            except Exception as exc:  # noqa: BLE001 - no readback means no proof
                if write_error is None:
                    write_error = f"{type(exc).__name__}: {exc}"
                after = None
        unavailable: list[str] = []

        params_match: bool | None = None
        parameter_mismatch: list[dict[str, Any]] = []
        baseline_params = baseline.get("parameters") or {}
        numeric_params = {
            handle: value for handle, value in baseline_params.items() if _is_number(value)
        }
        if not numeric_params or after is None:
            unavailable.append("parameters")
        else:
            params_match = True
            for handle, expected in numeric_params.items():
                actual = model_value(after, handle)
                if not _is_number(actual) or not approx_equal(
                    float(actual), float(expected), rel=0.0, abs_tol=1e-6
                ):
                    params_match = False
                    parameter_mismatch.append(
                        {"parameter": handle, "expected": expected, "actual": actual}
                    )

        baseline_volume = baseline.get("volumeMm3")
        after_geometry = (after or {}).get("geometry") if isinstance(after, dict) else None
        after_volume = (after_geometry or {}).get("volumeMm3") if isinstance(after_geometry, dict) else None
        volume_match: bool | None = None
        if _is_number(baseline_volume) and _is_number(after_volume):
            # Protocol volume tolerance: max(0.01 mm3, expected * 1e-6).
            volume_match = approx_equal(
                float(after_volume), float(baseline_volume), rel=1e-6, abs_tol=0.01
            )
        else:
            unavailable.append("volume")

        baseline_bbox = baseline.get("bbox") if isinstance(baseline.get("bbox"), dict) else {}
        baseline_dims = (baseline_bbox or {}).get("dimensions") if isinstance(baseline_bbox, dict) else None
        after_bbox = (after_geometry or {}).get("bbox") if isinstance(after_geometry, dict) else None
        after_dims = (after_bbox or {}).get("dimensions") if isinstance(after_bbox, dict) else None
        bbox_match: bool | None = None
        if isinstance(baseline_dims, dict) and isinstance(after_dims, dict):
            try:
                bbox_match = all(
                    approx_equal(
                        float(after_dims[axis]), float(baseline_dims[axis]), rel=0.0, abs_tol=1e-6
                    )
                    for axis in ("x", "y", "z")
                )
            except Exception:
                bbox_match = False
        else:
            unavailable.append("bbox")

        baseline_holes = baseline.get("holes")
        after_holes = (after or {}).get("holes") if isinstance(after, dict) else None
        holes_match: bool | None = None
        if isinstance(baseline_holes, list) and isinstance(after_holes, list):
            try:
                before_signature = _hole_signature(baseline_holes)
                after_signature = _hole_signature(after_holes)
                if not before_signature and not after_signature:
                    # Two empty hole lists are not proof that holes were
                    # restored; the model simply has no hole evidence to compare.
                    holes_match = None
                    unavailable.append("holes")
                else:
                    holes_match = len(before_signature) == len(after_signature) and all(
                        all(
                            approx_equal(float(a), float(b), rel=0.0, abs_tol=1e-6)
                            for a, b in zip(before_row, after_row)
                        )
                        for before_row, after_row in zip(before_signature, after_signature)
                    )
            except Exception:
                holes_match = False
        else:
            unavailable.append("holes")

        available = [value for value in (params_match, volume_match, bbox_match, holes_match) if value is not None]
        # Unavailable evidence is listed in ``unavailable`` instead of counting
        # against the proof; no evidence at all can never prove a restore.
        restored = bool(available) and all(available)
        evidence = {
            "attempted": True,
            "restored": restored,
            "paramsMatch": params_match,
            "volumeMatch": volume_match,
            "bboxMatch": bbox_match,
            "holesMatch": holes_match,
            "unavailable": unavailable,
            "steps": recorded,
            "error": write_error,
            "parameterMismatch": parameter_mismatch,
            "volumeBefore": baseline_volume,
            "volumeAfterRollback": after_volume,
            "bboxBefore": baseline_dims,
            "bboxAfterRollback": after_dims,
            "holesBefore": len(baseline_holes) if isinstance(baseline_holes, list) else None,
            "holesAfterRollback": len(after_holes) if isinstance(after_holes, list) else None,
        }
        self.journal.append_event(
            "rollback" if write_error is None else "rollback_failed",
            evidence,
            session_id=session_id,
            operation_id=operation_id,
        )
        return evidence

    def _result(
        self,
        session: dict[str, Any],
        plan: dict[str, Any],
        *,
        verdict: str,
        code: str,
        message: str,
        modified: bool,
        before: dict[str, Any],
        after: dict[str, Any],
        steps: list[dict[str, Any]],
        checks: list[dict[str, Any]],
        recovery: dict[str, Any] | None = None,
        business_state: str | None = None,
    ) -> dict[str, Any]:
        data = {
            "editSessionId": session["editSessionId"],
            "operationId": plan.get("operationId"),
            # The result's own state can differ from the session's (a rejected
            # plan leaves the session untouched).
            "businessState": business_state or session["status"],
            "sessionState": session["status"],
            "modelRevision": session["modelRevision"],
            "verdict": verdict,
            "planHash": "sha256:" + plan_hash(plan),
            "idempotentReplay": False,
            "before": {"parameters": _param_map(before), "geometry": before["geometry"], "holes": before["holes"]},
            "after": {"parameters": _param_map(after), "geometry": after["geometry"], "holes": after["holes"]},
            "steps": steps,
            "checks": checks,
            "recovery": recovery or {"attempted": False, "restored": None},
            "delivery": None,
        }
        return _envelope(verdict in ("PASS", "ALREADY_SATISFIED"), code, message, modified=modified, data=data)

    def _record_result(
        self,
        session: dict[str, Any],
        result: dict[str, Any],
        plan: dict[str, Any] | None = None,
    ) -> None:
        data = result.get("data") or {}
        results = session.setdefault("results", [])
        results.append(
            {
                "operationId": data.get("operationId"),
                "verdict": data.get("verdict"),
                "businessState": data.get("businessState"),
                "sessionState": data.get("sessionState"),
                "modelRevision": data.get("modelRevision"),
                "code": result.get("code"),
                "plan": plan,
                "intent": (plan or {}).get("intent") if isinstance(plan, dict) else None,
                "before": data.get("before"),
                "after": data.get("after"),
                "steps": data.get("steps"),
                "recovery": data.get("recovery"),
                "checks": data.get("checks"),
                "utc": now(),
            }
        )
        session["results"] = results[-20:]
        self._save(session)


def _update_sketches(part: Any) -> None:
    """Force every sketch to re-solve before Part.Update().

    On this build a formula-driven dimension change updates the parameter but
    Part.Update() alone leaves the sketch geometry at its previous solution
    (observed: W 100->120 updated 偏移.17 to 40 while the circles stayed at
    y=35 on the second apply of a session). UpdateObject(sketch) re-solves it.
    """
    try:
        body = part.MainBody
        for index in range(1, body.Sketches.Count + 1):
            try:
                part.UpdateObject(body.Sketches.Item(index))
            except Exception:
                continue
    except Exception:
        return


def model_value(model: dict[str, Any], handle: Any) -> Any:
    if not handle:
        return None
    for param in model.get("parameters", []):
        if param.get("handle") == handle:
            return param.get("value")
    return None


def _param_map(model: dict[str, Any]) -> dict[str, Any]:
    return {
        p["handle"]: p.get("value")
        for p in model.get("parameters", [])
        if p.get("kind") == "driver"
    }


def _baseline_from_model(model: dict[str, Any]) -> dict[str, Any]:
    """Module-level entry point for the normalized baseline shape."""
    return EditCoordinator._baseline_from_model(model)
