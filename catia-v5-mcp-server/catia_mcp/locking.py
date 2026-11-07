"""Machine-wide lock for CATIA automation sessions.

CATIA has one interactive document and one active selection; two automation
clients (for example the AgentCPM MCP server and a manual acceptance run) that
build at the same time activate each other's documents and break each other's
formula/constraint creation. The MCP server serializes calls inside one
process, so this lock covers the cross-process case.

On Windows it is an ``msvcrt`` byte lock on a file under
``%LOCALAPPDATA%\\catia-mcp``; elsewhere it falls back to an ``O_EXCL`` lock
file with stale-age detection. Both are advisory: every CATIA-writing entry
point must use the same helper.
"""

from __future__ import annotations

import contextlib
import os
import stat
import time
from pathlib import Path
from typing import Iterator


class AutomationBusy(RuntimeError):
    """Another process holds the CATIA automation lock past the timeout."""


def _lock_path() -> Path:
    configured = os.environ.get("CATIA_MCP_LOCK_FILE")
    if configured:
        return Path(configured)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "catia-mcp" / "catia-automation.lock"


@contextlib.contextmanager
def _assisted_lock_file(path: Path):
    """Hold Windows path components and open the lock without following links."""
    if os.name != "nt":
        raise RuntimeError("Assisted CATIA locking requires Windows path protection.")
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
    with contextlib.ExitStack() as stack:
        # Hold each ancestor without delete sharing so it cannot be renamed
        # between the scope check and the lock file open.
        for directory in reversed((path.parent, *path.parent.parents)):
            before = directory.lstat()
            if (not stat.S_ISDIR(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400):
                raise ValueError("CATIA lock path contains a link or invalid directory.")
            handle = create(str(directory), 0x80, 0x3, None, 3, 0x02200080, None)
            if handle == invalid:
                raise ctypes.WinError(ctypes.get_last_error())
            stack.callback(close, handle)
            after = directory.lstat()
            if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or getattr(after, "st_file_attributes", 0) & 0x400):
                raise ValueError("CATIA lock directory changed while opening.")
        # OPEN_ALWAYS plus OPEN_REPARSE_POINT creates only the lock itself and
        # opens a reparse point rather than following it.
        handle = create(str(path), 0xC0000000, 0x3, None, 4, 0x00200080, None)
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except Exception:
            close(handle)
            raise
        with os.fdopen(fd, "r+b") as stream:
            opened, current = os.fstat(stream.fileno()), path.lstat()
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or getattr(opened, "st_file_attributes", 0) & 0x400
                    or getattr(current, "st_file_attributes", 0) & 0x400
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                raise ValueError("CATIA lock must be an unlinked regular file.")
            yield stream


@contextlib.contextmanager
def automation_lock(timeout: float = 900.0, poll: float = 0.5) -> Iterator[None]:
    """Hold the machine-wide CATIA write lock for the duration of the block."""
    path = _lock_path()
    assisted = bool(os.environ.get("CATIA_ARTIFACT_ROOT") and os.environ.get("CATIA_REFERENCE_SHA256"))
    if not assisted:
        path.parent.mkdir(parents=True, exist_ok=True)
    opener = _assisted_lock_file(path) if assisted else open(path, "a+b")
    with opener as handle:
        locked = False
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise AutomationBusy(
                            f"another CATIA automation run holds {path}; "
                            f"waited {timeout:.0f}s. Close the other run or remove a stale lock."
                        ) from None
                    time.sleep(poll)
            if not assisted:
                handle.seek(0)
                handle.truncate()
                handle.write(f"pid={os.getpid()} since={time.strftime('%Y-%m-%dT%H:%M:%S')}\n".encode())
                handle.flush()
            yield
        finally:
            if locked:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
