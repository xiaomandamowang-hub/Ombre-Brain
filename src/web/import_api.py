"""
========================================
web/import_api.py — 宿主机 vault 设置 / 历史对话导入 / 桶编辑 / 导出 / 记忆包迁移
========================================

- /api/host-vault：读写 docker-compose 挂载的宿主机记忆目录（写 .env）
- /api/import/*：上传历史对话、状态/暂停/模式/结果/复核
- /api/bucket/{id}/edit：编辑桶正文（带内容体积校验）
- /api/export：导出全部记忆 zip
- /api/migrate/*：记忆包 zip 上传 / 状态 / 应用

对外暴露：register(mcp)。
========================================
"""

import os
import io
import json as _json_lib
import time
import asyncio
import zipfile

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

logger = sh.logger

try:
    from tools._common import (  # type: ignore
        check_content_size as _check_content_size,
        check_pinned_quota as _check_pinned_quota,
    )
except ImportError:  # pragma: no cover
    from ..tools._common import (  # type: ignore
        check_content_size as _check_content_size,
        check_pinned_quota as _check_pinned_quota,
    )


def register(mcp) -> None:

    @mcp.custom_route("/api/host-vault", methods=["GET"])
    async def api_host_vault_get(request: Request) -> Response:
        """Read the current OMBRE_HOST_VAULT_DIR (process env > project .env)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        value = sh._read_env_var("OMBRE_HOST_VAULT_DIR")
        return JSONResponse({
            "value": value,
            "source": "env" if os.environ.get("OMBRE_HOST_VAULT_DIR", "").strip() else ("file" if value else ""),
            "env_file": sh._project_env_path(),
        })


    @mcp.custom_route("/api/host-vault", methods=["POST"])
    async def api_host_vault_set(request: Request) -> Response:
        """
        Persist OMBRE_HOST_VAULT_DIR to the project .env file.
        Body: {"value": "/path/to/vault"}  (empty string clears the entry)
        Note: container restart is required for docker-compose to pick up the new mount.
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        raw = body.get("value", "")
        if not isinstance(raw, str):
            return JSONResponse({"error": "value must be a string"}, status_code=400)
        value = raw.strip()

        # Reject characters that would break .env / shell parsing
        if "\n" in value or "\r" in value or '"' in value or "'" in value:
            return JSONResponse({"error": "value must not contain quotes or newlines"}, status_code=400)

        try:
            sh._write_env_var("OMBRE_HOST_VAULT_DIR", value)
        except Exception as e:
            return JSONResponse({"error": f"failed to write .env: {e}"}, status_code=500)

        return JSONResponse({
            "ok": True,
            "value": value,
            "env_file": sh._project_env_path(),
            "note": "已写入 .env；需在宿主机执行 `docker compose down && docker compose up -d` 让新挂载生效。",
        })


    # =============================================================
    # Import API — conversation history import
    # 导入 API — 对话历史导入
    # =============================================================

    @mcp.custom_route("/api/import/upload", methods=["POST"])
    async def api_import_upload(request: Request) -> Response:
        """Upload a conversation file and start import."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        if sh.import_engine.is_running:
            return JSONResponse({"error": "Import already running"}, status_code=409)

        content_type = request.headers.get("content-type", "")
        filename = ""

        try:
            if "multipart/form-data" in content_type:
                form = await request.form()
                file_field = form.get("file")
                if not file_field or isinstance(file_field, str):
                    return JSONResponse({"error": "No file field"}, status_code=400)
                raw_bytes = await file_field.read()
                filename = getattr(file_field, "filename", "upload")
                raw_content = raw_bytes.decode("utf-8", errors="replace")
            else:
                body = await request.body()
                raw_content = body.decode("utf-8", errors="replace")
                # Try to get filename from query params
                filename = request.query_params.get("filename", "upload")

            if not raw_content.strip():
                return JSONResponse({"error": "Empty file"}, status_code=400)

            preserve_raw = request.query_params.get("preserve_raw", "").lower() in ("1", "true")
            resume = request.query_params.get("resume", "").lower() in ("1", "true")

        except Exception as e:
            return JSONResponse({"error": f"Failed to read upload: {e}"}, status_code=400)

        # Start import in background
        async def _run_import():
            try:
                await sh.import_engine.start(raw_content, filename, preserve_raw, resume)
            except Exception as e:
                logger.error(f"Import failed: {e}")

        asyncio.create_task(_run_import())

        return JSONResponse({
            "status": "started",
            "filename": filename,
            "size_bytes": len(raw_content.encode()),
        })


    @mcp.custom_route("/api/import/status", methods=["GET"])
    async def api_import_status(request: Request) -> Response:
        """Get current import progress."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse(sh.import_engine.get_status())


    @mcp.custom_route("/api/import/pause", methods=["POST"])
    async def api_import_pause(request: Request) -> Response:
        """Pause the running import."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if not sh.import_engine.is_running:
            return JSONResponse({"error": "No import running"}, status_code=400)
        sh.import_engine.pause()
        return JSONResponse({"status": "pause_requested"})


    @mcp.custom_route("/api/import/patterns", methods=["GET"])
    async def api_import_patterns(request: Request) -> Response:
        """Detect high-frequency patterns after import."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            patterns = await sh.import_engine.detect_patterns()
            return JSONResponse({"patterns": patterns})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/import/results", methods=["GET"])
    async def api_import_results(request: Request) -> Response:
        """List recently imported/created buckets for review."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            limit = int(request.query_params.get("limit", "50"))
            all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
            # Sort by created time, newest first
            all_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            results = []
            for b in all_buckets[:limit]:
                results.append({
                    "id": b["id"],
                    "name": b["metadata"].get("name", ""),
                    "content": b["content"][:300],
                    "type": b["metadata"].get("type", ""),
                    "domain": b["metadata"].get("domain", []),
                    "tags": b["metadata"].get("tags", []),
                    "importance": b["metadata"].get("importance", 5),
                    "created": b["metadata"].get("created", ""),
                })
            return JSONResponse({"buckets": results, "total": len(all_buckets)})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/import/review", methods=["POST"])
    async def api_import_review(request: Request) -> Response:
        """Apply review decisions: mark buckets as important/noise/pinned."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        decisions = body.get("decisions", [])
        if not decisions:
            return JSONResponse({"error": "No decisions provided"}, status_code=400)

        applied = 0
        errors = 0
        for d in decisions:
            bid = d.get("bucket_id", "")
            action = d.get("action", "")
            if not bid or not action:
                continue
            try:
                if action == "important":
                    await sh.bucket_mgr.update(bid, importance=9)
                elif action == "pin":
                    await sh.bucket_mgr.update(bid, pinned=True)
                elif action == "noise":
                    await sh.bucket_mgr.update(bid, resolved=True, importance=1)
                elif action == "delete":
                    await sh.bucket_mgr.delete(bid)
                applied += 1
            except Exception as e:
                logger.warning(f"Review action failed for {bid}: {e}")
                errors += 1

        return JSONResponse({"applied": applied, "errors": errors})


    # =============================================================
    # /api/bucket/{id}/edit  — iter 1.6 §6 trace 前端
    # 让 Dashboard 直接修改桶元数据：name / tags / importance / resolved /
    # pinned / digested / domain。content 也支持，会同步重建 embedding。
    # 内容大小受 §5 limits.max_bucket_bytes 约束；钉选量受 max_pinned 约束。
    # =============================================================
    @mcp.custom_route("/api/bucket/{bucket_id}/edit", methods=["PATCH", "POST"])
    async def api_bucket_edit(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        bucket = await sh.bucket_mgr.get(bucket_id)
        if not bucket:
            return JSONResponse({"error": "bucket not found"}, status_code=404)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        updates: dict = {}

        # --- 字符串型 ---
        if isinstance(body.get("name"), str):
            nm = body["name"].strip()[:120]
            if nm:
                updates["name"] = nm

        if isinstance(body.get("tags"), list):
            # 接受 ["a","b"]
            tags = [str(t).strip() for t in body["tags"] if str(t).strip()]
            updates["tags"] = tags
        elif isinstance(body.get("tags"), str):
            # 也接受 "a, b"
            tags = [t.strip() for t in body["tags"].split(",") if t.strip()]
            updates["tags"] = tags

        if isinstance(body.get("domain"), list):
            doms = [str(d).strip() for d in body["domain"] if str(d).strip()]
            updates["domain"] = doms
        elif isinstance(body.get("domain"), str) and body["domain"].strip():
            updates["domain"] = [d.strip() for d in body["domain"].split(",") if d.strip()]

        # --- 数值/布尔型 ---
        if "importance" in body:
            try:
                imp = int(body["importance"])
                if 1 <= imp <= 10:
                    updates["importance"] = imp
            except (TypeError, ValueError):
                pass

        for flag in ("resolved", "digested"):
            if flag in body:
                updates[flag] = bool(body[flag])

        # pinned 需要走配额检查
        if "pinned" in body:
            new_pinned = bool(body["pinned"])
            cur_pinned = bool(bucket["metadata"].get("pinned", False))
            if new_pinned and not cur_pinned:
                quota_err = await _check_pinned_quota()
                if quota_err:
                    return JSONResponse({"error": quota_err}, status_code=400)
                updates["pinned"] = True
                updates["importance"] = 10
                updates["type"] = "permanent"
            elif (not new_pinned) and cur_pinned:
                updates["pinned"] = False
                if bucket["metadata"].get("type") == "permanent":
                    updates["type"] = "dynamic"

        # content 替换 —— 走 §5 大小校验
        new_content = body.get("content")
        if isinstance(new_content, str) and new_content.strip() and new_content != bucket.get("content", ""):
            size_err = _check_content_size(new_content)
            if size_err:
                return JSONResponse({"error": size_err}, status_code=400)
            updates["content"] = new_content

        # type 字段直接改（不经 pinned 联动，调用方自己负责一致性）
        _valid_types = {"dynamic", "permanent", "feel", "plan", "letter", "i"}
        if isinstance(body.get("type"), str) and body["type"] in _valid_types:
            if body["type"] != bucket["metadata"].get("type"):
                updates["type"] = body["type"]

        if not updates:
            return JSONResponse({"error": "nothing to update"}, status_code=400)

        try:
            ok = await sh.bucket_mgr.update(bucket_id, **updates)
            if not ok:
                return JSONResponse({"error": "update failed"}, status_code=500)
            if "content" in updates:
                try:
                    await sh.embedding_engine.generate_and_store(bucket_id, updates["content"])
                except Exception as e:
                    logger.warning(f"edit: re-embedding failed for {bucket_id}: {e}")
                try:
                    sh.dehydrator.invalidate_cache(bucket["content"])
                except Exception:
                    pass
            return JSONResponse({"ok": True, "id": bucket_id, "updated": list(updates.keys())})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    # =============================================================
    # /api/export  — 完整记忆打包导出
    # 导出内容：所有 bucket markdown + embeddings.db + export_meta.json（含 embedding 模型信息）
    # 不导出 config（避免 api_key 等密钥泄露）
    # export_meta.json 中的 embedding 字段供导入端检查模型一致性。
    # =============================================================
    @mcp.custom_route("/api/export", methods=["GET"])
    async def api_export(request: Request) -> Response:
        from starlette.responses import StreamingResponse, JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        import io
        import zipfile

        buckets_dir = sh.config.get("buckets_dir", "")
        if not buckets_dir or not os.path.isdir(buckets_dir):
            return JSONResponse({"error": f"buckets_dir not found: {buckets_dir}"}, status_code=500)

        buf = io.BytesIO()
        try:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                # 1) bucket markdowns
                for root, _dirs, files in os.walk(buckets_dir):
                    for fn in files:
                        if not fn.endswith(".md"):
                            continue
                        full = os.path.join(root, fn)
                        rel = os.path.relpath(full, buckets_dir)
                        arc = os.path.join("buckets", rel)
                        try:
                            zf.write(full, arc)
                        except Exception as e:
                            logger.warning(f"export: skip {full}: {e}")

                # 2) embeddings.db（如果存在）
                emb_path = sh.embedding_engine.db_path if hasattr(sh.embedding_engine, "db_path") else None
                if emb_path and os.path.isfile(emb_path):
                    try:
                        zf.write(emb_path, "embeddings.db")
                    except Exception as e:
                        logger.warning(f"export: skip embeddings.db: {e}")

                # 3) export_meta.json — 包含 embedding 模型信息，供导入端检查模型一致性
                # 不包含 config（避免泄露 api_key 等密钥）
                try:
                    from datetime import datetime as _dt
                    _emb_backend = getattr(sh.embedding_engine, "_backend", None)
                    _emb_model = str(getattr(sh.embedding_engine, "model", "") or "")
                    try:
                        _emb_dim = int(_emb_backend.vector_dim()) if _emb_backend else 0
                    except Exception:
                        _emb_dim = 0
                    _emb_be_name = str(getattr(sh.embedding_engine, "backend", "") or "")
                    meta: dict = {
                        "exported_at": _dt.now().isoformat(timespec="seconds"),
                        "version": sh.version,
                        "embedding": {
                            "model": _emb_model,
                            "dim": _emb_dim,
                            "backend": _emb_be_name,
                        },
                    }
                    # stats 失败时不影响 meta 写入（测试环境 mock 对象无法序列化）
                    try:
                        meta["stats"] = await sh.bucket_mgr.get_stats()
                    except Exception:
                        pass
                    zf.writestr("export_meta.json", _json_lib.dumps(meta, ensure_ascii=False, indent=2))
                except Exception as e:
                    logger.warning(f"export: meta failed: {e}")
        except Exception as e:
            return JSONResponse({"error": f"export failed: {e}"}, status_code=500)

        buf.seek(0)
        fname = f"ombre_export_{int(time.time())}.zip"
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )


    # =============================================================
    # /api/migrate/* — 完整记忆包（zip）导入
    # 流程：POST /upload → GET /status（含冲突列表） → POST /apply（带决策）→ 轮询 GET /status
    # =============================================================

    @mcp.custom_route("/api/migrate/upload", methods=["POST"])
    async def api_migrate_upload(request: Request) -> Response:
        """上传 ombre_export_*.zip，解析内容并识别冲突，不实际写入。

        Body: multipart/form-data，字段名 'file'；或直接 POST zip 字节（Content-Type: application/zip）。
        成功返回解析状态（含冲突列表、embedding 模型匹配情况）。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        if sh.migrate_engine.is_busy:
            return JSONResponse({"error": "已有迁移任务正在进行，请等待完成后再上传"}, status_code=409)

        content_type = request.headers.get("content-type", "")
        try:
            if "multipart/form-data" in content_type:
                form = await request.form()
                file_field = form.get("file")
                if not file_field or isinstance(file_field, str):
                    return JSONResponse({"error": "缺少 file 字段"}, status_code=400)
                zip_bytes = await file_field.read()
            else:
                zip_bytes = await request.body()

            if not zip_bytes:
                return JSONResponse({"error": "文件为空"}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": f"读取上传内容失败: {e}"}, status_code=400)

        result = await sh.migrate_engine.parse_zip(zip_bytes)
        if not result.get("ok"):
            return JSONResponse(result, status_code=422)
        return JSONResponse(result)


    @mcp.custom_route("/api/migrate/status", methods=["GET"])
    async def api_migrate_status(request: Request) -> Response:
        """查询当前迁移任务状态（解析结果、冲突列表、执行进度、重新向量化进度）。"""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse(sh.migrate_engine.get_status())


    @mcp.custom_route("/api/migrate/apply", methods=["POST"])
    async def api_migrate_apply(request: Request) -> Response:
        """执行导入，携带冲突决策。

        Body (JSON):
            decisions: {bucket_id: "skip" | "overwrite" | "keep_both"}

        无冲突的 bucket 自动导入，无需出现在 decisions 中。
        冲突但未在 decisions 中的 bucket 默认 skip（安全优先）。
        成功启动后台任务返回 202；任务完成前轮询 GET /api/migrate/status。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        if sh.migrate_engine.phase != "parsed":
            return JSONResponse(
                {"error": f"当前状态为 '{sh.migrate_engine.phase}'，apply 需要先完成 upload 解析（phase=parsed）"},
                status_code=409,
            )

        try:
            body = await request.json()
        except Exception:
            body = {}

        decisions: dict[str, str] = {}
        raw_decisions = body.get("decisions", {})
        if isinstance(raw_decisions, dict):
            valid_opts = {"skip", "overwrite", "keep_both"}
            for bid, decision in raw_decisions.items():
                if isinstance(bid, str) and isinstance(decision, str) and decision in valid_opts:
                    decisions[bid] = decision

        # 后台执行（apply 可能耗时较长，含重新向量化）
        async def _run_apply():
            try:
                await sh.migrate_engine.apply(decisions)
            except Exception as e:
                logger.error(f"[migrate] background apply error: {e}", exc_info=True)

        asyncio.create_task(_run_apply())

        return JSONResponse(
            {"ok": True, "message": "导入任务已启动，请轮询 GET /api/migrate/status 查看进度"},
            status_code=202,
        )
