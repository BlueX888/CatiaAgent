import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "verify_case.py"
SPEC = importlib.util.spec_from_file_location("verify_case", SCRIPT)
verify_case = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_case)


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def inputs(tmp_path):
    reference = tmp_path / "reference.step"
    reference.write_bytes(b"frozen reference")
    expectation = {"caseId": "FTC-09", "reference": {"sha256": digest(reference.read_bytes())},
                   "expected": {"volumeMm3": 1, "bbox": {}},
                   "tolerances": {"lengthMm": 0.01, "coordinateMm": 0.01, "volumeMm3": 1}}
    return reference, expectation


def test_reference_is_bound_to_frozen_hash(inputs, tmp_path):
    reference, expectation = inputs
    assert verify_case.reference_provenance(reference, expectation)["verified"] is True
    wrong = tmp_path / "wrong.step"
    wrong.write_bytes(b"different reference")
    assert verify_case.reference_provenance(wrong, expectation)["verified"] is False


def test_alternate_requires_matching_material_evidence(inputs, tmp_path):
    original, expectation = inputs
    alternate = tmp_path / "alternate.step"
    alternate.write_bytes(b"re-export")
    evidence_path = tmp_path / "comparison.json"
    evidence = {"ok": True, "data": {"verdict": "PASS", "sources": {
        "actual": {"sha256": digest(alternate.read_bytes())},
        "reference": {"sha256": digest(original.read_bytes())}}, "checks": [
        {"name": "bbox", "passed": True, "actualMm": 0.001, "toleranceMm": 0.01},
        {"name": "missing_material", "passed": True, "actualMm3": 0.1, "toleranceMm3": 1},
        {"name": "excess_material", "passed": True, "actualMm3": 0.1, "toleranceMm3": 1}]}}
    evidence_path.write_text(json.dumps(evidence))
    provenance = verify_case.reference_provenance(alternate, expectation, evidence_path)
    assert provenance["verified"] is True
    assert provenance["remainingLengthMm"] == pytest.approx(0.009)
    assert provenance["remainingVolumeMm3"] == pytest.approx(0.9)
    evidence["data"]["checks"][1]["toleranceMm3"] = 2
    evidence_path.write_text(json.dumps(evidence))
    assert verify_case.reference_provenance(alternate, expectation, evidence_path)["verified"] is False
    evidence["data"]["checks"][1]["toleranceMm3"] = 1
    evidence["data"]["sources"]["reference"]["sha256"] = "wrong"
    evidence_path.write_text(json.dumps(evidence))
    assert verify_case.reference_provenance(alternate, expectation, evidence_path)["verified"] is False


def test_unverified_reference_cannot_pass_acceptance(inputs, tmp_path, monkeypatch):
    reference, expectation = inputs
    expectation_path = tmp_path / "expectation.json"
    expectation_path.write_text(json.dumps(expectation))
    wrong = tmp_path / "wrong.step"
    wrong.write_bytes(b"different reference")
    out = tmp_path / "acceptance.json"
    part = tmp_path / "delivery.CATPart"
    part.write_bytes(b"delivery")

    def fake_run(command, cwd=None):
        if "independent_verify_part.py" in command[1]:
            out.with_suffix(".delivered.step").write_bytes(b"export")
            return 0, json.dumps({"verdict": "PASS"}), ""
        return 0, json.dumps({"ok": True, "data": {"verdict": "PASS"}}), ""

    monkeypatch.setattr(verify_case, "run", fake_run)
    result = verify_case.main(["--case", "FTC-09", "--part", str(part), "--reference", str(wrong),
                               "--expectation", str(expectation_path), "--out", str(out)])
    report = json.loads(out.read_text())
    assert result == 1
    assert report["comparison"]["data"]["verdict"] == "PASS"
    assert report["referenceProvenance"]["verified"] is False
    assert report["verdict"] == "FAIL"


def test_case_id_must_match_frozen_expectation(inputs, tmp_path):
    reference, expectation = inputs
    expectation_path = tmp_path / "expectation.json"
    expectation_path.write_text(json.dumps(expectation))
    with pytest.raises(SystemExit, match="caseId"):
        verify_case.main(["--case", "FTC-08", "--part", "unused.CATPart", "--reference", str(reference),
                          "--expectation", str(expectation_path), "--out", str(tmp_path / "out.json")])
