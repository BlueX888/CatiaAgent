"""Read a PDF engineering drawing into evidence the model must cite.

The model is allowed to interpret the drawing, but it may not invent the
source of a dimension. Every text span on every page gets a stable id
(``p<page>:s<index>``) and a bounding box in millimetres; the requirements
validator later rejects a dimension whose cited span does not contain the
number (see :mod:`catia_mcp.drawing.requirements`).

Page rendering is deliberately *not* done here: AgentCPM already renders PDF
pages through ``read_file``, and duplicating a renderer would add a second,
divergent view of the same drawing. Crop requests are supported because a
caller sometimes needs a close-up of one view.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from catia_mcp.drawing.units import drawing_units

_UNBOUND_EVIDENCE = object()
MAX_BOUND_EVIDENCE_BYTES = 32 * 1024 * 1024


def _bound_evidence_bytes(value: dict) -> bytes:
    data = json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_BOUND_EVIDENCE_BYTES:
        raise RuntimeError("Generation evidence exceeds 32 MiB.")
    return data


def _check_bound_evidence(path: Path, expected: bytes) -> None:
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or getattr(before, "st_file_attributes", 0) & 0x400):
        raise RuntimeError("Evidence manifest is not an unlinked regular file.")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("Evidence manifest changed since inspection.")
        if stream.read(len(expected) + 1) != expected:
            raise RuntimeError("Evidence manifest changed since inspection.")
    after = path.lstat()
    if ((after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
            or getattr(after, "st_file_attributes", 0) & 0x400):
        raise RuntimeError("Evidence manifest changed since inspection.")


def _write_bound_evidence(path: Path, data: bytes, previous: bytes | None) -> None:
    """Write only the inspected evidence file, never follow a replacement link."""
    if previous is None:
        with path.open("xb") as stream:
            stream.write(data)
        return
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or getattr(before, "st_file_attributes", 0) & 0x400):
        raise RuntimeError("Evidence manifest is not an unlinked regular file.")
    with path.open("r+b") as stream:
        opened = os.fstat(stream.fileno())
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or stream.read(len(previous) + 1) != previous):
            raise RuntimeError("Evidence manifest changed since inspection.")
        stream.seek(0)
        stream.write(data)
        stream.truncate()
    after = path.lstat()
    if ((after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
            or getattr(after, "st_file_attributes", 0) & 0x400):
        raise RuntimeError("Evidence manifest identity changed during update.")

EVIDENCE_SCHEMA = "catia-pdf-evidence/1"

# A number with an optional decimal part. Dimension text on drawings also
# carries prefixes/suffixes (Ø, R, 4x, THRU, ±.2), handled by the callers.
_NUMBER_RE = re.compile(r"(?<![\d.])[-+]?(?:\d+\.\d+|\d+|\.\d+)(?![\d.])")
def _work_root() -> Path:
    configured = os.environ.get("CATIA_MCP_WORK_DIR")
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "catia-mcp" / "pdf-modeling"


def _looks_like_pdf(path: Path) -> bool:
    if path.suffix.lower() == ".pdf":
        return True
    try:
        with path.open("rb") as fh:
            return b"%PDF-" in fh.read(1024)
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mm(value: float) -> float:
    return round(value * 25.4 / 72.0, 3)


def _extract_spans(page: Any, page_index: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (spans, icons) with stable ids and millimetre boxes."""
    spans: list[dict[str, Any]] = []
    icons: list[dict[str, Any]] = []
    try:
        data = page.get_text("dict")
    except Exception as exc:  # a malformed page must not look like an empty drawing
        raise RuntimeError(f"page {page_index + 1}: text extraction failed: {exc}") from exc
    span_index = 0
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for item in line.get("spans", []):
                text = (item.get("text") or "").strip()
                if not text:
                    continue
                span_index += 1
                bbox = item.get("bbox") or (0, 0, 0, 0)
                spans.append(
                    {
                        "id": f"p{page_index + 1}:s{span_index}",
                        "text": text,
                        "bboxMm": [_mm(bbox[0]), _mm(bbox[1]), _mm(bbox[2]), _mm(bbox[3])],
                        "sizePt": round(float(item.get("size", 0.0)), 2),
                        "font": item.get("font"),
                    }
                )
    return spans, icons


def _ocr_spans(
    page: Any,
    page_index: int,
    *,
    dpi: int = 400,
    min_confidence: float = 0.5,
) -> list[dict[str, Any]]:
    """Render the page and OCR it, returning spans with ``p<n>:o<i>`` ids.

    NIST FTC/CTC drawings carry their dimensions as vector outlines rather
    than text runs (the text layer holds notes only), so the text-span gate
    cannot see them. OCR gives those numbers a citable id, a region and a
    confidence; the validator then applies the same "the cited number must
    appear in the span text" rule as for a text layer.
    """
    import subprocess
    import sys

    helper = Path(__file__).resolve().parents[2] / "scripts" / "ocr_image.py"
    if not helper.is_file():
        raise RuntimeError(f"OCR helper is missing: {helper}")

    pixmap = page.get_pixmap(dpi=dpi)
    png = pixmap.tobytes("png")
    del pixmap  # Release the full sheet before starting the OCR process.
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        completed = subprocess.run(
            [os.environ.get("CATIA_OCR_PYTHON") or sys.executable,
             str(helper), "-", str(min_confidence)],
            input=png,
            capture_output=True,
            timeout=120,
            # subprocess creates a fresh input pipe; MCP stdio is not inherited.
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("OCR subprocess timed out after 120 s.") from exc
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        detail = (stderr or stdout).strip()[-500:]
        raise RuntimeError(
            f"OCR subprocess failed (exit {completed.returncode}): {detail or 'no diagnostics'}"
        )
    try:
        result = json.loads(stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"OCR subprocess returned invalid JSON: {stdout[:300]!r}"
        ) from exc

    spans: list[dict[str, Any]] = []
    for entry in result:
        text = str(entry.get("text") or "").strip()
        confidence = float(entry.get("confidence") or 0.0)
        box = entry.get("bboxPx")
        if not text or confidence < min_confidence or not box:
            continue
        spans.append(
            {
                "id": "",
                "text": text,
                "source": "ocr",
                "confidence": round(confidence, 3),
                "bboxMm": [
                    round(float(box[0]) * 25.4 / dpi, 3),
                    round(float(box[1]) * 25.4 / dpi, 3),
                    round(float(box[2]) * 25.4 / dpi, 3),
                    round(float(box[3]) * 25.4 / dpi, 3),
                ],
            }
        )
    # Reading order makes ids reproducible across runs of the same engine.
    spans.sort(key=lambda span: (round(span["bboxMm"][1], 1), round(span["bboxMm"][0], 1)))
    for index, span in enumerate(spans, 1):
        span["id"] = f"p{page_index + 1}:o{index}"
    return spans


def _extract_drawing_summary(page: Any) -> dict[str, Any]:
    """Cheap vector summary: how much line work exists and where it sits."""
    try:
        drawings = page.get_drawings()
    except Exception:
        return {"pathCount": 0, "bboxMm": None}
    if not drawings:
        return {"pathCount": 0, "bboxMm": None}
    xs: list[float] = []
    ys: list[float] = []
    for path in drawings:
        rect = path.get("rect")
        if rect is None:
            continue
        xs.extend([rect.x0, rect.x1])
        ys.extend([rect.y0, rect.y1])
    bbox = None
    if xs and ys:
        bbox = [_mm(min(xs)), _mm(min(ys)), _mm(max(xs)), _mm(max(ys))]
    return {"pathCount": len(drawings), "bboxMm": bbox}


def _dimension_candidates(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Numeric text spans that could be dimensions, with their evidence id.

    This is a *candidate* list, not an interpretation: the caller still has to
    decide which candidate is the length, the thickness or a hole callout.
    """
    out: list[dict[str, Any]] = []
    for span in spans:
        text = span["text"]
        matches = _NUMBER_RE.findall(text)
        if not matches:
            continue
        values = []
        for raw in matches:
            try:
                values.append(float(raw))
            except ValueError:
                continue
        entry: dict[str, Any] = {"id": span["id"], "text": text, "values": values}
        upper = text.upper()
        if "Ø" in text or "DIA" in upper:
            entry["kindHint"] = "diameter"
        elif text.strip().upper().startswith("R"):
            entry["kindHint"] = "radius"
        if "THRU" in upper or "THROUGH" in upper:
            entry["throughHint"] = True
        if re.match(r"^\s*\d+\s*[xX×]\s", text):
            entry["countHint"] = int(re.match(r"^\s*(\d+)\s*[xX×]", text).group(1))  # type: ignore[union-attr]
        out.append(entry)
    return out


def _unit_hints(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hints = []
    for span in spans:
        for unit in sorted(drawing_units(span["text"]), reverse=True):
            hints.append({"id": span["id"], "text": span["text"], "unit": unit})
    return hints


def _drawing_context(path: str, pdf_sha256: str, data: bytes | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spans, source, _ = _load_drawing_context(path, pdf_sha256, data=data)
    return spans, source


def _read_context_bytes(source: Path) -> bytes:
    with source.open("rb") as context_file:
        return context_file.read(4 * 1024 * 1024 + 1)


_REFERENCE_ID = re.compile(r"rg:(?:cyl|plane|cone|torus|profile|section):[0-9]+\Z")
_REFERENCE_KINDS = {
    "cylinder", "plane", "cone", "torus", "planar_profile", "revolve_section",
}
_REFERENCE_NUMERIC_FIELDS = (
    "radiusMm", "offsetMm", "areaMm2", "semiAngleDeg",
    "majorRadiusMm", "minorRadiusMm",
)


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _reference_number(value: float) -> str:
    return f"{float(value):.10g}"


def _validate_reference_geometry(records: Any) -> None:
    if not isinstance(records, list) or len(records) > 20000:
        raise ValueError("Invalid drawing reference geometry records")
    seen = set()
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("Invalid drawing reference geometry item")
        item_id = item.get("id")
        if (not isinstance(item_id, str) or not _REFERENCE_ID.fullmatch(item_id)
                or item_id in seen):
            raise ValueError("Invalid or duplicate drawing reference geometry id")
        kind = item.get("kind")
        if kind not in _REFERENCE_KINDS:
            raise ValueError("Invalid drawing reference geometry kind")
        for key in _REFERENCE_NUMERIC_FIELDS:
            if key in item and not _finite_number(item[key]):
                raise ValueError(f"Drawing reference geometry {key} must be finite")
        if "through" in item and type(item["through"]) is not bool:
            raise ValueError("Drawing reference geometry through must be boolean")
        if "role" in item and item["role"] not in {"hole", "boss", "unknown"}:
            raise ValueError("Drawing reference geometry role is invalid")
        seen.add(item_id)


def _reference_geometry_values(item: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {"kind": item["kind"]}
    for key in _REFERENCE_NUMERIC_FIELDS:
        if key in item:
            values[key] = float(item[key])
    if item["kind"] == "cylinder" and "radiusMm" in item:
        values["diameterMm"] = 2.0 * float(item["radiusMm"])
    if "axialRangeMm" in item:
        axial = item["axialRangeMm"]
        if (not isinstance(axial, list) or len(axial) != 2
                or not all(_finite_number(value) for value in axial)):
            raise ValueError("Drawing reference geometry axial range must be two finite numbers")
        values["axialMinMm"] = float(axial[0])
        values["axialMaxMm"] = float(axial[1])
    if "through" in item:
        values["through"] = bool(item["through"])
    if "role" in item:
        values["role"] = str(item["role"])
    bbox = item.get("bboxMm")
    if bbox is not None:
        if (not isinstance(bbox, list) or len(bbox) != 2
                or any(not isinstance(point, list) or len(point) != 2
                       or not all(_finite_number(value) for value in point)
                       for point in bbox)):
            raise ValueError("Drawing reference geometry bbox must be two finite 2D points")
        values.update({"profileMinXMm": float(bbox[0][0]), "profileMinYMm": float(bbox[0][1]),
                       "profileMaxXMm": float(bbox[1][0]), "profileMaxYMm": float(bbox[1][1])})
    return values


def _reference_geometry_text(item: dict[str, Any]) -> str:
    kind = item["kind"]
    if kind == "cylinder":
        diameter = 2.0 * float(item["radiusMm"])
        return f"Ø{_reference_number(diameter)} R{_reference_number(item['radiusMm'])}"
    if kind == "cone":
        return (f"R{_reference_number(item['radiusMm'])} "
                f"{_reference_number(item['semiAngleDeg'])}°")
    if kind == "torus":
        return (f"R{_reference_number(item['majorRadiusMm'])} "
                f"r{_reference_number(item['minorRadiusMm'])}")
    if kind == "plane":
        return f"offset {_reference_number(item['offsetMm'])}"
    if kind == "planar_profile":
        text = (f"profile {item.get('plane')} "
                f"offset {_reference_number(item['offsetMm'])}")
        bbox = item.get("bboxMm")
        if (isinstance(bbox, list) and len(bbox) == 2
                and all(isinstance(point, list) and len(point) == 2 for point in bbox)
                and all(_finite_number(value) for point in bbox for value in point)):
            text += (f" x[{_reference_number(bbox[0][0])}..{_reference_number(bbox[1][0])}]"
                     f" y[{_reference_number(bbox[0][1])}..{_reference_number(bbox[1][1])}]")
        return text
    if kind == "revolve_section":
        return f"section {item.get('axis', 'z')}"
    raise ValueError("Invalid drawing reference geometry kind")


def _reference_geometry_span(item: dict[str, Any], asset_id: str,
                             source_sha256: str, part_id: str | None = None) -> dict[str, Any]:
    item_id = item["id"]
    return {
        "id": f"ctx:referenceGeometry:{item_id}",
        "text": _reference_geometry_text(item),
        "source": "drawing_context",
        "sourceKind": "referenceGeometry",
        "sourceSha256": source_sha256,
        "assetId": asset_id,
        "entityId": item_id,
        "spatialFrameId": "canonical",
        "coordinateSystem": "canonical",
        **({"partId": part_id} if isinstance(part_id, str) and part_id else {}),
        "values": _reference_geometry_values(item),
    }


def _load_drawing_context(path: str, pdf_sha256: str, expected_sha256: str | None = None, *, data: bytes | None = None):
    """Index explicitly supplied 2D context separately from PDF/OCR spans."""
    source = Path(path)
    from_snapshot = data is not None
    data = _read_context_bytes(source) if data is None else data
    if len(data) > 4 * 1024 * 1024:
        raise RuntimeError("Drawing context exceeds 4 MiB.")
    context_digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and context_digest != expected_sha256:
        raise RuntimeError("Drawing context changed since PDF inspection.")
    try:
        payload = json.loads(data)
        if not isinstance(payload, dict) or payload.get("schema") != "catia-drawing-context/1":
            raise ValueError("Unsupported drawing context schema")
        if payload.get("pdfSha256") != pdf_sha256:
            raise RuntimeError("Drawing context PDF hash does not match the inspected PDF.")
        asset_id = payload.get("assetId")
        if not isinstance(asset_id, str) or not asset_id.startswith("drawing_"):
            raise ValueError("Invalid drawing context asset ID")
        hashes = {}
        for item in payload.get("sources", []):
            kind, digest = item["kind"], item["sha256"]
            if kind in hashes or kind not in {"dimensions", "texts", "titleBlock", "geometry", "blocks", "layers", "referenceGeometry"}:
                raise ValueError("Invalid drawing context source")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Invalid drawing context source hash")
            hashes[kind] = digest
        if not {"dimensions", "texts", "titleBlock"}.issubset(hashes):
            raise ValueError("Drawing context source hashes are incomplete")
        if ("geometry" in hashes) != ("geometry" in payload):
            raise ValueError("Drawing geometry records and source hash must occur together")
        if ("blocks" in hashes) != ("blocks" in payload):
            raise ValueError("Drawing block records and source hash must occur together")
        if "blocks" in payload:
            from catia_mcp.drawing.block_context import validate_blocks
            validate_blocks(payload["blocks"], asset_id)
        if ("layers" in hashes) != ("layers" in payload):
            raise ValueError("Drawing layer records and source hash must occur together")
        if "layers" in payload:
            from catia_mcp.drawing.layer_context import validate_layers
            validate_layers(payload["layers"])
        if ("referenceGeometry" in hashes) != ("referenceGeometry" in payload):
            raise ValueError("Drawing reference geometry records and source hash must occur together")
        if "referenceGeometry" in payload:
            _validate_reference_geometry(payload["referenceGeometry"])
        spans, seen = [], set()
        def decode_dxf_escape(match):
            code = int(match.group(1), 16)
            return match.group(0) if 0xD800 <= code <= 0xDFFF else chr(code)
        for kind, text_key in (("dimensions", "display_text"), ("texts", "plain_text"), ("titleBlock", "value")):
            records = payload.get(kind)
            if not isinstance(records, list):
                raise ValueError(f"Invalid drawing context records: {kind}")
            for number, record in enumerate(records, 1):
                if not isinstance(record, dict):
                    raise ValueError(f"Invalid drawing context record: {kind}")
                text = record.get(text_key)
                if not isinstance(text, str) or not text.strip():
                    continue
                # DXF uses four hexadecimal digits; preserve lone surrogates.
                text = re.sub(r"\\U\+([0-9A-Fa-f]{4})", decode_dxf_escape, text)
                entity = record.get("entity_id") if kind != "titleBlock" else str(number)
                if not isinstance(entity, str) or not entity or len(entity) > 256:
                    raise ValueError(f"Invalid drawing context entity: {kind}")
                if kind != "titleBlock" and not entity.startswith(asset_id + ":"):
                    raise ValueError("Drawing context entity belongs to another drawing")
                ref = f"ctx:{kind}:{entity}"
                if ref in seen:
                    raise ValueError("Duplicate drawing context entity")
                seen.add(ref)
                span = {"id": ref, "text": text, "source": "drawing_context",
                              "sourceKind": kind, "sourceSha256": hashes[kind],
                              "assetId": asset_id, "entityId": entity,
                              "spatialFrameId": record.get("spatial_frame_id"),
                              "coordinateSystem": record.get("coordinate_system")}
                if kind == "dimensions":
                    raw = record.get("raw_measurement")
                    if type(raw) in (int, float) and math.isfinite(raw):
                        span["rawMeasurement"] = {
                            "value": raw, "unit": record.get("raw_measurement_unit"),
                            "basis": record.get("measurement_basis"),
                            "dimensionType": record.get("dimension_type"),
                            "textOverride": record.get("text_override"),
                            "textOverridePresent": "text_override" in record,
                            "lengthScaleFactor": record.get("length_scale_factor"),
                            "definitionPoints": record.get("definition_points"),
                            "definitionPointNames": record.get("definition_point_names"),
                            "dimensionRotationDegrees": record.get("dimension_rotation_degrees"),
                            "dimensionExtrusion": record.get("dimension_extrusion"),
                            "coordinateSystem": record.get("coordinate_system"),
                        }
                spans.append(span)
        if "referenceGeometry" in payload:
            for item in payload["referenceGeometry"]:
                span = _reference_geometry_span(
                    item, asset_id, hashes["referenceGeometry"], payload.get("partId")
                )
                if span["id"] in seen:
                    raise ValueError("Duplicate drawing context entity")
                seen.add(span["id"])
                spans.append(span)
        return spans, {"path": str(source.absolute() if from_snapshot else source.resolve()),
                       "sha256": context_digest, "assetId": asset_id,
                       "pdfSha256": pdf_sha256, "sources": dict(hashes)}, payload
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid drawing context: {exc}") from exc


def _geometry_bounds_2d(record: dict[str, Any]) -> tuple[list[float], list[float], bool] | None:
    """Bounds in the record's own 2D frame; an arc's full circle is conservative."""
    geometry, kind = record.get("geometry"), record.get("geometry_type")
    if not isinstance(geometry, dict):
        return None
    def point(value):
        if (not isinstance(value, list) or len(value) < 2
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in value[:2])):
            return None
        return value[:2]
    if kind == "LINE":
        a, b = point(geometry.get("start")), point(geometry.get("end"))
        if a is not None and b is not None:
            return [min(a[i], b[i]) for i in range(2)], [max(a[i], b[i]) for i in range(2)], False
    if kind in {"CIRCLE", "ARC"}:
        centre = point(geometry.get("center_ocs", geometry.get("center")))
        radius = geometry.get("radius")
        if centre is not None and type(radius) in (int, float) and math.isfinite(radius) and radius > 0:
            low, high = [centre[i] - radius for i in range(2)], [centre[i] + radius for i in range(2)]
            if all(math.isfinite(value) for value in (*low, *high)):
                return low, high, kind == "ARC"
    if kind == "LWPOLYLINE":
        vertices, closed, width = geometry.get("vertices_xyseb"), geometry.get("closed"), geometry.get("constant_width")
        if (not isinstance(vertices, list) or not 2 <= len(vertices) <= 20000 or type(closed) is not bool
                or type(width) not in (int, float) or width != 0):
            return None
        for vertex in vertices:
            if (not isinstance(vertex, list) or len(vertex) < 5
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in vertex[:5])
                    or vertex[2] != 0 or vertex[3] != 0):
                return None
        points = [[v[0], v[1]] for v in vertices]
        candidates = list(points)
        conservative = False
        for i in range(len(vertices) if closed else len(vertices) - 1):
            start, end, bulge = points[i], points[(i + 1) % len(points)], vertices[i][4]
            dx, dy = end[0] - start[0], end[1] - start[1]
            chord = math.hypot(dx, dy)
            if not math.isfinite(chord) or (chord <= 1e-12 and bulge != 0):
                return None
            if bulge == 0 or chord <= 1e-12:
                continue
            if abs(bulge) < 1e-8:
                # Avoid subtracting a huge centre/radius for a nearly straight
                # arc. Its sagitta bounds the deviation from the chord.
                padding = chord * abs(bulge) / 2 + max(1e-12, max(map(abs, (*start, *end))) * 1e-15)
                candidates.extend([[min(start[j], end[j]) - padding for j in range(2)],
                                   [max(start[j], end[j]) + padding for j in range(2)]])
                conservative = True
                continue
            centre_offset = chord * (1 - bulge * bulge) / (4 * bulge)
            centre = [(start[0] + end[0]) / 2 - dy / chord * centre_offset,
                      (start[1] + end[1]) / 2 + dx / chord * centre_offset]
            radius = math.hypot(start[0] - centre[0], start[1] - centre[1])
            if not all(math.isfinite(v) for v in (*centre, radius)):
                return None
            start_angle = math.atan2(start[1] - centre[1], start[0] - centre[0])
            sweep = 4 * math.atan(bulge)
            for angle in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
                delta = ((angle - start_angle) if sweep > 0 else (start_angle - angle)) % (2 * math.pi)
                if delta <= abs(sweep) + 1e-12:
                    candidates.append([centre[0] + radius * math.cos(angle), centre[1] + radius * math.sin(angle)])
        if all(math.isfinite(value) for candidate in candidates for value in candidate):
            return ([min(p[i] for p in candidates) for i in range(2)],
                    [max(p[i] for p in candidates) for i in range(2)], conservative)
    return None


def query_context_geometry(path: str, pdf_sha256: str, query: dict[str, Any],
                           expected_context_sha256: str | None = None, *, data: bytes | None = None) -> dict[str, Any]:
    """Page raw, explicitly supplied 2D geometry without assuming a view transform."""
    _, _, payload = _load_drawing_context(path, pdf_sha256, expected_context_sha256, data=data)
    if not isinstance(query, dict) or set(query) - {"frame", "coordinateSystem", "kind", "bbox", "offset", "limit", "summaryOnly"}:
        raise ValueError("Invalid geometry query")
    frame, coordinates, kind = query.get("frame"), query.get("coordinateSystem"), query.get("kind")
    bbox = query.get("bbox")
    offset, limit, summary = query.get("offset", 0), query.get("limit", 30), query.get("summaryOnly", False)
    if ((frame is not None and (not isinstance(frame, str) or len(frame) > 256))
            or (coordinates is not None and (not isinstance(coordinates, str) or len(coordinates) > 256))
            or (kind is not None and (not isinstance(kind, str) or len(kind) > 40))
            or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100
            or type(summary) is not bool):
        raise ValueError("Invalid geometry query parameters")
    if bbox is not None and (not isinstance(bbox, list) or len(bbox) != 4
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in bbox)
            or bbox[0] >= bbox[2] or bbox[1] >= bbox[3] or frame is None or coordinates is None):
        raise ValueError("bbox requires one frame, coordinateSystem and four finite increasing coordinates")
    records = payload.get("geometry")
    if not isinstance(records, list) or len(records) > 20000:
        raise ValueError("Drawing context has no valid 2D geometry")
    asset = payload["assetId"]
    groups = {}
    filtered = []
    uncertain = 0
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get("entity_id"), str)
                or not record["entity_id"].startswith(asset + ":")
                or not isinstance(record.get("spatial_frame_id"), str)
                or not isinstance(record.get("coordinate_system"), str)
                or not isinstance(record.get("geometry_type"), str)):
            raise ValueError("Invalid or mismatched 2D geometry record")
        key = (record["spatial_frame_id"], record["coordinate_system"], record["geometry_type"])
        group = groups.setdefault(key, {"count": 0, "boundedCount": 0, "bounds2D": None,
                                        "boundsConservative": False})
        group["count"] += 1
        bounds = _geometry_bounds_2d(record)
        if bounds is not None:
            low, high, conservative = bounds
            previous = group["bounds2D"]
            group["bounds2D"] = {"min": low, "max": high} if previous is None else {
                "min": [min(previous["min"][i], low[i]) for i in range(2)],
                "max": [max(previous["max"][i], high[i]) for i in range(2)]}
            group["boundedCount"] += 1
            group["boundsConservative"] |= conservative
        if ((frame is None or record["spatial_frame_id"] == frame)
                and (coordinates is None or record["coordinate_system"] == coordinates)
                and (kind is None or record["geometry_type"] == kind)):
            if bbox is not None and bounds is not None:
                low, high, _ = bounds
                if high[0] < bbox[0] or high[1] < bbox[1] or low[0] > bbox[2] or low[1] > bbox[3]:
                    continue
            elif bbox is not None:
                # Unsupported bounds cannot safely establish nonintersection.
                uncertain += 1
            filtered.append(record)
    if len(groups) > 1000:
        raise ValueError("Too many geometry coordinate groups")
    if not summary and len({(item["spatial_frame_id"], item["coordinate_system"]) for item in filtered}) > 1:
        raise ValueError("Choose one frame and coordinateSystem, or use summaryOnly to list groups")
    sources = {item["kind"]: item["sha256"] for item in payload["sources"]}
    return {"assetId": asset, "sourceSha256": sources["geometry"],
            "groups": [{"frame": f, "coordinateSystem": c, "kind": k, **group,
                        "boundsIncomplete": group["boundedCount"] != group["count"]}
                       for (f, c, k), group in sorted(groups.items())],
            "matchedCount": len(filtered), "boundsUnknownCount": uncertain, "offset": offset,
            "records": [] if summary else filtered[offset:offset + limit],
            "nextOffset": None if summary or offset + limit >= len(filtered) else offset + limit,
            "note": "Raw 2D source coordinates and bounds use source units; incomplete bounds may omit entities, and arc bounds include the whole circle. Frames and block-local transforms are not unified. Not PDF evidence or 3D model coordinates."}


def query_context_reference_geometry(path: str, pdf_sha256: str, query: dict[str, Any],
                                     expected_context_sha256: str | None = None, *,
                                     data: bytes | None = None) -> dict[str, Any]:
    """Page canonical reference geometry; it is not a PDF annotation."""
    _, _, payload = _load_drawing_context(path, pdf_sha256, expected_context_sha256, data=data)
    if "referenceGeometry" not in payload:
        raise ValueError("Drawing context has no reference geometry")
    if (not isinstance(query, dict)
            or set(query) - {"kind", "offset", "limit", "summaryOnly"}):
        raise ValueError("Invalid reference geometry query")
    kind = query.get("kind")
    offset, limit = query.get("offset", 0), query.get("limit", 30)
    summary_only = query.get("summaryOnly", False)
    if (kind is not None and (not isinstance(kind, str) or kind not in _REFERENCE_KINDS)
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 100
            or type(summary_only) is not bool):
        raise ValueError("Invalid reference geometry query parameters")
    records = payload["referenceGeometry"]
    filtered = [item for item in records if kind is None or item["kind"] == kind]
    by_kind: dict[str, int] = {}
    for item in records:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    sources = {item["kind"]: item["sha256"] for item in payload["sources"]}
    public = []
    for item in filtered[offset:offset + limit]:
        values = _reference_geometry_values(item)
        public.append({
            "id": item["id"],
            "spanId": f"ctx:referenceGeometry:{item['id']}",
            "kind": item["kind"],
            "text": _reference_geometry_text(item),
            "values": values,
            "role": item.get("role"),
            "through": item.get("through"),
            "faceCount": len(item.get("faceIds", [])),
            "loopCount": len(item.get("loops", [])) if item["kind"] == "planar_profile" else None,
            "segmentCount": len(item.get("segments", [])) if item["kind"] == "revolve_section" else None,
        })
    return {
        "assetId": payload["assetId"],
        "sourceSha256": sources["referenceGeometry"],
        "summary": {"count": len(records), "byKind": dict(sorted(by_kind.items()))},
        "matchedCount": len(filtered),
        "offset": offset,
        "limit": limit,
        "nextOffset": None if offset + limit >= len(filtered) else offset + limit,
        "records": [] if summary_only else public,
        "note": ("Canonical STEP-derived geometry, not PDF text or drawing annotation. "
                 "Cite the returned spanId; the value must match the requirement number."),
    }


def inspect_pdf(
    path: str,
    pages: list[int] | None = None,
    evidence_dir: str | None = None,
    *,
    include_ocr: bool = False,
    ocr_dpi: int = 400,
    ocr_min_confidence: float = 0.5,
    context_path: str | None = None,
    pdf_bytes: bytes | None = None,
    context_bytes: bytes | None = None,
    trusted_previous: Any = _UNBOUND_EVIDENCE,
) -> dict[str, Any]:
    """Return the evidence manifest for ``path`` and persist it to disk.

    ``pages`` is 1-based. A missing file or an unparsable PDF raises
    :class:`RuntimeError` with a message a user can act on; the MCP layer turns
    that into an ``invalid`` decision instead of an empty success.
    """
    source = Path(path).expanduser()
    if pdf_bytes is None and not source.is_file():
        raise RuntimeError(f"Drawing not found: {source}")
    # AgentCPM's upload store writes files under an opaque id with no
    # extension (踩坑 93), so the first bytes decide, not the suffix.
    if not (_looks_like_pdf(source) if pdf_bytes is None else b"%PDF-" in pdf_bytes[:1024]):
        raise RuntimeError(
            f"Only PDF drawings are supported in this version (got '{source.suffix or 'no extension'}', "
            "which is not a PDF header)."
        )

    try:
        import pymupdf  # type: ignore
    except ImportError:  # pragma: no cover - depends on install
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("PyMuPDF is not installed; install 'PyMuPDF' to read PDF drawings.") from exc

    try:
        document = pymupdf.open(str(source)) if pdf_bytes is None else pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise RuntimeError(f"PDF cannot be opened (damaged or truncated file): {exc}") from exc

    if getattr(document, "is_repaired", False):
        document.close()
        raise RuntimeError(
            "PDF is damaged (the parser had to repair it while opening); ask for a clean export."
        )

    try:
        total = document.page_count
        if total == 0:
            raise RuntimeError("PDF has no pages.")
        selected = list(range(1, total + 1)) if pages is None else pages
        bad = [p for p in selected if p < 1 or p > total]
        if bad:
            raise RuntimeError(f"Requested page(s) {bad} outside 1..{total}.")

        source_sha = _sha256(source) if pdf_bytes is None else hashlib.sha256(pdf_bytes).hexdigest()
        page_rows = []
        for number in selected:
            page = document.load_page(number - 1)
            spans, _ = _extract_spans(page, number - 1)
            for span in spans:
                span.setdefault("source", "text")
            ocr_spans: list[dict[str, Any]] = []
            if include_ocr:
                ocr_spans = _ocr_spans(
                    page, number - 1, dpi=ocr_dpi, min_confidence=ocr_min_confidence
                )
            rect = page.rect
            all_spans = spans + ocr_spans
            page_rows.append(
                {
                    "page": number,
                    "widthMm": _mm(rect.width),
                    "heightMm": _mm(rect.height),
                    "spans": all_spans,
                    "textSpanCount": len(spans),
                    "ocrSpanCount": len(ocr_spans),
                    "text": "\n".join(s["text"] for s in all_spans),
                    "dimensionCandidates": _dimension_candidates(all_spans),
                    "unitHints": _unit_hints(all_spans),
                    "drawings": _extract_drawing_summary(page),
                }
            )
    finally:
        document.close()

    target_dir = Path(evidence_dir) if evidence_dir else _work_root() / source_sha[:12]
    manifest_path = target_dir / "evidence.json"
    previous = None
    if trusted_previous is not _UNBOUND_EVIDENCE:
        previous = trusted_previous
        if previous is None and manifest_path.exists():
            raise RuntimeError("Evidence path already exists without a trusted inspection in this session.")
        if previous is not None:
            expected = _bound_evidence_bytes(previous)
            _check_bound_evidence(manifest_path, expected)
    elif manifest_path.exists():
        try:
            candidate = json.loads(manifest_path.read_text(encoding="utf-8"))
            if candidate.get("schema") == EVIDENCE_SCHEMA and candidate.get("source", {}).get("sha256") == source_sha:
                previous = candidate
        except (OSError, ValueError, TypeError):
            pass
    if previous:
        previous_pages = {row["page"]: row for row in previous.get("pages", [])}
        for row in page_rows:
            old = previous_pages.get(row["page"], {})
            old_ocr = [span for span in old.get("spans", []) if span.get("source") == "ocr"]
            if not old_ocr:
                continue
            new_ocr = [span for span in row["spans"] if span.get("source") == "ocr"]
            if include_ocr:
                # OCR ids are positional. Replacing them with a different run
                # would silently change the meaning of previously cited ids.
                if old_ocr != new_ocr:
                    raise RuntimeError("OCR evidence changed for an already inspected page; use a new evidenceDir.")
            else:
                row["spans"].extend(old_ocr)
                row["ocrSpanCount"] = len(old_ocr)
                row["ocrRetained"] = True
                row["text"] = "\n".join(span["text"] for span in row["spans"])
                row["dimensionCandidates"] = _dimension_candidates(row["spans"])
                row["unitHints"] = _unit_hints(row["spans"])
    context_spans, context_source = ([], None)
    if context_path:
        context_spans, context_source = _drawing_context(context_path, source_sha, context_bytes)
        if previous and previous.get("contextSource", {}).get("sha256") == context_source["sha256"]:
            from catia_mcp.drawing.requirements import _is_context_evidence
            for span in previous.get("contextEvidence", []):
                if (isinstance(span, dict) and span.get("sourceKind") == "derived_geometry"
                        and span.get("contextSha256") == context_source["sha256"]
                        and _is_context_evidence(span)):
                    context_spans.append(span)
    elif previous:
        context_spans = previous.get("contextEvidence", [])
        context_source = previous.get("contextSource")

    if not any(row["spans"] for row in page_rows) and not context_spans:
        raise RuntimeError(
            "The PDF has no text layer on the inspected page(s). Use includeOcr=true to read "
            "dimensions drawn as vector outlines or scans, or supply a text-layer PDF."
        )
    warnings = []
    for row in page_rows:
        if row["textSpanCount"] == 0 and row["ocrSpanCount"] == 0:
            warnings.append(
                {
                    "code": "PAGE_NO_TEXT_LAYER",
                    "page": row["page"],
                    "message": f"page {row['page']} has no text spans; its dimensions cannot be cited as evidence.",
                }
            )
        elif row["textSpanCount"] == 0:
            warnings.append(
                {
                    "code": "PAGE_TEXT_LAYER_ABSENT_OCR_USED",
                    "page": row["page"],
                    "message": (
                        f"page {row['page']} has no text layer; {row['ocrSpanCount']} OCR spans "
                        "were used instead. Treat their confidence values as reading evidence, "
                        "not as drawing authority."
                    ),
                }
            )

    target_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": EVIDENCE_SCHEMA,
        "source": {
            "path": str(source.absolute() if pdf_bytes is not None else source.resolve()),
            "sha256": source_sha,
            "bytes": source.stat().st_size if pdf_bytes is None else len(pdf_bytes),
            "pageCount": total,
            "inspectedPages": selected,
        },
        "pages": page_rows,
        "warnings": warnings,
        "ocr": {
            "requested": include_ocr,
            "engine": "rapidocr-onnxruntime" if include_ocr else None,
            "dpi": ocr_dpi if include_ocr else None,
            "minConfidence": ocr_min_confidence if include_ocr else None,
        },
        "howToCite": (
            "Every requirement dimension must cite one or more span ids from this manifest. "
            "A cited span must contain the stated number. OCR spans (p<n>:o<i>) are pixel reads: "
            "cite them only after checking the value against the rendered view."
        ),
    }
    if context_source:
        manifest["contextEvidence"] = context_spans
        manifest["contextSource"] = context_source
        manifest["howToCite"] += " Context ids (ctx:...) come from the separately supplied 2D drawing context, not the PDF."
        if any(span.get("sourceKind") == "derived_geometry" for span in context_spans):
            manifest["howToCite"] += " Derived geometry values are approximate; confirm the selected entities and view before using them."
    # Page-by-page inspection must not erase earlier evidence: a requirement
    # can cite a unit span read on page 1 after the last call inspected page 9.
    stored_pages = page_rows
    inspected = list(selected)
    if previous:
        by_page = {row["page"]: row for row in previous.get("pages", [])}
        by_page.update({row["page"]: row for row in page_rows})
        stored_pages = [by_page[number] for number in sorted(by_page)]
        inspected = sorted(set(previous.get("source", {}).get("inspectedPages", [])) | set(selected))
    stored = {**manifest, "pages": stored_pages}
    stored["source"] = {**manifest["source"], "inspectedPages": inspected}
    if trusted_previous is not _UNBOUND_EVIDENCE:
        _write_bound_evidence(manifest_path, _bound_evidence_bytes(stored),
                              _bound_evidence_bytes(previous) if previous is not None else None)
    else:
        manifest_path.write_text(json.dumps(stored, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest["evidencePath"] = str(manifest_path)
    if trusted_previous is not _UNBOUND_EVIDENCE:
        manifest["_trustedStored"] = stored
    return manifest


def crop_pdf_region(
    path: str,
    page: int,
    bbox_mm: list[float],
    dpi: int = 200,
    output_path: str | None = None,
    *, pdf_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Render one rectangular region (millimetres, page top-left origin) to PNG."""
    source = Path(path).expanduser()
    if pdf_bytes is None and not source.is_file():
        raise RuntimeError(f"Drawing not found: {source}")
    if not (_looks_like_pdf(source) if pdf_bytes is None else b"%PDF-" in pdf_bytes[:1024]):
        raise RuntimeError(f"Not a PDF drawing: {source}")
    if len(bbox_mm) != 4:
        raise RuntimeError("bboxMm must be [x0, y0, x1, y1] in millimetres.")
    x0, y0, x1, y1 = (float(v) for v in bbox_mm)
    if not (0 <= x0 < x1 and 0 <= y0 < y1):
        raise RuntimeError("bboxMm must satisfy 0 <= x0 < x1 and 0 <= y0 < y1.")
    try:
        import pymupdf  # type: ignore
    except ImportError:  # pragma: no cover
        import fitz as pymupdf  # type: ignore

    document = pymupdf.open(str(source)) if pdf_bytes is None else pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        if page < 1 or page > document.page_count:
            raise RuntimeError(f"page {page} outside 1..{document.page_count}.")
        pdf_page = document.load_page(page - 1)
        scale = 72.0 / 25.4
        clip = pymupdf.Rect(x0 * scale, y0 * scale, x1 * scale, y1 * scale)
        pixmap = pdf_page.get_pixmap(dpi=dpi, clip=clip)
    finally:
        document.close()

    sha = (_sha256(source) if pdf_bytes is None else hashlib.sha256(pdf_bytes).hexdigest())[:12]
    target = (
        Path(output_path)
        if output_path
        else _work_root() / sha / f"crop-p{page}-{int(x0)}-{int(y0)}-{int(x1)}-{int(y1)}.png"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(str(target))
    return {
        "ok": True,
        "path": str(target),
        "bytes": target.stat().st_size,
        "page": page,
        "bboxMm": [x0, y0, x1, y1],
        "dpi": dpi,
        "note": "Open this PNG with read_file to inspect the region visually.",
    }
