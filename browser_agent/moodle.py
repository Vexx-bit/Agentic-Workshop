"""Moodle web-service layer for the WhatsApp agent.

Why REST instead of the browser
-------------------------------
This Moodle reports ``limitconcurrentlogins: 1``: one interactive session per
user. Driving it with Playwright would open a second session and can evict the
student from their own browser mid-class. Web-service token calls do not go
through the interactive session path, so every Moodle interaction lives in this
module and none of it goes through the browser toolset.

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
It cannot submit coursework, start or answer a quiz, or write a grade. Those
functions are exposed by the site's mobile service, and a student token would
be refused for most of them anyway, but they are blocked here explicitly so the
answer does not depend on Moodle's capability checks holding.
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import Any

import requests

from . import config

REST_PATH = "/webservice/rest/server.php"
TOKEN_PATH = "/login/token.php"
MOBILE_SERVICE = "moodle_mobile_app"
HTTP_TIMEOUT = 30
FILE_TIMEOUT = 60

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
# response is to ask them to re-link.
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
# Token provision
# ---------------------------------------------------------------------------
#
# Phase 1: one token from the environment, so the demo runs today.
# Phase 2: the link-exchange flow calls set_token_for() with a token minted from
#          the student's own password, keyed by an opaque per-student key (a
#          hash of their WhatsApp number). Only this section changes; every tool
#          below already threads user_key through.
#
# A student's token carries only that student's permissions, so the agent cannot
# read another student's coursework even if it tried.

_TOKEN_STORE: dict[str, str] = {}


def set_token_for(user_key: str, token: str) -> None:
    """Registers a student's own Moodle token under an opaque key."""
    _TOKEN_STORE[user_key] = token


def forget_token(user_key: str) -> bool:
    """Deletes a student's stored token. Backs the `forget me` command."""
    return _TOKEN_STORE.pop(user_key, None) is not None


def has_token(user_key: str) -> bool:
    return bool(_TOKEN_STORE.get(user_key) or config.MOODLE_TOKEN)


def _token_for(user_key: str | None) -> str:
    if user_key and user_key in _TOKEN_STORE:
        return _TOKEN_STORE[user_key]
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
    chat message, because chat messages are logged and can be read later.
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
        raise MoodleError("notoken", "No Moodle token is available for this user.")

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
    if error.needs_relink:
        return {
            "status": "relink_required",
            "error_message": (
                "This student's Moodle link is no longer valid. Ask them to "
                "re-link, and do not ask for their password in chat."
            ),
        }
    return {"status": "error", "error_code": error.errorcode, "error_message": error.message}


# ---------------------------------------------------------------------------
# Expiring media registry
# ---------------------------------------------------------------------------
#
# A Moodle file URL only works with the wstoken appended. Handing that URL to
# Twilio would give a live credential to a third party and write it into their
# logs, so we never do. Files are registered here and served from our own
# domain by server/media.py until the entry expires.

_MEDIA: dict[str, dict[str, Any]] = {}


def register_file(
    file_url: str,
    filename: str,
    mimetype: str = "",
    user_key: str = "",
) -> str:
    _prune_media()
    media_id = secrets.token_urlsafe(24)
    _MEDIA[media_id] = {
        "url": file_url,
        "filename": filename,
        "mimetype": mimetype,
        "token": _token_for(user_key or None),
        "expires": time.time() + config.MOODLE_MEDIA_TTL_SECONDS,
    }
    return media_id


def _prune_media() -> None:
    now = time.time()
    for media_id in [k for k, v in _MEDIA.items() if v["expires"] < now]:
        _MEDIA.pop(media_id, None)


def fetch_media(media_id: str) -> tuple[bytes, str, str]:
    """Downloads a registered file from Moodle, server-side.

    Raises:
        KeyError: if the id is unknown or has expired.
    """
    entry = _MEDIA.get(media_id)
    if not entry or entry["expires"] < time.time():
        _MEDIA.pop(media_id, None)
        raise KeyError(media_id)

    url = entry["url"]
    # Moodle file URLs frequently already carry ?forcedownload=1.
    joiner = "&" if "?" in url else "?"
    response = requests.get(
        url + joiner + "token=" + entry["token"], timeout=FILE_TIMEOUT
    )
    response.raise_for_status()
    return response.content, entry["filename"], entry["mimetype"]


def media_path(media_id: str) -> str:
    base = (config.PUBLIC_BASE_URL or "").rstrip("/")
    return f"{base}/media/{media_id}" if base else f"/media/{media_id}"


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------


def list_my_courses(user_key: str = "") -> dict:
    """Lists the Moodle units this student is enrolled in, with progress.

    Hidden site-admin courses (orientation, contacts, timetables) are filtered
    out, because they are noise for a student asking about their units.

    Args:
        user_key (str): Opaque per-student key. Empty string uses the single
            configured token.

    Returns:
        dict: status, plus a list of units with id, code, name and percent.
    """
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


def whats_due_soon(days_ahead: int = 14, user_key: str = "") -> dict:
    """Lists upcoming Moodle deadlines across all of this student's units.

    Args:
        days_ahead (int): How far forward to look, in days.
        user_key (str): Opaque per-student key.

    Returns:
        dict: status, plus a list of events with name, unit and due timestamp.
    """
    now = int(time.time())
    try:
        payload = _call(
            "core_calendar_get_action_events_by_timesort",
            {
                "timesortfrom": now,
                "timesortto": now + days_ahead * 86400,
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
                "cmid": (event.get("action") or {}).get("itemcount") and event.get("instance"),
            }
        )
    return {"status": "success", "days_ahead": days_ahead, "events": events}


def list_course_notes(course_id: int, user_key: str = "") -> dict:
    """Lists the downloadable notes and slides in one unit.

    Each file gets a short-lived link on this service's own domain. The Moodle
    token stays server-side and is never included in the link.

    Args:
        course_id (int): Moodle course id, from list_my_courses.
        user_key (str): Opaque per-student key.

    Returns:
        dict: status, plus sections each holding files with name, size and link.
    """
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


def list_manual_activities(course_id: int, user_key: str = "") -> dict:
    """Lists activities in a unit whose completion the student can tick manually.

    Only activities reported with manual tracking can be changed by
    mark_activity_done. Everything Moodle completes automatically - viewing a
    file, submitting work, attempting a quiz - is read-only here and will be
    refused by the site if attempted.

    Args:
        course_id (int): Moodle course id.
        user_key (str): Opaque per-student key.

    Returns:
        dict: status, plus manual activities with cmid, type and done flag.
    """
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


def mark_activity_done(cmid: int, done: bool = True, user_key: str = "") -> dict:
    """Ticks or unticks a manually-completed Moodle activity. Reversible.

    This only affects the student's own completion tick. It does not submit
    work, does not touch a grade, and can be undone by calling it again with
    done set to false.

    Args:
        cmid (int): Course-module id, from list_manual_activities.
        done (bool): True to tick, False to untick.
        user_key (str): Opaque per-student key.

    Returns:
        dict: status and a short confirmation.
    """
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
    user_key: str = "",
) -> dict:
    """Creates a private reminder in this student's own Moodle calendar.

    The event is visible only to the student, appears on their Moodle
    dashboard, and can be deleted again.

    Args:
        title (str): Short reminder title.
        when_iso (str): When to remind, ISO-8601, for example 2026-08-30T09:00.
        note (str): Optional longer description.
        user_key (str): Opaque per-student key.

    Returns:
        dict: status and the created event id.
    """
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


# Wire these into the agent with:  tools=[..., *MOODLE_TOOLS]
MOODLE_TOOLS = [
    list_my_courses,
    whats_due_soon,
    list_course_notes,
    list_manual_activities,
    mark_activity_done,
    create_reminder,
]

# Names the guardrail must always gate. Kept here next to the tools so the two
# lists cannot drift apart unnoticed.
MOODLE_WRITE_TOOL_NAMES = frozenset({"mark_activity_done", "create_reminder"})
