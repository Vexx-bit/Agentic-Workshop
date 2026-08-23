"""Expiring media proxy for Moodle files.

A Moodle file URL only works with the wstoken appended to it. Handing that URL
to Twilio would give a live credential to a third party and write it into their
logs, so the agent never does. Files are registered in ``browser_agent.moodle``
under a random id, and Twilio is given a link on our own domain that stops
working once the entry expires.

Wire it up in server/main.py with:

    from server.media import router as media_router
    app.include_router(media_router)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response

from browser_agent import moodle

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/media/{media_id}")
def get_media(media_id: str) -> Response:
    """Streams one registered Moodle file, then lets the link expire."""
    try:
        content, filename, mimetype = moodle.fetch_media(media_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="This link has expired.")
    except Exception:
        # Never surface the upstream error: it can contain the token.
        logger.exception("Failed to fetch Moodle media %s", media_id)
        raise HTTPException(
            status_code=502, detail="Could not fetch that file from Moodle."
        )

    return Response(
        content=content,
        media_type=mimetype or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
