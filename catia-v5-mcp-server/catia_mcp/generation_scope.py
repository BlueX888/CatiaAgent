"""Opt-in tool/file boundary for a fresh PDF-only generation workspace.

This is a model-tool boundary, not an OS sandbox. The host must withhold all
other filesystem, shell, network and MCP tools from the generation session.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
from uuid import uuid4

from catia_mcp.input_binding import BoundInput


GENERATION_TOOLS = frozenset({
    "catia_inspect_drawing", "catia_crop_drawing", "catia_validate_requirements",
    "catia_compile_build_plan", "catia_build_part",
})


class GenerationScope:
    def __init__(self, root: str, *, reference_enabled=False, input_binding=None):
        configured = Path(root)
        if not configured.is_absolute() or not configured.is_dir():
            raise ValueError("CATIA_GENERATION_ROOT must be an existing absolute directory.")
        self.root = configured.resolve(strict=True)
        self.reference_enabled = reference_enabled
        self.bound_inputs = None
        if input_binding is not None:
            if not reference_enabled or not isinstance(input_binding, dict) or set(input_binding) != {"pdf", "context"}:
                raise ValueError("Paired inputs require a reference-enabled generation scope.")
            self.bound_inputs = {"pdf": BoundInput(self.root, "drawing.pdf", input_binding["pdf"], 256 * 1024 * 1024),
                                 "context": BoundInput(self.root, "drawing-context.json", input_binding["context"], 4 * 1024 * 1024)}

    def check_bound_inputs(self):
        if self.bound_inputs:
            for binding in self.bound_inputs.values():
                binding.check()

    def input_snapshots(self, include_context):
        if not self.bound_inputs:
            return None
        return (self.bound_inputs["pdf"].snapshot(),
                self.bound_inputs["context"].snapshot() if include_context else None)

    def _require_workspace_lock(self):
        """Keep the assisted-mode lock in the host workspace; it is not a global gate."""
        root_value = os.environ.get("CATIA_ARTIFACT_ROOT")
        lock_value = os.environ.get("CATIA_MCP_LOCK_FILE")
        if not root_value or not lock_value:
            raise ValueError("Assisted builds require an explicit workspace and CATIA lock path.")
        root, lock = Path(root_value), Path(lock_value)
        if (not root.is_absolute() or not lock.is_absolute() or root.parent == root
                or any(":" in part for part in lock.parts[1:])):
            raise ValueError("Invalid workspace or CATIA lock path.")
        root = root.resolve(strict=True)
        expected = root / "work" / "catia-agent-lock" / "catia-automation.lock"
        if (not self.root.is_relative_to(root) or lock != expected
                or lock.is_relative_to(self.root)):
            raise ValueError("CATIA lock must be the fixed host workspace lock outside the run.")
        for component in (lock.parent, *lock.parent.parents):
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("CATIA lock path must not contain links or reparse points.")
        if not lock.parent.is_dir() or lock.parent.resolve(strict=True) != lock.parent:
            raise ValueError("CATIA lock directory is invalid.")
        try:
            info = lock.lstat()
        except FileNotFoundError:
            return
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("CATIA lock must be an unlinked regular file.")

    def path(self, value, *, suffix=None, exists=False):
        if not isinstance(value, str) or not value or "\0" in value:
            raise ValueError("Generation paths must be nonempty absolute paths.")
        candidate = Path(value)
        if not candidate.is_absolute() or any(":" in part for part in candidate.parts[1:]):
            raise ValueError("Relative paths and alternate data streams are not allowed.")
        resolved = candidate.resolve(strict=exists)
        if not resolved.is_relative_to(self.root):
            raise ValueError("Path is outside this generation workspace.")
        if suffix and resolved.suffix.lower() != suffix:
            raise ValueError(f"Expected a {suffix} file.")
        if exists and not resolved.is_file():
            raise ValueError("Expected an existing regular file.")
        return str(resolved)

    def prepare(self, name, arguments):
        """Validate every caller-controlled I/O path before COM or file access."""
        self.check_bound_inputs()
        if name == "catia_build_part" and self.reference_enabled:
            self._require_workspace_lock()
        if name == "catia_query_reference" and self.reference_enabled:
            return dict(arguments)
        if name not in GENERATION_TOOLS:
            raise ValueError("Tool is unavailable in generation-only mode.")
        result = dict(arguments)
        if name in {"catia_inspect_drawing", "catia_crop_drawing"}:
            result["path"] = self.path(result.get("path"), suffix=".pdf", exists=True)
            if self.bound_inputs and Path(result["path"]) != self.bound_inputs["pdf"].path:
                raise ValueError("Only the bound drawing PDF may be inspected.")
            if name == "catia_inspect_drawing":
                if result.get("drawingContextPath"):
                    result["drawingContextPath"] = self.path(result["drawingContextPath"], suffix=".json", exists=True)
                    if self.bound_inputs and Path(result["drawingContextPath"]) != self.bound_inputs["context"].path:
                        raise ValueError("Only the bound drawing context may be inspected.")
                directory = result.get("evidenceDir") or str(self.root / "evidence")
                result["evidenceDir"] = self.path(directory)
                # inspect_pdf writes this fixed filename inside evidenceDir.
                self.path(str(Path(result["evidenceDir"]) / "evidence.json"))
            else:
                output = result.get("outputPath") or str(self.root / "evidence" / f"crop-{uuid4().hex}.png")
                result["outputPath"] = self.path(output, suffix=".png")
        else:
            if not result.get("evidencePath"):
                raise ValueError("Pass evidencePath returned by catia_inspect_drawing.")
            result["evidencePath"] = self.path(result.get("evidencePath"), suffix=".json", exists=True)
            drawing = (result.get("requirements") or {}).get("drawing") or {}
            if drawing.get("path"):
                path = self.path(drawing["path"], suffix=".pdf", exists=True)
                if self.bound_inputs and Path(path) != self.bound_inputs["pdf"].path:
                    raise ValueError("Only the bound drawing PDF may be used.")
            if result.get("requireEvidence", True) is not True:
                raise ValueError("Generation requires drawing evidence.")
            if name == "catia_build_part":
                result["deliveryPath"] = self.path(result.get("deliveryPath"), suffix=".catpart")
                if "expected" in result or result.get("editCheck", True) is not True:
                    raise ValueError("Generation requires automatic verification and edit checks.")
        return result

    def crop_bytes(self, result):
        target = Path(self.path(result["data"]["path"], suffix=".png", exists=True))
        with target.open("rb") as stream:
            data = stream.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Crop must be a PNG no larger than 10 MiB; request a smaller region.")
        return data
