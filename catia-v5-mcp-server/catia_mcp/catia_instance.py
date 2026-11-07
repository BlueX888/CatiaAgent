"""Bind a CATIA automation client to an explicitly selected instance."""
from __future__ import annotations

import os


def _same_directory(actual, expected):
    return (isinstance(actual, str) and os.path.isabs(actual)
            and os.path.normcase(os.path.realpath(actual)) == os.path.normcase(os.path.realpath(expected)))


def get_active_catia():
    """Use the host's unique CATTemp when configured; never fall back to another instance."""
    import win32com.client

    expected = os.environ.get("CATIA_REQUIRED_CATTEMP")
    if not expected:
        return win32com.client.GetActiveObject("CATIA.Application")
    root = os.environ.get("CATIA_ARTIFACT_ROOT")
    if not root or not os.path.isabs(root) or not os.path.isabs(expected):
        raise RuntimeError("The required CATIA instance path must be absolute and inside the artifact root.")
    try:
        resolved_root = os.path.realpath(root)
        resolved_expected = os.path.realpath(expected)
        if (os.path.normcase(os.path.commonpath((resolved_root, resolved_expected))) != os.path.normcase(resolved_root)
                or os.path.normcase(resolved_expected) == os.path.normcase(resolved_root)):
            raise ValueError("outside root")
    except ValueError as exc:
        raise RuntimeError("The required CATIA instance path is outside the artifact root.") from exc
    if not os.path.isdir(expected):
        raise RuntimeError("The required CATIA instance directory does not exist.")

    def matches(app):
        try:
            return _same_directory(app.SystemService.Environ("CATTemp"), expected)
        except Exception:
            return False

    try:
        active = win32com.client.GetActiveObject("CATIA.Application")
        if matches(active):
            return active
    except Exception:
        pass

    # Multiple CATIA instances need not share the active-object registration.
    # Search only already-running COM objects; never create a replacement.
    import pythoncom

    try:
        rot = pythoncom.GetRunningObjectTable()
        enum = rot.EnumRunning()
        for _ in range(1024):
            items = enum.Next(1)
            if not items:
                break
            try:
                obj = win32com.client.Dispatch(rot.GetObject(items[0]).QueryInterface(pythoncom.IID_IDispatch))
                if matches(obj):
                    return obj
                app = obj.Application
                if matches(app):
                    return app
            except Exception:
                continue
    except Exception as exc:
        raise RuntimeError("Cannot enumerate CATIA instances for the required identity.") from exc
    raise RuntimeError("No running CATIA instance matches the required temporary directory.")
