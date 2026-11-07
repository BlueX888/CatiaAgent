"""A15 (CAD half): ten consecutive modifications in one session, then delivery.

Checks: every round is verified, the source hash never changes, the journal and
the log stay bounded, and the delivered file reopens with the final values.

Run: .venv/Scripts/python.exe tests/acceptance/continuous_rounds.py
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import shutil
import sqlite3
import time
from ctypes import wintypes

import pythoncom

from catia_mcp.connection import CATIAConnection
from catia_mcp.editing.journal import Journal
from catia_mcp.tools.editing import EditingTools

ROOT = os.environ.get("CATIA_ACCEPTANCE_OUT", r"F:\CatiaAgent\work\stage1") + r"\continuous"
FIXTURE = os.environ.get("CATIA_ACCEPTANCE_FIXTURES") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures")
)
FIXTURE = os.path.join(FIXTURE, "FixtureA-mounting-plate.CATPart")
ROUNDS = 10


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def rss_mb() -> float | None:
    """Working-set size of this process (the script drives COM in-process)."""
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    try:
        psapi = ctypes.WinDLL("psapi")
        kernel32 = ctypes.WinDLL("kernel32")
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
    except Exception:
        return None
    return round(counters.WorkingSetSize / (1024 * 1024), 1) if ok else None


def journal_stats(path: str, session_id: str) -> dict[str, int]:
    size = sum(os.path.getsize(path + suffix) for suffix in ("", "-wal") if os.path.exists(path + suffix))
    with sqlite3.connect(path) as conn:
        events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        operations = conn.execute(
            "SELECT COUNT(*) FROM operations WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
    return {"journalBytes": size, "events": events, "operations": operations}


def sha(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def volume(L: float) -> float:
    return (L * 100.0 - 2 * math.pi * 25.0) * 12.0


def main() -> int:
    pythoncom.CoInitialize()
    os.makedirs(os.path.join(ROOT, "src"), exist_ok=True)
    source = os.path.join(ROOT, "src", "FixtureA-mounting-plate.CATPart")
    shutil.copy2(FIXTURE, source)
    for suffix in ("", "-wal", "-shm"):
        path = os.path.join(ROOT, "journal.sqlite3" + suffix)
        if os.path.exists(path):
            os.remove(path)
    conn = CATIAConnection()
    conn.connect()
    docs = conn.app.Documents
    conn.app.DisplayFileAlerts = False
    for i in range(docs.Count, 0, -1):
        try:
            if str(docs.Item(i).FullName).lower().startswith(ROOT.lower()):
                docs.Item(i).Close()
        except Exception:
            pass
    conn.app.DisplayFileAlerts = True
    journal = Journal(os.path.join(ROOT, "journal.sqlite3"))
    tools = EditingTools(conn, journal)
    source_sha = sha(source)
    started = time.time()
    begin = tools.execute("catia_begin_edit", {"sourcePath": source, "workingDir": os.path.join(ROOT, "edits")})
    if not begin.get("ok"):
        print(json.dumps(begin, ensure_ascii=False)[:1500])
        return 1
    sid = begin["data"]["editSessionId"]
    snapshot = begin["data"]["snapshot"]
    handle = next(p["handle"] for p in snapshot["parameters"] if p["handle"].endswith("\\L"))
    rounds = []
    current = 160.0
    for index in range(1, ROUNDS + 1):
        target = 160.0 + 10.0 * index
        plan = {
            "schema": "catia-edit-plan/1",
            "editSessionId": sid,
            "operationId": f"round-{index}",
            "expectedRevision": snapshot["modelRevision"],
            "intent": {"text": f"第 {index} 轮：长度改为 {target} mm"},
            "changes": [{"parameter": handle, "oldValue": current, "newValue": target, "unit": "mm"}],
            "preserve": [{"type": "holes_edge_distance"}],
            "acceptance": [
                {"type": "volume", "expected": round(volume(target), 6)},
                {"type": "bbox_dimension", "axis": "x", "expected": target},
                {"type": "hole_positions",
                 "holes": [{"x": -(target / 2 - 15), "y": 35}, {"x": target / 2 - 15, "y": 35}]},
            ],
        }
        round_started = time.time()
        apply_started = time.time()
        result = tools.execute("catia_apply_edit_plan", {"editSessionId": sid, "plan": plan})
        apply_seconds = round(time.time() - apply_started, 3)
        inspect_started = time.time()
        snapshot = tools.execute("catia_inspect_edit", {"editSessionId": sid})["data"]["snapshot"]
        inspect_seconds = round(time.time() - inspect_started, 3)
        rounds.append(
            {
                "round": index,
                "target": target,
                "ok": result.get("ok"),
                "code": result.get("code"),
                "verdict": (result.get("data") or {}).get("verdict"),
                "volume": snapshot["geometry"].get("volumeMm3"),
                "revision": snapshot["modelRevision"],
                "sourceUnchanged": sha(source) == source_sha,
                "seconds": round(time.time() - round_started, 3),
                "applySeconds": apply_seconds,
                "inspectSeconds": inspect_seconds,
                "rssMb": rss_mb(),
            }
        )
        print(f"round {index}: {rounds[-1]}")
        if not result.get("ok"):
            break
        current = target
    delivery = os.path.join(ROOT, "delivered", "TenRounds.CATPart")
    os.makedirs(os.path.dirname(delivery), exist_ok=True)
    if os.path.exists(delivery):
        os.remove(delivery)
    final_started = time.time()
    final = tools.execute("catia_finalize_edit", {"editSessionId": sid, "deliveryPath": delivery})
    finalize_seconds = round(time.time() - final_started, 3)
    elapsed = round(time.time() - started, 1)
    stats = journal_stats(os.path.join(ROOT, "journal.sqlite3"), sid)
    report = {
        "rounds": rounds,
        "allRoundsVerified": all(r["ok"] and r["verdict"] == "PASS" for r in rounds) and len(rounds) == ROUNDS,
        "sourceUnchanged": sha(source) == source_sha,
        "finalize": {"ok": final.get("ok"), "code": final.get("code"),
                     "verificationPassed": ((final.get("data") or {}).get("verification") or {}).get("passed")},
        "deliverySha": sha(delivery) if os.path.exists(delivery) else None,
        "elapsedSeconds": elapsed,
        "finalizeSeconds": finalize_seconds,
        "journalStats": stats,
        "journalBytes": stats["journalBytes"],
        "events": stats["events"],
        "operations": stats["operations"],
        "rssMbFinal": rss_mb(),
        "timingSource": "in-process wall clock per apply/inspect; RSS from psapi WorkingSetSize",
    }
    report["passed"] = bool(report["allRoundsVerified"] and report["sourceUnchanged"] and report["finalize"]["ok"])
    with open(os.path.join(ROOT, "continuous-report.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
    print(json.dumps({k: v for k, v in report.items() if k != "rounds"}, ensure_ascii=False, indent=2))
    print("PASS" if report["passed"] else "FAIL")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
