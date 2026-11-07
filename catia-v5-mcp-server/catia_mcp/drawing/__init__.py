"""PDF drawing -> structured requirements -> parametric part build.

The package is split so every stage is testable without CATIA:

* :mod:`catia_mcp.drawing.pdf_evidence` reads a PDF and returns text spans with
  stable evidence ids. It is the only stage that touches the drawing.
* :mod:`catia_mcp.drawing.requirements` validates the model-authored
  ``catia-build-requirements/1`` document against that evidence.
* :mod:`catia_mcp.drawing.plan` compiles the normalized requirements into a
  closed, whitelisted ``catia-build-plan/1`` operation list.
* :mod:`catia_mcp.drawing.builder` executes the plan on CATIA and checks the
  geometry after every feature.
* :mod:`catia_mcp.drawing.verify` runs the independent verifier in a separate
  process; it never trusts the builder's own measurements.
"""

from __future__ import annotations

from catia_mcp.drawing.pdf_evidence import inspect_pdf, crop_pdf_region
from catia_mcp.drawing.requirements import REQUIREMENTS_SCHEMA, validate_requirements
from catia_mcp.drawing.plan import PLAN_SCHEMA, compile_build_plan
from catia_mcp.drawing.builder import BuildExecutor
from catia_mcp.drawing.verify import run_independent_verifier

__all__ = [
    "inspect_pdf",
    "crop_pdf_region",
    "REQUIREMENTS_SCHEMA",
    "validate_requirements",
    "PLAN_SCHEMA",
    "compile_build_plan",
    "BuildExecutor",
    "run_independent_verifier",
]
