import hashlib
import os

import pytest

from catia_mcp.reference_binding import MAX_REFERENCE_BYTES, ReferenceBinding


def bind(root, data):
    return ReferenceBinding(str(root), hashlib.sha256(data).hexdigest(), len(data))


def test_reference_binding_rechecks_contents_and_identity(tmp_path):
    data = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;"
    reference = tmp_path / "reference.step"
    reference.write_bytes(data)
    binding = bind(tmp_path, data)
    assert binding.read() == data
    reference.write_bytes(data.replace(b"HEADER", b"MODIFY"))
    with pytest.raises(ValueError, match="manifest"):
        binding.read()
    reference.write_bytes(data)
    replacement = tmp_path / "replacement.step"
    replacement.write_bytes(data)
    os.replace(replacement, reference)
    with pytest.raises(ValueError, match="identity"):
        binding.read()


def test_reference_binding_rejects_wrong_hash_and_disguised_file(tmp_path):
    data = b"ISO-10303-21;\nEND-ISO-10303-21;"
    reference = tmp_path / "reference.step"
    reference.write_bytes(data)
    with pytest.raises(ValueError, match="manifest"):
        ReferenceBinding(str(tmp_path), "0" * 64, len(data))
    reference.write_bytes(b"x" * len(data))
    with pytest.raises(ValueError, match="STEP"):
        bind(tmp_path, b"x" * len(data))


def test_reference_binding_rejects_hardlinks(tmp_path):
    data = b"ISO-10303-21;\nEND-ISO-10303-21;"
    source = tmp_path / "other.step"
    source.write_bytes(data)
    try:
        os.link(source, tmp_path / "reference.step")
    except OSError as exc:
        pytest.skip(f"Hard link unavailable: {exc}")
    with pytest.raises(ValueError, match="unlinked"):
        bind(tmp_path, data)


def test_reference_binding_rejects_oversized_manifest_before_open(tmp_path):
    with pytest.raises(ValueError, match="64 MiB"):
        ReferenceBinding(str(tmp_path), "0" * 64, MAX_REFERENCE_BYTES + 1)


def test_reference_binding_rejects_symlinks(tmp_path):
    data = b"ISO-10303-21;\nEND-ISO-10303-21;"
    source = tmp_path / "other.step"
    source.write_bytes(data)
    try:
        (tmp_path / "reference.step").symlink_to(source)
    except OSError as exc:
        pytest.skip(f"Symlink unavailable: {exc}")
    with pytest.raises(ValueError, match="unlinked"):
        bind(tmp_path, data)
