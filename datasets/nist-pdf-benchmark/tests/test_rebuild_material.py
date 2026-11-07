import importlib.util
import json
from pathlib import Path

import build123d as bd
import pytest


MODULE = Path(__file__).resolve().parents[1] / "tools" / "rebuild_from_schedule.py"
spec = importlib.util.spec_from_file_location("rebuild_from_schedule", MODULE)
rebuild = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rebuild)


def test_equal_volume_shift_detects_missing_and_excess(tmp_path):
    reference = tmp_path / "reference.step"
    bd.export_step(bd.Box(10, 10, 10), reference)
    result = rebuild.compare_material([bd.Pos(1, 0, 0) * bd.Box(10, 10, 10)], reference)
    assert result["status"] == "measured", result
    assert result["missingMm3"] == pytest.approx(100)
    assert result["excessMm3"] == pytest.approx(100)


def test_overlapping_regions_are_inconclusive(tmp_path):
    reference = tmp_path / "reference.step"
    bd.export_step(bd.Box(10, 10, 10), reference)
    result = rebuild.compare_material([bd.Box(10, 10, 10), bd.Pos(1, 0, 0) * bd.Box(10, 10, 10)], reference)
    assert result["status"] == "inconclusive"
    assert "overlap" in result["reason"]


def test_adjacent_regions_do_not_double_count(tmp_path):
    reference = tmp_path / "reference.step"
    bd.export_step(bd.Box(10, 10, 10), reference)
    parts = [bd.Pos(-2.5, 0, 0) * bd.Box(5, 10, 10),
             bd.Pos(2.5, 0, 0) * bd.Box(5, 10, 10)]
    result = rebuild.compare_material(parts, reference)
    assert result["status"] == "measured", result
    assert result["missingMm3"] == pytest.approx(0, abs=1e-6)
    assert result["excessMm3"] == pytest.approx(0, abs=1e-6)


def test_disjoint_model_has_full_missing_and_excess(tmp_path):
    reference = tmp_path / "reference.step"
    bd.export_step(bd.Box(10, 10, 10), reference)
    result = rebuild.compare_material([bd.Pos(30, 0, 0) * bd.Box(10, 10, 10)], reference)
    assert result["status"] == "measured", result
    assert result["missingMm3"] == pytest.approx(1000)
    assert result["excessMm3"] == pytest.approx(1000)


def test_disjoint_solids_with_overlapping_boxes_are_not_rejected(tmp_path):
    reference = tmp_path / "reference.step"
    ring = bd.Cylinder(5, 10) - bd.Cylinder(3, 10)
    bd.export_step(ring, reference)
    center = bd.Cylinder(1, 10)
    result = rebuild.compare_material([center], reference)
    assert result["status"] == "measured", result
    assert result["missingMm3"] == pytest.approx(ring.volume)
    assert result["excessMm3"] == pytest.approx(center.volume)


def test_boolean_errors_and_wrong_hash_are_inconclusive(tmp_path, monkeypatch):
    reference = tmp_path / "reference.step"
    bd.export_step(bd.Box(10, 10, 10), reference)
    assert rebuild.compare_material([bd.Box(10, 10, 10)], reference, "wrong")["status"] == "inconclusive"
    def failed(*args):
        raise ValueError("test boolean failure")
    monkeypatch.setattr(rebuild, "_boolean_volume", failed)
    result = rebuild.compare_material([bd.Box(10, 10, 10)], reference)
    assert result["status"] == "inconclusive"
    assert "test boolean failure" in result["regions"][0]["attempts"][0]["reason"]


def test_zero_intersection_with_shared_interior_is_rejected(tmp_path, monkeypatch):
    reference = tmp_path / "reference.step"
    bd.export_step(bd.Box(10, 10, 10), reference)
    def silent_failure(operation, left, right, fuzzy):
        return 0.0 if operation == "common" else left.volume
    monkeypatch.setattr(rebuild, "_boolean_volume", silent_failure)
    result = rebuild.compare_material([bd.Box(10, 10, 10)], reference)
    assert result["status"] == "inconclusive"
    assert "shared interior" in result["regions"][0]["attempts"][0]["reason"]


def test_inconsistent_fuzzy_levels_are_inconclusive(tmp_path, monkeypatch):
    reference = tmp_path / "reference.step"
    bd.export_step(bd.Box(10, 10, 10), reference)
    def unstable(operation, left, right, fuzzy):
        overlap = left.volume * (0.9 if fuzzy < 1e-4 else 0.8)
        return overlap if operation == "common" else left.volume - overlap
    monkeypatch.setattr(rebuild, "_boolean_volume", unstable)
    result = rebuild.compare_material([bd.Box(10, 10, 10)], reference)
    assert result["status"] == "inconclusive"
    assert "disagree" in result["reason"]


def test_rebuild_refuses_to_overwrite_input_reference_or_other_files(tmp_path):
    sections = tmp_path / "input.json"
    sections.write_text('{"mainDirection":[0,0,1]}', encoding="utf-8")
    reference = tmp_path / "reference.step"
    reference.write_bytes(b"reference")
    previous = tmp_path / "previous.json"
    previous.write_text("previous result", encoding="utf-8")
    originals = {path: path.read_bytes() for path in (sections, reference, previous)}

    for output in originals:
        with pytest.raises(SystemExit, match="Refusing to overwrite"):
            rebuild.main(["--sections", str(sections), "--reference", str(reference), "--out", str(output)])
        assert output.read_bytes() == originals[output]


def test_rebuild_creates_new_report_without_reference(tmp_path):
    sections = tmp_path / "input.json"
    sections.write_text(json.dumps({
        "mainDirection": [0, 0, 1],
        "profiles": [{"kind": "CIRCLE", "u": 0, "v": 0, "radius": 2}],
        "layers": [{"z0": 0, "z1": 3, "regions": [[['OUTER', 1]]]}],
    }), encoding="utf-8")
    output = tmp_path / "new-report.json"

    assert rebuild.main(["--sections", str(sections), "--out", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["volumeMm3"] == pytest.approx(12 * 3.141592653589793)
