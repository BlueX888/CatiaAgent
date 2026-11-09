"""T08 CAD-support response contract: four outcomes stay distinct."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from catia_mcp.contracts.cad_support import (
    CANDIDATE_REJECTION,
    EVALUATION_FAILURE,
    INCONCLUSIVE,
    INVALID,
    RUN_FAILURE,
    SUCCESS,
    InconclusiveSummary,
    VolumeDiagnostics,
    classify_cad_support_response,
)

SUMMARY = {
    "reason": "INSUFFICIENT_CONSISTENT_BOOLEANS",
    "consistencyBudgetMm3": 0.01,
    "attemptCount": 10,
    "candidateCount": 0,
    "volumeDiagnostics": {"actualMm3": 500000.0, "referenceMm3": 500000.0,
                          "netDifferenceMm3": 0.0, "maxIntersectionExcessMm3": 0.5},
}


def success() -> dict:
    return {"ok": True, "operation": "evaluate", "exitCode": 0,
            "data": {"verdict": "PASS", "expected": {"volumeMm3": 100.0}}, "diagnostics": ""}


def inconclusive(summary=None) -> dict:
    return {"ok": False, "operation": "evaluate", "exitCode": 1,
            "code": "MATERIAL_COMPARISON_INCONCLUSIVE",
            "error": {"type": "MaterialComparisonInconclusive",
                      "message": "Material comparison could not establish a reliable bound.",
                      "summary": copy.deepcopy(SUMMARY if summary is None else summary)}}


def candidate_rejection() -> dict:
    return {"ok": False, "operation": "evaluate", "exitCode": 1, "code": "CANDIDATE_REJECTED",
            "error": {"type": "CandidateRejected", "message": "profile self-intersects"}}


def run_failure() -> dict:
    return {"ok": False, "operation": "evaluate", "code": "CAD_SUPPORT_TIMEOUT",
            "message": "Helper timed out; inspect output paths before retrying."}


def evaluation_failure() -> dict:
    return {"ok": False, "operation": "evaluate", "exitCode": 1,
            "error": {"type": "ValueError", "message": "invalid dimension"}}


def test_four_outcomes_are_distinct():
    assert classify_cad_support_response(success())["state"] == SUCCESS
    assert classify_cad_support_response(candidate_rejection())["state"] == CANDIDATE_REJECTION
    assert classify_cad_support_response(inconclusive())["state"] == INCONCLUSIVE
    assert classify_cad_support_response(run_failure())["state"] == RUN_FAILURE
    assert classify_cad_support_response(evaluation_failure())["state"] == EVALUATION_FAILURE


def test_inconclusive_is_never_converted_to_success():
    verdict = classify_cad_support_response(inconclusive())
    assert verdict["state"] == INCONCLUSIVE
    assert verdict["summary"].reason == "INSUFFICIENT_CONSISTENT_BOOLEANS"
    assert verdict["summary"].volumeDiagnostics.maxIntersectionExcessMm3 == 0.5
    # A contradictory success envelope carrying the inconclusive code is invalid.
    contradictory = inconclusive()
    contradictory["ok"] = True
    assert classify_cad_support_response(contradictory)["state"] == INVALID


def test_success_requires_an_explicit_verdict():
    value = success()
    del value["data"]["verdict"]
    assert classify_cad_support_response(value)["state"] == INVALID


# -- bounded summary ---------------------------------------------------------


def test_summary_rejects_missing_and_extra_fields():
    for field in ("reason", "consistencyBudgetMm3", "attemptCount", "candidateCount"):
        broken = copy.deepcopy(SUMMARY)
        del broken[field]
        with pytest.raises(ValidationError):
            InconclusiveSummary.model_validate(broken)
    extra = copy.deepcopy(SUMMARY)
    extra["unexpected"] = 1
    with pytest.raises(ValidationError):
        InconclusiveSummary.model_validate(extra)


@pytest.mark.parametrize("field,value", [
    ("consistencyBudgetMm3", 0), ("consistencyBudgetMm3", -1),
    ("consistencyBudgetMm3", float("nan")), ("consistencyBudgetMm3", float("inf")),
    ("attemptCount", 0), ("attemptCount", 11), ("attemptCount", 1.0), ("attemptCount", True),
    ("candidateCount", -1), ("candidateCount", 11), ("candidateCount", 1.0),
])
def test_summary_numeric_bounds(field, value):
    broken = copy.deepcopy(SUMMARY)
    broken[field] = value
    assert classify_cad_support_response(inconclusive(broken))["state"] == INVALID


def test_candidate_count_cannot_exceed_attempt_count():
    broken = copy.deepcopy(SUMMARY)
    broken["candidateCount"] = 11
    assert classify_cad_support_response(inconclusive(broken))["state"] == INVALID


def test_volume_diagnostics_are_closed_bounded_and_consistent():
    value = copy.deepcopy(SUMMARY)
    del value["volumeDiagnostics"]["maxIntersectionExcessMm3"]
    assert classify_cad_support_response(inconclusive(value))["state"] == INVALID
    value = copy.deepcopy(SUMMARY)
    value["volumeDiagnostics"]["actualMm3"] = 499999.0
    assert classify_cad_support_response(inconclusive(value))["state"] == INVALID
    value = copy.deepcopy(SUMMARY)
    value["volumeDiagnostics"]["extra"] = 1
    assert classify_cad_support_response(inconclusive(value))["state"] == INVALID
    value = copy.deepcopy(SUMMARY)
    value["volumeDiagnostics"] = None
    assert classify_cad_support_response(inconclusive(value))["state"] == INVALID


def test_volume_diagnostics_reject_nan_and_negative():
    for field, value in [("actualMm3", float("nan")), ("referenceMm3", -1),
                         ("netDifferenceMm3", float("inf")), ("maxIntersectionExcessMm3", -0.1)]:
        broken = copy.deepcopy(SUMMARY["volumeDiagnostics"])
        broken[field] = value
        with pytest.raises(ValidationError):
            VolumeDiagnostics.model_validate(broken)


def test_volume_diagnostics_accepts_a_valid_receipt():
    model = VolumeDiagnostics.model_validate(SUMMARY["volumeDiagnostics"])
    assert model.actualMm3 == 500000.0


def test_candidate_rejection_cannot_carry_an_inconclusive_summary():
    value = candidate_rejection()
    value["error"]["summary"] = SUMMARY
    assert classify_cad_support_response(value)["state"] == INVALID


def test_run_failure_must_not_carry_data():
    value = run_failure()
    value["data"] = {"verdict": "PASS"}
    assert classify_cad_support_response(value)["state"] == INVALID


def test_unknown_code_is_invalid():
    value = run_failure()
    value["code"] = "SOMETHING_NEW"
    assert classify_cad_support_response(value)["state"] == INVALID
