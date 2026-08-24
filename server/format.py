"""Convert model output into WhatsApp's own formatting.

WhatsApp does not render markdown. It has its own, narrower syntax:

    *bold*      _italic_      ~strikethrough~      ```monospace```
    > quoted line

Anything else shows up literally, so a model that writes `**bold**` or a
`[label](url)` link produces visible punctuation noise in the chat. Stripping
markdown entirely would be safe but flat - a verbatim exam question or a
deadline reads much better when it is actually emphasised.

So this translates rather than strips: markdown emphasis becomes WhatsApp
emphasis, headings become a bold line, links become a labelled bare URL, and
bullets become a real bullet character.

Applied at the channel boundary, so the agent's own reply, the deterministic
command text and the queued 'more' chunks all go out consistently - and any
future channel gets its own translation instead of inheriting WhatsApp's.
"""

from __future__ import annotations

import re

# Code fences: keep the content, drop the language tag. WhatsApp's monospace is
# also triple-backtick, but a tag like ```python renders literally.
_FENCE_OPEN = re.compile(r"```[a-zA-Z0-9_+#-]+\n")

# Stray HTML from a model that thought it was writing for a browser.
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAGS = re.compile(r"</?(?:b|i|u|s|em|strong|code|pre|p|div|span)[^>]*>", re.IGNORECASE)

# [label](https://x) -> label on one line, bare URL on the next. WhatsApp only
# makes a bare URL tappable.
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")

# Headings have no equivalent; a bold line is the closest thing.
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*(.+?)[ \t]*#*$", re.MULTILINE)

# **bold** / __bold__ -> *bold*   (WhatsApp bold is a single asterisk)
_BOLD_STARS = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_BOLD_SCORES = re.compile(r"__(?=\S)(.+?)(?<=\S)__", re.DOTALL)

# ~~struck~~ -> ~struck~
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)

# A model hedging against markdown writes \* and \_ , which WhatsApp shows.
_ESCAPED = re.compile(r"\\([*_~`\-.#\[\]()>+])")

# Markdown bullets -> a real bullet. Numbered lists are already fine as-is.
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)

_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_RUN = re.compile(r"\n{3,}")


def for_chat(text: str) -> str:
    """Rewrites markdown-ish model output as WhatsApp-flavoured text."""
    if not text:
        return ""

    out = text.replace("\r\n", "\n")

    out = _BR.sub("\n", out)
    out = _TAGS.sub("", out)

    # Links before emphasis: a label may contain emphasis, a URL must never be
    # touched by it.
    out = _LINK.sub(lambda m: f"{m.group(1).strip()}\n{m.group(2)}", out)

    out = _FENCE_OPEN.sub("```\n", out)
    out = _HEADING.sub(lambda m: f"*{m.group(1)}*", out)

    out = _BOLD_STARS.sub(lambda m: f"*{m.group(1)}*", out)
    out = _BOLD_SCORES.sub(lambda m: f"*{m.group(1)}*", out)
    out = _STRIKE.sub(lambda m: f"~{m.group(1)}~", out)

    out = _BULLET.sub(lambda m: f"{m.group(1)}\u2022 ", out)
    out = _ESCAPED.sub(r"\1", out)

    out = _TRAILING_WS.sub("", out)
    out = _BLANK_RUN.sub("\n\n", out)
    return out.strip()
