"""Independent verifier for a delivered CATPart.

This script deliberately imports no ``catia_mcp`` code and uses late-bound COM
only. It opens the file from disk, measures it, and compares the measurements
with a frozen expectation JSON. Anything it cannot measure is reported as a
failed check, never as a pass.

Usage:
    python scripts/independent_verify_part.py <spec.json> [--out report.json]

Spec schema (``catia-build-verification/1``)::

    {
      "partPath": "C:/.../part.CATPart",
      "expected": {
        "volumeMm3": 92858.407346,
        "bbox": {"min": {"x": -60, "y": -40, "z": 0},
                 "max": {"x": 60, "y": 40, "z": 10}},
        "holes": [{"x": -40, "y": -20, "diameter": 10, "through": true}],
        "featureCount": 2,
        "parameters": {"L": 120, "W": 80, "T": 10, "D": 10}
      },
      "tolerances": {"lengthMm": 0.01, "coordinateMm": 0.01, "volumeMm3": 0.01}
    }
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import time

VERIFIER_VERSION = "1.0"


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_stl_points(path: str) -> list[tuple[float, float, float]]:
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 84:
        return []
    count = int.from_bytes(data[80:84], "little")
    if count > 0 and 84 + count * 50 == len(data):
        points = []
        for i in range(count):
            base = 84 + i * 50 + 12
            for vertex in range(3):
                offset = base + vertex * 12
                points.append(struct.unpack_from("<fff", data, offset))
        return points
    points = []
    for line in data.decode("ascii", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            parts = line.split()
            if len(parts) == 4:
                points.append((float(parts[1]), float(parts[2]), float(parts[3])))
    return points


_COG_VBS = r"""
Public Function verify_cog(m)
    Dim a(2), i, s
    m.GetCOG a
    s = ""
    For i = 0 To 2
        s = s & a(i) & ","
    Next
    verify_cog = s
End Function
"""

_AXIS_VBS = r"""
Public Function verify_axis(m)
    Dim a(2), i, s
    m.GetAxis a
    s = ""
    For i = 0 To 2
        s = s & a(i) & ","
    Next
    verify_axis = s
End Function
"""


def _progress(spec_path: str, stage: str) -> None:
    """Append a stage marker next to the spec so a hang is diagnosable.

    A verifier that blocks inside COM produces no stdout; the progress file
    tells the operator which API call was in flight.
    """
    try:
        with open(spec_path + ".progress", "a", encoding="utf-8") as fh:
            print(time.strftime("%H:%M:%S"), stage, file=fh)
    except OSError:
        pass


def _check(name: str, passed: bool, expected=None, actual=None, detail: str = "") -> dict:
    return {"name": name, "passed": bool(passed), "expected": expected,
            "actual": actual, "detail": detail}


def _edit_copy_scope(part_path, spec):
    """Saving is opt-in and restricted to a fresh, byte-identical owned copy."""
    baseline = Path(spec["editCopyOf"]).resolve(strict=True)
    target = Path(part_path).resolve(strict=True)
    root = baseline.parent / f"{baseline.stem}.edit-evidence"
    if (not spec.get("parameterChanges") or "volumeMm3" in (spec.get("expected") or {})
            or not baseline.is_file() or not target.is_file()
            or baseline.suffix.lower() != ".catpart" or target.name != baseline.name
            or target.parent.parent != root.resolve() or os.path.samefile(target, baseline)):
        raise ValueError("Edit persistence requires a fresh owned parameter copy and no mixed-kernel volume expectation")
    baseline_hash = _sha256(str(baseline))
    if _sha256(str(target)) != baseline_hash:
        raise ValueError("Edit copy must initially match the unchanged baseline bytes")
    return {"baselinePath": str(baseline), "baselineSha256": baseline_hash,
            "copyPath": str(target), "copyBeforeSha256": baseline_hash}


def _same_document_path(doc, path):
    full_name = doc.FullName
    if not isinstance(full_name, str) or not os.path.isabs(full_name):
        raise ValueError("Document does not expose an absolute full path")
    return os.path.normcase(os.path.realpath(full_name)) == os.path.normcase(os.path.realpath(path))


def _document_is_open(app, path, doc=None):
    documents = app.Documents
    count = documents.Count
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("Documents.Count is invalid")
    found = False
    for index in range(1, count + 1):
        candidate = documents.Item(index)
        if doc is not None and candidate is doc:
            found = True
            continue
        full_name = candidate.FullName
        if isinstance(full_name, str) and os.path.isabs(full_name):
            if os.path.normcase(os.path.realpath(full_name)) == os.path.normcase(os.path.realpath(path)):
                found = True
            continue
        # A new, unsaved CATIA document can have only a simple name as its
        # FullName. It is unrelated only when its independently read Name is
        # the same simple name and differs from the target's basename. Same
        # names and malformed/unreadable identities remain ambiguous.
        name = candidate.Name
        if (not isinstance(full_name, str) or not full_name or not isinstance(name, str)
                or not name or os.path.basename(full_name) != full_name
                or os.path.normcase(name) != os.path.normcase(full_name)
                or os.path.normcase(name) == os.path.normcase(os.path.basename(path))):
            raise ValueError("Open document identity is ambiguous")
    final_count = documents.Count
    if isinstance(final_count, bool) or not isinstance(final_count, int) or final_count != count:
        raise ValueError("Documents collection changed during verification")
    return found


def _close_owned_document(app, doc, path, check_name):
    """Close only the expected document and prove it left the collection."""
    try:
        if not _same_document_path(doc, path):
            raise ValueError("Document path changed; close was not attempted")
        doc.Close()
        if _document_is_open(app, path, doc):
            raise ValueError("Expected document remains open")
    except Exception as exc:
        return _check(check_name, False, "document closed and absent from Documents", None,
                      f"CATIA session state unknown: {type(exc).__name__}: {exc}")
    return _check(check_name, True, "document closed and absent from Documents", "absent")


def _native_volume(doc, part):
    value = float(doc.GetWorkbench("SPAWorkbench").GetMeasurable(
        part.CreateReferenceFromObject(part.MainBody)).Volume) * 1e9
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Native volume must be finite and positive")
    return round(value, 6)


def parallel_axes(actual, expected):
    if actual is None or len(actual) != 3 or len(expected) != 3:
        return False
    lengths = [math.sqrt(sum(v * v for v in axis)) for axis in (actual, expected)]
    if not all(math.isfinite(n) and n > 0 for n in lengths):
        return False
    cosine = sum(a * b for a, b in zip(actual, expected)) / (lengths[0] * lengths[1])
    return abs(abs(cosine) - 1) < 1e-6


def match_full_cylinders(expected, actual, coordinate_tolerance, length_tolerance):
    """Match complete cylindrical faces by radius, 3D COG and axial extent.

    Partial cylindrical faces are not supported by this contract: their area
    does not determine axial extent. No plate thickness or Z-axis assumption.
    """
    remaining = list(actual)
    for wanted in expected:
        match = next((c for c in remaining
                      if abs(c["radius"] - wanted["radius"]) <= coordinate_tolerance
                      and c["height"] is not None
                      and abs(c["height"] - wanted["height"]) <= length_tolerance
                      and len(wanted["centre"]) == 3
                      and ("axis" not in wanted or parallel_axes(c.get("axis"), wanted["axis"]))
                      and all(abs(a - b) <= coordinate_tolerance
                              for a, b in zip(c["centre"], wanted["centre"]))), None)
        if match is None:
            return False
        remaining.remove(match)
    return not remaining


def verify(spec: dict, progress_path: str | None = None) -> dict:
    part_path = spec.get("partPath")
    if part_path:
        # CATIA's Documents.Open rejects a forward-slash path for a CATPart
        # with E_FAIL (verified 2026-10-28); normalize before every use.
        part_path = os.path.normpath(str(part_path))
    expected = spec.get("expected") or {}
    tolerances = spec.get("tolerances") or {}
    length_tol = float(tolerances.get("lengthMm", 0.01))
    coord_tol = float(tolerances.get("coordinateMm", 0.01))
    volume_tol = float(tolerances.get("volumeMm3", 0.01))
    if not all(math.isfinite(t) and t >= 0 for t in (length_tol, coord_tol, volume_tol)):
        return {"verdict": "FAIL", "checks": [_check("tolerances", False, "finite nonnegative values", tolerances)]}
    collect_cylinders = spec.get("collectCylindricalFaces", True)
    cylinder_keys = {"holes", "holeCentres", "cylinders", "fullCylinders"}
    if (type(collect_cylinders) is not bool
            or (not collect_cylinders and (not spec.get("parameterChanges") or cylinder_keys.intersection(expected)))):
        return {"verdict": "FAIL", "checks": [_check(
            "cylindrical_faces_collection", False,
            "skip only for parameter-copy verification without cylinder expectations", collect_cylinders)]}

    def mark(stage: str) -> None:
        if progress_path:
            _progress(progress_path, stage)

    mark("start")
    checks: list[dict] = []
    report: dict = {
        "schema": "catia-build-verification-report/1",
        "verifier": VERIFIER_VERSION,
        "partPath": part_path,
        "checks": checks,
        "measurements": {},
    }

    if not part_path or not os.path.isfile(part_path):
        checks.append(_check("file_exists", False, "a readable CATPart", part_path))
        report["verdict"] = "FAIL"
        return report
    checks.append(_check("file_exists", True, None, os.path.getsize(part_path)))
    report["fileSha256"] = _sha256(part_path)

    edit_scope = None
    if "editCopyOf" in spec:
        try:
            edit_scope = _edit_copy_scope(part_path, spec)
        except Exception as exc:
            checks.append(_check("edit_copy_scope", False, "fresh owned copy", str(exc)))
            report["verdict"] = "FAIL"
            return report
        checks.append(_check("edit_copy_scope", True))
        report["editPersistence"] = edit_scope

    mark("import_com")
    try:
        import win32com.client
    except ImportError:
        checks.append(_check("com_available", False, "pywin32 on Windows", None))
        report["verdict"] = "FAIL"
        return report
    checks.append(_check("com_available", True))

    mark("get_active_object")
    try:
        if os.environ.get("CATIA_REQUIRED_CATTEMP"):
            from catia_mcp.catia_instance import get_active_catia
            app = get_active_catia()
        else:
            app = win32com.client.GetActiveObject("CATIA.Application")
    except Exception as exc:
        checks.append(_check("catia_available", False, "running CATIA instance", str(exc)))
        report["verdict"] = "FAIL"
        return report
    checks.append(_check("catia_available", True))

    mark("open_document")
    doc = None
    try:
        report["sessionHealth"] = "unknown"
        if _document_is_open(app, part_path):
            raise ValueError("Target document is already open; no open, edit, save or close attempted")
        opened = app.Documents.Open(part_path)
        if not _same_document_path(opened, part_path):
            raise ValueError("Documents.Open returned a different document; no edit, save or close attempted")
        doc = opened
        report.pop("sessionHealth", None)
        doc.Activate()
        mark("opened")
        checks.append(_check("reopen", True, "file opens from disk", doc.Name))
        part = doc.Part
        for name, change in spec.get("parameterChanges", {}).items():
            unit = "mm" if change["type"] == "length" else "deg"
            part.Parameters.Item(name).ValuateFromString(f"{change['value']}{unit}")
        part.Update()
        edited_native_volume = None
        if edit_scope is not None:
            mark("measure_edited_before_save")
            edited_native_volume = _native_volume(doc, part)
            if _sha256(edit_scope["baselinePath"]) != edit_scope["baselineSha256"]:
                raise ValueError("Baseline changed before edit-copy save")
            mark("save_edit_copy")
            if not _same_document_path(doc, part_path):
                raise ValueError("Document path changed; save was not attempted")
            doc.Save()
            edit_scope["copySavedSha256"] = _sha256(part_path)
            close_check = _close_owned_document(app, doc, part_path, "edit_copy_closed_before_reopen")
            doc = None
            checks.append(close_check)
            if not close_check["passed"]:
                report["sessionHealth"] = "unknown"
                raise ValueError("Cannot confirm edited copy closed before reopen")
            mark("reopen_saved_edit_copy")
            report["sessionHealth"] = "unknown"
            if _document_is_open(app, part_path):
                raise ValueError("Target document is already open; reopen was not attempted")
            opened = app.Documents.Open(part_path)
            if not _same_document_path(opened, part_path):
                raise ValueError("Saved-copy reopen returned a different document; no measurements or close attempted")
            doc = opened
            report.pop("sessionHealth", None)
            doc.Activate()
            part = doc.Part
            edit_scope["savedAndReopened"] = True
            report["volumeExpectationBasis"] = {"method": "native_edited_copy_before_save",
                "nativeBeforeSaveMm3": edited_native_volume}
            checks.append(_check("edit_copy_save_reopen", True))
        body = part.MainBody

        if expected.get("featureCount") is not None:
            checks.append(
                _check("feature_count", int(body.Shapes.Count) == int(expected["featureCount"]),
                       expected["featureCount"], int(body.Shapes.Count))
            )

        mark("measure_volume")
        spa = doc.GetWorkbench("SPAWorkbench")
        measurable = spa.GetMeasurable(part.CreateReferenceFromObject(body))
        volume = round(float(measurable.Volume) * 1e9, 6)
        report["measurements"]["volumeMm3"] = volume
        volume_expected = edited_native_volume if edit_scope is not None else expected.get("volumeMm3")
        if volume_expected is not None:
            ok = math.isfinite(volume) and abs(volume - float(volume_expected)) <= volume_tol
            checks.append(_check("volume", ok, volume_expected, volume,
                                 f"tolerance {volume_tol} mm3"))
        else:
            checks.append(_check("volume", volume > 0, "> 0", volume))

        # Bounding box from an STL tessellation: a vertex sweep misses the top
        # of a cylindrical hole, the tessellation does not.
        mark("bbox_export")
        stl_dir = tempfile.mkdtemp(prefix="verify-bbox-")
        stl_path = os.path.join(stl_dir, "bbox.stl")
        try:
            doc.ExportData(stl_path, "stl")
            points = _read_stl_points(stl_path)
        finally:
            try:
                os.remove(stl_path)
                os.rmdir(stl_dir)
            except OSError:
                pass
        if not points:
            checks.append(_check("bbox", False, "STL tessellation", None))
        else:
            bbox = {
                "min": {axis: round(min(p[i] for p in points), 6) for i, axis in enumerate("xyz")},
                "max": {axis: round(max(p[i] for p in points), 6) for i, axis in enumerate("xyz")},
            }
            report["measurements"]["bbox"] = bbox
            if expected.get("bbox"):
                ok = True
                detail = []
                for corner in ("min", "max"):
                    for axis in "xyz":
                        want = float(expected["bbox"][corner][axis])
                        got = bbox[corner][axis]
                        if abs(got - want) > length_tol:
                            ok = False
                            detail.append(f"{corner}.{axis}: {got} != {want}")
                checks.append(_check("bbox", ok, expected["bbox"], bbox, "; ".join(detail)))
            else:
                checks.append(_check("bbox_available", True, None, bbox, "Tessellation only; no dimensional expectation supplied"))

        if collect_cylinders:
            # Cylindrical faces -> through-hole evidence. Radius and COG come from
            # SPAWorkbench; the height is derived from the lateral area.
            mark("cylinder_faces")
            selection = doc.Selection
            cylinders: list[dict] = []
            try:
                selection.Clear()
                selection.Add(body)
                count = 0
                for term in ("Type=面,sel", "Type=Face,sel", "Topology.Face,sel"):
                    try:
                        selection.Clear()
                        selection.Add(body)
                        selection.Search(term)
                        count = int(selection.Count)
                        break
                    except Exception:
                        continue
                refs = [selection.Item(i).Reference for i in range(1, count + 1)]
            finally:
                try:
                    selection.Clear()
                except Exception:
                    pass
            for ref in refs:
                face = spa.GetMeasurable(ref)
                try:
                    radius = float(face.Radius)
                except Exception:
                    continue
                try:
                    raw = str(app.SystemService.Evaluate(_COG_VBS, 0, "verify_cog", [face]))
                    cog = [float(v) for v in raw.strip(",").split(",")]
                except Exception:
                    continue
                area_mm2 = float(face.Area) * 1e6
                try:
                    raw_axis = str(app.SystemService.Evaluate(_AXIS_VBS, 0, "verify_axis", [face]))
                    axis = [float(v) for v in raw_axis.strip(",").split(",")]
                except Exception:
                    axis = None
                height = area_mm2 / (2.0 * math.pi * radius) if radius > 0 else None
                cylinders.append(
                    {
                        "radius": round(radius, 6),
                        "diameter": round(2 * radius, 6),
                        "centre": [round(cog[0], 6), round(cog[1], 6), round(cog[2], 6)],
                        "height": None if height is None else round(height, 6),
                        "axis": axis,
                    }
                )
            report["measurements"]["cylindricalFaces"] = cylinders

        else:
            report["measurements"]["cylindricalFacesStatus"] = "skipped_for_parameter_copy"

        if "fullCylinders" in expected:
            checks.append(_check("full_cylinders", match_full_cylinders(
                expected["fullCylinders"], cylinders, coord_tol, length_tol),
                expected["fullCylinders"], cylinders))

        expected_cylinders = expected.get("cylinders")
        if expected_cylinders is not None:
            remaining = list(cylinders)
            ok = True
            detail = []
            for item in expected_cylinders:
                radius = float(item["radius"])
                wanted = int(item.get("count", 1))
                matches = [c for c in remaining if abs(c["radius"] - radius) <= coord_tol]
                if len(matches) < wanted:
                    ok = False
                    detail.append(f"R{radius}: wanted {wanted}, found {len(matches)}")
                    continue
                for match in matches[:wanted]:
                    remaining.remove(match)
            if remaining:
                ok = False
                detail.append("unexpected cylindrical faces: "
                              + ", ".join(f"R{c['radius']}" for c in remaining))
            checks.append(
                _check("ring_cylinders", ok, expected_cylinders,
                       [c["radius"] for c in cylinders], "; ".join(detail))
            )

        holes = expected.get("holes")
        if holes is None and expected.get("holeCentres") is not None:
            # Older/hand-written specs list centres and one diameter separately.
            diameter = expected.get("holeDiameter")
            if diameter is not None:
                holes = [
                    {"x": pair[0], "y": pair[1], "diameter": diameter, "through": True}
                    for pair in expected["holeCentres"]
                ]
        if holes is not None:
            thickness = None
            if expected.get("bbox"):
                thickness = (float(expected["bbox"]["max"]["z"])
                             - float(expected["bbox"]["min"]["z"]))
            checks.append(_check("hole_count", len(cylinders) == len(holes),
                                 len(holes), len(cylinders)))
            remaining = list(cylinders)
            all_diameter = True
            all_centre = True
            all_through = True
            missing = []
            centre_errors = []
            for hole in holes:
                radius = float(hole["diameter"]) / 2.0
                candidates = [
                    c for c in remaining if abs(c["radius"] - radius) <= coord_tol
                ]
                if not candidates:
                    missing.append(f"D{hole['diameter']} at ({hole['x']}, {hole['y']})")
                    all_diameter = False
                    all_centre = False
                    all_through = False
                    continue
                match = min(
                    candidates,
                    key=lambda c: ((c["centre"][0] - float(hole["x"])) ** 2
                                   + (c["centre"][1] - float(hole["y"])) ** 2),
                )
                remaining.remove(match)
                dx = match["centre"][0] - float(hole["x"])
                dy = match["centre"][1] - float(hole["y"])
                if abs(dx) > coord_tol or abs(dy) > coord_tol:
                    all_centre = False
                    centre_errors.append(
                        f"expected ({hole['x']}, {hole['y']}), measured "
                        f"({match['centre'][0]}, {match['centre'][1]})"
                    )
                if hole.get("through", True):
                    if thickness is None or match["height"] is None:
                        all_through = False
                    elif abs(match["height"] - thickness) > length_tol:
                        all_through = False
            if missing:
                centre_errors.append("missing: " + ", ".join(missing))
            checks.append(_check("hole_diameter", all_diameter,
                                 [h["diameter"] for h in holes],
                                 [c["diameter"] for c in cylinders],
                                 "missing: " + ", ".join(missing) if missing else ""))
            checks.append(_check("hole_centres", all_centre,
                                 [[h["x"], h["y"]] for h in holes],
                                 [c["centre"][:2] for c in cylinders],
                                 "; ".join(centre_errors)))
            checks.append(_check("hole_through", all_through,
                                 "every hole spans the full thickness",
                                 [c["height"] for c in cylinders]))

        parameters = expected.get("parameters")
        if parameters:
            params = part.Parameters
            actual_params = {}
            for name, want in parameters.items():
                try:
                    actual = float(params.Item(name).Value)
                except Exception:
                    actual = None
                actual_params[name] = actual
            report["measurements"]["parameters"] = actual_params
            ok = all(
                actual_params.get(name) is not None
                and abs(actual_params[name] - float(want)) <= length_tol
                for name, want in parameters.items()
            )
            checks.append(_check("parameters", ok, parameters, actual_params))

            relations = part.Relations
            formula_bodies = []
            for i in range(1, relations.Count + 1):
                relation = relations.Item(i)
                try:
                    formula_bodies.append(str(relation.Value))
                except Exception:
                    continue
            report["measurements"]["formulaBodies"] = formula_bodies
            drives_geometry = any(
                any(name in body for name in parameters) for body in formula_bodies
            )
            checks.append(
                _check("parametric_formulas", drives_geometry,
                       "at least one formula references a user parameter",
                       formula_bodies[:12])
            )
        if "formulas" in expected:
            actual_formulas = {}
            for i in range(1, part.Relations.Count + 1):
                relation = part.Relations.Item(i)
                actual_formulas[str(relation.Name)] = str(relation.Value)
            normalize = lambda s: "".join(s.split())
            checks.append(_check("named_formulas", all(
                name in actual_formulas and normalize(actual_formulas[name]) == normalize(value)
                for name, value in expected["formulas"].items()), expected["formulas"], actual_formulas))
        if spec.get("exportStep"):
            step_path = os.path.normpath(spec["exportStep"])
            if os.path.splitext(step_path)[1].lower() not in {".step", ".stp"} or os.path.exists(step_path):
                raise ValueError("STEP export needs a new .step/.stp path")
            doc.ExportData(step_path, "stp")
            report["stepExport"] = {"path": step_path, "sha256": _sha256(step_path)}
    except Exception as exc:
        checks.append(_check("measurement_error", False, "all measurements", f"{type(exc).__name__}: {exc}"))
    finally:
        mark("close")
        if doc is not None:
            close_check = _close_owned_document(app, doc, part_path, "document_closed")
            checks.append(close_check)
            if not close_check["passed"]:
                report["sessionHealth"] = "unknown"
        if edit_scope is not None:
            try:
                baseline_hash = _sha256(edit_scope["baselinePath"])
                checks.append(_check("edit_baseline_unchanged", baseline_hash == edit_scope["baselineSha256"],
                                     edit_scope["baselineSha256"], baseline_hash))
            except Exception as exc:
                checks.append(_check("edit_baseline_unchanged", False, edit_scope["baselineSha256"], None,
                                     f"Hash unavailable: {type(exc).__name__}: {exc}"))
                report["sessionHealth"] = "unknown"
            try:
                report["fileSha256"] = _sha256(part_path)
            except Exception as exc:
                report.pop("fileSha256", None)
                checks.append(_check("edit_copy_hash", False, "readable copy hash", None,
                                     f"Hash unavailable: {type(exc).__name__}: {exc}"))
                report["sessionHealth"] = "unknown"

    report["verdict"] = "PASS" if checks and all(c["passed"] for c in checks) else "FAIL"
    mark("done " + report["verdict"])
    return report


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Independent CATPart verifier")
    parser.add_argument("spec", help="verification spec JSON")
    parser.add_argument("--out", help="write the report here as well as stdout")
    args = parser.parse_args()

    with open(args.spec, encoding="utf-8") as fh:
        spec = json.load(fh)
    report = verify(spec, progress_path=args.spec)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0 if report.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
