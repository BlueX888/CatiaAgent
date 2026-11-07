"""Public schema for the bounded feature requirement language."""
from catia_mcp.drawing.feature_model import SCHEMA


def obj(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


EXPR = {"description": "Named dimensions and arithmetic; sin/cos/tan require an angle parameter.",
        "oneOf": [{"type": "number", "const": 0}, {"type": "string", "maxLength": 256}]}
REVOLUTION_ANGLE = {"description": "Use 360 for a full revolution; do not derive it from an unrelated feature angle. A partial angle requires an evidenced angle parameter.",
                    "oneOf": [{"type": "number", "const": 360}, {"type": "string", "maxLength": 256}]}
COORD = {"oneOf": [{"type": "number", "minimum": -1000000, "maximum": 1000000}, {"type": "string", "maxLength": 256}]}
REFS = {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True}
PROFILE = {"oneOf": [obj({"type": {"const": kind}, **{key: (COORD if key in {"x", "y"} else EXPR) for key in fields}},
        ["type", *required])
    for kind, fields, required in [("rectangle", ["x", "y", "width", "height"], ["width", "height"]),
                                   ("circle", ["x", "y", "radius"], ["radius"])]]}
PROFILE["oneOf"].append(obj({"type": {"const": "polygon"},
    "points": {"type": "array", "minItems": 3, "maxItems": 256,
               "items": obj({"x": COORD, "y": COORD}, ["x", "y"])}}, ["type", "points"]))
POINT = obj({"x": COORD, "y": COORD}, ["x", "y"])
PROFILE["oneOf"].append(obj({"type": {"const": "wire"}, "start": POINT,
    "segments": {"type": "array", "minItems": 2, "maxItems": 256, "items": {"oneOf": [
        obj({"type": {"const": "line"}, "end": POINT}, ["type", "end"]),
        obj({"type": {"const": "arc"}, "end": POINT, "radius": EXPR,
             "clockwise": {"type": "boolean"}, "largeArc": {"type": "boolean"}}, ["type", "end", "radius"])
    ]}}}, ["type", "start", "segments"]))
PROFILE_REF = {"type": "string", "pattern": "^P[0-9]{1,4}$",
               "description": "Reference to a top-level named profile from the profiles table"}
COMPACT_POINT = {"oneOf": [POINT, {"type": "array", "prefixItems": [COORD, COORD],
                                   "minItems": 2, "maxItems": 2, "items": False}]}
COMPACT_SEGMENT = {"oneOf": [
    {"type": "array", "prefixItems": [{"const": "line"}, COORD, COORD],
     "minItems": 3, "maxItems": 3, "items": False},
    {"type": "array", "prefixItems": [{"const": "arc"}, COORD, COORD, EXPR,
                                      {"type": "boolean"}, {"type": "boolean"}],
     "minItems": 4, "maxItems": 6, "items": False},
]}
# Compact arrays mirror _compact_profile/_compact_segment in feature_model.py.
COMPACT_PROFILE = {"oneOf": [
    {"type": "array", "prefixItems": [{"const": "circle"}, COORD, COORD, EXPR],
     "minItems": 4, "maxItems": 4, "items": False},
    {"type": "array", "prefixItems": [{"const": "rectangle"}, COORD, COORD, EXPR, EXPR],
     "minItems": 5, "maxItems": 5, "items": False},
    {"type": "array", "prefixItems": [{"const": "polygon"},
                                      {"type": "array", "minItems": 3, "maxItems": 256,
                                       "items": COMPACT_POINT}],
     "minItems": 2, "maxItems": 2, "items": False},
    {"type": "array", "prefixItems": [{"const": "wire"}, COMPACT_POINT,
                                      {"type": "array", "minItems": 2, "maxItems": 256,
                                       "items": COMPACT_SEGMENT}],
     "minItems": 3, "maxItems": 3, "items": False},
]}
ANY_PROFILE = {"oneOf": [PROFILE, COMPACT_PROFILE]}
UNIT_VECTOR = {"type": "array", "minItems": 3, "maxItems": 3,
               "items": {"type": "number", "minimum": -1.000001, "maximum": 1.000001}}
SKETCH = obj({"plane": {"oneOf": [{"enum": ["xy", "yz", "zx"]},
              obj({"normal": UNIT_VECTOR, "xDirection": UNIT_VECTOR}, ["normal", "xDirection"])]}, "offset": COORD,
              "profiles": {"type": "array", "minItems": 1, "maxItems": 256,
                           "items": {"oneOf": [PROFILE, COMPACT_PROFILE, PROFILE_REF]}}},
             ["plane", "profiles"])
VECTOR = {"type": "array", "minItems": 3, "maxItems": 3,
          "items": {"type": "number", "minimum": -1000000, "maximum": 1000000}}
POINTS = {"type": "array", "minItems": 3, "maxItems": 3, "items": COORD}
EDGE_SELECTOR = {"oneOf": [
    obj({"type": {"const": "linear"}, "direction": VECTOR, "through": POINTS,
         "withinSegment": {"type": "boolean"}}, ["type", "direction", "through"]),
    obj({"type": {"const": "circular"}, "radius": EXPR, "centre": POINTS, "axis": VECTOR},
        ["type", "radius", "centre", "axis"]),
]}
FEATURE = {"oneOf": [obj({"id": {"type": "string", "pattern": "^[a-zA-Z][a-zA-Z0-9_]{0,47}$"},
    "operation": {"enum": operations}, "dependsOn": REFS, "sketch": SKETCH, extent: REVOLUTION_ANGLE if extent == "angle" else EXPR,
    "direction": {"enum": ["normal", "reverse"] if extent == "length" else ["normal"]},
    **({"axisX": COORD} if extent == "angle" else {}),
    **({"through": {"type": "boolean"}} if operations == ["pocket"] else {})},
    ["id", "operation", "sketch", extent])
    for operations, extent in [(["pad"], "length"), (["pocket"], "length"), (["shaft", "groove"], "angle")]]}
FEATURE["oneOf"].extend([
    obj({"id": {"type": "string", "pattern": "^[a-zA-Z][a-zA-Z0-9_]{0,47}$"},
         "operation": {"const": "fillet"}, "dependsOn": REFS, "radius": EXPR,
         "edges": {"oneOf":[EDGE_SELECTOR,{"type":"array","minItems":2,"maxItems":64,
                                          "uniqueItems":True,"items":EDGE_SELECTOR}]}},
        ["id", "operation", "radius", "edges"]),
    obj({"id": {"type": "string", "pattern": "^[a-zA-Z][a-zA-Z0-9_]{0,47}$"},
         "operation": {"const": "chamfer"}, "dependsOn": REFS, "length": EXPR,
         "angle": {"type": "number", "enum": [45]}, "edges": EDGE_SELECTOR},
        ["id", "operation", "length", "edges"]),
])
FEATURE_REQUIREMENTS_JSON = obj({
    "schema": {"const": SCHEMA}, "unit": {"enum": ["mm", "inch"]}, "unitEvidence": REFS,
    "partName": {"type": "string", "pattern": "^[a-zA-Z][a-zA-Z0-9_]{0,47}$"},
    "parameters": {"type": "array", "minItems": 1, "maxItems": 512,
        "items": obj({"name": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]{0,31}$"},
                      "type": {"enum": ["length", "angle"]}, "value": {"type": "number"},
                      "evidence": REFS, "source": {"enum": ["drawing", "reference_inference"]}}, ["name", "value", "evidence"])},
    "profiles": {"type": "object", "maxProperties": 512,
        "description": "Named profiles P<number> from the drawing; sketches reference them by name",
        "additionalProperties": ANY_PROFILE},
    "features": {"type": "array", "minItems": 1, "maxItems": 256, "items": FEATURE}
}, ["schema", "unit", "unitEvidence", "parameters", "features"])
