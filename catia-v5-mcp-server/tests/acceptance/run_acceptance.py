"""Real-CATIA acceptance harness for the edit workflow (A01-A14, A12b).

Each case gets its own journal, source copy and working directory, so cases are
independent. The evidence is the structured result of every tool call plus
independent hash/readback assertions; a case passes only if every assertion in
it passes. Rollback/recovery claims are only accepted with multi-dimensional
readback (parameters, volume, bbox, holes) and, for A13, a journal read from a
separate Python process.

Run:  .venv/Scripts/python.exe tests/acceptance/run_acceptance.py [A01 A02 ... A12b ...]
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

FIXTURES = os.environ.get("CATIA_ACCEPTANCE_FIXTURES") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures")
)
ROOT = os.environ.get("CATIA_ACCEPTANCE_OUT") or r"F:\CatiaAgent\work\stage1\acceptance"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catia_mcp.editing import report as report_mod  # noqa: E402
from catia_mcp.editing.validation import plan_hash  # noqa: E402


def vol(L: float, W: float, T: float, D: float = 10.0, holes: int = 2) -> float:
    return round((L * W - holes * math.pi * (D / 2) ** 2) * T, 6)


# ----------------------------------------------------------------------
# baseline / journal helpers
#
# Rollback and recovery are only accepted when the live model is back at the
# recorded baseline on every dimension we can measure: parameters, volume,
# bbox and hole geometry. A single reading is never enough evidence.
# `read_model()` returns `parameters` as a list, so the baseline converts it
# to the canonical `{handle: value}` map here.
# ----------------------------------------------------------------------
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _num_close(actual: Any, expected: Any, tolerance: float = 1e-6) -> bool:
    if actual is None or expected is None:
        return actual is expected
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual == expected
    if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
        return actual == expected
    return abs(float(actual) - float(expected)) <= tolerance


def _volume_close(actual: Any, expected: Any) -> bool:
    if actual is None or expected is None:
        return actual is expected
    return abs(float(actual) - float(expected)) <= max(0.01, abs(float(expected)) * 1e-6)


def _holes_signature(holes: Any) -> Any:
    if holes is None:
        return None
    out = []
    for hole in holes:
        center = hole.get("center") or {}
        out.append(
            (
                round(float(center.get("x")), 6),
                round(float(center.get("y")), 6),
                round(float(hole.get("radius")), 6),
            )
        )
    return sorted(out)


def _baseline(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the shared baseline shape: parameters / volumeMm3 / bbox / holes."""
    geometry = snapshot.get("geometry") or {}
    bbox = geometry.get("bbox") or {}
    bbox_out = None
    if isinstance(bbox, dict) and all(group in bbox for group in ("min", "max", "dimensions")):
        bbox_out = {
            group: {axis: (bbox.get(group) or {}).get(axis) for axis in ("x", "y", "z")}
            for group in ("min", "max", "dimensions")
        }
    holes = snapshot.get("holes")
    parameters = {
        p["handle"]: p.get("value")
        for p in snapshot.get("parameters", [])
        if isinstance(p.get("handle"), str) and _is_number(p.get("value"))
    }
    return {
        "parameters": parameters,
        "volumeMm3": geometry.get("volumeMm3"),
        "bbox": bbox_out,
        "holes": [dict(h) for h in holes] if isinstance(holes, list) else None,
    }


def _baseline_complete(baseline: dict[str, Any]) -> bool:
    # The fixtures used by the rollback cases have two measurable holes; an
    # empty hole list means the readback was unavailable, not "no holes".
    holes = baseline.get("holes")
    return (
        bool(baseline.get("parameters"))
        and baseline.get("volumeMm3") is not None
        and baseline.get("bbox") is not None
        and isinstance(holes, list)
        and bool(holes)
    )


def _assert_baseline(c: "Case", prefix: str, baseline: dict[str, Any], snapshot: dict[str, Any]) -> None:
    actual = _baseline(snapshot)
    c.check(
        f"{prefix}-baseline-complete",
        _baseline_complete(baseline),
        {
            "parameterCount": len(baseline.get("parameters") or {}),
            "volumeMm3": baseline.get("volumeMm3"),
            "bbox": baseline.get("bbox"),
            "holes": _holes_signature(baseline.get("holes")),
        },
    )
    expected_params = baseline.get("parameters") or {}
    actual_params = actual.get("parameters") or {}
    param_diff = [
        {"handle": handle, "expected": expected_params.get(handle), "actual": actual_params.get(handle)}
        for handle in sorted(set(expected_params) | set(actual_params))
        if not _num_close(actual_params.get(handle), expected_params.get(handle))
    ]
    c.check(
        f"{prefix}-parameters-restored",
        bool(expected_params) and not param_diff,
        {"differences": param_diff, "parameterCount": len(expected_params)},
    )
    c.check(
        f"{prefix}-volume-restored",
        _volume_close(actual.get("volumeMm3"), baseline.get("volumeMm3")),
        {"expected": baseline.get("volumeMm3"), "actual": actual.get("volumeMm3")},
    )
    expected_bbox = baseline.get("bbox")
    actual_bbox = actual.get("bbox")
    bbox_diff: Any = {}
    if not isinstance(expected_bbox, dict) or not isinstance(actual_bbox, dict):
        bbox_diff = {"expected": expected_bbox, "actual": actual_bbox}
    else:
        for group in ("min", "max", "dimensions"):
            for axis in ("x", "y", "z"):
                want = (expected_bbox.get(group) or {}).get(axis)
                got = (actual_bbox.get(group) or {}).get(axis)
                if not _num_close(got, want):
                    bbox_diff[f"{group}.{axis}"] = {"expected": want, "actual": got}
    c.check(
        f"{prefix}-bbox-restored",
        bool(expected_bbox) and not bbox_diff,
        {"differences": bbox_diff, "expected": expected_bbox, "actual": actual_bbox},
    )
    expected_holes = _holes_signature(baseline.get("holes"))
    actual_holes = _holes_signature(actual.get("holes"))
    same_holes = (
        bool(expected_holes)
        and bool(actual_holes)
        and len(expected_holes) == len(actual_holes)
        and all(
            all(_num_close(got, want) for got, want in zip(got_hole, want_hole))
            for got_hole, want_hole in zip(actual_holes, expected_holes)
        )
    )
    c.check(
        f"{prefix}-holes-restored",
        same_holes,
        {"expected": expected_holes, "actual": actual_holes},
    )


def _live_snapshot(c: "Case", check_id: str) -> dict[str, Any]:
    """Read the bound document again and require a live (not journal-only) snapshot."""
    inspect = c.tools.execute("catia_inspect_edit", {"editSessionId": c.session})
    data = inspect.get("data") or {}
    snapshot = data.get("snapshot")
    live = (
        inspect.get("ok") is True
        and data.get("documentOpen") is True
        and isinstance(snapshot, dict)
        and bool(snapshot.get("parameters"))
    )
    c.check(
        check_id,
        live,
        {
            "code": inspect.get("code"),
            "documentOpen": data.get("documentOpen"),
            "hasSnapshot": isinstance(snapshot, dict),
        },
    )
    return snapshot if live and isinstance(snapshot, dict) else {}


def _baseline_diff(baseline: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    actual = _baseline(snapshot)
    expected_params = baseline.get("parameters") or {}
    actual_params = actual.get("parameters") or {}
    param_diff = [
        {"handle": handle, "expected": expected_params.get(handle), "actual": actual_params.get(handle)}
        for handle in sorted(set(expected_params) | set(actual_params))
        if not _num_close(actual_params.get(handle), expected_params.get(handle))
    ]
    volume_changed = not _volume_close(actual.get("volumeMm3"), baseline.get("volumeMm3"))
    return {
        "parameters": param_diff,
        "volumeChanged": volume_changed,
        "volumeBefore": baseline.get("volumeMm3"),
        "volumeNow": actual.get("volumeMm3"),
    }


_JOURNAL_PROBE = r"""
import json
import sys

from catia_mcp.editing.journal import Journal

journal_path, session_id, operation_id = sys.argv[1], sys.argv[2], sys.argv[3]
journal = Journal(journal_path)
session = journal.load_session(session_id)
operation = journal.get_operation(operation_id, session_id=session_id)
print(json.dumps({
    "session": None if session is None else {
        "status": session.get("status"),
        "modelRevision": session.get("modelRevision"),
    },
    "operation": None if operation is None else {
        "operationId": operation.get("operation_id"),
        "sessionId": operation.get("session_id"),
        "state": operation.get("state"),
    },
}, ensure_ascii=False))
"""


def _probe_journal(journal_path: str, session_id: str, operation_id: str) -> dict[str, Any]:
    """Read the journal from a brand-new Python process (durability evidence)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _JOURNAL_PROBE, journal_path, session_id, operation_id],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 - a failed probe is failed evidence
        return {
            "returncode": None,
            "payload": None,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }
    payload = None
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        pass
    return {
        "returncode": proc.returncode,
        "payload": payload,
        "stdout": proc.stdout.strip()[-1000:],
        "stderr": proc.stderr.strip()[-1000:],
    }


def _source_line(markdown: str) -> str:
    markers = ("未变", "未修改", "未改动", "unchanged")
    for line in markdown.splitlines():
        if "源文件" in line and any(marker in line for marker in markers):
            return line
    return ""


def _source_claim_ok(line: str, measured: Any) -> bool:
    """The Markdown claim must match the measured value, and must not default."""
    if not line:
        return False
    if measured is True:
        return "是" in line and "否" not in line
    if measured is False:
        return "否" in line and "是" not in line
    return "是" not in line and "否" not in line


def _report_intent(report: dict[str, Any]) -> Any:
    request = report.get("request")
    if isinstance(request, dict) and request.get("intent") is not None:
        return request.get("intent")
    return report.get("intent")


def _intent_present(intent: Any) -> bool:
    """A placeholder {text: None, resolved: None} is not a recorded intent."""
    if isinstance(intent, dict):
        return bool(intent.get("text") or intent.get("resolved"))
    return bool(intent)


def _report_tolerances(report: dict[str, Any]) -> Any:
    for container in (report, report.get("request")):
        if not isinstance(container, dict):
            continue
        for key in ("tolerance", "tolerances"):
            if container.get(key) not in (None, {}, []):
                return container.get(key)
    return None


def _report_recovery_history(report: dict[str, Any]) -> Any:
    if report.get("recoveryHistory") is not None:
        return report.get("recoveryHistory")
    request = report.get("request")
    if isinstance(request, dict):
        return request.get("recoveryHistory")
    return None


def _report_source_unchanged(report: dict[str, Any]) -> Any:
    if "sourceUnchanged" in report:
        return report.get("sourceUnchanged")
    verification = report.get("verification")
    if isinstance(verification, dict):
        return verification.get("sourceUnchanged")
    return None


def _render_markdown_without_source_evidence(report: dict[str, Any]) -> dict[str, Any]:
    """Render the sheet after removing the source-unchanged evidence.

    The report must not fall back to claiming "是" (unchanged) when the evidence
    is absent. If the renderer refuses to produce a claim at all, that is also
    acceptable for this specific property; a rendered source line that still
    says 是 is not.
    """
    clone = json.loads(json.dumps(report, ensure_ascii=False, default=str))
    evidence_keys = ("sourceUnchanged", "sourceEvidence", "sourceSha256Before", "sourceSha256After")
    for key in evidence_keys:
        clone.pop(key, None)
    for container in (clone.get("verification"), clone.get("request"), clone.get("delivery")):
        if isinstance(container, dict):
            for key in evidence_keys:
                container.pop(key, None)
    verification = clone.get("verification")
    if isinstance(verification, dict) and isinstance(verification.get("checks"), list):
        verification["checks"] = [
            check
            for check in verification["checks"]
            if not (isinstance(check, dict) and check.get("id") == "source-unchanged")
        ]
    renderer = getattr(report_mod, "_markdown", None)
    if not callable(renderer):
        return {
            "rendered": False,
            "line": "",
            "ok": False,
            "error": "report module has no _markdown renderer to test",
        }
    try:
        markdown = renderer(clone)
    except Exception as exc:  # noqa: BLE001 - refusing to invent a value is acceptable
        return {"rendered": False, "line": "", "ok": True, "error": f"{type(exc).__name__}: {exc}"}
    line = _source_line(markdown)
    return {"rendered": True, "line": line, "ok": _source_claim_ok(line, None), "error": None}


class Case:
    def __init__(self, harness: "Harness", case_id: str) -> None:
        self.h = harness
        self.case_id = case_id
        self.dir = os.path.join(ROOT, case_id)
        os.makedirs(self.dir, exist_ok=True)
        self.checks: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.journal_path = os.path.join(self.dir, "journal.sqlite3")
        self.journal = self.h.new_journal(self.journal_path)
        self.tools = self.h.tools_with(self.journal)
        self.session: str | None = None
        self.snapshot: dict[str, Any] = {}

    # -- assertions ---------------------------------------------------
    def check(self, name: str, ok: bool, evidence: Any = None) -> bool:
        self.checks.append({"id": name, "status": "pass" if ok else "fail", "evidence": evidence})
        return bool(ok)

    def eq(self, name: str, actual: Any, expected: Any, tolerance: float = 1e-6) -> bool:
        ok = (
            actual is not None
            and expected is not None
            and abs(float(actual) - float(expected)) <= tolerance
        )
        return self.check(name, ok, {"expected": expected, "actual": actual})

    def note(self, text: str) -> None:
        self.notes.append(text)

    # -- source / session ---------------------------------------------
    def source(self, fixture_name: str, *, alias: str | None = None) -> str:
        src_dir = os.path.join(self.dir, "src", alias or "main")
        os.makedirs(src_dir, exist_ok=True)
        path = os.path.join(src_dir, os.path.basename(fixture_name))
        # Always refresh: a cached copy from an older fixture build would make the
        # case test the wrong model.
        shutil.copy2(os.path.join(FIXTURES, fixture_name), path)
        return path

    def begin(self, source_path: str, *, working: str | None = None) -> dict[str, Any]:
        result = self.tools.execute(
            "catia_begin_edit",
            {
                "sourcePath": source_path,
                "workingDir": working or os.path.join(self.dir, "edits", uuid.uuid4().hex[:8]),
            },
        )
        if result.get("ok") and result["data"].get("snapshot"):
            self.session = result["data"]["editSessionId"]
            self.snapshot = result["data"]["snapshot"]
        return result

    def handles(self) -> dict[str, str]:
        out = {}
        for param in self.snapshot.get("parameters", []):
            if param.get("kind") == "driver":
                out[param["handle"].split("\\")[-1]] = param["handle"]
        return out

    def value(self, name: str) -> float | None:
        handle = self.handles().get(name)
        for param in self.snapshot.get("parameters", []):
            if param["handle"] == handle:
                return float(param["value"])
        return None

    def plan(
        self,
        changes: list[tuple[str, float, float]],
        *,
        preserve: list[dict[str, Any]] | None = None,
        acceptance: list[dict[str, Any]] | None = None,
        operation: str | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        handles = self.handles()
        return {
            "schema": "catia-edit-plan/1",
            "editSessionId": self.session,
            "operationId": operation or f"{self.case_id}-{uuid.uuid4().hex[:6]}",
            "expectedRevision": self.snapshot.get("modelRevision")
            if revision is None
            else revision,
            "intent": {"text": self.case_id, "resolved": "acceptance fixture"},
            "changes": [
                {
                    "parameter": handles[name],
                    "oldValue": float(old),
                    "newValue": float(new),
                    "unit": "mm",
                    "reason": self.case_id,
                }
                for name, old, new in changes
            ],
            "preserve": preserve or [],
            "acceptance": acceptance or [],
        }

    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        result = self.tools.execute(
            "catia_apply_edit_plan", {"editSessionId": self.session, "plan": plan}
        )
        self.refresh_snapshot()
        return result

    def refresh_snapshot(self) -> None:
        inspect = self.tools.execute("catia_inspect_edit", {"editSessionId": self.session})
        if inspect.get("ok") and inspect["data"].get("snapshot"):
            self.snapshot = inspect["data"]["snapshot"]

    def finalize(self, name: str) -> dict[str, Any]:
        path = os.path.join(self.dir, "deliveries", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            os.remove(path)
        return self.tools.execute(
            "catia_finalize_edit", {"editSessionId": self.session, "deliveryPath": path}
        )

    def finish(self) -> dict[str, Any]:
        passed = all(c["status"] == "pass" for c in self.checks)
        return {
            "case": self.case_id,
            "passed": passed,
            "checks": self.checks,
            "notes": self.notes,
            "session": self.session,
        }


class Harness:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.tools_cache: Any = None
        os.makedirs(ROOT, exist_ok=True)

    def new_journal(self, path: str):
        from catia_mcp.editing.journal import Journal

        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
        return Journal(path)

    def tools_with(self, journal):
        from catia_mcp.tools.editing import EditingTools
        from catia_mcp.connection import CATIAConnection

        conn = CATIAConnection()
        conn.app = self.app
        conn._initialized_com = True
        return EditingTools(conn, journal)

    def close_docs_under_root(self) -> None:
        app = self.app
        docs = app.Documents
        app.DisplayFileAlerts = False
        # Close both the case work tree and any source fixture left open by a
        # fixture build. A source document that stays open makes Documents.NewFrom
        # pop the modal "新建自" dialog and blocks the COM call.
        roots = (ROOT.lower(), FIXTURES.lower())
        for i in range(docs.Count, 0, -1):
            try:
                name = str(docs.Item(i).FullName)
            except Exception:
                continue
            if any(name.lower().startswith(root) for root in roots):
                try:
                    docs.Item(i).Close()
                except Exception:
                    pass
        app.DisplayFileAlerts = True


# ----------------------------------------------------------------------
# cases
# ----------------------------------------------------------------------
def case_a01(h: Harness) -> dict[str, Any]:
    c = Case(h, "A01")
    source = c.source("FixtureA-mounting-plate.CATPart")
    source_sha = _sha(source)
    begin = c.begin(source)
    c.check("begin", begin.get("ok") is True, begin.get("code"))
    if not begin.get("ok"):
        return c.finish()
    c.eq("initial-volume", c.snapshot["geometry"]["volumeMm3"], vol(160, 100, 12))
    plan = c.plan(
        [("L", 160, 200)],
        preserve=[{"type": "holes_edge_distance"}],
        acceptance=[
            {"type": "bbox_range", "axis": "x", "min": -100, "max": 100},
            {"type": "bbox_dimension", "axis": "y", "expected": 100},
            {"type": "bbox_dimension", "axis": "z", "expected": 12},
            {"type": "volume", "expected": vol(200, 100, 12)},
            {"type": "hole_positions", "holes": [{"x": -85, "y": 35}, {"x": 85, "y": 35}]},
            {"type": "hole_radius", "expected": 5},
            {"type": "feature_count", "expected": 1},
        ],
    )
    applied = c.apply(plan)
    c.check("apply-ok", applied.get("ok") is True, {"code": applied.get("code")})
    c.check("verdict-PASS", applied.get("data", {}).get("verdict") == "PASS", applied.get("data", {}).get("verdict"))
    after = applied.get("data", {}).get("after", {})
    bbox = (after.get("geometry") or {}).get("bbox") or {}
    c.eq("x-min", (bbox.get("min") or {}).get("x"), -100)
    c.eq("x-max", (bbox.get("max") or {}).get("x"), 100)
    c.eq("volume", (after.get("geometry") or {}).get("volumeMm3"), vol(200, 100, 12))
    holes = [(round(hh["center"]["x"], 6), round(hh["center"]["y"], 6)) for hh in after.get("holes", [])]
    c.check("holes", holes == [(-85.0, 35.0), (85.0, 35.0)], holes)
    c.eq("hole-radius", after["holes"][0]["radius"], 5.0)
    c.check("no-extra-features", c.snapshot["part"]["featureCount"] == 1, c.snapshot["part"]["featureCount"])
    fin = c.finalize("A01-L200.CATPart")
    c.check("finalize-ok", fin.get("ok") is True, fin.get("code"))
    c.check(
        "reopen-verified",
        (fin.get("data", {}).get("verification") or {}).get("passed") is True,
        (fin.get("data", {}).get("verification") or {}).get("checks"),
    )
    c.check("source-untouched", _sha(source) == source_sha == c.snapshot["source"]["sha256"], {"sha": source_sha})
    return c.finish()


def case_a02_a03_a04(h: Harness) -> dict[str, Any]:
    c = Case(h, "A02-A04")
    begin = c.begin(c.source("FixtureA-mounting-plate.CATPart"))
    c.check("begin", begin.get("ok") is True, begin.get("code"))
    if not begin.get("ok"):
        return c.finish()
    # A02: "左右各延长 50，中心不动" resolved to L = 160 + 2*50 = 260
    applied = c.apply(
        c.plan(
            [("L", 160, 260)],
            preserve=[{"type": "holes_edge_distance"}],
            acceptance=[
                {"type": "bbox_range", "axis": "x", "min": -130, "max": 130},
                {"type": "volume", "expected": vol(260, 100, 12)},
                {"type": "hole_positions", "holes": [{"x": -115, "y": 35}, {"x": 115, "y": 35}]},
            ],
            operation="a02-extend-50-each-side",
        )
    )
    c.check("A02-PASS", applied.get("data", {}).get("verdict") == "PASS", {
        "code": applied.get("code"),
        "verdict": applied.get("data", {}).get("verdict"),
        "failed": [x for x in (applied.get("data") or {}).get("checks", []) if x["status"] != "pass"],
    })
    bbox = (((applied.get("data") or {}).get("after") or {}).get("geometry") or {}).get("bbox") or {}
    c.eq("A02-x-min", (bbox.get("min") or {}).get("x"), -130)
    c.eq("A02-x-max", (bbox.get("max") or {}).get("x"), 130)
    c.eq("A02-volume", (((applied.get("data") or {}).get("after") or {}).get("geometry") or {}).get("volumeMm3"), vol(260, 100, 12))
    c.check(
        "A02-holes",
        [(round(x["center"]["x"], 6), round(x["center"]["y"], 6)) for x in ((applied.get("data") or {}).get("after") or {}).get("holes", [])]
        == [(-115.0, 35.0), (115.0, 35.0)],
        ((applied.get("data") or {}).get("after") or {}).get("holes"),
    )
    # A03: width 120, holes keep 15 mm to the top edge
    applied = c.apply(
        c.plan(
            [("W", 100, 120)],
            preserve=[{"type": "holes_edge_distance"}],
            acceptance=[
                {"type": "bbox_range", "axis": "y", "min": -60, "max": 60},
                {"type": "volume", "expected": vol(260, 120, 12)},
                {"type": "hole_positions", "holes": [{"x": -115, "y": 45}, {"x": 115, "y": 45}]},
                {"type": "unchanged_dimension", "parameter": c.handles()["L"], "expected": 260},
            ],
            operation="a03-width-120",
        )
    )
    c.check("A03-PASS", applied.get("data", {}).get("verdict") == "PASS", {
        "code": applied.get("code"),
        "verdict": applied.get("data", {}).get("verdict"),
        "failed": [x for x in (applied.get("data") or {}).get("checks", []) if x["status"] != "pass"],
    })
    bbox = (((applied.get("data") or {}).get("after") or {}).get("geometry") or {}).get("bbox") or {}
    c.eq("A03-y-min", (bbox.get("min") or {}).get("y"), -60)
    c.eq("A03-y-max", (bbox.get("max") or {}).get("y"), 60)
    c.eq("A03-volume", (((applied.get("data") or {}).get("after") or {}).get("geometry") or {}).get("volumeMm3"), vol(260, 120, 12))
    c.check(
        "A03-holes",
        [(round(x["center"]["x"], 6), round(x["center"]["y"], 6)) for x in ((applied.get("data") or {}).get("after") or {}).get("holes", [])]
        == [(-115.0, 45.0), (115.0, 45.0)],
        ((applied.get("data") or {}).get("after") or {}).get("holes"),
    )
    if not (applied.get("ok") and c.checks[-1]["status"] == "pass"):
        return c.finish()
    # A04: thickness 15, holes still through
    applied = c.apply(
        c.plan(
            [("T", 12, 15)],
            preserve=[{"type": "holes_edge_distance"}],
            acceptance=[
                {"type": "bbox_range", "axis": "z", "min": 0, "max": 15},
                {"type": "volume", "expected": vol(260, 120, 15)},
                {"type": "hole_positions", "holes": [{"x": -115, "y": 45}, {"x": 115, "y": 45}]},
            ],
            operation="a04-thickness-15",
        )
    )
    c.check("A04-PASS", applied.get("data", {}).get("verdict") == "PASS", {
        "code": applied.get("code"),
        "verdict": applied.get("data", {}).get("verdict"),
        "failed": [x for x in (applied.get("data") or {}).get("checks", []) if x["status"] != "pass"],
        "message": (applied.get("message") or "")[:300],
    })
    bbox = (((applied.get("data") or {}).get("after") or {}).get("geometry") or {}).get("bbox") or {}
    c.eq("A04-z-min", (bbox.get("min") or {}).get("z"), 0)
    c.eq("A04-z-max", (bbox.get("max") or {}).get("z"), 15)
    c.eq("A04-volume", (((applied.get("data") or {}).get("after") or {}).get("geometry") or {}).get("volumeMm3"), vol(260, 120, 15))
    if not applied.get("ok"):
        return c.finish()
    fin = c.finalize("A04-chain.CATPart")
    c.check("finalize-ok", fin.get("ok") is True, fin.get("code"))
    c.check(
        "reopen-verified",
        (fin.get("data", {}).get("verification") or {}).get("passed") is True,
        (fin.get("data", {}).get("verification") or {}).get("checks"),
    )
    c.eq("final-volume", c.snapshot["geometry"]["volumeMm3"], vol(260, 120, 15))
    return c.finish()


def case_a05(h: Harness) -> dict[str, Any]:
    c = Case(h, "A05")
    begin = c.begin(c.source("FixtureA-mounting-plate.CATPart"))
    c.check("begin", begin.get("ok") is True, begin.get("code"))
    if not begin.get("ok"):
        return c.finish()
    applied = c.apply(
        c.plan(
            [("D", 10, 12)],
            acceptance=[
                {"type": "hole_radius", "expected": 6},
                {"type": "hole_positions", "holes": [{"x": -65, "y": 35}, {"x": 65, "y": 35}]},
                {"type": "volume", "expected": vol(160, 100, 12, D=12)},
                {"type": "feature_count", "expected": 1},
            ],
            operation="a05-hole-diameter-12",
        )
    )
    c.check("A05-PASS", applied.get("data", {}).get("verdict") == "PASS", applied.get("data", {}).get("verdict"))
    after = applied.get("data", {}).get("after") or {}
    c.eq("A05-volume", after["geometry"]["volumeMm3"], vol(160, 100, 12, D=12))
    c.check("A05-two-holes", len(after["holes"]) == 2, len(after["holes"]))
    c.check("A05-radius", all(abs(x["radius"] - 6) < 1e-6 for x in after["holes"]), after["holes"])
    c.check("A05-no-new-hole", c.snapshot["part"]["featureCount"] == 1, c.snapshot["part"]["featureCount"])
    return c.finish()


def case_a06_a07(h: Harness) -> dict[str, Any]:
    """A06/A07.

    The real fixtures (FixtureD-ambiguous / FixtureE-noparam) are used when
    present. When they are absent this case keeps the refusal probes but names
    the checks after what they actually prove, so a green run cannot be read as
    "A06/A07 semantics covered".
    """
    c = Case(h, "A06-A07")
    fixture_d = os.path.join(FIXTURES, "FixtureD-ambiguous.CATPart")
    fixture_e = os.path.join(FIXTURES, "FixtureE-noparam.CATPart")
    has_d = os.path.exists(fixture_d)
    has_e = os.path.exists(fixture_e)
    if not has_d:
        c.note(
            "A06: tests/fixtures/FixtureD-ambiguous.CATPart 不存在，未覆盖真实场景"
            "（方向/参数歧义）；本用例只证明编造的 handle 在写入前被拒绝"
            "（A06-unknown-handle-refused），不代表 A06 语义通过。"
        )
    if not has_e:
        c.note(
            "A07: tests/fixtures/FixtureE-noparam.CATPart 不存在，未覆盖真实场景"
            "（非参数化实体：无任何可写驱动参数）；本用例只验证负厚度/错误单位/扩展名拒绝"
            "（A07-extension-refused 等），不代表 A07 的 UNSUPPORTED_CAPABILITY 语义通过。"
        )

    # --- A06 real fixture: ambiguous handles stay distinguishable ---------
    if has_d:
        d_begin = c.begin(c.source("FixtureD-ambiguous.CATPart"))
        c.check("A06-real-begin", d_begin.get("ok") is True, d_begin.get("code"))
        if d_begin.get("ok"):
            driver_handles = [
                p["handle"] for p in c.snapshot.get("parameters", []) if p.get("kind") == "driver"
            ]
            tails: dict[str, list[str]] = {}
            for handle in driver_handles:
                tails.setdefault(handle.split("\\")[-1], []).append(handle)
            ambiguous = {tail: hs for tail, hs in tails.items() if len(hs) > 1}
            c.check(
                "A06-real-ambiguous-handles-visible",
                bool(driver_handles) and len(set(driver_handles)) == len(driver_handles),
                {
                    "ambiguousTails": ambiguous,
                    "driverCount": len(driver_handles),
                    "uniqueHandles": len(set(driver_handles)),
                },
            )
            base_volume = c.snapshot["geometry"]["volumeMm3"]
            base_revision = c.snapshot["modelRevision"]
            # Only a short name that is not itself a full handle can prove the
            # "ambiguous token" case; otherwise use a token that cannot exist.
            short_tails = [tail for tail in ambiguous if tail not in driver_handles]
            if short_tails:
                ambiguous_tail = sorted(short_tails)[0]
            else:
                ambiguous_tail = "AcceptanceAmbiguousLength"
            ambiguous_plan = {
                "schema": "catia-edit-plan/1",
                "editSessionId": c.session,
                "operationId": "a06-ambiguous-handle",
                "expectedRevision": base_revision,
                "intent": {"text": "A06 ambiguous handle", "resolved": ambiguous_tail},
                "changes": [
                    {
                        "parameter": ambiguous_tail,
                        "oldValue": 0.0,
                        "newValue": 1.0,
                        "unit": "mm",
                        "reason": "A06 real fixture",
                    }
                ],
            }
            ambiguous_rejected = c.apply(ambiguous_plan)
            c.check(
                "A06-real-ambiguous-handle-refused",
                ambiguous_rejected.get("ok") is False
                and ambiguous_rejected.get("code") == "PARAM_UNKNOWN",
                {"code": ambiguous_rejected.get("code"), "handle": ambiguous_tail},
            )
            c.refresh_snapshot()
            c.eq("A06-real-zero-write", c.snapshot["geometry"]["volumeMm3"], base_volume)
            c.eq("A06-real-revision-unchanged", c.snapshot["modelRevision"], base_revision)

    # --- fallback / A07 probes on fixture A --------------------------------
    begin = c.begin(c.source("FixtureA-mounting-plate.CATPart"))
    c.check("begin", begin.get("ok") is True, begin.get("code"))
    if not begin.get("ok"):
        return c.finish()
    before_volume = c.snapshot["geometry"]["volumeMm3"]
    # A06 fallback: no axis mapping -> the caller resolved nothing; an invented handle must be refused.
    plan = c.plan([("L", 160, 200)])
    plan["changes"][0]["parameter"] = "Part1\\AxisLength"
    rejected = c.apply(plan)
    c.check(
        "A06-unknown-handle-refused",
        rejected.get("ok") is False and rejected.get("code") == "PARAM_UNKNOWN",
        rejected.get("code"),
    )
    c.refresh_snapshot()
    c.eq("A06-zero-write", c.snapshot["geometry"]["volumeMm3"], before_volume)
    c.eq("A06-revision-unchanged", c.snapshot["modelRevision"], 0)
    # A07a: negative thickness
    neg = c.apply(c.plan([("T", 12, -5)]))
    c.check("A07-negative-refused", neg.get("ok") is False and neg.get("code") == "OUT_OF_RANGE", neg.get("code"))
    # A07b: wrong unit
    wrong_unit = c.plan([("L", 160, 200)])
    wrong_unit["changes"][0]["unit"] = "deg"
    resp = c.apply(wrong_unit)
    c.check(
        "A07-unit-refused",
        resp.get("ok") is False and resp.get("code") == "UNIT_INCOMPATIBLE",
        resp.get("code"),
    )
    # A07c: only the extension is checked here (a CATPart payload under a
    # .CATProduct name); this is NOT the non-parametric-entity scenario.
    bogus = os.path.join(c.dir, "src", "bogus", "part.CATProduct")
    os.makedirs(os.path.dirname(bogus), exist_ok=True)
    shutil.copy2(c.source("FixtureA-mounting-plate.CATPart"), bogus)
    unsupported = c.tools.execute("catia_begin_edit", {"sourcePath": bogus})
    c.check(
        "A07-extension-refused",
        unsupported.get("ok") is False and unsupported.get("code") == "UNSUPPORTED_CAPABILITY",
        {"code": unsupported.get("code"), "path": bogus},
    )
    # A07 real fixture: begin_edit must refuse a non-parametric imported entity.
    if has_e:
        imported = c.source("FixtureE-noparam.CATPart")
        imported_sha = _sha(imported)
        imported_begin = c.tools.execute("catia_begin_edit", {"sourcePath": imported})
        c.check(
            "A07-real-nonparametric-refused",
            imported_begin.get("ok") is False
            and imported_begin.get("code") == "UNSUPPORTED_CAPABILITY",
            {"code": imported_begin.get("code"), "path": imported},
        )
        c.check(
            "A07-real-source-untouched",
            _sha(imported) == imported_sha,
            {"before": imported_sha, "after": _sha(imported)},
        )
    c.refresh_snapshot()
    c.eq("A07-zero-write", c.snapshot["geometry"]["volumeMm3"], before_volume)
    return c.finish()


def case_a08_a09a(h: Harness) -> dict[str, Any]:
    c = Case(h, "A08-A09a")
    begin = c.begin(c.source("FixtureB-fixed-holes.CATPart"))
    c.check("begin", begin.get("ok") is True, begin.get("code"))
    if not begin.get("ok"):
        return c.finish()
    c.check(
        "B-invariant-unsupported",
        c.snapshot["capabilities"]["invariants"]["holes_edge_distance"]["supported"] is False,
        c.snapshot["capabilities"]["invariants"],
    )
    before_volume = c.snapshot["geometry"]["volumeMm3"]
    rejected = c.apply(
        c.plan([("W", 100, 120)], preserve=[{"type": "holes_edge_distance"}], operation="a08-reject")
    )
    c.check(
        "A08-rejected-before-write",
        rejected.get("ok") is False and rejected.get("code") == "INVARIANT_UNSUPPORTED",
        {"code": rejected.get("code"), "problems": rejected.get("data", {}).get("problems")},
    )
    c.refresh_snapshot()
    c.eq("A08-zero-write", c.snapshot["geometry"]["volumeMm3"], before_volume)
    c.eq("A08-revision-unchanged", c.snapshot["modelRevision"], 0)
    # A09a: a parameter that drives nothing must not PASS
    unused = c.value("UnusedParam")
    applied = c.apply(c.plan([("UnusedParam", float(unused), 50)], operation="a09-nonsemantic"))
    c.check(
        "A09-NO_EFFECT",
        applied.get("ok") is False and applied.get("data", {}).get("verdict") == "NO_EFFECT",
        {"code": applied.get("code"), "verdict": applied.get("data", {}).get("verdict")},
    )
    return c.finish()


def case_a09bc(h: Harness) -> dict[str, Any]:
    c = Case(h, "A09bc")
    begin = c.begin(c.source("FixtureC-no-holes.CATPart"))
    c.check("begin", begin.get("ok") is True, begin.get("code"))
    if not begin.get("ok"):
        return c.finish()
    c.check("no-holes-detected", begin["data"]["snapshot"]["holes"] == [], begin["data"]["snapshot"]["holes"])
    applied = c.apply(
        c.plan(
            [("L", 160, 200)],
            acceptance=[{"type": "hole_positions", "holes": [{"x": -85, "y": 35}]}],
            operation="a09-inconclusive",
        )
    )
    c.check(
        "A09-INCONCLUSIVE",
        applied.get("ok") is False and applied.get("data", {}).get("verdict") == "INCONCLUSIVE",
        {"code": applied.get("code"), "verdict": applied.get("data", {}).get("verdict")},
    )
    # A09c: the same target resent (new operationId, stale oldValue) must be ALREADY_SATISFIED
    again = c.apply(c.plan([("L", 160, 200)], operation="a09-already-satisfied"))
    c.check(
        "A09-ALREADY_SATISFIED",
        again.get("ok") is True and again.get("data", {}).get("verdict") == "ALREADY_SATISFIED",
        {"code": again.get("code"), "verdict": again.get("data", {}).get("verdict")},
    )
    c.eq("A09c-no-extra-change", c.snapshot["geometry"]["volumeMm3"], vol(200, 100, 12, holes=0))
    return c.finish()


def case_a10(h: Harness) -> dict[str, Any]:
    c = Case(h, "A10")
    source = c.source("FixtureA-mounting-plate.CATPart")
    begin = c.begin(source)
    c.check("begin", begin.get("ok") is True, begin.get("code"))
    if not begin.get("ok"):
        return c.finish()
    # active-document switch: another document becomes active before the write
    app = h.app
    other = app.Documents.Add("Part")
    other.Part.Update()
    other_state = (other.Part.MainBody.Shapes.Count, bool(other.Saved))
    c.note(f"other document '{other.Name}' active with state {other_state}")
    applied = c.apply(c.plan([("L", 160, 200)], operation="a10-switch-active-doc"))
    c.check("A10-PASS", applied.get("data", {}).get("verdict") == "PASS", applied.get("data", {}).get("verdict"))
    c.eq("A10-target-changed", c.snapshot["geometry"]["volumeMm3"], vol(200, 100, 12))
    c.check(
        "A10-other-doc-untouched",
        (other.Part.MainBody.Shapes.Count, bool(other.Saved)) == other_state,
        {"before": other_state, "after": (other.Part.MainBody.Shapes.Count, bool(other.Saved))},
    )
    other.Close()
    # stale revision
    stale = c.plan([("W", 100, 120)], revision=0, operation="a10-stale-revision")
    resp = c.apply(stale)
    c.check("A10-stale-revision", resp.get("ok") is False and resp.get("code") == "STALE_REVISION", resp.get("code"))
    # same source, second begin: must reuse the bound session instead of a second writer
    second = c.tools.execute("catia_begin_edit", {"sourcePath": source})
    c.check(
        "A10-single-writer",
        second.get("ok") is True
        and second.get("code") == "ALREADY_BOUND"
        and second["data"]["editSessionId"] == c.session,
        {"code": second.get("code"), "session": (second.get("data") or {}).get("editSessionId")},
    )
    # same base name in two directories: two independent sessions, no cross-talk
    source_a = c.source("FixtureA-mounting-plate.CATPart", alias="dirA")
    source_b = c.source("FixtureA-mounting-plate.CATPart", alias="dirB")
    begin_a = c.tools.execute("catia_begin_edit", {"sourcePath": source_a, "workingDir": os.path.join(c.dir, "edits", "dupA")})
    begin_b = c.tools.execute("catia_begin_edit", {"sourcePath": source_b, "workingDir": os.path.join(c.dir, "edits", "dupB")})
    ok_begin = begin_a.get("ok") and begin_b.get("ok") and begin_a["data"]["editSessionId"] != begin_b["data"]["editSessionId"]
    c.check("A10-two-same-named-files", ok_begin, {
        "a": (begin_a.get("data") or {}).get("editSessionId"),
        "b": (begin_b.get("data") or {}).get("editSessionId"),
    })
    if ok_begin:
        plan_a = _plan_for(begin_a["data"], 200.0, "dup-a")
        plan_b = _plan_for(begin_b["data"], 220.0, "dup-b")
        res_a = c.tools.execute("catia_apply_edit_plan", {"editSessionId": begin_a["data"]["editSessionId"], "plan": plan_a})
        res_b = c.tools.execute("catia_apply_edit_plan", {"editSessionId": begin_b["data"]["editSessionId"], "plan": plan_b})
        c.check("A10-dupA-ok", res_a.get("ok") is True, res_a.get("code"))
        c.check("A10-dupB-ok", res_b.get("ok") is True, res_b.get("code"))
        c.eq("A10-dupA-length", ((res_a.get("data") or {}).get("after") or {}).get("geometry", {}).get("bbox", {}).get("dimensions", {}).get("x"), 200)
        c.eq("A10-dupB-length", ((res_b.get("data") or {}).get("after") or {}).get("geometry", {}).get("bbox", {}).get("dimensions", {}).get("x"), 220)
    c.check("A10-source-untouched", _sha(source) == c.snapshot["source"]["sha256"], {})
    return c.finish()


def _plan_for(begin_data: dict[str, Any], new_length: float, operation: str) -> dict[str, Any]:
    snapshot = begin_data["snapshot"]
    handle = next(p["handle"] for p in snapshot["parameters"] if p["handle"].endswith("\\L"))
    return {
        "schema": "catia-edit-plan/1",
        "editSessionId": begin_data["editSessionId"],
        "operationId": operation,
        "expectedRevision": begin_data["modelRevision"],
        "changes": [{"parameter": handle, "oldValue": 160.0, "newValue": new_length, "unit": "mm"}],
    }


def case_a11(h: Harness) -> dict[str, Any]:
    c = Case(h, "A11")
    begin = c.begin(c.source("FixtureA-mounting-plate.CATPart"))
    c.check("begin", begin.get("ok") is True, begin.get("code"))
    if not begin.get("ok"):
        return c.finish()
    plan = c.plan([("L", 160, 200)], operation="a11-dup")
    first = c.apply(plan)
    c.check("A11-first-ok", first.get("ok") is True, first.get("code"))
    volume_after_first = c.snapshot["geometry"]["volumeMm3"]
    revision_after_first = c.snapshot["modelRevision"]
    replay = c.apply(plan)
    c.check("A11-replay-ok", replay.get("ok") is True, replay.get("code"))
    c.check(
        "A11-replay-flagged",
        (replay.get("data") or {}).get("idempotentReplay") is True,
        (replay.get("data") or {}).get("idempotentReplay"),
    )
    c.refresh_snapshot()
    c.eq("A11-no-double-apply", c.snapshot["geometry"]["volumeMm3"], volume_after_first)
    c.eq("A11-revision-unchanged", c.snapshot["modelRevision"], revision_after_first)
    conflict = dict(plan)
    conflict["changes"] = [dict(plan["changes"][0], newValue=210.0)]
    resp = c.apply(conflict)
    c.check(
        "A11-conflict",
        resp.get("ok") is False and resp.get("code") == "OPERATION_CONFLICT",
        resp.get("code"),
    )
    return c.finish()


def case_a12(h: Harness) -> dict[str, Any]:
    c = Case(h, "A12")
    begin = c.begin(c.source("FixtureA-mounting-plate.CATPart"))
    c.check("begin", begin.get("ok") is True, begin.get("code"))
    if not begin.get("ok"):
        return c.finish()
    before_volume = c.snapshot["geometry"]["volumeMm3"]
    # (1) a semantically broken model must never PASS: E=200 pushes the holes
    # outside the plate while the part stays a valid solid.
    outside = c.apply(c.plan([("E", 15, 200)], operation="a12-hole-outside"))
    c.check(
        "A12-hole-outside-not-pass",
        outside.get("ok") is False and outside.get("data", {}).get("verdict") == "WRONG_EFFECT",
        {"code": outside.get("code"), "verdict": outside.get("data", {}).get("verdict"),
         "checks": [c for c in (outside.get("data") or {}).get("checks", []) if c["status"] != "pass"][:3]},
    )
    back = c.apply(c.plan([("E", 200, 15)], operation="a12-hole-back"))
    c.check("A12-hole-restored", back.get("ok") is True, back.get("code"))
    c.eq("A12-hole-restored-volume", c.snapshot["geometry"]["volumeMm3"], before_volume)
    # The rollback baseline is the model exactly as it is right before the
    # injected failure; every dimension below must come back to this.
    baseline_snapshot = _live_snapshot(c, "A12-baseline-live-readback")
    baseline = _baseline(baseline_snapshot)
    baseline_revision = baseline_snapshot.get("modelRevision")
    # (2) injected COM failure on the second parameter: partial write + one bounded rollback
    import catia_mcp.editing.coordinator as coordinator_module

    real_assign = coordinator_module.assign_value
    calls = {"n": 0}

    def flaky(param, value, unit):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected COM timeout on the second parameter")
        return real_assign(param, value, unit)

    coordinator_module.assign_value = flaky
    try:
        applied = c.apply(
            c.plan([("L", 160, 200), ("T", 12, 20)], operation="a12-injected-failure")
        )
    finally:
        coordinator_module.assign_value = real_assign
    c.check("A12-not-ok", applied.get("ok") is False, {"code": applied.get("code")})
    c.check(
        "A12-partial-evidence",
        bool(applied.get("data", {}).get("steps")),
        (applied.get("data") or {}).get("steps"),
    )
    recovery = (applied.get("data") or {}).get("recovery") or {}
    restored = recovery.get("restored") is True
    c.check("A12-rollback-attempted", recovery.get("attempted") is True, recovery)
    # An unproven rollback is a failed acceptance case, never a note that is
    # allowed through: this check is unconditional and fails when restored=false.
    c.check("A12-rollback-proven", restored, recovery)
    status = c.tools.execute("catia_get_edit_status", {"editSessionId": c.session})
    status_state = (status.get("data") or {}).get("businessState")
    result_session_state = (applied.get("data") or {}).get("sessionState") or status_state
    c.check("A12-state-recorded", status_state == "rolled_back", status_state)
    c.check("A12-session-rolled-back", result_session_state == "rolled_back", {
        "sessionState": result_session_state,
        "status": status_state,
    })
    if not restored:
        return c.finish()
    c.check("A12-code", applied.get("code") == "EXECUTION_FAILED", applied.get("code"))
    c.check(
        "A12-modified-false",
        (applied.get("effects") or {}).get("modified") is False,
        applied.get("effects"),
    )
    c.check(
        "A12-revision-not-bumped",
        (applied.get("data") or {}).get("modelRevision") == baseline_revision
        and status_state == "rolled_back"
        and (status.get("data") or {}).get("modelRevision") == baseline_revision,
        {
            "baselineRevision": baseline_revision,
            "resultRevision": (applied.get("data") or {}).get("modelRevision"),
            "sessionRevision": (status.get("data") or {}).get("modelRevision"),
        },
    )
    restored_snapshot = _live_snapshot(c, "A12-live-readback")
    _assert_baseline(c, "A12", baseline, restored_snapshot)
    fixed = c.apply(c.plan([("L", 160, 200)], operation="a12-after-recovery"))
    c.check(
        "A12-recovers-to-usable",
        fixed.get("ok") is True and (fixed.get("data") or {}).get("verdict") == "PASS",
        {"code": fixed.get("code"), "verdict": (fixed.get("data") or {}).get("verdict")},
    )
    fixed_snapshot = _live_snapshot(c, "A12-fixed-live-readback")
    c.eq(
        "A12-recovered-volume",
        ((fixed_snapshot.get("geometry") or {}).get("volumeMm3")),
        vol(200, 100, 12),
    )
    return c.finish()


def case_a12b(h: Harness) -> dict[str, Any]:
    """A12b: the rollback itself fails -> RECOVERY_UNPROVEN, unknown, writes blocked."""
    c = Case(h, "A12b")
    begin = c.begin(c.source("FixtureA-mounting-plate.CATPart"))
    c.check("begin", begin.get("ok") is True, begin.get("code"))
    if not begin.get("ok"):
        return c.finish()
    baseline_snapshot = _live_snapshot(c, "A12b-baseline-live-readback")
    baseline = _baseline(baseline_snapshot)
    baseline_revision = baseline_snapshot.get("modelRevision")
    import catia_mcp.editing.coordinator as coordinator_module

    # Call 1-2 are the apply loop (L then T); call 3-4 are the single bounded
    # rollback in reversed plan order (T then L). Fail call 2 so the apply
    # stops after L, then fail call 4 so the rollback cannot put L back.
    real_assign = coordinator_module.assign_value
    calls = {"n": 0, "log": []}

    def failing(param, value, unit):
        calls["n"] += 1
        calls["log"].append({"call": calls["n"], "value": value, "unit": unit})
        if calls["n"] == 2:
            raise RuntimeError("injected COM timeout on the second parameter")
        if calls["n"] == 4:
            raise RuntimeError("injected COM failure while rolling back the first parameter")
        return real_assign(param, value, unit)

    coordinator_module.assign_value = failing
    try:
        applied = c.apply(
            c.plan([("L", 160, 200), ("T", 12, 20)], operation="a12b-rollback-fails")
        )
    finally:
        coordinator_module.assign_value = real_assign
    recovery = (applied.get("data") or {}).get("recovery") or {}
    c.check("A12b-not-ok", applied.get("ok") is False, {"code": applied.get("code")})
    c.check("A12b-code", applied.get("code") == "RECOVERY_UNPROVEN", applied.get("code"))
    c.check(
        "A12b-rollback-attempted",
        recovery.get("attempted") is True and recovery.get("restored") is False,
        recovery,
    )
    c.check(
        "A12b-modified-true",
        (applied.get("effects") or {}).get("modified") is True,
        applied.get("effects"),
    )
    status = c.tools.execute("catia_get_edit_status", {"editSessionId": c.session})
    status_data = status.get("data") or {}
    status_state = status_data.get("businessState")
    result_session_state = (applied.get("data") or {}).get("sessionState") or status_state
    c.check("A12b-session-unknown", status_state == "unknown", status_state)
    c.check("A12b-session-state-field", result_session_state == "unknown", {
        "sessionState": result_session_state,
        "status": status_state,
    })
    expected_revision = (
        baseline_revision + 1 if isinstance(baseline_revision, int) else None
    )
    c.check(
        "A12b-revision-bumped",
        expected_revision is not None and status_data.get("modelRevision") == expected_revision,
        {
            "baselineRevision": baseline_revision,
            "expectedRevision": expected_revision,
            "resultRevision": (applied.get("data") or {}).get("modelRevision"),
            "sessionRevision": status_data.get("modelRevision"),
        },
    )
    operations = status_data.get("operations") or []
    matching = [op for op in operations if op.get("operationId") == "a12b-rollback-fails"]
    c.check(
        "A12b-operation-visible",
        bool(matching) and matching[0].get("state") in ("unknown", "failed"),
        {"operations": operations},
    )
    # The partial write is still visible: this is what "modified=true" claims.
    failed_snapshot = _live_snapshot(c, "A12b-live-readback")
    partial = _baseline_diff(baseline, failed_snapshot)
    c.check(
        "A12b-partial-write-visible",
        bool(partial.get("parameters")) or partial.get("volumeChanged") is True,
        partial,
    )
    blocked = c.apply(c.plan([("W", 100, 120)], operation="a12b-blocked"))
    c.check(
        "A12b-writes-blocked",
        blocked.get("ok") is False and blocked.get("code") == "UNKNOWN_STATE",
        {"code": blocked.get("code"), "message": (blocked.get("message") or "")[:200]},
    )
    status_after = c.tools.execute("catia_get_edit_status", {"editSessionId": c.session})
    c.check(
        "A12b-stays-unknown",
        (status_after.get("data") or {}).get("businessState") == "unknown",
        (status_after.get("data") or {}).get("businessState"),
    )
    return c.finish()


def case_a13(h: Harness) -> dict[str, Any]:
    c = Case(h, "A13")
    from catia_mcp.editing.journal import Journal

    begin = c.begin(c.source("FixtureA-mounting-plate.CATPart"))
    c.check("begin", begin.get("ok") is True, begin.get("code"))
    if not begin.get("ok"):
        return c.finish()

    # --- checkpoint 1: restart after a verified operation -----------------
    # The journal must survive a process boundary: a brand-new Python process
    # reads the verified operation back before anything is recovered.
    verified = c.apply(c.plan([("L", 160, 200)], operation="a13-verified"))
    c.check(
        "A13-verified-apply",
        verified.get("ok") is True and (verified.get("data") or {}).get("verdict") == "PASS",
        {"code": verified.get("code"), "verdict": (verified.get("data") or {}).get("verdict")},
    )
    verified_snapshot = _live_snapshot(c, "A13-verified-live-readback")
    verified_revision = verified_snapshot.get("modelRevision")
    status_verified = c.tools.execute("catia_get_edit_status", {"editSessionId": c.session})
    c.check(
        "A13-status-verified",
        status_verified.get("ok") is True
        and (status_verified.get("data") or {}).get("businessState") == "verified",
        (status_verified.get("data") or {}).get("businessState"),
    )
    probe_verified = _probe_journal(c.journal_path, c.session, "a13-verified")
    probe_payload = probe_verified.get("payload") or {}
    c.check(
        "A13-subprocess-reads-verified",
        probe_verified.get("returncode") == 0
        and (probe_payload.get("session") or {}).get("status") == "verified"
        and (probe_payload.get("operation") or {}).get("state") == "verified"
        and (probe_payload.get("operation") or {}).get("sessionId") == c.session,
        probe_verified,
    )
    baseline = _baseline(verified_snapshot)

    # --- checkpoint 2: intent persisted, process died before evidence -----
    plan = c.plan([("W", 100, 120)], operation="a13-crash")
    crashed = c.journal.load_session(c.session)
    crashed["status"] = "executing"
    c.journal.save_session(crashed)
    c.tools.coordinator.journal.upsert_operation(
        "a13-crash",
        c.session,
        plan_hash(plan),
        "intent",
        {"plan": plan, "before": baseline, "revision": verified_revision},
    )
    probe_intent = _probe_journal(c.journal_path, c.session, "a13-crash")
    probe_intent_payload = probe_intent.get("payload") or {}
    c.check(
        "A13-subprocess-reads-intent",
        probe_intent.get("returncode") == 0
        and (probe_intent_payload.get("session") or {}).get("status") == "executing"
        and (probe_intent_payload.get("operation") or {}).get("state") == "intent",
        probe_intent,
    )
    # "restart": a fresh coordinator/journal instance over the same database
    restarted_journal = Journal(c.journal_path)
    conn_tools = h.tools_with(restarted_journal)
    status = conn_tools.execute("catia_get_edit_status", {"editSessionId": c.session})
    c.check("A13-status-after-restart", status.get("ok") is True, status.get("code"))
    c.check(
        "A13-operation-visible",
        any(
            op.get("operationId") == "a13-crash"
            for op in (status.get("data") or {}).get("operations") or []
        ),
        (status.get("data") or {}).get("operations"),
    )
    dry = conn_tools.execute("catia_recover_edit", {"editSessionId": c.session, "dryRun": True})
    c.check("A13-dry-run-ok", dry.get("ok") is True, dry.get("code"))
    assessment = ((dry.get("data") or {}).get("assessments") or [{}])[0]
    c.check("A13-assessed-not-applied", assessment.get("state") == "not_applied", assessment)
    # now emulate the opposite: the write happened, evidence did not
    from catia_mcp.editing.coordinator import _update_sketches
    from catia_mcp.editing.session import get_param, unit_of, assign_value

    doc = conn_tools.coordinator._doc(conn_tools.coordinator._load(c.session))
    param = get_param(doc.Part, plan["changes"][0]["parameter"])
    assign_value(param, 120.0, unit_of(param))
    _update_sketches(doc.Part)
    doc.Part.Update()
    dry2 = conn_tools.execute("catia_recover_edit", {"editSessionId": c.session, "dryRun": True})
    assessment2 = ((dry2.get("data") or {}).get("assessments") or [{}])[0]
    c.check("A13-assessed-applied", assessment2.get("state") == "applied", assessment2)
    real = conn_tools.execute("catia_recover_edit", {"editSessionId": c.session, "dryRun": False})
    recovered_state = (real.get("data") or {}).get("businessState")
    c.check(
        "A13-recover-business-state",
        recovered_state in ("rolled_back", "unknown"),
        {"code": real.get("code"), "businessState": recovered_state},
    )
    c.check(
        "A13-recover-proven",
        real.get("ok") is True and real.get("code") == "ROLLED_BACK" and recovered_state == "rolled_back",
        {
            "code": real.get("code"),
            "businessState": recovered_state,
            "effects": real.get("effects"),
            "restorations": (real.get("data") or {}).get("restorations"),
        },
    )
    status_after = conn_tools.execute("catia_get_edit_status", {"editSessionId": c.session})
    c.check(
        "A13-status-matches-recovery",
        (status_after.get("data") or {}).get("businessState") == recovered_state,
        {
            "recoverBusinessState": recovered_state,
            "statusBusinessState": (status_after.get("data") or {}).get("businessState"),
        },
    )
    operations_after = (status_after.get("data") or {}).get("operations") or []
    matching = [op for op in operations_after if op.get("operationId") == "a13-crash"]
    c.check(
        "A13-operation-recovered",
        bool(matching) and matching[0].get("state") == "recovered",
        {"operations": operations_after},
    )
    # Multi-dimensional readback: the live model is back at the pre-crash baseline.
    recovered_snapshot = _live_snapshot(c, "A13-recovered-live-readback")
    _assert_baseline(c, "A13", baseline, recovered_snapshot)
    # The recovered state is also queryable from a new process, not just memory.
    probe_recovered = _probe_journal(c.journal_path, c.session, "a13-crash")
    probe_after = probe_recovered.get("payload") or {}
    c.check(
        "A13-subprocess-reads-recovered",
        probe_recovered.get("returncode") == 0
        and (probe_after.get("session") or {}).get("status") == recovered_state
        and (probe_after.get("operation") or {}).get("state") == "recovered",
        probe_recovered,
    )
    return c.finish()


def case_a14(h: Harness) -> dict[str, Any]:
    c = Case(h, "A14")
    source = c.source("FixtureA-mounting-plate.CATPart")
    source_sha = _sha(source)
    begin = c.begin(source)
    c.check("begin", begin.get("ok") is True, begin.get("code"))
    if not begin.get("ok"):
        return c.finish()
    applied = c.apply(c.plan([("L", 160, 200)], operation="a14-save"))
    c.check("A14-apply-ok", applied.get("ok") is True, applied.get("code"))
    existing = os.path.join(c.dir, "deliveries", "existing.CATPart")
    os.makedirs(os.path.dirname(existing), exist_ok=True)
    with open(existing, "wb") as handle:
        handle.write(b"do-not-overwrite")
    conflict = c.tools.execute(
        "catia_finalize_edit", {"editSessionId": c.session, "deliveryPath": existing}
    )
    c.check(
        "A14-save-conflict",
        conflict.get("ok") is False and conflict.get("code") == "SAVE_CONFLICT",
        conflict.get("code"),
    )
    c.check("A14-existing-untouched", open(existing, "rb").read() == b"do-not-overwrite", {
        "path": existing, "sha256": _sha(existing),
    })
    bad = c.tools.execute(
        "catia_finalize_edit", {"editSessionId": c.session, "deliveryPath": r"Q:\nope\x.CATPart"}
    )
    c.check("A14-save-failed", bad.get("ok") is False and bad.get("code") == "SAVE_FAILED", bad.get("code"))
    status = c.tools.execute("catia_get_edit_status", {"editSessionId": c.session})
    status_state = (status.get("data") or {}).get("businessState")
    c.check("A14-still-verified", status_state == "verified", status_state)
    good = c.finalize("A14-good.CATPart")
    c.check("A14-finalize-after-failure", good.get("ok") is True, good.get("code"))
    final_verification = (good.get("data") or {}).get("verification") or {}
    final_checks = final_verification.get("checks") or []
    passed_ids = {
        check.get("id")
        for check in final_checks
        if isinstance(check, dict) and check.get("status") == "pass"
    }
    c.check(
        "A14-reopen-evidence",
        final_verification.get("passed") is True
        and {"volume", "bbox", "holes", "drivers"} <= passed_ids,
        {"passed": final_verification.get("passed"), "passedCheckIds": sorted(str(i) for i in passed_ids)},
    )

    # --- delivered report: machine-readable evidence ----------------------
    delivery = (good.get("data") or {}).get("delivery") or {}
    delivery_path = delivery.get("path")
    delivery_sha = delivery.get("sha256")
    c.check(
        "A14-delivery-exists",
        bool(delivery_path)
        and os.path.exists(delivery_path)
        and _sha(delivery_path) == delivery_sha,
        {"path": delivery_path, "sha256": delivery_sha},
    )
    report_json_path = delivery.get("reportJson")
    report_md_path = delivery.get("reportMarkdown")
    report: dict[str, Any] = {}
    report_error = None
    if report_json_path and os.path.exists(report_json_path):
        try:
            with open(report_json_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                report = loaded
            else:
                report_error = f"report root is {type(loaded).__name__}, expected object"
        except Exception as exc:  # noqa: BLE001 - a broken report is a failed check
            report_error = f"{type(exc).__name__}: {exc}"
    c.check(
        "A14-report-json-exists",
        bool(report),
        {"path": report_json_path, "error": report_error},
    )
    report_intent = _report_intent(report)
    report_tolerances = _report_tolerances(report)
    report_recovery_history = _report_recovery_history(report)
    report_source_unchanged = _report_source_unchanged(report)
    c.check(
        "A14-report-intent",
        _intent_present(report_intent),
        report_intent,
    )
    c.check(
        "A14-report-tolerance",
        bool(report_tolerances),
        report_tolerances,
    )
    c.check(
        "A14-report-recovery-history",
        isinstance(report_recovery_history, list),
        report_recovery_history,
    )
    c.check(
        "A14-report-source-unchanged",
        report_source_unchanged is True,
        report_source_unchanged,
    )

    # Source-untouched is proven by hashes read independently of the report.
    after_sha = _sha(source)
    snapshot_sha = (c.snapshot.get("source") or {}).get("sha256")
    report_source = report.get("source") if report else None
    report_source_sha = report_source.get("sha256") if isinstance(report_source, dict) else None
    source_evidence = {
        "sourcePath": source,
        "startSha256": source_sha,
        "endSha256": after_sha,
        "snapshotSha256": snapshot_sha,
        "reportSha256": report_source_sha,
        "reportSourceUnchanged": report_source_unchanged,
    }
    c.check(
        "A14-source-untouched",
        after_sha == source_sha and snapshot_sha == source_sha and report_source_sha == source_sha,
        source_evidence,
    )
    c.check(
        "A14-source-evidence-nonempty",
        all(
            isinstance(source_evidence[key], str) and source_evidence[key]
            for key in ("startSha256", "endSha256", "snapshotSha256", "reportSha256")
        ),
        source_evidence,
    )
    report_verification = report.get("verification")
    if not isinstance(report_verification, dict):
        report_verification = {}
    c.check(
        "A14-report-reopen-verified",
        report_verification.get("passed") is True,
        report_verification.get("passed"),
    )
    verification_checks = report_verification.get("checks") or []
    source_check = next(
        (
            item
            for item in verification_checks
            if isinstance(item, dict) and item.get("id") == "source-unchanged"
        ),
        None,
    )
    source_check_evidence = (source_check or {}).get("evidence") or {}
    measured_before = report_verification.get("sourceSha256Before")
    measured_after = report_verification.get("sourceSha256After")
    c.check(
        "A14-source-check-evidence",
        (
            isinstance(source_check_evidence, dict)
            and bool(source_check_evidence)
            and source_check_evidence.get("before") == source_sha
            and source_check_evidence.get("after") == source_sha
        )
        or (measured_before == source_sha and measured_after == source_sha),
        {
            "sourceSha256": source_sha,
            "check": source_check,
            "sourceSha256Before": measured_before,
            "sourceSha256After": measured_after,
        },
    )

    # --- delivered report: Markdown must not default to "是" --------------
    markdown = ""
    if report_md_path and os.path.exists(report_md_path):
        with open(report_md_path, "r", encoding="utf-8") as handle:
            markdown = handle.read()
    c.check("A14-report-markdown-exists", bool(markdown), {"path": report_md_path})
    actual_source_line = _source_line(markdown)
    c.check(
        "A14-markdown-source-claim-backed",
        _source_claim_ok(actual_source_line, report_source_unchanged),
        {"line": actual_source_line, "sourceUnchanged": report_source_unchanged},
    )
    default_probe = (
        _render_markdown_without_source_evidence(report)
        if report
        else {"rendered": False, "line": "", "ok": False, "error": "no report"}
    )
    c.check(
        "A14-markdown-no-default-yes",
        default_probe.get("ok") is True,
        default_probe,
    )
    return c.finish()


def case_t2_renamed(h: Harness) -> dict[str, Any]:
    """T2: the same fixture with English parameter names stays readable and editable."""
    c = Case(h, "T2-renamed")
    begin = c.begin(c.source("FixtureA-renamed.CATPart"))
    c.check("begin", begin.get("ok") is True, begin.get("code"))
    if not begin.get("ok"):
        return c.finish()
    names = {p["handle"].split("\\")[-1] for p in c.snapshot["parameters"] if p.get("kind") == "driver"}
    c.check(
        "renamed-drivers-detected",
        {"PlateLength", "PlateWidth", "PlateThickness", "HoleDiameter", "EdgeDistance"} <= names,
        sorted(names),
    )
    applied = c.apply(
        c.plan(
            [("PlateLength", 160, 200)],
            preserve=[{"type": "holes_edge_distance"}],
            acceptance=[
                {"type": "volume", "expected": vol(200, 100, 12)},
                {"type": "hole_positions", "holes": [{"x": -85, "y": 35}, {"x": 85, "y": 35}]},
            ],
            operation="t2-renamed-length",
        )
    )
    c.check("renamed-PASS", applied.get("data", {}).get("verdict") == "PASS", applied.get("data", {}).get("verdict"))
    c.eq("renamed-volume", c.snapshot["geometry"]["volumeMm3"], vol(200, 100, 12))
    return c.finish()


CASES = {
    "A01": case_a01,
    "A02A03A04": case_a02_a03_a04,
    "A05": case_a05,
    "A06A07": case_a06_a07,
    "A08": case_a08_a09a,
    "A09": case_a09bc,
    "A10": case_a10,
    "A11": case_a11,
    "A12": case_a12,
    "A12b": case_a12b,
    "A13": case_a13,
    "A14": case_a14,
    "T2": case_t2_renamed,
}


def _sha(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _volume(app: Any, doc: Any) -> float | None:
    try:
        spa = doc.GetWorkbench("SPAWorkbench")
        return round(float(spa.GetMeasurable(doc.Part.CreateReferenceFromObject(doc.Part.MainBody)).Volume) * 1e9, 6)
    except Exception:
        return None


def main(argv: list[str]) -> int:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    app = win32com.client.Dispatch("CATIA.Application")
    app.Visible = True
    app.DisplayFileAlerts = False
    harness = Harness(app)
    harness.close_docs_under_root()
    selected = [name for name in argv[1:] if name in CASES] or list(CASES)
    results = []
    started = time.time()
    for key in selected:
        print(f"\n=== {key}", flush=True)
        case_started = time.time()
        try:
            result = CASES[key](harness)
        except Exception as exc:  # noqa: BLE001 - a crashed case is a failed case
            import traceback

            traceback.print_exc()
            result = {"case": key, "passed": False, "checks": [{"id": "crash", "status": "fail", "evidence": str(exc)}], "notes": []}
        result["seconds"] = round(time.time() - case_started, 1)
        results.append(result)
        failed = [c["id"] for c in result["checks"] if c["status"] != "pass"]
        print(f"    {'PASS' if result['passed'] else 'FAIL'} in {result['seconds']}s; failed checks: {failed}")
        harness.close_docs_under_root()

    report = {
        "generatedUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.time() - started, 1),
        "passed": sum(1 for r in results if r["passed"]),
        "total": len(results),
        "cases": results,
    }
    os.makedirs(ROOT, exist_ok=True)
    out = os.path.join(ROOT, "acceptance-report.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
    print(f"\n{report['passed']}/{report['total']} cases passed in {report['seconds']}s -> {out}")
    for case in results:
        if not case["passed"]:
            for check in case["checks"]:
                if check["status"] != "pass":
                    print(f"  [{case['case']}] {check['id']}: {json.dumps(check['evidence'], ensure_ascii=False)[:300]}")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
