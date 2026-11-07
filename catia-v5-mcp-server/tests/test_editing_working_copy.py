"""Working-copy creation must not depend on CATIA's save authorization (踩坑 66)."""

from __future__ import annotations

from pathlib import Path

import pytest

from catia_mcp.editing.session import create_working_copy

SOURCE_BYTES = b"CATPart-fixture-bytes"


class FakeDoc:
    def __init__(self, full_name: str | None = None) -> None:
        self._full_name = full_name
        self.closed = False
        self.activated = False
        self.saved_as: str | None = None

    @property
    def FullName(self) -> str:
        if self._full_name is None:
            raise RuntimeError("document was never bound to a file")
        return self._full_name

    def SaveAs(self, path: str) -> None:  # pragma: no cover - overridden per test
        self.saved_as = path

    def Close(self) -> None:
        self.closed = True

    def Activate(self) -> None:
        self.activated = True


class FakeDocuments:
    def __init__(self, *, new_from_error=None, save_error=None, open_error=None, save_writes=True):
        self.new_from_error = new_from_error
        self.save_error = save_error
        self.open_error = open_error
        self.save_writes = save_writes
        self.opened: list[FakeDoc] = []
        self.new_from_calls = 0

    def NewFrom(self, source: str) -> FakeDoc:
        self.new_from_calls += 1
        if self.new_from_error:
            raise self.new_from_error
        doc = FakeDoc()
        doc.SaveAs = lambda path: self._save(doc, path)  # type: ignore[method-assign]
        return doc

    def _save(self, doc: FakeDoc, path: str) -> None:
        if self.save_error:
            raise self.save_error
        doc.saved_as = path
        if self.save_writes:
            Path(path).write_bytes(b"saved-by-catia")
            object.__setattr__(doc, "_full_name", path)

    def Open(self, path: str) -> FakeDoc:
        if self.open_error:
            raise self.open_error
        doc = FakeDoc(path)
        self.opened.append(doc)
        return doc


class FakeApp:
    def __init__(self, documents: FakeDocuments) -> None:
        self.Documents = documents
        self.DisplayFileAlerts = True


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    path = tmp_path / "Fixture.CATPart"
    path.write_bytes(SOURCE_BYTES)
    return path


def test_save_as_success_keeps_new_from_identity(source: Path, tmp_path: Path) -> None:
    documents = FakeDocuments()
    app = FakeApp(documents)

    doc, working_path, copied = create_working_copy(app, str(source), str(tmp_path / "work"), "s1")

    assert copied is False
    assert documents.new_from_calls == 1
    assert doc.saved_as == working_path
    assert app.DisplayFileAlerts is True


def test_forward_slash_work_dir_is_normalized_before_save_as(source: Path, tmp_path: Path) -> None:
    documents = FakeDocuments()
    app = FakeApp(documents)
    forward_source = str(source).replace("\\", "/")
    forward_work = str(tmp_path / "work").replace("\\", "/")

    doc, working_path, copied = create_working_copy(app, forward_source, forward_work, "s-slash")

    assert copied is False
    assert doc.saved_as == working_path
    assert "/" not in working_path
    assert Path(working_path).exists()


def test_save_as_denied_falls_back_to_byte_copy(source: Path, tmp_path: Path) -> None:
    denied = Exception("(-2147352567, '发生意外。', ..., '方法 SaveAs 失败')")
    documents = FakeDocuments(save_error=denied)
    app = FakeApp(documents)

    doc, working_path, copied = create_working_copy(app, str(source), str(tmp_path / "work"), "s2")

    assert copied is True
    assert Path(working_path).read_bytes() == SOURCE_BYTES
    assert doc.FullName == working_path
    assert documents.opened and documents.opened[-1] is doc
    assert app.DisplayFileAlerts is True


def test_new_from_unavailable_also_uses_byte_copy(source: Path, tmp_path: Path) -> None:
    documents = FakeDocuments(new_from_error=RuntimeError("no NewFrom in this build"))
    app = FakeApp(documents)

    _, working_path, copied = create_working_copy(app, str(source), str(tmp_path / "work"), "s3")

    assert copied is True
    assert Path(working_path).read_bytes() == SOURCE_BYTES


def test_both_paths_failing_reports_both_reasons(source: Path, tmp_path: Path) -> None:
    documents = FakeDocuments(save_error=RuntimeError("saveas refused"), open_error=RuntimeError("lock held"))
    app = FakeApp(documents)

    with pytest.raises(RuntimeError) as excinfo:
        create_working_copy(app, str(source), str(tmp_path / "work"), "s4")

    message = str(excinfo.value)
    assert "saveas refused" in message
    assert "lock held" in message
    assert app.DisplayFileAlerts is True


def test_existing_working_copy_path_is_refused(source: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / f"{source.stem}-s5.CATPart").write_bytes(b"already here")
    app = FakeApp(FakeDocuments())

    with pytest.raises(FileExistsError):
        create_working_copy(app, str(source), str(work), "s5")
