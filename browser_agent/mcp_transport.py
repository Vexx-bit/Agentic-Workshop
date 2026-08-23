"""Transport selection for the Playwright MCP server.

Two deployment shapes, one agent:

* **Local dev** - the MCP server runs as a stdio subprocess (`npx
  @playwright/mcp`). Zero infrastructure, fastest feedback loop.
* **Cloud Run** - the browser cannot live in the same container as the agent
  (the Cloud Run source-deploy path gives us a plain Python base image with no
  Node and no Chromium). So the browser runs as its *own* Cloud Run service
  from the official `mcr.microsoft.com/playwright/mcp` image, and the agent
  talks to it over streamable HTTP.

Set `PLAYWRIGHT_MCP_URL` to switch to the remote transport. Everything above
this module - tools, instruction, guardrails - is identical either way.
"""

from __future__ import annotations

import logging

from .config import (
    ARTIFACT_DIR,
    BROWSER_ALLOWED_ORIGINS,
    BROWSER_HEADLESS,
    PLAYWRIGHT_MCP_TOKEN_AUDIENCE,
    PLAYWRIGHT_MCP_URL,
)

logger = logging.getLogger(__name__)

# --- MCP toolset import (naming differs across ADK versions) ---------------
try:  # ADK >= 1.x
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset as _McpToolset
except ImportError:  # older naming
    from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset as _McpToolset

from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams

_HttpConnectionParams = None
try:
    from google.adk.tools.mcp_tool.mcp_session_manager import (
        StreamableHTTPConnectionParams as _HttpConnectionParams,
    )
except ImportError:  # pragma: no cover - older ADK only shipped SSE
    try:
        from google.adk.tools.mcp_tool.mcp_session_manager import (
            SseConnectionParams as _HttpConnectionParams,
        )
    except ImportError:
        _HttpConnectionParams = None

from mcp import StdioServerParameters

# Only the tools this agent actually needs. A small surface keeps turns cheap
# and shrinks the blast radius of a bad model decision. Note the deliberate
# omissions: no `browser_run_code_unsafe`, no `browser_file_upload`.
PLAYWRIGHT_TOOL_FILTER = [
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_find",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_wait_for",
    "browser_take_screenshot",
    "browser_tabs",
    "browser_close",
]


def _stdio_args() -> list[str]:
    args = [
        "-y",
        "@playwright/mcp@latest",
        "--isolated",
        "--browser",
        "chromium",
        "--caps",
        "vision",
        "--output-dir",
        str(ARTIFACT_DIR),
        "--image-responses",
        "omit",  # vision fallback reads the file from disk instead
        "--viewport-size",
        "1280x900",
    ]
    if BROWSER_HEADLESS:
        args.append("--headless")
    if BROWSER_ALLOWED_ORIGINS:
        args += ["--allowed-origins", BROWSER_ALLOWED_ORIGINS]
    return args


def _id_token(audience: str) -> str | None:
    """Mint a Cloud Run ID token from the metadata server.

    Only used when the browser service is deployed privately. Returns None off
    of GCP (local dev) so the caller can fall back to no auth header.
    """
    try:
        import google.auth.transport.requests
        import google.oauth2.id_token

        request = google.auth.transport.requests.Request()
        return google.oauth2.id_token.fetch_id_token(request, audience)
    except Exception as exc:  # noqa: BLE001 - never let auth break startup
        logger.warning("Could not mint an ID token for %s: %s", audience, exc)
        return None


def build_playwright_toolset():
    """Return an McpToolset wired to whichever transport is configured."""
    if PLAYWRIGHT_MCP_URL:
        if _HttpConnectionParams is None:
            raise RuntimeError(
                "PLAYWRIGHT_MCP_URL is set but this ADK version exposes no HTTP "
                "MCP connection params. Upgrade google-adk or unset the URL to "
                "fall back to the local stdio transport."
            )
        headers: dict[str, str] = {}
        audience = PLAYWRIGHT_MCP_TOKEN_AUDIENCE or PLAYWRIGHT_MCP_URL.split("/mcp")[0]
        token = _id_token(audience)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        logger.info(
            "Playwright MCP: remote transport at %s (auth=%s)",
            PLAYWRIGHT_MCP_URL,
            "yes" if token else "no",
        )
        return _McpToolset(
            connection_params=_HttpConnectionParams(
                url=PLAYWRIGHT_MCP_URL,
                headers=headers or None,
                timeout=180,
            ),
            tool_filter=PLAYWRIGHT_TOOL_FILTER,
        )

    logger.info("Playwright MCP: local stdio transport via npx")
    return _McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="npx",
                args=_stdio_args(),
            ),
            timeout=180,
        ),
        tool_filter=PLAYWRIGHT_TOOL_FILTER,
    )
