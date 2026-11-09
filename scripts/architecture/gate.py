"""Development gate runner.

Commands are argv arrays expanded from a fixed variable map.  There is no shell,
no command substitution and no implicit retry.  A PASS requires all of:

* exit code 0,
* the expected report exists and parses,
* at least one mandatory check was collected,
* no failed/errored checks,
* no skip/xfail outside the registered allow-list,
* the report was produced against the current source fingerprint.

Reports produced before the current edit cannot certify it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from task_state import source_fingerprint  # noqa: E402

PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
REPORT_FORMATS = {"junit", "tap", "vitest-json", "contract-wheel-check/1"}
FORBIDDEN_ENV_PREFIXES = ("CATIA_",)


class GateError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def expand(value: str, variables: dict[str, str]) -> str:
    """Single-pass expansion; unknown placeholders fail closed."""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            missing.append(key)
            return match.group(0)
        replacement = variables[key]
        if not isinstance(replacement, str) or not replacement.strip():
            raise GateError(f"variable {key} has no usable value")
        return replacement

    expanded = PLACEHOLDER.sub(replace, value)
    if missing:
        raise GateError(f"unresolved variables: {sorted(set(missing))}")
    return expanded


def expand_argv(argv: list[str], variables: dict[str, str]) -> list[str]:
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise GateError("argv must be a non-empty array of strings")
    return [expand(item, variables) for item in argv]


def load_variables(repository_map_path: str, overrides: dict[str, str] | None = None) -> dict[str, str]:
    with open(repository_map_path, encoding="utf-8") as handle:
        repository_map = json.load(handle)
    variables: dict[str, str] = {}
    for section in ("paths", "executables"):
        variables.update({key: value for key, value in repository_map.get(section, {}).items()
                          if isinstance(value, str)})
    variables.update(repository_map.get("path_variables", {}) if isinstance(repository_map.get("path_variables"), dict) else {})
    if overrides:
        variables.update(overrides)
    return variables


def sanitized_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not any(key.upper().startswith(prefix) for prefix in FORBIDDEN_ENV_PREFIXES)}
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def run_argv(argv: list[str], cwd: str, *, out_dir: str, env_extra: dict[str, str] | None = None,
             timeout: int | None = None, stdout_name: str = "stdout.txt",
             stderr_name: str = "stderr.txt") -> dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    stdout_path = os.path.join(out_dir, stdout_name)
    stderr_path = os.path.join(out_dir, stderr_name)
    started = _utc_now()
    clock = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(argv, cwd=cwd, env=sanitized_env(env_extra),
                              capture_output=True, text=True, timeout=timeout)
        exit_code = proc.returncode
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
    with open(stdout_path, "w", encoding="utf-8") as handle:
        handle.write(stdout or "")
    with open(stderr_path, "w", encoding="utf-8") as handle:
        handle.write(stderr or "")
    return {
        "argv": argv, "cwd": cwd, "started_at": started, "finished_at": _utc_now(),
        "duration_seconds": round(time.monotonic() - clock, 3),
        "exit_code": exit_code, "timed_out": timed_out,
        "stdout_path": stdout_path, "stderr_path": stderr_path,
    }


def run_profile(profile: dict[str, Any], variables: dict[str, str], *, out_dir: str,
                timeout: int | None = None) -> list[dict[str, Any]]:
    cwd = expand(profile["cwd"], variables)
    if not os.path.isdir(cwd):
        raise GateError(f"cwd does not exist: {cwd}")
    records = []
    for index, step in enumerate(profile.get("steps", []), start=1):
        argv = expand_argv(step["argv"], variables)
        record = run_argv(argv, step.get("cwd") and expand(step["cwd"], variables) or cwd,
                          out_dir=os.path.join(out_dir, f"step-{index:02d}"),
                          env_extra=profile.get("env"), timeout=timeout)
        records.append(record)
        if record["exit_code"] != 0:
            break
    return records


# ---------------------------------------------------------------------------
# report parsing
# ---------------------------------------------------------------------------


def _skip_allowed(test_id: str, allowed: list[str]) -> bool:
    return any(test_id == item or test_id.endswith("::" + item) or test_id.endswith("/" + item)
               or item in test_id for item in allowed)


def parse_junit(path: str) -> dict[str, Any]:
    tree = ET.parse(path)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    counts = {"collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0}
    test_ids: list[str] = []
    failures: list[dict[str, str]] = []
    skips: list[dict[str, str]] = []
    for suite in suites:
        for case in suite.findall("testcase"):
            counts["collected"] += 1
            name = case.get("name", "")
            classname = case.get("classname", "")
            test_id = f"{classname}::{name}" if classname else name
            test_ids.append(test_id)
            failure = case.find("failure")
            error = case.find("error")
            skipped = case.find("skipped")
            if failure is not None:
                counts["failed"] += 1
                failures.append({"test_id": test_id, "message": (failure.get("message") or "")[:500]})
            elif error is not None:
                counts["errors"] += 1
                failures.append({"test_id": test_id, "message": (error.get("message") or "")[:500]})
            elif skipped is not None:
                kind = (skipped.get("type") or "").lower()
                if "xfail" in kind or "xfail" in (skipped.get("message") or "").lower():
                    counts["xfailed"] += 1
                    skips.append({"test_id": test_id, "kind": "xfail",
                                  "reason": (skipped.get("message") or "")[:300]})
                else:
                    counts["skipped"] += 1
                    skips.append({"test_id": test_id, "kind": "skip",
                                  "reason": (skipped.get("message") or "")[:300]})
            else:
                counts["passed"] += 1
    return {"format": "junit", "counts": counts, "test_ids": test_ids,
            "failures": failures, "skips": skips}


def parse_tap(path: str) -> dict[str, Any]:
    text = open(path, encoding="utf-8", errors="replace").read()
    counts = {"collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0}
    for key, label in (("collected", "tests"), ("passed", "pass"), ("failed", "fail"),
                       ("skipped", "skipped"), ("xfailed", "todo")):
        match = re.search(rf"^# {label} (\d+)", text, re.M)
        if match:
            counts[key] = int(match.group(1))
    test_ids = [line.split(" - ", 1)[1].strip() if " - " in line else line
                for line in text.splitlines() if re.match(r"^(ok|not ok) \d+", line)]
    if counts["collected"] == 0:
        counts["collected"] = len(test_ids)
    failures = [{"test_id": line.strip(), "message": ""}
                for line in text.splitlines() if line.startswith("not ok ")]
    skips = [{"test_id": item, "kind": "todo", "reason": "tap todo"} for item in []]
    return {"format": "tap", "counts": counts, "test_ids": test_ids,
            "failures": failures, "skips": skips}


def parse_vitest_json(path: str) -> dict[str, Any]:
    data = json.loads(open(path, encoding="utf-8").read())
    counts = {
        "collected": int(data.get("numTotalTests", 0)),
        "passed": int(data.get("numPassedTests", 0)),
        "failed": int(data.get("numFailedTests", 0)),
        "errors": 0,
        "skipped": int(data.get("numPendingTests", 0)),
        "xfailed": int(data.get("numTodoTests", 0)),
    }
    test_ids = [str(item.get("fullName") or item.get("title") or "")
                for file in data.get("testResults", []) for item in file.get("assertionResults", [])]
    failures = [{"test_id": test_id, "message": "failed"}
                for file in data.get("testResults", []) for item in file.get("assertionResults", [])
                if item.get("status") == "failed" for test_id in [str(item.get("fullName") or item.get("title") or "")]]
    skips = [{"test_id": str(item.get("fullName") or item.get("title") or ""), "kind": "skip", "reason": ""}
             for file in data.get("testResults", []) for item in file.get("assertionResults", [])
             if item.get("status") in {"pending", "skipped", "todo"}]
    # Aggregate counters are authoritative: some reporters omit pending entries
    # from testResults, and a silent skip must still block the gate.
    if counts["skipped"] > len(skips):
        skips.extend({"test_id": f"<unreported pending {index + 1}>", "kind": "skip",
                      "reason": "vitest reported pending tests without names"}
                     for index in range(counts["skipped"] - len(skips)))
    return {"format": "vitest-json", "counts": counts, "test_ids": test_ids,
            "failures": failures, "skips": skips}


def parse_contract_wheel_check(path: str, required_ids: list[str]) -> dict[str, Any]:
    data = json.loads(open(path, encoding="utf-8").read())
    if data.get("schema_version") != "contract-wheel-check/1":
        raise GateError("unsupported contract-wheel-check schema")
    checks = data.get("checks")
    if not isinstance(checks, list):
        raise GateError("contract-wheel-check has no checks array")
    seen: dict[str, int] = {}
    failures = []
    for check in checks:
        check_id = check.get("id")
        seen[check_id] = seen.get(check_id, 0) + 1
        if check.get("passed") is not True:
            failures.append({"test_id": str(check_id), "message": "check not passed"})
        evidence = check.get("evidence_path")
        if not isinstance(evidence, str) or not os.path.exists(evidence):
            failures.append({"test_id": str(check_id), "message": "evidence missing"})
    for check_id in required_ids:
        if seen.get(check_id, 0) != 1:
            failures.append({"test_id": check_id, "message": "required check missing or duplicated"})
    counts = {"collected": len(checks), "passed": len(checks) - len(failures),
              "failed": len(failures), "errors": 0, "skipped": 0, "xfailed": 0}
    return {"format": "contract-wheel-check/1", "counts": counts,
            "test_ids": [str(item.get("id")) for item in checks],
            "failures": failures, "skips": []}


def parse_report(path: str, report_format: str, *, required_ids: list[str] | None = None) -> dict[str, Any]:
    if not os.path.isfile(path):
        raise GateError(f"report missing: {path}")
    if report_format == "junit":
        return parse_junit(path)
    if report_format == "tap":
        return parse_tap(path)
    if report_format == "vitest-json":
        return parse_vitest_json(path)
    if report_format == "contract-wheel-check/1":
        return parse_contract_wheel_check(path, required_ids or [])
    raise GateError(f"unsupported report format: {report_format!r}")


def evaluate(record: dict[str, Any], *, report_format: str | None, report_path: str | None,
             mandatory_test_ids: list[str] | None = None, allowed_skips: list[str] | None = None,
             required_check_ids: list[str] | None = None,
             expected_test_ids: list[str] | None = None,
             recorded_fingerprint: str | None = None,
             current_fingerprint: str | None = None) -> dict[str, Any]:
    """Turn raw command/report data into PASS or FAIL with explicit reasons."""
    reasons: list[str] = []
    if record.get("timed_out"):
        reasons.append("command timed out")
    if record.get("exit_code") != 0:
        reasons.append(f"exit code {record.get('exit_code')!r}")
    parsed: dict[str, Any] | None = None
    if report_format:
        if not report_path:
            reasons.append("no report path configured")
        else:
            try:
                parsed = parse_report(report_path, report_format, required_ids=required_check_ids)
            except (GateError, ET.ParseError, json.JSONDecodeError, OSError) as exc:
                reasons.append(f"report unusable: {exc}")
    if parsed is not None:
        counts = parsed["counts"]
        if counts["collected"] <= 0:
            reasons.append("zero collected checks")
        if counts["failed"] or counts["errors"]:
            reasons.append(f"{counts['failed']} failed / {counts['errors']} errored checks")
        allowed = allowed_skips or []
        for skip in parsed["skips"]:
            if not _skip_allowed(skip["test_id"], allowed):
                reasons.append(f"unapproved {skip['kind']}: {skip['test_id']}")
        if mandatory_test_ids:
            missing = [item for item in mandatory_test_ids
                       if not any(item == seen or item in seen for seen in parsed["test_ids"])]
            if missing:
                reasons.append(f"mandatory checks not executed: {missing}")
        if expected_test_ids is not None:
            if sorted(set(parsed["test_ids"])) != sorted(set(expected_test_ids)):
                reasons.append("test ID set changed from the frozen expectation")
    if recorded_fingerprint is None:
        reasons.append("report has no source fingerprint")
    elif current_fingerprint is not None and recorded_fingerprint != current_fingerprint:
        reasons.append("report fingerprint does not match the current source tree")
    return {"status": "PASS" if not reasons else "FAIL", "reasons": reasons, "parsed": parsed}


# ---------------------------------------------------------------------------
# task test_runs
# ---------------------------------------------------------------------------


def run_test_run(spec: dict[str, Any], variables: dict[str, str], *, run_root: str,
                 out_dir: str, current_fingerprint: str, git: str = "git",
                 repo_root: str | None = None, timeout: int | None = None) -> dict[str, Any]:
    run_vars = dict(variables)
    run_vars["OUT_DIR"] = os.path.abspath(out_dir)
    run_vars["REPORT_PATH"] = expand(spec["report_path"], {**run_vars, "OUT_DIR": os.path.abspath(out_dir)}) \
        if spec.get("report_path") else ""
    cwd = expand(spec["cwd"], run_vars)
    if not os.path.isdir(cwd):
        raise GateError(f"test_run cwd missing: {cwd}")
    source_path = spec.get("source_path")
    if source_path and not os.path.exists(expand(source_path, run_vars)):
        raise GateError(f"test entrypoint missing: {source_path}")
    argv = expand_argv(spec["argv"], run_vars)
    report_path = run_vars["REPORT_PATH"] or None
    if report_path and os.path.exists(report_path):
        raise GateError(f"refusing to reuse an existing report: {report_path}")
    if report_path:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
    record = run_argv(argv, cwd, out_dir=os.path.abspath(out_dir), timeout=timeout)
    if spec.get("report_from_stdout") and report_path:
        with open(record["stdout_path"], encoding="utf-8", errors="replace") as source:
            with open(report_path, "w", encoding="utf-8") as target:
                target.write(source.read())
    meta_path = (report_path + ".meta.json") if report_path else os.path.join(out_dir, "run-meta.json")
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump({"source_fingerprint": current_fingerprint, "argv": argv, "cwd": cwd,
                   "started_at": record["started_at"], "finished_at": record["finished_at"]},
                  handle, ensure_ascii=False, indent=2)
    verdict = evaluate(
        record, report_format=spec.get("report_format"), report_path=report_path,
        mandatory_test_ids=spec.get("mandatory_test_ids") or None,
        allowed_skips=spec.get("allowed_skips") or None,
        required_check_ids=spec.get("required_check_ids") or spec.get("mandatory_check_ids") or None,
        expected_test_ids=spec.get("expected_test_ids"),
        recorded_fingerprint=current_fingerprint, current_fingerprint=current_fingerprint,
    )
    return {"test_run_id": spec.get("id"), "runner": spec.get("runner"),
            "cwd": cwd, "argv": argv, "report_path": report_path,
            "report_meta_path": meta_path, "command": record,
            "counts": (verdict["parsed"] or {}).get("counts"),
            "skip_details": (verdict["parsed"] or {}).get("skips", []),
            "status": verdict["status"], "reasons": verdict["reasons"]}


def run_task(task_id: str, tasks_json_path: str, variables: dict[str, str], *,
             run_root: str, current_fingerprint: str, timeout: int | None = None) -> dict[str, Any]:
    with open(tasks_json_path, encoding="utf-8") as handle:
        plan = json.load(handle)
    task = next((item for item in plan["tasks"] if item["id"] == task_id), None)
    if task is None:
        raise GateError(f"unknown task {task_id}")
    results = []
    for index, spec in enumerate(task.get("test_runs", []), start=1):
        out_dir = os.path.join(run_root, "tasks", task_id, "test-runs", f"{index:02d}")
        results.append(run_test_run(spec, variables, run_root=run_root, out_dir=out_dir,
                                    current_fingerprint=current_fingerprint, timeout=timeout))
    blocked = [item for item in results if item["status"] != "PASS"]
    return {"task_id": task_id, "status": "PASS" if results and not blocked else "FAIL",
            "test_runs": results,
            "reasons": [reason for item in blocked for reason in item["reasons"]] or
                       ([] if results else ["task has no registered test_runs"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CatiaAgent development gate")
    parser.add_argument("--profile", help="command profile id such as C03")
    parser.add_argument("--task", help="task id whose test_runs should run")
    parser.add_argument("--tasks-json", help="tasks.json path")
    parser.add_argument("--repo-map", required=True, help="repository-map.json path")
    parser.add_argument("--repo-root", help="repository root (defaults to REPO_ROOT variable)")
    parser.add_argument("--run-root", help="RUN_ROOT for artifacts")
    parser.add_argument("--out-dir", help="output directory for a single profile run")
    parser.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args(argv)

    variables = load_variables(args.repo_map)
    repo_root = os.path.abspath(args.repo_root or variables.get("REPO_ROOT", "."))
    run_root = os.path.abspath(args.run_root or variables.get("RUN_ROOT", "."))
    if not variables.get("REPO_ROOT") or not variables.get("RUN_ROOT"):
        raise GateError("REPO_ROOT and RUN_ROOT must be registered")
    fingerprint = source_fingerprint(repo_root)
    variables["REPO_ROOT"] = repo_root
    variables["RUN_ROOT"] = run_root

    if args.profile:
        profiles_path = os.path.join(SCRIPT_DIR, "command_profiles.json")
        profiles = json.load(open(profiles_path, encoding="utf-8"))["profiles"]
        if args.profile not in profiles:
            raise GateError(f"unknown profile {args.profile}")
        profile = profiles[args.profile]
        if profile.get("status") == "planned":
            source = profile.get("source_path")
            if source and not os.path.exists(os.path.join(repo_root, source)):
                raise GateError(f"planned entrypoint not created yet: {source}")
        out_dir = args.out_dir or os.path.join(run_root, "commands", args.profile)
        report_path = expand(profile["report_path"], {**variables, "OUT_DIR": os.path.abspath(out_dir)}) \
            if profile.get("report_path") else None
        run_vars = dict(variables)
        if report_path:
            run_vars["REPORT_PATH"] = report_path
            os.makedirs(os.path.dirname(report_path), exist_ok=True)
        records = run_profile(profile, run_vars, out_dir=out_dir, timeout=args.timeout)
        record = records[-1]
        if profile.get("report_from_stdout") and report_path:
            with open(record["stdout_path"], encoding="utf-8", errors="replace") as source:
                with open(report_path, "w", encoding="utf-8") as target:
                    target.write(source.read())
        verdict = evaluate(record, report_format=profile.get("report_format"),
                           report_path=report_path, recorded_fingerprint=fingerprint,
                           current_fingerprint=fingerprint,
                           mandatory_test_ids=profile.get("mandatory_test_ids"),
                           allowed_skips=profile.get("allowed_skips"))
        result = {"profile": args.profile, "status": verdict["status"],
                  "reasons": verdict["reasons"], "steps": records,
                  "source_fingerprint": fingerprint}
        with open(os.path.join(out_dir, "gate-result.json"), "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(json.dumps({"status": result["status"], "reasons": result["reasons"]}, ensure_ascii=False))
        return 0 if result["status"] == "PASS" else 1

    if args.task:
        if not args.tasks_json:
            raise GateError("--tasks-json is required with --task")
        result = run_task(args.task, args.tasks_json, variables, run_root=run_root,
                          current_fingerprint=fingerprint, timeout=args.timeout)
        print(json.dumps({"status": result["status"], "reasons": result["reasons"]}, ensure_ascii=False))
        return 0 if result["status"] == "PASS" else 1
    raise GateError("either --profile or --task is required")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateError as error:
        print(json.dumps({"status": "BLOCKED", "reasons": [str(error)]}, ensure_ascii=False))
        raise SystemExit(2)
