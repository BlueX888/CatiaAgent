"""Keep a reference snapshot stable while an isolated CAD helper reopens it."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import stat
import tempfile


@contextlib.contextmanager
def guarded_reference_snapshot(root: Path, data: bytes):
    """Yield a private, verified STEP path with non-cooperative write protection.

    The CAD reader opens the path several times, so hashing only before and
    after the subprocess is insufficient. Other platforms need an equivalent
    non-advisory guard before this path can provide trusted reference evidence.
    """
    if os.name != "nt":
        raise ValueError("Reference queries require Windows snapshot protection.")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    invalid = ctypes.c_void_p(-1).value
    with tempfile.TemporaryDirectory(prefix="reference-query-", dir=root) as directory:
        parent = Path(directory)
        with contextlib.ExitStack() as stack:
            for item in reversed((parent, *parent.parents)):
                before = item.lstat()
                if not stat.S_ISDIR(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Reference snapshot directory contains a link.")
                handle = create(str(item), 0x80, 0x3, None, 3, 0x02200080, None)
                if handle == invalid:
                    raise ctypes.WinError(ctypes.get_last_error())
                stack.callback(close, handle)
                after = item.lstat()
                if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                        or getattr(after, "st_file_attributes", 0) & 0x400):
                    raise ValueError("Reference snapshot directory changed while opening.")
            target = parent / "reference.step"
            with target.open("xb") as stream:
                stream.write(data)
            # FILE_SHARE_READ permits the helper to read, but blocks writers and deletion.
            handle = create(str(target), 0x80000000, 0x1, None, 3, 0x00200080, None)
            if handle == invalid:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except Exception:
                close(handle)
                raise
            with os.fdopen(fd, "rb") as stream:
                opened, current = os.fstat(stream.fileno()), target.lstat()
                if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                        or getattr(opened, "st_file_attributes", 0) & 0x400
                        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                        or stream.read(len(data) + 1) != data):
                    raise ValueError("Reference snapshot differs from verified input.")
                yield target
                stream.seek(0)
                current = target.lstat()
                if ((opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                        or current.st_nlink != 1 or getattr(current, "st_file_attributes", 0) & 0x400
                        or stream.read(len(data) + 1) != data):
                    raise ValueError("Reference snapshot changed during query.")
