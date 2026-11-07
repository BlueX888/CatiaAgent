"""MCP tools for the controlled edit workflow (catia-edit/1)."""

from __future__ import annotations

from typing import Any

from catia_mcp.connection import CATIAConnection
from catia_mcp.editing.coordinator import EditCoordinator

# While an edit session owns a model, every tool that is not on this read-only
# list is refused. A deny-list drifts every time a new tool is registered
# (GSD/drawing writers resolve the active document, which is the working copy),
# so the safe default is "read-only unless proven otherwise".
READ_ONLY_TOOLS = {
    "catia_connect",
    "catia_disconnect",
    "catia_list_documents",
    "catia_get_active_document_info",
    "catia_diagnose",
    "catia_get_inertia",
    "catia_get_bounding_box",
    "catia_get_parameters",
    "catia_measure_distance",
    "catia_list_features",
    "catia_list_faces",
    "catia_list_edges",
    "catia_list_relations",
    "catia_sketch_get_geometry",
    "catia_begin_edit",
    "catia_inspect_edit",
    "catia_compile_intent",
    "catia_get_task_context",
    "catia_find_cases",
    "catia_get_edit_history",
    "catia_apply_edit_plan",
    "catia_revert_last_edit",
    "catia_get_edit_status",
    "catia_finalize_edit",
    "catia_recover_edit",
    # PDF drawing intake and plan compilation are read-only: they may read the
    # drawing and produce a plan, but cannot touch the model owned by a session.
    "catia_inspect_drawing",
    "catia_crop_drawing",
    "catia_validate_requirements",
    "catia_compile_build_plan",
}


class EditingTools:
    def __init__(self, connection: CATIAConnection, journal=None) -> None:
        self.conn = connection
        # Supplying a journal avoids opening the machine-level default database
        # (acceptance/tests use an isolated journal per case).
        self.coordinator = EditCoordinator(connection, journal)

    # ------------------------------------------------------------------
    def get_tool_definitions(self) -> list[dict[str, Any]]:
        plan_schema = {
            "type": "object",
            "description": (
                "catia-edit-plan/1. changes[] target writable driver handles taken from the "
                "snapshot; acceptance[] only adds assertions and can never replace the "
                "server-side response checks."
            ),
            "properties": {
                "schema": {"type": "string", "enum": ["catia-edit-plan/1"]},
                "editSessionId": {"type": "string"},
                "operationId": {"type": "string"},
                "expectedRevision": {"type": "integer"},
                "intent": {"type": "object"},
                "changes": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "parameter": {"type": "string"},
                            "oldValue": {"type": "number"},
                            "newValue": {"type": "number"},
                            "unit": {"type": "string", "enum": ["mm", "deg", "none"]},
                            "reason": {"type": "string"},
                        },
                        "required": ["parameter", "oldValue", "newValue", "unit"],
                    },
                },
                "preserve": {"type": "array", "items": {"type": "object"}},
                "acceptance": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["schema", "editSessionId", "operationId", "expectedRevision", "changes"],
        }
        return [
            {
                "name": "catia_begin_edit",
                "description": (
                    "Bind a saved .CATPart to a server-owned working copy and return a "
                    "model snapshot (parameters, relations, geometry, holes, measured "
                    "dependencies). The source file is never opened or written."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sourcePath": {"type": "string", "description": "Absolute path of the .CATPart"},
                        "operationId": {"type": "string", "description": "Optional caller token"},
                        "workingDir": {
                            "type": "string",
                            "description": "Optional absolute directory for the working copy",
                        },
                    },
                    "required": ["sourcePath"],
                },
            },
            {
                "name": "catia_inspect_edit",
                "description": (
                    "Re-read the bound working model and return an up-to-date snapshot "
                    "(read-only; no writes)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"editSessionId": {"type": "string"}},
                    "required": ["editSessionId"],
                },
            },
            {
                "name": "catia_compile_intent",
                "description": (
                    "Compile a cad-intent-draft/1 into a validated catia-edit-plan/1 using the "
                    "current snapshot and its measured perturbation evidence. Binding, unit "
                    "conversion, relative arithmetic, scope feasibility and preserve checks are "
                    "done deterministically. Read-only: it never changes the model. The decision "
                    "is planned / needs_clarification / unsupported / invalid; only submit the "
                    "returned plan when decision is planned. After a needs_clarification, the "
                    "same session cannot compile a plan until the user answers: pass "
                    "userConfirmed=true plus the user's reply in confirmationText."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "editSessionId": {"type": "string"},
                        "intent": {
                            "type": "object",
                            "description": (
                                "cad-intent-draft/1. target.parameter is an exact snapshot handle, "
                                "target.attribute is the user's quantity (length/width/thickness/"
                                "diameter/radius/angle), operation.type is set/increase/decrease, "
                                "operation.scope is overall/each_side/single_side, reference.basis "
                                "is current/original, and preserve is a list of "
                                "center_position / hole_edge_distance / hole_centers / "
                                "hole_world_position / other_dimensions."
                            ),
                            "properties": {
                                "schema": {"type": "string", "enum": ["cad-intent-draft/1"]},
                                "text": {"type": "string"},
                                "decision": {
                                    "type": "string",
                                    "enum": ["planned", "needs_clarification", "unsupported"],
                                },
                                "target": {"type": "object"},
                                "operation": {"type": "object"},
                                "reference": {"type": "object"},
                                "preserve": {"type": "array"},
                                "unresolved": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["schema", "target", "operation"],
                        },
                        "operationId": {
                            "type": "string",
                            "description": "Optional plan operation id; generated deterministically when omitted",
                        },
                        "userText": {
                            "type": "string",
                            "description": (
                                "Read-only. Injected by the host from the current user message; "
                                "do not fill or rewrite it. Used to verify intent.text and to "
                                "clear a pending clarification."
                            ),
                        },
                        "userConfirmed": {
                            "type": "boolean",
                            "description": (
                                "Set only after the user answered a pending clarification in this "
                                "session. Requires confirmationText."
                            ),
                        },
                        "confirmationText": {
                            "type": "string",
                            "description": (
                                "The user's answer to the pending clarification, copied verbatim "
                                "from the current user message."
                            ),
                        },
                    },
                    "required": ["editSessionId", "intent"],
                },
                # The host replaces userText with the current user message; the
                # model cannot paraphrase it. See PilotDeck PluginToToolBridge.
                "_meta": {"pilotdeck/injectUserText": True},
            },
            {
                "name": "catia_apply_edit_plan",
                "description": (
                    "Validate and execute a catia-edit-plan/1 on the bound working copy, then "
                    "read back parameters and geometry and return a verdict "
                    "(PASS / NO_EFFECT / WRONG_EFFECT / INCONCLUSIVE / ALREADY_SATISFIED)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "editSessionId": {"type": "string"},
                        "plan": plan_schema,
                    },
                    "required": ["editSessionId", "plan"],
                },
            },
            {
                "name": "catia_get_edit_status",
                "description": "Read the business state, operation ids and recent events of an edit session (no COM writes).",
                "inputSchema": {
                    "type": "object",
                    "properties": {"editSessionId": {"type": "string"}},
                    "required": ["editSessionId"],
                },
            },
            {
                "name": "catia_finalize_edit",
                "description": (
                    "Save the verified working model to a new delivery path, reopen it from "
                    "disk, re-verify it, and write JSON + Markdown reports. Never overwrites."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "editSessionId": {"type": "string"},
                        "deliveryPath": {"type": "string", "description": "Absolute .CATPart path"},
                    },
                    "required": ["editSessionId", "deliveryPath"],
                },
            },
            {
                "name": "catia_recover_edit",
                "description": (
                    "Reconcile an interrupted or failed operation against the real model: "
                    "assess (and when asked, roll back once) and prove the result by readback."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "editSessionId": {"type": "string"},
                        "dryRun": {"type": "boolean", "default": True},
                    },
                    "required": ["editSessionId"],
                },
            },
            {
                "name": "catia_get_task_context",
                "description": (
                    "Read-only task-scoped selection from the current snapshot: the exact "
                    "candidate handles/values/units, measured circle roles and sketch frames, "
                    "the relations that touch those handles, preserve capabilities and "
                    "limitations. It keeps a pointer to the full snapshot (catia_inspect_edit) "
                    "instead of replacing it. Use this instead of re-reading the whole snapshot "
                    "when the task is a single dimension or hole-size change."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "editSessionId": {"type": "string"},
                        "attribute": {
                            "type": "string",
                            "description": "length/width/height/thickness/diameter/radius/angle (optional)",
                        },
                        "text": {
                            "type": "string",
                            "description": "User's words (optional); only used to prefer holes or an additive profile",
                        },
                        "operationType": {"type": "string"},
                        "includeCases": {"type": "boolean", "default": False},
                    },
                    "required": ["editSessionId"],
                },
            },
            {
                "name": "catia_find_cases",
                "description": (
                    "Read-only retrieval from the verified case library, filtered by structural "
                    "compatibility (roles, feature kinds, sketch planes) and operation, never by "
                    "text similarity. Returns plan templates without handles or values; always "
                    "recompile against the current snapshot."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "editSessionId": {"type": "string"},
                        "attribute": {"type": "string"},
                        "operationType": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["editSessionId"],
                },
            },
            {
                "name": "catia_get_edit_history",
                "description": (
                    "Read-only multi-turn state: recorded operations with their before/after values "
                    "and revisions, the begin_edit baseline (reference.basis=original), current "
                    "values and whether the last operation can be reverted."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"editSessionId": {"type": "string"}},
                    "required": ["editSessionId"],
                },
            },
            {
                "name": "catia_revert_last_edit",
                "description": (
                    "Write: restore the parameter values recorded before the last verified PASS, "
                    "through the normal apply path (new revision, validation, rollback and "
                    "independent assertions). Requires confirm=true and a verified session."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "editSessionId": {"type": "string"},
                        "confirm": {"type": "boolean"},
                    },
                    "required": ["editSessionId", "confirm"],
                },
            },
        ]

    # ------------------------------------------------------------------
    def active_session_ids(self) -> list[str]:
        """Sessions that really own an open working document.

        A session whose working copy is no longer open cannot protect anything,
        so it is abandoned here instead of blocking the server forever.
        """
        from catia_mcp.editing.session import resolve_document

        # "failed"/"rolled_back" still own the working copy: the model may need
        # recovery or finalize, so generic writers must stay out. "rejected" is
        # only a plan-level verdict, and "abandoned"/"completed" no longer hold
        # the document.
        owning_states = (
            "ready",
            "verified",
            "executing",
            "saving",
            "recovering",
            "unknown",
            "failed",
            "rolled_back",
        )
        active = []
        for row in self.coordinator.journal.list_sessions():
            if row["status"] not in owning_states:
                continue
            record = self.coordinator.journal.load_session(row["session_id"])
            if not record:
                continue
            try:
                is_open = resolve_document(self.coordinator.conn.app, record["workingCopy"]["path"]) is not None
            except Exception:
                # "Could not look" is not "not open": stay exclusive.
                is_open = True
            if not is_open:
                record["status"] = "abandoned"
                self.coordinator.journal.save_session(record)
                self.coordinator.journal.append_event(
                    "session_abandoned", {"reason": "working copy is not open"}, session_id=row["session_id"]
                )
                continue
            active.append(row["session_id"])
        return active

    def guard(self, tool_name: str) -> dict[str, Any] | None:
        """Server-side write exclusivity: only read-only tools pass while a session owns a model."""
        if tool_name in READ_ONLY_TOOLS:
            return None
        active = self.active_session_ids()
        if not active:
            return None
        return {
            "ok": False,
            "code": "BUSY",
            "operation_id": None,
            "tool": tool_name,
            "message": (
                "an edit session is active ("
                + ", ".join(active)
                + "); use the catia_*_edit workflow instead of generic write tools"
            ),
            "data": {"activeSessions": active},
            "effects": {"modified": False},
            "diagnostics": [],
            "warnings": [],
            "recovery": {"rollback_supported": False, "automatic_retry_allowed": False, "next_actions": []},
        }

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.conn.ensure_connected()
        match tool_name:
            case "catia_begin_edit":
                return self.coordinator.begin_edit(
                    arguments["sourcePath"],
                    arguments.get("operationId"),
                    arguments.get("workingDir"),
                )
            case "catia_inspect_edit":
                return self.coordinator.inspect_edit(arguments["editSessionId"])
            case "catia_compile_intent":
                return self.coordinator.compile_intent(
                    arguments["editSessionId"],
                    arguments["intent"],
                    arguments.get("operationId"),
                    user_text=arguments.get("userText"),
                    user_confirmed=arguments.get("userConfirmed") is True,
                    confirmation_text=arguments.get("confirmationText"),
                )
            case "catia_get_task_context":
                return self.coordinator.get_task_context(
                    arguments["editSessionId"],
                    attribute=arguments.get("attribute"),
                    text=arguments.get("text"),
                    operation_type=arguments.get("operationType"),
                    include_cases=arguments.get("includeCases") is True,
                )
            case "catia_find_cases":
                return self.coordinator.find_cases(
                    arguments["editSessionId"],
                    attribute=arguments.get("attribute"),
                    operation_type=arguments.get("operationType"),
                    limit=int(arguments.get("limit") or 5),
                )
            case "catia_get_edit_history":
                return self.coordinator.get_history(arguments["editSessionId"])
            case "catia_apply_edit_plan":
                return self.coordinator.apply_edit_plan(arguments["editSessionId"], arguments["plan"])
            case "catia_revert_last_edit":
                return self.coordinator.revert_last_edit(
                    arguments["editSessionId"], bool(arguments.get("confirm"))
                )
            case "catia_get_edit_status":
                return self.coordinator.get_status(arguments["editSessionId"])
            case "catia_finalize_edit":
                return self.coordinator.finalize_edit(arguments["editSessionId"], arguments["deliveryPath"])
            case "catia_recover_edit":
                return self.coordinator.recover_edit(arguments["editSessionId"], bool(arguments.get("dryRun", True)))
            case _:
                raise ValueError(f"Unknown editing tool: {tool_name}")
