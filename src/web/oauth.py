"""
========================================
web/oauth.py — MCP 远程鉴权（OAuth 2.1 + PKCE）
========================================

Claude.ai 网页版 / Claude Code 通过 HTTPS 连接 MCP 时走的 OAuth 流程：
动态注册 → 授权页（输 Dashboard 密码）→ 换 code → 换 Bearer token。
token 落盘 <buckets_dir>/.dashboard_mcp_tokens.json，100 年有效（实际永久），
Docker 重启不强制重新授权。

server.py 的 MCP 鉴权中间件需要 _is_valid_mcp_token 来校验 /mcp(-extra) 的 Bearer，
故它对外可见。

对外暴露：
- register(mcp)：注册 /.well-known/* 与 /oauth/* 路由（并在注册时载入持久化 token）
- _is_valid_mcp_token：供 server.py 启动期的 _MCPAuthMiddleware 调用
========================================
"""

import os
import json as _json_lib
import secrets
import time as _time_mod
import urllib.parse as _urlparse
import base64 as _base64
import hashlib as _hashlib_oauth
import html as _html_escape

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

logger = sh.logger

_oauth_clients: dict[str, dict] = {}
_oauth_codes: dict[str, dict] = {}    # code -> {client_id, redirect_uri, code_challenge, expires}
_mcp_tokens: dict[str, float] = {}    # token -> expiry timestamp

_OAUTH_CODE_TTL = 300               # 5 min
_MCP_TOKEN_TTL = 86400 * 36500      # 100 年（实际永久）


def _public_base_url(request: Request) -> str:
    """Return the externally-visible base URL, honoring Cloudflare/reverse-proxy headers."""
    proto = (request.headers.get("x-forwarded-proto") or "").lower() or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _mcp_tokens_file() -> str:
    return os.path.join(sh.config["buckets_dir"], ".dashboard_mcp_tokens.json")


def _load_mcp_tokens() -> None:
    global _mcp_tokens
    try:
        path = _mcp_tokens_file()
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            raw = _json_lib.load(f)
        now = _time_mod.time()
        _mcp_tokens = {tok: exp for tok, exp in raw.items()
                       if isinstance(exp, (int, float)) and exp > now}
    except Exception as e:
        logger.warning(f"[oauth] failed to load mcp tokens: {e}")


def _save_mcp_tokens() -> None:
    try:
        path = _mcp_tokens_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        now = _time_mod.time()
        active = {tok: exp for tok, exp in _mcp_tokens.items() if exp > now}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json_lib.dump(active, f)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"[oauth] failed to save mcp tokens: {e}")


def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    digest = _hashlib_oauth.sha256(code_verifier.encode()).digest()
    computed = _base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return computed == code_challenge


def _is_valid_mcp_token(token: str) -> bool:
    expiry = _mcp_tokens.get(token)
    if expiry is None:
        return False
    if _time_mod.time() > expiry:
        del _mcp_tokens[token]
        return False
    return True


def _mcp_auth_check(request: Request):
    """Return True if request has a valid MCP Bearer token."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return _is_valid_mcp_token(auth[7:])
    return False


def _oauth_authorize_html(client_id: str, redirect_uri: str, state: str,
                           code_challenge: str, error: str = "") -> str:
    e = _html_escape.escape
    err_html = f'<p style="color:#ff6b6b;font-size:13px;margin-top:12px;">{e(error)}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ombre Brain · 授权 MCP</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,system-ui,sans-serif;background:#0f0f0f;color:#e0e0e0;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{background:#1a1a1a;border:1px solid #333;border-radius:16px;padding:40px 36px;
  max-width:380px;width:90%;text-align:center}}
h2{{color:#c9a96e;font-family:Georgia,serif;font-size:24px;margin:0 0 6px}}
.sub{{color:#888;font-size:13px;margin:0 0 24px}}
input[type=password]{{display:block;width:100%;padding:11px 14px;background:#111;
  border:1px solid #444;border-radius:8px;color:#e0e0e0;font-size:14px;margin-bottom:14px}}
button{{width:100%;padding:12px;background:#c9a96e;color:#0f0f0f;border:none;
  border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}}
button:hover{{background:#d4b87a}}
.note{{color:#666;font-size:11px;margin-top:16px;line-height:1.6}}
</style></head>
<body><div class="card">
<h2>◐ Ombre Brain</h2>
<p class="sub">授权 Claude Code 连接 MCP</p>
<form method="POST">
<input type="hidden" name="client_id" value="{e(client_id)}">
<input type="hidden" name="redirect_uri" value="{e(redirect_uri)}">
<input type="hidden" name="state" value="{e(state)}">
<input type="hidden" name="code_challenge" value="{e(code_challenge)}">
<input type="password" name="password" placeholder="输入 Dashboard 密码" autofocus>
<button type="submit">授权并连接</button>
</form>
{err_html}
<p class="note">授权后 Claude Code 将可使用 MCP 工具读写记忆。<br>Token 永久有效，无需重复授权。<br>若工具调用失败，请在客户端断开重连，再重新点击此页授权即可。</p>
</div></body></html>"""


def register(mcp) -> None:
    """注册 /.well-known/* 与 /oauth/* 路由，并在装配时载入持久化 token。"""
    _load_mcp_tokens()   # 启动时恢复持久化 token，Docker 重启不再强制重新 OAuth

    @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    @mcp.custom_route("/.well-known/oauth-protected-resource/{resource_path:path}", methods=["GET"])
    async def oauth_protected_resource(request: Request) -> Response:
        from starlette.responses import JSONResponse
        base = _public_base_url(request)
        # 带路径时（如 /mcp-extra）按请求路径动态返回对应 resource，
        # 以便 Claude.ai 的严格 resource 匹配通过；无路径时返回根资源。
        sub = request.path_params.get("resource_path", "")
        resource = f"{base}/{sub}" if sub else base
        return JSONResponse({
            "resource": resource,
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
        })

    @mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def oauth_authorization_server(request: Request) -> Response:
        from starlette.responses import JSONResponse
        base = _public_base_url(request)
        return JSONResponse({
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["mcp"],
        })

    @mcp.custom_route("/oauth/register", methods=["POST"])
    async def oauth_register(request: Request) -> Response:
        from starlette.responses import JSONResponse
        try:
            body = await request.json()
        except Exception:
            body = {}
        client_id = secrets.token_urlsafe(16)
        _oauth_clients[client_id] = {
            "redirect_uris": body.get("redirect_uris", []),
            "client_name": body.get("client_name", "MCP Client"),
        }
        return JSONResponse({
            "client_id": client_id,
            "client_id_issued_at": int(_time_mod.time()),
            "redirect_uris": body.get("redirect_uris", []),
            "client_name": body.get("client_name", "MCP Client"),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        }, status_code=201)

    @mcp.custom_route("/oauth/authorize", methods=["GET", "POST"])
    async def oauth_authorize(request: Request) -> Response:
        from starlette.responses import HTMLResponse, RedirectResponse
        if request.method == "GET":
            p = dict(request.query_params)
            return HTMLResponse(_oauth_authorize_html(
                p.get("client_id", ""), p.get("redirect_uri", ""),
                p.get("state", ""), p.get("code_challenge", ""),
            ))
        # POST
        form = await request.form()
        password     = str(form.get("password", ""))
        client_id    = str(form.get("client_id", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        state        = str(form.get("state", ""))
        code_challenge = str(form.get("code_challenge", ""))

        if not sh._verify_any_password(password):
            return HTMLResponse(_oauth_authorize_html(
                client_id, redirect_uri, state, code_challenge, error="密码错误，请重试"
            ), status_code=401)

        client_info = _oauth_clients.get(client_id)
        if client_info and redirect_uri not in client_info.get("redirect_uris", []):
            return HTMLResponse(_oauth_authorize_html(
                client_id, redirect_uri, state, code_challenge,
                error="redirect_uri 与注册不符，拒绝授权"
            ), status_code=400)

        code = secrets.token_urlsafe(32)
        _oauth_codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "expires": _time_mod.time() + _OAUTH_CODE_TTL,
        }
        sep = "&" if "?" in redirect_uri else "?"
        location = f"{redirect_uri}{sep}code={_urlparse.quote(code)}"
        if state:
            location += f"&state={_urlparse.quote(state)}"
        return RedirectResponse(location, status_code=302)

    @mcp.custom_route("/oauth/token", methods=["POST"])
    async def oauth_token(request: Request) -> Response:
        from starlette.responses import JSONResponse
        content_type = request.headers.get("content-type", "")
        try:
            if "json" in content_type:
                body = await request.json()
            else:
                form = await request.form()
                body = dict(form)
        except Exception:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        if body.get("grant_type") != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

        code = str(body.get("code", ""))
        code_verifier = str(body.get("code_verifier", ""))
        code_data = _oauth_codes.pop(code, None)
        if not code_data:
            return JSONResponse({"error": "invalid_grant", "error_description": "unknown or expired code"}, status_code=400)
        if code_data["expires"] < _time_mod.time():
            return JSONResponse({"error": "invalid_grant", "error_description": "code expired"}, status_code=400)

        if code_data.get("code_challenge"):
            if not code_verifier or not _verify_pkce(code_verifier, code_data["code_challenge"]):
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

        token = secrets.token_urlsafe(32)
        _mcp_tokens[token] = _time_mod.time() + _MCP_TOKEN_TTL
        _save_mcp_tokens()
        return JSONResponse({
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": _MCP_TOKEN_TTL,
            "scope": "mcp",
        })
