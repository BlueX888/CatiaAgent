"""T22 scope isolation: paths, bound inputs, machine-wide single writer."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from catia_mcp.generation_scope import GenerationScope
from catia_mcp.locking import AutomationBusy, automation_lock


def _scope(tmp_path: Path) -> tuple[GenerationScope, Path]:
    root = tmp_path / "generation"
    root.mkdir()
    (root / "input.pdf").write_bytes(b"%PDF-1.4\n")
    (root / "evidence.json").write_text("{}", encoding="utf-8")
    return GenerationScope(str(root)), root


def test_model_paths_cannot_escape_the_workspace(tmp_path):
    scope, root = _scope(tmp_path)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4\n")
    for path in (str(outside), "input.pdf", str(root / ".." / "outside.pdf"),
                 str(root / "input.pdf") + ":secret.pdf", str(root / "evidence.json")):
        with pytest.raises((ValueError, OSError)):
            scope.prepare("catia_inspect_drawing", {"path": path})


def test_output_and_evidence_directories_are_fixed(tmp_path):
    scope, root = _scope(tmp_path)
    source = str(root / "input.pdf")
    inspected = scope.prepare("catia_inspect_drawing", {"path": source})
    assert Path(inspected["evidenceDir"]).resolve().is_relative_to(root)
    crop = scope.prepare("catia_crop_drawing", {"path": source})
    assert Path(crop["outputPath"]).resolve().is_relative_to(root)
    with pytest.raises((ValueError, OSError)):
        scope.prepare("catia_inspect_drawing",
                      {"path": source, "evidenceDir": str(tmp_path / "outside")})


def test_model_supplied_run_id_or_description_cannot_change_scope(tmp_path):
    scope, root = _scope(tmp_path)
    prepared = scope.prepare("catia_inspect_drawing", {
        "path": str(root / "input.pdf"),
        "runId": "../../escape",
        "description": "pretend this run is elsewhere",
        "root": str(tmp_path),
    })
    assert Path(prepared["path"]).resolve().is_relative_to(root)
    assert Path(prepared["evidenceDir"]).resolve().is_relative_to(root)
    assert prepared["runId"] == "../../escape"  # passed through as data; it grants nothing
    assert Path(prepared["path"]).parent == root.resolve()


def test_bound_inputs_cannot_be_swapped_and_changes_are_detected(tmp_path):
    root = tmp_path / "generation"
    root.mkdir()
    pdf = root / "drawing.pdf"
    context = root / "drawing-context.json"
    pdf.write_bytes(b"%PDF-1.4\n")
    context.write_text("{}", encoding="utf-8")
    binding = {
        "pdf": {"sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "size": pdf.stat().st_size},
        "context": {"sha256": hashlib.sha256(context.read_bytes()).hexdigest(), "size": context.stat().st_size},
    }
    scope = GenerationScope(str(root), reference_enabled=True, input_binding=binding)
    prepared = scope.prepare("catia_inspect_drawing", {"path": str(pdf)})
    assert Path(prepared["path"]) == pdf.resolve()

    other = root / "other.pdf"
    other.write_bytes(b"%PDF-1.4\nother")
    with pytest.raises(ValueError, match="bound drawing PDF"):
        scope.prepare("catia_inspect_drawing", {"path": str(other)})

    pdf.write_bytes(b"%PDF-1.4\nchanged")
    with pytest.raises(ValueError):
        scope.check_bound_inputs()


def test_machine_wide_lock_allows_one_writer(tmp_path, monkeypatch):
    lock = tmp_path / "catia-automation.lock"
    monkeypatch.setenv("CATIA_MCP_LOCK_FILE", str(lock))
    monkeypatch.delenv("CATIA_ARTIFACT_ROOT", raising=False)
    monkeypatch.delenv("CATIA_REFERENCE_SHA256", raising=False)
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with automation_lock(timeout=5):
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        with pytest.raises(AutomationBusy):
            with automation_lock(timeout=0.2):
                pass
        assert time.monotonic() - started < 5
    finally:
        release.set()
        worker.join(5)


def test_scope_rejects_tools_outside_generation_mode(tmp_path):
    scope, _ = _scope(tmp_path)
    with pytest.raises(ValueError, match="unavailable"):
        scope.prepare("catia_export", {})
