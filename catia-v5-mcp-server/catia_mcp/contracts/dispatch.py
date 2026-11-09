"""Shadow structural checks for editing tool arguments.

Shadow mode never decides the outcome: jsonschema, the tool router and the
dynamic generation policy remain authoritative.  A shadow difference is only
recorded in bounded, process-local diagnostics for the parity work in T16.

Only explicitly registered tools are checked.  Active promotion is deliberately
not implemented here.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import StrictBool

from .base import WireModel
from .edit_plan import EditPlanWire
from .errors import PydanticValidationError, problems_from_pydantic
from .intent import IntentDraftWire


class CompileIntentInputWire(WireModel):
    editSessionId: str
    intent: IntentDraftWire
    operationId: Optional[str] = None
    userText: Optional[str] = None
    userConfirmed: Optional[StrictBool] = None
    confirmationText: Optional[str] = None


class ApplyEditPlanInputWire(WireModel):
    editSessionId: str
    plan: EditPlanWire


SHADOW_CONTRACTS = {
    "catia_compile_intent": CompileIntentInputWire,
    "catia_apply_edit_plan": ApplyEditPlanInputWire,
}


def shadow_check(tool_name: str, arguments: Any) -> dict[str, Any]:
    """Validate a registered editing payload without changing the decision."""
    model = SHADOW_CONTRACTS.get(tool_name)
    if model is None:
        return {"registered": False, "status": "not_registered", "tool": tool_name, "problems": []}
    if not isinstance(arguments, dict):
        return {
            "registered": True, "status": "difference", "tool": tool_name,
            "problems": [{"code": "INVALID_ARGUMENT", "message": "arguments must be an object", "path": ""}],
        }
    try:
        model.model_validate(arguments)
    except PydanticValidationError as error:
        return {
            "registered": True, "status": "difference", "tool": tool_name,
            "problems": problems_from_pydantic(error)[:20],
        }
    return {"registered": True, "status": "ok", "tool": tool_name, "problems": []}
