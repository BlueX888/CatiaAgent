"""Adversarial-regression tests for delivery-path handling (踩坑 69)."""

from __future__ import annotations

from catia_mcp.editing.coordinator import close_document_check, delivery_conflict


class FakeDoc:
    def __init__(self, close_error: Exception | None = None) -> None:
        self.close_error = close_error
        self.closed = False

    def Close(self) -> None:
        if self.close_error:
            raise self.close_error
        self.closed = True


def test_delivery_conflict_is_silent_when_path_is_free() -> None:
    assert delivery_conflict(r"F:\p\delivery.CATPart", None) is None


def test_delivery_conflict_names_the_open_document_and_the_way_out() -> None:
    result = delivery_conflict(r"F:\p\delivery.CATPart", "delivery.CATPart")
    assert result is not None
    assert result["ok"] is False
    assert result["code"] == "SAVE_CONFLICT"
    message = result["message"]
    assert "delivery.CATPart" in message
    assert "close" in message.lower()
    assert "different path" in message
    assert result["data"]["openDocument"] == "delivery.CATPart"
    assert result["effects"]["modified"] is False


def test_close_check_reports_success() -> None:
    doc = FakeDoc()
    check = close_document_check(doc)
    assert check == {"id": "close", "status": "pass", "evidence": {}}
    assert doc.closed is True


def test_close_check_reports_failure_instead_of_swallowing_it() -> None:
    doc = FakeDoc(close_error=RuntimeError("document is in a command"))
    check = close_document_check(doc)
    assert check["id"] == "close"
    assert check["status"] == "fail"
    assert "document is in a command" in check["evidence"]["error"]
    assert doc.closed is False
