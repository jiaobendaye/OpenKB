"""HTTP MCP (Model Context Protocol) server for OpenKB.

Mounts 4 read-only wiki tools onto the existing FastAPI app so any
MCP-compatible client (Claude Desktop, Cursor, Continue, etc.) can
query the knowledge base over HTTP without file-system access or a
local skill file.

Path A: the MCP ASGI app is mounted at ``/mcp`` inside
:func:`openkb.api.create_app`, sharing the same uvicorn worker, port,
and bearer-token auth as the REST API.

Endpoint: ``http://<host>:<port>/mcp``
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

from openkb.agent.tools import (
    get_wiki_page_content,
    list_wiki_files,
    read_wiki_file,
    read_wiki_image,
)
from openkb.api_helpers import _resolve_kb

# Cached FastMCP instance — created once per process, reused across
# requests. Typed as Any to avoid importing mcp at module level (the
# [web] extra is optional; the mcp import is deferred to create_mcp_server()).
_mcp_instance: Any = None


def _wiki_root_for(kb: str) -> str:
    """Resolve a KB name to its wiki root path string for tool functions."""
    return str(_resolve_kb(kb) / "wiki")


def create_mcp_server() -> Any:
    """Build (or return cached) FastMCP server with 4 wiki read tools.

    Tools mirror :mod:`openkb.agent.tools` but add a ``kb`` parameter so
    the MCP client can target any knowledge base on the server — the same
    multi-KB routing the REST API uses via :func:`_resolve_kb`.

    The ``mcp`` import is inside this function so the module imports
    cleanly without the optional ``mcp`` package installed.
    """
    global _mcp_instance
    if _mcp_instance is not None:
        return _mcp_instance

    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("openkb")

    # Set the endpoint path to "/" so the mount prefix ("/mcp") becomes
    # the full public path — clients connect to http://host:port/mcp.
    # Without this, the default "/mcp" app path would yield "/mcp/mcp".
    mcp.settings.streamable_http_path = "/"

    # Relax host-based transport security to match the REST API's model:
    # the REST API does no host checking — the bearer token (OPENKB_API_TOKEN)
    # is the security boundary, not DNS rebinding protection. Without this,
    # the MCP SDK rejects any Host header not in its default allowlist
    # (127.0.0.1 / localhost), breaking proxies and non-loopback binds.
    mcp.settings.transport_security.enable_dns_rebinding_protection = False
    mcp.settings.transport_security.allowed_hosts = ["*"]
    mcp.settings.transport_security.allowed_origins = ["*"]

    @mcp.tool()
    def list_kbs() -> str:
        """List available knowledge bases on this server.

        Call this first to discover KB names, then pass the name as the
        ``kb`` parameter to every other tool.

        Returns a text list of knowledge bases with document counts.
        """
        from openkb.api_kbs import _list_knowledge_bases

        kbs = _list_knowledge_bases()["knowledge_bases"]
        if not kbs:
            return "No knowledge bases found."
        lines = [f"{len(kbs)} knowledge base(s):"]
        for kb in kbs:
            lines.append(f"- {kb['name']} ({kb['document_count']} docs)")
        return "\n".join(lines)

    @mcp.tool()
    def list_files(directory: str, kb: str) -> str:
        """List all Markdown files in a wiki subdirectory.

        Use to discover what concept, entity, summary, or source pages
        exist before reading specific files. For a structured overview,
        read "index.md" via read_file instead.

        Args:
            directory: Subdirectory relative to wiki root
                (e.g. "concepts", "summaries", "entities", "sources").
            kb: Knowledge base name (as shown by `openkb status`).
        """
        return list_wiki_files(directory, _wiki_root_for(kb))

    @mcp.tool()
    def read_file(path: str, kb: str) -> str:
        """Read a Markdown file from the wiki.

        Start with "index.md" to see the full table of contents (all
        documents, concepts, entities with one-line briefs), then read
        specific pages by their paths.

        Args:
            path: File path relative to wiki root
                (e.g. "index.md", "concepts/attention.md",
                "summaries/paper.md", "entities/some-person.md").
            kb: Knowledge base name.
        """
        return read_wiki_file(path, _wiki_root_for(kb))

    @mcp.tool()
    def get_page_content(doc_name: str, pages: str, kb: str) -> str:
        """Get text content of specific pages from a PageIndex (long) document.

        Only for documents with doc_type: pageindex (long PDFs ≥ 20 pages).
        For short documents, use read_file with the source path instead.

        Args:
            doc_name: Document name without extension
                (e.g. "attention-is-all-you-need").
            pages: Page specification (e.g. "3-5,7,10-12").
            kb: Knowledge base name.
        """
        return get_wiki_page_content(doc_name, pages, _wiki_root_for(kb))

    @mcp.tool()
    def get_image(image_path: str, kb: str) -> str:
        """Read an image from the wiki as a base64 data URL.

        Returns a ``data:<mime>;base64,...`` string usable in <img> tags
        or decodable to binary. Use when a question asks about a specific
        figure, chart, or diagram.

        Args:
            image_path: Image path relative to wiki root, or note-relative
                as embedded in source .md pages
                (e.g. "sources/images/doc/fig1.png").
            kb: Knowledge base name.
        """
        result = read_wiki_image(image_path, _wiki_root_for(kb))
        if result["type"] == "image":
            return result["image_url"]
        return result["text"]

    @mcp.tool()
    def grep(
        pattern: str,
        kb: str,
        directory: str = "",
        case_insensitive: bool = True,
        max_results: int = 100,
        context_lines: int = 2,
    ) -> str:
        """Search wiki content with a regex pattern.

        Returns matching lines with surrounding context across all
        ``.md`` files in the wiki. Use this to find which pages mention
        a topic, verify entity coverage, or locate a phrase without
        reading every file.

        Args:
            pattern: Regex pattern (e.g. "attention mechanism").
            kb: Knowledge base name.
            directory: Optional subdirectory to limit search scope
                (e.g. "concepts", "summaries"). Empty = search all.
            case_insensitive: Default true.
            max_results: Max matches to return. Default 100.
            context_lines: Lines of context around each match. Default 2.
        """
        import re

        wiki_root = _wiki_root_for(kb)
        root = Path(wiki_root).resolve()
        search_root = root
        if directory:
            search_root = (root / directory).resolve()
            if not search_root.is_relative_to(root):
                return "Access denied: path escapes wiki root."
        if not search_root.is_dir():
            return f"Directory not found: {directory or '/'}"

        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return f"Invalid regex: {exc}"

        matches: list[dict[str, Any]] = []
        truncated = False
        for md_file in sorted(search_root.rglob("*.md")):
            if truncated:
                break
            try:
                lines = md_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel_path = str(md_file.relative_to(root))
            for i, line in enumerate(lines):
                if regex.search(line):
                    start = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)
                    context: list[str] = []
                    for j in range(start, end):
                        marker = ">>" if j == i else "  "
                        context.append(f"{marker} {j + 1}: {lines[j]}")
                    matches.append(
                        {"file": rel_path, "line": i + 1, "snippet": "\n".join(context)}
                    )
                    if len(matches) >= max_results:
                        truncated = True
                        break

        if not matches:
            return "No matches found."
        parts = [f"{len(matches)} match(es):"]
        for m in matches:
            parts.append(f"\n{m['file']}:{m['line']}\n{m['snippet']}")
        if truncated:
            parts.append(f"\n... (truncated at {max_results} results)")
        return "\n".join(parts)

    _mcp_instance = mcp
    return mcp


class BearerAuthMiddleware:
    """Raw ASGI middleware enforcing OPENKB_API_TOKEN on the MCP mount.

    Mounted sub-applications bypass FastAPI dependency injection, so
    :func:`require_bearer_token` cannot protect the MCP endpoint. This
    wrapper does the same ``hmac.compare_digest`` check at the ASGI
    layer.

    When ``OPENKB_API_TOKEN`` is unset (local-first default), the
    middleware is a pass-through — matching the REST API's opt-in auth.

    Implemented as a raw ASGI middleware (not ``BaseHTTPMiddleware``) to
    avoid response buffering that would break MCP's SSE streaming.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "https"):
            await self.app(scope, receive, send)
            return

        # Normalize empty path to "/" — Starlette's Mount("/mcp", ...) forwards
        # a request to exactly "/mcp" with path="" to the sub-app, but the MCP
        # route is at "/" and doesn't match "". Without this, POST /mcp returns
        # 405 while POST /mcp/ works. The copy avoids mutating the caller's scope.
        if scope.get("path") == "":
            scope = {**scope, "path": "/"}

        expected = os.environ.get("OPENKB_API_TOKEN")
        if expected:
            auth = ""
            for key, value in scope.get("headers", []):
                if key == b"authorization":
                    auth = value.decode("latin-1")
                    break
            token = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not token or not hmac.compare_digest(token, expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"detail":"Bearer token required."}',
                    }
                )
                return

        await self.app(scope, receive, send)


def mount_mcp_onto(app: Any, mcp_server: Any) -> None:
    """Create the MCP ASGI app and mount it at ``/mcp`` on the FastAPI app.

    Call this AFTER ``mcp_server = create_mcp_server()`` and BEFORE
    ``_mount_web_ui(app)`` — the web UI's catch-all ``/`` mount must be
    added last so it does not shadow ``/mcp``.

    A redirect from ``/mcp`` to ``/mcp/`` is added before the mount
    because Starlette's ``Mount("/mcp")`` doesn't auto-redirect exact
    prefix matches (it forwards path="" to the sub-app, which 405s).
    """
    from fastapi.responses import RedirectResponse

    @app.api_route("/mcp", methods=["GET", "POST", "DELETE", "HEAD", "OPTIONS"])
    async def _mcp_redirect() -> RedirectResponse:
        return RedirectResponse("/mcp/", status_code=307)

    mcp_asgi = mcp_server.streamable_http_app()
    app.mount("/mcp", BearerAuthMiddleware(mcp_asgi))
