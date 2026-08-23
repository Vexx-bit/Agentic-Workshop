"""Human-in-the-loop guardrail for state-changing actions.

Design goals:
- Reading the web is free and unblocked (snapshot, find, navigate, screenshot).
- Irreversible / side-effecting actions are blocked until the user says yes in
  the chat, per action, with the exact arguments that were approved.

Implementation is an explicit `before_tool_callback`, not a prompt instruction,
so the gate cannot be talked around by the model.
"""

from __future__ import annotations

import json
from typing import Any

# Tools that always require confirmation, regardless of arguments.
ALWAYS_CONFIRM = {
    "browser_file_upload",
    "browser_evaluate",
    "browser_run_code_unsafe",
    "browser_handle_dialog",
    "browser_drop",
    # Moodle writes. These names must stay in step with
    # browser_agent.moodle.MOODLE_WRITE_TOOL_NAMES.
    "mark_activity_done",
    "create_reminder",
}

# Tools that require confirmation only when the target looks side-effecting.
CONFIRM_IF_RISKY = {
    "browser_click",
    "browser_press_key",
    "browser_select_option",
    "browser_fill_form",
    "browser_type",
}

RISKY_KEYWORDS = (
    "submit",
    "send",
    "post",
    "buy",
    "pay",
    "purchase",
    "checkout",
    "place order",
    "order",
    "delete",
    "remove",
    "cancel",
    "confirm",
    "finish",
    "apply",
    "enroll",
    "register",
    "sign up",
    "subscribe",
    "transfer",
    "withdraw",
    "upload",
    "save",
    "publish",
)

PENDING_KEY = "pending_action"
APPROVED_KEY = "approved_action"


def _fingerprint(tool_name: str, args: dict[str, Any]) -> str:
    return f"{tool_name}::{json.dumps(args, sort_keys=True, default=str)}"


def _looks_risky(args: dict[str, Any]) -> bool:
    blob = json.dumps(args, default=str).lower()
    if "\"submit\": true" in blob:
        return True
    return any(word in blob for word in RISKY_KEYWORDS)


def needs_confirmation(tool_name: str, args: dict[str, Any]) -> bool:
    if tool_name in ALWAYS_CONFIRM:
        return True
    if tool_name in CONFIRM_IF_RISKY:
        return _looks_risky(args)
    return False


def require_confirmation(tool, args, tool_context):
    """ADK before_tool_callback. Returns a dict to short-circuit the tool call."""
    tool_name = getattr(tool, "name", "") or ""
    if not needs_confirmation(tool_name, args or {}):
        return None

    fingerprint = _fingerprint(tool_name, args or {})
    state = tool_context.state

    if state.get(APPROVED_KEY) == fingerprint:
        # Single-use approval: burn it so a second identical action re-asks.
        state[APPROVED_KEY] = None
        state[PENDING_KEY] = None
        return None

    state[PENDING_KEY] = fingerprint
    return {
        "status": "confirmation_required",
        "blocked_tool": tool_name,
        "blocked_arguments": args,
        "message": (
            "This action changes state on a real system, so it was blocked. "
            "Describe the exact action to the user in plain language, ask them "
            "to reply YES to proceed, and only then call "
            "`approve_pending_action`."
        ),
    }


def approve_pending_action(confirmed: bool, tool_context) -> dict:
    """Approves (or rejects) the action that was just blocked.

    Call this ONLY after the user has explicitly agreed in the conversation.

    Args:
        confirmed (bool): True if the user explicitly approved the action.

    Returns:
        dict: status and a short description of what happens next.
    """
    state = tool_context.state
    pending = state.get(PENDING_KEY)

    if not pending:
        return {
            "status": "error",
            "error_message": "There is no pending action awaiting confirmation.",
        }

    if not confirmed:
        state[PENDING_KEY] = None
        state[APPROVED_KEY] = None
        return {"status": "rejected", "message": "Action cancelled by the user."}

    state[APPROVED_KEY] = pending
    return {
        "status": "approved",
        "message": "Approved once. Retry the exact same tool call with identical arguments.",
    }
