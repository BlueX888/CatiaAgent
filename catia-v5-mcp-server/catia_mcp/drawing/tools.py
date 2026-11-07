"""MCP tools for the PDF drawing -> parametric part workflow.

Workflow and tool boundaries:

``catia_inspect_drawing``      PDF -> evidence manifest (span ids + boxes)
``catia_crop_drawing``         one region -> PNG the agent can read visually
``catia_validate_requirements`` model's structured reading -> decision/questions
``catia_compile_build_plan``   validated requirements -> whitelisted steps
``catia_build_part``           execute + independent verify + save + edit check
``catia_verify_part``          independent verification of an existing file

Nothing in this module paraphrases a dimension: the numbers always come from
the requirement document, and the validator checks each one against the
evidence span the model cited.
"""

from __future__ import annotations

import json
import copy
import os
from pathlib import Path
from typing import Any, Callable

from catia_mcp.connection import CATIAConnection
from catia_mcp.drawing.builder import BuildError, BuildExecutor
from catia_mcp.drawing.pdf_evidence import (
    crop_pdf_region, inspect_pdf, query_context_geometry,
    query_context_reference_geometry,
)
from catia_mcp.drawing.block_context import query_context_blocks
from catia_mcp.drawing.plan import PlanError, compile_build_plan, plan_summary
from catia_mcp.drawing.requirements import REQUIREMENTS_SCHEMA, validate_requirements
from catia_mcp.drawing.verify import run_independent_verifier
from catia_mcp.locking import AutomationBusy, automation_lock
from catia_mcp.results import operation_result
from catia_mcp.drawing.feature_schema import FEATURE_REQUIREMENTS_JSON
from catia_mcp.drawing.feature_model import PLAN_SCHEMA as FEATURE_PLAN_SCHEMA

_REQUIREMENTS_SCHEMA_JSON = {
    "type": "object",
    "description": (
        "catia-build-requirements/1. The structured reading of the drawing. Every dimension "
        "should cite one or more span ids from catia_inspect_drawing under evidence{}."
    ),
    "properties": {
        "schema": {"type": "string", "enum": [REQUIREMENTS_SCHEMA]},
        "unit": {"type": "string", "description": "'mm' or 'inch', as stated on the drawing"},
        "partName": {"type": "string"},
        "drawing": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "page": {"type": "integer"},
                "sha256": {"type": "string"},
            },
        },
        "body": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["flat_plate", "annular_ring", "extruded_profile"]},
                "length": {"type": "number", "description": "flat_plate only"},
                "width": {"type": "number", "description": "flat_plate only"},
                "thickness": {"type": "number", "description": "plate/ring web thickness"},
                "outerDiameter": {"type": "number", "description": "annular_ring only"},
                "innerDiameter": {"type": "number", "description": "annular_ring only"},
                "edge": {
                    "type": "object",
                    "description": "annular_ring rolled edge",
                    "properties": {
                        "style": {"type": "string", "enum": ["rolled", "round"]},
                        "radius": {"type": "number"},
                    },
                },
                "profile": {
                    "type": "array",
                    "description": "extruded_profile: closed loop of line/arc segments in model XY (mm)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["line", "arc"]},
                            "x1": {"type": "number"}, "y1": {"type": "number"},
                            "x2": {"type": "number"}, "y2": {"type": "number"},
                            "cx": {"type": "number"}, "cy": {"type": "number"},
                            "radius": {"type": "number"},
                            "startAngle": {"type": "number", "description": "degrees, CCW"},
                            "endAngle": {"type": "number", "description": "degrees, CCW"},
                        },
                    },
                },
            },
            "required": ["type"],
        },
        "holes": {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "diameter": {"type": "number"},
                "through": {"type": "boolean"},
                "pattern": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["symmetric", "explicit"]},
                        "edgeX": {"type": "number"},
                        "edgeY": {"type": "number"},
                        "centres": {
                            "type": "array",
                            "items": {"type": "array", "items": {"type": "number"}},
                            "description": "Model coordinates [x, y] measured from the plate centre",
                        },
                    },
                },
            },
        },
        "evidence": {
            "type": "object",
            "description": "field -> [span ids], e.g. {\"length\": [\"p1:s12\"]}",
            "additionalProperties": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
        },
        "clarifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "source": {"type": "string"},
                },
            },
        },
        "conflicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "message": {"type": "string"},
                    "question": {"type": "string"},
                    "resolved": {"type": "boolean"},
                    "answer": {"type": "string"},
                },
            },
        },
    },
    "required": ["schema", "body"],
}

_EXPECTATION_JSON = {
    "type": "object",
    "description": (
        "Frozen expected geometry. Use the verification-side values, not the builder's "
        "measurements. Omit fields that are not being checked."
    ),
    "properties": {
        "volumeMm3": {"type": "number"},
        "bbox": {
            "type": "object",
            "properties": {
                "min": {"type": "object"},
                "max": {"type": "object"},
            },
        },
        "holes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "diameter": {"type": "number"},
                    "through": {"type": "boolean"},
                },
                "required": ["x", "y", "diameter"],
            },
        },
        "featureCount": {"type": "integer"},
        "parameters": {"type": "object"},
    },
}


class GenerationBuildConnectionError(RuntimeError):
    """CATIA connection failed before a generation build could modify a model."""


class DrawingTools:
    """PDF drawing intake and from-scratch build tools."""

    def __init__(self, connection: CATIAConnection, *, before_build: Callable[[], dict[str, Any] | None] | None = None,
                 reference_evidence: Callable[[], dict[str, Any]] | None = None,
                 input_snapshots: Callable[[bool], tuple[bytes, bytes | None] | None] | None = None) -> None:
        self.conn = connection
        self.before_build = before_build
        self.reference_evidence = reference_evidence
        self.input_snapshots = input_snapshots
        self._trusted_evidence = {} if input_snapshots else None

    # ------------------------------------------------------------------
    def get_tool_definitions(self) -> list[dict[str, Any]]:
        definitions = [
            {
                "name": "catia_inspect_drawing",
                "description": (
                    "Read a PDF engineering drawing and return every text span with a stable "
                    "evidence id (p<page>:s<index>) and a millimetre bounding box, plus numeric "
                    "dimension candidates and unit hints. The manifest is written to disk and "
                    "its path is returned as evidencePath. Use this before writing build "
                    "requirements so every dimension can cite a real span. With includeOcr=true "
                    "the page is rendered and OCR-read as well, giving ids p<page>:o<index> for "
                    "dimensions drawn as vector outlines or images (NIST FTC/CTC sheets). Optional "
                    "drawingContextPath adds separate ctx: evidence from explicitly supplied 2D drawing records."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path to the PDF"},
                        "pages": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "1-based page numbers; omit for all pages",
                        },
                        "evidenceDir": {
                            "type": "string",
                            "description": "Optional directory for the evidence manifest",
                        },
                        "includeOcr": {
                            "type": "boolean",
                            "default": False,
                            "description": "Render each page and add OCR spans for vector/image dimensions",
                        },
                        "drawingContextPath": {
                            "type": "string",
                            "description": "Optional staged 2D drawing context JSON bound to this PDF; returns separate ctx: evidence IDs.",
                        },
                        "geometryQuery": {
                            "type": "object", "additionalProperties": False,
                            "description": "Page optional 2D geometry from drawingContextPath; frames are not automatically transformed.",
                            "properties": {"frame": {"type": "string"}, "coordinateSystem": {"type": "string"}, "kind": {"type": "string"},
                                           "bbox": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "number"},
                                                    "description": "Optional [xmin,ymin,xmax,ymax] in the selected source frame; unknown bounds remain included."},
                                           "offset": {"type": "integer", "minimum": 0},
                                           "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                           "summaryOnly": {"type": "boolean"}},
                        },
                        "blockQuery": {
                            "type": "object", "additionalProperties": False,
                            "description": "Page unexpanded 2D block definitions or instances from drawingContextPath. Frames are not transformed, external references and visibility are not resolved, and these records are not dimension evidence. Definitions omit entity IDs unless includeMembers is requested for one definitionId.",
                            "properties": {
                                "kind": {"enum": ["definitions", "instances"], "default": "instances"},
                                "frame": {"type": "string"}, "coordinateSystem": {"type": "string"},
                                "definitionId": {"type": "string"}, "entityId": {"type": "string"},
                                "offset": {"type": "integer", "minimum": 0},
                                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                "summaryOnly": {"type": "boolean"}, "includeMembers": {"type": "boolean"},
                                "memberOffset": {"type": "integer", "minimum": 0},
                                "memberLimit": {"type": "integer", "minimum": 1, "maximum": 100},
                            },
                        },
                        "layerQuery": {
                            "type": "object", "additionalProperties": False,
                            "description": "Look up one raw 2D layer by layerId. Its name and line type do not prove actual visibility, geometry or PDF dimensions.",
                            "properties": {"layerId": {"type": "string", "maxLength": 134}},
                            "required": ["layerId"],
                        },
                        "referenceGeometryQuery": {
                            "type": "object", "additionalProperties": False,
                            "description": "Page canonical STEP-derived reference geometry from drawingContextPath by kind. Returns spanId, text and finite numeric values for citations. The default inspect reply only summarizes these records.",
                            "properties": {
                                "kind": {"type": "string", "maxLength": 40},
                                "offset": {"type": "integer", "minimum": 0},
                                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                "summaryOnly": {"type": "boolean"},
                            },
                        },
                        "circleMeasurement": {
                            "type": "object", "additionalProperties": False,
                            "description": "Derive an approximate diameter or signed same-frame center offset from 2D circles and a matching radial dimension; cite the returned separate derived-geometry evidence.",
                            "properties": {
                                "targetCircleId": {"type": "string"},
                                "referenceCircleId": {"type": "string"},
                                "referenceDimensionId": {"type": "string"},
                                "quantity": {"enum": ["diameter", "offsetX", "offsetY"], "default": "diameter"},
                            },
                            "required": ["targetCircleId", "referenceCircleId", "referenceDimensionId"],
                        },
                        "geometryOnly": {
                            "type": "boolean", "default": False,
                            "description": "With geometryQuery, blockQuery, layerQuery or circleMeasurement, return only its result and source/evidence paths; the full evidence manifest is still preserved.",
                        },
                        "compactSpans": {
                            "type": "boolean",
                            "default": False,
                            "description": "Return only span id/text (no duplicated page text, candidates or drawing paths) to keep large schedules within model tool-result limits",
                        },
                        "ocrDpi": {"type": "integer", "default": 400},
                        "ocrMinConfidence": {"type": "number", "default": 0.5},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "catia_crop_drawing",
                "description": (
                    "Render one rectangular region of a PDF drawing page to PNG so a view, a "
                    "dimension chain or a title block can be inspected visually. Coordinates are "
                    "millimetres from the page's top-left corner."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "page": {"type": "integer", "description": "1-based page number"},
                        "bboxMm": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                            "description": "[x0, y0, x1, y1] millimetres",
                        },
                        "dpi": {"type": "integer", "default": 200},
                        "outputPath": {"type": "string"},
                    },
                    "required": ["path", "page", "bboxMm"],
                },
            },
            {
                "name": "catia_validate_requirements",
                "description": (
                    "Validate a catia-build-requirements/1 document against the drawing evidence. "
                    "Returns decision ready/needs_clarification/unsupported/invalid, the questions "
                    "to ask the user (one round), per-field evidence checks, and the normalized "
                    "millimetre geometry. No CATIA model is touched."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "requirements": _REQUIREMENTS_SCHEMA_JSON,
                        "evidencePath": {
                            "type": "string",
                            "description": "Manifest returned by catia_inspect_drawing",
                        },
                        "requireEvidence": {"type": "boolean", "default": True},
                    },
                    "required": ["requirements"],
                },
            },
            {
                "name": "catia_compile_build_plan",
                "description": (
                    "Compile validated requirements into a catia-build-plan/1 operation list. "
                    "Refuses with the same decision/questions as catia_validate_requirements when "
                    "the reading is incomplete or out of scope. Read-only: nothing is built."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "requirements": _REQUIREMENTS_SCHEMA_JSON,
                        "evidencePath": {"type": "string"},
                        "requireEvidence": {"type": "boolean", "default": True},
                    },
                    "required": ["requirements"],
                },
            },
            {
                "name": "catia_build_part",
                "description": (
                    "Build a parametric CATPart from validated drawing requirements: new part, "
                    "named parameters, sketch constraints, pad, through-hole pocket, save to a NEW "
                    "path, then independent reopen verification and a copy-based editability check. "
                    "Refuses to overwrite an existing file or to build before requirements are ready."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "requirements": _REQUIREMENTS_SCHEMA_JSON,
                        "evidencePath": {"type": "string"},
                        "deliveryPath": {
                            "type": "string",
                            "description": "Absolute path to a new .CATPart file (must not exist)",
                        },
                        "editCheck": {
                            "type": "boolean",
                            "default": True,
                            "description": "Modify a throw-away copy and verify geometry follows",
                        },
                        "expected": {
                            **_EXPECTATION_JSON,
                            "description": (
                                "Optional frozen expectation for the independent verifier. "
                                "Defaults to the values compiled from the requirements."
                            ),
                        },
                    },
                    "required": ["requirements", "deliveryPath"],
                },
            },
            {
                "name": "catia_verify_part",
                "description": (
                    "Independently verify an existing CATPart in a separate process: reopen from "
                    "disk, measure volume, bounding box and cylindrical faces, and compare with a "
                    "frozen expectation. Does not rely on the build plan or the builder's checks."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "partPath": {"type": "string"},
                        "expected": _EXPECTATION_JSON,
                        "tolerances": {
                            "type": "object",
                            "properties": {
                                "lengthMm": {"type": "number"},
                                "coordinateMm": {"type": "number"},
                                "volumeMm3": {"type": "number"},
                            },
                        },
                    },
                    "required": ["partPath", "expected"],
                },
            },
        ]
        for definition in definitions:
            properties = definition["inputSchema"]["properties"]
            if "requirements" in properties:
                properties["requirements"] = {"oneOf": [_REQUIREMENTS_SCHEMA_JSON, FEATURE_REQUIREMENTS_JSON]}
                definition["description"] += (
                    " Also accepts catia-feature-requirements/1: ordered pad/pocket/shaft/groove plus "
                    "fillet/chamfer with geometric edge selectors (linear: direction + one point on the line; "
                    "circular: radius + centre + axis), rectangle/circle/polygon/line-arc wire profiles, "
                    "xy/yz/zx or fixed orthonormal {normal:[nx,ny,nz],xDirection:[hx,hy,hz]} planes with signed normal offsets and evidence-backed expressions. Frame orientation is fixed; offset is parametric. An edge selector must match exactly "
                    "one edge of the current solid or the build fails with EDGE_SELECTION_MISMATCH. "
                    "Fillets use tangency propagation and may use 2-64 unique edge selectors as one edges array to round connected roots together; chamfer remains single-edge. "
                    "Feature builds require configured isolated CAD support, automatic geometry verification "
                    "and editCheck=true; expected overrides are rejected.")
        return definitions

    # ------------------------------------------------------------------
    def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        match tool_name:
            case "catia_inspect_drawing":
                return self._inspect(arguments)
            case "catia_crop_drawing":
                return self._crop(arguments)
            case "catia_validate_requirements":
                return self._validate(arguments)
            case "catia_compile_build_plan":
                return self._compile(arguments)
            case "catia_build_part":
                return self._build(arguments)
            case "catia_verify_part":
                return self._verify(arguments)
            case _:
                raise ValueError(f"Unknown drawing tool: {tool_name}")

    # ------------------------------------------------------------------
    def _evidence_for(
        self,
        requirements: dict[str, Any],
        arguments: dict[str, Any],
        *,
        require: bool,
    ) -> Any:
        evidence_path = arguments.get("evidencePath")
        if evidence_path:
            if self._trusted_evidence is not None:
                evidence = self._trusted_evidence.get(str(Path(evidence_path)))
                if evidence is None:
                    raise ValueError("Evidence was not produced by this generation session.")
                from catia_mcp.drawing.pdf_evidence import _bound_evidence_bytes, _check_bound_evidence
                expected = _bound_evidence_bytes(evidence)
                _check_bound_evidence(Path(evidence_path), expected)
                return {**copy.deepcopy(evidence), "evidencePath": str(evidence_path)}
            return evidence_path
        drawing = requirements.get("drawing") or {}
        drawing_path = drawing.get("path")
        if drawing_path and Path(str(drawing_path)).is_file():
            manifest = inspect_pdf(str(drawing_path))
            return manifest
        return None

    def _validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        requirements = arguments["requirements"]
        try:
            evidence = self._evidence_for(requirements, arguments, require=True)
            result = validate_requirements(
                requirements,
                evidence,
                require_evidence=bool(arguments.get("requireEvidence", True)),
                reference_evidence=self.reference_evidence() if self.reference_evidence else None,
            )
        except Exception as exc:
            return operation_result(
                tool="catia_validate_requirements", ok=False, code="REQUIREMENTS_INVALID",
                message=str(exc), data={}, modified=False,
            )
        data = {
            "decision": result["decision"],
            "questions": result["questions"],
            "problems": result["problems"],
            "warnings": result["warnings"],
            "evidenceChecks": result.get("evidenceChecks", []),
            "normalized": result["normalized"],
            "evidencePath": evidence if isinstance(evidence, str) else (
                evidence.get("evidencePath") if isinstance(evidence, dict) else None
            ),
        }
        code = {
            "ready": "REQUIREMENTS_READY",
            "needs_clarification": "NEEDS_CLARIFICATION",
            "unsupported": "UNSUPPORTED_CAPABILITY",
            "invalid": "REQUIREMENTS_INVALID",
        }[result["decision"]]
        return operation_result(
            tool="catia_validate_requirements",
            ok=result["decision"] == "ready",
            code=code,
            message=_decision_message(result),
            data=data,
            modified=False,
        )

    def _compile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        requirements = arguments["requirements"]
        try:
            evidence = self._evidence_for(requirements, arguments, require=True)
            validated = validate_requirements(
                requirements, evidence,
                require_evidence=bool(arguments.get("requireEvidence", True)),
                reference_evidence=self.reference_evidence() if self.reference_evidence else None,
            )
        except Exception as exc:
            return operation_result(
                tool="catia_compile_build_plan", ok=False, code="REQUIREMENTS_INVALID",
                message=str(exc), data={}, modified=False,
            )
        if validated["decision"] != "ready":
            code = {
                "needs_clarification": "NEEDS_CLARIFICATION",
                "unsupported": "UNSUPPORTED_CAPABILITY",
                "invalid": "REQUIREMENTS_INVALID",
            }[validated["decision"]]
            return operation_result(
                tool="catia_compile_build_plan", ok=False, code=code,
                message=_decision_message(validated),
                data={
                    "decision": validated["decision"],
                    "questions": validated["questions"],
                    "problems": validated["problems"],
                    "normalized": validated["normalized"],
                },
                modified=False,
            )
        try:
            plan = compile_build_plan(validated["normalized"])
        except PlanError as exc:
            return operation_result(
                tool="catia_compile_build_plan", ok=False, code="PLAN_UNSUPPORTED",
                message=str(exc), data={"normalized": validated["normalized"]}, modified=False,
            )
        return operation_result(
            tool="catia_compile_build_plan", ok=True, code="PLAN_READY",
            message=plan_summary(plan),
            data={
                "decision": "ready",
                "plan": plan,
                "summary": plan_summary(plan),
                "normalized": validated["normalized"],
                "evidencePath": evidence if isinstance(evidence, str) else (
                    evidence.get("evidencePath") if isinstance(evidence, dict) else None
                ),
            },
            modified=False,
        )

    def _build(self, arguments: dict[str, Any]) -> dict[str, Any]:
        requirements = arguments["requirements"]
        delivery_path = str(arguments["deliveryPath"])
        stages: list[dict[str, Any]] = []
        tool = "catia_build_part"

        try:
            evidence = self._evidence_for(requirements, arguments, require=True)
            validated = validate_requirements(
                requirements, evidence,
                require_evidence=bool(arguments.get("requireEvidence", True)),
                reference_evidence=self.reference_evidence() if self.reference_evidence else None,
            )
        except Exception as exc:
            return operation_result(
                tool=tool, ok=False, code="REQUIREMENTS_INVALID", message=str(exc),
                data={"stages": stages}, modified=False,
            )
        stages.append(
            {
                "stage": "requirements",
                "status": "passed" if validated["decision"] == "ready" else "blocked",
                "decision": validated["decision"],
                "questions": validated["questions"],
                "problems": validated["problems"],
            }
        )
        if validated["decision"] != "ready":
            code = {
                "needs_clarification": "NEEDS_CLARIFICATION",
                "unsupported": "UNSUPPORTED_CAPABILITY",
                "invalid": "REQUIREMENTS_INVALID",
            }[validated["decision"]]
            return operation_result(
                tool=tool, ok=False, code=code,
                message=_decision_message(validated),
                data={
                    "stages": stages,
                    "decision": validated["decision"],
                    "questions": validated["questions"],
                    "problems": validated["problems"],
                },
                modified=False,
            )
        try:
            plan = compile_build_plan(validated["normalized"])
        except PlanError as exc:
            stages.append({"stage": "plan", "status": "failed", "detail": str(exc)})
            return operation_result(
                tool=tool, ok=False, code="PLAN_UNSUPPORTED", message=str(exc),
                data={"stages": stages}, modified=False,
            )
        stages.append({"stage": "plan", "status": "passed", "planId": plan["planId"],
                       "summary": plan_summary(plan), "steps": len(plan.get("steps", plan.get("features", [])))})

        target = Path(delivery_path).expanduser()
        if target.exists():
            stages.append({"stage": "delivery", "status": "blocked",
                           "detail": f"file already exists: {target}"})
            return operation_result(
                tool=tool, ok=False, code="SAVE_CONFLICT",
                message=f"Refusing to overwrite an existing file: {target}",
                data={"stages": stages}, modified=False,
            )

        try:
            with automation_lock(timeout=float(os.environ.get("CATIA_MCP_LOCK_TIMEOUT", "900"))):
                # Another build may have created the target while this call waited for the lock.
                if target.exists():
                    stages.append({"stage": "delivery", "status": "blocked",
                                   "detail": f"file already exists: {target}"})
                    return operation_result(
                        tool=tool, ok=False, code="SAVE_CONFLICT",
                        message=f"Refusing to overwrite an existing file: {target}",
                        data={"stages": stages}, modified=False,
                    )
                if self.before_build is not None:
                    blocked = self.before_build()
                    if blocked is not None:
                        return blocked
                return self._execute_build(plan, target, arguments, stages, tool)
        except AutomationBusy as exc:
            stages.append({"stage": "modeling", "status": "blocked", "detail": str(exc)})
            return operation_result(
                tool=tool, ok=False, code="AUTOMATION_BUSY", message=str(exc),
                data={"stages": stages, "planId": plan["planId"]}, modified=False,
            )

    def _execute_build(
        self,
        plan: dict[str, Any],
        target: Path,
        arguments: dict[str, Any],
        stages: list[dict[str, Any]],
        tool: str,
    ) -> dict[str, Any]:
        executor = BuildExecutor(self.conn)
        if plan.get("schema") == FEATURE_PLAN_SCHEMA:
            from catia_mcp.drawing.feature_workflow import build_features
            return build_features(self.conn, arguments["requirements"], plan, target, stages, arguments)
        try:
            built = executor.execute(plan)
        except BuildError as exc:
            stages.append({"stage": "modeling", "status": "failed", "detail": str(exc),
                           "failedStep": exc.stage, "scene": exc.scene,
                           "steps": executor.steps})
            executor.close()
            return operation_result(
                tool=tool, ok=False, code="BUILD_FAILED",
                message=f"Build stopped at {exc.stage}: {exc}",
                data={"stages": stages, "planId": plan["planId"]}, modified=True,
            )
        # Keep the result small enough for the chat bridge's 20 kB preview cap:
        # the full 31-step plan is already returned by compile_build_plan, so
        # echo only the count and the parameter bindings here.
        stages.append({"stage": "modeling", "status": "passed",
                       "stepCount": len(built["steps"]), "bindings": built["bindings"],
                       "final": built["final"]})

        try:
            delivery = executor.save(str(target))
        except BuildError as exc:
            stages.append({"stage": "delivery", "status": "failed", "detail": str(exc)})
            executor.close()
            return operation_result(
                tool=tool, ok=False, code="SAVE_FAILED", message=str(exc),
                data={"stages": stages, "planId": plan["planId"]}, modified=True,
            )

        expected_kind = plan["expected"].get("kind")
        is_ring = expected_kind == "annular_ring"
        if is_ring:
            expectation = arguments.get("expected") or {
                "kind": "annular_ring",
                "volumeMm3": plan["expected"]["volumeMm3"],
                "bbox": plan["expected"]["bbox"],
                "cylinders": plan["expected"]["cylinders"],
                "featureCount": plan["expected"]["featureCount"],
                "parameters": plan["expected"]["parameters"],
            }
        else:
            expectation = arguments.get("expected") or {
                "volumeMm3": plan["expected"]["volumeMm3"],
                "bbox": plan["expected"]["bbox"],
                "holes": plan["expected"]["holes"],
                "featureCount": plan["expected"]["featureCount"],
                "parameters": plan["expected"]["parameters"],
            }
        if expected_kind == "extruded_profile":
            tolerances = arguments.get("tolerances") or {
                "lengthMm": 0.1, "coordinateMm": 0.01, "volumeMm3": 1.0,
            }
        elif is_ring:
            tolerances = arguments.get("tolerances") or {
                "lengthMm": 0.05, "coordinateMm": 0.01, "volumeMm3": 5.0,
            }
        else:
            tolerances = arguments.get("tolerances")
        # The verifier child is spawned with stdin=DEVNULL; an inherited MCP
        # stdio pipe made it block before its first statement (踩坑 94). Do not
        # disconnect COM here: CoUninitialize/CoInitialize around every build
        # crashes CATIA on the second build in one process.
        # Export the independently reopened part to a fresh path so downstream
        # geometric comparison can use exactly the delivered CATPart.
        from uuid import uuid4
        step_path = target.parent / f"{target.stem}-{uuid4().hex}.step"
        verification = run_independent_verifier(
            delivery["path"], expectation, tolerances=tolerances, export_step=str(step_path)
        )
        verified = verification.get("verdict") == "PASS" and verification.get("sessionHealth") != "unknown"
        stages.append({"stage": "verification", "status": "passed" if verified else "failed",
                       "verdict": verification.get("verdict"),
                       "checks": verification.get("checks", [])})
        if verification.get("sessionHealth") == "unknown":
            return operation_result(
                tool=tool, ok=False, code="VERIFIER_SESSION_UNKNOWN", modified=True,
                message="CATIA session state is unknown after independent verification; inspect before further automation.",
                data={"businessState": "session_unknown", "sessionHealth": "unknown",
                      "planId": plan["planId"], "stages": stages,
                      "verification": verification, "delivery": delivery},
            )

        editability = None
        if verified and arguments.get("editCheck", True):
            try:
                editability = executor.editability_check(delivery["path"])
                stages.append({"stage": "editability", "status": "passed",
                               "detail": editability})
            except BuildError as exc:
                editability = {"passed": False, "error": str(exc)}
                stages.append({"stage": "editability", "status": "failed", "detail": str(exc)})

        completed = verified and (editability is None or editability.get("passed", False))
        if completed:
            from catia_mcp.drawing.preview import create_part_preview
            delivery["preview"] = create_part_preview(delivery["path"], expectation, step_path=step_path, tolerances=tolerances)
        reports = _write_reports(
            delivery=delivery,
            plan=plan,
            verification=verification,
            editability=editability,
            completed=completed,
        )
        delivery.update(reports)
        stages.append({
            "stage": "delivery",
            "status": "completed" if completed else "blocked",
            "path": delivery["path"],
            "sha256": delivery["sha256"],
            "sizeBytes": delivery["sizeBytes"],
            "reportJson": reports.get("reportJson"),
            "reportMarkdown": reports.get("reportMarkdown"),
        })
        data = {
            "businessState": "completed" if completed else "verification_failed",
            "planId": plan["planId"],
            "summary": plan_summary(plan),
            "stages": stages,
            "verification": verification,
            "editability": editability,
            "delivery": delivery,
        }
        return operation_result(
            tool=tool,
            ok=completed,
            code="BUILD_COMPLETED" if completed else "VERIFICATION_FAILED",
            message=(
                f"Built and independently verified {target.name}."
                if completed
                else "The part was saved but independent verification did not pass; "
                     "this is not a completed delivery."
            ),
            data=data,
            modified=True,
        )

    def _verify(self, arguments: dict[str, Any]) -> dict[str, Any]:
        report = run_independent_verifier(
            arguments["partPath"],
            arguments["expected"],
            tolerances=arguments.get("tolerances"),
        )
        session_unknown = report.get("sessionHealth") == "unknown"
        verified = report.get("verdict") == "PASS" and not session_unknown
        return operation_result(
            tool="catia_verify_part",
            ok=verified,
            code="VERIFIED" if verified else ("VERIFIER_SESSION_UNKNOWN" if session_unknown else "VERIFICATION_FAILED"),
            message=(
                "Independent verification passed."
                if verified else "CATIA session state is unknown; inspect before further automation."
                if session_unknown else "Independent verification failed; see failed checks."
            ),
            data={"report": report, **({"sessionHealth": "unknown"} if session_unknown else {})},
            modified=None if session_unknown else False,
        )

    # ------------------------------------------------------------------
    def _inspect(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            snapshot = self.input_snapshots(bool(arguments.get("drawingContextPath"))) if self.input_snapshots else None
        except (OSError, ValueError) as exc:
            return operation_result(tool="catia_inspect_drawing", ok=False, code="DRAWING_UNREADABLE",
                                    message=str(exc), data={}, modified=False)
        pdf_bytes, context_bytes = snapshot if snapshot is not None else (None, None)
        requested_ocr = bool(arguments.get("includeOcr", False))
        declares_text_layer = False
        if requested_ocr:
            # A derived schedule declares its text layer on page 1. OCR over
            # dozens of dense pages would exceed the tool timeout and add no
            # information; honour the declaration and read the spans instead.
            try:
                import pymupdf
                with (pymupdf.open(arguments["path"]) if pdf_bytes is None else
                      pymupdf.open(stream=pdf_bytes, filetype="pdf")) as document:
                    head = document[0].get_text() if document.page_count else ""
                declares_text_layer = "TEXT LAYER" in head.upper()
            except Exception:
                declares_text_layer = False
        try:
            if arguments.get("geometryOnly") and not ({"geometryQuery", "blockQuery", "layerQuery", "circleMeasurement", "referenceGeometryQuery"} & arguments.keys()):
                raise ValueError("geometryOnly requires geometryQuery, blockQuery, layerQuery, referenceGeometryQuery or circleMeasurement")
            if arguments.get("geometryOnly") and requested_ocr:
                raise ValueError("geometryOnly cannot be combined with includeOcr")
            evidence_key = str(Path(arguments["evidenceDir"]) / "evidence.json") if self._trusted_evidence is not None else None
            trusted = ({"trusted_previous": copy.deepcopy(self._trusted_evidence.get(evidence_key))}
                       if self._trusted_evidence is not None else {})
            manifest = inspect_pdf(
                arguments["path"],
                arguments.get("pages"),
                arguments.get("evidenceDir"),
                include_ocr=requested_ocr and not declares_text_layer,
                ocr_dpi=int(arguments.get("ocrDpi", 400)),
                ocr_min_confidence=float(arguments.get("ocrMinConfidence", 0.5)),
                context_path=arguments.get("drawingContextPath"),
                pdf_bytes=pdf_bytes, context_bytes=context_bytes,
                **trusted,
            )
            trusted_stored = manifest.pop("_trustedStored", None)
            geometry = None
            blocks = None
            layers = None
            reference_geometry = None
            measurement = None
            context_sha = (manifest.get("contextSource") or {}).get("sha256")
            if ({"geometryQuery", "blockQuery", "layerQuery", "circleMeasurement", "referenceGeometryQuery"} & arguments.keys()
                    and not context_sha):
                raise ValueError("Drawing queries require a validated drawing context.")
            if "geometryQuery" in arguments:
                if not arguments.get("drawingContextPath"):
                    raise ValueError("geometryQuery requires drawingContextPath")
                geometry = query_context_geometry(arguments["drawingContextPath"], manifest["source"]["sha256"],
                                                  arguments["geometryQuery"], context_sha, data=context_bytes)
            if "blockQuery" in arguments:
                if not arguments.get("drawingContextPath"):
                    raise ValueError("blockQuery requires drawingContextPath")
                blocks = query_context_blocks(arguments["drawingContextPath"], manifest["source"]["sha256"],
                                              arguments["blockQuery"], context_sha, data=context_bytes)
            if "layerQuery" in arguments:
                if not arguments.get("drawingContextPath"):
                    raise ValueError("layerQuery requires drawingContextPath")
                from catia_mcp.drawing.layer_context import query_context_layers
                layers = query_context_layers(arguments["drawingContextPath"], manifest["source"]["sha256"],
                                              arguments["layerQuery"], context_sha, data=context_bytes)
            if "referenceGeometryQuery" in arguments:
                if not arguments.get("drawingContextPath"):
                    raise ValueError("referenceGeometryQuery requires drawingContextPath")
                reference_geometry = query_context_reference_geometry(
                    arguments["drawingContextPath"], manifest["source"]["sha256"],
                    arguments["referenceGeometryQuery"], context_sha, data=context_bytes,
                )
            if "circleMeasurement" in arguments:
                if not arguments.get("drawingContextPath"):
                    raise ValueError("circleMeasurement requires drawingContextPath")
                from catia_mcp.drawing.circle_evidence import derive_circle_measurement
                measurement = derive_circle_measurement(arguments["drawingContextPath"], manifest["source"]["sha256"],
                                                    arguments["circleMeasurement"], context_sha, data=context_bytes)
                evidence_path = Path(manifest["evidencePath"])
                stored = trusted_stored if self._trusted_evidence is not None else json.loads(evidence_path.read_text(encoding="utf-8"))
                spans = stored.setdefault("contextEvidence", [])
                existing = [span for span in spans if span.get("id") == measurement["id"]]
                if existing and existing != [measurement]:
                    raise ValueError("Derived evidence id already exists with different content.")
                if not existing:
                    previous = copy.deepcopy(stored) if self._trusted_evidence is not None else None
                    spans.append(measurement)
                    stored["howToCite"] += " Derived geometry values are approximate; confirm the selected entities and view before using them."
                    if self._trusted_evidence is not None:
                        from catia_mcp.drawing.pdf_evidence import _bound_evidence_bytes, _write_bound_evidence
                        _write_bound_evidence(evidence_path, _bound_evidence_bytes(stored), _bound_evidence_bytes(previous))
                    else:
                        evidence_path.write_text(json.dumps(stored, indent=2, ensure_ascii=False), encoding="utf-8")
                if not any(span.get("id") == measurement["id"] for span in manifest["contextEvidence"]):
                    manifest["contextEvidence"].append(measurement)
                    manifest["howToCite"] += " Derived geometry values are approximate; confirm the selected entities and view before using them."
            if self._trusted_evidence is not None:
                self._trusted_evidence[evidence_key] = copy.deepcopy(trusted_stored)
        except Exception as exc:
            return operation_result(
                tool="catia_inspect_drawing", ok=False, code="DRAWING_UNREADABLE",
                message=str(exc), data={}, modified=False,
            )
        if arguments.get("geometryOnly"):
            return operation_result(
                tool="catia_inspect_drawing", ok=True, code="DRAWING_READ",
                message="Returned 2D drawing geometry; full drawing evidence remains in the manifest.",
                data={"schema": manifest["schema"], "source": manifest["source"],
                      "contextSource": manifest.get("contextSource"), "contextGeometry": geometry,
                      **({"contextBlocks": blocks} if blocks is not None else {}),
                      **({"contextLayers": layers} if layers is not None else {}),
                      **({"contextReferenceGeometry": reference_geometry}
                         if reference_geometry is not None else {}),
                      **({"derivedMeasurement": measurement} if measurement is not None else {}),
                      "evidencePath": manifest.get("evidencePath")}, modified=False,
            )
        compact = bool(arguments.get("compactSpans", False))
        pages = []
        for page in manifest["pages"]:
            if compact:
                pages.append(
                    {
                        "page": page["page"],
                        "widthMm": page["widthMm"],
                        "heightMm": page["heightMm"],
                        "textSpanCount": page.get("textSpanCount"),
                        "ocrSpanCount": page.get("ocrSpanCount"),
                        "spans": [{"id": span["id"], "text": span["text"]}
                                  for span in page["spans"]],
                        "warnings": page.get("warnings", []),
                    }
                )
                continue
            pages.append(
                {
                    "page": page["page"],
                    "widthMm": page["widthMm"],
                    "heightMm": page["heightMm"],
                    "text": page["text"],
                    "textSpanCount": page.get("textSpanCount"),
                    "ocrSpanCount": page.get("ocrSpanCount"),
                    "spans": page["spans"],
                    "dimensionCandidates": page["dimensionCandidates"],
                    "unitHints": page["unitHints"],
                    "drawings": page["drawings"],
                }
            )
        all_context = manifest.get("contextEvidence", [])
        reference_spans = [
            span for span in all_context if span.get("sourceKind") == "referenceGeometry"
        ]
        visible_context = [
            span for span in all_context if span.get("sourceKind") != "referenceGeometry"
        ]
        reference_by_kind: dict[str, int] = {}
        for span in reference_spans:
            kind = str((span.get("values") or {}).get("kind") or "unknown")
            reference_by_kind[kind] = reference_by_kind.get(kind, 0) + 1
        reference_summary = {
            "count": len(reference_spans),
            "byKind": dict(sorted(reference_by_kind.items())),
            "note": ("Canonical STEP-derived geometry. Use referenceGeometryQuery "
                     "with kind/offset/limit to read item spanId and values before citing."),
        }
        return operation_result(
            tool="catia_inspect_drawing", ok=True, code="DRAWING_READ",
            message=(
                f"Read {manifest['source']['pageCount']} page(s); "
                f"{sum(len(p['spans']) for p in pages)} drawing spans, "
                f"{len(visible_context)} context evidence entries and "
                f"{len(reference_spans)} reference geometry entries."
            ),
            data={
                "schema": manifest["schema"],
                "source": manifest["source"],
                "pages": pages,
                "contextEvidence": ([{"id": span["id"], "text": span["text"],
                                      **({"coordinateSystem": span["coordinateSystem"]}
                                         if isinstance(span.get("coordinateSystem"), str) else {}),
                                      **({"rawMeasurement": span["rawMeasurement"]}
                                         if "rawMeasurement" in span else {})}
                                     for span in visible_context]
                                    if compact else visible_context),
                "contextSource": manifest.get("contextSource"),
                "referenceGeometrySummary": reference_summary,
                **({"contextGeometry": geometry} if geometry is not None else {}),
                **({"contextBlocks": blocks} if blocks is not None else {}),
                **({"contextLayers": layers} if layers is not None else {}),
                **({"contextReferenceGeometry": reference_geometry}
                   if reference_geometry is not None else {}),
                **({"derivedMeasurement": measurement} if measurement is not None else {}),
                "evidencePath": manifest.get("evidencePath"),
                "howToCite": manifest.get("howToCite"),
                "warnings": [warning.get("message", str(warning)) for warning in manifest.get("warnings", [])]
                            + (["TEXT_LAYER_PRESENT: OCR was skipped; read the text spans."]
                               if declares_text_layer else []),
            },
            modified=False,
        )

    def _crop(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            snapshot = self.input_snapshots(False) if self.input_snapshots else None
            result = crop_pdf_region(
                arguments["path"],
                int(arguments["page"]),
                list(arguments["bboxMm"]),
                int(arguments.get("dpi", 200)),
                arguments.get("outputPath"),
                pdf_bytes=snapshot[0] if snapshot is not None else None,
            )
        except Exception as exc:
            return operation_result(
                tool="catia_crop_drawing", ok=False, code="CROP_FAILED",
                message=str(exc), data={}, modified=False,
            )
        return operation_result(
            tool="catia_crop_drawing", ok=True, code="CROP_READY",
            message=f"Region rendered to {result['path']}.",
            data=result, modified=False,
        )


def _write_reports(
    *,
    delivery: dict[str, Any],
    plan: dict[str, Any],
    verification: dict[str, Any],
    editability: dict[str, Any] | None,
    completed: bool,
) -> dict[str, Any]:
    """Write the JSON + Markdown delivery report next to the CATPart.

    Never overwrites: a second report for the same stem gets a numeric suffix.
    Report writing is best effort and must not turn a verified build into a
    failure, so errors are returned in the payload instead of raised.
    """
    import time

    target = Path(delivery["path"])
    base = target.with_suffix("")
    json_path = base.parent / f"{base.name}-report.json"
    md_path = base.parent / f"{base.name}-report.md"
    expected = plan.get("expected") or {}
    params = expected.get("parameters") or {}
    bbox = expected.get("bbox") or {}
    dims = [
        bbox.get("max", {}).get(axis, 0) - bbox.get("min", {}).get(axis, 0)
        for axis in ("x", "y", "z")
    ]
    checks = verification.get("checks") or []
    failed = [check for check in checks if check.get("passed") is False]
    report = {
        "schema": "catia-pdf-build-report/1",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "completed": completed,
        "planId": plan.get("planId"),
        "part": {
            "name": plan.get("partName"),
            "kind": expected.get("kind", "flat_plate"),
            "sizeMm": {"length": dims[0], "width": dims[1], "thickness": dims[2]},
            "parameters": params,
            "holeCount": expected.get("holeCount"),
            "holes": expected.get("holes"),
            "expectedVolumeMm3": expected.get("volumeMm3"),
        },
        "delivery": {
            "path": delivery["path"],
            "sha256": delivery.get("sha256"),
            "sizeBytes": delivery.get("sizeBytes"),
        },
        "verification": {
            "verdict": verification.get("verdict"),
            "checks": checks,
            "measurements": verification.get("measurements"),
            "stepExport": verification.get("stepExport"),
        },
        "editability": editability,
        "unverified": [
            "Manufacturing compliance, tolerances and surface finish were not checked.",
            "The drawing's general tolerance was not applied to the model.",
        ],
        "limitations": [
            "Supported bodies: flat plates and extruded line/arc profiles with circular through holes, and annular rings with rolled edges.",
        ],
    }
    lines = [
        f"# {plan.get('partName') or target.stem} — PDF 图纸建模报告",
        "",
        f"- 结果：{'已完成并通过独立校验' if completed else '未完成（校验未通过）'}",
        f"- 交付文件：`{delivery['path']}`",
        f"- SHA-256：`{delivery.get('sha256')}`",
        f"- 建模方案：`{plan.get('planId')}`",
        f"- 尺寸（包围盒）：{dims[0]:g} × {dims[1]:g} × {dims[2]:g} mm",
        *([f"- 孔：{expected.get('holeCount')} × Ø"
           + (f"{(expected.get('holes') or [{}])[0].get('diameter', '?')}" if expected.get("holes") else "?")]
          if expected.get("holeCount") is not None else
          ["- 主体：圆环（外径/内径/板厚/圆边）" if expected.get("kind") == "annular_ring" else ""]),
        f"- 期望体积：{expected.get('volumeMm3')} mm³",
        "",
        "## 独立校验",
        "",
        f"结论：**{verification.get('verdict')}**（{len(checks) - len(failed)}/{len(checks)} 项通过）",
        "",
        "| 检查 | 结果 | 说明 |",
        "| --- | --- | --- |",
    ]
    for check in checks:
        detail = str(check.get("detail") or "").replace("|", "\\|")
        lines.append(f"| {check.get('name')} | {'PASS' if check.get('passed') else 'FAIL'} | {detail} |")
    lines += ["", "## 可编辑性", ""]
    if editability:
        lines.append(
            f"- 参数 {editability.get('parameter')}：{editability.get('beforeMm')} → "
            f"{editability.get('afterMm')} mm，体积 {editability.get('volumeBeforeMm3')} → "
            f"{editability.get('volumeAfterMm3')} mm³（副本修改，未保存）"
        )
    else:
        lines.append("- 未执行")
    lines += [
        "",
        "## 未验证事项",
        "",
        *[f"- {item}" for item in report["unverified"]],
        "",
        "## 范围",
        "",
        *[f"- {item}" for item in report["limitations"]],
        "",
    ]
    try:
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        md_path.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        return {"reportError": str(exc)}
    return {"reportJson": str(json_path), "reportMarkdown": str(md_path)}


def _decision_message(result: dict[str, Any]) -> str:
    decision = result.get("decision")
    if decision == "ready":
        return "Requirements are complete and every dimension cites a matching drawing span."
    if decision == "needs_clarification":
        questions = result.get("questions") or []
        return "More information is needed: " + " ".join(q["text"] for q in questions)
    if decision == "unsupported":
        return "Out of scope for this version: " + "; ".join(
            p["message"] for p in result.get("problems", [])
        )
    return "Requirements are invalid: " + "; ".join(
        p["message"] for p in result.get("problems", [])
    )
