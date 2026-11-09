"""Result shell and key business payloads.

The contract validates structure and classifies the *receipt*.  It never claims
that geometry was verified because a field exists: success requires an explicit
PASS verdict plus the editability receipt, and an unknown session stays unknown
instead of being rewritten as "not modified".

``automatic_retry_allowed`` semantics are unchanged: a CATIA receipt must carry
``false``; this module only refuses contradictory receipts.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import ConfigDict, Field, StrictBool

from .base import ContractModel, WireInt, WireModel

_HEX64 = r"^[a-fA-F0-9]{64}$"


class OpenModel(WireModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True, validate_assignment=True)


class Effects(ContractModel):
    modified: Optional[StrictBool]


class Recovery(ContractModel):
    rollback_supported: StrictBool
    automatic_retry_allowed: StrictBool
    next_actions: list[str] = Field(default_factory=list)


class OperationResultWire(OpenModel):
    ok: StrictBool
    code: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    message: str
    data: dict[str, Any]
    effects: Effects
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    recovery: Recovery


class StageReceipt(OpenModel):
    stage: str
    status: str


class VerificationReceipt(OpenModel):
    verdict: str
    stepExport: Optional[dict[str, Any]] = None
    checks: Optional[list[dict[str, Any]]] = None
    sessionHealth: Optional[str] = None


class EditabilityReceipt(OpenModel):
    passed: StrictBool
    declaredParameterCount: WireInt = Field(ge=0)
    verifiedParameterCount: WireInt = Field(ge=0)
    sampled: StrictBool
    manifestSha256: Optional[str] = None
    journalHeadSha256: Optional[str] = None
    attempts: Optional[WireInt] = None


class DeliveryReceipt(OpenModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    reportJson: str = Field(min_length=1)
    reportJsonSha256: str = Field(pattern=_HEX64)
    stepPath: str = Field(min_length=1)
    stepSha256: Optional[str] = Field(default=None, pattern=_HEX64)
    readOnly: Optional[Any] = None


class BuildSuccessData(OpenModel):
    businessState: Literal["completed"]
    planId: str = Field(min_length=1)
    stages: list[StageReceipt] = Field(min_length=1)
    verification: VerificationReceipt
    editability: EditabilityReceipt
    delivery: DeliveryReceipt


class UnknownData(OpenModel):
    businessState: Literal["session_unknown"]
    sessionHealth: Literal["unknown"]
    planId: Optional[str] = None


class FailureData(OpenModel):
    businessState: str
    failure: Optional[dict[str, Any]] = None
    stages: list[StageReceipt] = Field(default_factory=list)
    delivery: Optional[dict[str, Any]] = None
    sessionHealth: Optional[str] = None
    editProgress: Optional[dict[str, Any]] = None
    planId: Optional[str] = None


SUCCESS = "success"
DEFINITE_FAILURE = "definite_failure"
PARTIAL_FAILURE = "partial_failure"
UNKNOWN = "unknown"
INVALID_RECEIPT = "invalid_receipt"


def classify_build_result(result: OperationResultWire | dict[str, Any]) -> dict[str, Any]:
    """Classify a build receipt without asserting geometry beyond its evidence.

    Returns ``{"state", "modified", "retry_allowed", "reasons", "payload"}``.
    ``payload`` is the typed success/unknown/failure view when it parses.
    """
    reasons: list[str] = []
    try:
        wire = result if isinstance(result, OperationResultWire) else OperationResultWire.model_validate(result)
    except Exception as exc:  # malformed receipt, not a business verdict
        return {"state": INVALID_RECEIPT, "modified": None, "retry_allowed": None,
                "reasons": [f"malformed receipt: {exc}"], "payload": None}

    retry_allowed = wire.recovery.automatic_retry_allowed
    if retry_allowed is not False:
        reasons.append("automatic_retry_allowed must be false for a CATIA receipt")

    if wire.ok:
        if wire.code != "BUILD_COMPLETED":
            reasons.append(f"ok=true with code {wire.code!r}")
        if wire.effects.modified is not True:
            reasons.append("ok=true without effects.modified=true")
        payload = None
        try:
            payload = BuildSuccessData.model_validate(wire.data)
        except Exception as exc:
            reasons.append(f"success payload invalid: {exc}")
        if payload is not None:
            if payload.verification.verdict != "PASS":
                reasons.append("success payload without verification.verdict=PASS")
            if payload.verification.sessionHealth not in (None,):
                reasons.append("success payload carries sessionHealth")
            if payload.editability.passed is not True:
                reasons.append("success payload without editability.passed=true")
            if payload.editability.verifiedParameterCount != payload.editability.declaredParameterCount:
                reasons.append("success payload did not verify every declared parameter")
            step_export = payload.verification.stepExport or {}
            if step_export.get("path") and payload.delivery.stepPath != step_export.get("path"):
                reasons.append("delivery.stepPath disagrees with verification.stepExport.path")
        return {
            "state": SUCCESS if not reasons else INVALID_RECEIPT,
            "modified": wire.effects.modified,
            "retry_allowed": retry_allowed,
            "reasons": reasons,
            "payload": payload,
        }

    data = wire.data or {}
    if data.get("sessionHealth") == "unknown" or data.get("businessState") == "session_unknown":
        try:
            payload = UnknownData.model_validate(data)
        except Exception as exc:
            reasons.append(f"unknown payload invalid: {exc}")
            payload = None
        return {
            "state": UNKNOWN if not reasons else INVALID_RECEIPT,
            "modified": wire.effects.modified,
            "retry_allowed": retry_allowed,
            "reasons": reasons,
            "payload": payload,
        }

    try:
        payload = FailureData.model_validate(data)
    except Exception as exc:
        reasons.append(f"failure payload invalid: {exc}")
        payload = None

    if wire.effects.modified is False and data.get("businessState") in {
            "build_failed", "verification_failed", "verification_incomplete"}:
        state = DEFINITE_FAILURE
    elif wire.effects.modified is True:
        state = PARTIAL_FAILURE
    elif wire.effects.modified is None:
        state = UNKNOWN
    else:
        state = INVALID_RECEIPT
        reasons.append(f"failure receipt with contradictory businessState {data.get('businessState')!r}")
    return {
        "state": state if not reasons else INVALID_RECEIPT,
        "modified": wire.effects.modified,
        "retry_allowed": retry_allowed,
        "reasons": reasons,
        "payload": payload,
    }
