"""Typed CAD-support subprocess responses.

The helper has four materially different outcomes that must not collapse into
one "error":

* evaluate success (explicit PASS/FAIL verdict with data);
* candidate rejection (the proposed geometry could not be built);
* run failure (timeout, start failure, protocol error, evaluation failure);
* inconclusive material comparison (bounded diagnostics, never a PASS).

Missing fields are structural errors: defaults such as zero would hide an
absent measurement.  Numerical consistency and the verdict remain the existing
workflow's judgement; this module only refuses malformed or contradictory
envelopes.
"""

from __future__ import annotations

import math
from typing import Any, Literal, Optional

from pydantic import ConfigDict, Field, StrictBool, StrictInt, model_validator

from .base import ContractModel, WireFloat, WireModel

INCONCLUSIVE_REASONS = {
    "COMMON_UNION_DISAGREES",
    "INSUFFICIENT_CONSISTENT_BOOLEANS",
    "FUZZY_LEVELS_DISAGREE",
}
RUN_FAILURE_CODES = {
    "CAD_SUPPORT_TIMEOUT",
    "CAD_SUPPORT_START_FAILED",
    "CAD_SUPPORT_PROTOCOL_ERROR",
    "CAD_SUPPORT_EVALUATION_FAILED",
    "CAD_SUPPORT_REQUEST_FAILED",
}


class ClosedModel(WireModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, validate_assignment=True)


class OpenModel(WireModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True, validate_assignment=True)


class VolumeDiagnostics(ClosedModel):
    actualMm3: WireFloat = Field(gt=0)
    referenceMm3: WireFloat = Field(gt=0)
    netDifferenceMm3: WireFloat = Field(ge=0)
    maxIntersectionExcessMm3: WireFloat = Field(ge=0)

    @model_validator(mode="after")
    def difference_matches(self) -> "VolumeDiagnostics":
        expected = abs(self.actualMm3 - self.referenceMm3)
        if not math.isclose(self.netDifferenceMm3, expected, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("netDifferenceMm3 must equal |actualMm3 - referenceMm3|")
        return self


class InconclusiveSummary(ClosedModel):
    reason: Literal[
        "COMMON_UNION_DISAGREES",
        "INSUFFICIENT_CONSISTENT_BOOLEANS",
        "FUZZY_LEVELS_DISAGREE",
    ]
    consistencyBudgetMm3: WireFloat = Field(gt=0)
    attemptCount: StrictInt = Field(ge=1, le=10)
    candidateCount: StrictInt = Field(ge=0)
    volumeDiagnostics: Optional[VolumeDiagnostics] = None

    @model_validator(mode="after")
    def counts_and_diagnostics_are_consistent(self) -> "InconclusiveSummary":
        if self.candidateCount > self.attemptCount:
            raise ValueError("candidateCount must not exceed attemptCount")
        if "volumeDiagnostics" in self.model_fields_set and self.volumeDiagnostics is None:
            raise ValueError("volumeDiagnostics, when present, must be an object")
        return self


class ErrorDetail(OpenModel):
    type: Optional[str] = None
    message: Optional[str] = None
    summary: Optional[dict[str, Any]] = None


class Envelope(OpenModel):
    ok: StrictBool
    operation: str = Field(min_length=1)
    code: Optional[str] = None
    exitCode: Optional[StrictInt] = None
    data: Optional[dict[str, Any]] = None
    error: Optional[ErrorDetail] = None
    message: Optional[str] = None
    diagnostics: Optional[str] = None


class EvaluateSuccess(Envelope):
    ok: Literal[True]  # type: ignore[assignment]
    code: Optional[str] = None
    exitCode: Literal[0]  # type: ignore[assignment]
    data: dict[str, Any]

    @model_validator(mode="after")
    def verdict_is_explicit(self) -> "EvaluateSuccess":
        if self.operation != "evaluate":
            raise ValueError("operation must be evaluate")
        if self.data.get("verdict") not in {"PASS", "FAIL"}:
            raise ValueError("evaluate data must carry an explicit PASS/FAIL verdict")
        return self


class CandidateRejected(Envelope):
    ok: Literal[False]  # type: ignore[assignment]
    code: Literal["CANDIDATE_REJECTED"]
    exitCode: Literal[1]  # type: ignore[assignment]
    error: ErrorDetail

    @model_validator(mode="after")
    def rejection_identity(self) -> "CandidateRejected":
        if self.operation != "evaluate":
            raise ValueError("candidate rejection must come from evaluate")
        if self.error.type != "CandidateRejected" or not self.error.message:
            raise ValueError("candidate rejection needs type=CandidateRejected and a message")
        if self.error.summary is not None:
            raise ValueError("candidate rejection must not carry an inconclusive summary")
        return self


class Inconclusive(Envelope):
    ok: Literal[False]  # type: ignore[assignment]
    code: Literal["MATERIAL_COMPARISON_INCONCLUSIVE"]
    exitCode: Literal[1]  # type: ignore[assignment]
    error: ErrorDetail
    summary: InconclusiveSummary

    @model_validator(mode="after")
    def identity(self) -> "Inconclusive":
        if self.operation != "evaluate":
            raise ValueError("inconclusive material comparison must come from evaluate")
        if self.error.type != "MaterialComparisonInconclusive":
            raise ValueError("error.type must be MaterialComparisonInconclusive")
        if not self.error.message:
            raise ValueError("inconclusive response needs a message")
        return self


class RunFailure(Envelope):
    ok: Literal[False]  # type: ignore[assignment]
    code: str

    @model_validator(mode="after")
    def failure_code_is_known(self) -> "RunFailure":
        if self.code not in RUN_FAILURE_CODES:
            raise ValueError(f"unknown run-failure code {self.code!r}")
        if self.data is not None:
            raise ValueError("a failed run must not carry data")
        return self


class EvaluationFailure(Envelope):
    """A domain rejection: the helper ran but the candidate/parameters failed."""

    ok: Literal[False]  # type: ignore[assignment]
    error: ErrorDetail

    @model_validator(mode="after")
    def has_a_reason(self) -> "EvaluationFailure":
        if self.code in RUN_FAILURE_CODES or self.code in {
                "MATERIAL_COMPARISON_INCONCLUSIVE", "CANDIDATE_REJECTED"}:
            raise ValueError("use the specific response type for this code")
        if self.data is not None:
            raise ValueError("an evaluation failure must not carry data")
        if not (self.error.message or self.message):
            raise ValueError("evaluation failure needs a message")
        return self


SUCCESS = "success"
CANDIDATE_REJECTION = "candidate_rejected"
RUN_FAILURE = "run_failure"
EVALUATION_FAILURE = "evaluation_failure"
INCONCLUSIVE = "inconclusive"
INVALID = "invalid"


def _inconclusive_summary(raw: dict[str, Any]) -> dict[str, Any]:
    error = raw.get("error") if isinstance(raw.get("error"), dict) else {}
    summary = error.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("inconclusive response has no summary")
    return summary


def classify_cad_support_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a typed outcome; never converts inconclusive into success."""
    if not isinstance(raw, dict):
        return {"state": INVALID, "response": None, "reasons": ["response is not an object"]}
    code = raw.get("code")
    if code == "MATERIAL_COMPARISON_INCONCLUSIVE":
        try:
            summary = InconclusiveSummary.model_validate(_inconclusive_summary(raw))
            response = Inconclusive.model_validate({**raw, "summary": summary.model_dump(exclude_none=True)})
            return {"state": INCONCLUSIVE, "response": response, "summary": summary, "reasons": []}
        except Exception as exc:
            return {"state": INVALID, "response": None, "reasons": [str(exc)]}
    if code == "CANDIDATE_REJECTED":
        try:
            return {"state": CANDIDATE_REJECTION, "response": CandidateRejected.model_validate(raw), "reasons": []}
        except Exception as exc:
            return {"state": INVALID, "response": None, "reasons": [str(exc)]}
    if raw.get("ok") is True:
        try:
            return {"state": SUCCESS, "response": EvaluateSuccess.model_validate(raw), "reasons": []}
        except Exception as exc:
            return {"state": INVALID, "response": None, "reasons": [str(exc)]}
    if code in RUN_FAILURE_CODES:
        try:
            return {"state": RUN_FAILURE, "response": RunFailure.model_validate(raw), "reasons": []}
        except Exception as exc:
            return {"state": INVALID, "response": None, "reasons": [str(exc)]}
    if raw.get("ok") is False and raw.get("data") is None:
        try:
            return {"state": EVALUATION_FAILURE,
                    "response": EvaluationFailure.model_validate(raw), "reasons": []}
        except Exception as exc:
            return {"state": INVALID, "response": None, "reasons": [str(exc)]}
    return {"state": INVALID, "response": None, "reasons": [f"unrecognised support response code {code!r}"]}
