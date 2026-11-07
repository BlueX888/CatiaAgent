import json
from pathlib import Path
import runpy
import subprocess

import pytest

from catia_mcp.drawing.verify import run_independent_verifier

MATCH = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/independent_verify_part.py"))["match_full_cylinders"]


def test_three_dimensional_cylinders_do_not_use_plate_thickness():
    faces = [{"radius": 4, "height": 15, "centre": [7.5, 25, 15]},
             {"radius": 8, "height": 5, "centre": [12.5, 25, 15]}]
    assert MATCH(faces, list(reversed(faces)), .01, .01)
    assert not MATCH(faces, [{**faces[0], "centre": [7.5, 20, 15]}, faces[1]], .01, .01)
    assert not MATCH(faces, [faces[0], faces[0]], .01, .01)
    assert not MATCH(faces, [*faces, faces[0]], .01, .01)
    wanted = [{**faces[0], "axis": [1, 0, 0]}]
    assert MATCH(wanted, [{**faces[0], "axis": [-10, 0, 0]}], .01, .01)
    assert not MATCH(wanted, [{**faces[0], "axis": [0, 10, 0]}], .01, .01)
    assert not MATCH(wanted, [{**faces[0], "axis": [0, 0, 0]}], .01, .01)


@pytest.mark.parametrize("payload,exit_code", [
    ({"verdict": "PASS", "checks": [{"passed": True}]}, 1),
    ({"verdict": "PASS", "checks": [{"passed": False}]}, 0),
    ({"verdict": "PASS", "checks": []}, 0),
    ({"verdict": "PASS", "checks": [{"passed": "yes"}]}, 0),
    ([], 0), (None, 0),
])
def test_untrustworthy_subprocess_never_passes(tmp_path, monkeypatch, payload, exit_code):
    part = tmp_path / "part.CATPart"
    part.write_bytes(b"test double")
    class Child:
        returncode = exit_code
        def communicate(self, **kwargs):
            return json.dumps(payload), ""
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: Child())
    monkeypatch.setenv("CATIA_MCP_WORK_DIR", str(tmp_path))
    assert run_independent_verifier(str(part), {})["verdict"] == "FAIL"
