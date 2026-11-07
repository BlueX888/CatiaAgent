"""Independent OCCT evaluation of the closed feature language, outside COM.

Only drawing-derived requirements are consumed. No dataset reference geometry
or model scripts enter this path. This does not validate drawing interpretation.
"""
from __future__ import annotations

import math
from pathlib import Path
import tempfile

from catia_mcp.drawing.feature_model import FeatureError, compile_features, arc_geometry


class CandidateRejected(ValueError):
    """A candidate failed a known, explicitly checked requirement."""


def _select_edge(body, selector, fid):
    """Resolve a geometric edge selector on the OCCT body, uniquely."""
    import build123d as bd

    tolerance = 1e-3
    matches = []
    seen = set()
    for edge in body.edges():
        try:
            kind = edge.geom_type
        except Exception:
            continue
        if selector["type"] == "circular":
            if kind != bd.GeomType.CIRCLE:
                continue
            try:
                radius = float(edge.radius)
            except Exception:
                continue
            if abs(radius - selector["radius"]["value"]) > tolerance:
                continue
            centre = edge.arc_center
            wanted = [point["value"] for point in selector["centre"]]
            if math.dist([centre.X, centre.Y, centre.Z], wanted) > tolerance:
                continue
            first, second = edge.tangent_at(0.0), edge.tangent_at(0.25)
            axis = [first.Y * second.Z - first.Z * second.Y,
                    first.Z * second.X - first.X * second.Z,
                    first.X * second.Y - first.Y * second.X]
            actual, expected = _unit(axis), _unit(selector["axis"])
            if abs(sum(a * b for a, b in zip(actual, expected))) < 1 - 1e-6:
                continue
            closed = abs(edge.length - 2 * math.pi * radius) <= 1e-3
            key = ("circular", round(radius, 6), round(centre.X, 6),
                   round(centre.Y, 6), round(centre.Z, 6),
                   "closed" if closed else f"arc-{id(edge)}")
        else:
            if kind != bd.GeomType.LINE:
                continue
            point, tangent = edge.position_at(0.5), edge.tangent_at(0.5)
            actual, expected = _unit([tangent.X, tangent.Y, tangent.Z]), _unit(selector["direction"])
            if abs(sum(a * b for a, b in zip(actual, expected))) < 1 - 1e-6:
                continue
            through = [item["value"] for item in selector["through"]]
            offset = [point.X - through[0], point.Y - through[1], point.Z - through[2]]
            cross = [offset[1] * expected[2] - offset[2] * expected[1],
                     offset[2] * expected[0] - offset[0] * expected[2],
                     offset[0] * expected[1] - offset[1] * expected[0]]
            if math.sqrt(sum(value * value for value in cross)) > tolerance:
                continue
            if selector.get("withinSegment") and abs(sum(a * b for a, b in zip(offset, actual))) > edge.length / 2 + tolerance:
                continue
            key = ("linear", round(point.X, 6), round(point.Y, 6), round(point.Z, 6))
        if key in seen:
            continue
        seen.add(key)
        matches.append(edge)
    if len(matches) != 1:
        raise ValueError(f"EDGE_SELECTION_MISMATCH: selector matched {len(matches)} edges; "
                         "exactly one is required")
    return matches[0]


def _unit(vector):
    length = math.sqrt(sum(value * value for value in vector))
    if not length:
        return [0.0, 0.0, 0.0]
    return [value / length for value in vector]


def evaluate_features(spec):
    import build123d as bd
    from catia_mcp.cad_support import _fields, _volume, compare_steps, inspect_shape

    def inspect_finite(shape):
        diagnostic = inspect_shape(shape)
        if (not math.isfinite(diagnostic["volumeMm3"])
                or not all(math.isfinite(volume) for volume in diagnostic["solidVolumesMm3"])):
            raise ValueError("Nonfinite geometry volume")
        return diagnostic

    _fields(spec, {"requirements", "actual", "lengthToleranceMm", "volumeToleranceMm3"}, {"requirements"})
    try:
        plan = compile_features(spec["requirements"], None, require_evidence=False)
    except FeatureError as exc:
        raise CandidateRejected(str(exc)) from exc
    body = None
    steps = []
    for feature in plan["features"]:
        fid, operation = feature["id"], feature["operation"]
        try:
            if operation in {"fillet", "chamfer"}:
                if body is None:
                    raise CandidateRejected("SINGLE_SOLID_REQUIRED: a finishing feature needs a solid")
                selectors = feature["edges"] if isinstance(feature["edges"],list) else [feature["edges"]]
                edges = [_select_edge(body,selector,fid) for selector in selectors]
                if any(edge.wrapped.IsSame(other.wrapped) for i,edge in enumerate(edges) for other in edges[:i]):
                    raise ValueError("EDGE_SELECTION_MISMATCH: group selectors resolve to the same edge")
                before = steps[-1]["afterMm3"]
                size = feature["size"]["value"]
                body = (body.fillet(size, edges) if operation == "fillet"
                        else body.chamfer(size, None, edges))
                diagnostic = inspect_finite(body)
                if not diagnostic["valid"] or diagnostic["solidCount"] != 1:
                    raise CandidateRejected("SINGLE_SOLID_REQUIRED: feature produced invalid or disconnected geometry")
                after = diagnostic["volumeMm3"]
                delta = after - before
                if not math.isfinite(delta):
                    raise ValueError("Nonfinite geometry volume change")
                if abs(delta) <= max(1e-6, after * 1e-9):
                    raise CandidateRejected("FEATURE_NO_EFFECT: no material change")
                steps.append({"featureId": fid, "operation": operation, "beforeMm3": before,
                              "afterMm3": after, "deltaMm3": delta, "geometry": diagnostic})
                continue
            sketch = feature["sketch"]
            profiles = []
            for p in sketch["profiles"]:
                if p["type"] == "wire":
                    start = tuple(p["start"][k]["value"] for k in ("x", "y"))
                    edges = []
                    for segment in p["segments"]:
                        end = tuple(segment["end"][k]["value"] for k in ("x", "y"))
                        if segment["type"] == "arc":
                            radius = segment["radius"]["value"]
                            center, first, sweep = arc_geometry(start, end, radius, segment["clockwise"], segment["largeArc"])
                            middle = (center[0] + radius * math.cos(first + sweep / 2),
                                      center[1] + radius * math.sin(first + sweep / 2))
                            edge = bd.ThreePointArc(start, middle, end).edge()
                        else:
                            edge = bd.Edge.make_line(start, end)
                        edges.append(edge)
                        start = end
                    wire = bd.Wire(edges)
                    shape = bd.Face(wire)
                    if not wire.is_closed or not shape.is_valid or shape.area <= 1e-8:
                        raise CandidateRejected("PROFILE_INVALID: wire must enclose a valid simple face")
                    for i, edge in enumerate(edges):
                        for j in range(i + 2, len(edges)):
                            if i == 0 and j == len(edges) - 1:
                                continue
                            if edge.distance_to(edges[j]) <= 1e-7:
                                raise CandidateRejected("PROFILE_INTERSECTION: wire crosses or touches itself")
                elif p["type"] == "polygon":
                    shape = bd.Polygon(*[(v["x"]["value"], v["y"]["value"]) for v in p["points"]], align=None)
                elif p["type"] == "rectangle":
                    x, y = p["x"]["value"], p["y"]["value"]
                    shape = bd.Pos(x, y) * bd.Rectangle(p["width"]["value"], p["height"]["value"], align=(bd.Align.MIN, bd.Align.MIN))
                else:
                    x, y = p["x"]["value"], p["y"]["value"]
                    shape = bd.Pos(x, y) * bd.Circle(p["radius"]["value"])
                profiles.append(shape)
            profiles.sort(key=lambda p: p.area, reverse=True)
            section = None
            roots = []
            for i, profile in enumerate(profiles):
                depth = 0
                for outer in profiles[:i]:
                    if profile.wires()[0].distance_to(outer.wires()[0]) <= 1e-7:
                        raise CandidateRejected("PROFILE_INTERSECTION: contours touch or cross")
                    overlap = profile & outer
                    if overlap and overlap.area > 1e-7:
                        if abs(overlap.area - profile.area) > 1e-7:
                            raise CandidateRejected("PROFILE_INTERSECTION: contours overlap")
                        depth += 1
                if depth == 0:
                    roots.append(profile)
                section = profile if section is None else (section - profile if depth % 2 else section + profile)
            frame, offset = sketch["plane"], sketch["offset"]["value"]
            if isinstance(frame, dict):
                plane = bd.Plane(origin=tuple(x*offset for x in frame["normal"]),
                                 x_dir=frame["xDirection"], z_dir=frame["normal"])
            else:
                plane = {"xy": bd.Plane.XY, "yz": bd.Plane.YZ, "zx": bd.Plane.ZX}[frame].offset(offset)
            extent = feature["extent"]["value"]
            if operation in {"shaft", "groove"}:
                axis_x = feature["axisX"]["value"] if "axisX" in feature else 0.0
                if section.bounding_box().min.X < axis_x - 1e-7:
                    raise CandidateRejected("REVOLUTION_AXIS_SIDE: profile must lie on the positive horizontal side")
                tool = bd.revolve(plane * section,
                    axis=bd.Axis(plane.origin + plane.x_dir * axis_x, plane.y_dir), revolution_arc=extent)
            else:
                direction = 1 if feature["direction"] == "normal" else -1
                tool = bd.extrude(plane * section, amount=extent * direction, dir=plane.z_dir)
            before = steps[-1]["afterMm3"] if steps else 0
            additive = operation in {"pad", "shaft"}
            previous = body
            if not additive and previous is not None and len(roots) > 1:
                for root_index, profile in enumerate(roots, 1):
                    individual = profile & section
                    if not individual:
                        raise CandidateRejected(f"PROFILE_NO_EFFECT: separate cutting contour {root_index} is empty")
                    if operation == "groove":
                        individual_tool = bd.revolve(plane * individual,
                            axis=bd.Axis(plane.origin + plane.x_dir * axis_x, plane.y_dir), revolution_arc=extent)
                    else:
                        individual_tool = bd.extrude(plane * individual, amount=extent * direction, dir=plane.z_dir)
                    overlap = previous & individual_tool
                    overlap_volume = sum(_volume(solid.wrapped) for solid in overlap.solids()) if overlap else 0.0
                    if not math.isfinite(overlap_volume):
                        raise ValueError("Nonfinite profile overlap volume")
                    if overlap_volume <= max(1e-9, individual_tool.volume * 1e-12):
                        raise CandidateRejected(f"PROFILE_NO_EFFECT: separate cutting contour {root_index} removes no material")
            body = tool if body is None else (body + tool if additive else body - tool)
            diagnostic = inspect_finite(body)
            numerical_join = None
            # Single-body pads can inherit micron-scale trimmed-surface gaps.
            # Only disconnected additions get a bounded numerical join attempt.
            if additive and previous is not None and diagnostic["solidCount"]>1:
                joined = previous.fuse(tool,tol=1e-5)
                joined_diagnostic = inspect_finite(joined)
                if joined_diagnostic["valid"] and joined_diagnostic["solidCount"]==1:
                    body,diagnostic,numerical_join = joined,joined_diagnostic,1e-5
            if not diagnostic["valid"] or diagnostic["solidCount"] != 1:
                raise CandidateRejected("SINGLE_SOLID_REQUIRED: feature produced invalid or disconnected geometry")
            after = diagnostic["volumeMm3"]
            delta = after - before
            if not math.isfinite(delta):
                raise ValueError("Nonfinite geometry volume change")
            if abs(delta) <= max(1e-6, after * 1e-9):
                raise CandidateRejected("FEATURE_NO_EFFECT: no material change")
            if (delta > 0) != additive:
                raise CandidateRejected("WRONG_GEOMETRY_EFFECT")
            if feature.get("through") is True:
                # Check only material beneath this sketch, not the global bbox.
                # A plane through the middle needs coverage in both directions;
                # checking the cut length alone misses the uncut half.
                box = previous.bounding_box()
                normal, origin = plane.z_dir, plane.origin
                distances = [(bd.Vector(x, y, z) - origin).dot(normal)
                    for x in (box.min.X, box.max.X) for y in (box.min.Y, box.max.Y)
                    for z in (box.min.Z, box.max.Z)]
                reach = max(abs(value) for value in distances) + 1.0
                leftover = 0.0
                for distance in (reach, -reach):
                    prism = bd.extrude(plane * section, amount=distance, dir=normal)
                    remainder = body & prism
                    if remainder:
                        leftover += sum(_volume(solid.wrapped) for solid in remainder.solids())
                if not math.isfinite(leftover):
                    raise ValueError("Nonfinite through-cut remainder")
                if leftover > max(1e-6, before * 1e-9):
                    raise CandidateRejected(f"THROUGH_CUT_INCOMPLETE: {leftover:.6g} mm3 remains within the through profile; check start, direction and length")
            steps.append({"featureId": fid, "operation": operation, "beforeMm3": before,
                          "afterMm3": after, "deltaMm3": delta, "geometry": diagnostic})
            if numerical_join is not None:
                steps[-1]["numericalJoinToleranceMm"] = numerical_join
        except CandidateRejected as exc:
            raise CandidateRejected(f"Feature {fid}: {exc}") from exc
        except Exception as exc:
            raise ValueError(f"Feature {fid}: {exc}") from exc
    result = {"verdict": "PASS", "planId": plan["planId"], "steps": steps,
              "expected": inspect_finite(body),
              "limitations": ["Geometry of drawing-derived requirements only; drawing interpretation and CATIA history require separate verification."]}
    if "actual" in spec:
        with tempfile.TemporaryDirectory(prefix="feature-expected-") as temporary:
            reference = Path(temporary) / "expected.step"
            bd.export_step(body, str(reference))
            volume_tolerance = spec.get("volumeToleranceMm3")
            if volume_tolerance == "auto":
                # CATIA and OCCT boolean kernels differ by a small floor that
                # grows with the part; 0.01% stays far below any real edit.
                volume_tolerance = max(0.01, result["expected"]["volumeMm3"] * 1e-4)
            compare_spec = {"actual": spec["actual"], "reference": str(reference)}
            if "lengthToleranceMm" in spec:
                compare_spec["lengthToleranceMm"] = spec["lengthToleranceMm"]
            if volume_tolerance is not None:
                compare_spec["volumeToleranceMm3"] = volume_tolerance
            comparison = compare_steps(compare_spec)
            if "sources" in comparison:
                comparison["sources"]["reference"]["origin"] = "evaluated_requirements"
                comparison["sources"]["reference"].pop("path", None)
            result["comparison"] = comparison
            result["verdict"] = comparison["verdict"]
    return result
