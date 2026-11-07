"""Host-bound generation inputs, checked before and after tool execution."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat


class BoundInput:
    def __init__(self, root: Path, name: str, metadata: dict, limit: int):
        if (not isinstance(metadata, dict) or set(metadata) != {"sha256", "size"}
                or not isinstance(metadata["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"])
                or type(metadata["size"]) is not int or not 0 < metadata["size"] <= limit):
            raise ValueError("Invalid generation input binding.")
        self.path = root / name
        self.sha256 = metadata["sha256"]
        self.size = metadata["size"]
        info = self._stat()
        self.identity = (info.st_dev, info.st_ino)
        self.check()

    def _stat(self):
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or info.st_nlink != 1 or self.path.resolve(strict=True) != self.path
                or info.st_size != self.size):
            raise ValueError("Generation input identity or size changed.")
        return info

    def check(self):
        self._read(False)

    def snapshot(self):
        """Return the exact bounded bytes whose identity and digest were checked."""
        return self._read(True)

    def _read(self, retain):
        before = self._stat()
        if (before.st_dev, before.st_ino) != self.identity:
            raise ValueError("Generation input identity changed.")
        digest = hashlib.sha256()
        count = 0
        chunks = [] if retain else None
        with self.path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != self.identity:
                raise ValueError("Generation input changed while opening.")
            while chunk := stream.read(min(1024 * 1024, self.size + 1 - count)):
                count += len(chunk)
                if count > self.size:
                    raise ValueError("Generation input grew during verification.")
                digest.update(chunk)
                if chunks is not None:
                    chunks.append(chunk)
            after_open = os.fstat(stream.fileno())
        after = self._stat()
        def identity(info):
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        if (identity(before) != identity(opened) or identity(before) != identity(after_open)
                or identity(before) != identity(after) or count != self.size or digest.hexdigest() != self.sha256):
            raise ValueError("Generation input changed or hash differs from its binding.")
        return b"".join(chunks) if chunks is not None else None
