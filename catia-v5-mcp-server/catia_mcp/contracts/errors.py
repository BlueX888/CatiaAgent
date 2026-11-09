"""Structural error mapping that preserves the existing business error shape.

The server already imports ``jsonschema.ValidationError``. Contracts therefore
export Pydantic's exception under an explicit alias so both can coexist while
the service entry is migrated in shadow mode.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from .base import ContractModel


class ContractError(ValueError):
    """A structural contract violation with code/message/path."""

    def __init__(self, code: str, message: str, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path

    def to_problem(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path}


def _path(location: tuple[Any, ...]) -> str:
    parts: list[str] = []
    for item in location:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        else:
            parts.append(("." if parts else "") + str(item))
    return "".join(parts)


def problems_from_pydantic(error: PydanticValidationError, *,
                           code: str = "INVALID_ARGUMENT") -> list[dict[str, str]]:
    """Translate a Pydantic validation error into the existing problem shape.

    The caller keeps its own business codes; structural failures use
    ``INVALID_ARGUMENT`` unless a narrower code is explicitly supplied.
    """
    problems: list[dict[str, str]] = []
    for item in error.errors():
        problems.append({
            "code": code,
            "message": str(item.get("msg") or "invalid value"),
            "path": _path(tuple(item.get("loc") or ())),
        })
    return problems


def validate_contract(model: type[ContractModel], value: Any) -> ContractModel:
    """Validate and return the model, raising ContractError on structure failure."""
    try:
        return model.model_validate(value)
    except PydanticValidationError as error:
        problem = problems_from_pydantic(error)[0]
        raise ContractError(problem["code"], problem["message"], problem["path"]) from error
