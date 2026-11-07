"""Regression checks for the data-preparation drawing legend and side surfaces."""

import importlib.util
import json
from pathlib import Path

import build123d as bd
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "derived" / "generate_section_schedule.py"
spec = importlib.util.spec_from_file_location("section_schedule", SCRIPT)
section_schedule = importlib.util.module_from_spec(spec)
spec.loader.exec_module(section_schedule)


def test_side_cone_angle_and_legend_are_in_dry_run_output(tmp_path, monkeypatch):
    # The cone axis is perpendicular to the chosen Z stack axis.
    shape = bd.Solid.make_cone(4, 2, 5, plane=bd.Plane(origin=(0, 0, 0), z_dir=(1, 0, 0)))
    source = tmp_path / "source.step"
    bd.export_step(shape, source)
    monkeypatch.setattr(section_schedule.base, "load_case", lambda _: ({"caseId": "TEST"}, source))

    out = tmp_path / "out"
    assert section_schedule.main(["--case", "TEST", "--out-dir", str(out), "--main-axis", "2", "--dry-run"]) == 0
    data = json.loads((out / "TEST.sections.json").read_text(encoding="utf-8"))
    drawing = json.loads((out / "TEST.spec.json").read_text(encoding="utf-8"))

    cone = next(feature for feature in data["sideFeatures"] if feature["kind"] == "CONE")
    assert abs(abs(cone["semiAngle"]) - 21.8014) < 0.001
    assert any("S rows describe side surfaces" in line for line in drawing["sheets"][0]["notes"])
    assert any("do not specify whether to add or remove" in line for line in drawing["sheets"][0]["notes"])


def test_cylinder_radius_does_not_require_circular_boundary_edges():
    class CylinderFace:
        radius = 5.0

        def edges(self):
            return []  # trimmed side faces can be bounded by lines and ellipses

    assert section_schedule.side_radius(CylinderFace(), "CYLINDER") == 5.0


def test_cone_angle_falls_back_to_occt_when_face_property_is_unavailable():
    shape = bd.Solid.make_cone(4, 2, 5)
    cone = next(face for face in shape.faces() if str(face.geom_type).endswith("CONE"))

    class WrappedFace:
        semi_angle = None
        wrapped = cone.wrapped

    assert abs(abs(section_schedule.side_semi_angle(WrappedFace(), "CONE")) - 21.8014) < 0.001


@pytest.mark.parametrize("artifact", ["TEST.pdf", "TEST.sections.json", "TEST.spec.json",
                                      "TEST.provenance.json", "TEST.withtext.pdf"])
def test_existing_drawing_artifacts_are_never_overwritten(tmp_path, monkeypatch, artifact):
    monkeypatch.setattr(section_schedule.base, "load_case", lambda _: ({"caseId": "TEST"}, tmp_path / "unused.step"))
    existing = tmp_path / artifact
    existing.write_bytes(b"prior evidence")

    with pytest.raises(SystemExit, match="Refusing to overwrite"):
        section_schedule.main(["--case", "TEST", "--out-dir", str(tmp_path), "--dry-run"])
    assert existing.read_bytes() == b"prior evidence"

