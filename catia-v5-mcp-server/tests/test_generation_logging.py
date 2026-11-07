"""Assisted MCP instances never reuse a shared source-tree log."""
import os
import subprocess
import sys


def test_assisted_logs_are_unique_and_bound_to_generation(tmp_path):
    root = tmp_path / "generation"
    logs = root / ".runtime" / "logs"
    logs.mkdir(parents=True)
    existing = logs / "existing.log"
    existing.write_text("preserve", encoding="utf-8")
    env = dict(os.environ, CATIA_GENERATION_ROOT=str(root), CATIA_MCP_LOG_DIR=str(logs),
               CATIA_REFERENCE_SHA256="a" * 64, PYTHONDONTWRITEBYTECODE="1")
    for _ in range(2):
        result = subprocess.run([sys.executable, "-B", "-c", "import catia_mcp.server"],
                                env=env, capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
    assert existing.read_text(encoding="utf-8") == "preserve"
    assert len(list(logs.glob("mcp-*.log"))) == 2
    env["CATIA_MCP_LOG_DIR"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-B", "-c", "import catia_mcp.server"],
                            env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode != 0
    assert not list(tmp_path.glob("mcp-*.log"))
