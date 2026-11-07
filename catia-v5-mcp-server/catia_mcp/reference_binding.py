"""Verify the fixed reference input before handing its bytes to a CAD helper.

This boundary accepts trusted host manifest data, never model-supplied paths.
It does not itself expose an MCP tool or establish reference evidence.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

MAX_REFERENCE_BYTES = 64 * 1024 * 1024


class ReferenceBinding:
    def __init__(self, root: str, sha256: str, size: int):
        directory = Path(root)
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError("Reference root must be an existing absolute directory.")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("Reference manifest must contain a lowercase SHA-256.")
        if type(size) is not int or not 0 < size <= MAX_REFERENCE_BYTES:
            raise ValueError("Reference manifest size must be between 1 and 64 MiB.")
        self.root = directory.resolve(strict=True)
        self.path = self.root / "reference.step"
        self.sha256 = sha256
        self.size = size
        info = self._path_stat()
        self.identity = (info.st_dev, info.st_ino)
        self.read()

    def _path_stat(self):
        info = self.path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse
                or info.st_nlink != 1 or self.path.resolve(strict=True) != self.path):
            raise ValueError("Reference must be a regular, unlinked file in the generation root.")
        if info.st_size != self.size:
            raise ValueError("Reference size differs from its manifest.")
        return info

    def read(self) -> bytes:
        before = self._path_stat()
        if (before.st_dev, before.st_ino) != self.identity:
            raise ValueError("Reference file identity changed.")
        with self.path.open("rb") as stream:
            opened = stream.fileno()
            opened_info = os.fstat(opened)
            if (opened_info.st_dev, opened_info.st_ino) != self.identity:
                raise ValueError("Reference file identity changed while opening.")
            data = stream.read(self.size + 1)
            after_open = os.fstat(opened)
        after = self._path_stat()
        if ((after.st_dev, after.st_ino) != self.identity
                or (after_open.st_dev, after_open.st_ino) != self.identity
                or len(data) != self.size or hashlib.sha256(data).hexdigest() != self.sha256):
            raise ValueError("Reference file changed or differs from its manifest.")
        if not data.lstrip().startswith(b"ISO-10303-21;"):
            raise ValueError("Reference is not an ISO 10303-21 STEP file.")
        return data
