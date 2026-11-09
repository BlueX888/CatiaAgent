"""Structural contracts for the CATIA MCP boundary.

Importing this package must stay free of pywin32, COM, file I/O and the
execution layer. Contract families live in sibling modules and build on these
primitives.
"""

from .base import (
    CanonicalModel,
    ContractModel,
    WireFloat,
    WireInt,
    WireModel,
    is_explicit_null,
    present_fields,
)
from .errors import (
    ContractError,
    PydanticValidationError,
    problems_from_pydantic,
    validate_contract,
)

__all__ = [
    "CanonicalModel",
    "ContractError",
    "ContractModel",
    "PydanticValidationError",
    "WireFloat",
    "WireInt",
    "WireModel",
    "is_explicit_null",
    "present_fields",
    "problems_from_pydantic",
    "validate_contract",
]
