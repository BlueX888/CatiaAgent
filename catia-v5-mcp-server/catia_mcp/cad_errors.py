"""Exceptions shared by the CAD helper's module and command-line entry points."""

import math


class MaterialComparisonInconclusive(ValueError):
    """Boolean comparisons could not establish a material-difference bound."""

    def __init__(self, message, reason, budget, attempts, candidates, volumes=None):
        super().__init__(message)
        self.summary = {"reason": reason, "consistencyBudgetMm3": budget,
                        "attemptCount": len(attempts), "candidateCount": len(candidates)}
        if volumes is not None and len(volumes) == 2 and all(
                type(value) in (int, float) and math.isfinite(value) and value > 0 for value in volumes):
            common = [row.get("commonMm3") for row in attempts if isinstance(row, dict)]
            common = [value for value in common
                      if type(value) in (int, float) and math.isfinite(value) and value >= 0]
            difference = abs(volumes[0] - volumes[1])
            excess = max(0.0, max(common) - min(volumes)) if common else None
            if math.isfinite(difference) and excess is not None and math.isfinite(excess):
                self.summary["volumeDiagnostics"] = {
                    "actualMm3": volumes[0], "referenceMm3": volumes[1],
                    "netDifferenceMm3": difference, "maxIntersectionExcessMm3": excess,
                }
