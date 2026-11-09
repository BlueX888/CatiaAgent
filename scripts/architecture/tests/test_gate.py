"""Gate runner self-tests: synthetic reports only, no product tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gate  # noqa: E402


FINGERPRINT = "a" * 64
OTHER = "b" * 64


def junit(tmp_path: Path, body: str, name: str = "report.xml") -> str:
    path = tmp_path / name
    path.write_text(f'<testsuite name="s">{body}</testsuite>', encoding="utf-8")
    return str(path)


def record(exit_code: int = 0, timed_out: bool = False) -> dict:
    return {"exit_code": exit_code, "timed_out": timed_out}


def verdict(report_path: str | None, report_format: str | None = "junit", *,
            exit_code: int = 0, mandatory=None, allowed=None, expected=None,
            required=None, recorded=FINGERPRINT, current=FINGERPRINT) -> dict:
    return gate.evaluate(record(exit_code), report_format=report_format, report_path=report_path,
                         mandatory_test_ids=mandatory, allowed_skips=allowed,
                         required_check_ids=required, expected_test_ids=expected,
                         recorded_fingerprint=recorded, current_fingerprint=current)


# -- expansion ---------------------------------------------------------------


def test_unknown_placeholder_fails_closed():
    with pytest.raises(gate.GateError, match="unresolved"):
        gate.expand("{MISSING}", {"KNOWN": "x"})


def test_values_are_not_shell_expanded(tmp_path):
    variables = {"REPO_ROOT": str(tmp_path)}
    argv = gate.expand_argv([sys.executable, "-c", "import sys;print(sys.argv[1])", "{REPO_ROOT};echo injected"], variables)
    out = gate.run_argv(argv, str(tmp_path), out_dir=str(tmp_path / "out"))
    assert out["exit_code"] == 0
    assert (tmp_path / "out" / "stdout.txt").read_text(encoding="utf-8").strip() == str(tmp_path) + ";echo injected"


def test_environment_drops_catia_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CATIA_MCP_EDIT_DB", "production.db")
    monkeypatch.setenv("KEEP_ME", "yes")
    argv = [sys.executable, "-c", "import os;print(os.environ.get('CATIA_MCP_EDIT_DB'), os.environ.get('KEEP_ME'))"]
    out = gate.run_argv(argv, str(tmp_path), out_dir=str(tmp_path / "out"))
    assert out["exit_code"] == 0
    assert (tmp_path / "out" / "stdout.txt").read_text(encoding="utf-8").strip() == "None yes"


# -- junit -------------------------------------------------------------------


def test_zero_collected_is_not_pass(tmp_path):
    result = verdict(junit(tmp_path, ""))
    assert result["status"] == "FAIL"
    assert any("zero collected" in reason for reason in result["reasons"])


def test_failed_check_is_not_pass(tmp_path):
    result = verdict(junit(tmp_path, '<testcase classname="m" name="t"><failure message="boom"/></testcase>'))
    assert result["status"] == "FAIL"
    assert any("failed" in reason for reason in result["reasons"])


def test_unapproved_skip_is_not_pass(tmp_path):
    result = verdict(junit(tmp_path, '<testcase classname="m" name="t"><skipped message="skip"/></testcase>'))
    assert result["status"] == "FAIL"
    assert any("unapproved skip" in reason for reason in result["reasons"])


def test_registered_skip_is_allowed(tmp_path):
    result = verdict(junit(tmp_path, '<testcase classname="m" name="t"><skipped message="G4 only"/></testcase>'),
                     allowed=["m::t"])
    assert result["status"] == "PASS", result["reasons"]


def test_xfail_is_not_proof_unless_registered(tmp_path):
    body = '<testcase classname="m" name="t"><skipped type="pytest.xfail" message="known"/></testcase>'
    assert verdict(junit(tmp_path, body))["status"] == "FAIL"
    assert verdict(junit(tmp_path, body), allowed=["m::t"])["status"] == "PASS"


def test_missing_report_is_not_pass(tmp_path):
    result = verdict(str(tmp_path / "missing.xml"))
    assert result["status"] == "FAIL"
    assert any("report missing" in reason for reason in result["reasons"])


def test_nonzero_exit_is_not_pass(tmp_path):
    result = verdict(junit(tmp_path, '<testcase classname="m" name="t"/>'), exit_code=3)
    assert result["status"] == "FAIL"


def test_fingerprint_mismatch_is_not_pass(tmp_path):
    result = verdict(junit(tmp_path, '<testcase classname="m" name="t"/>'), recorded=OTHER)
    assert result["status"] == "FAIL"
    assert any("fingerprint" in reason for reason in result["reasons"])


def test_missing_mandatory_check_is_not_pass(tmp_path):
    result = verdict(junit(tmp_path, '<testcase classname="m" name="t"/>'), mandatory=["m::other"])
    assert result["status"] == "FAIL"
    assert any("mandatory" in reason for reason in result["reasons"])


def test_changed_test_id_set_is_not_pass(tmp_path):
    result = verdict(junit(tmp_path, '<testcase classname="m" name="t"/>'), expected=["m::t", "m::gone"])
    assert result["status"] == "FAIL"
    assert any("test ID set changed" in reason for reason in result["reasons"])


def test_clean_report_passes(tmp_path):
    result = verdict(junit(tmp_path, '<testcase classname="m" name="t"/>'))
    assert result["status"] == "PASS", result["reasons"]
    assert result["parsed"]["counts"] == {"collected": 1, "passed": 1, "failed": 0,
                                          "errors": 0, "skipped": 0, "xfailed": 0}


# -- other report formats ----------------------------------------------------


def test_tap_zero_tests_fails(tmp_path):
    path = tmp_path / "t.tap"
    path.write_text("TAP version 13\n1..0\n# tests 0\n# pass 0\n# fail 0\n", encoding="utf-8")
    assert verdict(str(path), "tap")["status"] == "FAIL"


def test_tap_failure_fails(tmp_path):
    path = tmp_path / "t.tap"
    path.write_text("1..1\nnot ok 1 - case\n# tests 1\n# pass 0\n# fail 1\n", encoding="utf-8")
    result = verdict(str(path), "tap")
    assert result["status"] == "FAIL"
    assert result["parsed"]["failures"]


def test_tap_pass(tmp_path):
    path = tmp_path / "t.tap"
    path.write_text("1..1\nok 1 - case\n# tests 1\n# pass 1\n# fail 0\n# skipped 0\n", encoding="utf-8")
    assert verdict(str(path), "tap")["status"] == "PASS"


def test_vitest_pending_fails_unless_allowed(tmp_path):
    path = tmp_path / "v.json"
    path.write_text(json.dumps({"numTotalTests": 2, "numPassedTests": 1, "numFailedTests": 0,
                                "numPendingTests": 1, "testResults": []}), encoding="utf-8")
    assert verdict(str(path), "vitest-json")["status"] == "FAIL"


def test_contract_wheel_check_requires_each_id_once(tmp_path):
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("ok", encoding="utf-8")
    path = tmp_path / "wheel.json"
    payload = {"schema_version": "contract-wheel-check/1",
               "checks": [{"id": "a", "passed": True, "evidence_path": str(evidence)}]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert verdict(str(path), "contract-wheel-check/1", required=["a"])["status"] == "PASS"
    assert verdict(str(path), "contract-wheel-check/1", required=["a", "b"])["status"] == "FAIL"
    payload["checks"].append({"id": "a", "passed": True, "evidence_path": str(evidence)})
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert verdict(str(path), "contract-wheel-check/1", required=["a"])["status"] == "FAIL"


def test_unknown_format_fails(tmp_path):
    path = tmp_path / "x"
    path.write_text("whatever", encoding="utf-8")
    assert verdict(str(path), "made-up")["status"] == "FAIL"


# -- test_run mechanics ------------------------------------------------------


def test_run_test_run_refuses_to_reuse_a_report(tmp_path, monkeypatch):
    report = tmp_path / "junit.xml"
    report.write_text("old", encoding="utf-8")
    spec = {"id": "x", "source_path": None, "runner": "pytest", "cwd": str(tmp_path),
            "argv": [sys.executable, "-c", "pass"], "report_format": "junit",
            "report_path": str(report)}
    with pytest.raises(gate.GateError, match="reuse"):
        gate.run_test_run(spec, {}, run_root=str(tmp_path), out_dir=str(tmp_path / "out"),
                          current_fingerprint=FINGERPRINT)


def test_run_test_run_binds_fingerprint_and_parses_report(tmp_path):
    report = tmp_path / "junit.xml"
    body = '<testsuite><testcase classname="m" name="t"/></testsuite>'
    script = f"open(r'{report}', 'w', encoding='utf-8').write({body!r})"
    spec = {"id": "x", "source_path": None, "runner": "pytest", "cwd": str(tmp_path),
            "argv": [sys.executable, "-c", script], "report_format": "junit",
            "report_path": str(report)}
    result = gate.run_test_run(spec, {}, run_root=str(tmp_path), out_dir=str(tmp_path / "out"),
                               current_fingerprint=FINGERPRINT)
    assert result["status"] == "PASS", result["reasons"]
    meta = json.loads((report.parent / "junit.xml.meta.json").read_text(encoding="utf-8"))
    assert meta["source_fingerprint"] == FINGERPRINT


def test_run_test_run_blocks_on_missing_entrypoint(tmp_path):
    spec = {"id": "x", "source_path": str(tmp_path / "missing.py"), "runner": "pytest",
            "cwd": str(tmp_path), "argv": [sys.executable, "-c", "pass"],
            "report_format": None, "report_path": None}
    with pytest.raises(gate.GateError, match="entrypoint missing"):
        gate.run_test_run(spec, {}, run_root=str(tmp_path), out_dir=str(tmp_path / "out"),
                          current_fingerprint=FINGERPRINT)
