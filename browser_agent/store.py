"""Shared state for multi-user linking: per-student tokens, link nonces, files.

Why this module exists
----------------------
A dict on one process is not a store. Cloud Run can run several instances and
recycles them whenever it likes, so:

- the HTTPS link page can be served by instance A while the student's next
  WhatsApp message is handled by instance B, and
- every restart would silently un-link every student.

Anything that must outlive a single request goes through here.

Two backends, chosen by the TOKEN_STORE env var:

- ``memory`` (default): a process dict. Correct only while the service runs a
  single instance, which is what ``--min-instances=1 --max-instances=1`` gives
  you. Cheapest option, no extra Google API, fine for a demo.
- ``firestore``: one small document per record. Survives restarts and
  scale-out, and sits inside the Firestore free tier at classroom scale. The
  import is lazy, so the memory path never pays for it.

Privacy
-------
Records are keyed by ``user_key_for(sender)``: a salted SHA-256 of the phone
number, truncated. Set USER_KEY_PEPPER and a dump of the store cannot be
reversed into a list of student phone numbers.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from typing import Any

logger = logging.getLogger(__name__)

BACKEND = os.getenv("TOKEN_STORE", "memory").strip().lower()
# Salt for the user key. Set this in Secret Manager; changing it un-links
# everyone, which is also a blunt way to revoke every student at once.
PEPPER = os.getenv("USER_KEY_PEPPER", "")
COLLECTION = os.getenv("FIRESTORE_COLLECTION", "agent_links")

# A link page is single-use and short-lived: the window in which a stolen URL
# is worth anything should be measured in minutes.
LINK_TTL_SECONDS = int(os.getenv("LINK_TTL_SECONDS", "600"))
# How long a student stays linked before they must link again.
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "2592000"))  # 30 days
MAX_LINK_ATTEMPTS = 5

# Token reads happen on every Moodle call. Cache briefly so a chatty turn is
# one store read, not six.
_CACHE_SECONDS = 60

TOKEN_KIND = "token"
NONCE_KIND = "nonce"
MEDIA_KIND = "media"


def user_key_for(sender: str) -> str:
    """Opaque, stable key for one WhatsApp sender.

    Not reversible into a phone number without PEPPER.
    """
    raw = f"{PEPPER}|{sender or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _MemoryBackend:
    name = "memory"

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def get(self, kind: str, key: str) -> dict | None:
        row = self._rows.get((kind, key))
        if not row:
            return None
        if row["expires"] and row["expires"] < time.time():
            self._rows.pop((kind, key), None)
            return None
        return dict(row["value"])

    def put(self, kind: str, key: str, value: dict, ttl_seconds: int) -> None:
        self._rows[(kind, key)] = {
            "value": dict(value),
            "expires": time.time() + ttl_seconds if ttl_seconds else 0.0,
        }

    def delete(self, kind: str, key: str) -> bool:
        return self._rows.pop((kind, key), None) is not None


class _FirestoreBackend:
    name = "firestore"

    def __init__(self) -> None:
        # Lazy import: the memory path must not require this dependency, an
        # enabled API, or IAM.
        from google.cloud import firestore

        self._client = firestore.Client()

    def _doc(self, kind: str, key: str):
        return self._client.collection(COLLECTION).document(f"{kind}__{key}")

    def get(self, kind: str, key: str) -> dict | None:
        snapshot = self._doc(kind, key).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        expires = float(data.get("expires") or 0)
        if expires and expires < time.time():
            self.delete(kind, key)
            return None
        return dict(data.get("value") or {})

    def put(self, kind: str, key: str, value: dict, ttl_seconds: int) -> None:
        self._doc(kind, key).set(
            {
                "value": dict(value),
                "expires": time.time() + ttl_seconds if ttl_seconds else 0.0,
            }
        )

    def delete(self, kind: str, key: str) -> bool:
        self._doc(kind, key).delete()
        return True


def _make_backend():
    if BACKEND == "firestore":
        try:
            backend = _FirestoreBackend()
            logger.info("link store: firestore collection %s", COLLECTION)
            return backend
        except Exception:
            # Losing the store is bad; losing the whole service is worse.
            logger.exception("firestore store unavailable, falling back to memory")
    logger.info("link store: in-process memory (single instance only)")
    return _MemoryBackend()


_backend = _make_backend()
_token_cache: dict[str, tuple[float, str]] = {}


def backend_name() -> str:
    return getattr(_backend, "name", "unknown")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def put_token(user_key: str, token: str, moodle_userid: Any = None) -> None:
    """Stores one student's Moodle web-service token under their opaque key."""
    _backend.put(
        TOKEN_KIND,
        user_key,
        {"token": token, "moodle_userid": moodle_userid, "linked_at": time.time()},
        TOKEN_TTL_SECONDS,
    )
    _token_cache[user_key] = (time.time() + _CACHE_SECONDS, token)


def get_token(user_key: str) -> str | None:
    if not user_key:
        return None
    cached = _token_cache.get(user_key)
    if cached and cached[0] > time.time():
        return cached[1]
    record = _backend.get(TOKEN_KIND, user_key)
    token = str(record.get("token")) if record and record.get("token") else None
    if token:
        _token_cache[user_key] = (time.time() + _CACHE_SECONDS, token)
    else:
        _token_cache.pop(user_key, None)
    return token


def delete_token(user_key: str) -> bool:
    """Revokes a student locally. Returns True if they were linked."""
    was_linked = bool(get_token(user_key))
    _token_cache.pop(user_key, None)
    _backend.delete(TOKEN_KIND, user_key)
    return was_linked


# ---------------------------------------------------------------------------
# Link nonces
# ---------------------------------------------------------------------------


def new_link_nonce(user_key: str) -> str:
    """Mints a single-use, short-lived id for one student's link page."""
    nonce = secrets.token_urlsafe(32)
    _backend.put(
        NONCE_KIND,
        nonce,
        {"user_key": user_key, "attempts": 0, "created": time.time()},
        LINK_TTL_SECONDS,
    )
    return nonce


def read_nonce(nonce: str) -> dict | None:
    if not nonce:
        return None
    return _backend.get(NONCE_KIND, nonce)


def bump_nonce_attempt(nonce: str) -> int:
    """Counts a failed attempt. Returns attempts used so far, or -1 if gone."""
    record = read_nonce(nonce)
    if not record:
        return -1
    attempts = int(record.get("attempts") or 0) + 1
    record["attempts"] = attempts
    _backend.put(NONCE_KIND, nonce, record, LINK_TTL_SECONDS)
    return attempts


def consume_nonce(nonce: str) -> str | None:
    """Burns the nonce and returns the user key it belonged to."""
    record = read_nonce(nonce)
    _backend.delete(NONCE_KIND, nonce)
    return str(record.get("user_key")) if record else None


# ---------------------------------------------------------------------------
# Media records
# ---------------------------------------------------------------------------
#
# A Moodle file URL only works with a token appended, so the token never leaves
# this service. The file is registered here and served from our own domain.


def put_media(media_id: str, record: dict, ttl_seconds: int) -> None:
    _backend.put(MEDIA_KIND, media_id, record, ttl_seconds)


def get_media(media_id: str) -> dict | None:
    if not media_id:
        return None
    return _backend.get(MEDIA_KIND, media_id)


def drop_media(media_id: str) -> None:
    _backend.delete(MEDIA_KIND, media_id)
