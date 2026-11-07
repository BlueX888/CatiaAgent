"""Optional OCCT tools for dataset preparation and independent verification.

Run with the isolated requirements-cad-support environment, never inside the
CATIA COM server. This module reads geometry, not model code. It uses cadgen's
public drawing/diagnostic APIs; reference-model operations are not MCP tools.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import sys
import tempfile
from typing import Any

from catia_mcp.cad_errors import MaterialComparisonInconclusive


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fields(value, allowed, required=(), label="spec"):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    if set(value) - set(allowed) or set(required) - set(value):
        raise ValueError(f"{label} has unknown or missing fields.")


def _positive(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive.")
    return value


def read_shape(path: str):
    import build123d as bd

    source = Path(path).resolve(strict=True)
    if source.suffix.lower() not in {".step", ".stp"}:
        raise ValueError("Only STEP/STP geometry is accepted; model scripts are never executed.")
    shape = bd.import_step(str(source))
    if not shape.solids():
        raise ValueError("STEP contains no solids.")
    return source, shape


def _volume(shape):
    """Use one adaptive integration rule for inputs and boolean results."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties, 1e-12)
    return float(properties.Mass())


def inspect_shape(shape) -> dict[str, Any]:
    from cadgen.geometry import is_sound, topology_errors

    solids = list(shape.solids())
    volumes = [_volume(s.wrapped) for s in solids]
    import build123d as bd
    box = bd.Compound(children=solids).bounding_box() if solids else shape.bounding_box()
    errors = [{"code": str(e.code), "entityCount": len(e.entities)}
              for e in topology_errors(shape)]
    sound = bool(is_sound(shape))
    valid = bool(shape.is_valid) and sound and not errors and bool(solids)
    valid = valid and all(math.isfinite(v) and v > 0 for v in volumes)
    return {
        "valid": valid, "solidCount": len(solids), "volumeMm3": sum(volumes),
        "solidVolumesMm3": volumes, "topologyErrors": errors, "sound": sound,
        "bbox": {"min": list(box.min), "max": list(box.max)},
    }


def _complete_circles(edges):
    """Join STEP seam-split arcs only when their angular union covers a circle."""
    groups = {}
    for edge in edges:
        if edge["kind"] != "circular":
            continue
        axis = list(edge["axis"])
        length = math.sqrt(sum(x*x for x in axis)); axis = [x/length for x in axis]
        if next(x for x in axis if abs(x)>1e-9) < 0:
            axis = [-x for x in axis]
        key = (round(edge["radius"],6), *(round(x,6) for x in edge["centre"]), *(round(x,6) for x in axis))
        groups.setdefault(key, []).append((edge,axis))
    complete = []
    for group in groups.values():
        first,axis = group[0]; radius = first["radius"]
        if len(group)<2 or any(abs(e["length"]-2*math.pi*radius)<1e-7 for e,_ in group):
            continue
        basis = [0.,0.,0.]; basis[min(range(3),key=lambda i:abs(axis[i]))] = 1.
        dot = sum(a*b for a,b in zip(axis,basis)); horizontal = [b-dot*a for a,b in zip(axis,basis)]
        length = math.sqrt(sum(x*x for x in horizontal)); horizontal = [x/length for x in horizontal]
        vertical = [axis[1]*horizontal[2]-axis[2]*horizontal[1],axis[2]*horizontal[0]-axis[0]*horizontal[2],axis[0]*horizontal[1]-axis[1]*horizontal[0]]
        def angle(point):
            delta = [p-c for p,c in zip(point,first["centre"])]
            return math.atan2(sum(a*b for a,b in zip(delta,vertical)),sum(a*b for a,b in zip(delta,horizontal))) % math.tau
        intervals = []; valid = True
        for edge,_ in group:
            start,mid,end = [angle(edge[k]) for k in ('start','midpoint','end')]
            sweep = edge["length"]/radius
            if abs((end-start)%math.tau-sweep)<1e-7 and abs((mid-start)%math.tau-sweep/2)<1e-7:
                low = start
            elif abs((start-end)%math.tau-sweep)<1e-7 and abs((mid-end)%math.tau-sweep/2)<1e-7:
                low = end
            else:
                valid = False; break
            high = low+sweep
            intervals.extend([(low,min(high,math.tau))]+([(0,high-math.tau)] if high>math.tau else []))
        cursor = 0.
        for low,high in sorted(intervals):
            if low>cursor+1e-8:
                break
            cursor = max(cursor,high)
        if valid and cursor>=math.tau-1e-8:
            complete.append({**first,"length":math.tau*radius,"axis":axis,"mergedCurrentArcCount":len(group)})
    return complete


def inspect_edges(spec):
    """Inventory the current exported solid, excluding CATIA feature history."""
    import build123d as bd
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    _fields(spec, {"sourceStep"}, {"sourceStep"})
    path, shape = read_shape(spec["sourceStep"])
    diagnostic = inspect_shape(shape)
    if not diagnostic["valid"] or diagnostic["solidCount"] != 1:
        raise ValueError("Current-edge inventory requires one sound solid")
    edges = []
    for edge in shape.solids()[0].edges():
        if edge.geom_type not in {bd.GeomType.LINE, bd.GeomType.CIRCLE}:
            continue
        row = {"kind": "linear" if edge.geom_type == bd.GeomType.LINE else "circular",
               "length": float(edge.length), "midpoint": list(edge.position_at(.5)),
               "start": list(edge.position_at(0)), "end": list(edge.position_at(1))}
        if row["kind"] == "linear":
            row["direction"] = list(edge.tangent_at(.5))
        else:
            circle = BRepAdaptor_Curve(edge.wrapped).Circle()
            row.update(radius=float(circle.Radius()), centre=list(circle.Location().Coord()),
                       axis=list(circle.Axis().Direction().Coord()))
        edges.append(row)
    edges.extend(_complete_circles(edges))
    if not 1 <= len(edges) <= 8192:
        raise ValueError("Current-edge inventory requires 1-8192 supported edges")
    return {"valid": True, "sourceSha256": file_digest(path), "edges": edges}


def _boolean_volume(operation, left, right, fuzzy):
    """Measure one non-destructive OCCT boolean without repairing its volume."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.collections import List_TopoDS_Shape

    algo = {"common": BRepAlgoAPI_Common, "cut": BRepAlgoAPI_Cut, "union": BRepAlgoAPI_Fuse}[operation]()
    arguments, tools = List_TopoDS_Shape(), List_TopoDS_Shape()
    arguments.Append(left.wrapped)
    tools.Append(right.wrapped)
    algo.SetArguments(arguments)
    algo.SetTools(tools)
    algo.SetNonDestructive(True)
    algo.SetFuzzyValue(fuzzy)
    algo.Build()
    if not algo.IsDone() or algo.Shape().IsNull() or not BRepCheck_Analyzer(algo.Shape()).IsValid():
        raise ValueError(f"{operation} boolean failed or returned invalid geometry")
    volume = _volume(algo.Shape())
    if not math.isfinite(volume) or volume < 0:
        raise ValueError(f"{operation} boolean returned invalid volume")
    return volume


def _shared_interior(actual, reference):
    from itertools import product
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_IN
    from OCP.gp import gp_Pnt

    a, b = actual.bounding_box(), reference.bounding_box()
    bounds = [(max(x, y), min(u, v)) for x, y, u, v in zip(a.min, b.min, a.max, b.max)]
    if any(high <= low for low, high in bounds):
        return False
    classifiers = [BRepClass3d_SolidClassifier(shape.wrapped) for shape in (actual, reference)]
    for fractions in product((0.25, 0.5, 0.75), repeat=3):
        point = gp_Pnt(*(low + (high - low) * t for (low, high), t in zip(bounds, fractions)))
        for classifier in classifiers:
            classifier.Perform(point, 1e-7)
        if all(classifier.State() == TopAbs_IN for classifier in classifiers):
            return True
    # A coarse grid misses thin walls around a large opening. Surface-adjacent
    # points are only witnesses if both solid classifiers strictly agree IN.
    for shape in (actual, reference):
        for face in shape.faces():
            for u, v in ((.5, .5), (.25, .5), (.75, .5)):
                try:
                    surface = face.position_at(u, v)
                    normal = face.normal_at(u, v)
                except (ValueError, RuntimeError):
                    continue
                for distance in (.001, .0001, .01):
                    point = gp_Pnt(*(surface - normal * distance))
                    for classifier in classifiers:
                        classifier.Perform(point, 1e-7)
                    if all(classifier.State() == TopAbs_IN for classifier in classifiers):
                        return True
    return False


def _difference_witnesses(actual, reference):
    """Find strict material-only points, without trusting a boolean result."""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_IN, TopAbs_OUT
    from OCP.gp import gp_Pnt
    classifiers = [BRepClass3d_SolidClassifier(s.wrapped) for s in (actual,reference)]
    witnesses = [None,None]
    for shape in (actual,reference):
        for face in shape.faces():
            for u,v in ((.5,.5),(.25,.5),(.75,.5)):
                try:
                    surface,normal = face.position_at(u,v),face.normal_at(u,v)
                except (ValueError,RuntimeError):
                    continue
                for distance in (.001,-.001,.01,-.01,.0001,-.0001):
                    point = surface+normal*distance
                    for classifier in classifiers:
                        classifier.Perform(gp_Pnt(*point),1e-7)
                    states = [c.State() for c in classifiers]
                    if states==[TopAbs_IN,TopAbs_OUT]:witnesses[0]=list(point)
                    if states==[TopAbs_OUT,TopAbs_IN]:witnesses[1]=list(point)
                    if all(w is not None for w in witnesses):return witnesses
    return witnesses


def _shared_ball_volume(actual, reference):
    """A strict shared point and its boundary distances bound common material."""
    from itertools import product
    import build123d as bd
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.TopAbs import TopAbs_IN, TopAbs_VERTEX, TopAbs_EDGE, TopAbs_FACE
    from OCP.gp import gp_Pnt

    shapes=(actual,reference)
    a,b=[shape.bounding_box() for shape in shapes]
    bounds=[(max(x,y),min(u,v)) for x,y,u,v in zip(a.min,b.min,a.max,b.max)]
    if any(high<=low for low,high in bounds):return 0.0
    classifiers=[BRepClass3d_SolidClassifier(shape.wrapped) for shape in shapes]
    for fractions in product((.25,.5,.75),repeat=3):
        point=gp_Pnt(*(low+(high-low)*t for (low,high),t in zip(bounds,fractions)))
        for classifier in classifiers:classifier.Perform(point,1e-7)
        if not all(c.State()==TopAbs_IN for c in classifiers):continue
        distances=[]
        for shape in shapes:
            boundary=bd.Compound(children=list(shape.faces()))
            distance=BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(point).Vertex(),boundary.wrapped)
            distance.Perform()
            if not distance.IsDone():raise ValueError('Shared-ball boundary distance is inconclusive')
            distances.append(float(distance.Value()))
        tolerance=max(BRep_Tool.MaxTolerance_s(s.wrapped,kind) for s in shapes
                      for kind in (TopAbs_VERTEX,TopAbs_EDGE,TopAbs_FACE))
        radius=max(0.0,min(*distances,1.0)-max(1e-4,2*tolerance))
        return 4*math.pi*radius**3/3
    return 0.0


def _union_overlap_volume(actual, reference, a, b, budget, cut_attempts):
    """Bound overlap using I+U=A+B when cut masses cannot be trusted."""
    candidates, attempts = [], []
    shared_ball = None
    shared = None
    witnesses = _difference_witnesses(actual, reference)
    for fuzzy in (0.0, 1e-6, 1e-4, 1e-3):
        row = {"fuzzyMm": fuzzy}
        try:
            common = _boolean_volume("common", actual, reference, fuzzy)
            union = _boolean_volume("union", actual, reference, fuzzy)
            residual = abs(math.fsum((common, union, -a, -b)))
            row.update(commonMm3=common, unionMm3=union, residualMm3=residual)
            if common <= 1e-8:
                if shared is None:shared = _shared_interior(actual, reference)
                if shared:raise ValueError("zero intersection contradicts a shared interior point")
            if common < min(a,b)*.1:
                if shared_ball is None:shared_ball = _shared_ball_volume(actual,reference)
                row['sharedMaterialLowerBoundMm3'] = shared_ball
                if shared_ball-common > budget:
                    raise ValueError('intersection contradicts a shared interior ball')
            if (common-min(a,b) > budget or max(a,b)-union > budget
                    or math.fsum((union,-a,-b,-budget)) > 0
                    or math.fsum((common,union,-a,-b,-budget)) > 0
                    or math.fsum((a,b,-common,-union,-budget)) > 0):
                raise ValueError("intersection and union do not reconstruct both input volumes")
            if any(volume-common <= 1e-8 and point is not None for volume,point in zip((a,b),witnesses)):
                raise ValueError("full overlap contradicts a strict material-only witness")
            uncertainty = max(residual, max(0.0,common-min(a,b)), max(0.0,max(a,b)-union))
            if uncertainty:
                uncertainty = math.nextafter(uncertainty, math.inf)
            lower = min(math.fsum((min(common,a,b),-uncertainty)),
                        math.fsum((a,b,-union,-uncertainty)))
            upper = max(math.fsum((common,uncertainty)),
                        math.fsum((a,b,-union,uncertainty)))
            if uncertainty:
                lower = math.nextafter(lower, -math.inf)
                upper = math.nextafter(upper, math.inf)
            lower = max(0.0, lower)
            upper = min(a,b,upper)
            candidates.append((fuzzy,lower,upper))
            row.update(status="candidate", uncertaintyMm3=uncertainty,
                       overlapLowerMm3=lower, overlapUpperMm3=upper)
        except Exception as exc:
            row.update(status="rejected",reason=f"{type(exc).__name__}: {exc}")
        attempts.append(row)
    if len(candidates)<2 or max(c[1] for c in candidates)-min(c[1] for c in candidates)>budget:
        raise MaterialComparisonInconclusive(
            f"Intersection is inconclusive: common/union identity disagrees; cut attempts={cut_attempts}; union attempts={attempts}",
            "COMMON_UNION_DISAGREES", budget, attempts, candidates, volumes=(a, b))
    overlap = min(c[1] for c in candidates)
    return overlap, any(c[0]!=0 for c in candidates), {
        "method":"common_union_identity", "consistencyToleranceMm3":budget,
        "selectedOverlapMm3":overlap,
        "overlapIntervalMm3":{"min":overlap,"max":max(c[2] for c in candidates)},
        "volumeIntegration":"adaptive_OCCT_eps_1e-12", "differenceWitnesses":witnesses,
        "cutAttempts":cut_attempts,"attempts":attempts}


def _overlap_volume(actual, reference, volume_tolerance=0.01):
    """Check both material differences and require agreement across fuzzy levels."""
    a, b = _volume(actual.wrapped), _volume(reference.wrapped)
    limit = min(a, b)
    # Charge the full measured residual within the caller's material budget.
    budget = min(max(1e-8, max(a, b) * 2e-6), volume_tolerance)
    candidates = []
    attempts = []
    shared_interior = None
    shared_ball = None
    difference_witnesses = None
    # Coarser fuzzy tolerances can collapse a tiny real feature or invalidate
    # an otherwise valid vendor STEP. If the usual levels cannot establish
    # agreement, also measure finer levels under exactly the same checks.
    for index, fuzzy in enumerate((0.0, 1e-6, 1e-4, 1e-3, 1e-9, 1e-8, 1e-7, 1e-5, 2e-4, 5e-4)):
        if index == 4 and len(candidates) >= 2:
            break
        attempt = {"fuzzyMm": fuzzy}
        try:
            common = _boolean_volume("common", actual, reference, fuzzy)
            actual_only = _boolean_volume("cut", actual, reference, fuzzy)
            reference_only = _boolean_volume("cut", reference, actual, fuzzy)
            attempt.update(commonMm3=common, actualOnlyMm3=actual_only, referenceOnlyMm3=reference_only)
            if min(actual_only,reference_only)<=1e-8:
                if difference_witnesses is None:
                    difference_witnesses = _difference_witnesses(actual,reference)
                if actual_only<=1e-8 and difference_witnesses[0] is not None:
                    raise ValueError("zero actual difference contradicts a strict material-only witness")
                if reference_only<=1e-8 and difference_witnesses[1] is not None:
                    raise ValueError("zero reference difference contradicts a strict material-only witness")
            if common > limit:
                if common - limit > budget:
                    raise ValueError("intersection exceeds an input volume")
            if common <= 1e-8:
                if shared_interior is None:
                    shared_interior = _shared_interior(actual, reference)
                if shared_interior:
                    raise ValueError("zero intersection contradicts a shared interior point")
            if common < limit*.1:
                if shared_ball is None:shared_ball = _shared_ball_volume(actual,reference)
                attempt['sharedMaterialLowerBoundMm3'] = shared_ball
                if shared_ball-common > budget:
                    raise ValueError('intersection contradicts a shared interior ball')
            residual = max(abs(math.fsum((common, actual_only, -a))),
                           abs(math.fsum((common, reference_only, -b))))
            if residual > budget:
                raise ValueError("intersection and differences do not reconstruct both input volumes")
            estimates = (common, a - actual_only, b - reference_only)
            if min(estimates) < -budget:
                raise ValueError("material difference exceeds an input volume")
            # Charge the full decomposition mismatch to the material result.
            # An impossible raw common is never rounded into a zero difference.
            uncertainty = residual
            lower = min(estimates) - uncertainty
            upper = max(estimates) + uncertainty
            if uncertainty:
                # Outward rounding prevents cancellation from hiding a residual.
                lower = math.nextafter(lower, -math.inf)
                upper = math.nextafter(upper, math.inf)
            lower = max(0.0, lower)
            upper = min(limit, upper)
            candidates.append((fuzzy, lower, upper))
            attempt.update(status="candidate", residualMm3=residual,
                           uncertaintyMm3=uncertainty, overlapLowerMm3=lower, overlapUpperMm3=upper)
        except Exception as exc:
            attempt.update(status="rejected", reason=f"{type(exc).__name__}: {exc}")
        attempts.append(attempt)
    if len(candidates) < 2:
        invalid_cut = "ValueError: cut boolean returned invalid volume"
        false_zero_cuts = {
            "ValueError: zero actual difference contradicts a strict material-only witness",
            "ValueError: zero reference difference contradicts a strict material-only witness",
        }
        # Only diagnosed cut mass contradictions enable another identity check.
        reasons = [row.get("reason") for row in attempts]
        cut_mass_failures = (all(reason == invalid_cut for reason in reasons)
            or (sum(reason in false_zero_cuts for reason in reasons) >= 2
                and all(reason in false_zero_cuts or reason in {
                    invalid_cut, "ValueError: intersection contradicts a shared interior ball",
                    "ValueError: zero intersection contradicts a shared interior point",
                } for reason in reasons)))
        if not candidates and cut_mass_failures:
            return _union_overlap_volume(actual,reference,a,b,budget,attempts)
        raise MaterialComparisonInconclusive(
            f"Intersection is inconclusive: fewer than two consistent booleans; {attempts}",
            "INSUFFICIENT_CONSISTENT_BOOLEANS", budget, attempts, candidates, volumes=(a, b))
    if max(c[1] for c in candidates) - min(c[1] for c in candidates) > budget:
        raise MaterialComparisonInconclusive(
            f"Intersection is inconclusive: fuzzy levels disagree; {attempts}",
            "FUZZY_LEVELS_DISAGREE", budget, attempts, candidates, volumes=(a, b))
    # The smallest independently measured overlap gives conservative material
    # differences; never select the largest or clip an impossible intersection.
    overlap = min(c[1] for c in candidates)
    return overlap, any(c[0] != 0 for c in candidates), {
        "consistencyToleranceMm3": budget, "selectedOverlapMm3": overlap,
        "overlapIntervalMm3": {"min": overlap, "max": max(c[2] for c in candidates)},
        "volumeIntegration": "adaptive_OCCT_eps_1e-12", "differenceWitnesses": difference_witnesses,
        "attempts": attempts}


def _scale_bd_shape(shape, factor):
    """Scale a build123d shape about the origin without changing its topology."""
    import build123d as bd
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Pnt, gp_Trsf

    transform = gp_Trsf()
    transform.SetScale(gp_Pnt(0.0, 0.0, 0.0), factor)
    return bd.Shape.cast(BRepBuilderAPI_Transform(shape.wrapped, transform, True).Shape())


def _descale_diagnostics(diagnostics, factor):
    """Convert scaled boolean diagnostics back to the caller's millimetres."""
    if not isinstance(diagnostics, dict):
        return diagnostics
    # Volumes were measured after scaling by `factor`, so scaled mm3 must be
    # divided by factor**3 to return to the caller's millimetres.
    inverse = 1.0 / (factor ** 3) if factor else 1.0
    result = copy.deepcopy(diagnostics)
    result["scaleFactor"] = factor
    for key in ("consistencyToleranceMm3", "selectedOverlapMm3",
                "sharedMaterialLowerBoundMm3"):
        if type(result.get(key)) in (int, float):
            result[key] = float(result[key]) * inverse
    interval = result.get("overlapIntervalMm3")
    if isinstance(interval, dict):
        for key in ("min", "max"):
            if type(interval.get(key)) in (int, float):
                interval[key] = float(interval[key]) * inverse
    for attempt in result.get("attempts", []):
        if not isinstance(attempt, dict):
            continue
        for key in ("commonMm3", "actualOnlyMm3", "referenceOnlyMm3", "residualMm3",
                    "uncertaintyMm3", "overlapLowerMm3", "overlapUpperMm3",
                    "sharedMaterialLowerBoundMm3"):
            if type(attempt.get(key)) in (int, float):
                attempt[key] = float(attempt[key]) * inverse
        if type(attempt.get("fuzzyMm")) in (int, float) and factor:
            attempt["fuzzyMm"] = float(attempt["fuzzyMm"]) / factor
    return result


def _surface_samples(shape, deflection, limit=8000):
    """Boundary vertices of a tessellated solid, deduplicated."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    BRepMesh_IncrementalMesh(shape, deflection, False, 0.5, True)
    points = []
    seen = set()
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face(explorer.Current())
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is not None:
            transform = location.Transformation()
            for index in range(1, triangulation.NbNodes() + 1):
                point = triangulation.Node(index).Transformed(transform)
                key = (round(point.X(), 5), round(point.Y(), 5), round(point.Z(), 5))
                if key in seen:
                    continue
                seen.add(key)
                points.append((point.X(), point.Y(), point.Z()))
                if len(points) >= limit:
                    return points
        explorer.Next()
    return points


def _max_boundary_distance(points, shape):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt

    maximum = 0.0
    for point in points:
        vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
        distance = BRepExtrema_DistShapeShape(vertex, shape)
        if not distance.IsDone():
            return None
        value = float(distance.Value())
        if not math.isfinite(value):
            return None
        maximum = max(maximum, value)
    return maximum


def _surface_change_check(actual, reference, length_tol):
    """Equality check that still sees a moved feature with equal volume/bbox."""
    try:
        box = reference.bounding_box()
        reference_span = max(float(box.max.X) - float(box.min.X),
                             float(box.max.Y) - float(box.min.Y),
                             float(box.max.Z) - float(box.min.Z))
    except Exception:
        return {"name": "surface_distance", "passed": False,
                "detail": "bounding box unavailable"}
    deflection = max(length_tol * 4.0, reference_span * 1e-4)
    samples_actual = _surface_samples(actual.wrapped, deflection)
    samples_reference = _surface_samples(reference.wrapped, deflection)
    if not samples_actual or not samples_reference:
        return {"name": "surface_distance", "passed": False,
                "detail": "tessellation produced no samples"}
    forward = _max_boundary_distance(samples_actual, reference.wrapped)
    backward = _max_boundary_distance(samples_reference, actual.wrapped)
    if forward is None or backward is None:
        return {"name": "surface_distance", "passed": False,
                "detail": "boundary distance failed"}
    distance = max(forward, backward)
    return {"name": "surface_distance", "passed": distance <= length_tol,
            "actualMm": distance, "toleranceMm": length_tol,
            "samples": [len(samples_actual), len(samples_reference)]}


def _measurement_fallback(actual, reference, a, b, length_tol, volume_tol,
                          scale_factor, actual_path, reference_path, spec, error):
    """Definite PASS/FAIL from measured volume, bbox and optional probes.

    Used only when the boolean material comparison is inconclusive. The receipt
    marks method="measurement_fallback" so a caller never mistakes this for a
    full material-difference proof.
    """
    volume_delta = abs(float(a["volumeMm3"]) - float(b["volumeMm3"]))
    axes = []
    bbox_error = 0.0
    for index, axis in enumerate("xyz"):
        low = abs(float(a["bbox"]["min"][index]) - float(b["bbox"]["min"][index]))
        high = abs(float(a["bbox"]["max"][index]) - float(b["bbox"]["max"][index]))
        bbox_error = max(bbox_error, low, high)
        axes.append({"axis": axis, "deltaMm": max(low, high),
                     "toleranceMm": length_tol,
                     "passed": max(low, high) <= length_tol})
    checks = [
        {"name": "volume_measurement", "passed": volume_delta <= volume_tol,
         "actualMm3": volume_delta, "toleranceMm3": volume_tol},
        {"name": "bbox_measurement", "passed": bbox_error <= length_tol,
         "actualMm": bbox_error, "toleranceMm": length_tol, "axes": axes},
        _surface_change_check(actual.solids()[0], reference.solids()[0], length_tol),
    ]
    if "probes" in spec:
        probes = spec["probes"]
        measured = []
        for probe in probes:
            _fields(probe, {"name", "pointMm", "inside"}, {"name", "pointMm", "inside"}, "probe")
            point = probe["pointMm"]
            if (not isinstance(probe["name"], str) or not probe["name"]
                    or type(probe["inside"]) is not bool or not isinstance(point, list)
                    or len(point) != 3
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in point)):
                raise ValueError("Probe requires a name, three finite coordinates and a boolean material state")
            expected = "inside" if probe["inside"] else "outside"
            if _classify_point(reference.solids()[0], point) != expected:
                raise ValueError(f"Frozen probe {probe['name']} disagrees with the reference")
            actual_state = _classify_point(actual.solids()[0], point)
            measured.append({"name": probe["name"], "expected": expected,
                             "actual": actual_state, "passed": actual_state == expected})
        checks.append({"name": "nominal_feature_probes",
                       "passed": all(item["passed"] for item in measured),
                       "detail": measured})
    verdict = "PASS" if all(check["passed"] for check in checks) else "FAIL"
    return {
        "verdict": verdict,
        "method": "measurement_fallback",
        "checks": checks,
        "actual": a,
        "reference": b,
        "scaleFactor": scale_factor,
        "sources": {
            "actual": {"path": str(actual_path), "sha256": file_digest(actual_path)},
            "reference": {"path": str(reference_path),
                          "sha256": file_digest(reference_path)},
        },
        "overlapDiagnostics": {
            "method": "measurement_fallback",
            "reason": "MATERIAL_COMPARISON_INCONCLUSIVE",
            "errorSummary": getattr(error, "summary", None),
            "measured": {
                "actualMm3": float(a["volumeMm3"]),
                "referenceMm3": float(b["volumeMm3"]),
                "volumeDeltaMm3": volume_delta,
                "bboxErrorMm": bbox_error,
            },
        },
        "limitations": [
            "Material booleans were inconclusive; this verdict uses measured volume, "
            "bounding box and any frozen probes, not a material-difference bound.",
            "Geometric comparison does not validate CATIA feature history or constraints.",
        ],
    }


def _classify_point(shape, point):
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_IN, TopAbs_OUT, TopAbs_ON
    from OCP.gp import gp_Pnt
    classifier = BRepClass3d_SolidClassifier(shape.wrapped)
    classifier.Perform(gp_Pnt(*point), 1e-6)
    return {TopAbs_IN: "inside", TopAbs_OUT: "outside", TopAbs_ON: "boundary"}.get(classifier.State(), "unknown")


def compare_steps(spec: dict[str, Any]) -> dict[str, Any]:
    """Compare in supplied coordinates. No automatic registration or hidden fit."""
    _fields(spec, {"actual", "reference", "lengthToleranceMm", "volumeToleranceMm3", "probes"}, {"actual", "reference"})
    actual_path, actual = read_shape(spec["actual"])
    reference_path, reference = read_shape(spec["reference"])
    length_tol = float(spec.get("lengthToleranceMm", 0.01))
    volume_tol = float(spec.get("volumeToleranceMm3", 0.01))
    if not all(math.isfinite(t) and t >= 0 for t in (length_tol, volume_tol)):
        raise ValueError("Tolerances must be finite nonnegative values.")
    a, b = inspect_shape(actual), inspect_shape(reference)
    reference_span = max(
        float(b["bbox"]["max"][i]) - float(b["bbox"]["min"][i]) for i in range(3)
    )
    if not math.isfinite(reference_span) or reference_span <= 0:
        raise ValueError("Reference bbox is empty; cannot scale the comparison.")
    scale_factor = 100.0 / reference_span
    actual_scaled = _scale_bd_shape(actual, scale_factor)
    reference_scaled = _scale_bd_shape(reference, scale_factor)

    def usable(shape, diagnostics):
        # Vendor STEP references are often valid single solids that fail the
        # stricter shell-soundness gate; comparison must retain that limitation.
        return (diagnostics["solidCount"] == 1 and not diagnostics["topologyErrors"]
                and bool(shape.is_valid)
                and all(math.isfinite(value) and value > 0
                        for value in diagnostics["solidVolumesMm3"]))

    if not a["valid"] or not b["valid"]:
        if not (usable(actual, a) and usable(reference, b)):
            return {"verdict": "FAIL", "code": "INVALID_GEOMETRY", "actual": a, "reference": b}
    # Pairwise overlap sums double-count overlapping components. Limit this
    # comparator to single-solid parts rather than return a misleading PASS.
    if a["solidCount"] != 1 or b["solidCount"] != 1:
        raise ValueError("Reference comparison requires one solid per STEP; assemblies are unsupported.")
    try:
        overlap_scaled, used_fuzzy, overlap_diagnostics = _overlap_volume(
            actual_scaled.solids()[0], reference_scaled.solids()[0],
            volume_tol * (scale_factor ** 3),
        )
    except MaterialComparisonInconclusive as exc:
        return _measurement_fallback(
            actual, reference, a, b, length_tol, volume_tol, scale_factor,
            actual_path, reference_path, spec, exc,
        )
    overlap = overlap_scaled / (scale_factor ** 3)
    overlap_diagnostics = _descale_diagnostics(overlap_diagnostics, scale_factor)
    if not math.isfinite(overlap) or overlap < 0:
        raise ValueError("Intersection is nonfinite or negative; comparison is inconclusive.")
    missing = b["volumeMm3"] - overlap
    excess = a["volumeMm3"] - overlap
    epsilon = max(1e-7, max(a["volumeMm3"], b["volumeMm3"]) * 1e-9)
    if missing < -epsilon or excess < -epsilon:
        raise ValueError("Intersection exceeds an input volume; comparison is inconclusive.")
    missing, excess = max(0.0, missing), max(0.0, excess)
    overlap_upper = overlap_diagnostics["overlapIntervalMm3"]["max"]
    bbox_error = max(abs(x - y) for side in ("min", "max")
                     for x, y in zip(a["bbox"][side], b["bbox"][side]))
    checks = [
        {"name": "bbox", "passed": bbox_error <= length_tol,
         "actualMm": bbox_error, "toleranceMm": length_tol},
        {"name": "missing_material", "passed": missing <= volume_tol,
         "actualMm3": missing, "minimumMm3": max(0.0, b["volumeMm3"]-overlap_upper), "toleranceMm3": volume_tol},
        {"name": "excess_material", "passed": excess <= volume_tol,
         "actualMm3": excess, "minimumMm3": max(0.0, a["volumeMm3"]-overlap_upper), "toleranceMm3": volume_tol},
    ]
    if "probes" in spec:
        probes = spec["probes"]
        if not isinstance(probes, list) or not probes:
            raise ValueError("probes must be a nonempty list")
        measured = []
        for probe in probes:
            _fields(probe, {"name", "pointMm", "inside"}, {"name", "pointMm", "inside"}, "probe")
            point = probe["pointMm"]
            if (not isinstance(probe["name"], str) or not probe["name"]
                    or type(probe["inside"]) is not bool or not isinstance(point, list) or len(point) != 3
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in point)):
                raise ValueError("Probe requires a name, three finite coordinates and a boolean material state")
            expected = "inside" if probe["inside"] else "outside"
            if _classify_point(reference.solids()[0], point) != expected:
                raise ValueError(f"Frozen probe {probe['name']} disagrees with the reference")
            actual_state = _classify_point(actual.solids()[0], point)
            measured.append({"name": probe["name"], "expected": expected, "actual": actual_state,
                             "passed": actual_state == expected})
        checks.append({"name": "nominal_feature_probes", "passed": all(p["passed"] for p in measured),
                       "detail": measured})
    return {
        "verdict": "PASS" if all(c["passed"] for c in checks) else "FAIL",
        "checks": checks, "actual": a, "reference": b, "overlapDiagnostics": overlap_diagnostics,
        "scaleFactor": scale_factor,
        "sources": {"actual": {"path": str(actual_path), "sha256": file_digest(actual_path)},
                    "reference": {"path": str(reference_path), "sha256": file_digest(reference_path)}},
        "limitations": ["Same coordinate frame required; no automatic registration.",
                        "Geometric comparison does not validate CATIA feature history or constraints.",
                        "Features smaller than the declared tolerances require separate checks.",
                        "Booleans were evaluated after scaling both inputs by scaleFactor; tolerances were scaled with them.",
                        *(["Material values include measured decomposition uncertainty; minimumMm3 "
                           "is used to distinguish a measured edit from that uncertainty."]
                          if any(attempt.get("uncertaintyMm3", 0) > 0 for attempt in overlap_diagnostics["attempts"]) else []),
                        *(["An input is a valid single solid that failed the stricter "
                           "shell-soundness check; material volumes are measured without repair."]
                          if not a["sound"] or not b["sound"] else []),
                        *(["The intersection needed OCCT's fuzzy boolean fallback; both inputs "
                           "were used unchanged."] if used_fuzzy else [])],
    }


def _fit_iso_label(sheet, shape, view):
    """Keep a small isometric view's label inside the sheet frame."""
    from cadgen.eng_drawing import _MARGIN, _text_width, _view_extent

    scale = view.scale if view.scale is not None else sheet.scale
    width, _ = _view_extent(shape, "iso", scale)
    label_width = _text_width((view.label or view.name).upper(), 3.5)
    half_width = max(width, label_width) / 2
    min_center = _MARGIN + 6 + half_width
    max_center = sheet.width - _MARGIN - 6 - half_width
    if min_center > max_center:
        raise ValueError("Isometric view or label does not fit on the sheet")
    view.at = (max(min_center, min(view.at[0], max_center)), view.at[1])
    # Keep the explicit label-aware placement if the layout guard retries.
    view._arranged = None


def generate_drawing(spec: dict[str, Any]) -> dict[str, Any]:
    from cadgen.eng_drawing import Sheet, eng_drawing

    _fields(spec, {"sourceStep", "outputPdf", "caseId", "title", "sheets"}, {"sourceStep", "outputPdf", "sheets"})
    source, shape = read_shape(spec["sourceStep"])
    target = Path(spec["outputPdf"]).resolve()
    provenance_path = target.with_suffix(".provenance.json")
    if target.suffix.lower() != ".pdf":
        raise ValueError("outputPdf must end in .pdf")
    if target.exists() or provenance_path.exists():
        raise FileExistsError("Drawing or provenance already exists; choose a new output path.")
    diagnostics = inspect_shape(shape)
    if not diagnostics["valid"]:
        # A drawing only projects the model. Vendor STEP files that are valid
        # single solids but fail the stricter shell-soundness check are usable;
        # the soundness status is recorded instead of silently repaired.
        usable = (diagnostics["solidCount"] == 1 and not diagnostics["topologyErrors"]
                  and bool(shape.is_valid)
                  and all(math.isfinite(v) and v > 0 for v in diagnostics["solidVolumesMm3"]))
        if not usable:
            raise ValueError("Cannot derive a drawing from invalid geometry.")
    pages = spec.get("sheets")
    if not isinstance(pages, list) or not pages:
        raise ValueError("sheets must be a nonempty list with explicit annotations.")
    sheets = []
    allowed_actions = {"dim", "hole", "diameter", "radius", "angle", "note", "overall"}
    for page in pages:
        _fields(page, {"size", "scale", "projection", "notes", "gap", "annotations", "views"},
                {"annotations"}, "sheet")
        _positive(page.get("scale", 1), "scale")
        if page.get("gap") is not None:
            _positive(page["gap"], "gap")
        if not isinstance(page.get("notes", []), list) or any(not isinstance(n, str) for n in page.get("notes", [])):
            raise ValueError("notes must be a list of strings.")
        with_views = page.get("views", True)
        if type(with_views) is not bool:
            raise ValueError("views must be a boolean.")
        if not isinstance(page["annotations"], list) or (with_views and not page["annotations"]):
            raise ValueError("annotations must be a nonempty list unless the sheet has no views.")
        if not with_views:
            if page["annotations"]:
                raise ValueError("a sheet without views cannot carry view annotations.")
            if not page.get("notes"):
                raise ValueError("a sheet without views needs notes.")
        sheet = Sheet(
            size=page.get("size", "A3"), scale=float(page.get("scale", 1)),
            title=spec.get("title", source.stem), part_number=spec.get("caseId", ""),
            projection=page.get("projection", "THIRD ANGLE"), units="mm",
            notes=["DERIVED DRAWING - NOMINAL GEOMETRY", *page.get("notes", [])],
        )
        if with_views:
            top, front, right, iso = sheet.three_views(shape, iso=True, gap=page.get("gap"))
            _fit_iso_label(sheet, shape, iso)
            views = {"top": top, "front": front, "right": right, "iso": iso}
        else:
            views = {}
        for annotation in page.get("annotations", []):
            _fields(annotation, {"kind", "view", "args", "kwargs"}, {"kind", "view"}, "annotation")
            kind, view_name = annotation["kind"], annotation["view"]
            if kind not in allowed_actions or view_name not in views:
                raise ValueError(f"Unsupported annotation {kind!r} on {view_name!r}.")
            kwargs = annotation.get("kwargs", {})
            if not isinstance(kwargs, dict) or not isinstance(annotation.get("args", []), list):
                raise ValueError("Annotation args/kwargs must be an array/object.")
            if "text" in kwargs:
                raise ValueError("Dimension text overrides are forbidden; use measured values.")
            getattr(views[view_name], kind)(*annotation.get("args", []), **kwargs)
        sheets.append(sheet)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cad-drawing-", dir=target.parent) as temporary:
        staged = Path(temporary) / target.name
        eng_drawing(out=str(staged))(lambda: sheets)()
        provenance = {
            "schema": "nist-derived-drawing/1", "caseId": spec.get("caseId"),
            "source": {"path": str(source), "sha256": file_digest(source)},
            "pdf": {"path": str(target), "sha256": file_digest(staged)},
            "cadgenVersion": importlib.metadata.version("cadgen"),
            "sourceGeometry": {"sound": diagnostics["sound"], "solidCount": diagnostics["solidCount"],
                               "topologyErrorCount": len(diagnostics["topologyErrors"])},
            "spec": spec, "reviewStatus": "pending_independent_review",
            "limitations": ["Views and specified annotations only; dimension completeness is not certified.",
                            "No section/detail/auxiliary views; prepare those separately when required.",
                            *(["Source geometry is a valid single solid but failed the stricter "
                               "shell-soundness check; views are projected without repair."]
                              if not diagnostics["sound"] else [])],
        }
        # Reserve both names before writing, so collisions never overwrite prior work.
        created = []
        try:
            with target.open("xb") as pdf:
                created.append(target)
                with provenance_path.open("x", encoding="utf-8") as metadata:
                    created.append(provenance_path)
                    pdf.write(staged.read_bytes())
                    json.dump(provenance, metadata, ensure_ascii=False, indent=2, allow_nan=False)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
    return {"path": str(target), "sha256": provenance["pdf"]["sha256"],
            "provenancePath": str(provenance_path), "reviewStatus": provenance["reviewStatus"]}


def _reference_faces(shape, surface_type):
    """Return supported surfaces with IDs based on the unfiltered topology order."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane

    result = []
    unsupported = 0
    for index, face in enumerate(shape.faces()):
        if index >= 8192:
            raise ValueError("Reference face inventory exceeds 8192 faces.")
        surface = BRepAdaptor_Surface(face.wrapped)
        kind = surface.GetType()
        if kind == GeomAbs_Plane:
            geometry = surface.Plane()
            name = "planar"
        elif kind == GeomAbs_Cylinder:
            geometry = surface.Cylinder()
            name = "cylindrical"
        else:
            unsupported += 1
            continue
        if surface_type and name != surface_type:
            continue
        box = face.bounding_box()
        row = {"id": str(index), "kind": name, "areaMm2": float(face.area),
               "centreOfMass": list(face.center()),
               "bbox": {"min": list(box.min), "max": list(box.max)}}
        if name == "cylindrical":
            row.update(radius=float(geometry.Radius()), axisLocation=list(geometry.Location().Coord()),
                       axisDirection=list(geometry.Axis().Direction().Coord()))
        else:
            row.update(planeLocation=list(geometry.Location().Coord()),
                       planeNormal=list(geometry.Axis().Direction().Coord()))
        result.append(row)
    return result, unsupported


def query_reference(spec):
    """Inspect a caller-verified snapshot; only supported units are reported."""
    from OCP.STEPControl import STEPControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.collections import Sequence_TCollection_AsciiString

    _fields(spec, {"sourceStep", "kind", "offset", "limit", "surfaceType"}, {"sourceStep", "kind"})
    kind = spec["kind"]
    offset, limit = spec.get("offset", 0), spec.get("limit", 50)
    surface_type = spec.get("surfaceType")
    if (kind not in {"summary", "edges", "faces"} or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 100
            or (surface_type is not None and (kind != "faces" or surface_type not in {"planar", "cylindrical"}))):
        raise ValueError("Invalid reference query or pagination.")
    reader = STEPControl_Reader()
    if reader.ReadFile(spec["sourceStep"]) != IFSelect_RetDone:
        raise ValueError("STEP could not be parsed.")
    lengths, angles, solid_angles = (Sequence_TCollection_AsciiString() for _ in range(3))
    reader.FileUnits(lengths, angles, solid_angles)
    units = [lengths.Value(i).ToCString() for i in range(1, lengths.Length() + 1)]
    if units != ["millimetre"]:
        raise ValueError("Unsupported, mixed or unknown STEP length units.")
    path, shape = read_shape(spec["sourceStep"])
    summary = inspect_shape(shape)
    if not summary["valid"]:
        raise ValueError("Reference geometry is invalid.")
    source_hash = file_digest(path)
    result = {"valid": True, "sourceSha256": source_hash, "sourceUnit": "mm",
              "coordinateFrame": "STEP model coordinates", "query": kind}
    if kind == "summary":
        result.update(summary, availableQueries=["summary", "edges", "faces"],
                      supportedFaceTypes=["planar", "cylindrical"])
    elif kind == "edges":
        inventory = inspect_edges({"sourceStep": str(path)})["edges"]
        rows = inventory[offset:offset + limit]
        result.update(offset=offset, total=len(inventory),
                      nextOffset=offset + len(rows) if offset + len(rows) < len(inventory) else None,
                      edges=[{**row, "id": str(offset + index), "sourceSha256": source_hash,
                              "coordinateFrame": result["coordinateFrame"]} for index, row in enumerate(rows)])
    else:
        inventory, unsupported = _reference_faces(shape, surface_type)
        rows = inventory[offset:offset + limit]
        result.update(offset=offset, total=len(inventory), unsupportedFaceCount=unsupported,
                      surfaceType=surface_type,
                      nextOffset=offset + len(rows) if offset + len(rows) < len(inventory) else None,
                      faces=[{**row, "sourceSha256": source_hash, "coordinateFrame": result["coordinateFrame"]}
                             for row in rows])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("draw", "compare", "inspect", "evaluate", "preview", "edges", "reference"))
    parser.add_argument("spec", type=Path)
    args = parser.parse_args(argv)
    candidate_rejected = ()
    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8-sig"))
        # Kernel/render diagnostics belong on stderr; stdout is one JSON envelope.
        with contextlib.redirect_stdout(sys.stderr):
            if args.operation == "draw":
                result = generate_drawing(spec)
            elif args.operation == "compare":
                result = compare_steps(spec)
            elif args.operation == "evaluate":
                from catia_mcp.feature_geometry import CandidateRejected, evaluate_features
                candidate_rejected = CandidateRejected
                result = evaluate_features(spec)
            elif args.operation == "preview":
                result = generate_preview(spec)
            elif args.operation == "reference":
                result = query_reference(spec)
            elif args.operation == "edges":
                result = inspect_edges(spec)
            else:
                path, shape = read_shape(spec["sourceStep"])
                result = {"path": str(path), "sha256": file_digest(path), **inspect_shape(shape)}
        ok = result.get("verdict", "PASS") == "PASS" and result.get("valid", True)
        payload = {"ok": ok, "operation": args.operation, "data": result}
    except Exception as exc:
        payload = {"ok": False, "operation": args.operation,
                   "error": {"type": type(exc).__name__, "message": str(exc)}}
        if isinstance(exc, candidate_rejected):
            payload["code"] = "CANDIDATE_REJECTED"
        elif isinstance(exc, MaterialComparisonInconclusive):
            payload["code"] = "MATERIAL_COMPARISON_INCONCLUSIVE"
            payload["error"] = {"type": "MaterialComparisonInconclusive",
                                "message": "Material comparison could not establish a reliable bound.",
                                "summary": exc.summary}
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload["ok"] else 1


def generate_preview(spec):
    _fields(spec, {"sourceStep", "outputMesh"}, {"sourceStep", "outputMesh"})
    source, shape = read_shape(spec["sourceStep"])
    if not inspect_shape(shape)["valid"]:
        raise ValueError("Cannot preview invalid geometry")
    target = Path(spec["outputMesh"]).resolve()
    if target.suffix.lower() != ".json":
        raise ValueError("outputMesh must end in .json")
    if target.exists():
        raise FileExistsError("Preview already exists")
    vertices, triangles = shape.tessellate(0.1, 0.2)
    if not triangles or len(triangles) > 100000:
        raise ValueError("Preview requires 1..100000 triangles")
    payload = {"schema": "catia-preview-mesh/1", "unit": "mm",
               "sourceSha256": file_digest(source),
               "vertices": [list(v) for v in vertices], "triangles": list(triangles)}
    encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 15_000_000:
        raise ValueError("Preview exceeds 15 MB")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as output:
        output.write(encoded)
    return {"status": "ready", "path": str(target), "sha256": file_digest(target),
            "sourceSha256": payload["sourceSha256"], "triangleCount": len(triangles)}


if __name__ == "__main__":
    raise SystemExit(main())
