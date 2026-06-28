import pytest

import server
from mcp.types import CallToolResult
from web import oauth


def test_tools_advertise_oauth_security_scheme():
    tool = server.mcp._tool_manager.get_tool("breath")

    assert tool.meta == {
        "securitySchemes": [{"type": "oauth2", "scopes": ["mcp"]}],
    }


@pytest.mark.asyncio
async def test_unauthenticated_tool_call_returns_chatgpt_oauth_challenge():
    async def operation():
        raise AssertionError("operation must not run before authentication")

    previous = server.config.get("mcp_require_auth", True)
    server.config["mcp_require_auth"] = True
    token = oauth._mcp_request_auth.set((
        False,
        "https://example.test/.well-known/oauth-protected-resource/mcp",
    ))
    try:
        result = await server._with_notice(operation(), op="breath")
    finally:
        oauth._mcp_request_auth.reset(token)
        server.config["mcp_require_auth"] = previous

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    challenge = result.meta["mcp/www_authenticate"][0]
    assert 'resource_metadata="https://example.test/.well-known/oauth-protected-resource/mcp"' in challenge
    assert 'error="insufficient_scope"' in challenge
    assert "error_description=" in challenge



@pytest.mark.asyncio
async def test_fastmcp_preserves_oauth_challenge_for_string_tools():
    previous = server.config.get("mcp_require_auth", True)
    server.config["mcp_require_auth"] = True
    token = oauth._mcp_request_auth.set((
        False,
        "https://example.test/.well-known/oauth-protected-resource/mcp",
    ))
    try:
        result = await server.mcp._tool_manager.call_tool(
            "breath", {}, convert_result=True,
        )
    finally:
        oauth._mcp_request_auth.reset(token)
        server.config["mcp_require_auth"] = previous

    assert isinstance(result, CallToolResult)
    assert result.meta["mcp/www_authenticate"]

def test_authorization_form_preserves_resource_and_scope():
    html = oauth._oauth_authorize_html(
        "client", "https://chatgpt.com/connector/oauth/callback", "state",
        "challenge", "https://example.test/mcp", "mcp",
    )

    assert 'name="resource" value="https://example.test/mcp"' in html
    assert 'name="scope" value="mcp"' in html
