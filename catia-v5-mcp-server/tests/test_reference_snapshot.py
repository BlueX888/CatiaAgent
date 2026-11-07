import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from catia_mcp.reference_snapshot import guarded_reference_snapshot
from catia_mcp.reference_binding import ReferenceBinding
from catia_mcp.reference_tools import ReferenceTools


@pytest.mark.skipif(os.name != "nt", reason="Windows non-advisory file-sharing guard")
def test_reference_snapshot_blocks_other_process_writes_and_directory_rename(tmp_path):
    data = b"ISO-10303-21;\noriginal\nEND-ISO-10303-21;"
    script = (
        "from pathlib import Path\nimport sys\n"
        "p=Path(sys.argv[1])\n"
        "try:\n p.write_bytes(b'replaced')\n print('wrote')\n"
        "except OSError:\n print('write-blocked')\n"
        "try:\n p.parent.rename(p.parent.with_name(p.parent.name+'-moved'))\n print('moved')\n"
        "except OSError:\n print('move-blocked')\n"
    )
    with guarded_reference_snapshot(tmp_path, data) as path:
        assert path.read_bytes() == data
        result = subprocess.run([sys.executable, "-B", "-c", script, str(path)],
                                capture_output=True, text=True, check=True, timeout=15)
        assert result.stdout.splitlines() == ["write-blocked", "move-blocked"]
        assert path.read_bytes() == data
    assert not path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows non-advisory file-sharing guard")
def test_reference_snapshot_fails_closed_when_file_already_exists(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from catia_mcp import reference_snapshot

    directory = tmp_path / "known"
    directory.mkdir()
    existing = directory / "reference.step"
    existing.write_bytes(b"unrelated")

    @contextmanager
    def fixed_directory(*args, **kwargs):
        yield str(directory)

    monkeypatch.setattr(reference_snapshot.tempfile, "TemporaryDirectory", fixed_directory)
    with pytest.raises(FileExistsError):
        with guarded_reference_snapshot(tmp_path, b"new"):
            pass
    assert existing.read_bytes() == b"unrelated"


@pytest.mark.skipif(os.name != "nt", reason="Windows non-advisory file-sharing guard")
def test_reference_tool_registers_only_bytes_protected_during_helper_read(tmp_path, monkeypatch):
    data = b"ISO-10303-21;\noriginal\nEND-ISO-10303-21;"
    (tmp_path / "reference.step").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    tool = ReferenceTools(ReferenceBinding(str(tmp_path), digest, len(data)))

    def query(_, spec):
        target = Path(spec["sourceStep"])
        with pytest.raises(PermissionError):
            target.write_bytes(b"replacement")
        assert target.read_bytes() == data
        return {"ok": True, "data": {"query": "edges", "sourceSha256": digest,
            "sourceUnit": "mm", "coordinateFrame": "STEP model coordinates",
            "edges": [{"id": "0", "kind": "linear", "length": 5.0}]}}

    monkeypatch.setattr("catia_mcp.reference_tools.run_support", query)
    result = tool.execute("catia_query_reference", {"kind": "edges"})
    assert result["ok"] is True
    assert tool.evidence()[f"ref3d:{digest}:edge:0:length"]["value"] == 5.0


def test_reference_tool_does_not_call_helper_if_guard_fails(tmp_path, monkeypatch):
    from contextlib import contextmanager

    data = b"ISO-10303-21;\nEND-ISO-10303-21;"
    (tmp_path / "reference.step").write_bytes(data)
    tool = ReferenceTools(ReferenceBinding(str(tmp_path), hashlib.sha256(data).hexdigest(), len(data)))

    @contextmanager
    def unavailable(*args):
        raise ValueError("snapshot guard unavailable")
        yield

    monkeypatch.setattr("catia_mcp.reference_tools.guarded_reference_snapshot", unavailable)
    monkeypatch.setattr("catia_mcp.reference_tools.run_support", lambda *_: pytest.fail("must not query"))
    result = tool.execute("catia_query_reference", {"kind": "edges"})
    assert result["ok"] is False and result["code"] == "REFERENCE_QUERY_FAILED"
    assert tool.evidence() == {}
