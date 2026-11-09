"""Pure structural contract primitives.

This module deliberately depends only on the standard library and Pydantic v2.
It performs no file I/O, no COM access and imports no execution-layer module, so
it can be imported on a Linux logic-only host without pywin32.

Policy (plan section 4.3):

* wire integers follow JSON Schema's mathematical-integer reading: a finite
  integer-valued float is accepted and normalised to ``int``; booleans and
  numeric strings are not integers;
* numeric fields reject booleans, numeric strings and non-finite values;
* closed objects reject unknown fields;
* a required field that is missing is an error, while an explicitly ``null``
  value is visible through ``model_fields_set`` and is never guessed.
"""

from __future__ import annotations

import math
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict


class ContractModel(BaseModel):
    """Closed base model shared by wire and canonical structures."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        validate_assignment=True,
    )


class WireModel(ContractModel):
    """Complete accepted wire representation; aliases may widen field names."""


class CanonicalModel(ContractModel):
    """Normalised internal representation; never exported as the wire schema."""


def _wire_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("a boolean is not an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ValueError("expected a JSON integer; integer-valued floats are accepted, strings are not")


def _wire_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a JSON number; booleans and strings are not numbers")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite numbers are not accepted")
    return number


WireInt = Annotated[int, BeforeValidator(_wire_int)]
WireFloat = Annotated[float, BeforeValidator(_wire_float)]


def present_fields(model: BaseModel) -> set[str]:
    """Fields supplied by the caller, distinguishing explicit null from missing."""
    return set(model.model_fields_set)


def is_explicit_null(model: BaseModel, field: str) -> bool:
    return field in model.model_fields_set and getattr(model, field) is None
