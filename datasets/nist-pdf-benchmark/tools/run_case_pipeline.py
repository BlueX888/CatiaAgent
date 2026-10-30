"""Run one NIST case through prepare -> isolated generation -> acceptance.

The generation side sees only the PDF copied by prepare-generation-run.mjs.
Acceptance runs the independent reopen verifier and the OCCT material
comparison against the reference STEP supplied by the caller (which may be a
CATIA re-export when the original STEP fails the shell-soundness gate).

Usage:
    python run_case_pipeline.py --case FTC-09 --pdf <derived.pdf> \
        --reference <reference.step> --run-label v4
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AGENT = ROOT / "AgentCPM"
REPO = ROOT / "catia-v5-mcp-server"
BENCH = ROOT / "datasets" / "nist-pdf-benchmark"
NODE = Path(r"F:\CodingAgent\claude-code\work\CatiaAgent\20261021-node22\node\node-v22.23.3-win-x64\node.exe")
PILOT_HOME = Path(r"F:\CatiaAgent\work\ui-home-20261024")
RUNS = ROOT / "work" / "isolated-generation"
RESULTS = ROOT / "runs" / "nist-pdf-benchmark" / "20261030"


def node(args, timeout=None):
    env = dict(os.environ, PILOT_HOME=str(PILOT_HOME))
    completed = subprocess.run([str(NODE), *args], cwd=str(AGENT), env=env,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
    return completed.returncode, completed.stdout, completed.stderr


def trim_registry():
    path = PILOT_HOME / "runs" / "registry.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if data.get("events"):
        data["events"] = []
        temporary = path.with_suffix(".json.pipe-trim")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    for stale in [path.with_suffix(".json.lock"), *path.parent.glob("*.tmp")]:
        try:
            stale.unlink()
        except OSError:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--skip-run", action="store_true")
    args = parser.parse_args(argv)
    pdf = Path(args.pdf).resolve()
    reference = Path(args.reference).resolve()
    label = args.run_label
    out_dir = RESULTS / args.case / label
    out_dir.mkdir(parents=True, exist_ok=True)

    trim_registry()
    code, stdout, stderr = node([str(AGENT / "products" / "catia-agent" / "acceptance"
                                     / "prepare-generation-run.mjs"),
                                 "--pdf", str(pdf), "--runs-dir", str(RUNS)], timeout=180)
    if code:
        print(stdout[-2000:], stderr[-2000:])
        return 1
    prepared = json.loads(stdout.strip().splitlines()[-1])
    host = Path(prepared["directory"]) / "host"
    print("prepared", prepared["directory"], flush=True)
    if args.skip_run:
        return 0

    code, stdout, stderr = node(["--import", "tsx",
                                 str(AGENT / "products" / "catia-agent" / "acceptance"
                                     / "run-prepared-generation.mjs"), str(host)], timeout=3 * 3600)
    (out_dir / "runner.log").write_text(stdout + "\n" + stderr, encoding="utf-8")
    manifest = json.loads((host / "manifest.json").read_text(encoding="utf-8"))
    terminal = None
    events_path = host / "events.jsonl"
    for line in events_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("type") in {"error", "turn_completed"}:
            terminal = event
    summary = {"case": args.case, "label": label, "host": str(host),
               "manifestStatus": manifest.get("status"),
               "finishReason": (terminal or {}).get("finishReason"),
               "terminal": (terminal or {}).get("type"),
               "runCode": code,
               "events": events_path.read_text(encoding="utf-8").splitlines()[-1] if events_path.exists() else None}
    (out_dir / "run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    delivery = host.parent / "generation" / "deliveries" / "part.CATPart"
    if not delivery.exists():
        print("no delivery", flush=True)
        return 2
    completed = subprocess.run(
        [str(REPO / ".venv" / "Scripts" / "python.exe"),
         str(BENCH / "tools" / "verify_case.py"), "--case", args.case,
         "--part", str(delivery), "--reference", str(reference),
         "--out", str(out_dir / "acceptance.json")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    (out_dir / "acceptance.stdout").write_text(stdout + "\n" + stderr, encoding="utf-8")
    print(stdout[-2000:], flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
