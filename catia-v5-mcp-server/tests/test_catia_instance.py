"""An owned run must never silently attach to another CATIA instance."""
from types import SimpleNamespace

import pytest
import pythoncom
import win32com.client

from catia_mcp.catia_instance import get_active_catia


def _app(path):
    return SimpleNamespace(SystemService=SimpleNamespace(Environ=lambda _: str(path)))


def test_selects_matching_active_instance(tmp_path, monkeypatch):
    temp = tmp_path / "cattemp"
    temp.mkdir()
    app = _app(temp)
    monkeypatch.setenv("CATIA_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("CATIA_REQUIRED_CATTEMP", str(temp))
    monkeypatch.setattr(win32com.client, "GetActiveObject", lambda _: app)
    monkeypatch.setattr(pythoncom, "GetRunningObjectTable", lambda: pytest.fail("unexpected ROT search"))
    assert get_active_catia() is app


def test_searches_running_objects_when_active_instance_is_different(tmp_path, monkeypatch):
    temp = tmp_path / "cattemp"
    temp.mkdir()
    wanted = _app(temp)
    old = _app(tmp_path / "old")
    monkeypatch.setenv("CATIA_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("CATIA_REQUIRED_CATTEMP", str(temp))
    monkeypatch.setattr(win32com.client, "GetActiveObject", lambda _: old)

    class Enum:
        def __init__(self): self.remaining = ["other", "target"]
        def Next(self, _): return [self.remaining.pop(0)] if self.remaining else []

    class Object:
        def __init__(self, app): self.Application = app
        def QueryInterface(self, _): return self

    class Rot:
        def EnumRunning(self): return Enum()
        def GetObject(self, item): return Object(wanted if item == "target" else old)

    monkeypatch.setattr(pythoncom, "GetRunningObjectTable", lambda: Rot())
    monkeypatch.setattr(win32com.client, "Dispatch", lambda obj: obj)
    assert get_active_catia() is wanted


def test_never_falls_back_to_wrong_instance(tmp_path, monkeypatch):
    temp = tmp_path / "cattemp"
    temp.mkdir()
    monkeypatch.setenv("CATIA_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("CATIA_REQUIRED_CATTEMP", str(temp))
    monkeypatch.setattr(win32com.client, "GetActiveObject", lambda _: _app(tmp_path / "old"))
    monkeypatch.setattr(pythoncom, "GetRunningObjectTable", lambda: SimpleNamespace(
        EnumRunning=lambda: SimpleNamespace(Next=lambda _: [])))
    with pytest.raises(RuntimeError, match="No running CATIA instance"):
        get_active_catia()


def test_legacy_without_required_instance_is_unchanged(monkeypatch):
    monkeypatch.delenv("CATIA_REQUIRED_CATTEMP", raising=False)
    app = object()
    monkeypatch.setattr(win32com.client, "GetActiveObject", lambda _: app)
    assert get_active_catia() is app


def test_required_instance_outside_artifact_root_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("CATIA_ARTIFACT_ROOT", str(root))
    monkeypatch.setenv("CATIA_REQUIRED_CATTEMP", str(tmp_path))
    with pytest.raises(RuntimeError, match="outside the artifact root"):
        get_active_catia()


def test_connection_does_not_launch_or_attach_another_instance(monkeypatch):
    import catia_mcp.catia_instance
    from catia_mcp.connection import CATIAConnection

    monkeypatch.setenv("CATIA_REQUIRED_CATTEMP", "configured-by-host")
    monkeypatch.setattr(catia_mcp.catia_instance, "get_active_catia",
                        lambda: (_ for _ in ()).throw(RuntimeError("required instance unavailable")))
    monkeypatch.setattr(win32com.client, "Dispatch", lambda *_: pytest.fail("unexpected launch"))
    connection = CATIAConnection()
    with pytest.raises(RuntimeError, match="required instance unavailable"):
        connection.connect()
    assert connection.app is None
    connection.disconnect()
