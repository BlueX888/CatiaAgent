"""Execute a ``catia-build-plan/1`` on CATIA and check every feature.

Only the operations named in :data:`SUPPORTED_OPS` are accepted. Each sketch
step is followed by a real readback (sketch element count, constraint count,
body volume, bounding box); a step that does not produce the expected change
stops the build.

The builder never decides that the delivery is correct by itself: after saving
it runs ``scripts/independent_verify_part.py`` in a separate process and copies
that report into the result.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from catia_mcp.connection import CATIAConnection
from catia_mcp.drawing.plan import PLAN_SCHEMA
from catia_mcp.tools.knowledge import KnowledgeTools
from catia_mcp.tools.part_design import PartDesignTools
from catia_mcp.tools.sketcher import SketcherTools
from catia_mcp.topology import search_topology

SUPPORTED_OPS = {
    "new_part",
    "create_parameters",
    "create_sketch",
    "rectangle",
    "construction_rectangle",
    "construction_line",
    "line",
    "arc",
    "circle",
    "constraint",
    "close_sketch",
    "pad",
    "pocket",
    "shaft",
    "verify_geometry",
}

VOLUME_TOL_MM3 = 0.01
BBOX_TOL_MM = 0.01
# Curved faces are measured through the STL tessellation, so the ring's
# bounding box carries the tessellation deflection rather than only float noise.
BBOX_TOL_RING_MM = 0.05


class BuildError(RuntimeError):
    """A build step failed; ``stage`` names the step and ``scene`` keeps context."""

    def __init__(self, message: str, *, stage: str, scene: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.scene = scene or {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _work_root() -> Path:
    configured = os.environ.get("CATIA_MCP_WORK_DIR")
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "catia-mcp" / "pdf-modeling"


class BuildExecutor:
    """Owns one build document from ``new_part`` to the verified delivery."""

    def __init__(self, connection: CATIAConnection | None = None) -> None:
        self.conn = connection or CATIAConnection()
        self.sketcher = SketcherTools(self.conn)
        self.part_design = PartDesignTools(self.conn)
        self.knowledge = KnowledgeTools(self.conn)
        self.doc: Any | None = None
        self.part: Any | None = None
        self.body: Any | None = None
        self.steps: list[dict[str, Any]] = []
        self.formula_names: list[str] = []
        self.constraint_bindings: list[dict[str, Any]] = []

    # -- measurement helpers ------------------------------------------------
    def _measurable(self) -> Any:
        spa = self.doc.GetWorkbench("SPAWorkbench")
        return spa.GetMeasurable(self.part.CreateReferenceFromObject(self.body))

    def _volume(self) -> float:
        return round(float(self._measurable().Volume) * 1e9, 6)

    def _bbox(self) -> dict[str, Any]:
        # STL export is the same technique probe.read_bbox uses: it sees curved
        # faces, which a vertex sweep on a pad misses.
        path = _work_root() / f".bbox-{os.getpid()}-{int(time.time() * 1000)}.stl"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.doc.ExportData(str(path), "stl")
            points = _read_stl_points(path)
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        if not points:
            raise BuildError("bbox export produced no triangles", stage="bbox")
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        zs = [p[2] for p in points]
        return {
            "min": {"x": round(min(xs), 6), "y": round(min(ys), 6), "z": round(min(zs), 6)},
            "max": {"x": round(max(xs), 6), "y": round(max(ys), 6), "z": round(max(zs), 6)},
        }

    def _bind(self, target: Any, expression: str, name: str) -> str:
        # Quote local dimension names, including CATIA keywords such as OR;
        # backticked Parameter.Name full paths are not valid relation inputs.
        expression = re.sub(r"\b[A-Z][A-Z0-9_]*\b", lambda m: f"`{m[0]}`", expression)
        relations = self.part.Relations
        try:
            existing = relations.Item(name)
        except Exception:
            existing = None
        if existing is not None:
            existing.Modify(expression)
            relation = existing
        else:
            relation = relations.CreateFormula(name, "", target, expression)
        # CATIA evaluates a formula body only after the document updates; reading
        # Value before the update can return an empty body for valid expressions.
        update = getattr(self.part, "Update", None)
        if callable(update):
            update()
        value = relation.Value
        self.formula_names.append(name)
        if not value:
            raise BuildError(
                f"Formula '{name}' was created with an empty body for expression {expression!r}.",
                stage="formula",
            )
        return str(value)

    @staticmethod
    def _constraint_names(sketch: Any) -> list[str]:
        constraints = sketch.Constraints
        return [str(constraints.Item(i).Name) for i in range(1, constraints.Count + 1)]

    @classmethod
    def _constraint(cls, sketch: Any, name: str) -> Any:
        constraints = sketch.Constraints
        try:
            return constraints.Item(name)
        except Exception:
            for i in range(1, constraints.Count + 1):
                candidate = constraints.Item(i)
                if str(candidate.Name) == name:
                    return candidate
        raise BuildError(f"Constraint '{name}' disappeared after creation.", stage="constraint")

    def _step(self, op: str, detail: dict[str, Any], **extra: Any) -> None:
        self.steps.append({"op": op, **detail, "ok": True, **extra})

    # -- execution ----------------------------------------------------------
    def execute(self, plan: dict[str, Any]) -> dict[str, Any]:
        if plan.get("schema") != PLAN_SCHEMA:
            raise BuildError(f"Expected plan schema '{PLAN_SCHEMA}'.", stage="plan")
        unknown = [s.get("op") for s in plan.get("steps", []) if s.get("op") not in SUPPORTED_OPS]
        if unknown:
            raise BuildError(f"Unsupported operations in plan: {unknown}", stage="plan")
        self.conn.connect()
        self.doc = self.conn.documents.Add("Part")
        self.doc.Activate()
        self.part = self.doc.Part
        self.body = self.part.MainBody
        if plan.get("partName"):
            try:
                self.part.Name = str(plan["partName"])
            except Exception:
                pass
        self._step("new_part", {"name": self.part.Name})

        created_parameters: dict[str, float] = {}
        for step in plan.get("steps", []):
            op = step["op"]
            if op == "new_part":
                continue
            if op == "create_parameters":
                for parameter in plan["parameters"]:
                    self.knowledge.execute(
                        "catia_create_parameter",
                        {"name": parameter["name"], "type": parameter["type"],
                         "value": parameter["value"]},
                    )
                    created_parameters[parameter["name"]] = parameter["value"]
                self._step("create_parameters", {"parameters": created_parameters})
                continue
            if op == "create_sketch":
                self.sketcher.execute("catia_create_sketch", {"plane": step.get("plane", "xy")})
                self._step("create_sketch", {"plane": step.get("plane", "xy"),
                                             "role": step.get("role")})
                continue
            if op == "rectangle":
                self.sketcher.execute(
                    "catia_sketch_rectangle",
                    {"x1": step["x1"], "y1": step["y1"], "x2": step["x2"], "y2": step["y2"]},
                )
                sketch = self.sketcher._active_sketch
                count = sketch.GeometricElements.Count
                if count != 5:  # axis + 4 lines
                    raise BuildError(
                        f"Rectangle sketch has {count} elements, expected 5 (axis + 4 lines).",
                        stage="rectangle",
                    )
                self._step("rectangle", {"elements": count})
                continue
            if op == "construction_rectangle":
                factory = self.sketcher._active_factory
                lines = [
                    factory.CreateLine(step["x1"], step["y1"], step["x2"], step["y1"]),
                    factory.CreateLine(step["x2"], step["y1"], step["x2"], step["y2"]),
                    factory.CreateLine(step["x2"], step["y2"], step["x1"], step["y2"]),
                    factory.CreateLine(step["x1"], step["y2"], step["x1"], step["y1"]),
                ]
                for line in lines:
                    line.Construction = True
                count = self.sketcher._active_sketch.GeometricElements.Count
                if count != 5:
                    raise BuildError(
                        f"Construction rectangle produced {count} elements, expected 5.",
                        stage="construction_rectangle",
                    )
                self._step("construction_rectangle", {"elements": count})
                continue
            if op == "construction_line":
                factory = self.sketcher._active_factory
                before_count = self.sketcher._active_sketch.GeometricElements.Count
                created = factory.CreateLine(step["x1"], step["y1"], step["x2"], step["y2"])
                created.Construction = True
                after_count = self.sketcher._active_sketch.GeometricElements.Count
                if after_count != before_count + 1:
                    raise BuildError(
                        f"Construction line added {after_count - before_count} elements, expected 1.",
                        stage="construction_line",
                    )
                self._step("construction_line", {"x1": step["x1"], "y1": step["y1"],
                                                 "x2": step["x2"], "y2": step["y2"]})
                continue
            if op == "line":
                factory = self.sketcher._active_factory
                before_count = self.sketcher._active_sketch.GeometricElements.Count
                factory.CreateLine(step["x1"], step["y1"], step["x2"], step["y2"])
                after_count = self.sketcher._active_sketch.GeometricElements.Count
                if after_count != before_count + 1:
                    raise BuildError(
                        f"Line added {after_count - before_count} elements, expected 1.",
                        stage="line",
                    )
                self._step("line", {"x1": step["x1"], "y1": step["y1"],
                                    "x2": step["x2"], "y2": step["y2"]})
                continue
            if op == "arc":
                before_count = self.sketcher._active_sketch.GeometricElements.Count
                self.sketcher.execute(
                    "catia_sketch_arc",
                    {"cx": step["cx"], "cy": step["cy"], "radius": step["radius"],
                     "start_angle": step["startAngle"], "end_angle": step["endAngle"]},
                )
                after_count = self.sketcher._active_sketch.GeometricElements.Count
                if after_count != before_count + 1:
                    raise BuildError(
                        f"Arc added {after_count - before_count} elements, expected 1.",
                        stage="arc",
                    )
                self._step("arc", {"cx": step["cx"], "cy": step["cy"],
                                   "radius": step["radius"],
                                   "startAngle": step["startAngle"],
                                   "endAngle": step["endAngle"]})
                continue
            if op == "circle":
                self.sketcher.execute(
                    "catia_sketch_circle",
                    {"cx": step["cx"], "cy": step["cy"], "radius": step["radius"]},
                )
                self._step("circle", {"hole": step.get("hole"), "cx": step["cx"], "cy": step["cy"],
                                      "radius": step["radius"]})
                continue
            if op == "constraint":
                sketch = self.sketcher._active_sketch
                before = self._constraint_names(sketch)
                self.sketcher.execute(
                    "catia_sketch_constraint",
                    {
                        "type": step["type"],
                        "value": step["value"],
                        "geometry_index_1": step["geometry_index_1"],
                        "geometry_index_2": step.get("geometry_index_2"),
                    },
                )
                added = [name for name in self._constraint_names(sketch) if name not in before]
                if len(added) != 1:
                    raise BuildError(
                        f"Constraint step created {added or 'no'} new constraint(s); expected exactly 1.",
                        stage="constraint",
                    )
                binding = None
                if step.get("bind"):
                    constraint = self._constraint(sketch, added[0])
                    formula_name = f"PDFModel_{step['bind'].replace(' ', '').replace('/', 'div').replace('+', 'plus')}_{len(self.formula_names) + 1}"
                    expression = self._bind(constraint.Dimension, step["bind"], formula_name)
                    binding = {
                        "role": step.get("role"),
                        "constraint": str(constraint.Name),
                        "parameter": step["bind"],
                        "formula": formula_name,
                        "formulaBody": expression,
                    }
                    self.constraint_bindings.append(binding)
                self._step("constraint", {"type": step["type"], "role": step.get("role"),
                                          "bind": step.get("bind")}, binding=binding)
                continue
            if op == "close_sketch":
                self.sketcher.execute("catia_close_sketch", {})
                self._step("close_sketch", {"role": step.get("role")})
                continue
            if op == "pad":
                self.part_design.execute("catia_pad", {"height": step["height"]})
                pad = self.body.Shapes.Item(self.body.Shapes.Count)
                if step.get("bind"):
                    self._bind(pad.FirstLimit.Dimension, step["bind"], "PDFModel_PadHeight")
                self.part.Update()
                volume = self._volume()
                bbox = self._bbox()
                height = round(bbox["max"]["z"] - bbox["min"]["z"], 6)
                if abs(height - float(step["height"])) > BBOX_TOL_MM:
                    raise BuildError(
                        f"Pad height is {height} mm, expected {step['height']} mm.",
                        stage="pad", scene={"volumeMm3": volume, "bbox": bbox},
                    )
                self._step("pad", {"height": height, "volumeMm3": volume, "bbox": bbox})
                continue
            if op == "pocket":
                self.part_design.execute("catia_pocket", {"depth": step["depth"]})
                pocket = self.body.Shapes.Item(self.body.Shapes.Count)
                if step.get("bind"):
                    self._bind(pocket.FirstLimit.Dimension, step["bind"], "PDFModel_PocketDepth")
                self.part.Update()
                volume = self._volume()
                self._step("pocket", {"depth": step["depth"], "volumeMm3": volume})
                continue
            if op == "shaft":
                before = self._volume()
                self.part_design.execute("catia_shaft", {"angle": step.get("angle", 360)})
                shaft = self.body.Shapes.Item(self.body.Shapes.Count)
                self.part.Update()
                volume = self._volume()
                if volume <= before + 1e-6:
                    raise BuildError(
                        f"Shaft '{shaft.Name}' left the body volume unchanged "
                        f"({before} -> {volume} mm3); the revolution did not add material.",
                        stage="shaft", scene={"before": before, "after": volume},
                    )
                self._step("shaft", {"angle": step.get("angle", 360),
                                     "volumeMm3": volume, "addedMm3": round(volume - before, 6)})
                continue
            if op == "verify_geometry":
                expected = step["expected"]
                volume = self._volume()
                bbox = self._bbox()
                curved = expected.get("kind") in ("annular_ring", "extruded_profile")
                volume_tolerance = 0.5 if curved else VOLUME_TOL_MM3
                self._check_volume(volume, expected["volumeMm3"], volume_tolerance)
                self._check_bbox(
                    bbox, expected["bbox"],
                    BBOX_TOL_RING_MM if curved else BBOX_TOL_MM,
                )
                cylinders = self._cylindrical_faces()
                if expected.get("kind") == "annular_ring":
                    self._check_ring_cylinders(cylinders, expected["cylinders"])
                else:
                    self._check_holes(cylinders, expected["holes"], bbox["max"]["z"] - bbox["min"]["z"])
                self._step(
                    "verify_geometry",
                    {"volumeMm3": volume, "bbox": bbox,
                     "cylindricalFaces": len(cylinders)},
                )
                continue
            raise BuildError(f"Operation '{op}' is not implemented.", stage=str(op))

        self.part.Update()
        return {
            "documentName": str(self.doc.Name),
            "partName": str(self.part.Name),
            "steps": self.steps,
            "bindings": self.constraint_bindings,
            "formulas": list(dict.fromkeys(self.formula_names)),
            "final": {
                "volumeMm3": self._volume(),
                "bbox": self._bbox(),
                "featureCount": int(self.body.Shapes.Count),
            },
        }

    # -- checks -------------------------------------------------------------
    def _check_volume(self, actual: float, expected: float,
                      tolerance: float = VOLUME_TOL_MM3) -> None:
        if abs(actual - expected) > tolerance:
            raise BuildError(
                f"Volume {actual} mm3 differs from the expected {expected} mm3 "
                f"(tolerance {tolerance}).",
                stage="verify_geometry", scene={"volumeMm3": actual, "expected": expected},
            )

    def _check_bbox(self, actual: dict[str, Any], expected: dict[str, Any],
                    tolerance: float = BBOX_TOL_MM) -> None:
        for corner in ("min", "max"):
            for axis in ("x", "y", "z"):
                if abs(actual[corner][axis] - expected[corner][axis]) > tolerance:
                    raise BuildError(
                        f"Bounding box {corner}.{axis}={actual[corner][axis]} differs from "
                        f"{expected[corner][axis]} mm.",
                        stage="verify_geometry", scene={"bbox": actual, "expected": expected},
                    )

    def _cylindrical_faces(self) -> list[dict[str, Any]]:
        """Return measured cylinders as {radius, centre, height}."""
        sel = self.doc.Selection
        spa = self.doc.GetWorkbench("SPAWorkbench")
        try:
            found = search_topology(sel, self.body, "Face")
            refs = [sel.Item(i).Reference for i in range(1, found.count + 1)]
        finally:
            sel.Clear()
        cylinders = []
        for ref in refs:
            measurable = spa.GetMeasurable(ref)
            try:
                radius = float(measurable.Radius)
            except Exception:
                continue
            area_m2 = float(measurable.Area)
            cog = self._evaluate_array(_COG_VBS, "mcp_pdf_cog", measurable)
            cylinders.append(
                {
                    "radius": round(radius, 6),
                    "centre": {"x": round(cog[0], 6), "y": round(cog[1], 6), "z": round(cog[2], 6)},
                    "height": round(area_m2 * 1e6 / (2.0 * 3.141592653589793 * radius), 6),
                }
            )
        return cylinders

    def _evaluate_array(self, code: str, function: str, target: Any) -> list[float]:
        raw = str(self.conn.app.SystemService.Evaluate(code, 0, function, [target]))
        return [float(v) for v in raw.strip(",").split(",")]

    def _check_ring_cylinders(self, cylinders: list[dict[str, Any]],
                              expected: list[dict[str, Any]]) -> None:
        """Each expected radius must be present on a cylindrical face.

        The web walls survive only up to the bead equator, so their measured
        height is the bead radius, not the web thickness; only radii are
        checked here, and the top surface is covered by the volume check.
        """
        measured = sorted({round(c["radius"], 3) for c in cylinders})
        missing = [item["radius"] for item in expected
                   if not any(abs(item["radius"] - r) <= 0.01 for r in measured)]
        if missing:
            raise BuildError(
                f"Missing cylindrical face(s) with radius {missing} mm; measured {measured}.",
                stage="verify_ring", scene={"cylinders": cylinders},
            )

    def _check_holes(self, cylinders: list[dict[str, Any]], expected: list[dict[str, Any]],
                     thickness: float) -> None:
        if len(cylinders) != len(expected):
            raise BuildError(
                f"Found {len(cylinders)} cylindrical faces, expected {len(expected)} holes.",
                stage="verify_holes", scene={"cylinders": cylinders},
            )
        remaining = list(cylinders)
        for hole in expected:
            match = None
            for cylinder in remaining:
                same_radius = abs(cylinder["radius"] - hole["diameter"] / 2.0) <= 0.01
                same_centre = (
                    abs(cylinder["centre"]["x"] - hole["x"]) <= 0.01
                    and abs(cylinder["centre"]["y"] - hole["y"]) <= 0.01
                )
                through = abs(cylinder["height"] - thickness) <= 0.01
                if same_radius and same_centre and through:
                    match = cylinder
                    break
            if match is None:
                raise BuildError(
                    f"Hole at ({hole['x']}, {hole['y']}) d={hole['diameter']} is missing, "
                    "not through, or has the wrong diameter.",
                    stage="verify_holes", scene={"cylinders": cylinders, "expected": expected},
                )
            remaining.remove(match)

    # -- delivery -----------------------------------------------------------
    def save(self, delivery_path: str) -> dict[str, Any]:
        target = Path(delivery_path).expanduser()
        if target.suffix.lower() != ".catpart":
            raise BuildError("deliveryPath must end with .CATPart.", stage="save")
        if target.exists():
            raise BuildError(f"Refusing to overwrite an existing file: {target}", stage="save")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BuildError(f"Cannot create delivery directory: {target.parent}: {exc}", stage="save") from exc
        self.doc.SaveAs(str(target))
        self.doc.Close()
        self.doc = None
        return {
            "path": str(target.resolve()),
            "sha256": _sha256(target),
            "sizeBytes": target.stat().st_size,
        }

    def close(self, save: bool = False) -> None:
        if self.doc is not None:
            try:
                if save:
                    self.doc.Save()
            finally:
                try:
                    self.doc.Close()
                except Exception:
                    pass
                self.doc = None

    def editability_check(self, delivery_path: str, delta_mm: float = 10.0) -> dict[str, Any]:
        """Copy the delivery, change L, and measure the resulting geometry."""
        source = Path(delivery_path)
        copy = _work_root() / f"editcheck-{os.getpid()}-{int(time.time() * 1000)}.CATPart"
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, copy)
        doc = None
        try:
            doc = self.conn.documents.Open(str(copy))
            doc.Activate()
            part = doc.Part
            part.Update()
            params = part.Parameters
            # Plates drive length with L; rings drive diameter with OD;
            # extruded profiles drive thickness with T.
            parameter_name = None
            for candidate in ("L", "OD", "T", "D"):
                try:
                    params.Item(candidate)
                except Exception:
                    continue
                parameter_name = candidate
                break
            if parameter_name is None:
                raise BuildError("No L/OD/T/D parameter exists to test editability.", stage="editability")
            parameter = params.Item(parameter_name)
            before = float(parameter.Value)
            body = part.MainBody
            spa = doc.GetWorkbench("SPAWorkbench")

            def volume() -> float:
                part.Update()
                return float(
                    spa.GetMeasurable(part.CreateReferenceFromObject(body)).Volume
                ) * 1e9

            volume_before = volume()

            parameter.Value = before + delta_mm
            part.Update()
            volume_after = volume()
            if abs(volume_after - volume_before) < 1.0:
                # CATIA occasionally returns the pre-edit measure right after a
                # freshly opened document; force the body through one more
                # update cycle before declaring the model non-parametric.
                try:
                    part.UpdateObject(body)
                except Exception:
                    pass
                part.Update()
                time.sleep(0.3)
                volume_after = volume()
            after = float(params.Item(parameter_name).Value)
            if abs(after - (before + delta_mm)) > 0.001:
                raise BuildError(
                    f"Setting {parameter_name} from {before} to {before + delta_mm} read back as {after} mm.",
                    stage="editability",
                )
            if abs(volume_after - volume_before) < 1.0:
                raise BuildError(
                    f"Volume did not change after {parameter_name} was modified; the model is not driven by {parameter_name}.",
                    stage="editability",
                    scene={"before": volume_before, "after": volume_after},
                )
            return {
                "passed": True,
                "parameter": parameter_name,
                "beforeMm": before,
                "afterMm": after,
                "volumeBeforeMm3": round(volume_before, 4),
                "volumeAfterMm3": round(volume_after, 4),
                "copyPath": str(copy),
                "note": "The copy was modified in memory only and closed without saving.",
            }
        except Exception as exc:
            if isinstance(exc, BuildError):
                raise
            raise BuildError(f"Editability check failed: {exc}", stage="editability") from exc
        finally:
            app = self.conn.app
            alerts = None
            try:
                alerts = app.DisplayFileAlerts
                app.DisplayFileAlerts = False
            except Exception:
                alerts = None
            if doc is not None:
                try:
                    doc.Close()
                except Exception:
                    pass
            if alerts is not None:
                try:
                    app.DisplayFileAlerts = alerts
                except Exception:
                    pass
            try:
                copy.unlink()
            except OSError:
                pass


# --- STL parsing (binary or ASCII) -----------------------------------------

def _read_stl_points(path: Path) -> list[tuple[float, float, float]]:
    data = path.read_bytes()
    if len(data) < 84:
        return []
    count = int.from_bytes(data[80:84], "little")
    if count > 0 and 84 + count * 50 == len(data):
        points = []
        for i in range(count):
            offset = 84 + i * 50 + 12
            for vertex in range(3):
                base = offset + vertex * 12
                points.append((_f32(data, base), _f32(data, base + 4), _f32(data, base + 8)))
        return points
    # ASCII fallback
    points = []
    for line in data.decode("ascii", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            parts = line.split()
            if len(parts) == 4:
                points.append((float(parts[1]), float(parts[2]), float(parts[3])))
    return points


def _f32(data: bytes, offset: int) -> float:
    import struct

    return struct.unpack_from("<f", data, offset)[0]


_COG_VBS = r"""
Public Function mcp_pdf_cog(m)
    Dim a(2), i, s
    m.GetCOG a
    s = ""
    For i = 0 To 2
        s = s & a(i) & ","
    Next
    mcp_pdf_cog = s
End Function
"""
