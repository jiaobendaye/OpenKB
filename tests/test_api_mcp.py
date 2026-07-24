"""Tests for the HTTP MCP (Model Context Protocol) endpoint at /mcp.

Covers:
  * Tool registration (4 tools: list_files, read_file, get_page_content, get_image)
  * MCP protocol flow (initialize -> tools/list -> tools/call) over SSE
  * Bearer-token auth (enforced when OPENKB_API_TOKEN is set, open when unset)
  * /mcp -> /mcp/ redirect for clients that omit the trailing slash
  * Path-traversal rejection in read_file
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openkb.api import create_app

_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture(autouse=True)
def _reset_mcp_singleton():
    """Reset the cached FastMCP instance so each test gets a fresh
    session manager (StreamableHTTPSessionManager.run() can only be
    called once per instance)."""
    import openkb.api_mcp

    openkb.api_mcp._mcp_instance = None
    yield
    openkb.api_mcp._mcp_instance = None


def _client(monkeypatch, token: str | None = "secret") -> TestClient:
    if token is None:
        monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("OPENKB_API_TOKEN", token)
    return TestClient(create_app())


def _auth(token: str = "secret") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _use_named_kb(monkeypatch, kb_dir, name: str = "test-kb") -> str:
    def resolve(kb):
        assert kb == name
        return kb_dir

    monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", resolve)
    return name


def _parse_sse(text: str) -> dict[str, Any]:
    """Extract the first JSON-RPC result from an SSE response body."""
    for line in text.split("\n"):
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(text)


def _init_request() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.1"},
        },
    }


def _mcp_session(client: TestClient, headers: dict[str, str]) -> str:
    """Initialize an MCP session and return the session ID."""
    resp = client.post("/mcp/", json=_init_request(), headers=headers)
    assert resp.status_code == 200, f"init failed: {resp.status_code} {resp.text[:200]}"
    sid = resp.headers.get("mcp-session-id")
    assert sid, "no session id in init response"
    client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**headers, "mcp-session-id": sid},
    )
    return sid


def _mcp_call(
    client: TestClient,
    sid: str,
    headers: dict[str, str],
    *,
    method: str,
    params: dict[str, Any] | None = None,
    req_id: int = 2,
) -> dict[str, Any]:
    """Send a tools/list or tools/call request and return the parsed result."""
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    resp = client.post(
        "/mcp/",
        json=body,
        headers={**headers, "mcp-session-id": sid},
    )
    assert resp.status_code == 200, f"{method} failed: {resp.status_code} {resp.text[:200]}"
    return _parse_sse(resp.text)


def test_mcp_server_registers_5_tools(monkeypatch):
    """The FastMCP server exposes exactly the 5 wiki tools."""
    import openkb.api_mcp

    openkb.api_mcp._mcp_instance = None
    mcp = openkb.api_mcp.create_mcp_server()
    tools = sorted(mcp._tool_manager._tools.keys())
    assert tools == ["get_image", "get_page_content", "list_files", "list_kbs", "read_file"]


def test_mcp_rejects_missing_token(monkeypatch, kb_dir):
    """When OPENKB_API_TOKEN is set, /mcp/ without a bearer token returns 401."""
    _use_named_kb(monkeypatch, kb_dir)
    with _client(monkeypatch, token="secret") as client:
        resp = client.post("/mcp/", json=_init_request(), headers=_MCP_HEADERS)
    assert resp.status_code == 401
    assert "Bearer" in resp.json()["detail"]


def test_mcp_rejects_wrong_token(monkeypatch, kb_dir):
    """A wrong bearer token is rejected with 401."""
    _use_named_kb(monkeypatch, kb_dir)
    with _client(monkeypatch, token="secret") as client:
        resp = client.post(
            "/mcp/",
            json=_init_request(),
            headers={**_MCP_HEADERS, **_auth("wrong")},
        )
    assert resp.status_code == 401


def test_mcp_open_when_no_token_set(monkeypatch, kb_dir):
    """With no OPENKB_API_TOKEN configured, /mcp/ is open (local-first default)."""
    _use_named_kb(monkeypatch, kb_dir)
    with _client(monkeypatch, token=None) as client:
        resp = client.post("/mcp/", json=_init_request(), headers=_MCP_HEADERS)
    assert resp.status_code == 200


def test_mcp_redirect_no_trailing_slash(monkeypatch, kb_dir):
    """POST /mcp (no trailing slash) redirects to /mcp/ so clients don't 405."""
    _use_named_kb(monkeypatch, kb_dir)
    with _client(monkeypatch, token="secret") as client:
        resp = client.post(
            "/mcp",
            json=_init_request(),
            headers={**_MCP_HEADERS, **_auth()},
            follow_redirects=False,
        )
    assert resp.status_code == 307
    assert resp.headers["location"] == "/mcp/"


def test_mcp_tools_list(monkeypatch, kb_dir):
    """tools/list returns all 5 tools with names and descriptions."""
    _use_named_kb(monkeypatch, kb_dir)
    headers = {**_MCP_HEADERS, **_auth()}
    with _client(monkeypatch) as client:
        sid = _mcp_session(client, headers)
        result = _mcp_call(client, sid, headers, method="tools/list")
    tools = result["result"]["tools"]
    assert len(tools) == 5
    names = sorted(t["name"] for t in tools)
    assert names == ["get_image", "get_page_content", "list_files", "list_kbs", "read_file"]
    for t in tools:
        assert t.get("description"), f"tool {t['name']} has no description"


def test_mcp_list_kbs(monkeypatch, kb_dir):
    """list_kbs returns the knowledge base name and document count."""
    monkeypatch.setattr(
        "openkb.api_kbs._list_knowledge_bases",
        lambda: {"root": str(kb_dir), "knowledge_bases": [
            {"name": "test-kb", "path": str(kb_dir), "document_count": 3, "last_compile": None, "has_raw": True}
        ]},
    )
    headers = {**_MCP_HEADERS, **_auth()}
    with _client(monkeypatch) as client:
        sid = _mcp_session(client, headers)
        result = _mcp_call(
            client, sid, headers,
            method="tools/call",
            params={"name": "list_kbs", "arguments": {}},
        )
    text = result["result"]["content"][0]["text"]
    assert "1 knowledge base" in text
    assert "test-kb" in text
    assert "3 docs" in text


def test_mcp_read_file(monkeypatch, kb_dir):
    """read_file returns wiki markdown content."""
    (kb_dir / "wiki" / "index.md").write_text("# My KB\n\n## Documents\nnone\n", encoding="utf-8")
    kb = _use_named_kb(monkeypatch, kb_dir)
    headers = {**_MCP_HEADERS, **_auth()}
    with _client(monkeypatch) as client:
        sid = _mcp_session(client, headers)
        result = _mcp_call(
            client, sid, headers,
            method="tools/call",
            params={"name": "read_file", "arguments": {"path": "index.md", "kb": kb}},
        )
    content = result["result"]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert "# My KB" in content[0]["text"]


def test_mcp_read_file_not_found(monkeypatch, kb_dir):
    """read_file on a missing path returns an error string, not a crash."""
    kb = _use_named_kb(monkeypatch, kb_dir)
    headers = {**_MCP_HEADERS, **_auth()}
    with _client(monkeypatch) as client:
        sid = _mcp_session(client, headers)
        result = _mcp_call(
            client, sid, headers,
            method="tools/call",
            params={"name": "read_file", "arguments": {"path": "nonexistent.md", "kb": kb}},
        )
    text = result["result"]["content"][0]["text"]
    assert "not found" in text.lower()


def test_mcp_read_file_path_traversal(monkeypatch, kb_dir):
    """read_file rejects paths that escape the wiki root."""
    kb = _use_named_kb(monkeypatch, kb_dir)
    headers = {**_MCP_HEADERS, **_auth()}
    with _client(monkeypatch) as client:
        sid = _mcp_session(client, headers)
        result = _mcp_call(
            client, sid, headers,
            method="tools/call",
            params={"name": "read_file", "arguments": {"path": "../../../etc/passwd", "kb": kb}},
        )
    text = result["result"]["content"][0]["text"]
    assert "denied" in text.lower() or "escapes" in text.lower()


def test_mcp_list_files(monkeypatch, kb_dir):
    """list_files returns .md filenames in a wiki subdirectory."""
    (kb_dir / "wiki" / "concepts" / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    (kb_dir / "wiki" / "concepts" / "beta.md").write_text("# Beta\n", encoding="utf-8")
    kb = _use_named_kb(monkeypatch, kb_dir)
    headers = {**_MCP_HEADERS, **_auth()}
    with _client(monkeypatch) as client:
        sid = _mcp_session(client, headers)
        result = _mcp_call(
            client, sid, headers,
            method="tools/call",
            params={"name": "list_files", "arguments": {"directory": "concepts", "kb": kb}},
        )
    text = result["result"]["content"][0]["text"]
    assert "alpha.md" in text
    assert "beta.md" in text


def test_mcp_list_files_empty_dir(monkeypatch, kb_dir):
    """list_files on an empty directory returns 'No files found.'"""
    kb = _use_named_kb(monkeypatch, kb_dir)
    headers = {**_MCP_HEADERS, **_auth()}
    with _client(monkeypatch) as client:
        sid = _mcp_session(client, headers)
        result = _mcp_call(
            client, sid, headers,
            method="tools/call",
            params={"name": "list_files", "arguments": {"directory": "summaries", "kb": kb}},
        )
    text = result["result"]["content"][0]["text"]
    assert "No files" in text


def test_mcp_get_page_content(monkeypatch, kb_dir):
    """get_page_content returns formatted text from a PageIndex JSON document."""
    pages = [
        {"page": 1, "content": "Introduction text."},
        {"page": 2, "content": "Methodology text."},
        {"page": 3, "content": "Results text."},
    ]
    (kb_dir / "wiki" / "sources" / "my-paper.json").write_text(
        json.dumps(pages), encoding="utf-8"
    )
    kb = _use_named_kb(monkeypatch, kb_dir)
    headers = {**_MCP_HEADERS, **_auth()}
    with _client(monkeypatch) as client:
        sid = _mcp_session(client, headers)
        result = _mcp_call(
            client, sid, headers,
            method="tools/call",
            params={
                "name": "get_page_content",
                "arguments": {"doc_name": "my-paper", "pages": "1,3", "kb": kb},
            },
        )
    text = result["result"]["content"][0]["text"]
    assert "Page 1" in text
    assert "Introduction text" in text
    assert "Page 3" in text
    assert "Results text" in text
    assert "Page 2" not in text


def test_mcp_get_image(monkeypatch, kb_dir):
    """get_image returns a base64 data URL for a wiki image."""
    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    img_dir = kb_dir / "wiki" / "sources" / "images" / "test-doc"
    img_dir.mkdir(parents=True, exist_ok=True)
    (img_dir / "fig1.png").write_bytes(png)
    kb = _use_named_kb(monkeypatch, kb_dir)
    headers = {**_MCP_HEADERS, **_auth()}
    with _client(monkeypatch) as client:
        sid = _mcp_session(client, headers)
        result = _mcp_call(
            client, sid, headers,
            method="tools/call",
            params={
                "name": "get_image",
                "arguments": {
                    "image_path": "sources/images/test-doc/fig1.png",
                    "kb": kb,
                },
            },
        )
    text = result["result"]["content"][0]["text"]
    assert text.startswith("data:image/png;base64,")


def test_mcp_get_image_not_found(monkeypatch, kb_dir):
    """get_image on a missing image returns an error string."""
    kb = _use_named_kb(monkeypatch, kb_dir)
    headers = {**_MCP_HEADERS, **_auth()}
    with _client(monkeypatch) as client:
        sid = _mcp_session(client, headers)
        result = _mcp_call(
            client, sid, headers,
            method="tools/call",
            params={
                "name": "get_image",
                "arguments": {"image_path": "sources/images/nope.png", "kb": kb},
            },
        )
    text = result["result"]["content"][0]["text"]
    assert "not found" in text.lower()
