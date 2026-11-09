"""Structured failures without changing server-wide session availability."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

_SHADOW_DIFFERENCES: list[dict[str, Any]] = []
_MAX_SHADOW_DIFFERENCES = 20


def shadow_differences() -> list[dict[str, Any]]:
    """Bounded, process-local shadow diagnostics; never part of a response."""
    return list(_SHADOW_DIFFERENCES)


def clear_shadow_differences() -> None:
    _SHADOW_DIFFERENCES.clear()


def record_shadow_difference(kind: str, tool: str | None, detail: Any) -> None:
    if len(_SHADOW_DIFFERENCES) >= _MAX_SHADOW_DIFFERENCES:
        _SHADOW_DIFFERENCES.pop(0)
    _SHADOW_DIFFERENCES.append({"kind": kind, "tool": tool, "detail": detail})


def record_result_shadow(result: dict[str, Any]) -> None:
    """Validate a result dict in shadow mode; the returned dict is untouched."""
    if not isinstance(result, dict):
        return
    try:
        from catia_mcp.contracts.results import OperationResultWire, classify_build_result
        wire = OperationResultWire.model_validate(result)
        if wire.tool == "catia_build_part":
            verdict = classify_build_result(wire)
            if verdict["state"] == "invalid_receipt":
                record_shadow_difference("output_receipt", wire.tool, verdict["reasons"])
    except Exception as error:
        record_shadow_difference("output_structure", result.get("tool"), [str(error)])


def minimal_failure_result(
    *,
    tool: str,
    code: str,
    message: str,
    operation_id: str | None = None,
    modified: bool | None,
    stage: str = "execute",
) -> dict[str, Any]:
    """Independent minimal failure receipt.

    The future active path must not call the same factory that may have failed.
    This constructor touches no shared state and cannot recurse into
    ``operation_result``.
    """
    return {
        "ok": False,
        "code": code,
        "operation_id": operation_id or uuid4().hex,
        "tool": tool,
        "message": message,
        "data": {"failure_stage": stage},
        "effects": {"modified": modified},
        "diagnostics": [],
        "warnings": [],
        "recovery": {
            "rollback_supported": False,
            "automatic_retry_allowed": False,
            "next_actions": (
                ["Inspect the CATIA model before retrying; model changes were not verified"]
                if modified is None else
                (["Inspect partial model changes; recovery is manual"] if modified else [])
            ),
        },
    }


def operation_result(
    *,
    tool: str,
    ok: bool,
    code: str,
    message: str,
    data: dict[str, Any],
    modified: bool | None,
    operation_id: str | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    if modified is None:
        next_actions = ["Inspect the CATIA model before retrying; model changes were not verified"]
    elif not ok and modified:
        next_actions = ["Inspect partial model changes; recovery is manual"]
    else:
        next_actions = []
    result = {
        "ok": ok,
        "code": code,
        "operation_id": operation_id or uuid4().hex,
        "tool": tool,
        "message": message,
        "data": data,
        "effects": {"modified": modified},
        "diagnostics": diagnostics or [],
        "warnings": warnings or [],
        "recovery": {
            "rollback_supported": False,
            "automatic_retry_allowed": False,
            "next_actions": next_actions,
        },
    }
    record_result_shadow(result)
    return result
