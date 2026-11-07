import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from catia_mcp.drawing.feature_executor import validate_compiled_plan
from catia_mcp.drawing.feature_model import FeatureError, compile_features
from catia_mcp.server import CATIAMCPServer


def test_reference_tool_is_bound_and_cannot_bypass_evidence(tmp_path, monkeypatch):
    data = b"ISO-10303-21;\nEND-ISO-10303-21;"
    (tmp_path / "reference.step").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(tmp_path))
    monkeypatch.setenv("CATIA_REFERENCE_SHA256", digest)
    monkeypatch.setenv("CATIA_REFERENCE_SIZE", str(len(data)))
    monkeypatch.setattr("catia_mcp.reference_tools.run_support", lambda operation, spec: {
        "ok": True, "data": {"sourceSha256": digest, "query": spec["kind"]}})

    async def run():
        server = CATIAMCPServer()
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: pytest.fail("COM accessed"))
        try:
            response = await server.call_tool("catia_query_reference", {"kind": "summary"})
            assert response.structuredContent["data"]["evidenceStatus"] == "informational_only"
            bad = await server.call_tool("catia_query_reference", {"kind": "summary", "path": "elsewhere.step"})
            assert bad.structuredContent["code"] == "INVALID_ARGUMENT"
            (tmp_path / "reference.step").write_bytes(b"x" * len(data))
            changed = await server.call_tool("catia_query_reference", {"kind": "summary"})
            assert changed.isError
            denied = await server.call_tool("catia_build_part", {"requirements": {}, "deliveryPath": str(tmp_path / "part.CATPart")})
            assert denied.isError
        finally:
            await server.close()

    asyncio.run(run())


@pytest.mark.skipif(not os.environ.get("CATIA_REFERENCE_TEST_STEP") or not os.environ.get("CATIA_CAD_SUPPORT_PYTHON"),
                    reason="Set a real STEP and isolated OCCT interpreter for this integration test")
def test_real_reference_query_is_usable_by_same_server(tmp_path, monkeypatch):
    source = Path(os.environ["CATIA_REFERENCE_TEST_STEP"])
    target = tmp_path / "reference.step"
    shutil.copyfile(source, target)
    payload = target.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(tmp_path))
    monkeypatch.setenv("CATIA_REFERENCE_SHA256", digest)
    monkeypatch.setenv("CATIA_REFERENCE_SIZE", str(len(payload)))
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps({"pages": [{"spans": [{"id": "p1:u", "text": "UNITS mm"},
        {"id": "p1:W", "text": "30"}, {"id": "p1:T", "text": "8"}]}]}), encoding="utf-8")

    async def run():
        server = CATIAMCPServer()
        try:
            queried = await server.call_tool("catia_query_reference", {"kind": "edges", "limit": 2})
            assert not queried.isError, json.dumps(queried.structuredContent)
            faces = await server.call_tool("catia_query_reference", {"kind": "faces", "limit": 2})
            assert not faces.isError, json.dumps(faces.structuredContent)
            assert faces.structuredContent["data"]["evidenceStatus"] == "informational_only"
            assert all(face["sourceSha256"] == digest for face in faces.structuredContent["data"]["faces"])
            row = queried.structuredContent["data"]["edges"][0]
            length = next(e for e in row["evidence"] if e["quantity"] == "length")
            assert length["sourceSha256"] == digest
            req = {"schema": "catia-feature-requirements/1", "unit": "mm", "unitEvidence": ["p1:u"],
                "parameters": [{"name": "L", "value": length["value"], "source": "reference_inference",
                                "evidence": [length["id"]]},
                               {"name": "W", "value": 30, "evidence": ["p1:W"]},
                               {"name": "T", "value": 8, "evidence": ["p1:T"]}],
                "features": [{"id": "base", "operation": "pad", "length": "T", "sketch": {
                    "plane": "xy", "profiles": [{"type": "rectangle", "width": "L", "height": "W"}]}}]}
            response = await server.call_tool("catia_compile_build_plan", {
                "requirements": req, "evidencePath": str(evidence_path)})
            assert not response.isError, json.dumps(response.structuredContent)
            validate_compiled_plan(response.structuredContent["data"]["plan"])
        finally:
            await server.close()
    asyncio.run(run())


def test_face_page_is_bounded_and_bound_to_reference(tmp_path, monkeypatch):
    data = b"ISO-10303-21;\nEND-ISO-10303-21;"
    (tmp_path / "reference.step").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(tmp_path))
    monkeypatch.setenv("CATIA_REFERENCE_SHA256", digest)
    monkeypatch.setenv("CATIA_REFERENCE_SIZE", str(len(data)))
    face = {"id": "5", "kind": "cylindrical", "areaMm2": 10.0, "centreOfMass": [0, 0, 0],
            "bbox": {"min": [-1, -1, 0], "max": [1, 1, 5]}, "radius": 1.0,
            "axisLocation": [0, 0, 0], "axisDirection": [0, 0, 1],
            "sourceSha256": digest, "coordinateFrame": "STEP model coordinates"}
    page = {"sourceSha256": digest, "sourceUnit": "mm", "query": "faces",
            "coordinateFrame": "STEP model coordinates", "offset": 0, "total": 1,
            "nextOffset": None, "surfaceType": "cylindrical", "unsupportedFaceCount": 0, "faces": [face]}
    monkeypatch.setattr("catia_mcp.reference_tools.run_support", lambda *_: {"ok": True, "data": page})

    async def run():
        server = CATIAMCPServer()
        try:
            module = next(module for module in server._tool_modules if hasattr(module, "evidence"))
            assert not module.execute("catia_query_reference", {"kind": []})["ok"]
            args = {"kind": "faces", "surfaceType": "cylindrical", "limit": 1}
            result = await server.call_tool("catia_query_reference", args)
            assert not result.isError
            assert result.structuredContent["data"]["evidenceStatus"] == "informational_only"
            assert "evidence" not in result.structuredContent["data"]["faces"][0]
            face["sourceSha256"] = "0" * 64
            assert (await server.call_tool("catia_query_reference", args)).isError
            face["sourceSha256"] = digest
            face["axisDirection"] = [0, 0, 2]
            assert (await server.call_tool("catia_query_reference", args)).isError
            face["axisDirection"] = [0, 0, 1]
            face["unexpected"] = "x" * (96 * 1024)
            assert (await server.call_tool("catia_query_reference", args)).isError
            assert (await server.call_tool("catia_query_reference", {"kind": "faces", "limit": 101})).isError
            assert (await server.call_tool("catia_query_reference", {"kind": "faces", "path": "x"})).isError
            assert (await server.call_tool("catia_query_reference", {"kind": "edges", "surfaceType": "planar"})).isError
        finally:
            await server.close()
    asyncio.run(run())


def test_partial_reference_binding_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(tmp_path))
    monkeypatch.setenv("CATIA_REFERENCE_SHA256", "0" * 64)
    monkeypatch.delenv("CATIA_REFERENCE_SIZE", raising=False)
    with pytest.raises(ValueError, match="Reference binding"):
        CATIAMCPServer()


def test_reference_evidence_is_queried_bound_and_value_checked(tmp_path, monkeypatch):
    data = b"ISO-10303-21;\nEND-ISO-10303-21;"
    (tmp_path / "reference.step").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(tmp_path))
    monkeypatch.setenv("CATIA_REFERENCE_SHA256", digest)
    monkeypatch.setenv("CATIA_REFERENCE_SIZE", str(len(data)))
    monkeypatch.setattr("catia_mcp.reference_tools.run_support", lambda operation, spec: {
        "ok": True, "data": {"sourceSha256": digest, "sourceUnit": "mm", "query": "edges",
            "coordinateFrame": "STEP model coordinates", "edges": [
                {"id": "0", "kind": "circular", "length": 31.41592653589793,
                 "radius": 5.0, "centre": [10.0, 0.0, 0.0]}]}})
    manifest = tmp_path / "drawing.json"
    manifest.write_text(json.dumps({"pages": [{"spans": [{"id": "p1:u", "text": "UNITS mm"},
        {"id": "p1:L", "text": "40"}, {"id": "p1:W", "text": "30"},
        {"id": "p1:T", "text": "8"}, {"id": "p1:R5", "text": "R5"},
        {"id": "p1:R6", "text": "R6"}, {"id": "p1:tol", "text": "R6 ± 5"},
        {"id": "p1:matching_tol", "text": "R5 ± 0.1"},
        {"id": "p1:asymmetric", "text": "R6 +5/-0.2"},
        {"id": "p1:matching_asymmetric", "text": "R5 +0.1/-0.2"},
        {"id": "p1:note", "text": "See detail A"}]}]}), encoding="utf-8")
    ref = f"ref3d:{digest}:edge:0:radius"
    req = {"schema": "catia-feature-requirements/1", "unit": "mm", "unitEvidence": ["p1:u"],
           "parameters": [{"name": n, "value": v, "evidence": ["p1:" + n]}
                          for n, v in [("L", 40), ("W", 30), ("T", 8)]] +
                         [{"name": "R", "value": 5, "source": "reference_inference", "evidence": [ref]}],
           "features": [{"id": "base", "operation": "pad", "length": "T + R", "sketch": {
               "plane": "xy", "profiles": [{"type": "rectangle", "width": "L", "height": "W"}]}}]}

    async def run():
        server = CATIAMCPServer()
        args = {"requirements": req, "evidencePath": str(manifest)}
        try:
            forged = json.loads(manifest.read_text(encoding="utf-8"))
            forged["pages"][0]["spans"].append({"id": ref, "text": "5"})
            manifest.write_text(json.dumps(forged), encoding="utf-8")
            assert (await server.call_tool("catia_validate_requirements", args)).isError
            forged["pages"][0]["spans"].pop()
            manifest.write_text(json.dumps(forged), encoding="utf-8")
            with pytest.raises(FeatureError):
                compile_features(req, forged, require_evidence=False)
            result = await server.call_tool("catia_query_reference", {"kind": "edges"})
            assert result.structuredContent["data"]["edges"][0]["evidence"][1]["id"] == ref
            validated = await server.call_tool("catia_validate_requirements", args)
            assert not validated.isError, json.dumps(validated.structuredContent)
            req["parameters"][-1]["evidence"].extend(["p1:R5", "p1:R6"])
            conflict = await server.call_tool("catia_validate_requirements", args)
            assert conflict.isError
            assert any(item["code"] == "EVIDENCE_CONFLICT"
                       for item in conflict.structuredContent["data"]["problems"])
            assert (await server.call_tool("catia_compile_build_plan", args)).isError
            req["parameters"][-1]["evidence"] = [ref, "p1:R5", "p1:tol"]
            assert (await server.call_tool("catia_validate_requirements", args)).isError
            req["parameters"][-1]["evidence"] = [ref, "p1:matching_tol"]
            assert not (await server.call_tool("catia_validate_requirements", args)).isError
            req["parameters"][-1]["evidence"] = [ref, "p1:R5", "p1:asymmetric"]
            assert (await server.call_tool("catia_validate_requirements", args)).isError
            req["parameters"][-1]["evidence"] = [ref, "p1:matching_asymmetric"]
            assert not (await server.call_tool("catia_validate_requirements", args)).isError
            req["parameters"][-1]["evidence"] = [ref, "p1:R5", "p1:note"]
            assert not (await server.call_tool("catia_validate_requirements", args)).isError
            req["parameters"][-1]["evidence"] = [ref]
            compiled = await server.call_tool("catia_compile_build_plan", args)
            assert not compiled.isError, json.dumps(compiled.structuredContent)
            validate_compiled_plan(compiled.structuredContent["data"]["plan"])
            from copy import deepcopy
            inches_req = deepcopy(req)
            inches_req["unit"] = "inch"
            inches_req["parameters"][-1]["value"] = 5 / 25.4
            inch_evidence = deepcopy(forged)
            inch_evidence["pages"][0]["spans"][0]["text"] = "UNITS inch"
            inch_plan = compile_features(inches_req, inch_evidence,
                reference_evidence=next(module for module in server._tool_modules
                                        if hasattr(module, "evidence")).evidence())
            assert inch_plan["parameters"][-1]["value"] == pytest.approx(5)
            req["parameters"][-1]["evidence"].append("p1:T")
            assert (await server.call_tool("catia_validate_requirements", args)).isError
            req["parameters"][-1]["evidence"].pop()
            req["parameters"][-1]["value"] = 6
            assert (await server.call_tool("catia_validate_requirements", args)).isError
            req["parameters"][-1]["value"] = 5
            req["parameters"][-1].pop("source")
            assert (await server.call_tool("catia_validate_requirements", args)).isError
            req["parameters"][-1]["source"] = "reference_inference"
            (tmp_path / "reference.step").write_bytes(b"x" * len(data))
            assert (await server.call_tool("catia_validate_requirements", args)).isError
        finally:
            await server.close()
    asyncio.run(run())
