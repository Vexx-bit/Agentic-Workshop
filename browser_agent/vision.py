"""Vision fallback: read a page screenshot with Gemini when the DOM fails.

This is deliberately a *separate, explicit* code path. The agent is instructed
to try the accessibility snapshot first (`browser_snapshot` / `browser_find`)
and only call this tool when the target content is genuinely absent from the
snapshot (canvas, image-only content, custom-rendered widgets).
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from .config import ARTIFACT_DIR, VISION_MODEL

_IMAGE_SUFFIXES = (".png", ".jpeg", ".jpg", ".webp")


def _latest_screenshot() -> Path | None:
    candidates = [
        p
        for p in ARTIFACT_DIR.glob("**/*")
        if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def read_screenshot_with_vision(question: str, filename: str = "") -> dict:
    """Answers a question about a page screenshot using a vision model.

    Use this ONLY as a fallback after the accessibility snapshot did not contain
    the information you needed. Before calling this, take a screenshot with the
    `browser_take_screenshot` tool.

    Args:
        question (str): What to look for or read in the screenshot, e.g.
            "What is the total price shown in the cart?".
        filename (str): Optional screenshot file name previously passed to
            `browser_take_screenshot`. Leave empty to use the most recent
            screenshot in the artifact directory.

    Returns:
        dict: status plus either the extracted `answer` or an `error_message`.
    """
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:  # pragma: no cover
        return {
            "status": "error",
            "error_message": f"google-genai is not available: {exc}",
        }

    path: Path | None
    if filename:
        candidate = Path(filename)
        path = candidate if candidate.is_absolute() else (ARTIFACT_DIR / filename)
        if not path.exists():
            path = _latest_screenshot()
    else:
        path = _latest_screenshot()

    if path is None or not path.exists():
        return {
            "status": "error",
            "error_message": (
                "No screenshot found. Call browser_take_screenshot first, then "
                "retry the vision fallback."
            ),
        }

    mime = mimetypes.guess_type(path.name)[0] or "image/png"

    try:
        client = genai.Client()
        response = client.models.generate_content(
            model=VISION_MODEL,
            contents=[
                types.Part.from_bytes(data=path.read_bytes(), mime_type=mime),
                (
                    "You are reading a screenshot of a web page for a browser "
                    "automation agent. Answer strictly from what is visible. "
                    "If the answer is not visible, say exactly: NOT_VISIBLE.\n\n"
                    f"Question: {question}"
                ),
            ],
        )
        answer = (response.text or "").strip()
    except Exception as exc:
        return {
            "status": "error",
            "error_message": f"Vision call failed: {exc}",
        }

    if not answer or answer == "NOT_VISIBLE":
        return {
            "status": "not_found",
            "screenshot": str(path),
            "error_message": "The requested content was not visible in the screenshot.",
        }

    return {"status": "success", "screenshot": str(path), "answer": answer}
