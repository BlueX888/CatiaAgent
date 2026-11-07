"""Derive circle dimensions from paired 2D geometry and a radial scale anchor."""
from __future__ import annotations

import math

from catia_mcp.drawing.pdf_evidence import _load_drawing_context


def derive_circle_measurement(context_path: str, pdf_sha256: str, selection: dict,
                              expected_context_sha256: str | None = None, *, data: bytes | None = None) -> dict:
    """Return an attributed, approximate diameter or center offset, never PDF text.

    The caller must select both circles and a radial dimension. We only accept
    an untransformed XY OCS and verify that the radial dimension's endpoints
    describe the selected reference circle. No STEP or 3D data is consulted.
    """
    required = {"targetCircleId", "referenceCircleId", "referenceDimensionId"}
    if (not isinstance(selection, dict) or not required <= set(selection)
            or set(selection) - required - {"quantity"}
            or any(not isinstance(selection[k], str) or not selection[k] for k in required)):
        raise ValueError("Select a target circle, reference circle and radial dimension.")
    quantity = selection.get("quantity", "diameter")
    if not isinstance(quantity, str) or quantity not in {"diameter", "offsetX", "offsetY"}:
        raise ValueError("quantity must be diameter, offsetX or offsetY.")
    spans, source, payload = _load_drawing_context(context_path, pdf_sha256, expected_context_sha256, data=data)
    asset = payload["assetId"]
    geometry = payload.get("geometry")
    dimensions = payload.get("dimensions")
    if not isinstance(geometry, list) or not isinstance(dimensions, list):
        raise ValueError("Paired 2D geometry and dimensions are required.")

    def one(records, key):
        found = [r for r in records if isinstance(r, dict) and r.get("entity_id") == selection[key]]
        if len(found) != 1 or not selection[key].startswith(asset + ":"):
            raise ValueError(f"{key} must identify one entity in this drawing.")
        return found[0]

    target = one(geometry, "targetCircleId")
    reference = one(geometry, "referenceCircleId")
    dimension = one(dimensions, "referenceDimensionId")
    if target is reference:
        raise ValueError("Select different target and reference circles.")
    frame = target.get("spatial_frame_id")
    if (not isinstance(frame, str) or not frame
            or reference.get("spatial_frame_id") != frame or dimension.get("spatial_frame_id") != frame
            or dimension.get("coordinate_system") != "layout_wcs"):
        raise ValueError("Both circles and the dimension must share one directly comparable frame.")

    def circle(record):
        g = record.get("geometry")
        if (record.get("geometry_type") != "CIRCLE" or record.get("coordinate_system") != "ocs"
                or not isinstance(g, dict) or g.get("extrusion") != [0, 0, 1]):
            raise ValueError("Only untransformed XY-plane circles are supported.")
        center, radius = g.get("center_ocs"), g.get("radius")
        if (not isinstance(center, list) or len(center) != 3
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in center)
                or abs(center[2]) > 1e-8
                or type(radius) not in (int, float) or not math.isfinite(radius) or radius <= 0):
            raise ValueError("Invalid circle geometry.")
        return center, radius

    target_center, target_radius = circle(target)
    reference_center, reference_radius = circle(reference)
    points, raw, scale = (dimension.get("definition_points"), dimension.get("raw_measurement"),
                          dimension.get("length_scale_factor"))
    if (dimension.get("dimension_type") != 4 or dimension.get("raw_measurement_unit") != "drawing_unit"
            or dimension.get("measurement_basis") != "declared_drawing_unit"
            or "text_override" not in dimension or dimension.get("text_override") not in (None, "", "<>")
            or type(raw) not in (int, float) or not math.isfinite(raw) or raw <= 0
            or type(scale) not in (int, float) or not math.isfinite(scale)
            or not (0 < scale <= 1e6 or scale == -1)
            or not isinstance(points, list) or len(points) != 2
            or any(not isinstance(p, list) or len(p) != 3
                   or any(type(v) not in (int, float) or not math.isfinite(v) for v in p) for p in points)):
        raise ValueError("A radial dimension with explicit unit, scale and endpoints is required.")
    tol = max(1e-7, reference_radius * 1e-8)
    if (math.dist(points[0], reference_center) > tol
            or any(abs(point[2]) > 1e-8 for point in points)
            or abs(math.dist(points[0], points[1]) - reference_radius) > tol
            or not (math.isclose(raw, reference_radius, rel_tol=1e-10, abs_tol=tol)
                    or math.isclose(raw, reference_radius * abs(scale), rel_tol=1e-10, abs_tol=tol))):
        raise ValueError("The radial dimension does not describe the selected reference circle.")
    anchor = next((s for s in spans if s["id"] == "ctx:dimensions:" + selection["referenceDimensionId"]), None)
    from catia_mcp.drawing.feature_model import _dimension_evidence_matches
    if anchor is None or not _dimension_evidence_matches(anchor, reference_radius * abs(scale), "length"):
        raise ValueError("The scaled radial measurement does not match its displayed annotation.")
    delta_x, delta_y = target_center[0] - reference_center[0], target_center[1] - reference_center[1]
    value = {"diameter": target_radius * 2, "offsetX": delta_x, "offsetY": delta_y}[quantity] * abs(scale)
    if not math.isfinite(value) or abs(value) > 1e6 or (quantity == "diameter" and value <= 0):
        raise ValueError("Derived measurement is outside the supported range.")
    text = f"{value:.12f}".rstrip("0").rstrip(".")
    if text == "-0":
        text = "0"
    if quantity == "diameter" and float(text) <= 0:
        raise ValueError("Derived diameter is below the supported numeric precision.")
    sources = {s["kind"]: s["sha256"] for s in payload["sources"]}
    identity = ":".join((selection["targetCircleId"], selection["referenceCircleId"],
                         selection["referenceDimensionId"], quantity))
    return {
        "id": "ctx:derived_geometry:" + identity, "text": text,
        "source": "drawing_context", "sourceKind": "derived_geometry",
        "sourceSha256": sources["geometry"], "dimensionSourceSha256": sources["dimensions"],
        "assetId": asset, "entityId": identity, "spatialFrameId": frame,
        "coordinateSystem": "ocs", "valueKind": quantity, "approximate": True,
        "targetCircleId": selection["targetCircleId"], "referenceCircleId": selection["referenceCircleId"],
        "referenceDimensionId": selection["referenceDimensionId"], "scaleFactor": abs(scale),
        "sourceRadius": target_radius, "sourceDeltaX": delta_x, "sourceDeltaY": delta_y,
        "contextSha256": source["sha256"],
    }
