"""Lecturer announcements, read out of the unit's forum.

Why this is a separate module
-----------------------------
Announcements are where a lecturer says "CAT moved to Thursday", "submit as PDF
 not DOCX", "lab is in Block C today". Students miss them because nobody opens
Moodle to check, so they are one of the highest-value things this agent can
read. But forums are also the most dangerous surface in Moodle: the same module
exposes add_discussion, add_discussion_post, update_discussion_post,
delete_post, set_lock_state and set_pin_state, and this site has the Open Forum
(hsuforum) variants of those too. Posting publicly to a class forum in a
student's name would be far worse than any coursework mistake.

So this module carries its own hard-coded allowlist of exactly two read
functions. Nothing else is reachable through it, whatever the model is
prompted to do, and the writes are not in ``moodle.ALLOWED_FUNCTIONS`` either,
so there is no path to them from anywhere in the agent.

It lives outside ``moodle.py`` on purpose: that module carries every working
coursework tool, and a late feature should not be able to break it.
"""

from __future__ import annotations

from typing import Any

import requests

from .moodle import (
    HTTP_TIMEOUT,
    REST_PATH,
    MoodleError,
    _base_url,
    _fail,
    _flatten,
    _iso,
    _key,
    _plain,
    _token_for,
)

# Exactly two functions, both reads. This is deliberately not shared with
# moodle.ALLOWED_FUNCTIONS: widening that set must never widen this one.
FORUM_READ_FUNCTIONS = frozenset(
    {
        "mod_forum_get_forums_by_courses",
        "mod_forum_get_forum_discussions",
    }
)

# How many forums in one unit to look at, newest-activity first, and how many
# announcements to hand back by default. WhatsApp replies stay short.
MAX_FORUMS = 3
DEFAULT_LIMIT = 3
MESSAGE_CHARS = 600


def _read(wsfunction: str, params: dict[str, Any], user_key: str | None) -> Any:
    """Calls one allowlisted forum read function."""
    if wsfunction not in FORUM_READ_FUNCTIONS:
        raise MoodleError(
            "blocked",
            f"{wsfunction} is not readable by this agent and never will be.",
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

    # Moodle reports failure as HTTP 200 plus an `exception` key.
    if isinstance(body, dict) and "exception" in body:
        raise MoodleError(
            str(body.get("errorcode", "unknown")),
            str(body.get("message", "Moodle rejected the call.")),
        )
    return body


def _forums(course_id: int, user_key: str | None) -> list[dict]:
    """Returns the unit's forums, announcement forums first.

    A Moodle course usually has one ``news`` forum ("Announcements", where only
    staff can post) plus discussion forums. The news one is what a student
    means by "has anything been announced", so it is read first.
    """
    forums = _read(
        "mod_forum_get_forums_by_courses", {"courseids": [course_id]}, user_key
    )
    if not isinstance(forums, list):
        return []

    def rank(forum: dict) -> tuple:
        is_news = 0 if forum.get("type") == "news" else 1
        return (is_news, -int(forum.get("timemodified") or 0))

    return sorted(forums, key=rank)[:MAX_FORUMS]


def whats_announced(
    course_id: int, limit: int = DEFAULT_LIMIT, tool_context: Any = None
) -> dict:
    """Reads the recent announcements a lecturer posted in one unit.

    This is where lecturers say a class moved, a deadline changed, a submission
    format changed, or an exam venue was set. Call it when a student asks what
    is new, what they missed while away, whether anything has changed, or when
    they say they heard a deadline moved - the forum is where that was said, and
    it overrides an older due date shown against the assignment.

    Read-only. This agent cannot post, reply to, edit, delete, lock or pin
    anything in a forum, and those functions are not reachable from any tool.

    Args:
        course_id (int): Moodle course id, from list_my_courses.
        limit (int): How many recent announcements to return.

    Returns:
        dict: status, plus announcements with the forum name, who posted, when,
            and the text of the post.
    """
    user_key = _key(tool_context)
    wanted = max(1, min(int(limit or DEFAULT_LIMIT), 8))

    try:
        forums = _forums(course_id, user_key or None)
    except MoodleError as error:
        return _fail(error)

    if not forums:
        return {
            "status": "success",
            "course_id": course_id,
            "announcements": [],
            "note": "This unit has no forum, so there is nothing to announce.",
        }

    posts: list[dict] = []
    for forum in forums:
        forum_id = forum.get("id")
        if not forum_id:
            continue
        try:
            # Only forumid is sent: parameter names on the paginated variants
            # differ between Moodle versions, and slicing here cannot break.
            body = _read(
                "mod_forum_get_forum_discussions", {"forumid": forum_id}, user_key or None
            )
        except MoodleError:
            # One unreadable forum (group restrictions, hidden activity) must
            # not lose the announcements from the others.
            continue

        for discussion in (body or {}).get("discussions", []) or []:
            when = discussion.get("timemodified") or discussion.get("created")
            posts.append(
                {
                    "forum": forum.get("name"),
                    "is_announcement_forum": forum.get("type") == "news",
                    "subject": (discussion.get("subject") or discussion.get("name") or "").strip(),
                    "posted_by": discussion.get("userfullname"),
                    "posted_iso": _iso(when),
                    "pinned": bool(discussion.get("pinned")),
                    "message": _plain(discussion.get("message", ""), MESSAGE_CHARS),
                    "_sort": int(when or 0),
                }
            )

    # Pinned first, then newest. Lecturers pin the thing that still matters.
    posts.sort(key=lambda item: (0 if item["pinned"] else 1, -item["_sort"]))
    for item in posts:
        item.pop("_sort", None)

    return {
        "status": "success",
        "course_id": course_id,
        "announcements": posts[:wanted],
        "total_found": len(posts),
    }


# Wire into the agent with:  tools=[..., *FORUM_TOOLS]
FORUM_TOOLS = [whats_announced]
