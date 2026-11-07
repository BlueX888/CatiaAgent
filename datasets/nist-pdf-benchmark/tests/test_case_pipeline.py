import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "run_case_pipeline.py"
spec = importlib.util.spec_from_file_location("case_pipeline", SCRIPT)
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


@pytest.fixture
def prepared(tmp_path):
    host = tmp_path / "trial" / "host"
    deliveries = host.parent / "generation" / "deliveries"
    host.mkdir(parents=True)
    deliveries.mkdir(parents=True)
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"pdf")
    # A failed first attempt can leave this file behind.
    (deliveries / "part.CATPart").write_bytes(b"partial")
    actual = deliveries / "part_v2.CATPart"
    actual.write_bytes(b"completed")
    report = {"completed": True, "verification": {"verdict": "PASS", "checks": [{"passed": True}]},
              "editability": {"passed": True, "parameters": [{"parameter": "R", "passed": True}]},
              "delivery": {"path": str(actual), "sha256": hashlib.sha256(actual.read_bytes()).hexdigest()}}
    report_path = deliveries / "part_v2.build.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    manifest = {"workspace": str(deliveries.parent), "inputSha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                "status": "generated_verified", "result": {"passed": True, "reportPath": str(report_path)}}
    (host / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (host / "events.jsonl").write_text(json.dumps({"type": "turn_completed", "finishReason": "completed"}) + "\n", encoding="utf-8")
    return host, pdf, actual, report_path


def test_successful_generation_uses_reported_delivery_not_leftover_part(prepared):
    host, pdf, actual, _ = prepared
    evidence = pipeline.generation_evidence(host, pdf, 0)
    assert evidence["passed"] is True
    assert evidence["delivery"] == str(actual)


def test_primary_span_coverage_cannot_be_replaced_by_local_or_translation_edits(prepared):
    host,pdf,_,report_path=prepared
    assert pipeline.generation_evidence(host,pdf,0,required_span_axes=['x','y','z'])['passed'] is False
    report=json.loads(report_path.read_text())
    base={'min':{'x':0,'y':0,'z':0},'max':{'x':10,'y':20,'z':30}}
    moved={'min':{'x':1,'y':1,'z':1},'max':{'x':11,'y':21,'z':31}}
    report['editability']['parameters'][0].update(bboxBeforeMm=base,bboxAfterMm=moved)
    report_path.write_text(json.dumps(report))
    assert pipeline.generation_evidence(host,pdf,0,required_span_axes=['x'])['passed'] is False
    resized={'min':base['min'],'max':{'x':11,'y':21,'z':31}}
    report['editability']['parameters'][0]['bboxAfterMm']=resized
    report_path.write_text(json.dumps(report))
    evidence=pipeline.generation_evidence(host,pdf,0,required_span_axes=['x','y','z'])
    assert evidence['passed'] is True
    assert evidence['editedBBoxSpanAxes']==['x','y','z']


@pytest.mark.parametrize("failure", ["exit", "manifest", "terminal", "editability", "hash", "input"])
def test_generation_evidence_fails_closed(prepared, failure):
    host, pdf, actual, report_path = prepared
    code = 0
    if failure == "exit":
        code = 1
    elif failure == "manifest":
        path = host / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["status"] = "not_verified"
        path.write_text(json.dumps(manifest), encoding="utf-8")
    elif failure == "terminal":
        (host / "events.jsonl").write_text(json.dumps({"type": "turn_completed", "finishReason": "error"}), encoding="utf-8")
    elif failure == "editability":
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report.pop("editability")
        report_path.write_text(json.dumps(report), encoding="utf-8")
    elif failure == "hash":
        actual.write_bytes(b"changed after build")
    else:
        pdf.write_bytes(b"different drawing")
    evidence = pipeline.generation_evidence(host, pdf, code)
    assert evidence["passed"] is False
    assert evidence["errors"]


def test_skip_run_checks_existing_generation_before_reporting_success(prepared, tmp_path, monkeypatch):
    host, pdf, actual, _ = prepared
    result_dir = tmp_path / "results" / "FTC-08" / "previous"
    result_dir.mkdir(parents=True)
    (result_dir / "run.json").write_text(json.dumps({"case": "FTC-08", "label": "previous",
                                                     "host": str(host), "runCode": 1}), encoding="utf-8")
    monkeypatch.setattr(pipeline, "RESULTS", tmp_path / "results")
    bench = tmp_path / "bench"
    (bench / "verification").mkdir(parents=True)
    (bench / "verification" / "FTC-08.json").write_text(json.dumps({"caseId": "FTC-08"}), encoding="utf-8")
    monkeypatch.setattr(pipeline, "BENCH", bench)
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="diagnostic geometry passed", stderr="")
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    code = pipeline.main(["--case", "FTC-08", "--pdf", str(pdf), "--reference", str(tmp_path / "ref.step"),
                          "--run-label", "previous", "--skip-run"])
    assert code == 1
    assert calls[0][calls[0].index("--part") + 1] == str(actual)
    assert "diagnostic-acceptance-recheck-" in calls[0][calls[0].index("--out") + 1]


def test_frozen_input_and_unfinished_terminal_cannot_pass(prepared):
    host, pdf, _, _ = prepared
    assert pipeline.generation_evidence(host, pdf, 0, "0" * 64)["passed"] is False
    (host / "events.jsonl").write_text(json.dumps({"type": "tool_call_started"}), encoding="utf-8")
    evidence = pipeline.generation_evidence(host, pdf, 0)
    assert evidence["passed"] is False
    assert evidence["terminalCompleted"] is False


def test_wrong_frozen_drawing_stops_before_start(prepared, tmp_path, monkeypatch):
    _, pdf, _, _ = prepared
    bench = tmp_path / "bench"
    (bench / "verification").mkdir(parents=True)
    (bench / "verification" / "FTC-08.json").write_text(
        json.dumps({"caseId": "FTC-08", "input": {"sha256": "0" * 64}}), encoding="utf-8")
    monkeypatch.setattr(pipeline, "BENCH", bench)
    monkeypatch.setattr(pipeline, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(pipeline, "node", lambda *args, **kwargs: pytest.fail("must not start generation"))
    with pytest.raises(SystemExit, match="does not match frozen"):
        pipeline.main(["--case", "FTC-08", "--pdf", str(pdf), "--reference", "ref.step", "--run-label", "new"])


def test_skip_run_without_existing_run_does_not_prepare_new_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "RESULTS", tmp_path)
    with pytest.raises(SystemExit, match="requires an existing run"):
        pipeline.main(["--case", "FTC-08", "--pdf", "drawing.pdf", "--reference", "ref.step",
                       "--run-label", "missing", "--skip-run"])

