"""Answers waiting to be collected, per student.

This lives outside server/main.py because two different entry points now produce
answers for the same student: the WhatsApp webhook, and the upload page where a
voice note or photo arrives. Both need to hand work to the same queue, and a
queue owned by the webhook module would force the upload page to import it - or
worse, to keep a second queue that 'more' would never find.

In-memory is correct for this deployment: the service runs single-instance by
design (min=max=1), and a queued answer is worthless after a restart anyway.

Chunking and WhatsApp formatting happen here rather than at each call site,
because forgetting either produces a message WhatsApp silently truncates.
"""

from __future__ import annotations

import logging

from server import format as fmt
from server import whatsapp

logger = logging.getLogger(__name__)

# sender -> message chunks still to deliver
_PENDING: dict[str, list[str]] = {}

# sender -> label of the work currently running, so 'more' can say what it is
# waiting on instead of claiming nothing is queued
_INFLIGHT: dict[str, str] = {}


def stash_chunks(sender: str, chunks: list[str]) -> None:
    if chunks:
        _PENDING.setdefault(sender, []).extend(chunks)


def stash_text(sender: str, text: str) -> None:
    """Formats, chunks and queues an answer for later collection."""
    stash_chunks(sender, whatsapp.chunk(fmt.for_chat(text)))


def stash_labelled(sender: str, label: str, text: str) -> None:
    """Queues an answer tagged with what produced it.

    Without the label a late answer arrives after the student has already asked
    something else, and reads as though the assistant ignored the question -
    which is exactly how it looked in testing.
    """
    stash_text(sender, f"*Re:* {label}\n\n{text}")
    logger.info("queued a late answer for %s", sender)


def pop(sender: str, limit: int) -> list[str]:
    """Takes up to `limit` chunks, appending a nudge if more remain."""
    queue = _PENDING.get(sender) or []
    if not queue:
        return []
    taken, rest = queue[:limit], queue[limit:]
    if rest:
        _PENDING[sender] = rest
        taken = taken[:-1] + [taken[-1] + f"\n\n(...{len(rest)} more - send: more)"]
    else:
        _PENDING.pop(sender, None)
    return taken


def has_pending(sender: str) -> bool:
    return bool(_PENDING.get(sender))


def set_inflight(sender: str, label: str) -> None:
    _INFLIGHT[sender] = label


def clear_inflight(sender: str) -> None:
    _INFLIGHT.pop(sender, None)


def inflight(sender: str) -> str | None:
    return _INFLIGHT.get(sender)
