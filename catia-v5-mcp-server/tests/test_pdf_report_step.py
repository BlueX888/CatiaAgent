import json

from catia_mcp.drawing.tools import _write_reports


def test_report_keeps_reopened_part_step_export(tmp_path):
    part = tmp_path / "plate.CATPart"
    step = {"path": str(tmp_path / "plate-unique.step"), "sha256": "abc"}
    paths = _write_reports(delivery={"path": str(part), "sha256": "def"},
                           plan={"planId": "plan-1", "expected": {}},
                           verification={"verdict": "PASS", "stepExport": step},
                           editability={"passed": True}, completed=True)
    report = json.loads((tmp_path / "plate-report.json").read_text(encoding="utf-8"))
    assert paths["reportJson"] == str(tmp_path / "plate-report.json")
    assert report["verification"]["stepExport"] == step
