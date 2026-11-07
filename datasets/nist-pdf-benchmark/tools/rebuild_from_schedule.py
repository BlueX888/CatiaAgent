"""Rebuild a solid from a cross-section stack sidecar and compare to the source.

This is a data-preparation screening check. Matching overall volume does not
prove complete geometry or that unhandled side features are represented.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path

import build123d as bd


def _shared_interior_witness(left, right):
    """Find a point strictly inside both solids, if a sampled point exists."""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_IN
    from OCP.gp import gp_Pnt

    a, b = left.bounding_box(), right.bounding_box()
    bounds = [(max(u, v), min(x, y)) for x, y, u, v in zip(a.max, b.max, a.min, b.min)]
    if any(high <= low for low, high in bounds):
        return None
    left_classifier = BRepClass3d_SolidClassifier(left.wrapped)
    right_classifier = BRepClass3d_SolidClassifier(right.wrapped)
    for fractions in itertools.product((0.25, 0.5, 0.75), repeat=3):
        point = tuple(low + (high - low) * fraction
                      for (low, high), fraction in zip(bounds, fractions))
        sample = gp_Pnt(*point)
        left_classifier.Perform(sample, 1e-7)
        if left_classifier.State() != TopAbs_IN:
            continue
        right_classifier.Perform(sample, 1e-7)
        if right_classifier.State() == TopAbs_IN:
            return point
    return None


def _boolean_volume(operation, left, right, fuzzy=0.0):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.collections import List_TopoDS_Shape

    algo = (BRepAlgoAPI_Common if operation == "common" else BRepAlgoAPI_Cut)()
    arguments, tools = List_TopoDS_Shape(), List_TopoDS_Shape()
    arguments.Append(left.wrapped)
    tools.Append(right.wrapped)
    algo.SetArguments(arguments)
    algo.SetTools(tools)
    algo.SetNonDestructive(True)
    algo.SetFuzzyValue(fuzzy)
    algo.Build()
    if not algo.IsDone() or algo.Shape().IsNull():
        raise ValueError(f"{operation} boolean failed")
    shape = algo.Shape()
    if not BRepCheck_Analyzer(shape).IsValid():
        raise ValueError(f"{operation} boolean returned invalid geometry")
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    volume = abs(float(properties.Mass()))
    if not math.isfinite(volume):
        raise ValueError(f"{operation} boolean returned a nonfinite volume")
    return volume


def _consistent_overlap(left, right):
    volume = float(left.volume)
    limit = min(volume, float(right.volume))
    epsilon = max(1e-6, volume * 2e-6)
    attempts = []
    candidates = []
    witness = None
    witness_checked = False
    for fuzzy in (0.0, 1e-6, 1e-4, 1e-3):
        item = {"fuzzyMm": fuzzy, "consistencyToleranceMm3": epsilon}
        try:
            common = _boolean_volume("common", left, right, fuzzy)
            outside = _boolean_volume("cut", left, right, fuzzy)
            item.update(commonMm3=common, outsideMm3=outside)
            if common > limit + epsilon or abs(common + outside - volume) > epsilon:
                raise ValueError("intersection and difference do not add to input volume")
            if common <= 1e-7:
                if not witness_checked:
                    witness = _shared_interior_witness(left, right)
                    witness_checked = True
                if witness is not None:
                    raise ValueError(f"zero intersection contradicts shared interior point {witness}")
            candidates.append((common, outside, fuzzy))
            item["status"] = "candidate"
        except Exception as exc:
            item.update(status="rejected", reason=f"{type(exc).__name__}: {exc}")
        attempts.append(item)
    if len(candidates) < 2:
        return None, attempts, "fewer than two consistent boolean candidates"
    if (max(c[0] for c in candidates) - min(c[0] for c in candidates) > epsilon or
            max(c[1] for c in candidates) - min(c[1] for c in candidates) > epsilon):
        return None, attempts, "boolean volumes disagree across fuzzy levels"
    # Use the least fuzzy member of the mutually consistent group; do not
    # maximize intersection, which could silently conceal geometric errors.
    return candidates[0], attempts, None


def compare_material(parts, reference, expected_sha256=None):
    """Compare disjoint reconstructed solids in the reference coordinate system.

    A compound's volume can double count overlapping children. Check for such
    overlaps before summing part-by-part intersections with the reference.
    """
    regions = []
    try:
        source = Path(reference).resolve(strict=True)
        if source.suffix.lower() not in {".stp", ".step"}:
            raise ValueError("reference must be a STEP file")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if expected_sha256 and digest.lower() != expected_sha256.lower():
            raise ValueError("reference SHA-256 does not match the schedule source")
        solids = list(bd.import_step(str(source)).solids())
        if len(solids) != 1 or not solids[0].is_valid:
            raise ValueError("reference must contain exactly one valid solid")
        ref = solids[0]
        ref_volume = float(ref.volume)
        if not math.isfinite(ref_volume) or ref_volume <= 0:
            raise ValueError("reference volume is not finite and positive")
        rebuilt = []
        for part in parts:
            part_solids = list(part.solids())
            if len(part_solids) != 1 or not part_solids[0].is_valid:
                raise ValueError("each reconstructed region must contain exactly one valid solid")
            solid = part_solids[0]
            if not math.isfinite(float(solid.volume)) or float(solid.volume) <= 0:
                raise ValueError("reconstructed region volume is not finite and positive")
            rebuilt.append(solid)
        if not rebuilt:
            raise ValueError("no reconstructed solids")
        for index, left in enumerate(rebuilt):
            a = left.bounding_box()
            for right in rebuilt[index + 1:]:
                b = right.bounding_box()
                if all(min(x, y) > max(u, v) for x, y, u, v in zip(a.max, b.max, a.min, b.min)):
                    result, attempts, error = _consistent_overlap(left, right)
                    if error:
                        raise ValueError(f"reconstructed region pair has inconclusive overlap: {error}; {attempts}")
                    if result[0] > 1e-7 or _shared_interior_witness(left, right) is not None:
                        raise ValueError("reconstructed regions overlap; summing intersections is unsafe")
        overlap = 0.0
        for index, solid in enumerate(rebuilt, start=1):
            result, attempts, error = _consistent_overlap(solid, ref)
            regions.append({"region": index, "attempts": attempts,
                            **({"selectedFuzzyMm": result[2]} if result is not None else {})})
            if error:
                raise ValueError(f"region {index}: {error}")
            overlap += result[0]
        total = sum(float(solid.volume) for solid in rebuilt)
        epsilon = max(1e-6, max(total, ref_volume) * 1e-7)
        if overlap > min(total, ref_volume) + epsilon:
            raise ValueError("summed overlap exceeds an input volume")
        return {"status": "measured", "reference": str(source), "referenceSha256": digest,
                "referenceVolumeMm3": ref_volume, "reconstructedVolumeMm3": total,
                "overlapMm3": overlap, "missingMm3": max(0.0, ref_volume - overlap),
                "excessMm3": max(0.0, total - overlap), "regions": regions,
                "limitations": ["Same coordinate frame required; no registration performed.",
                                "Fuzzy booleans are screened for consistency; results remain subject to independent review.",
                                "Screening only; input completeness, small features and parameterization require separate checks."]}
    except Exception as exc:
        return {"status": "inconclusive", "reason": f"{type(exc).__name__}: {exc}", "regions": regions}


def world(u, v, w, u_index, v_index, axis_index):
    point = [0.0, 0.0, 0.0]
    point[u_index] = u
    point[v_index] = v
    point[axis_index] = w
    return tuple(point)


def profile_face(profile, z, indices):
    u_index, v_index, axis_index = indices
    if profile["kind"] == "CIRCLE":
        u, v, radius = profile["u"], profile["v"], profile["radius"]
        u_vec = world(1, 0, 0, u_index, v_index, axis_index)
        w_vec = world(0, 0, 1, u_index, v_index, axis_index)
        plane = bd.Plane(origin=world(0, 0, z, u_index, v_index, axis_index),
                         x_dir=u_vec, z_dir=w_vec)
        return plane * bd.Pos(u, v) * bd.Circle(radius)
    rows = profile.get("rows") or []
    parsed = []
    for row in rows:
        tokens = row.split()
        if not tokens:
            continue
        if tokens[0] == "L":
            parsed.append(("L", (float(tokens[1]), float(tokens[2])),
                           (float(tokens[3]), float(tokens[4]))))
        elif tokens[0] == "U":
            # U <kind> x1 y1 x2 y2 L <length>: a non-circular edge (conic or
            # bspline from a blend or a side-feature cut). Its chord closes the
            # loop, matching uv_path.
            parsed.append(("L", (float(tokens[2]), float(tokens[3])),
                           (float(tokens[4]), float(tokens[5]))))
        elif tokens[0] == "A":
            numbers = [float(token) for token in tokens[1:-1]]
            start = (numbers[3], numbers[4])
            middle = (numbers[5], numbers[6])
            end = (numbers[7], numbers[8]) if len(numbers) >= 9 else None
            parsed.append(("A", start, middle, end))
    if not parsed:
        raise ValueError("Wire profile has no rows")
    def point(sample):
        return world(sample[0], sample[1], z, u_index, v_index, axis_index)

    # Rows are rounded to 1e-4, which is below OCCT's edge-connectivity
    # tolerance: anchor every segment at the previous end and close any
    # residual gap with a short line instead of trusting the printed start.
    first = parsed[0][1]
    previous = first
    edges = []
    for index, item in enumerate(parsed):
        if item[0] == "L":
            a, b = item[1], item[2]
        else:
            a = item[1]
            b = item[3] if item[3] is not None else parsed[(index + 1) % len(parsed)][1]
        if math.dist(previous, b) < math.dist(previous, a):
            a, b = b, a
        if math.dist(previous, b) < 5e-4:
            continue
        if item[0] == "L":
            edges.append(bd.Edge.make_line(point(previous), point(b)))
        else:
            edges.append(bd.ThreePointArc(point(previous), point(item[2]), point(b)))
        previous = b
    if math.dist(previous, first) > 1e-9:
        edges.append(bd.Edge.make_line(point(previous), point(first)))
    wire = bd.Wire(edges)
    return bd.Face(wire)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--sections", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--reference", help="STEP reference in the schedule's coordinate system")
    args = parser.parse_args(argv)
    output = Path(args.out)
    if output.exists() or output.is_symlink():
        raise SystemExit(f"Refusing to overwrite {output}")
    data = json.loads(Path(args.sections).read_text(encoding="utf-8"))
    direction = data["mainDirection"]
    axis_index = max(range(3), key=lambda i: abs(direction[i]))
    u_index, v_index = {2: (0, 1), 0: (1, 2), 1: (2, 0)}[axis_index]
    indices = (u_index, v_index, axis_index)
    profiles = data["profiles"]
    parts = []
    built = 0
    skipped = 0
    for layer in data["layers"]:
        z0, z1 = layer["z0"], layer["z1"]
        for region in layer["regions"]:
            outer_id = next((pid for role, pid in region if role == "OUTER"), None)
            inner_ids = [pid for role, pid in region if role != "OUTER"]
            used = ([outer_id] if outer_id is not None else []) + inner_ids
            if outer_id is None or any(
                    profiles[pid - 1]["kind"] == "WIRE" and not (profiles[pid - 1].get("path") or [None])[0]
                    for pid in used):
                skipped += 1
                continue
            face = profile_face(profiles[outer_id - 1], z0, indices)
            for pid in inner_ids:
                hole = profile_face(profiles[pid - 1], z0, indices)
                face = face - hole
            parts.append(bd.extrude(face, amount=z1 - z0, dir=tuple(direction)))
            built += 1
    if not parts:
        raise SystemExit("Schedule produced no solids")
    # Adjacent layers share a face but do not overlap; a compound sums their
    # volumes exactly, while a 250-solid boolean fuse silently dropped most of
    # the body (FTC-10 with 5 blend samples reconstructs 31468 of 188318 mm3).
    body = bd.Compound(children=parts)
    report = {
        "parts": built,
        "skippedRegions": skipped,
        "volumeMm3": round(float(body.volume), 6),
        "bbox": {"min": [round(v, 4) for v in body.bounding_box().min],
                 "max": [round(v, 4) for v in body.bounding_box().max]},
        "referenceVolumeMm3": data.get("volumeMm3"),
        "sideFeatureCount": len(data.get("sideFeatures", [])),
        "sideFeaturesApplied": False,
        "limitations": ["Volume and bbox do not establish geometric completeness; side features are not explicitly applied."],
    }
    if args.reference:
        report["materialComparison"] = (
            {"status": "inconclusive", "reason": "reconstruction skipped one or more regions"}
            if skipped else compare_material(parts, args.reference, data.get("sourceSha256"))
        )
    with output.open("x", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=1)
    print(json.dumps(report))
    return 2 if args.reference and report["materialComparison"]["status"] == "inconclusive" else 0


if __name__ == "__main__":
    raise SystemExit(main())
