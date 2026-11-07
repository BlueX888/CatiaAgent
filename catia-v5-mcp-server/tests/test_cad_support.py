"""Real OCCT integration tests; run in the optional cad-support environment."""
from __future__ import annotations

import json

import pytest

bd = pytest.importorskip("build123d")
pytest.importorskip("cadgen")

from catia_mcp.cad_support import compare_steps, generate_drawing, generate_preview, main, query_reference


def export(tmp_path, name, shape):
    path = tmp_path / name
    bd.export_step(shape, str(path))
    return str(path)


def test_reference_face_pagination_and_cylinder_geometry(tmp_path):
    source = export(tmp_path, "faces.step", bd.Box(40, 30, 8) - bd.Cylinder(3, 8))
    summary = query_reference({"sourceStep": source, "kind": "summary"})
    assert "faces" in summary["availableQueries"]
    all_faces = query_reference({"sourceStep": source, "kind": "faces"})
    first = query_reference({"sourceStep": source, "kind": "faces", "offset": 0, "limit": 2})
    second = query_reference({"sourceStep": source, "kind": "faces", "offset": 2, "limit": 100})
    assert first["nextOffset"] == 2 and first["faces"] + second["faces"] == all_faces["faces"]
    cylinders = query_reference({"sourceStep": source, "kind": "faces", "surfaceType": "cylindrical"})
    assert len(cylinders["faces"]) == 1
    cylinder = cylinders["faces"][0]
    assert cylinder["radius"] == pytest.approx(3)
    assert cylinder["axisDirection"] == pytest.approx([0, 0, 1])
    assert cylinder in all_faces["faces"]
    assert cylinder["sourceSha256"] == summary["sourceSha256"]
    assert cylinder["coordinateFrame"] == "STEP model coordinates"
    with pytest.raises(ValueError, match="Invalid reference query"):
        query_reference({"sourceStep": source, "kind": "edges", "surfaceType": "planar"})
    sphere = export(tmp_path, "sphere.step", bd.Sphere(5))
    unsupported = query_reference({"sourceStep": sphere, "kind": "faces"})
    assert unsupported["faces"] == [] and unsupported["unsupportedFaceCount"] > 0


def test_preview_is_geometry_only_traceable_and_never_overwritten(tmp_path):
    source = export(tmp_path, "preview.step", bd.Box(40, 30, 8) - bd.Cylinder(3, 8))
    output = tmp_path / "mesh.json"
    result = generate_preview({"sourceStep": source, "outputMesh": str(output)})
    mesh = json.loads(output.read_text())
    assert mesh["schema"] == "catia-preview-mesh/1"
    assert result["triangleCount"] == len(mesh["triangles"]) > 12
    assert result["sourceSha256"] == mesh["sourceSha256"]
    assert all(len(t) == 3 and all(0 <= i < len(mesh["vertices"]) for i in t) for t in mesh["triangles"])
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        generate_preview({"sourceStep": source, "outputMesh": str(output)})
    assert output.read_bytes() == before


def test_same_volume_and_bounds_do_not_hide_a_wrong_hole_position(tmp_path):
    body = bd.Box(40, 30, 8)
    original = body - bd.Pos(-8, 0, 0) * bd.Cylinder(3, 8)
    wrong = body - bd.Pos(8, 0, 0) * bd.Cylinder(3, 8)
    a = export(tmp_path, "actual.step", wrong)
    b = export(tmp_path, "reference.step", original)
    report = compare_steps({"actual": a, "reference": b})
    assert report["verdict"] == "FAIL"
    assert report["checks"][0]["passed"]  # identical envelope
    assert not report["checks"][1]["passed"]
    assert not report["checks"][2]["passed"]
    assert compare_steps({"actual": b, "reference": b})["verdict"] == "PASS"


def test_feature_probe_detects_a_missing_hole_below_material_tolerance(tmp_path):
    body = bd.Box(10, 10, 1)
    reference = export(tmp_path, "tiny-hole.step", body - bd.Cylinder(.1, 1))
    actual = export(tmp_path, "missing-hole.step", body)
    spec = {"actual": actual, "reference": reference, "volumeToleranceMm3": 1}
    assert compare_steps(spec)["verdict"] == "PASS"
    report = compare_steps({**spec, "probes": [{"name": "tiny-hole-axis", "pointMm": [0, 0, 0], "inside": False}]})
    assert report["verdict"] == "FAIL"
    assert report["checks"][-1]["passed"] is False


def test_reference_probe_must_be_independently_consistent(tmp_path):
    source = export(tmp_path, "block.step", bd.Box(10, 10, 1))
    with pytest.raises(ValueError, match="disagrees with the reference"):
        compare_steps({"actual": source, "reference": source,
                       "probes": [{"name": "false-expectation", "pointMm": [0, 0, 0], "inside": False}]})


def test_shared_interior_witness_finds_thin_frame_walls():
    from catia_mcp.cad_support import _shared_interior
    frame=(bd.Box(100,100,10)-bd.Box(96,96,12)).solids()[0]
    assert _shared_interior(frame,frame)
    assert not _shared_interior(frame,bd.Box(95,95,9).solids()[0])


@pytest.mark.parametrize("tolerance", [-1, float("nan"), float("inf")])
def test_invalid_tolerance_cannot_turn_difference_into_pass(tmp_path, tolerance):
    source = export(tmp_path, "part.step", bd.Box(10, 10, 10))
    with pytest.raises(ValueError, match="Tolerances"):
        compare_steps({"actual": source, "reference": source, "volumeToleranceMm3": tolerance})


def test_multisolid_comparison_is_explicitly_unsupported(tmp_path):
    body = bd.Compound(children=[bd.Box(10, 10, 10), bd.Pos(20, 0, 0) * bd.Box(10, 10, 10)])
    source = export(tmp_path, "assembly.step", body)
    with pytest.raises(ValueError, match="one solid"):
        compare_steps({"actual": source, "reference": source})


def test_drawing_has_readable_dimensions_and_traceable_origin(tmp_path):
    fitz = pytest.importorskip("fitz")
    source = export(tmp_path, "plate.step", bd.Box(40, 30, 8))
    output = tmp_path / "plate.pdf"
    spec = {"sourceStep": source, "outputPdf": str(output), "caseId": "TEST-PLATE",
            "sheets": [{"annotations": [{"kind": "overall", "view": "top"},
                                          {"kind": "overall", "view": "front"}]}]}
    # Match production: rendering exits before OCR starts, releasing the
    # Matplotlib/OCCT process memory on shared Windows workstations.
    import sys
    from catia_mcp.cad_support_client import run_support
    drawn = run_support("draw", spec, python=sys.executable)
    assert drawn["ok"], drawn
    result = drawn["data"]
    with fitz.open(output) as doc:
        assert len(doc) == 1
    from catia_mcp.drawing.pdf_evidence import inspect_pdf
    evidence = inspect_pdf(str(output), evidence_dir=str(tmp_path / "evidence"), include_ocr=True)
    page = evidence["pages"][0]
    assert page["ocrSpanCount"] > 0
    numbers = {value for candidate in page["dimensionCandidates"] for value in candidate["values"]}
    assert {40, 30, 8} <= numbers
    assert "DERIVED" in page["text"]
    provenance = json.loads((tmp_path / "plate.provenance.json").read_text(encoding="utf-8"))
    assert provenance["source"]["path"] == source
    assert provenance["pdf"]["sha256"] == result["sha256"]
    assert result["reviewStatus"] == "pending_independent_review"
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        generate_drawing(spec)
    assert output.read_bytes() == before


def test_dimension_override_is_rejected_before_writing(tmp_path):
    source = export(tmp_path, "plate.step", bd.Box(40, 30, 8))
    output = tmp_path / "wrong.pdf"
    with pytest.raises(ValueError, match="overrides"):
        generate_drawing({"sourceStep": source, "outputPdf": str(output), "sheets": [{
            "annotations": [{"kind": "dim", "view": "top",
                             "args": [[0, 0, 0], [10, 0, 0]], "kwargs": {"text": "100"}}]}]})
    assert not output.exists()


def test_cli_failure_is_one_json_result_and_nonzero_exit(tmp_path, capsys):
    spec = tmp_path / "bad.json"
    spec.write_text(json.dumps({"sourceStep": str(tmp_path / "missing.step")}), encoding="utf-8")
    assert main(["inspect", str(spec)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["error"]["type"] == "FileNotFoundError"


@pytest.mark.parametrize("overlap", [float("nan"), float("inf"), -1])
def test_inconclusive_intersection_never_passes(tmp_path, monkeypatch, overlap):
    import catia_mcp.cad_support as support
    source = export(tmp_path, "part.step", bd.Box(10, 10, 10))
    # The comparator owns the boolean now; a non-finite or negative result is
    # never allowed to pass regardless of where it comes from.
    monkeypatch.setattr(support, "_overlap_volume", lambda actual, reference, tolerance: (overlap, True, {}))
    with pytest.raises(ValueError, match="inconclusive"):
        compare_steps({"actual": source, "reference": source})


@pytest.mark.parametrize("page", [None, {}, {"annotations": []},
    {"annotations": [None]}, {"annotations": [{"kind": "overall", "view": "top"}], "scale": 0}])
def test_bad_drawing_spec_does_not_publish_files(tmp_path, page):
    source = export(tmp_path, "part.step", bd.Box(10, 10, 10))
    target = tmp_path / "bad.pdf"
    with pytest.raises(ValueError):
        generate_drawing({"sourceStep": source, "outputPdf": str(target), "sheets": [page]})
    assert not target.exists()
    assert not target.with_suffix(".provenance.json").exists()


def test_unsound_but_valid_solid_draws_with_recorded_limitation(tmp_path, monkeypatch):
    import catia_mcp.cad_support as support
    source = export(tmp_path, "plate.step", bd.Box(40, 30, 8))
    real = support.inspect_shape

    def unsound(shape):
        result = real(shape)
        return {**result, "valid": False, "sound": False}

    monkeypatch.setattr(support, "inspect_shape", unsound)
    output = tmp_path / "unsound.pdf"
    result = support.generate_drawing({
        "sourceStep": source, "outputPdf": str(output),
        "sheets": [{"annotations": [{"kind": "overall", "view": "top"}]}]})
    assert result["reviewStatus"] == "pending_independent_review"
    provenance = json.loads((tmp_path / "unsound.provenance.json").read_text(encoding="utf-8"))
    assert provenance["sourceGeometry"]["sound"] is False
    assert any("shell-soundness" in item for item in provenance["limitations"])


def test_invalid_geometry_still_refuses_a_drawing(tmp_path, monkeypatch):
    import catia_mcp.cad_support as support
    source = export(tmp_path, "plate.step", bd.Box(40, 30, 8))
    real = support.inspect_shape

    def broken(shape):
        result = real(shape)
        return {**result, "valid": False, "sound": False,
                "topologyErrors": [{"code": "BRepCheck", "entityCount": 1}]}

    monkeypatch.setattr(support, "inspect_shape", broken)
    with pytest.raises(ValueError, match="invalid geometry"):
        support.generate_drawing({
            "sourceStep": source, "outputPdf": str(tmp_path / "bad.pdf"),
            "sheets": [{"annotations": [{"kind": "overall", "view": "top"}]}]})
    assert not (tmp_path / "bad.pdf").exists()
