"""Adapt model markdown into text that reads correctly in a chat app.

The model writes markdown by habit. WhatsApp is not markdown: bold is a single
asterisk, italics a single underscore, there are no headings, and there is no
link syntax - anything else renders literally. A real student session showed
the cost of ignoring that: answers arrived containing '**Deadline**', stray
backslash escapes in front of asterisks, and '[ASSIGNMENT I.docx](link)'
printed verbatim instead of as a tappable link.

Rather than pile more formatting rules into the prompt and hope the model obeys
them every time, the model stays free to write natural markdown and this module
normalises it at the boundary. That keeps the rules in one testable place and
works no matter which model sits behind it - which mattered when the model was
swapped mid-build.
"""

from __future__ import annotations

import re

# Inline HTML the model sometimes emits when it decides to be helpful.
_TAGS = re.compile(r"</?(?:b|strong|i|em|u|s|code|pre|br)\s*/?>", re.I)
# Markdown link: keep the label, expose the URL so the app can linkify it.
_LINK = re.compile(r"\[([^\]\n]+)\]\((\S+?)\)")
# Headings have no WhatsApp equivalent; bold is the closest honest mapping.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*$", re.M)
_BOLD_STARS = re.compile(r"\*\*(.+?)\*\*", re.S)
_BOLD_SCORES = re.compile(r"__(.+?)__", re.S)
# Backslash-escaped punctuation, which WhatsApp shows as the backslash itself.
_ESCAPED = re.compile(r"\\([*_~`\[\]()#>+\-.!])")
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.M)
_FENCE = re.compile(r"^\s*```.*$", re.M)
_TRAILING_WS = re.compile(r"[ \t]+$", re.M)
_BLANK_RUN = re.compile(r"\n{3,}")


def for_chat(text: str) -> str:
    """Normalises markdown-ish model output for a chat channel.

    Order matters: links and headings are resolved before bold, so a bolded
    heading does not end up double-marked, and escapes are removed after the
    emphasis passes so an escaped asterisk cannot be mistaken for markup.
    """
    if not text:
        return ""

    out = _TAGS.sub("", text)
    out = _FENCE.sub("", out)
    out = _LINK.sub(r"\1: \2", out)
    out = _HEADING.sub(r"*\1*", out)
    out = _BOLD_STARS.sub(r"*\1*", out)
    out = _BOLD_SCORES.sub(r"*\1*", out)
    out = _ESCAPED.sub(r"\1", out)
    out = _BULLET.sub(r"\1\u2022 ", out)
    out = _TRAILING_WS.sub("", out)
    out = _BLANK_RUN.sub("\n\n", out)
    return out.strip()
