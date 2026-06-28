"""
========================================
web/system.py — 心跳 / 日志 / 错误码面板
========================================

- /api/heartbeat：前端心跳灯轮询（alive/uptime/last_op/decay 状态）
- /api/logs：读 server.log 末尾若干行（按级别过滤）
- /api/errors/recent、/api/errors/clear：统一错误码体系（errors.jsonl）读取/清空

对外暴露：register(mcp)。
========================================
"""

import os
import time

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

try:
    from errors import recent_errors, format_error, clear_errors_log, get_recent_logs  # type: ignore
except ImportError:  # pragma: no cover
    from ..errors import recent_errors, format_error, clear_errors_log, get_recent_logs  # type: ignore

_LOGS_DEFAULT_LIMIT = 200
_LOGS_MAX_LIMIT = 2000
_ERRORS_DEFAULT_LIMIT = 50
_ERRORS_MAX_LIMIT = 500


def register(mcp) -> None:

    @mcp.custom_route("/api/heartbeat", methods=["GET"])
    async def api_heartbeat(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse({
            "alive": True,
            "ts": time.time(),
            "uptime_s": int(time.time() - sh._SERVER_START_TS),
            "last_op_ts": sh._LAST_OP_TS,
            "decay_engine": "running" if sh.decay_engine.is_running else "stopped",
        })

    @mcp.custom_route("/api/logs", methods=["GET"])
    async def api_logs(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        log_file = os.environ.get("OMBRE_LOG_FILE", "")
        if not log_file or not os.path.isfile(log_file):
            return JSONResponse({
                "lines": [],
                "log_file": log_file or "",
                "note": "日志文件尚未创建（可能未启用文件日志或刚启动）",
            })
        try:
            limit = max(1, min(int(request.query_params.get("limit", str(_LOGS_DEFAULT_LIMIT))), _LOGS_MAX_LIMIT))
        except ValueError:
            limit = _LOGS_DEFAULT_LIMIT
        level = request.query_params.get("level", "WARNING").upper()
        allow = {"ERROR": ("ERROR",),
                 "WARNING": ("WARNING", "ERROR"),
                 "INFO": ("INFO", "WARNING", "ERROR"),
                 "ALL": None}
        keep = allow.get(level, ("WARNING", "ERROR"))
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if keep is not None:
                lines = [ln for ln in lines if any(f" {lv}: " in ln for lv in keep)]
            lines = lines[-limit:]
            return JSONResponse({
                "lines": [ln.rstrip("\n") for ln in lines],
                "log_file": log_file,
                "level": level,
                "count": len(lines),
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/errors/recent", methods=["GET"])
    async def api_errors_recent(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            limit = max(1, min(int(request.query_params.get("limit", str(_ERRORS_DEFAULT_LIMIT))), _ERRORS_MAX_LIMIT))
        except ValueError:
            limit = _ERRORS_DEFAULT_LIMIT
        min_level = request.query_params.get("min_level", "W").upper()
        items = recent_errors(limit=limit, min_level=min_level)
        tail = get_recent_logs(15)
        for it in items:
            it["formatted"] = format_error(
                it.get("code", ""), it.get("detail", ""),
                extra=it.get("extra"), include_logs=True,
            )
        return JSONResponse({
            "ok": True,
            "count": len(items),
            "min_level": min_level,
            "log_tail": tail,
            "errors": items,
        })

    @mcp.custom_route("/api/errors/clear", methods=["POST"])
    async def api_errors_clear(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        n = clear_errors_log()
        return JSONResponse({"ok": True, "cleared": n})
