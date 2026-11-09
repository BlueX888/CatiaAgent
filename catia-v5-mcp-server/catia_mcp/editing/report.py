"""Delivery reports: a machine-readable JSON envelope plus a human Markdown sheet.

The report is an audit artifact: it must be readable without the server process
and without trusting "the tool returned ok". Every verdict is paired with the
evidence the coordinator actually read back, and anything that was not measured
is reported as 未验证 instead of being assumed good.

Two data sources are merged, because the session record only keeps summaries
while the journal keeps the full payloads:

* ``session["results"]`` - per-operation verdict/code/checks/utc summaries.
* ``Journal`` - plan, intent, before/after baselines, steps and rollback
  evidence for each operation of the session.

The journal is consulted only when a session record lacks the detail (or when a
journal is passed explicitly), so building a report from a complete session
dict never touches the database.
"""

from __future__ import annotations

import json
import os
from typing import Any

from catia_mcp.editing.journal import Journal, default_db_path

SCHEMA = "catia-edit-report/1"

# Defaults frozen in docs/editing-protocol-v1.md section 1. They are written
# into the report so an auditor can tell a plan-declared tolerance from the
# protocol default.
DEFAULT_LENGTH_TOLERANCE_MM = 0.01
DEFAULT_VOLUME_ABS_TOLERANCE_MM3 = 0.01
DEFAULT_VOLUME_REL_TOLERANCE = 1e-6
DEFAULT_TOLERANCE_RULE = "长度/坐标 ±0.01 mm，体积 max(0.01 mm³, 期望×1e-6)"

# All operation states the journal may hold (the state machine was extended
# beyond intent/applied/verified/failed/unknown).
_JOURNAL_STATES = (
    "intent",
    "applied",
    "verified",
    "failed",
    "unknown",
    "recovered",
    "rejected",
)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _first(*values: Any) -> Any:
    """First value that is not None (False/0 are legitimate values)."""
    for value in values:
        if value is not None:
            return value
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _latest_operation(session: dict[str, Any]) -> dict[str, Any]:
    results = session.get("results") or []
    return results[-1] if results else {}


# ---------------------------------------------------------------------------
# operation records
# ---------------------------------------------------------------------------


def _parameter_values(parameters: Any) -> dict[str, Any]:
    """Canonical {handle: value} map.

    ``read_model()`` returns a list of parameter dicts; a baseline (and some
    journal payloads) already carries a mapping. Never call ``.get()`` on the
    list itself.
    """
    if isinstance(parameters, dict):
        return dict(parameters)
    out: dict[str, Any] = {}
    for param in parameters if isinstance(parameters, (list, tuple)) else []:
        if not isinstance(param, dict):
            continue
        handle = _first(param.get("handle"), param.get("name"))
        if handle is not None:
            out[str(handle)] = param.get("value")
    return out


def _baseline(model: Any) -> dict[str, Any] | None:
    """Normalize a before/after model to the shared baseline shape.

    Shape: {"parameters": {handle: value}, "volumeMm3": number|null,
    "bbox": {...}|null, "holes": [...]|null}.
    """
    if not isinstance(model, dict):
        return None
    geometry = model.get("geometry") if isinstance(model.get("geometry"), dict) else {}
    return {
        "parameters": _parameter_values(model.get("parameters")),
        "volumeMm3": _first(model.get("volumeMm3"), geometry.get("volumeMm3")),
        "bbox": _first(model.get("bbox"), geometry.get("bbox")),
        "holes": model.get("holes"),
    }


def _normalized_intent(intent: Any) -> dict[str, Any]:
    """Expose the request text and the parsed semantics under stable keys."""
    if isinstance(intent, dict):
        out = dict(intent)
        if "text" not in out:
            out["text"] = _first(out.get("utterance"), out.get("rawText"), out.get("request"))
        if "resolved" not in out:
            out["resolved"] = _first(out.get("semantics"), out.get("parsed"))
        return out
    if intent is None:
        return {"text": None, "resolved": None, "available": False}
    return {"text": str(intent), "resolved": None, "available": True}


def _normalized_changes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    changes = plan.get("changes") if isinstance(plan, dict) else None
    out: list[dict[str, Any]] = []
    for change in changes if isinstance(changes, list) else []:
        if not isinstance(change, dict):
            out.append({"parameter": None, "oldValue": None, "newValue": change, "unit": None, "reason": None})
            continue
        out.append(
            {
                "parameter": change.get("parameter"),
                "oldValue": change.get("oldValue"),
                "newValue": change.get("newValue"),
                "unit": change.get("unit"),
                "reason": change.get("reason"),
            }
        )
    return out


def _get_operation(journal: Any, operation_id: str, session_id: str) -> dict[str, Any] | None:
    """Session-scoped lookup, tolerant of the pre-/post-contract signature."""
    try:
        row = journal.get_operation(operation_id, session_id=session_id)
    except TypeError:
        try:
            row = journal.get_operation(operation_id)
        except Exception:  # noqa: BLE001 - degrade to the session record
            return None
    except Exception:  # noqa: BLE001 - degrade to the session record
        return None
    if not isinstance(row, dict):
        return None
    if row.get("session_id") not in (None, session_id):
        # Never leak another session's operation into this report.
        return None
    return row


def _load_journal_operations(
    session_id: str | None, journal: Any | None, operation_ids: list[Any] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read every journaled operation for this session (never raises)."""
    if not session_id:
        return [], ["session has no editSessionId; journal detail was not looked up"]
    try:
        if journal is None:
            path = default_db_path()
            if not os.path.exists(path):
                return [], ["journal database not found; operations are limited to the session record"]
            journal = Journal(path)
    except Exception as exc:  # noqa: BLE001 - a missing journal must not fail the delivery
        return [], [f"journal unavailable: {type(exc).__name__}: {exc}"]

    rows: list[Any] | None = None
    lister = getattr(journal, "operations_in_state", None)
    if callable(lister):
        try:
            rows = lister(session_id, _JOURNAL_STATES)
        except Exception:  # noqa: BLE001 - fall back to per-operation lookup
            rows = None
    if rows is None:
        # A minimal journal (or a test double) may only expose get_operation.
        rows = [
            row
            for row in (
                _get_operation(journal, str(operation_id), session_id)
                for operation_id in (operation_ids or [])
                if operation_id is not None
            )
            if row is not None
        ]
    operations: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("session_id") not in (None, session_id):
            continue
        operations.append(row)
    return operations, []


def _has_detail(entry: Any) -> bool:
    """A record is audit-complete when it carries the plan and a baseline."""
    if not isinstance(entry, dict):
        return False
    nested = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    has_plan = "plan" in entry or "plan" in nested
    has_baseline = any(key in entry for key in ("before", "after")) or any(
        key in nested for key in ("before", "after")
    )
    return has_plan and has_baseline


def _operation_record(entry: Any, journal_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge one session summary with its journal row into an auditable record."""
    entry = entry if isinstance(entry, dict) else {}
    row = journal_row if isinstance(journal_row, dict) else {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}

    # A full EditResult envelope keeps its payload under data and has a
    # top-level "recovery" *capability* block; its rollback evidence is
    # data.recovery. A flattened session record keeps evidence at top level.
    nested = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
    data = nested or result_data

    plan = _first(entry.get("plan"), payload.get("plan"), data.get("plan"))
    plan = plan if isinstance(plan, dict) else {}

    before = _baseline(_first(entry.get("before"), data.get("before"), payload.get("before")))
    after = _baseline(_first(entry.get("after"), data.get("after")))
    steps = _first(entry.get("steps"), data.get("steps"), payload.get("steps"))
    checks = _first(entry.get("checks"), data.get("checks"), payload.get("checks"))
    top_recovery = entry.get("recovery")
    if isinstance(top_recovery, dict) and "rollback_supported" in top_recovery and "attempted" not in top_recovery:
        # The envelope's capability block is not rollback evidence.
        top_recovery = None
    recovery = _first(top_recovery, data.get("recovery"), payload.get("recovery"))

    plan_hash = _first(entry.get("planHash"), data.get("planHash"), row.get("plan_hash"))
    if isinstance(plan_hash, str) and plan_hash and not plan_hash.startswith("sha256:"):
        plan_hash = "sha256:" + plan_hash

    code = _first(entry.get("code"), result.get("code"), payload.get("code"))
    if code is None:
        problems = payload.get("problems")
        if isinstance(problems, list) and problems and isinstance(problems[0], dict):
            code = problems[0].get("code")

    return {
        "operationId": _first(
            entry.get("operationId"), entry.get("operation_id"), data.get("operationId"), row.get("operation_id")
        ),
        "plan": plan,
        "intent": _normalized_intent(_first(plan.get("intent"), entry.get("intent"), data.get("intent"))),
        "before": before,
        "after": after,
        "steps": steps if isinstance(steps, list) else [],
        "checks": checks if isinstance(checks, list) else [],
        "recovery": recovery if isinstance(recovery, dict) else {"attempted": False, "restored": None},
        "verdict": _first(entry.get("verdict"), data.get("verdict"), result.get("verdict")),
        "code": code,
        "utc": _first(
            entry.get("utc"),
            entry.get("createdUtc"),
            data.get("utc"),
            row.get("updated_utc"),
            row.get("created_utc"),
        ),
        "modelRevision": _first(entry.get("modelRevision"), data.get("modelRevision"), payload.get("revision")),
        "state": _first(entry.get("state"), row.get("state")),
        "businessState": _first(entry.get("businessState"), data.get("businessState")),
        "sessionState": _first(entry.get("sessionState"), data.get("sessionState")),
        "planHash": plan_hash,
        "message": _first(entry.get("message"), result.get("message")),
    }


def _entry_operation_id(entry: dict[str, Any]) -> Any:
    nested = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    return _first(entry.get("operationId"), entry.get("operation_id"), nested.get("operationId"))


def _operations(
    session: dict[str, Any], journal: Any | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    results = [item for item in (session.get("results") or []) if isinstance(item, dict)]
    fresh = [item for item in results if _has_detail(item)]
    known_ids = [_entry_operation_id(entry) for entry in results]

    journal_ops: list[dict[str, Any]] = []
    notes: list[str] = []
    # A journal supplied by the caller is always honored (it may know about
    # rejected/intent operations); the default journal is only opened when the
    # session record alone cannot provide the detail an audit needs.
    if journal is not None or len(fresh) < len(results) or not results:
        journal_ops, notes = _load_journal_operations(session.get("editSessionId"), journal, known_ids)

    by_id = {row.get("operation_id"): row for row in journal_ops if row.get("operation_id")}
    records: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for entry in results:
        operation_id = _entry_operation_id(entry)
        records.append(_operation_record(entry, by_id.get(operation_id)))
        if operation_id is not None:
            seen.add(operation_id)
    for row in journal_ops:
        if row.get("operation_id") in seen:
            continue
        records.append(_operation_record({}, row))
        seen.add(row.get("operation_id"))
    # session['results'] is capped at the newest 20 entries, so older operations
    # are appended from the journal at the end. Order by the recorded time so
    # "the latest request" means chronologically latest, not list-latest.
    records.sort(key=lambda record: record.get("utc") or "9999")
    return records, notes


def _request_operation(operations: list[dict[str, Any]]) -> dict[str, Any]:
    """The operation the delivery's model state actually comes from.

    A later rejected attempt may be journaled after a successful apply; it has
    no after-baseline and must not replace the applied request in the report.
    """
    for operation in reversed(operations):
        plan = operation.get("plan")
        if (
            isinstance(plan, dict)
            and plan
            and isinstance(operation.get("after"), dict)
            and operation.get("verdict") in ("PASS", "ALREADY_SATISFIED")
        ):
            return operation
    for operation in reversed(operations):
        plan = operation.get("plan")
        if isinstance(plan, dict) and plan and isinstance(operation.get("after"), dict):
            return operation
    for operation in reversed(operations):
        plan = operation.get("plan")
        if isinstance(plan, dict) and plan:
            return operation
    return operations[-1] if operations else {}


# ---------------------------------------------------------------------------
# tolerances / unverified / recovery
# ---------------------------------------------------------------------------


def _default_tolerance(kind: Any, expected: Any) -> dict[str, Any]:
    if kind == "volume":
        value = float(expected) if _is_number(expected) else None
        absolute = (
            max(DEFAULT_VOLUME_ABS_TOLERANCE_MM3, value * DEFAULT_VOLUME_REL_TOLERANCE)
            if value is not None
            else DEFAULT_VOLUME_ABS_TOLERANCE_MM3
        )
        return {
            "abs": absolute,
            "rel": DEFAULT_VOLUME_REL_TOLERANCE,
            "rule": "max(0.01 mm³, 期望×1e-6)",
        }
    if kind == "feature_count":
        return {"abs": 0, "rel": None, "rule": "整数精确相等"}
    return {"abs": DEFAULT_LENGTH_TOLERANCE_MM, "rel": None, "rule": "长度/坐标 ±0.01 mm"}


def _default_tolerance_rules() -> dict[str, Any]:
    return {
        "lengthMm": DEFAULT_LENGTH_TOLERANCE_MM,
        "coordinateMm": DEFAULT_LENGTH_TOLERANCE_MM,
        "volume": {
            "absMm3": DEFAULT_VOLUME_ABS_TOLERANCE_MM3,
            "rel": DEFAULT_VOLUME_REL_TOLERANCE,
            "rule": "max(0.01 mm³, 期望×1e-6)",
        },
        "rule": DEFAULT_TOLERANCE_RULE,
    }


def _tolerances(plan: dict[str, Any], operation_id: Any) -> dict[str, Any]:
    """Explicit plan tolerances plus the protocol default rule for the rest."""
    items: list[dict[str, Any]] = []
    explicit: list[dict[str, Any]] = []
    acceptance = plan.get("acceptance") if isinstance(plan, dict) else None
    for index, item in enumerate(acceptance if isinstance(acceptance, list) else []):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        has_abs = _is_number(item.get("absTolerance"))
        has_rel = _is_number(item.get("relTolerance"))
        default = _default_tolerance(kind, item.get("expected"))
        record = {
            "operationId": operation_id,
            "index": index,
            "type": kind,
            "details": {key: value for key, value in item.items() if key not in ("absTolerance", "relTolerance")},
            "absTolerance": item.get("absTolerance") if has_abs else None,
            "relTolerance": item.get("relTolerance") if has_rel else None,
            "explicit": has_abs or has_rel,
            "effective": {
                "abs": float(item["absTolerance"]) if has_abs else default["abs"],
                "rel": float(item["relTolerance"]) if has_rel else default["rel"],
                "rule": "计划显式容差" if (has_abs or has_rel) else default["rule"],
            },
        }
        items.append(record)
        if has_abs or has_rel:
            explicit.append(
                {
                    "operationId": operation_id,
                    "index": index,
                    "type": kind,
                    "absTolerance": record["absTolerance"],
                    "relTolerance": record["relTolerance"],
                }
            )
    if not items:
        source = "default"
    elif len(explicit) == len(items):
        source = "explicit"
    elif explicit:
        source = "mixed"
    else:
        source = "default"
    return {
        "operationId": operation_id,
        "source": source,
        "explicit": explicit,
        "items": items,
        "defaults": _default_tolerance_rules(),
    }


def _unverified(verification: Any, limitations: list[Any]) -> dict[str, Any]:
    verification = verification if isinstance(verification, dict) else {}
    checks = verification.get("checks") if isinstance(verification.get("checks"), list) else []
    not_passed = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        if check.get("status") != "pass":
            not_passed.append(
                {"id": check.get("id"), "status": check.get("status"), "evidence": check.get("evidence")}
            )
    return {"checks": not_passed, "limitations": list(limitations or [])}


def _recovery_history(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for operation in operations:
        recovery = operation.get("recovery")
        if not isinstance(recovery, dict):
            continue
        attempted = recovery.get("attempted")
        restored = recovery.get("restored")
        if attempted is True or restored is not None or recovery.get("error"):
            history.append(
                {
                    "operationId": operation.get("operationId"),
                    "attempted": attempted,
                    "restored": restored,
                    "evidence": recovery,
                    "utc": operation.get("utc"),
                }
            )
    return history


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def build_report(
    session: dict[str, Any],
    delivery_path: str,
    delivery_sha: str,
    delivered_bytes: int,
    expected: dict[str, Any],
    verification: dict[str, Any],
    journal: Any | None = None,
) -> dict[str, Any]:
    last = _latest_operation(session)
    operations, journal_notes = _operations(session, journal)
    latest = _request_operation(operations)
    plan = latest.get("plan") if isinstance(latest.get("plan"), dict) else {}
    operation_id = latest.get("operationId")
    limitations = (session.get("snapshot") or {}).get("limitations", [])

    report = {
        "schema": SCHEMA,
        "editSessionId": session["editSessionId"],
        "source": session["source"],
        "workingCopy": session["workingCopy"],
        "delivery": {
            "path": delivery_path,
            "sha256": delivery_sha,
            "sizeBytes": delivered_bytes,
        },
        "modelRevision": session.get("modelRevision"),
        "operations": operations,
        "lastVerdict": last.get("verdict"),
        "expected": {
            "geometry": expected.get("geometry"),
            "holes": expected.get("holes"),
            "drivers": {
                handle: value
                for handle, value in _parameter_values(expected.get("parameters")).items()
                if _is_driver(expected.get("parameters"), handle)
            },
        },
        "verification": verification,
        "limitations": limitations,
        "capabilities": (session.get("snapshot") or {}).get("capabilities", {}),
        # --- audit sections -------------------------------------------------
        "request": {
            "operationId": operation_id,
            "intent": _normalized_intent(plan.get("intent")),
            "changes": _normalized_changes(plan),
        },
        "tolerances": _tolerances(plan, operation_id),
        "unverified": _unverified(verification, limitations),
        "recoveryHistory": _recovery_history(operations),
        "generatedUtc": _now(),
    }
    if journal_notes:
        # Report-source limitations are not model limitations; keep them apart
        # but still visible to the auditor.
        report["journalNotes"] = journal_notes
    return report


def _is_driver(parameters: Any, handle: str) -> bool:
    """Whether a handle is a driver, for both list and mapping parameter shapes."""
    if isinstance(parameters, dict):
        # A mapping carries no kind; keep every entry so no value is hidden.
        return True
    for param in parameters if isinstance(parameters, (list, tuple)) else []:
        if isinstance(param, dict) and _first(param.get("handle"), param.get("name")) == handle:
            return param.get("kind") == "driver"
    return False


def write_reports(
    session: dict[str, Any],
    delivery_path: str,
    delivery_sha: str,
    delivered_bytes: int,
    expected: dict[str, Any],
    verification: dict[str, Any],
    journal: Any | None = None,
) -> dict[str, str]:
    report = build_report(
        session, delivery_path, delivery_sha, delivered_bytes, expected, verification, journal=journal
    )
    base = os.path.splitext(delivery_path)[0]
    json_path = base + "-report.json"
    md_path = base + "-report.md"
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(_markdown(report))
    try:
        from catia_mcp.run_evidence import write_edit_evidence
        write_edit_evidence(
            json_path,
            session=session,
            delivery={"path": delivery_path, "sha256": delivery_sha},
            verification=verification,
            operation_id=(report.get("request") or {}).get("operationId"),
        )
    except Exception as evidence_error:  # sidecar failure is recorded, not fatal
        with open(json_path + ".evidence-error.txt", "w", encoding="utf-8") as handle:
            handle.write(f"{type(evidence_error).__name__}: {evidence_error}")
    return {"json": json_path, "markdown": md_path}


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _text(value: Any, missing: str = "未记录") -> str:
    if value is None:
        return missing
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _cell(value: Any, missing: str = "未记录") -> str:
    return _text(value, missing).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _number(value: Any) -> str:
    return _text(value, "未记录")


def _source_unchanged_state(verification: dict[str, Any]) -> str:
    """Measured only: a missing field is 未验证, never an assumed 是."""
    value = verification.get("sourceUnchanged")
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "未验证"


def _passed_state(value: Any) -> str:
    if value is True:
        return "通过"
    if value is False:
        return "未通过"
    return "未验证"


def _bbox_dimensions(bbox: Any) -> dict[str, Any] | None:
    if isinstance(bbox, dict) and isinstance(bbox.get("dimensions"), dict):
        return bbox["dimensions"]
    return None


def _holes_summary(holes: Any) -> str:
    if holes is None:
        return "未读取"
    if not isinstance(holes, list):
        return _text(holes)
    if not holes:
        return "无孔"
    parts = []
    for hole in holes:
        if not isinstance(hole, dict):
            parts.append(_text(hole))
            continue
        center = hole.get("center") if isinstance(hole.get("center"), dict) else {}
        parts.append(
            "(x={x}, y={y}, z={z}, r={r})".format(
                x=_number(center.get("x")),
                y=_number(center.get("y")),
                z=_number(center.get("z")),
                r=_number(hole.get("radius")),
            )
        )
    return "；".join(parts)


def _format_change(change: dict[str, Any]) -> str:
    return (
        f"`{_cell(change.get('parameter'))}`: {_cell(change.get('oldValue'))} → "
        f"{_cell(change.get('newValue'))} {_cell(change.get('unit'), '')}"
        f"（{_cell(change.get('reason'), '未说明理由')}）"
    )


def _markdown(report: dict[str, Any]) -> str:
    verification = report.get("verification") if isinstance(report.get("verification"), dict) else {}
    source = report.get("source") if isinstance(report.get("source"), dict) else {}
    delivery = report.get("delivery") if isinstance(report.get("delivery"), dict) else {}
    lines = [
        "# CATIA 修改交付报告",
        "",
        f"- 会话：`{report['editSessionId']}`，模型版本 r{report['modelRevision']}",
        f"- 源文件：`{source.get('path')}`",
        f"- 源文件 SHA-256：`{source.get('sha256')}`（会话结束时未变：{_source_unchanged_state(verification)}）",
        f"- 交付文件：`{delivery.get('path')}`",
        f"- 交付文件 SHA-256：`{delivery.get('sha256')}`",
        f"- 重开验证：{_passed_state(verification.get('passed'))}",
        "",
        "## 原始需求与解析语义",
    ]

    request = report.get("request") or {}
    intent = request.get("intent") or {}
    lines.append(f"- 关联操作：`{_text(request.get('operationId'))}`")
    lines.append(f"- 意图原文：{_text(intent.get('text'), '未提供')}")
    lines.append(f"- 解析语义：{_text(intent.get('resolved'), '未提供')}")
    changes = request.get("changes") or []
    if changes:
        lines += ["", "| 参数 | 旧值 | 新值 | 单位 | 理由 |", "|---|---|---|---|---|"]
        for change in changes:
            lines.append(
                "| `{parameter}` | {old} | {new} | {unit} | {reason} |".format(
                    parameter=_cell(change.get("parameter")),
                    old=_cell(change.get("oldValue")),
                    new=_cell(change.get("newValue")),
                    unit=_cell(change.get("unit")),
                    reason=_cell(change.get("reason")),
                )
            )
    else:
        lines.append("- 计划变更：未记录")

    lines += ["", "## 参数前后值"]
    operations = report.get("operations") or []
    rendered = 0
    for operation in operations:
        before = operation.get("before")
        after = operation.get("after")
        if not isinstance(before, dict) and not isinstance(after, dict):
            continue
        rendered += 1
        before = before if isinstance(before, dict) else {}
        after = after if isinstance(after, dict) else {}
        lines.append("")
        lines.append(
            f"### `{_text(operation.get('operationId'))}`（{_text(operation.get('verdict'))}，"
            f"{_text(operation.get('code'))}）"
        )
        before_params = before.get("parameters") if isinstance(before.get("parameters"), dict) else {}
        after_params = after.get("parameters") if isinstance(after.get("parameters"), dict) else {}
        handles = list(before_params)
        handles += [handle for handle in after_params if handle not in before_params]
        if handles:
            lines += ["", "| 参数 | 前值 | 后值 |", "|---|---|---|"]
            for handle in handles:
                lines.append(
                    f"| `{_cell(handle)}` | {_cell(before_params.get(handle))} | {_cell(after_params.get(handle))} |"
                )
            lines.append("")
        else:
            lines.append("- 参数：未记录")
        lines.append(f"- 体积：{_number(before.get('volumeMm3'))} mm³ → {_number(after.get('volumeMm3'))} mm³")
        before_dims = _bbox_dimensions(before.get("bbox"))
        after_dims = _bbox_dimensions(after.get("bbox"))
        if before_dims or after_dims:
            axes = ("x", "y", "z")
            lines.append(
                "- 包围盒尺寸："
                + "，".join(
                    f"{axis} {_number((before_dims or {}).get(axis))} → {_number((after_dims or {}).get(axis))}"
                    for axis in axes
                )
                + " mm"
            )
        else:
            lines.append("- 包围盒尺寸：未读取")
        lines.append(f"- 孔位：{_holes_summary(before.get('holes'))} → {_holes_summary(after.get('holes'))}")
    if not rendered:
        lines.append("- 未记录前后参数")

    lines += ["", "## 验收项与容差"]
    tolerances = report.get("tolerances") or {}
    items = tolerances.get("items") or []
    if items:
        lines += ["", "| # | 类型 | 期望 | absTolerance | relTolerance | 生效规则 |", "|---|---|---|---|---|---|"]
        for item in items:
            details = item.get("details") if isinstance(item.get("details"), dict) else {}
            expected_value = details.get("expected")
            if expected_value is None:
                expected_value = {
                    key: details[key] for key in ("axis", "min", "max", "holes") if details.get(key) is not None
                } or None
            lines.append(
                "| {index} | {kind} | {expected} | {abs_tol} | {rel_tol} | {rule} |".format(
                    index=_cell(item.get("index")),
                    kind=_cell(item.get("type")),
                    expected=_cell(expected_value, "未声明"),
                    abs_tol=_cell(item.get("absTolerance"), "默认"),
                    rel_tol=_cell(item.get("relTolerance"), "默认"),
                    rule=_cell((item.get("effective") or {}).get("rule")),
                )
            )
        lines.append("")
    else:
        lines.append("- 计划未声明验收项")
    defaults = tolerances.get("defaults") or {}
    lines.append(f"- 容差来源：{_text(tolerances.get('source'), '默认')}")
    lines.append(f"- 默认规则：{_text(defaults.get('rule'), DEFAULT_TOLERANCE_RULE)}")

    lines += ["", "## 操作历史"]
    if not operations:
        lines.append("- 未记录")
    for operation in operations:
        lines.append(
            f"- `{operation.get('operationId')}` → {operation.get('verdict')}（{operation.get('code')}），"
            f"r{operation.get('modelRevision')}"
        )
        if operation.get("utc"):
            lines.append(f"  - 记录时间：{_text(operation.get('utc'))}")
        plan_changes = (operation.get("plan") or {}).get("changes") or []
        if plan_changes:
            lines.append("  - 变更：" + "；".join(_format_change(change) for change in plan_changes if isinstance(change, dict)))
        if operation.get("steps"):
            lines.append(f"  - 执行步骤：{len(operation['steps'])} 步")
        recovery = operation.get("recovery") or {}
        if recovery.get("attempted") or recovery.get("restored") is not None:
            lines.append(
                f"  - 恢复：attempted={_text(recovery.get('attempted'))}，"
                f"restored={_text(recovery.get('restored'))}"
            )

    lines += ["", "## 重开验证项"]
    checks = verification.get("checks") if isinstance(verification.get("checks"), list) else []
    if not checks:
        lines.append("- 未记录重开验证项")
    for check in checks:
        if not isinstance(check, dict):
            lines.append(f"- {_text(check)}")
            continue
        lines.append(
            f"- {_text(check.get('id'))}: {_text(check.get('status'))} — "
            f"`{json.dumps(check.get('evidence'), ensure_ascii=False, default=str)}`"
        )

    unverified = report.get("unverified") or {}
    lines += ["", "## 未判定/未验证项"]
    unverified_checks = unverified.get("checks") or []
    limitations = unverified.get("limitations") or []
    if not unverified_checks and not limitations and not report.get("journalNotes"):
        lines.append("- 没有记录未判定/未验证项")
    for check in unverified_checks:
        if not isinstance(check, dict):
            lines.append(f"- {_text(check)}")
            continue
        lines.append(
            f"- [检查] {_text(check.get('id'))}: {_text(check.get('status'))} — "
            f"`{json.dumps(check.get('evidence'), ensure_ascii=False, default=str)}`"
        )
    for limitation in limitations:
        lines.append(f"- [限制] {_text(limitation)}")
    for note in report.get("journalNotes") or []:
        lines.append(f"- [报告数据] {_text(note)}")

    lines += ["", "## 恢复历史"]
    history = report.get("recoveryHistory") or []
    if not history:
        lines.append("- 无恢复记录")
    for item in history:
        lines.append(
            f"- `{_text(item.get('operationId'))}`：attempted={_text(item.get('attempted'))}，"
            f"restored={_text(item.get('restored'))}；证据："
            f"`{json.dumps(item.get('evidence'), ensure_ascii=False, default=str)}`"
        )

    legacy_limitations = report.get("limitations") or []
    if legacy_limitations:
        lines += ["", "## 未验证/不可用项"]
        lines += [f"- {_text(item)}" for item in legacy_limitations]
    return "\n".join(lines) + "\n"
