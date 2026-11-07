import importlib.util
from pathlib import Path

import build123d as bd
import pytest


MODULE = Path(__file__).resolve().parents[1] / "derived" / "generate_derived.py"
spec = importlib.util.spec_from_file_location("generate_derived", MODULE)
drawing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drawing)


def test_oblique_cylindrical_face_uses_axis_point_and_direction(tmp_path):
    shape = bd.Box(30, 30, 20) - (bd.Rot(0, 45, 0) * bd.Cylinder(2, 60))
    source = tmp_path / "oblique.step"
    bd.export_step(shape, source)
    measurements = drawing.cylinder_records(shape)
    assert len(measurements) == 1
    point, axis = measurements[0]["centre"], measurements[0]["axis"]
    assert point[0] == pytest.approx(point[2])
    assert axis[0] == pytest.approx(2 ** -0.5)
    assert axis[2] == pytest.approx(2 ** -0.5)
    generated = drawing.build_spec({"caseId": "test"}, source, tmp_path / "candidate.pdf")
    text = "\n".join(note for sheet in generated["sheets"] for note in sheet["notes"])
    assert "DIRECTION (0.707,0.000,0.707)" in text
    face_row = next(line for line in text.splitlines() if "AXIAL SPAN" in line)
    assert "THRU" not in face_row and "DEPTH" not in face_row
    assert "NOT A COMPLETE ENGINEERING DRAWING" in text
    assert all(annotation["kind"] == "overall" for sheet in generated["sheets"]
               for annotation in sheet["annotations"])


def test_render_source_rejects_unattached_construction_edges(tmp_path):
    shape = bd.Compound(children=[bd.Box(10, 20, 30), bd.Edge.make_line((100, 0, 0), (200, 0, 0))])
    source = tmp_path / "reference.step"
    bd.export_step(shape, source)
    with pytest.raises(ValueError, match="non-solid geometry"):
        drawing.build_spec({"caseId": "test"}, source, tmp_path / "candidate.pdf")


def test_render_source_accepts_pure_solid(tmp_path):
    source = tmp_path / "reference.step"
    bd.export_step(bd.Box(10, 20, 30), source)
    generated = drawing.build_spec({"caseId": "test"}, source, tmp_path / "candidate.pdf")
    assert "OVERALL 10.000 X 20.000 X 30.000" in generated["sheets"][0]["notes"][0]


def test_face_radius_uses_curve_radius_for_partial_arc():
    arc = bd.Edge.make_circle(5, start_angle=0, end_angle=180)

    class SurfaceWithoutRadius:
        radius = None

        def edges(self):
            return [arc]

    assert drawing.face_radius(SurfaceWithoutRadius()) == pytest.approx(5.0)
