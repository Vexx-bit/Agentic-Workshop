"""Moodle web-service layer for the WhatsApp agent.

Why REST instead of the browser
-------------------------------
This Moodle reports ``limitconcurrentlogins: 1``: one interactive session per
user. Driving it with Playwright would open a second session and can evict the
student from their own browser mid-class. Web-service token calls do not go
through the interactive session path, so every Moodle interaction lives in this
module and none of it goes through the browser toolset. That is also what makes
many students at once possible: token calls are stateless, so twenty students
can be served concurrently without fighting over one session.

One token per student
---------------------
Each student links their own account through the HTTPS page in
``server/link.py``. Their token is stored under an opaque key (a salted hash of
their phone number) and carries only their own permissions, so the agent cannot
read another student's coursework even if it tried. The key is read from the ADK
session, never from a model argument, so the model cannot ask for someone
else's data by guessing a key.

Safety model
------------
1. ``ALLOWED_FUNCTIONS`` is a server-side allowlist. The model cannot reach a
   wsfunction outside this set, however it is prompted.
2. ``DENIED_FUNCTIONS`` is a second, independent check covering the functions
   that must never be callable: quiz attempts, coursework submission, grade
   writes. It is deliberately redundant with the allowlist, so that a careless
   future edit to the allowlist still cannot unlock them.
3. Moodle reports failures as HTTP 200 with an ``exception`` key, so every
   response is inspected for that key rather than trusting the status code.
4. Writes are separate tools with distinct names, so the existing
   ``require_confirmation`` guardrail can gate each one individually.

What this module deliberately cannot do
---------------------------------------
It cannot submit coursework, start or answer a quiz, or write a grade. The
helpful thing is the opposite direction: fetch the brief, the questions and the
notes, so the student can do the work and submit it themselves.
"""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any

import requests

from . import config, store

REST_PATH = "/webservice/rest/server.php"
TOKEN_PATH = "/login/token.php"
MOBILE_SERVICE = "moodle_mobile_app"
HTTP_TIMEOUT = 30
FILE_TIMEOUT = 60

# ADK session-state key holding this student's opaque key. Set by server/runner.
USER_KEY_STATE = "user_key"

# ---------------------------------------------------------------------------
# Allowlist / denylist
# ---------------------------------------------------------------------------

READ_FUNCTIONS = frozenset(
    {
        "core_webservice_get_site_info",
        "core_enrol_get_users_courses",
        "core_course_get_contents",
        "core_course_get_course_module",
        "core_calendar_get_action_events_by_timesort",
        "core_completion_get_activities_completion_status",
        "mod_assign_get_assignments",
        "mod_assign_get_submission_status",
    }
)

WRITE_FUNCTIONS = frozenset(
    {
        "core_completion_update_activity_completion_status_manually",
        "core_calendar_create_calendar_events",
        "core_calendar_delete_calendar_events",
    }
)

ALLOWED_FUNCTIONS = READ_FUNCTIONS | WRITE_FUNCTIONS

# Never callable. Redundant with the allowlist on purpose.
DENIED_FUNCTIONS = frozenset(
    {
        # Quiz attempts. This site runs quizaccess_proctoring_*; an agent must
        # never be able to touch a proctored attempt.
        "mod_quiz_start_attempt",
        "mod_quiz_save_attempt",
        "mod_quiz_process_attempt",
        "mod_quiz_get_attempt_data",
        "mod_lesson_launch_attempt",
        "mod_lesson_process_page",
        "mod_lesson_finish_attempt",
        # Coursework submission. Irreversible against real marked work.
        "mod_assign_save_submission",
        "mod_assign_submit_for_grading",
        "mod_assign_start_submission",
        "mod_assign_remove_submission",
        "mod_workshop_add_submission",
        "mod_workshop_update_submission",
        "mod_workshop_delete_submission",
        "mod_checkmark_submit",
        "mod_questionnaire_submit_questionnaire_response",
        # Grade writes.
        "mod_assign_save_grade",
        "mod_assign_save_grades",
        "mod_assign_reveal_identities",
        "core_grades_grader_gradingpanel_point_store",
        "core_grades_grader_gradingpanel_scale_store",
        "core_competency_grade_competency_in_course",
        "mod_workshop_evaluate_assessment",
        "mod_workshop_evaluate_submission",
        # Destructive, public, or policy-level.
        "core_message_delete_message_for_all_users",
        "core_message_send_instant_messages",
        "enrol_self_enrol_user",
        "tool_policy_set_acceptances_status",
        "core_user_agree_site_policy",
        "mod_forum_add_discussion",
        "mod_forum_add_discussion_post",
        "mod_forum_delete_post",
    }
)

# Error codes that mean "this student's token is no longer usable". There is no
# way to refresh silently, because we never keep the password. The honest
# response is to ask them to link again.
RELINK_CODES = frozenset({"invalidtoken", "accessexception", "sessionexpired"})


class MoodleError(RuntimeError):
    """A structured failure returned by Moodle, or refused before sending."""

    def __init__(self, errorcode: str, message: str) -> None:
        super().__init__(f"{errorcode}: {message}")
        self.errorcode = errorcode
        self.message = message

    @property
    def needs_relink(self) -> bool:
        return self.errorcode in RELINK_CODES


# ---------------------------------------------------------------------------
# Per-student tokens
# ---------------------------------------------------------------------------


def _key(tool_context: Any = None) -> str:
    """Reads this student's opaque key out of the ADK session state.

    Deliberately not a model-supplied argument: if the model could pass a key,
    it could be talked into passing someone else's.
    """
    if tool_context is None:
        return ""
    state = getattr(tool_context, "state", None)
    if state is None:
        return ""
    try:
        return str(state.get(USER_KEY_STATE) or "")
    except Exception:
        return ""


def set_token_for(user_key: str, token: str, moodle_userid: Any = None) -> None:
    """Registers a student's own Moodle token under an opaque key."""
    store.put_token(user_key, token, moodle_userid)


def forget_token(user_key: str) -> bool:
    """Deletes a student's stored token. Backs the `unlink` command."""
    return store.delete_token(user_key)


def has_token(user_key: str) -> bool:
    return bool(store.get_token(user_key) or config.MOODLE_TOKEN)


def _token_for(user_key: str | None) -> str:
    if user_key:
        token = store.get_token(user_key)
        if token:
            return token
    # Single-token fallback, for local dev and the solo demo path.
    return config.MOODLE_TOKEN


def _base_url() -> str:
    base = (config.MOODLE_BASE_URL or "").rstrip("/")
    if not base:
        raise MoodleError("noconfig", "MOODLE_BASE_URL is not set.")
    return base


def exchange_password_for_token(username: str, password: str) -> str:
    """Trades a username and password for a Moodle web-service token.

    The password is used once, inside this function, and is never stored,
    logged, or returned. Call this only from the HTTPS link page - never from a
    chat message, because chat messages are stored on the phone and in Twilio's
    logs, and can be read later.
    """
    response = requests.post(
        _base_url() + TOKEN_PATH,
        data={
            "username": username,
            "password": password,
            "service": MOBILE_SERVICE,
        },
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("token")
    if not token:
        raise MoodleError(
            payload.get("errorcode", "tokenfailed"),
            payload.get("error", "Moodle refused the login."),
        )
    return str(token)


def whoami(user_key: str) -> dict:
    """Confirms a freshly stored token works, and returns who it belongs to.

    Used by the link page to show the student their own name, which is a much
    better confirmation than the word 'success'.
    """
    info = _call("core_webservice_get_site_info", user_key=user_key)
    return {
        "userid": info.get("userid"),
        "fullname": info.get("fullname"),
        "username": info.get("username"),
    }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _flatten(params: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flattens nested params into Moodle's ``events[0][name]`` form."""
    flat: dict[str, Any] = {}
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten(value, name))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    flat.update(_flatten(item, f"{name}[{index}]"))
                else:
                    flat[f"{name}[{index}]"] = item
        elif isinstance(value, bool):
            flat[name] = 1 if value else 0
        elif value is not None:
            flat[name] = value
    return flat


def _call(
    wsfunction: str,
    params: dict[str, Any] | None = None,
    user_key: str | None = None,
) -> Any:
    """Calls a single allowlisted wsfunction and returns the decoded body."""
    if wsfunction in DENIED_FUNCTIONS:
        raise MoodleError(
            "blocked",
            f"{wsfunction} is permanently blocked by this agent and will never run.",
        )
    if wsfunction not in ALLOWED_FUNCTIONS:
        raise MoodleError(
            "blocked",
            f"{wsfunction} is not on this agent's allowlist.",
        )

    token = _token_for(user_key)
    if not token:
        raise MoodleError("notoken", "This student has not linked Moodle yet.")

    payload: dict[str, Any] = {
        "wstoken": token,
        "moodlewsrestformat": "json",
        "wsfunction": wsfunction,
    }
    payload.update(_flatten(params or {}))

    response = requests.post(
        _base_url() + REST_PATH, data=payload, timeout=HTTP_TIMEOUT
    )
    response.raise_for_status()
    body = response.json()

    # Moodle signals failure with HTTP 200 plus an `exception` key. This is the
    # single most common source of silent bugs against this API.
    if isinstance(body, dict) and "exception" in body:
        raise MoodleError(
            str(body.get("errorcode", "unknown")),
            str(body.get("message", "Moodle rejected the call.")),
        )
    return body


def _user_id(user_key: str | None = None) -> int:
    info = _call("core_webservice_get_site_info", user_key=user_key)
    return int(info["userid"])


def _fail(error: MoodleError) -> dict:
    """Turns a MoodleError into a result the model can explain to the user."""
    if error.errorcode == "notoken":
        return {
            "status": "link_required",
            "error_message": (
                "This student has not linked their Moodle account yet. Call "
                "link_my_moodle and send them the link. Never ask for a "
                "password in chat."
            ),
        }
    if error.needs_relink:
        return {
            "status": "relink_required",
            "error_message": (
                "This student's Moodle link is no longer valid. Call "
                "link_my_moodle and send them a fresh link, and do not ask "
                "for their password in chat."
            ),
        }
    return {"status": "error", "error_code": error.errorcode, "error_message": error.message}


# ---------------------------------------------------------------------------
# Expiring media registry
# ---------------------------------------------------------------------------
#
# A Moodle file URL only works with the wstoken appended. Handing that URL to
# Twilio would give a live credential to a third party and write it into their
# logs, so we never do. Files are registered in the shared store and served
# from our own domain by server/media.py until the entry expires.


def register_file(
    file_url: str,
    filename: str,
    mimetype: str = "",
    user_key: str = "",
) -> str:
    media_id = secrets.token_urlsafe(24)
    store.put_media(
        media_id,
        {
            "url": file_url,
            "filename": filename,
            "mimetype": mimetype,
            "token": _token_for(user_key or None),
        },
        config.MOODLE_MEDIA_TTL_SECONDS,
    )
    return media_id


def fetch_media(media_id: str) -> tuple[bytes, str, str]:
    """Downloads a registered file from Moodle, server-side.

    Raises:
        KeyError: if the id is unknown or has expired.
    """
    entry = store.get_media(media_id)
    if not entry:
        raise KeyError(media_id)

    url = str(entry.get("url") or "")
    # Moodle file URLs frequently already carry ?forcedownload=1.
    joiner = "&" if "?" in url else "?"
    response = requests.get(
        url + joiner + "token=" + str(entry.get("token") or ""), timeout=FILE_TIMEOUT
    )
    response.raise_for_status()
    return (
        response.content,
        str(entry.get("filename") or "file"),
        str(entry.get("mimetype") or ""),
    )


def media_path(media_id: str) -> str:
    base = (config.PUBLIC_BASE_URL or "").rstrip("/")
    return f"{base}/media/{media_id}" if base else f"/media/{media_id}"


# ---------------------------------------------------------------------------
# Linking tools
# ---------------------------------------------------------------------------


def link_my_moodle(tool_context: Any = None) -> dict:
    """Creates a private, single-use link for this student to connect Moodle.

    Use this whenever a Moodle tool reports status link_required or
    relink_required. The link opens a page on this service where the student
    types their own Moodle username and password once. Never ask for a
    password in the chat itself.

    Returns:
        dict: status, the link to send, and how long it stays valid.
    """
    user_key = _key(tool_context)
    if not user_key:
        return {
            "status": "error",
            "error_message": (
                "This session has no per-student key, so no link can be issued."
            ),
        }

    base = (config.PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        return {
            "status": "error",
            "error_message": "PUBLIC_BASE_URL is not set, so the link page has no address.",
        }

    nonce = store.new_link_nonce(user_key)
    return {
        "status": "success",
        "link": f"{base}/link/{nonce}",
        "expires_in_minutes": max(1, store.LINK_TTL_SECONDS // 60),
        "already_linked": bool(store.get_token(user_key)),
        "message": (
            "Send this link to the student as-is. It works once and then dies. "
            "Tell them their password is exchanged for a token and discarded "
            "immediately, and that they can send 'unlink' at any time."
        ),
    }


def unlink_my_moodle(tool_context: Any = None) -> dict:
    """Forgets this student's stored Moodle token.

    After this the agent can no longer see any of their coursework until they
    link again. It does not change anything inside Moodle itself.

    Returns:
        dict: status and a short confirmation.
    """
    user_key = _key(tool_context)
    if not user_key:
        return {"status": "error", "error_message": "No per-student key in this session."}

    if forget_token(user_key):
        return {
            "status": "success",
            "message": "Moodle token deleted. This student is no longer linked.",
        }
    return {"status": "not_linked", "message": "There was no stored token to delete."}


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


def list_my_courses(tool_context: Any = None) -> dict:
    """Lists the Moodle units this student is enrolled in, with progress.

    Hidden site-admin courses (orientation, contacts, timetables) are filtered
    out, because they are noise for a student asking about their units.

    Returns:
        dict: status, plus a list of units with id, code, name and percent.
    """
    user_key = _key(tool_context)
    try:
        courses = _call(
            "core_enrol_get_users_courses",
            {"userid": _user_id(user_key or None)},
            user_key=user_key or None,
        )
    except MoodleError as error:
        return _fail(error)

    units = []
    for course in courses:
        if course.get("hidden"):
            continue
        progress = course.get("progress")
        units.append(
            {
                "course_id": course.get("id"),
                "code": course.get("shortname"),
                "name": (course.get("fullname") or "").strip(),
                "percent_complete": round(progress, 1) if isinstance(progress, (int, float)) else None,
            }
        )
    return {"status": "success", "units": units}


def whats_due_soon(days_ahead: int = 14, tool_context: Any = None) -> dict:
    """Lists upcoming Moodle deadlines across all of this student's units.

    An empty list is a real answer, not a failure: late in a semester every
    deadline can already be behind them. Say so plainly rather than guessing.

    Args:
        days_ahead (int): How far forward to look, in days.

    Returns:
        dict: status, plus a list of events with name, unit and due date.
    """
    user_key = _key(tool_context)
    now = int(time.time())
    try:
        payload = _call(
            "core_calendar_get_action_events_by_timesort",
            {
                "timesortfrom": now,
                "timesortto": now + max(1, days_ahead) * 86400,
                "limitnum": 20,
            },
            user_key=user_key or None,
        )
    except MoodleError as error:
        return _fail(error)

    events = []
    for event in payload.get("events", []):
        course = event.get("course") or {}
        timestamp = event.get("timesort") or event.get("timestart")
        events.append(
            {
                "name": event.get("name"),
                "unit": course.get("shortname") or course.get("fullname"),
                "due_iso": _iso(timestamp),
                "activity": event.get("modulename"),
            }
        )
    return {"status": "success", "days_ahead": days_ahead, "events": events}


def list_course_notes(course_id: int, tool_context: Any = None) -> dict:
    """Lists the downloadable notes and slides in one unit.

    Each file gets a short-lived link on this service's own domain. The Moodle
    token stays server-side and is never included in the link.

    Args:
        course_id (int): Moodle course id, from list_my_courses.

    Returns:
        dict: status, plus sections each holding files with name, size and link.
    """
    user_key = _key(tool_context)
    try:
        sections = _call(
            "core_course_get_contents",
            {"courseid": course_id},
            user_key=user_key or None,
        )
    except MoodleError as error:
        return _fail(error)

    result = []
    for section in sections:
        files = []
        for module in section.get("modules", []):
            # Respect Moodle's own visibility decision. Restricted activities
            # must not be surfaced just because they appear in the payload.
            if not module.get("uservisible", True):
                continue
            for item in module.get("contents", []) or []:
                if item.get("type") != "file" or not item.get("fileurl"):
                    continue
                media_id = register_file(
                    item["fileurl"],
                    item.get("filename", "file"),
                    item.get("mimetype", ""),
                    user_key=user_key,
                )
                files.append(
                    {
                        "activity": module.get("name"),
                        "filename": item.get("filename"),
                        "size_bytes": item.get("filesize"),
                        "link": media_path(media_id),
                    }
                )
        if files:
            result.append({"section": section.get("name"), "files": files})
    return {"status": "success", "course_id": course_id, "sections": result}


def whats_new_in_unit(course_id: int, tool_context: Any = None) -> dict:
    """Summarises the most recent topics in a unit: objectives plus notes.

    Use this for questions like "what is mobile programming about now" or
    "what did we cover last week". It returns the lecturer's own objectives
    text for the latest topics and download links for that week's material, so
    the student can study offline.

    Args:
        course_id (int): Moodle course id, from list_my_courses.

    Returns:
        dict: status, plus the latest topics with objectives text and files.
    """
    user_key = _key(tool_context)
    try:
        sections = _call(
            "core_course_get_contents",
            {"courseid": course_id},
            user_key=user_key or None,
        )
    except MoodleError as error:
        return _fail(error)

    topics = []
    # Moodle returns sections in course order, so the newest teaching weeks are
    # at the end. Skip section 0 (General) and any admin-only section.
    for section in reversed(sections):
        if len(topics) >= 3:
            break
        if not section.get("uservisible", True):
            continue
        notes, objectives = [], []
        for module in section.get("modules", []) or []:
            if not module.get("uservisible", True):
                continue
            if module.get("modname") == "label":
                text = _plain(module.get("description", ""))
                if text:
                    objectives.append(text)
                continue
            for item in module.get("contents", []) or []:
                if item.get("type") != "file" or not item.get("fileurl"):
                    continue
                media_id = register_file(
                    item["fileurl"],
                    item.get("filename", "file"),
                    item.get("mimetype", ""),
                    user_key=user_key,
                )
                notes.append(
                    {
                        "filename": item.get("filename"),
                        "size_bytes": item.get("filesize"),
                        "link": media_path(media_id),
                    }
                )
        if objectives or notes:
            topics.append(
                {
                    "topic": section.get("name"),
                    "objectives": objectives,
                    "files": notes,
                }
            )
    return {"status": "success", "course_id": course_id, "latest_topics": topics}


def get_assignment_brief(course_id: int, tool_context: Any = None) -> dict:
    """Fetches the assignment questions and submission rules for one unit.

    This is the read side of coursework: the questions, the deadline, what file
    types the lecturer accepts, the size cap, and links to the brief documents.
    The student does the work and submits it themselves - this agent cannot
    submit, and must never imply that it did.

    Args:
        course_id (int): Moodle course id, from list_my_courses.

    Returns:
        dict: status, plus assignments with questions, format rules and files.
    """
    user_key = _key(tool_context)
    try:
        payload = _call(
            "mod_assign_get_assignments",
            {"courseids": [course_id]},
            user_key=user_key or None,
        )
    except MoodleError as error:
        return _fail(error)

    items = []
    for course in payload.get("courses", []) or []:
        for assignment in course.get("assignments", []) or []:
            cfg = {
                (c.get("plugin"), c.get("name")): c.get("value")
                for c in assignment.get("configs") or []
            }
            max_bytes = cfg.get(("file", "maxsubmissionsizebytes"))
            files = []
            for group in ("introattachments", "activityattachments"):
                for item in assignment.get(group) or []:
                    if not item.get("fileurl"):
                        continue
                    media_id = register_file(
                        item["fileurl"],
                        item.get("filename", "file"),
                        item.get("mimetype", ""),
                        user_key=user_key,
                    )
                    files.append(
                        {
                            "filename": item.get("filename"),
                            "size_bytes": item.get("filesize"),
                            "link": media_path(media_id),
                        }
                    )

            questions = " ".join(
                part
                for part in (
                    _plain(assignment.get("intro", ""), 500),
                    _plain(assignment.get("activity", ""), 1800),
                )
                if part
            ).strip()

            items.append(
                {
                    "name": assignment.get("name"),
                    "cmid": assignment.get("cmid"),
                    "due_iso": _iso(assignment.get("duedate")),
                    "cutoff_iso": _iso(assignment.get("cutoffdate")),
                    "submission_required": not bool(assignment.get("nosubmissions")),
                    "accepted_file_types": cfg.get(("file", "filetypeslist")) or "",
                    "accepts_typed_text": cfg.get(("onlinetext", "enabled")) == "1",
                    "max_upload_mb": round(int(max_bytes) / 1048576, 1) if max_bytes else None,
                    "questions": questions,
                    "brief_files": files,
                }
            )

    # Live work first: nearest real deadline, then undated, then the past.
    now = int(time.time())

    def _order(item: dict) -> tuple:
        due = item.get("due_iso")
        if not due:
            return (1, "")
        return (0 if _epoch_safe(due) >= now else 2, due)

    items.sort(key=_order)
    return {"status": "success", "course_id": course_id, "assignments": items}


def list_manual_activities(course_id: int, tool_context: Any = None) -> dict:
    """Lists activities in a unit whose completion the student can tick manually.

    Only activities reported with manual tracking can be changed by
    mark_activity_done. Everything Moodle completes automatically - viewing a
    file, submitting work, attempting a quiz - is read-only here and will be
    refused by the site if attempted.

    Args:
        course_id (int): Moodle course id.

    Returns:
        dict: status, plus manual activities with cmid, type and done flag.
    """
    user_key = _key(tool_context)
    try:
        payload = _call(
            "core_completion_get_activities_completion_status",
            {"courseid": course_id, "userid": _user_id(user_key or None)},
            user_key=user_key or None,
        )
    except MoodleError as error:
        return _fail(error)

    manual, automatic = [], 0
    for status in payload.get("statuses", []):
        if status.get("tracking") == 1:
            manual.append(
                {
                    "cmid": status.get("cmid"),
                    "type": status.get("modname"),
                    "done": bool(status.get("state")),
                }
            )
        else:
            automatic += 1
    return {
        "status": "success",
        "course_id": course_id,
        "manual_activities": manual,
        "automatic_count": automatic,
    }


# ---------------------------------------------------------------------------
# Write tools (guardrail-gated)
# ---------------------------------------------------------------------------


def mark_activity_done(cmid: int, done: bool = True, tool_context: Any = None) -> dict:
    """Ticks or unticks a manually-completed Moodle activity. Reversible.

    This only affects the student's own completion tick. It does not submit
    work, does not touch a grade, and can be undone by calling it again with
    done set to false.

    Args:
        cmid (int): Course-module id, from list_manual_activities.
        done (bool): True to tick, False to untick.

    Returns:
        dict: status and a short confirmation.
    """
    user_key = _key(tool_context)
    try:
        result = _call(
            "core_completion_update_activity_completion_status_manually",
            {"cmid": cmid, "completed": 1 if done else 0},
            user_key=user_key or None,
        )
    except MoodleError as error:
        return _fail(error)

    if not result.get("status"):
        return {
            "status": "error",
            "error_message": (
                "Moodle refused the change. This activity is most likely "
                "completed automatically rather than by hand."
            ),
        }
    return {
        "status": "success",
        "cmid": cmid,
        "done": done,
        "message": "Marked done in Moodle." if done else "Unmarked in Moodle.",
    }


def create_reminder(
    title: str,
    when_iso: str,
    note: str = "",
    tool_context: Any = None,
) -> dict:
    """Creates a private reminder in this student's own Moodle calendar.

    The event is visible only to the student, appears on their Moodle
    dashboard, and can be deleted again.

    Args:
        title (str): Short reminder title.
        when_iso (str): When to remind, ISO-8601, for example 2026-08-30T09:00.
        note (str): Optional longer description.

    Returns:
        dict: status and the created event id.
    """
    user_key = _key(tool_context)
    try:
        timestamp = _epoch(when_iso)
    except ValueError:
        return {
            "status": "error",
            "error_message": f"Could not read '{when_iso}' as a date and time.",
        }

    try:
        payload = _call(
            "core_calendar_create_calendar_events",
            {
                "events": [
                    {
                        "name": title,
                        "description": note,
                        "format": 1,
                        "eventtype": "user",
                        "timestart": timestamp,
                        "timeduration": 0,
                        "visible": 1,
                    }
                ]
            },
            user_key=user_key or None,
        )
    except MoodleError as error:
        return _fail(error)

    events = payload.get("events", [])
    if not events:
        return {
            "status": "error",
            "error_message": "Moodle accepted the call but created no event.",
        }
    return {
        "status": "success",
        "event_id": events[0].get("id"),
        "when_iso": _iso(timestamp),
        "message": "Reminder added to the Moodle calendar.",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ENTITIES = (
    ("&nbsp;", " "),
    ("\u00a0", " "),
    ("&amp;", "&"),
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&quot;", '"'),
    ("&#39;", "'"),
)


def _plain(html: str, limit: int = 700) -> str:
    """Turns Moodle's stored HTML into text safe to send over WhatsApp."""
    text = _TAG_RE.sub(" ", html or "")
    for needle, replacement in _ENTITIES:
        text = text.replace(needle, replacement)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "\u2026"
    return text


def _iso(timestamp: Any) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()


def _epoch(value: str) -> int:
    text = (value or "").strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _epoch_safe(value: str) -> int:
    try:
        return _epoch(value)
    except ValueError:
        return 0


# Wire these into the agent with:  tools=[..., *MOODLE_TOOLS]
MOODLE_TOOLS = [
    link_my_moodle,
    unlink_my_moodle,
    list_my_courses,
    whats_due_soon,
    whats_new_in_unit,
    list_course_notes,
    get_assignment_brief,
    list_manual_activities,
    mark_activity_done,
    create_reminder,
]

# Names the guardrail must always gate. Kept here next to the tools so the two
# lists cannot drift apart unnoticed.
MOODLE_WRITE_TOOL_NAMES = frozenset({"mark_activity_done", "create_reminder"})
