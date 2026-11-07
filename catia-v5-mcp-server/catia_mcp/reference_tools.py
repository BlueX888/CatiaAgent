"""Bound, read-only reference geometry queries in generation sessions."""
from __future__ import annotations

import copy
import json
import math
import re

from catia_mcp.cad_support_client import run_support
from catia_mcp.reference_snapshot import guarded_reference_snapshot
from catia_mcp.results import operation_result


class ReferenceTools:
    def __init__(self, binding, views=None):
        self.binding = binding
        self.views = views
        self._evidence = {}

    def evidence(self):
        """Return only previously queried values after rechecking the bound source."""
        self.binding.read()
        return copy.deepcopy(self._evidence)

    def _record_edges(self, data, arguments):
        if (data.get("sourceUnit") != "mm"
                or data.get("coordinateFrame") != "STEP model coordinates"
                or not isinstance(data.get("edges"), list)
                or len(data["edges"]) > arguments.get("limit", 50)):
            raise ValueError("Invalid reference edge result.")
        pending = {}
        for index, edge in enumerate(data["edges"]):
            if (not isinstance(edge, dict) or not isinstance(edge.get("id"), str)
                    or not edge["id"].isascii() or not edge["id"].isdigit()
                    or edge["id"] != str(arguments.get("offset", 0) + index)
                    or edge.get("kind") not in {"linear", "circular"}):
                raise ValueError("Invalid reference edge identity.")
            values = {"length": edge.get("length")}
            if edge.get("kind") == "circular":
                radius = edge.get("radius")
                if (not isinstance(edge.get("centre"), list) or len(edge["centre"]) != 3
                        or type(radius) not in (int, float) or not math.isfinite(radius) or radius <= 0):
                    raise ValueError("Invalid reference circle.")
                values["radius"] = radius
                if type(radius) in (int, float) and math.isfinite(radius):
                    values["diameter"] = radius * 2
                for axis, value in zip("xyz", edge.get("centre", [])):
                    values[f"centre_{axis}"] = value
            for quantity, value in values.items():
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError("Invalid reference geometry value.")
                if quantity == "length" and value <= 0:
                    raise ValueError("Invalid reference edge length.")
                ref = f"ref3d:{self.binding.sha256}:edge:{edge['id']}:{quantity}"
                record = {"id": ref, "source": "reference_inference", "sourceSha256": self.binding.sha256,
                          "query": "edges", "entityId": str(edge["id"]), "quantity": quantity,
                          "value": value, "unit": "mm", "coordinateFrame": "STEP model coordinates",
                          "originalValue": radius if quantity == "diameter" else value,
                          "transform": {"operation": "multiply", "factor": 2} if quantity == "diameter"
                          else {"operation": "identity"}}
                if ref in self._evidence and self._evidence[ref] != record:
                    raise ValueError("Reference edge identity changed between queries.")
                pending[ref] = record
        if len(self._evidence.keys() | pending.keys()) > 50000:
            raise ValueError("Reference evidence limit exceeded.")
        for edge in data["edges"]:
            edge["evidence"] = [copy.deepcopy(record) for record in pending.values()
                                if record["entityId"] == str(edge["id"])]
        if len(json.dumps(data, allow_nan=False, separators=(",", ":")).encode("utf-8")) > 512 * 1024:
            raise ValueError("Reference query result exceeds 512 KiB.")
        self._evidence.update(copy.deepcopy(pending))

    def get_tool_definitions(self):
        return [{"name": "catia_query_reference", "description": (
            "Query the host-bound STEP reference. Edge records provide reference_inference evidence; "
            "cite its id and mark parameter source reference_inference. Face geometry is informational only; "
            "axisLocation is one point on the infinite cylinder axis, not necessarily its centre. "
            "These values are not drawing dimensions."
        ), "inputSchema": {"type": "object", "additionalProperties": False,
            "properties": {"kind": {"enum": ["summary", "edges", "faces"] + (["views", "view"] if self.views else [])},
                           "viewId": {"type": "string", "pattern": "^view_[0-9]{1,6}$"},
                           "surfaceType": {"enum": ["planar", "cylindrical"]},
                           "offset": {"type": "integer", "minimum": 0},
                           "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            "required": ["kind"]}}]

    def execute(self, tool_name, arguments):
        try:
            if (not isinstance(arguments, dict) or set(arguments) - {"kind", "surfaceType", "offset", "limit", "viewId"}
                    or arguments.get("kind") not in {"summary", "edges", "faces", "views", "view"}
                    or (arguments.get("kind") in {"views", "view"} and not self.views)
                    or (arguments.get("kind") == "view" and (not isinstance(arguments.get("viewId"), str)
                        or not re.fullmatch(r"view_[0-9]{1,6}", arguments["viewId"])))
                    or (arguments.get("kind") != "view" and "viewId" in arguments)
                    or (arguments.get("kind") == "view" and ("offset" in arguments or "limit" in arguments))
                    or ("surfaceType" in arguments and (arguments["kind"] != "faces"
                        or arguments["surfaceType"] not in {"planar", "cylindrical"}))
                    or type(arguments.get("offset", 0)) is not int or arguments.get("offset", 0) < 0
                    or type(arguments.get("limit", 50)) is not int or not 1 <= arguments.get("limit", 50) <= 100):
                raise ValueError("Invalid reference query or pagination.")
            if arguments["kind"] == "views":
                data = self.views.list(arguments.get("offset", 0), arguments.get("limit", 50))
                data.update(query="views", evidenceStatus="informational_only")
                return operation_result(tool=tool_name, ok=True, code="OK", message="Reference views listed.", data=data, modified=False)
            if arguments["kind"] == "view":
                metadata, _ = self.views.image(arguments["viewId"])
                metadata.update(query="view", sourceSha256=self.binding.sha256, evidenceStatus="informational_only")
                return operation_result(tool=tool_name, ok=True, code="OK", message="Reference view returned.", data=metadata, modified=False)
            snapshot = self.binding.read()
            with guarded_reference_snapshot(self.binding.root, snapshot) as target:
                result = run_support("reference", {"sourceStep": str(target), **arguments})
                if not result.get("ok"):
                    return operation_result(tool=tool_name, ok=False, code=result.get("code", "REFERENCE_QUERY_FAILED"),
                        message="Reference query failed or its geometry/units are unsupported.",
                        data={"detail": result.get("error", {})}, modified=False)
                data = result["data"]
                with target.open("rb") as stream:
                    final_snapshot = stream.read(self.binding.size + 1)
                if data.get("sourceSha256") != self.binding.sha256 or final_snapshot != snapshot:
                    raise ValueError("Reference snapshot identity changed during query.")
                self.binding.read()
        except (OSError, TypeError, ValueError) as error:
            return operation_result(tool=tool_name, ok=False, code="REFERENCE_QUERY_FAILED",
                message=str(error), data={}, modified=False)
        try:
            if data.get("query") != arguments.get("kind"):
                raise ValueError("Reference result does not match the query.")
            if len(json.dumps(data, allow_nan=False, separators=(",", ":")).encode("utf-8")) > 96 * 1024:
                raise ValueError("Reference query result exceeds 96 KiB before evidence annotation.")
            if data["query"] == "edges":
                self._record_edges(data, arguments)
            elif data["query"] == "faces":
                self._check_faces(data, arguments)
        except (TypeError, ValueError) as error:
            return operation_result(tool=tool_name, ok=False, code="REFERENCE_QUERY_FAILED",
                                    message=str(error), data={}, modified=False)
        data["evidenceStatus"] = "reference_inference" if data.get("query") == "edges" else "informational_only"
        return operation_result(tool=tool_name, ok=True, code="OK", message="Reference query completed.",
                                data=data, modified=False)

    def _check_faces(self, data, arguments):
        faces = data.get("faces")
        offset = arguments.get("offset", 0)
        requested = arguments.get("surfaceType")
        total = data.get("total")
        if (data.get("sourceUnit") != "mm" or data.get("coordinateFrame") != "STEP model coordinates"
                or data.get("surfaceType") != requested or data.get("offset") != offset
                or type(total) is not int or not 0 <= total <= 8192 or not isinstance(faces, list)
                or len(faces) != min(arguments.get("limit", 50), max(0, total - offset))
                or data.get("nextOffset") != (offset + len(faces) if offset + len(faces) < total else None)
                or type(data.get("unsupportedFaceCount")) is not int or data["unsupportedFaceCount"] < 0):
            raise ValueError("Invalid reference face page.")
        seen = set()
        def vector(value):
            return (isinstance(value, list) and len(value) == 3
                    and all(type(v) in (int, float) and math.isfinite(v) for v in value))
        for face in faces:
            if not isinstance(face, dict):
                raise ValueError("Invalid reference face.")
            face_id, kind = face.get("id"), face.get("kind")
            box = face.get("bbox")
            if (not isinstance(face_id, str) or not face_id.isascii() or not face_id.isdigit()
                    or face_id in seen or kind not in {"planar", "cylindrical"} or (requested and requested != kind)
                    or face.get("sourceSha256") != self.binding.sha256
                    or face.get("coordinateFrame") != "STEP model coordinates"
                    or type(face.get("areaMm2")) not in (int, float)
                    or not math.isfinite(face["areaMm2"]) or face["areaMm2"] <= 0
                    or not vector(face.get("centreOfMass")) or not isinstance(box, dict)
                    or not vector(box.get("min")) or not vector(box.get("max"))
                    or any(lo > hi for lo, hi in zip(box["min"], box["max"]))):
                raise ValueError("Invalid reference face geometry.")
            seen.add(face_id)
            position = face.get("axisLocation" if kind == "cylindrical" else "planeLocation")
            direction = face.get("axisDirection" if kind == "cylindrical" else "planeNormal")
            if (not vector(position) or not vector(direction)
                    or not math.isclose(sum(v * v for v in direction), 1.0, abs_tol=1e-8)):
                raise ValueError("Invalid reference face axis or plane.")
            if kind == "cylindrical" and (type(face.get("radius")) not in (int, float)
                    or not math.isfinite(face["radius"]) or face["radius"] <= 0):
                raise ValueError("Invalid reference cylinder radius.")
