"""Validate a model-authored ``catia-build-requirements/1`` document.

The model reads the PDF and fills the requirement document; this module is the
gate between that reading and any geometry. It does three things the model is
not allowed to do for itself:

1. normalize units to millimetres (an inch drawing is converted once, here);
2. check that every dimension cites a span that really contains that number;
3. decide between ``ready``, ``needs_clarification``, ``unsupported`` and
   ``invalid`` -- and answer with the questions to ask instead of guessing.

Nothing here talks to CATIA or to a PDF parser; the evidence manifest comes
from :func:`catia_mcp.drawing.pdf_evidence.inspect_pdf`.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from catia_mcp.drawing.units import unit_evidence_matches

REQUIREMENTS_SCHEMA = "catia-build-requirements/1"

UNIT_TO_MM = {
    "mm": 1.0,
    "millimetre": 1.0,
    "millimetres": 1.0,
    "millimeter": 1.0,
    "millimeters": 1.0,
    "inch": 25.4,
    "inches": 25.4,
    "in": 25.4,
}

MIN_EDGE_MARGIN_MM = 0.5
REFERENCE_GEOMETRY_LEN_ABS_MM = 0.05

# fields whose citation is checked against the evidence manifest
NUMERIC_FIELDS = ("length", "width", "thickness", "holeDiameter", "holeCount", "edgeX", "edgeY")
REQUIRED_FIELDS = ("length", "width", "thickness")


def _number_tokens(text: str) -> list[float]:
    tokens = re.findall(r"[-+]?(?:\d+(?:[.,]\d+)*|\.\d+)", text)
    values = []
    for token in tokens:
        # A comma followed by three digits can also be a thousands separator.
        if "," in token:
            if token.count(",") != 1 or "." in token or len(token.rsplit(",", 1)[1]) > 2:
                continue
            token = token.replace(",", ".")
        try:
            values.append(float(token))
        except ValueError:
            continue
    return values


def _contains_number(text: str, value: float) -> bool:
    for candidate in _number_tokens(text):
        if abs(candidate - value) <= max(1e-9, abs(value) * 1e-9):
            return True
    return False


def _without_multiplicity(text: str) -> str:
    """Exclude a leading hole-count prefix when matching a length or angle."""
    return re.sub(r"^\s*(?:\(\s*\d+\s*[xX]\s*\)\s*|\d+\s*[xX]\s*(?=[Ø⌀∅]))", "", text)


def _block_definition_text(span: dict) -> bool:
    # Block definitions may contain stale dimension text that is not displayed.
    return (span.get("source") == "drawing_context" and span.get("sourceKind") == "texts"
            and span.get("coordinateSystem") == "block_local")


def _reference_geometry_number_matches(span: Any, value: float) -> bool:
    """Match a requirement number against any finite numeric field of one item."""
    if not isinstance(span, dict) or span.get("sourceKind") != "referenceGeometry":
        return False
    values = span.get("values")
    if not isinstance(values, dict):
        return False
    for key, item in values.items():
        if key in {"kind", "role", "through"}:
            continue
        if type(item) not in (int, float) or not math.isfinite(item):
            continue
        if abs(float(item) - value) <= max(REFERENCE_GEOMETRY_LEN_ABS_MM,
                                          abs(value) * 1e-9):
            return True
    return False


def _evidence_number_matches(index, refs, combined, value, *, allow_raw):
    spans = [index[str(ref)] for ref in refs if not _block_definition_text(index[str(ref)])]
    if any(_reference_geometry_number_matches(span, value) for span in spans):
        return True
    texts = [str(span.get("text", "")) for span in spans]
    text = " ".join(_without_multiplicity(value) for value in texts) if allow_raw else " ".join(texts)
    if _contains_number(text, value):
        return True
    if not allow_raw:
        return False
    # Shared with the feature workflow so all model schemas apply the same
    # provenance, scale, rounding and dimension-type restrictions.
    from catia_mcp.drawing.feature_model import _dimension_evidence_matches
    return any(_dimension_evidence_matches(span, value, "length") for span in spans)


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _is_context_evidence(span: Any) -> bool:
    if not isinstance(span, dict) or span.get("source") != "drawing_context":
        return False
    kind, entity, asset, digest = (span.get("sourceKind"), span.get("entityId"),
                                   span.get("assetId"), span.get("sourceSha256"))
    if kind == "derived_geometry":
        ids = [span.get("targetCircleId"), span.get("referenceCircleId"), span.get("referenceDimensionId")]
        number, scale, radius = span.get("text"), span.get("scaleFactor"), span.get("sourceRadius")
        quantity, dx, dy = span.get("valueKind"), span.get("sourceDeltaX"), span.get("sourceDeltaY")
        return (isinstance(asset, str) and asset.startswith("drawing_")
                and all(isinstance(item, str) and item.startswith(asset + ":") for item in ids)
                and isinstance(quantity, str) and quantity in {"diameter", "offsetX", "offsetY"}
                and entity == ":".join([*ids, quantity]) and span.get("id") == f"ctx:derived_geometry:{entity}"
                and all(isinstance(span.get(k), str) and re.fullmatch(r"[0-9a-f]{64}", span[k])
                        for k in ("sourceSha256", "dimensionSourceSha256", "contextSha256"))
                and span.get("approximate") is True
                and type(scale) in (int, float) and math.isfinite(scale) and scale > 0
                and type(radius) in (int, float) and math.isfinite(radius) and radius > 0
                and all(type(v) in (int, float) and math.isfinite(v) for v in (dx, dy))
                and isinstance(number, str) and bool(re.fullmatch(r"-?\d+(?:\.\d+)?", number))
                and (quantity != "diameter" or float(number) > 0)
                and math.isclose(float(number), {"diameter": radius * 2, "offsetX": dx, "offsetY": dy}[quantity] * scale,
                                 rel_tol=1e-10, abs_tol=1e-8))
    if kind == "referenceGeometry":
        values = span.get("values")
        if (not isinstance(entity, str)
                or re.fullmatch(r"rg:(?:cyl|plane|cone|torus|profile|section):[0-9]+", entity) is None
                or not isinstance(asset, str) or not asset.startswith("drawing_")
                or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or span.get("id") != f"ctx:referenceGeometry:{entity}"
                or not isinstance(span.get("text"), str) or not span["text"].strip()
                or not isinstance(values, dict)
                or values.get("kind") not in {"cylinder", "plane", "cone", "torus",
                                              "planar_profile", "revolve_section"}):
            return False
        for key in ("radiusMm", "offsetMm", "areaMm2", "semiAngleDeg", "majorRadiusMm",
                    "minorRadiusMm", "diameterMm", "axialMinMm", "axialMaxMm"):
            if key in values and (type(values[key]) not in (int, float)
                                  or not math.isfinite(values[key])):
                return False
        if "through" in values and type(values["through"]) is not bool:
            return False
        if "role" in values and values["role"] not in {"hole", "boss", "unknown"}:
            return False
        return True
    return (kind in {"dimensions", "texts", "titleBlock"} and isinstance(entity, str) and bool(entity)
            and isinstance(asset, str) and asset.startswith("drawing_")
            and (kind == "titleBlock" or entity.startswith(asset + ":"))
            and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            and span.get("id") == f"ctx:{kind}:{entity}" and isinstance(span.get("text"), str))


def _evidence_index(manifest: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not manifest:
        return index
    for page in manifest.get("pages", []):
        for span in page.get("spans", []):
            if not str(span["id"]).startswith("ref3d:"):
                index[str(span["id"])] = {**span, "page": page.get("page")}
    context_hashes = (manifest.get("contextSource") or {}).get("sources") or {}
    for span in manifest.get("contextEvidence", []):
        if not _is_context_evidence(span) or span["id"] in index:
            continue
        if span.get("sourceKind") == "referenceGeometry":
            expected = context_hashes.get("referenceGeometry")
            if expected and span.get("sourceSha256") != expected:
                continue
        index[span["id"]] = dict(span)
    return index


def _load_evidence(source: Any) -> dict[str, Any] | None:
    if source is None:
        return None
    if isinstance(source, dict):
        return source
    path = Path(str(source))
    if not path.is_file():
        raise RuntimeError(f"Evidence manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_requirements(source: Any) -> dict[str, Any]:
    if isinstance(source, dict):
        return source
    path = Path(str(source))
    if not path.is_file():
        raise RuntimeError(f"Requirements file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _clarified_topics(requirements: dict[str, Any]) -> set[str]:
    topics = set()
    for item in requirements.get("clarifications") or []:
        if isinstance(item, dict) and item.get("answer") not in (None, ""):
            topic = str(item.get("topic") or "").strip().lower()
            if topic:
                topics.add(topic)
    return topics


def _question(topic: str, text: str, *, blocking: bool = True) -> dict[str, Any]:
    return {"topic": topic, "text": text, "blocking": blocking}


def validate_requirements(
    requirements: Any,
    evidence: Any = None,
    *,
    require_evidence: bool = True,
    reference_evidence: dict | None = None,
) -> dict[str, Any]:
    """Return ``{decision, problems, questions, warnings, normalized}``.

    ``decision`` is one of ``ready`` / ``needs_clarification`` / ``unsupported``
    / ``invalid``. Only ``ready`` may be compiled into a plan.
    """
    problems: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []

    try:
        req = load_requirements(requirements)
    except Exception as exc:
        return {
            "decision": "invalid",
            "problems": [{"code": "REQUIREMENTS_UNREADABLE", "message": str(exc)}],
            "questions": [],
            "warnings": [],
            "normalized": None,
        }

    from catia_mcp.drawing.feature_model import SCHEMA as FEATURE_SCHEMA, validate_features
    if isinstance(req, dict) and req.get("schema") == FEATURE_SCHEMA:
        return validate_features(req, evidence, require_evidence=require_evidence, reference_evidence=reference_evidence)
    if req.get("schema") != REQUIREMENTS_SCHEMA:
        problems.append(
            {
                "code": "SCHEMA_MISMATCH",
                "message": f"Expected schema '{REQUIREMENTS_SCHEMA}', got '{req.get('schema')}'.",
            }
        )

    body_type = str((req.get("body") or {}).get("type") or "").strip().lower()
    if body_type == "annular_ring":
        return _validate_ring(req, evidence, require_evidence=require_evidence)
    if body_type == "extruded_profile":
        return _validate_profile(req, evidence, require_evidence=require_evidence)

    try:
        manifest = _load_evidence(evidence)
    except Exception as exc:
        return {
            "decision": "invalid",
            "problems": [{"code": "EVIDENCE_UNREADABLE", "message": str(exc)}],
            "questions": [],
            "warnings": [],
            "normalized": None,
        }
    index = _evidence_index(manifest)
    clarified = _clarified_topics(req)

    # ---- unit -----------------------------------------------------------
    raw_unit = req.get("unit")
    unit = str(raw_unit).strip().lower() if raw_unit is not None else ""
    unit_factor = UNIT_TO_MM.get(unit)
    if not unit:
        if "unit" in clarified:
            warnings.append({"code": "UNIT_FROM_CLARIFICATION", "message": "Unit came from the user's answer."})
        else:
            questions.append(
                _question("unit", "The drawing does not state a unit. Should I model it in millimetres or inches?")
            )
    elif unit_factor is None:
        problems.append(
            {
                "code": "UNIT_UNSUPPORTED",
                "message": f"Unit '{raw_unit}' is not supported; use mm or inch.",
            }
        )

    # ---- body -----------------------------------------------------------
    body = req.get("body") or {}
    body_type = str(body.get("type") or "").strip().lower()
    if body_type != "flat_plate":
        problems.append(
            {
                "code": "BODY_UNSUPPORTED",
                "message": (
                    "This version builds flat plates only (rectangle + thickness + through holes). "
                    f"Got body type '{body.get('type')}'."
                ),
            }
        )

    dims_raw: dict[str, float] = {}
    for name in ("length", "width", "thickness"):
        value = body.get(name)
        if value in (None, ""):
            if name in clarified:
                warnings.append({"code": f"{name.upper()}_FROM_CLARIFICATION",
                                 "message": f"{name} came from the user's answer."})
                continue
            questions.append(
                _question(name, f"The drawing does not state the plate {name}. What value should I use?")
            )
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            problems.append({"code": "DIMENSION_NOT_NUMERIC",
                             "message": f"body.{name} must be a number, got {value!r}."})
            continue
        if numeric <= 0:
            problems.append({"code": "DIMENSION_NOT_POSITIVE",
                             "message": f"body.{name} must be positive, got {numeric}."})
            continue
        dims_raw[name] = numeric

    # ---- holes ----------------------------------------------------------
    holes = req.get("holes") or {}
    count_raw = holes.get("count")
    diameter_raw = holes.get("diameter")
    through = holes.get("through")
    pattern = holes.get("pattern") or {}

    count: int | None = None
    if count_raw in (None, ""):
        if "hole_count" not in clarified and "holes" not in clarified:
            questions.append(
                _question("hole_count", "The drawing does not state how many holes there are. How many should I model?")
            )
    else:
        try:
            count = int(count_raw)
            if count <= 0:
                problems.append({"code": "HOLE_COUNT_INVALID", "message": f"holes.count must be positive, got {count}."})
                count = None
        except (TypeError, ValueError):
            problems.append({"code": "HOLE_COUNT_NOT_INTEGER",
                             "message": f"holes.count must be an integer, got {count_raw!r}."})

    diameter: float | None = None
    if diameter_raw in (None, ""):
        if "hole_diameter" not in clarified and "holes" not in clarified:
            questions.append(
                _question("hole_diameter", "The drawing does not state the hole diameter. What diameter should I use?")
            )
    else:
        try:
            diameter = float(diameter_raw)
            if diameter <= 0:
                problems.append({"code": "HOLE_DIAMETER_INVALID",
                                 "message": f"holes.diameter must be positive, got {diameter}."})
                diameter = None
        except (TypeError, ValueError):
            problems.append({"code": "HOLE_DIAMETER_NOT_NUMERIC",
                             "message": f"holes.diameter must be a number, got {diameter_raw!r}."})

    if through is False:
        problems.append(
            {
                "code": "BLIND_HOLES_UNSUPPORTED",
                "message": "This version supports through holes only; a blind-hole depth needs a different plan.",
            }
        )

    pattern_type = str(pattern.get("type") or "").strip().lower()
    if count is not None and count > 0:
        if pattern_type in ("", "symmetric"):
            if count != 4:
                problems.append(
                    {
                        "code": "PATTERN_UNSUPPORTED",
                        "message": "A symmetric pattern describes exactly 4 holes; give explicit centres for other counts.",
                    }
                )
        elif pattern_type == "explicit":
            centres = pattern.get("centres")
            if not isinstance(centres, list) or len(centres) != count:
                problems.append(
                    {
                        "code": "PATTERN_CENTRE_COUNT",
                        "message": (
                            f"pattern.centres must list {count} [x, y] pairs; "
                            f"got {len(centres) if isinstance(centres, list) else 'none'}."
                        ),
                    }
                )
        else:
            problems.append(
                {"code": "PATTERN_UNSUPPORTED",
                 "message": f"Pattern type '{pattern.get('type')}' is not supported; use symmetric or explicit."}
            )

    # ---- evidence -------------------------------------------------------
    citations = req.get("evidence") or {}
    evidence_checks: list[dict[str, Any]] = []
    if require_evidence and manifest is None:
        problems.append(
            {
                "code": "EVIDENCE_NOT_SUPPLIED",
                "message": "No evidence manifest was supplied; dimension provenance was not checked.",
            }
        )
    elif manifest is not None:
        checks = [(name, name) for name in NUMERIC_FIELDS]
        checks.append(("unit", "unit"))
        for field, topic in checks:
            refs = citations.get(field)
            if not refs:
                if field in clarified or topic in clarified:
                    continue
                if field in REQUIRED_FIELDS or field in ("unit", "holeDiameter", "holeCount"):
                    questions.append(
                        _question(
                            topic,
                            f"The {field} has no drawing reference. Which dimension on the sheet should I use?",
                        )
                    )
                    problems.append(
                        {"code": "EVIDENCE_MISSING",
                         "message": f"No evidence span is cited for {field}."}
                    )
                continue
            if isinstance(refs, str):
                refs = [refs]
            if not isinstance(refs, list):
                problems.append({"code": "EVIDENCE_FORMAT",
                                 "message": f"evidence.{field} must be a list of span ids."})
                continue
            texts = []
            missing = []
            for ref in refs:
                span = index.get(str(ref))
                if span is None:
                    missing.append(str(ref))
                else:
                    texts.append(str(span.get("text", "")))
            if missing:
                problems.append(
                    {"code": "EVIDENCE_REF_UNKNOWN",
                     "message": f"{field} cites span(s) not in the manifest: {', '.join(missing)}."}
                )
                evidence_checks.append({"field": field, "refs": refs, "passed": False,
                                        "reason": "unknown span id"})
                continue
            combined = " ".join(texts)
            expected_value = None
            if field == "unit":
                if unit and unit_factor is not None:
                    passed = unit_evidence_matches(combined, unit_factor)
                else:
                    passed = True
                evidence_checks.append({"field": field, "refs": refs, "text": combined, "passed": passed})
                if not passed:
                    problems.append(
                        {"code": "EVIDENCE_VALUE_MISMATCH",
                         "message": f"Cited span(s) for unit do not mention '{unit}': {combined!r}."}
                    )
                continue
            if field == "holeCount":
                expected_value = float(count) if count is not None else None
            elif field == "holeDiameter":
                expected_value = diameter
            elif field in ("edgeX", "edgeY"):
                expected_value = pattern.get(field)
            else:
                expected_value = dims_raw.get(field)
            if expected_value is None:
                evidence_checks.append({"field": field, "refs": refs, "text": combined, "passed": True,
                                        "reason": "field not present yet"})
                continue
            try:
                numeric = float(expected_value)
            except (TypeError, ValueError):
                numeric = None
            passed = numeric is not None and _evidence_number_matches(
                index, refs, combined, numeric, allow_raw=field != "holeCount")
            evidence_checks.append(
                {"field": field, "refs": refs, "text": combined,
                 "expected": numeric, "passed": passed}
            )
            if numeric is not None and not passed:
                problems.append(
                    {
                        "code": "EVIDENCE_VALUE_MISMATCH",
                        "message": (
                            f"{field}={_format_number(numeric)} is not present in the cited span(s) "
                            f"{refs}: {combined!r}."
                        ),
                    }
                )

        if pattern_type == "explicit" and "hole_pattern" not in clarified:
            refs = citations.get("holePattern")
            if not refs:
                questions.append(
                    _question(
                        "hole_pattern",
                        "Which dimensions on the sheet locate the hole centres? Cite their span ids.",
                    )
                )
                problems.append(
                    {"code": "EVIDENCE_MISSING",
                     "message": "No evidence span is cited for holePattern."}
                )
            else:
                if isinstance(refs, str):
                    refs = [refs]
                missing = [str(r) for r in refs if str(r) not in index]
                if missing:
                    problems.append(
                        {"code": "EVIDENCE_REF_UNKNOWN",
                         "message": f"holePattern cites span(s) not in the manifest: {', '.join(missing)}."}
                    )
                evidence_checks.append(
                    {"field": "holePattern", "refs": refs,
                     "text": " ".join(str(index[str(r)].get("text", "")) for r in refs if str(r) in index),
                     "passed": not missing}
                )

    # ---- explicit conflicts --------------------------------------------
    conflicts = req.get("conflicts") or []
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        if conflict.get("resolved") or conflict.get("answer") not in (None, ""):
            continue
        topic = str(conflict.get("field") or "dimension")
        problems.append(
            {
                "code": "DIMENSION_CONFLICT",
                "message": conflict.get("message") or f"Conflicting values for {topic}.",
            }
        )
        questions.append(
            _question(topic, conflict.get("question") or f"The drawing gives conflicting values for {topic}. Which one is correct?")
        )

    # ---- normalize ------------------------------------------------------
    normalized = None
    if not problems and not questions and unit_factor is not None and len(dims_raw) == 3:
        length = dims_raw["length"] * unit_factor
        width = dims_raw["width"] * unit_factor
        thickness = dims_raw["thickness"] * unit_factor
        if diameter is not None and count is not None:
            diameter_mm = diameter * unit_factor
            if pattern_type in ("", "symmetric"):
                edge_x = pattern.get("edgeX")
                edge_y = pattern.get("edgeY")
                if edge_x is None or edge_y is None:
                    questions.append(
                        _question("hole_pattern", "What is the hole edge distance from the plate edges?")
                    )
                else:
                    edge_x_mm = float(edge_x) * unit_factor
                    edge_y_mm = float(edge_y) * unit_factor
                    centres = [
                        [-(length / 2 - edge_x_mm), -(width / 2 - edge_y_mm)],
                        [length / 2 - edge_x_mm, -(width / 2 - edge_y_mm)],
                        [length / 2 - edge_x_mm, width / 2 - edge_y_mm],
                        [-(length / 2 - edge_x_mm), width / 2 - edge_y_mm],
                    ]
                    normalized = {
                        "unit": "mm",
                        "sourceUnit": unit,
                        "partName": str(req.get("partName") or "Plate"),
                        "body": {"type": "flat_plate", "length": length, "width": width,
                                 "thickness": thickness},
                        "holes": {
                            "count": count,
                            "diameter": diameter_mm,
                            "through": True,
                            "pattern": {"type": "symmetric", "edgeX": edge_x_mm, "edgeY": edge_y_mm},
                            "centres": centres,
                        },
                        "clarifications": req.get("clarifications") or [],
                        "drawing": req.get("drawing") or {},
                    }
            elif pattern_type == "explicit":
                raw_centres = pattern.get("centres") or []
                centres = [[float(pair[0]) * unit_factor, float(pair[1]) * unit_factor]
                           for pair in raw_centres]
                normalized = {
                    "unit": "mm",
                    "sourceUnit": unit,
                    "partName": str(req.get("partName") or "Plate"),
                    "body": {"type": "flat_plate", "length": length, "width": width,
                             "thickness": thickness},
                    "holes": {
                        "count": count,
                        "diameter": diameter_mm,
                        "through": True,
                        "pattern": {"type": "explicit"},
                        "centres": centres,
                    },
                    "clarifications": req.get("clarifications") or [],
                    "drawing": req.get("drawing") or {},
                }

    # ---- geometry feasibility ------------------------------------------
    if normalized is not None:
        geo_problems = _check_geometry(normalized)
        problems.extend(geo_problems)

    codes = {str(p.get("code")) for p in problems}
    unsupported_codes = {
        "UNIT_UNSUPPORTED",
        "BODY_UNSUPPORTED",
        "PATTERN_UNSUPPORTED",
        "BLIND_HOLES_UNSUPPORTED",
    }
    clarification_codes = {"EVIDENCE_MISSING", "DIMENSION_CONFLICT"}
    if codes & unsupported_codes:
        decision = "unsupported"
    elif codes - clarification_codes:
        decision = "invalid"
    elif codes & clarification_codes or questions:
        decision = "needs_clarification"
    else:
        decision = "ready"

    # One question per topic, and no questions when the case is out of scope:
    # asking for a missing diameter would imply the body type could be built.
    seen: set[str] = set()
    unique_questions = []
    for question in questions:
        key = str(question.get("topic") or "").strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique_questions.append(question)
    questions = [] if decision == "unsupported" else unique_questions

    return {
        "decision": decision,
        "problems": problems,
        "questions": questions,
        "warnings": warnings,
        "evidenceChecks": evidence_checks,
        "normalized": normalized,
    }


RING_NUMERIC_FIELDS = ("outerDiameter", "innerDiameter", "thickness", "edgeRadius")


def _validate_ring(
    req: dict[str, Any], evidence: Any, *, require_evidence: bool
) -> dict[str, Any]:
    """Validate an ``annular_ring`` body: a flat web with a rolled edge.

    Geometry is the union of two solids: a ring web (outer/inner diameter,
    thickness) and a full torus of ``edge.radius`` centred on each edge at
    height ``radius`` above the bottom face. The solid is therefore taller
    than the web whenever ``2 * radius > thickness``, which is what the
    FTC-11 section shows.
    """
    problems: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []

    try:
        manifest = _load_evidence(evidence)
    except Exception as exc:
        return {
            "decision": "invalid",
            "problems": [{"code": "EVIDENCE_UNREADABLE", "message": str(exc)}],
            "questions": [],
            "warnings": [],
            "normalized": None,
        }
    index = _evidence_index(manifest)
    clarified = _clarified_topics(req)

    raw_unit = req.get("unit")
    unit = str(raw_unit).strip().lower() if raw_unit is not None else ""
    unit_factor = UNIT_TO_MM.get(unit)
    if not unit:
        if "unit" in clarified:
            warnings.append({"code": "UNIT_FROM_CLARIFICATION", "message": "Unit came from the user's answer."})
        else:
            questions.append(_question("unit", "The drawing does not state a unit. Millimetres or inches?"))
    elif unit_factor is None:
        problems.append({"code": "UNIT_UNSUPPORTED", "message": f"Unit '{raw_unit}' is not supported; use mm or inch."})

    body = req.get("body") or {}
    outer_raw = body.get("outerDiameter")
    inner_raw = body.get("innerDiameter")
    thickness_raw = body.get("thickness")
    edge = body.get("edge") or {}
    edge_style = str(edge.get("style") or "rolled").strip().lower()
    radius_raw = edge.get("radius")

    if edge_style not in ("rolled", "round"):
        problems.append(
            {
                "code": "EDGE_STYLE_UNSUPPORTED",
                "message": f"This version supports a rolled/round edge only; got '{edge.get('style')}'.",
            }
        )

    values: dict[str, float] = {}
    for name, raw in (
        ("outerDiameter", outer_raw),
        ("innerDiameter", inner_raw),
        ("thickness", thickness_raw),
        ("edgeRadius", radius_raw),
    ):
        if raw in (None, ""):
            if name in clarified or "dimensions" in clarified:
                warnings.append({"code": f"{name.upper()}_FROM_CLARIFICATION",
                                 "message": f"{name} came from the user's answer."})
                continue
            questions.append(_question(name, f"The drawing does not state the {name}. What value should I use?"))
            continue
        try:
            numeric = float(raw)
        except (TypeError, ValueError):
            problems.append({"code": "DIMENSION_NOT_NUMERIC",
                             "message": f"body.{name} must be a number, got {raw!r}."})
            continue
        if numeric <= 0:
            problems.append({"code": "DIMENSION_NOT_POSITIVE",
                             "message": f"body.{name} must be positive, got {numeric}."})
            continue
        values[name] = numeric

    # Evidence mapping: the same four numbers, every one traceable to a span.
    if require_evidence and manifest is None:
        problems.append({"code": "EVIDENCE_NOT_SUPPLIED",
                         "message": "No evidence manifest was supplied; dimension provenance was not checked."})
    elif manifest is not None:
        citations = req.get("evidence") or {}
        for field in RING_NUMERIC_FIELDS:
            refs = citations.get(field)
            if not refs:
                if field in clarified or "dimensions" in clarified:
                    continue
                questions.append(_question(field, f"The {field} has no drawing reference. Which dimension should I use?"))
                problems.append({"code": "EVIDENCE_MISSING",
                                 "message": f"No evidence span is cited for {field}."})
                continue
            if isinstance(refs, str):
                refs = [refs]
            missing = [str(r) for r in refs if str(r) not in index]
            if missing:
                problems.append({"code": "EVIDENCE_REF_UNKNOWN",
                                 "message": f"{field} cites span(s) not in the manifest: {', '.join(missing)}."})
                continue
            combined = " ".join(str(index[str(r)].get("text", "")) for r in refs)
            expected = values.get(field)
            passed = expected is None or _evidence_number_matches(
                index, refs, combined, expected, allow_raw=True)
            if not passed:
                problems.append(
                    {
                        "code": "EVIDENCE_VALUE_MISMATCH",
                        "message": (f"{field}={_format_number(expected)} is not present in the cited "
                                    f"span(s) {refs}: {combined!r}."),
                    }
                )
        unit_refs = citations.get("unit")
        if unit_refs and unit and unit_factor is not None:
            if isinstance(unit_refs, str):
                unit_refs = [unit_refs]
            combined = " ".join(str(index[str(r)].get("text", "")) for r in unit_refs if str(r) in index)
            if not unit_evidence_matches(combined, unit_factor):
                problems.append({"code": "EVIDENCE_VALUE_MISMATCH",
                                 "message": f"Cited span(s) for unit do not mention '{unit}': {combined!r}."})
        elif unit and unit_factor is not None and "unit" not in clarified:
            questions.append(_question("unit", "The unit has no drawing reference. Which text states it?"))

    normalized = None
    if not problems and not questions and unit_factor is not None and len(values) == 4:
        outer = values["outerDiameter"] * unit_factor
        inner = values["innerDiameter"] * unit_factor
        thickness = values["thickness"] * unit_factor
        radius = values["edgeRadius"] * unit_factor
        if outer <= inner:
            problems.append({"code": "RING_DIAMETER_ORDER",
                             "message": f"outerDiameter {outer:g} must exceed innerDiameter {inner:g}."})
        elif radius >= inner / 2.0:
            problems.append({"code": "EDGE_RADIUS_TOO_LARGE",
                             "message": f"edge radius {radius:g} must be smaller than the inner radius {inner / 2:g}."})
        elif 2.0 * radius <= thickness:
            problems.append({"code": "EDGE_RADIUS_TOO_SMALL",
                             "message": (f"a rolled edge needs 2*radius > thickness; got "
                                         f"2*{radius:g}={2 * radius:g} vs thickness {thickness:g}.")})
        elif (outer / 2.0 - radius) <= (inner / 2.0 + radius):
            problems.append({"code": "EDGE_BEADS_OVERLAP",
                             "message": "The outer and inner rolled edges meet; the web has no flat band."})
        else:
            normalized = {
                "unit": "mm",
                "sourceUnit": unit,
                "partName": str(req.get("partName") or "Ring"),
                "body": {
                    "type": "annular_ring",
                    "outerDiameter": outer,
                    "innerDiameter": inner,
                    "thickness": thickness,
                    "edge": {"style": "rolled", "radius": radius},
                },
                "clarifications": req.get("clarifications") or [],
                "drawing": req.get("drawing") or {},
            }

    codes = {str(p.get("code")) for p in problems}
    unsupported_codes = {"UNIT_UNSUPPORTED", "EDGE_STYLE_UNSUPPORTED"}
    clarification_codes = {"EVIDENCE_MISSING", "DIMENSION_CONFLICT"}
    if codes & unsupported_codes:
        decision = "unsupported"
    elif codes - clarification_codes:
        decision = "invalid"
    elif codes & clarification_codes or questions:
        decision = "needs_clarification"
    else:
        decision = "ready"

    seen: set[str] = set()
    unique_questions = []
    for question in questions:
        key = str(question.get("topic") or "").strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique_questions.append(question)

    return {
        "decision": decision,
        "problems": problems,
        "questions": [] if decision == "unsupported" else unique_questions,
        "warnings": warnings,
        "evidenceChecks": [],
        "normalized": normalized,
    }


def _segment_endpoints(segment: dict[str, Any], factor: float) -> tuple[tuple[float, float], tuple[float, float]]:
    kind = str(segment.get("type") or "").strip().lower()
    if kind == "line":
        return (
            (float(segment["x1"]) * factor, float(segment["y1"]) * factor),
            (float(segment["x2"]) * factor, float(segment["y2"]) * factor),
        )
    if kind == "arc":
        cx = float(segment["cx"]) * factor
        cy = float(segment["cy"]) * factor
        radius = float(segment["radius"]) * factor
        start = math.radians(float(segment["startAngle"]))
        end = math.radians(float(segment["endAngle"]))
        while end <= start:
            end += 2.0 * math.pi
        return (
            (cx + radius * math.cos(start), cy + radius * math.sin(start)),
            (cx + radius * math.cos(end), cy + radius * math.sin(end)),
        )
    raise ValueError(f"profile segment type '{segment.get('type')}' is not line/arc")


def _profile_polygon(profile: list[dict[str, Any]], factor: float, curve_steps: int = 24) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for segment in profile:
        kind = str(segment.get("type") or "").strip().lower()
        if kind == "line":
            start, _ = _segment_endpoints(segment, factor)
            points.append(start)
            continue
        cx = float(segment["cx"]) * factor
        cy = float(segment["cy"]) * factor
        radius = float(segment["radius"]) * factor
        start = math.radians(float(segment["startAngle"]))
        end = math.radians(float(segment["endAngle"]))
        while end <= start:
            end += 2.0 * math.pi
        for step in range(curve_steps):
            angle = start + (end - start) * step / curve_steps
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def _point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < cross:
                inside = not inside
    return inside


def _distance_to_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> float:
    best = float("inf")
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        dx, dy = x2 - x1, y2 - y1
        length_sq = dx * dx + dy * dy
        t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length_sq))
        px, py = x1 + t * dx, y1 + t * dy
        best = min(best, math.hypot(x - px, y - py))
    return best


def _validate_profile(
    req: dict[str, Any], evidence: Any, *, require_evidence: bool
) -> dict[str, Any]:
    """Validate a closed line/arc profile extruded along +Z with through holes."""
    problems: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []

    try:
        manifest = _load_evidence(evidence)
    except Exception as exc:
        return {
            "decision": "invalid",
            "problems": [{"code": "EVIDENCE_UNREADABLE", "message": str(exc)}],
            "questions": [],
            "warnings": [],
            "normalized": None,
        }
    index = _evidence_index(manifest)
    clarified = _clarified_topics(req)

    raw_unit = req.get("unit")
    unit = str(raw_unit).strip().lower() if raw_unit is not None else ""
    unit_factor = UNIT_TO_MM.get(unit)
    if not unit:
        if "unit" in clarified:
            warnings.append({"code": "UNIT_FROM_CLARIFICATION", "message": "Unit came from the user's answer."})
        else:
            questions.append(_question("unit", "The drawing does not state a unit. Millimetres or inches?"))
    elif unit_factor is None:
        problems.append({"code": "UNIT_UNSUPPORTED", "message": f"Unit '{raw_unit}' is not supported; use mm or inch."})

    body = req.get("body") or {}
    thickness_raw = body.get("thickness")
    thickness = None
    if thickness_raw in (None, ""):
        if "thickness" in clarified:
            warnings.append({"code": "THICKNESS_FROM_CLARIFICATION", "message": "Thickness came from the user's answer."})
        else:
            questions.append(_question("thickness", "The drawing does not state the extrusion thickness. What value should I use?"))
    else:
        try:
            thickness = float(thickness_raw)
            if thickness <= 0:
                problems.append({"code": "THICKNESS_INVALID", "message": "Thickness must be positive."})
                thickness = None
        except (TypeError, ValueError):
            problems.append({"code": "DIMENSION_NOT_NUMERIC", "message": f"body.thickness must be a number, got {thickness_raw!r}."})

    profile_raw = body.get("profile")
    segments: list[dict[str, Any]] = []
    endpoints: list[tuple[tuple[float, float], tuple[float, float]]] = []
    if not isinstance(profile_raw, list) or len(profile_raw) < 2:
        problems.append({"code": "PROFILE_INVALID", "message": "body.profile must list at least 2 line/arc segments."})
    elif unit_factor is not None:
        for index_, segment in enumerate(profile_raw, 1):
            if not isinstance(segment, dict):
                problems.append({"code": "PROFILE_SEGMENT_INVALID", "message": f"Segment {index_} is not an object."})
                continue
            try:
                endpoints.append(_segment_endpoints(segment, unit_factor))
                segments.append(segment)
            except (KeyError, TypeError, ValueError) as exc:
                problems.append({"code": "PROFILE_SEGMENT_INVALID",
                                 "message": f"Segment {index_} ({segment.get('type')!r}): {exc}"})
        if len(endpoints) == len(profile_raw) and len(endpoints) >= 2:
            for index_ in range(len(endpoints)):
                end = endpoints[index_][1]
                start = endpoints[(index_ + 1) % len(endpoints)][0]
                if math.hypot(end[0] - start[0], end[1] - start[1]) > 0.01:
                    problems.append({
                        "code": "PROFILE_NOT_CLOSED",
                        "message": (f"Segment {index_ + 1} ends at ({end[0]:g}, {end[1]:g}) but "
                                    f"segment {(index_ + 2 - 1) % len(endpoints) + 1} starts at "
                                    f"({start[0]:g}, {start[1]:g})."),
                    })

    # Holes are optional; when present they reuse the flat-plate hole schema.
    holes = req.get("holes")
    count = None
    diameter = None
    centres: list[list[float]] = []
    if holes:
        count_raw = holes.get("count")
        diameter_raw = holes.get("diameter")
        pattern = holes.get("pattern") or {}
        pattern_type = str(pattern.get("type") or "").strip().lower()
        if count_raw in (None, ""):
            if "hole_count" not in clarified:
                questions.append(_question("hole_count", "The drawing does not state how many holes there are."))
        else:
            try:
                count = int(count_raw)
                if count <= 0:
                    problems.append({"code": "HOLE_COUNT_INVALID", "message": "holes.count must be positive."})
                    count = None
            except (TypeError, ValueError):
                problems.append({"code": "HOLE_COUNT_NOT_INTEGER", "message": f"holes.count must be an integer, got {count_raw!r}."})
        if diameter_raw in (None, ""):
            if "hole_diameter" not in clarified:
                questions.append(_question("hole_diameter", "The drawing does not state the hole diameter."))
        else:
            try:
                diameter = float(diameter_raw)
                if diameter <= 0:
                    problems.append({"code": "HOLE_DIAMETER_INVALID", "message": "holes.diameter must be positive."})
                    diameter = None
            except (TypeError, ValueError):
                problems.append({"code": "HOLE_DIAMETER_NOT_NUMERIC", "message": f"holes.diameter must be a number, got {diameter_raw!r}."})
        if holes.get("through") is False:
            problems.append({"code": "BLIND_HOLES_UNSUPPORTED", "message": "Through holes only."})
        if pattern_type != "explicit":
            problems.append({"code": "PATTERN_UNSUPPORTED",
                             "message": "An extruded profile needs pattern.type='explicit' with centres."})
        else:
            raw_centres = pattern.get("centres")
            if not isinstance(raw_centres, list) or (count is not None and len(raw_centres) != count):
                problems.append({"code": "PATTERN_CENTRE_COUNT",
                                 "message": f"pattern.centres must list {count} [x, y] pairs."})
            elif unit_factor is not None:
                centres = [[float(pair[0]) * unit_factor, float(pair[1]) * unit_factor] for pair in raw_centres]

    # Evidence ------------------------------------------------------------
    if require_evidence and manifest is None:
        problems.append({"code": "EVIDENCE_NOT_SUPPLIED", "message": "No evidence manifest was supplied."})
    elif manifest is not None:
        citations = req.get("evidence") or {}
        required = ["thickness", "unit"]
        if holes:
            required += ["holeDiameter", "holeCount", "holePattern"]
        required.append("profile")
        for field in required:
            refs = citations.get(field)
            if not refs:
                if field in clarified:
                    continue
                questions.append(_question(field, f"The {field} has no drawing reference. Which span should I cite?"))
                problems.append({"code": "EVIDENCE_MISSING", "message": f"No evidence span is cited for {field}."})
                continue
            if isinstance(refs, str):
                refs = [refs]
            missing = [str(r) for r in refs if str(r) not in index]
            if missing:
                problems.append({"code": "EVIDENCE_REF_UNKNOWN",
                                 "message": f"{field} cites span(s) not in the manifest: {', '.join(missing)}."})
                continue
            combined = " ".join(str(index[str(r)].get("text", "")) for r in refs)
            expected_value = None
            if field == "thickness":
                expected_value = thickness
            elif field == "holeDiameter":
                expected_value = diameter
            elif field == "holeCount":
                expected_value = float(count) if count is not None else None
            elif field == "unit":
                if unit and unit_factor is not None:
                    if not unit_evidence_matches(combined, unit_factor):
                        problems.append({"code": "EVIDENCE_VALUE_MISMATCH",
                                         "message": f"Cited span(s) for unit do not mention '{unit}': {combined!r}."})
                continue
            if expected_value is not None and not _evidence_number_matches(
                    index, refs, combined, float(expected_value), allow_raw=field != "holeCount"):
                problems.append({
                    "code": "EVIDENCE_VALUE_MISMATCH",
                    "message": f"{field}={_format_number(float(expected_value))} is not present in {refs}: {combined!r}.",
                })

    # Geometry ------------------------------------------------------------
    normalized = None
    if (not problems and not questions and unit_factor is not None and thickness is not None
            and len(segments) == len(profile_raw or []) and len(segments) >= 2):
        polygon = _profile_polygon(segments, unit_factor)
        if len(polygon) < 3:
            problems.append({"code": "PROFILE_INVALID", "message": "Profile has fewer than 3 distinct points."})
        elif not holes:
            normalized = {
                "unit": "mm",
                "sourceUnit": unit,
                "partName": str(req.get("partName") or "Profile"),
                "body": {
                    "type": "extruded_profile",
                    "thickness": thickness * unit_factor,
                    "profile": [
                        ({**segment, **{k: float(segment[k]) * unit_factor
                                       for k in ("x1", "y1", "x2", "y2")}}
                         if segment.get("type") == "line" else
                         {**segment, **{k: float(segment[k]) * unit_factor
                                       for k in ("cx", "cy", "radius")}})
                        for segment in segments
                    ],
                },
                "holes": None,
                "clarifications": req.get("clarifications") or [],
                "drawing": req.get("drawing") or {},
            }
        elif count is not None and diameter is not None and len(centres) == count:
            duplicate = len({(round(x, 6), round(y, 6)) for x, y in centres}) != len(centres)
            if duplicate:
                problems.append({"code": "HOLE_CENTRES_DUPLICATE", "message": "Two holes share the same centre."})
            radius = diameter * unit_factor / 2.0
            for hole_index, (x, y) in enumerate(centres, 1):
                if not _point_in_polygon(x, y, polygon):
                    problems.append({"code": "HOLE_OUTSIDE_BODY",
                                     "message": f"Hole {hole_index} at ({x:g}, {y:g}) is outside the profile."})
                elif _distance_to_polygon(x, y, polygon) <= radius + MIN_EDGE_MARGIN_MM:
                    problems.append({"code": "HOLE_OUTSIDE_BODY",
                                     "message": f"Hole {hole_index} at ({x:g}, {y:g}) breaks the profile edge."})
                for other in range(hole_index, len(centres)):
                    ox, oy = centres[other]
                    if math.hypot(x - ox, y - oy) <= 2 * radius:
                        problems.append({"code": "HOLES_OVERLAP",
                                         "message": f"Holes {hole_index} and {other + 1} overlap or touch."})
            if not problems:
                converted_segments = []
                for segment in segments:
                    kind = str(segment.get("type") or "").strip().lower()
                    if kind == "line":
                        converted_segments.append({
                            "type": "line",
                            "x1": float(segment["x1"]) * unit_factor,
                            "y1": float(segment["y1"]) * unit_factor,
                            "x2": float(segment["x2"]) * unit_factor,
                            "y2": float(segment["y2"]) * unit_factor,
                        })
                    else:
                        converted_segments.append({
                            "type": "arc",
                            "cx": float(segment["cx"]) * unit_factor,
                            "cy": float(segment["cy"]) * unit_factor,
                            "radius": float(segment["radius"]) * unit_factor,
                            "startAngle": float(segment["startAngle"]),
                            "endAngle": float(segment["endAngle"]),
                        })
                normalized = {
                    "unit": "mm",
                    "sourceUnit": unit,
                    "partName": str(req.get("partName") or "Profile"),
                    "body": {
                        "type": "extruded_profile",
                        "thickness": thickness * unit_factor,
                        "profile": converted_segments,
                    },
                    "holes": {
                        "count": count,
                        "diameter": diameter * unit_factor,
                        "through": True,
                        "pattern": {"type": "explicit"},
                        "centres": centres,
                    },
                    "clarifications": req.get("clarifications") or [],
                    "drawing": req.get("drawing") or {},
                }

    codes = {str(p.get("code")) for p in problems}
    unsupported_codes = {"UNIT_UNSUPPORTED", "PATTERN_UNSUPPORTED", "BLIND_HOLES_UNSUPPORTED"}
    clarification_codes = {"EVIDENCE_MISSING", "DIMENSION_CONFLICT"}
    if codes & unsupported_codes:
        decision = "unsupported"
    elif codes - clarification_codes:
        decision = "invalid"
    elif codes & clarification_codes or questions:
        decision = "needs_clarification"
    else:
        decision = "ready"

    seen: set[str] = set()
    unique_questions = []
    for question in questions:
        key = str(question.get("topic") or "").strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique_questions.append(question)

    return {
        "decision": decision,
        "problems": problems,
        "questions": [] if decision == "unsupported" else unique_questions,
        "warnings": warnings,
        "evidenceChecks": [],
        "normalized": normalized,
    }


def _check_geometry(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    body = normalized["body"]
    holes = normalized["holes"]
    length = body["length"]
    width = body["width"]
    thickness = body["thickness"]
    radius = holes["diameter"] / 2.0

    if thickness <= 0:
        problems.append({"code": "THICKNESS_INVALID", "message": "Thickness must be positive."})
    if holes["diameter"] >= min(length, width):
        problems.append(
            {"code": "HOLE_TOO_LARGE",
             "message": f"Hole diameter {holes['diameter']} does not fit in {length}x{width}."}
        )
    for index, (x, y) in enumerate(holes["centres"], 1):
        if abs(x) + radius > length / 2 - MIN_EDGE_MARGIN_MM:
            problems.append(
                {
                    "code": "HOLE_OUTSIDE_BODY",
                    "message": (
                        f"Hole {index} at ({x:g}, {y:g}) with radius {radius:g} is not fully inside "
                        f"the plate (length {length:g}); it breaks the edge or leaves less than "
                        f"{MIN_EDGE_MARGIN_MM} mm of material."
                    ),
                }
            )
        if abs(y) + radius > width / 2 - MIN_EDGE_MARGIN_MM:
            problems.append(
                {
                    "code": "HOLE_OUTSIDE_BODY",
                    "message": (
                        f"Hole {index} at ({x:g}, {y:g}) with radius {radius:g} is not fully inside "
                        f"the plate (width {width:g}); it breaks the edge or leaves less than "
                        f"{MIN_EDGE_MARGIN_MM} mm of material."
                    ),
                }
            )
    centres = [tuple(round(v, 9) for v in pair) for pair in holes["centres"]]
    if len(set(centres)) != len(centres):
        problems.append({"code": "HOLE_CENTRES_DUPLICATE",
                         "message": "Two holes share the same centre."})
    for i in range(len(centres)):
        for j in range(i + 1, len(centres)):
            dx = centres[i][0] - centres[j][0]
            dy = centres[i][1] - centres[j][1]
            if math.hypot(dx, dy) <= 2 * radius:
                problems.append(
                    {"code": "HOLES_OVERLAP",
                     "message": f"Holes {i + 1} and {j + 1} overlap or touch."}
                )
    return problems
