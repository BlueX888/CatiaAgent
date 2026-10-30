"""Rebuild an agent's requirements on CATIA and print the failing stage detail."""
import json
import sys
import time
from pathlib import Path

ROOT = Path(r"F:\CatiaAgent\catia-v5-mcp-server")
sys.path.insert(0, str(ROOT))

from catia_mcp.connection import CATIAConnection  # noqa: E402
from catia_mcp.drawing.feature_model import compile_features  # noqa: E402
from catia_mcp.drawing.feature_workflow import build_features  # noqa: E402
from catia_mcp.locking import automation_lock  # noqa: E402


def main():
    requirements = Path(sys.argv[1])
    evidence = Path(sys.argv[2])
    req = json.loads(requirements.read_text(encoding="utf-8"))
    manifest = json.loads(evidence.read_text(encoding="utf-8"))
    plan = compile_features(req, manifest, require_evidence=True)
    target = requirements.with_name(f"repro-{time.strftime('%H%M%S')}.CATPart")
    stages = []
    conn = CATIAConnection()
    with automation_lock(timeout=600):
        conn.connect()
        result = build_features(conn, req, plan, target, stages, {})
    data = result.get("data", {})
    print(json.dumps({
        "ok": result.get("ok"), "code": result.get("code"), "message": result.get("message"),
        "stages": [(s.get("stage"), s.get("status"), str(s.get("detail"))[:500]) for s in stages],
        "editability": data.get("editability"),
    }, ensure_ascii=False, indent=1)[:6000])
    requirements.with_name("repro-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
