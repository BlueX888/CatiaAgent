"""T07 result shell: required three-state modified, receipts, contradictions."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from catia_mcp.contracts.results import (
    DEFINITE_FAILURE,
    INVALID_RECEIPT,
    PARTIAL_FAILURE,
    SUCCESS,
    UNKNOWN,
    Effects,
    OperationResultWire,
    classify_build_result,
)
from catia_mcp.results import operation_result

HEX = "a" * 64


def success_data() -> dict:
    return {
        "businessState": "completed",
        "planId": "sha256:" + "b" * 64,
        "stages": [
            {"stage": "requirements", "status": "passed"},
            {"stage": "delivery", "status": "completed"},
        ],
        "verification": {"verdict": "PASS",
                          "stepExport": {"path": "C:\\out\\part.step", "sha256": HEX}},
        "editability": {"passed": True, "declaredParameterCount": 4,
                        "verifiedParameterCount": 4, "sampled": False},
        "delivery": {"path": "C:\\out\\part.CATPart", "sha256": HEX,
                     "reportJson": "C:\\out\\report.json", "reportJsonSha256": HEX,
                     "stepPath": "C:\\out\\part.step"},
    }


def success_receipt() -> dict:
    return operation_result(tool="catia_build_part", ok=True, code="BUILD_COMPLETED",
                            message="built", data=success_data(), modified=True,
                            operation_id="op-1")


def failure_receipt() -> dict:
    return operation_result(tool="catia_build_part", ok=False, code="FEATURE_BUILD_FAILED",
                            message="boom", data={"businessState": "build_failed",
                                                  "failure": {"stage": "modeling", "reason": "boom"}},
                            modified=False, operation_id="op-2")


def unknown_receipt() -> dict:
    return operation_result(tool="catia_build_part", ok=False, code="VERIFIER_SESSION_UNKNOWN",
                            message="unknown", modified=None,
                            data={"businessState": "session_unknown", "sessionHealth": "unknown",
                                  "planId": "sha256:" + "c" * 64}, operation_id="op-3")


# -- shell and null semantics ------------------------------------------------


def test_existing_success_and_failure_receipts_keep_wire_fields():
    for receipt in (success_receipt(), failure_receipt(), unknown_receipt()):
        wire = OperationResultWire.model_validate(receipt)
        assert wire.model_dump() == receipt


def test_modified_is_required_and_keeps_null():
    wire = OperationResultWire.model_validate(unknown_receipt())
    assert wire.effects.modified is None
    broken = copy.deepcopy(failure_receipt())
    del broken["effects"]["modified"]
    with pytest.raises(ValidationError):
        OperationResultWire.model_validate(broken)
    assert Effects.model_validate({"modified": None}).modified is None


@pytest.mark.parametrize("value", ["false", "true", 0, 1, [], {}])
def test_modified_rejects_coercion(value):
    with pytest.raises(ValidationError):
        Effects.model_validate({"modified": value})


# -- classification ----------------------------------------------------------


def test_complete_success_is_success():
    verdict = classify_build_result(success_receipt())
    assert verdict["state"] == SUCCESS
    assert verdict["modified"] is True
    assert verdict["retry_allowed"] is False


def test_definite_failure_is_not_unknown():
    verdict = classify_build_result(failure_receipt())
    assert verdict["state"] == DEFINITE_FAILURE
    assert verdict["modified"] is False


def test_unknown_stays_unknown_and_is_not_rewritten_as_unmodified():
    verdict = classify_build_result(unknown_receipt())
    assert verdict["state"] == UNKNOWN
    assert verdict["modified"] is None


def test_partial_modification_is_reported_without_retry():
    receipt = failure_receipt()
    receipt["effects"]["modified"] = True
    receipt["data"]["businessState"] = "verification_failed"
    verdict = classify_build_result(receipt)
    assert verdict["state"] == PARTIAL_FAILURE
    assert verdict["modified"] is True
    assert verdict["retry_allowed"] is False


# -- contradictory receipts --------------------------------------------------


def test_success_code_without_modified_true_is_invalid():
    receipt = success_receipt()
    receipt["effects"]["modified"] = None
    assert classify_build_result(receipt)["state"] == INVALID_RECEIPT
    receipt = success_receipt()
    receipt["effects"]["modified"] = False
    assert classify_build_result(receipt)["state"] == INVALID_RECEIPT


def test_ok_with_non_success_code_is_invalid():
    receipt = success_receipt()
    receipt["code"] = "BUILD_FAILED"
    assert classify_build_result(receipt)["state"] == INVALID_RECEIPT


def test_success_without_pass_verdict_is_invalid():
    receipt = success_receipt()
    receipt["data"]["verification"]["verdict"] = "FAIL"
    assert classify_build_result(receipt)["state"] == INVALID_RECEIPT


def test_success_without_delivery_or_editability_is_invalid():
    receipt = success_receipt()
    del receipt["data"]["delivery"]
    assert classify_build_result(receipt)["state"] == INVALID_RECEIPT
    receipt = success_receipt()
    receipt["data"]["editability"]["passed"] = False
    assert classify_build_result(receipt)["state"] == INVALID_RECEIPT
    receipt = success_receipt()
    receipt["data"]["editability"]["verifiedParameterCount"] = 2
    assert classify_build_result(receipt)["state"] == INVALID_RECEIPT


def test_success_with_session_health_is_invalid():
    receipt = success_receipt()
    receipt["data"]["verification"]["sessionHealth"] = "unknown"
    assert classify_build_result(receipt)["state"] == INVALID_RECEIPT


def test_success_with_mismatched_step_path_is_invalid():
    receipt = success_receipt()
    receipt["data"]["delivery"]["stepPath"] = "C:\\out\\other.step"
    assert classify_build_result(receipt)["state"] == INVALID_RECEIPT


def test_retry_true_is_never_accepted():
    receipt = unknown_receipt()
    receipt["recovery"]["automatic_retry_allowed"] = True
    assert classify_build_result(receipt)["state"] == INVALID_RECEIPT


def test_malformed_receipt_is_invalid_not_success():
    verdict = classify_build_result({"ok": True, "code": "BUILD_COMPLETED"})
    assert verdict["state"] == INVALID_RECEIPT
    assert verdict["reasons"]


def test_geometry_claim_requires_explicit_verdict_not_field_presence():
    receipt = success_receipt()
    del receipt["data"]["verification"]
    verdict = classify_build_result(receipt)
    assert verdict["state"] == INVALID_RECEIPT
    assert any("verification" in reason for reason in verdict["reasons"])
