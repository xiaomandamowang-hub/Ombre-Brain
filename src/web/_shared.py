"""
========================================
web/_shared.py — Dashboard/HTTP 层的共享依赖与鉴权工具
========================================

类比 tools/_runtime.py：web/ 下的各路由模块（auth/tunnel/oauth/…）都从这里取
运行期依赖（config）和横切工具（cookie 会话鉴权、密码哈希、安全问题急救）。

为什么单独抽出来：
- server.py 历史上把 93 个 @mcp.custom_route 全平铺在一个 5000 行文件里，难维护。
- 鉴权是所有 /api/* 路由的横切关注点，必须有一个单一来源，否则一拆就到处重复。

关键行为：
- init(config)：启动时由 server.py 注入 config（之后函数按需读 config["buckets_dir"]）。
- 会话：基于 cookie 的简单会话，落盘到 <buckets_dir>/.dashboard_sessions.json，
  100 年滚动有效（实际永久）；_load_sessions 原地改 _sessions（不重绑），
  这样 server.py / 其它模块 `from ._shared import _sessions` 始终指向同一对象。
- 密码：salt:sha256 存 <buckets_dir>/.dashboard_auth.json；支持环境变量
  OMBRE_DASHBOARD_PASSWORD 覆盖；安全问题用于忘密码急救。

不做什么：
- 不定义任何路由（路由在 web/<模块>.py 里，用 register(mcp) 注册）。
- 不持有业务引擎（bucket_mgr 等仍在 server.py / tools/_runtime；需要时再按同样方式注入）。

对外暴露：init + 一组鉴权/会话/密码 helper（名字与原 server.py 完全一致，便于 import 回去）。
========================================
"""

import os
import time
import json as _json_lib
import hashlib
import hmac
import secrets
import logging

from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("ombre_brain")

# --- 运行环境探测（Docker vs 裸机）---
# 本地向量化要按宿主类型分流：Docker 里 ollama 是独立容器（连 ombre-ollama），
# 裸机/原生则连本机 127.0.0.1。结果缓存一次，避免每次 IO。
_in_docker_cache: "bool | None" = None


def in_docker() -> bool:
    """是否运行在 Docker 容器里。看 /.dockerenv 与 /proc/1/cgroup。结果缓存。"""
    global _in_docker_cache
    if _in_docker_cache is not None:
        return _in_docker_cache
    found = False
    try:
        if os.path.exists("/.dockerenv"):
            found = True
        else:
            with open("/proc/1/cgroup", "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            found = ("docker" in txt) or ("containerd" in txt) or ("kubepods" in txt)
    except Exception:
        found = False
    _in_docker_cache = found
    return found


# --- 注入的运行期配置（server.py 启动时 init 进来）---
config: dict = {}

# --- 注入的业务引擎与运行期信息（类比 tools/_runtime；server.py 启动时 init_runtime）---
# 各 web 路由模块通过 sh.<name> 读取，避免和 server.py 各持一份不一致。
# embedding_engine 会被热重载替换 —— 替换方必须写 sh.embedding_engine（属性赋值），
# 这样所有模块下次读 sh.embedding_engine 都拿到新实例。
version: str = ""
repo_root: str = ""   # 仓库根目录（server.py 注入；用于定位 frontend/ 等，避免各模块各算 __file__）
bucket_mgr = None
dehydrator = None
decay_engine = None
embedding_engine = None
import_engine = None
migrate_engine = None
github_sync_instance = None


def init(cfg: dict) -> None:
    """启动时由 server.py 调用，注入全局 config。"""
    global config
    config = cfg


def init_runtime(**kwargs) -> None:
    """启动时注入业务引擎与版本等运行期对象。

    用法：init_runtime(version=..., bucket_mgr=..., decay_engine=..., ...)
    只更新传入的键，未传的保持不变。
    """
    globals().update(kwargs)


# --- 心跳 / 活跃时间戳（原 server.py；移到这里让 heartbeat 路由与工具共用同一来源）---
_SERVER_START_TS = time.time()
_LAST_OP_TS = _SERVER_START_TS


def _mark_op(name: str = "") -> None:
    """记录一次工具/接口活跃时间，供 /api/heartbeat 上报。

    server.py 启动时把本函数注入 tools._runtime.mark_op，工具调用即更新；
    /api/heartbeat（web/system.py）读 _LAST_OP_TS。两边同一来源，不会不一致。
    """
    global _LAST_OP_TS
    _LAST_OP_TS = time.time()


# --- server.py 级 helper 的注入位（保持定义在 server.py，这里只持引用）---
# 这些函数读/写 server.py 的 webhook 全局等，搬过来会引发级联，故用注入而非搬迁。
# 在它们各自定义之后由 server.py 调 init_runtime(...) 填入。
fire_webhook = None            # async def(event: str, payload: dict) -> None
write_deletion_notice = None   # def(names: list) -> None
pop_deletion_notice = None     # def() -> str
restart_github_auto_task = None # def(interval_minutes: int) -> None（起停后台 GitHub 同步任务）


# --- 项目 .env 读写（config / env-config / host-vault 路由共用，故放共享层）---
# 与原 server.py 行为一致：.env 落在 src/.env。本文件在 src/web/ 下，上两级即 src/。
def _project_env_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _read_env_var(name: str) -> str:
    """Return current value of `name` from process env first, then .env file (best-effort)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    env_path = _project_env_path()
    if not os.path.exists(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _write_env_var(name: str, value: str) -> None:
    """Idempotent upsert of `NAME=value` in project .env. Creates file if missing.
    Preserves other entries verbatim. Quotes values containing spaces.
    """
    env_path = _project_env_path()
    quoted = f'"{value}"' if value and (" " in value or "#" in value) else value
    new_line = f"{name}={quoted}\n"

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, _v = stripped.partition("=")
        if k.strip() == name:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


# --- Dashboard 鉴权常量（原 server.py 调参面板）---
_PASSWORD_SALT_BYTES = 16            # secrets.token_hex(该值) → 32 char hex salt
_SESSION_TOKEN_BYTES = 32            # secrets.token_urlsafe(该值) → ~43 char token
_SESSION_TTL_SECONDS = 86400 * 36500  # 100 年 rolling（实际永久）
_SESSION_TTL = _SESSION_TTL_SECONDS

_sessions: dict[str, float] = {}  # {token: expiry_timestamp}


def _get_auth_file() -> str:
    return os.path.join(config["buckets_dir"], ".dashboard_auth.json")


def _get_sessions_file() -> str:
    return os.path.join(config["buckets_dir"], ".dashboard_sessions.json")


def _load_sessions() -> None:
    """Load persisted sessions from disk on startup. Drop expired ones.

    原地改 _sessions（clear+update），不重绑对象 —— 这样别处 `from ._shared import
    _sessions` 拿到的引用始终有效。
    """
    try:
        path = _get_sessions_file()
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            raw = _json_lib.load(f)
        now = time.time()
        # 文件格式：{token: expiry_ts}；过期的丢掉
        valid = {tok: exp for tok, exp in raw.items() if isinstance(exp, (int, float)) and exp > now}
        _sessions.clear()
        _sessions.update(valid)
    except Exception as e:
        logger.warning(f"[auth] failed to load sessions: {e}")


def _save_sessions() -> None:
    """Atomically persist active sessions to disk."""
    try:
        path = _get_sessions_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 只写未过期的；用 .tmp + os.replace 做原子写，避免 iCloud 同步看到半截 JSON
        now = time.time()
        active = {tok: exp for tok, exp in _sessions.items() if exp > now}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json_lib.dump(active, f)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"[auth] failed to save sessions: {e}")


def _load_auth_data() -> dict:
    try:
        auth_file = _get_auth_file()
        if os.path.exists(auth_file):
            with open(auth_file, "r", encoding="utf-8") as f:
                return _json_lib.load(f)
    except Exception:
        pass
    return {}


def _load_password_hash() -> str | None:
    return _load_auth_data().get("password_hash")


def _save_password_hash(password: str, *, keep_qa: bool = True) -> None:
    salt = secrets.token_hex(_PASSWORD_SALT_BYTES)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    auth_file = _get_auth_file()
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    data: dict = {"password_hash": f"{salt}:{h}"}
    if keep_qa:
        existing = _load_auth_data()
        if existing.get("security_question"):
            data["security_question"] = existing["security_question"]
        if existing.get("security_answer_hash"):
            data["security_answer_hash"] = existing["security_answer_hash"]
    with open(auth_file, "w", encoding="utf-8") as f:
        _json_lib.dump(data, f, ensure_ascii=False)


def _save_security_qa(question: str, answer: str) -> None:
    salt = secrets.token_hex(_PASSWORD_SALT_BYTES)
    h = hashlib.sha256(f"{salt}:{answer.strip().lower()}".encode()).hexdigest()
    auth_file = _get_auth_file()
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    data = _load_auth_data()
    data["security_question"] = question.strip()
    data["security_answer_hash"] = f"{salt}:{h}"
    with open(auth_file, "w", encoding="utf-8") as f:
        _json_lib.dump(data, f, ensure_ascii=False)


def _verify_security_answer(answer: str) -> bool:
    stored = _load_auth_data().get("security_answer_hash", "")
    if not stored or ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return hmac.compare_digest(
        h, hashlib.sha256(f"{salt}:{answer.strip().lower()}".encode()).hexdigest()
    )


def _verify_password_hash(password: str, stored: str) -> bool:
    if ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return hmac.compare_digest(
        h, hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    )


def _is_setup_needed() -> bool:
    """True if no password is configured (env var or file)."""
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return False
    return _load_password_hash() is None


def _verify_any_password(password: str) -> bool:
    """Check password against env var (first) or stored hash."""
    env_pwd = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")
    if env_pwd:
        return hmac.compare_digest(password, env_pwd)
    stored = _load_password_hash()
    if not stored:
        return False
    return _verify_password_hash(password, stored)


def _create_session() -> str:
    token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
    _sessions[token] = time.time() + _SESSION_TTL
    _save_sessions()
    return token


def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get("ombre_session")
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None or time.time() > expiry:
        if expiry is not None:
            _sessions.pop(token, None)
            _save_sessions()
        return False
    return True


def _is_https_request(request: Request) -> bool:
    """Detect HTTPS through Cloudflare/reverse-proxy via X-Forwarded-Proto header."""
    proto = (request.headers.get("x-forwarded-proto") or "").lower()
    if proto == "https":
        return True
    try:
        return request.url.scheme == "https"
    except Exception:
        return False


def _set_session_cookie(resp: Response, token: str, request: Request) -> None:
    """Set the ombre_session cookie. Mark Secure when behind HTTPS so modern
    browsers (Safari/Chrome) actually persist it across navigations.
    本地 http://127.0.0.1 走 secure=False，公网 https 自动开启 Secure。
    """
    resp.set_cookie(
        "ombre_session",
        token,
        httponly=True,
        samesite="lax",
        secure=_is_https_request(request),
        max_age=_SESSION_TTL,
        path="/",
    )


def _require_auth(request: Request) -> Response | None:
    """Return JSONResponse(401) if not authenticated, else None."""
    from starlette.responses import JSONResponse
    if not _is_authenticated(request):
        return JSONResponse(
            {"error": "Unauthorized", "setup_needed": _is_setup_needed()},
            status_code=401,
        )
    return None
