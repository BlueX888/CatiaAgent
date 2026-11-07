"""Read only host-bound, STEP-associated reference images."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat


class ReferenceViews:
    def __init__(self, binding, metadata):
        self.binding = binding
        if (not isinstance(metadata, dict) or set(metadata) != {"index", "views"}
                or not isinstance(metadata["views"], list) or not 1 <= len(metadata["views"]) <= 100):
            raise ValueError("Invalid reference view binding.")
        self.directory = binding.root / "views"
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or self.directory.resolve(strict=True) != self.directory):
            raise ValueError("Reference views directory must not be linked.")
        self.index_binding = metadata["index"]
        self.views = {}
        for item in metadata["views"]:
            if not isinstance(item, dict) or set(item) != {"id", "sha256", "size"}:
                raise ValueError("Invalid reference view identity.")
            view_id = item["id"]
            if (not isinstance(view_id, str) or not re.fullmatch(r"view_[0-9]{1,6}", view_id)
                    or view_id in self.views or not isinstance(item["sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
                    or type(item["size"]) is not int or not 0 < item["size"] <= 4 * 1024 * 1024):
                raise ValueError("Invalid reference view metadata or duplicate ID.")
            self.views[view_id] = item
        self._index()

    def _read(self, name, item, limit):
        if (not isinstance(item, dict) or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
                or type(item.get("size")) is not int or not 0 < item["size"] <= limit):
            raise ValueError("Invalid reference view file metadata.")
        path = self.directory / name
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400
                or before.st_nlink != 1 or path.resolve(strict=True) != path or before.st_size != item["size"]):
            raise ValueError("Reference view file identity changed.")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            payload = stream.read(item["size"] + 1)
            after = os.fstat(stream.fileno())
        final = path.lstat()
        if (any((info.st_dev, info.st_ino, info.st_size) != (before.st_dev, before.st_ino, before.st_size)
                for info in (opened, after, final)) or len(payload) != item["size"]
                or hashlib.sha256(payload).hexdigest() != item["sha256"]):
            raise ValueError("Reference view file changed or hash differs.")
        return payload

    def _index(self):
        self.binding.read()
        index = json.loads(self._read("index.json", self.index_binding, 1024 * 1024))
        if not isinstance(index, dict):
            raise ValueError("Invalid reference view index.")
        records = index.get("records")
        if (not isinstance(records, list) or index.get("record_count") != len(records)
                or len(records) != len(self.views)):
            raise ValueError("Reference view index does not match binding.")
        result = {}
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Invalid reference view record.")
            view_id = record.get("view_id")
            if (view_id not in self.views or view_id in result or record.get("record_type") != "view"
                    or record.get("png_path") != f"views/{view_id}.png"
                    or record.get("geometry_sha256") != self.binding.sha256):
                raise ValueError("Reference view is not bound to this STEP.")
            for key in ("camera_position", "target", "up"):
                vector = record.get(key)
                try:
                    valid = (isinstance(vector, list) and len(vector) == 3
                             and all(type(x) in (int, float) and math.isfinite(x) for x in vector))
                except OverflowError:
                    valid = False
                if not valid:
                    raise ValueError("Invalid reference view camera.")
            projection = record.get("projection_type")
            if not isinstance(projection, str) or not 1 <= len(projection) <= 64:
                raise ValueError("Invalid reference view projection.")
            result[view_id] = {key: record[key] for key in ("view_id", "camera_position", "target", "up", "projection_type")}
            result[view_id].update(imageSha256=self.views[view_id]["sha256"], imageSize=self.views[view_id]["size"],
                                   sourceSha256=self.binding.sha256)
        return result

    def list(self, offset, limit):
        records = list(self._index().values())
        rows = records[offset:offset + limit]
        return {"offset": offset, "total": len(records),
                "nextOffset": offset + len(rows) if offset + len(rows) < len(records) else None,
                "views": rows, "sourceSha256": self.binding.sha256}

    def image(self, view_id):
        records = self._index()
        if view_id not in records:
            raise ValueError("Unknown reference view.")
        payload = self._read(f"{view_id}.png", self.views[view_id], 4 * 1024 * 1024)
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Reference view is not a PNG.")
        return records[view_id], payload
