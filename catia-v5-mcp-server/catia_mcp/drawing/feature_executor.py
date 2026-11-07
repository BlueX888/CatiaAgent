"""Composable CATIA executor. Caller must own the STA and automation lock.

Runtime semantics still require live probes; execution success is not delivery
acceptance. Independent geometry and editability checks remain mandatory.
"""
from __future__ import annotations

import math
import os
import json
from pathlib import Path
import tempfile
from uuid import uuid4
from itertools import chain,product

from catia_mcp.cad_support_client import run_support
from catia_mcp.drawing.builder import BuildError, BuildExecutor
from catia_mcp.drawing.feature_model import (
    EDGE_OPERATIONS, PLAN_SCHEMA, SCHEMA, compile_features, arc_geometry)
from catia_mcp.tools.measurement import read_measurable_array
from catia_mcp.topology import search_topology


def _sketch_anchor(feature):
    """Keep signed-position reference axes well outside the nominal sketch."""
    values = []
    def collect(item):
        if isinstance(item, dict):
            if "formula" in item and type(item.get("value")) in (int, float):
                values.append(abs(item["value"]))
            else:
                for child in item.values():
                    collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)
    collect(feature["sketch"]["profiles"])
    collect(feature.get("axisX"))
    return max(1000.0, 8.0 * max(values, default=0.0) + 1.0)


def _reference_choices(groups):
    """Bound alias retries; try both common native topology-cell variants early."""
    base = tuple(0 for _ in groups)
    second = tuple(min(1,len(group)-1) for group in groups)
    preferred = [base,second]
    for seed in (base,second):
        for i,group in enumerate(groups):
            if len(group)>1:
                choice=list(seed);choice[i]=1-choice[i];preferred.append(tuple(choice))
    seen=set()
    for choice in chain(preferred,product(*(range(len(group)) for group in groups))):
        if choice in seen:continue
        seen.add(choice);yield choice
        if len(seen)==16:return


def material_effect(operation, before, after):
    if not all(math.isfinite(v) for v in (before, after)) or before < 0 or after <= 0:
        raise BuildError("MEASUREMENT_FAILED: invalid body volume", stage=operation)
    delta = after - before
    epsilon = max(1e-6, max(before, after) * 1e-9)
    if abs(delta) <= epsilon:
        raise BuildError("FEATURE_NO_EFFECT: material did not change", stage=operation,
                         scene={"beforeMm3": before, "afterMm3": after})
    # A fillet adds material on a concave edge and removes it on a convex one;
    # the sign is a property of the edge, not of the operation.
    if operation not in EDGE_OPERATIONS and (delta > 0) != (operation in {"pad", "shaft"}):
        raise BuildError("WRONG_GEOMETRY_EFFECT: material changed in the wrong direction", stage=operation,
                         scene={"beforeMm3": before, "afterMm3": after})
    return {"beforeMm3": before, "afterMm3": after, "deltaMm3": delta}


def validate_compiled_plan(plan):
    """Recompile formulas instead of trusting caller-supplied numeric results."""
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise BuildError("Expected a feature plan", stage="plan")
    def expr(dimension):
        # Literal coordinates carry no parameter; the number is the value the
        # compiler accepts back (a "12.5mm" formula string is not an expression).
        if dimension["parameters"]:
            return dimension["formula"]
        return dimension["value"] if dimension["value"] else 0
    def profile_requirement(profile):
        if isinstance(profile, dict):
            if "formula" in profile:
                return expr(profile)
            return {k: profile_requirement(v) for k, v in profile.items()}
        if isinstance(profile, list):
            return [profile_requirement(v) for v in profile]
        return profile
    try:
        def selector_requirement(selector):
            if isinstance(selector,list):
                return [selector_requirement(item) for item in selector]
            if selector["type"] == "linear":
                result = {"type": "linear", "direction": selector["direction"],
                        "through": [expr(point) for point in selector["through"]]}
                if "withinSegment" in selector:
                    result["withinSegment"] = selector["withinSegment"]
                return result
            return {"type": "circular", "radius": expr(selector["radius"]),
                    "centre": [expr(point) for point in selector["centre"]],
                    "axis": selector["axis"]}
        req = {"schema": SCHEMA, "partName": plan["partName"], "unit": "mm",
               "parameters": plan["parameters"], "features": []}
        for feature in plan["features"]:
            if feature["operation"] in EDGE_OPERATIONS:
                entry = {"id": feature["id"], "operation": feature["operation"],
                         "dependsOn": feature["dependsOn"],
                         "edges": selector_requirement(feature["edges"])}
                entry["radius" if feature["operation"] == "fillet" else "length"] = expr(feature["size"])
                if feature["operation"] == "chamfer":
                    entry["angle"] = feature["angle"]
                req["features"].append(entry)
                continue
            sketch = feature["sketch"]
            req["features"].append({
                "id": feature["id"], "operation": feature["operation"],
                "dependsOn": feature["dependsOn"], "direction": feature["direction"],
                "angle" if feature["operation"] in {"shaft", "groove"} else "length": expr(feature["extent"]),
                "sketch": {"plane": sketch["plane"], "offset": expr(sketch["offset"]),
                           "profiles": [profile_requirement(profile) for profile in sketch["profiles"]]}})
            if "axisX" in feature:
                req["features"][-1]["axisX"] = expr(feature["axisX"])
            if "through" in feature:
                req["features"][-1]["through"] = feature["through"]
        checked = compile_features(req, None, require_evidence=False, _recompile_plan=True)
        # Formula formatting is canonicalized again; compare all evaluated
        # dimensions and parameter dependencies, not only a caller's hash.
        def values(value):
            if isinstance(value, dict):
                return {k: values(v) for k, v in value.items() if k != "formula"}
            if isinstance(value, list):
                return [values(v) for v in value]
            return value
        if values(checked["features"]) != values(plan["features"]):
            raise ValueError("Compiled dimensions or dependencies do not match formulas")
        return checked
    except (KeyError, TypeError, ValueError) as exc:
        raise BuildError(f"Invalid feature plan: {exc}", stage="plan") from exc


class FeatureExecutor(BuildExecutor):
    def _dimension(self, target, dimension, name):
        self._bind(target, dimension["formula"], name)
        self.constraint_bindings.append({"target": str(target.Name), "formula": name,
                                         "formulaBody": dimension["formula"],
                                         "parameters": dimension["parameters"]})

    def _sketch(self, feature):
        self._revolution_axis = None
        fid, spec = feature["id"], feature["sketch"]
        reference = self.part.CreateReferenceFromObject
        frame = spec["plane"]
        if isinstance(frame, dict):
            plane = self.part.HybridShapeFactory.AddNewPlaneEquation(*frame["normal"], spec["offset"]["value"])
            dimension = plane.D
        else:
            origin = getattr(self.part.OriginElements, {"xy": "PlaneXY", "yz": "PlaneYZ", "zx": "PlaneZX"}[frame])
            plane = self.part.HybridShapeFactory.AddNewPlaneOffset(reference(origin), spec["offset"]["value"], False)
            dimension = plane.Offset
        plane.Name = f"{fid}_Support"
        self.body.InsertHybridShape(plane)
        self._dimension(dimension, spec["offset"], f"{fid}_Offset")
        self.part.UpdateObject(plane)
        sketch = self.body.Sketches.Add(reference(plane))
        sketch.Name = f"{fid}_Sketch"
        if isinstance(frame, dict):
            n,h = frame["normal"],frame["xDirection"]
            v = [n[1]*h[2]-n[2]*h[1],n[2]*h[0]-n[0]*h[2],n[0]*h[1]-n[1]*h[0]]
            sketch.SetAbsoluteAxisData(tuple(x*spec["offset"]["value"] for x in n)+tuple(h)+tuple(v))
        factory = sketch.OpenEdition()
        constraints = sketch.Constraints
        # Remote fixed axes make positional distances positive even for signed
        # coordinates. Circle2D.CenterPoint is unavailable on the target CATIA.
        # A fixed 2,000,001 mm reference caused small formula edits to be
        # suppressed by CATIA's sketch solver; use the nominal sketch scale.
        anchor = _sketch_anchor(feature)
        vertical = factory.CreateLine(-anchor, -anchor, -anchor, anchor)
        horizontal = factory.CreateLine(-anchor, -anchor, anchor, -anchor)
        for axis in (vertical, horizontal):
            axis.Construction = True
            constraints.AddMonoEltCst(0, reference(axis))
        try:
            for i, profile in enumerate(spec["profiles"]):
                prefix = f"{fid}_P{i}"
                def bind(constraint, dimension, suffix):
                    self._dimension(constraint.Dimension, dimension, prefix + suffix)
                def position(geometry, axis, dimension, suffix, radius=None):
                    formula = f"({dimension['formula']}) + {anchor}mm"
                    if radius:
                        formula += f" - ({radius['formula']})"
                    bind(constraints.AddBiEltCst(1, reference(geometry), reference(axis)),
                         {**dimension, "formula": formula,
                          "parameters": sorted(set(dimension["parameters"]) | set(radius["parameters"] if radius else []))}, suffix)
                if profile["type"] == "wire":
                    point = profile["start"]
                    geometries = []
                    for j, segment in enumerate(profile["segments"]):
                        start = tuple(point[k]["value"] for k in ("x", "y"))
                        end = tuple(segment["end"][k]["value"] for k in ("x", "y"))
                        if segment["type"] == "arc":
                            radius = segment["radius"]["value"]
                            center, first, sweep = arc_geometry(start, end, radius, segment["clockwise"], segment["largeArc"])
                            # CATIA only creates positive-parametric arcs. Map
                            # their endpoints back to the ordered wire for CW.
                            low, high = (first, first + sweep) if sweep > 0 else (first + sweep, first)
                            created = factory.CreateCircle(*center, radius, low, high)
                            bind(constraints.AddMonoEltCst(14, reference(created)), segment["radius"], f"_E{j}_Radius")
                            first_point, last_point = (created.StartPoint, created.EndPoint) if sweep > 0 else (created.EndPoint, created.StartPoint)
                        else:
                            created = factory.CreateLine(*start, *end)
                            first_point, last_point = created.StartPoint, created.EndPoint
                        position(first_point, vertical, point["x"], f"_E{j}_X")
                        position(first_point, horizontal, point["y"], f"_E{j}_Y")
                        geometries.append((first_point, last_point))
                        point = segment["end"]
                    for j, (_, last_point) in enumerate(geometries):
                        constraints.AddBiEltCst(2, reference(last_point), reference(geometries[(j + 1) % len(geometries)][0]))
                elif profile["type"] == "polygon":
                    points = profile["points"]
                    coords = [(p["x"]["value"], p["y"]["value"]) for p in points]
                    lines = [factory.CreateLine(*point, *coords[(j + 1) % len(coords)]) for j, point in enumerate(coords)]
                    for j, line in enumerate(lines):
                        constraints.AddBiEltCst(2, reference(line.EndPoint), reference(lines[(j + 1) % len(lines)].StartPoint))
                        position(line.StartPoint, vertical, points[j]["x"], f"_V{j}_X")
                        position(line.StartPoint, horizontal, points[j]["y"], f"_V{j}_Y")
                elif profile["type"] == "rectangle":
                    x, y = profile["x"]["value"], profile["y"]["value"]
                    w, h = profile["width"]["value"], profile["height"]["value"]
                    corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
                    lines = [factory.CreateLine(*corners[j], *corners[(j + 1) % 4]) for j in range(4)]
                    for j, line in enumerate(lines):
                        constraints.AddMonoEltCst(10 if j % 2 == 0 else 13, reference(line))
                        constraints.AddBiEltCst(2, reference(line.EndPoint), reference(lines[(j + 1) % 4].StartPoint))
                    for line, key in [(lines[0], "width"), (lines[1], "height")]:
                        bind(constraints.AddBiEltCst(1, reference(line.StartPoint), reference(line.EndPoint)), profile[key], "_" + key)
                    position(lines[3], vertical, profile["x"], "_X")
                    position(lines[0], horizontal, profile["y"], "_Y")
                else:
                    x, y = profile["x"]["value"], profile["y"]["value"]
                    circle = factory.CreateClosedCircle(x, y, profile["radius"]["value"])
                    bind(constraints.AddMonoEltCst(14, reference(circle)), profile["radius"], "_Radius")
                    position(circle, vertical, profile["x"], "_X", profile["radius"])
                    position(circle, horizontal, profile["y"], "_Y", profile["radius"])
            if "axisX" in feature:
                x = feature["axisX"]["value"]
                axis = factory.CreateLine(x, -anchor, x, anchor)
                axis.Construction = True
                sketch.CenterLine = axis
                constraints.AddMonoEltCst(13, reference(axis))
                position(axis, vertical, feature["axisX"], "_AxisX")
                self._revolution_axis = reference(axis)
        finally:
            sketch.CloseEdition()
        self.part.UpdateObject(sketch)
        return sketch

    @staticmethod
    def _unit(vector):
        length = math.sqrt(sum(value * value for value in vector))
        if not length:
            return [0.0, 0.0, 0.0]
        return [value / length for value in vector]

    @staticmethod
    def _matches_current_edge(candidate, current, tolerance=1e-3):
        for edge in (candidate, current):
            vector = edge["direction" if edge["kind"] == "linear" else "axis"]
            values = [edge["length"], *edge["midpoint"], *vector]
            if edge["kind"] == "circular":
                values += [edge["radius"], *edge["centre"], *edge["start"], *edge["end"]]
            if (any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
                    or edge["length"] <= 0 or sum(v*v for v in vector) < 1e-18):
                return False
        if candidate["kind"] != current["kind"] or abs(candidate["length"] - current["length"]) > tolerance:
            return False
        if candidate["kind"] == "linear":
            a, b = FeatureExecutor._unit(candidate["direction"]), FeatureExecutor._unit(current["direction"])
            return (math.dist(candidate["midpoint"], current["midpoint"]) <= tolerance
                    and abs(sum(x*y for x,y in zip(a,b))) >= 1-1e-6)
        a, b = FeatureExecutor._unit(candidate["axis"]), FeatureExecutor._unit(current["axis"])
        if (abs(candidate["radius"] - current["radius"]) > tolerance
                or math.dist(candidate["centre"], current["centre"]) > tolerance
                or abs(sum(x*y for x,y in zip(a,b))) < 1-1e-6):
            return False
        if abs(candidate["length"] - 2*math.pi*candidate["radius"]) <= tolerance:
            return True
        return (math.dist(candidate["midpoint"], current["midpoint"]) <= tolerance
                and min(max(math.dist(candidate[a],current[b]) for a,b in pairs)
                        for pairs in ((('start','start'),('end','end')),
                                      (('start','end'),('end','start')))) <= tolerance)

    def _current_edges(self):
        root = getattr(self, "diagnostic_dir", None)
        if root is None:
            root = Path(os.environ.get("CATIA_MCP_WORK_DIR", tempfile.gettempdir())) / "native-edge-evidence"
        root = Path(root); root.mkdir(parents=True, exist_ok=True)
        step = root / f"current-{uuid4().hex}.step"
        self.doc.ExportData(str(step), "stp")
        result = run_support("edges", {"sourceStep": str(step)})
        report = step.with_suffix(".json")
        with report.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
        data = result.get("data", {})
        if (not result.get("ok") or data.get("valid") is not True
                or not isinstance(data.get("edges"), list) or not 1 <= len(data["edges"]) <= 8192):
            raise BuildError("CURRENT_EDGE_INVENTORY_FAILED: inspect the retained current-solid evidence",
                             stage="edge_selection", scene={"evidence": str(report)})
        evidence = {"step": str(step), "report": str(report), "sha256": data["sourceSha256"]}
        self.edge_snapshots = getattr(self, "edge_snapshots", []) + [evidence]
        return data["edges"]

    def _edge_candidates(self):
        """Enumerate measurable edges of the current solid.

        Circle centres and directions come from Measurable.GetCenter /
        GetDirection; linear edges expose direction and three curve points.
        Sketch construction geometry shows up in the edge search with a huge
        length, so it is rejected by a bounding-box multiple.
        """
        selection = self.doc.Selection
        spa = self.doc.GetWorkbench("SPAWorkbench")
        last = self.body
        box = self._bbox()
        diagonal = math.dist([box["min"][axis] for axis in "xyz"],
                             [box["max"][axis] for axis in "xyz"])
        limit = max(10.0 * diagonal, 1e4)
        candidates = []
        current_edges = self._current_edges()
        try:
            found = search_topology(selection, last, "Edge")
            for index in range(1, found.count + 1):
                reference = selection.Item(index).Reference
                measurable = spa.GetMeasurable(reference)
                try:
                    length = float(measurable.Length)
                except Exception:
                    continue
                if not math.isfinite(length) or length <= 0 or length > limit:
                    continue
                entry = {"index": index, "reference": reference, "length": length}
                try:
                    entry["radius"] = float(measurable.Radius)
                    entry["centre"] = read_measurable_array(self.conn.app, measurable, "GetCenter", 3)
                    entry["axis"] = read_measurable_array(self.conn.app, measurable, "GetDirection", 3)
                    points = read_measurable_array(self.conn.app, measurable, "GetPointsOnCurve", 9)
                    entry.update(start=points[:3], midpoint=points[3:6], end=points[6:9])
                    entry["kind"] = "circular"
                except Exception:
                    try:
                        entry["direction"] = read_measurable_array(
                            self.conn.app, measurable, "GetDirection", 3)
                        points = read_measurable_array(
                            self.conn.app, measurable, "GetPointsOnCurve", 9)
                        entry["midpoint"] = points[3:6]
                        entry.update(start=points[:3], end=points[6:9])
                        entry["kind"] = "linear"
                    except Exception:
                        continue
                if any(self._matches_current_edge(entry, current) for current in current_edges):
                    candidates.append(entry)
        finally:
            try:
                selection.Clear()
            except Exception:
                pass
        return candidates

    @staticmethod
    def _edge_groups(candidates):
        """Group coincident edge references that share one geometry.

        A pocket cut flush with a face makes CATIA expose the same circular rim
        as two topology cells. The selector describes geometry, so they are one
        match; the group keeps every reference because one of them may be the
        cell the finishing API accepts.
        """
        groups = {}
        for candidate in candidates:
            if candidate["kind"] == "circular":
                axis = list(candidate["axis"])
                for value in axis:
                    if abs(value) > 1e-9:
                        if value < 0:
                            axis = [-item for item in axis]
                        break
                # Only a closed circle can be a coincident duplicate of itself;
                # two arcs of the same circle are distinct edges and must not
                # collapse into one finishing target.
                closed = (abs(candidate["length"] - 2 * math.pi * candidate["radius"])
                          <= 1e-3)
                key = ("circular", round(candidate["radius"], 6),
                       *(round(value, 6) for value in candidate["centre"]),
                       *(round(value, 6) for value in axis),
                       "closed" if closed else f"arc-{candidate['index']}")
            else:
                direction = FeatureExecutor._unit(candidate["direction"])
                if next(value for value in direction if abs(value)>1e-9) < 0:
                    direction = [-value for value in direction]
                key = ("linear", round(candidate["length"], 6),
                       *(round(value, 6) for value in candidate["midpoint"]),
                       *(round(value, 6) for value in direction))
            groups.setdefault(key, []).append(candidate)
        return list(groups.values())

    def _resolve_edge(self, selector, stage, candidates=None):
        """Return the references of the single edge matching a selector.

        The result is a list of coincident references for the same geometry;
        the caller tries them in order because CATIA does not accept every
        topology cell for a finishing feature.
        """
        tolerance = 1e-3
        matches = []
        groups = self._edge_groups(self._edge_candidates() if candidates is None else candidates)
        candidates = [group[0] for group in groups]
        for group_index, candidate in enumerate(candidates):
            if selector["type"] == "circular":
                if candidate["kind"] != "circular":
                    continue
                if abs(candidate["radius"] - selector["radius"]["value"]) > tolerance:
                    continue
                wanted = [point["value"] for point in selector["centre"]]
                if math.dist(candidate["centre"], wanted) > tolerance:
                    continue
                actual, expected = self._unit(candidate["axis"]), self._unit(selector["axis"])
                if abs(sum(a * b for a, b in zip(actual, expected))) < 1 - 1e-6:
                    continue
            else:
                if candidate["kind"] != "linear":
                    continue
                actual, expected = self._unit(candidate["direction"]), self._unit(selector["direction"])
                if abs(sum(a * b for a, b in zip(actual, expected))) < 1 - 1e-6:
                    continue
                through = [point["value"] for point in selector["through"]]
                offset = [candidate["midpoint"][i] - through[i] for i in range(3)]
                cross = [offset[1] * expected[2] - offset[2] * expected[1],
                         offset[2] * expected[0] - offset[0] * expected[2],
                         offset[0] * expected[1] - offset[1] * expected[0]]
                if math.sqrt(sum(value * value for value in cross)) > tolerance:
                    continue
                if selector.get("withinSegment") and abs(sum(a * b for a, b in zip(offset, actual))) > candidate["length"] / 2 + tolerance:
                    continue
            matches.append(groups[group_index])
        if len(matches) != 1:
            signatures = [
                {key: item[key] for key in ("index", "kind", "length", "radius", "centre", "midpoint")
                 if key in item}
                for group in groups for item in group
            ]
            raise BuildError(
                f"EDGE_SELECTION_MISMATCH: the selector matched {len(matches)} edges; "
                "exactly one is required.",
                stage=stage, scene={"matchCount": len(matches), "candidates": signatures})
        return matches[0]

    def execute(self, plan):
        checked = validate_compiled_plan(plan)
        self.conn.connect()
        self.doc = self.conn.documents.Add("Part")
        self.doc.Activate()
        self.part, self.body = self.doc.Part, self.doc.Part.MainBody
        stage = "parameters"
        try:
            try:
                self.doc.Product.PartNumber = checked["partName"]
            except Exception:
                # CATIA rejects a PartNumber already used by an open document
                # (cancelled runs can leave documents open); keep the preferred
                # name when free and fall back to a process-unique suffix.
                self.doc.Product.PartNumber = f"{checked['partName']}_{os.getpid()}"
            for p in checked["parameters"]:
                parameter = self.part.Parameters.CreateDimension(p["name"], p["type"].upper(), 0)
                parameter.ValuateFromString(f"{p['value']}{'mm' if p['type'] == 'length' else 'deg'}")
            before = 0.0
            for feature in checked["features"]:
                stage = feature["id"]
                self.part.InWorkObject = self.body
                op = feature["operation"]
                factory = self.part.ShapeFactory
                if op in EDGE_OPERATIONS:
                    selectors = feature["edges"] if isinstance(feature["edges"],list) else [feature["edges"]]
                    def resolve_groups():
                        candidates = self._edge_candidates()
                        groups = [self._resolve_edge(selector,stage,candidates) for selector in selectors]
                        seen = set()
                        for group in groups:
                            indices = {c["index"] for c in group}
                            if seen.intersection(indices):
                                raise BuildError("EDGE_SELECTION_MISMATCH: group selectors resolve to the same edge",stage=stage)
                            seen.update(indices)
                        return groups
                    groups = resolve_groups()
                    created, match, dimension, detail = None, None, None, None
                    last_error = None
                    for attempt,choice in enumerate(_reference_choices(groups)):
                        if attempt:
                            groups = resolve_groups()
                        if any(index>=len(group) for index,group in zip(choice,groups)):
                            continue
                        selected = tuple(group[index] for index,group in zip(choice,groups))
                        candidate = selected[0]
                        feature_object = None
                        shape_count = int(self.body.Shapes.Count)
                        edge_number = 1
                        try:
                            if op == "fillet":
                                feature_object = factory.AddNewSolidEdgeFilletWithConstantRadius(
                                    # Match OCCT's tangent propagation; minimal
                                    # propagation produces different corner material.
                                    candidate["reference"], 1, feature["size"]["value"])
                                for edge_number,additional in enumerate(selected[1:],2):
                                    feature_object.AddObjectToFillet(additional["reference"])
                                dimension = feature_object.Radius
                                detail = {"radiusMm": feature["size"]["value"],"edgeCount":len(selected),"propagation":"tangency"}
                            else:
                                feature_object = factory.AddNewChamfer(
                                    candidate["reference"], 1, 1, 0,
                                    feature["size"]["value"], feature["angle"])
                                dimension = feature_object.Length1
                                detail = {"lengthMm": feature["size"]["value"],
                                          "angleDeg": feature["angle"]}
                            feature_object.Name = stage
                            self.part.Update()
                        except Exception as exc:
                            last_error = f"edge {edge_number}/{len(selected)}: {exc}"
                            if feature_object is not None:
                                try:
                                    selection = self.doc.Selection
                                    selection.Clear()
                                    selection.Add(feature_object)
                                    selection.Delete()
                                    selection.Clear()
                                except Exception as rollback_error:
                                    raise BuildError(f"ROLLBACK_FAILED: cannot remove rejected finishing feature: {rollback_error}",stage=stage) from rollback_error
                            self.part.Update()
                            if (int(self.body.Shapes.Count)!=shape_count
                                    or abs(self._volume()-before)>max(1e-5,abs(before)*1e-8)):
                                raise BuildError("ROLLBACK_FAILED: finishing failure changed the body state",stage=stage)
                            continue
                        created, match = feature_object, candidate
                        self._dimension(dimension, feature["size"], f"{stage}_Size")
                        self.part.Update()
                        break
                    if created is None:
                        raise BuildError(
                            f"EDGE_OPERATION_FAILED: no coincident reference produced a valid "
                            f"{op}: {last_error}", stage=stage)
                    after = self._volume()
                    effects = material_effect(op, before, after)
                    self._step(op, {"featureId": stage, "objectName": str(created.Name),
                                    "edge": detail,
                                    "edgeMatch": {"kind": match["kind"], "length": match["length"],
                                                  "radius": match.get("radius"),
                                                  "centre": match.get("centre"),
                                                  "midpoint": match.get("midpoint"),
                                                  "direction": match.get("direction")},
                                    **({"edgeMatches":[{k:v for k,v in c.items() if k!="reference"} for c in selected]} if len(selected)>1 else {}),
                                    "evidence": feature["evidence"], "effects": effects,
                                    "bbox": self._bbox()})
                    before = after
                    continue
                sketch = self._sketch(feature)
                amount = feature["extent"]
                if op in {"pad", "pocket"}:
                    created = (factory.AddNewPad if op == "pad" else factory.AddNewPocket)(sketch, amount["value"])
                    created.DirectionOrientation = 0 if feature["direction"] == "normal" else 1
                    dimension = created.FirstLimit.Dimension
                else:
                    created = (factory.AddNewShaft if op == "shaft" else factory.AddNewGroove)(sketch)
                    created.RevoluteAxis = (self._revolution_axis if self._revolution_axis is not None else
                        self.part.CreateReferenceFromObject(sketch.AbsoluteAxis.VerticalReference))
                    dimension = created.FirstAngle
                created.Name = stage
                self._dimension(dimension, amount, f"{stage}_Extent")
                self.part.Update()
                after = self._volume()
                effects = material_effect(op, before, after)
                self._step(op, {"featureId": stage, "objectName": str(created.Name),
                                "sketchName": str(sketch.Name), "evidence": feature["evidence"],
                                "effects": effects, "bbox": self._bbox()})
                before = after
            return {"steps": self.steps, "bindings": self.constraint_bindings,
                    "edgeSnapshots": getattr(self, "edge_snapshots", []),
                    "final": {"volumeMm3": before, "bbox": self._bbox()},
                    "acceptance": "pending_independent_verification"}
        except Exception as exc:
            raise BuildError(str(exc), stage=stage,
                             scene={"steps": self.steps, "failure": getattr(exc, "scene", {})}) from exc
