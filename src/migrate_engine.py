"""
========================================
migrate_engine.py — 完整记忆包导入引擎
========================================

把 /api/export 产生的 zip 包（buckets/*.md + embeddings.db + export_meta.json）
以增量 merge 方式写入当前系统。

关键行为：
- 解析 zip，识别 bucket 文件，读取 export_meta.json 中的 embedding 模型信息
- 对比导入包与当前系统的 embedding 模型，决定是否保留向量数据
- 检测 bucket ID 冲突，返回冲突列表等待她/他决策
- 冲突决策：skip（跳过）| overwrite（覆盖）| keep_both（保留两者，重分配 ID）
- embedding 模型一致 → 合并向量数据；不一致 → 仅导入 md 文件，完成后自动重新向量化

状态机：idle → parsed → applying → reindexing → done | error

不做什么：
- 不调用 LLM（不做内容解析/摘要/打标，只做文件迁移）
- 不修改 config
- 不做对话历史解析（那是 import_memory.py 的事）

对外暴露：MigrateEngine 类（被 server.py 实例化并注入路由）
========================================
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sqlite3
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from typing import Any, Optional

import frontmatter

logger = logging.getLogger("ombre_brain.migrate")

# ============================================================
# 状态常量
# ============================================================
PHASE_IDLE = "idle"
PHASE_PARSED = "parsed"
PHASE_APPLYING = "applying"
PHASE_REINDEXING = "reindexing"
PHASE_DONE = "done"
PHASE_ERROR = "error"

# bucket type → 存储子目录映射（与 bucket_manager.py 保持一致）
_TYPE_SUBDIR: dict[str, str] = {
    "permanent": "permanent",
    "dynamic": "dynamic",
    "archive": "archive",
    "feel": "feel",
    "plan": "plans",
    "letter": "letters",
}

# 默认子目录（unknown type 时）
_DEFAULT_SUBDIR = "dynamic"


# ============================================================
# 数据类
# ============================================================

@dataclass
class _ParsedBucket:
    """zip 内解析到的单个 bucket 文件。"""
    bucket_id: str
    arc_path: str        # zip 内路径，e.g. "buckets/dynamic/foo/name_id.md"
    md_bytes: bytes      # 原始文件字节
    name: str
    bucket_type: str
    domain: list[str]
    created: str


@dataclass
class ConflictInfo:
    """导入包内某 bucket_id 与当前系统冲突的描述。"""
    bucket_id: str
    import_name: str
    import_created: str
    current_name: str
    current_created: str


# ============================================================
# 辅助函数
# ============================================================

def _parse_md_meta(raw: bytes) -> tuple[dict, str]:
    """从 md 字节中解析 frontmatter 元数据 + 正文。失败返回空 dict + 空串。"""
    try:
        post = frontmatter.loads(raw.decode("utf-8", errors="replace"))
        return dict(post.metadata), post.content
    except Exception:
        return {}, ""


def _safe_str(val: Any, max_len: int = 512) -> str:
    """安全地将值转为字符串，并截断。"""
    return str(val)[:max_len] if val is not None else ""


# ============================================================
# MigrateEngine
# ============================================================

class MigrateEngine:
    """完整记忆包（zip）导入引擎。每个服务进程单例使用；同一时刻只允许一个任务。"""

    def __init__(self, config: dict, bucket_mgr: Any, embedding_engine: Any) -> None:
        self._config = config
        self._bucket_mgr = bucket_mgr
        self._embedding_engine = embedding_engine

        # ---- 状态 ----
        self._phase: str = PHASE_IDLE

        # ---- 解析阶段产物 ----
        self._parsed_buckets: list[_ParsedBucket] = []
        self._conflicts: list[ConflictInfo] = []
        self._import_model: str = ""
        self._import_model_dim: int = 0
        self._import_backend: str = ""
        self._has_embeddings: bool = False
        self._zip_db_bytes: Optional[bytes] = None

        # ---- 执行阶段计数 ----
        self._apply_total: int = 0
        self._apply_done: int = 0
        self._apply_imported: int = 0
        self._apply_skipped: int = 0
        self._apply_errors: list[str] = []

        # ---- 重新向量化阶段 ----
        self._reindex_total: int = 0
        self._reindex_done: int = 0
        self._reindex_errors: int = 0
        self._buckets_to_reindex: list[tuple[str, str]] = []  # (bucket_id, content)

        # ---- 错误信息 ----
        self._error_message: str = ""

    # ----------------------------------------------------------
    # 属性
    # ----------------------------------------------------------

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def is_busy(self) -> bool:
        return self._phase in (PHASE_APPLYING, PHASE_REINDEXING)

    def _embedding_match(self) -> bool:
        """当前 embedding 模型是否与导入包一致。"""
        if not self._import_model:
            return False
        current_model = getattr(self._embedding_engine, "model", "")
        return bool(current_model) and self._import_model == current_model

    # ----------------------------------------------------------
    # 状态查询
    # ----------------------------------------------------------

    def get_status(self) -> dict:
        return {
            "phase": self._phase,
            "total_buckets": len(self._parsed_buckets),
            "conflicts_count": len(self._conflicts),
            "conflicts": [
                {
                    "bucket_id": c.bucket_id,
                    "import_name": c.import_name,
                    "import_created": c.import_created,
                    "current_name": c.current_name,
                    "current_created": c.current_created,
                }
                for c in self._conflicts
            ],
            "import_model": self._import_model,
            "import_backend": self._import_backend,
            "current_model": getattr(self._embedding_engine, "model", ""),
            "embedding_match": self._embedding_match(),
            "has_embeddings": self._has_embeddings,
            "apply_progress": {
                "done": self._apply_done,
                "total": self._apply_total,
            },
            "reindex_progress": {
                "done": self._reindex_done,
                "total": self._reindex_total,
                "errors": self._reindex_errors,
            },
            "apply_errors": self._apply_errors[-20:],
            "result": {
                "imported": self._apply_imported,
                "skipped": self._apply_skipped,
            },
            "error": self._error_message,
        }

    # ----------------------------------------------------------
    # 第一步：解析 zip
    # ----------------------------------------------------------

    async def parse_zip(self, zip_bytes: bytes) -> dict:
        """解析 zip 字节，识别 buckets 文件和 embedding 信息，检测 ID 冲突。

        解析成功后 phase → 'parsed'，并返回包含冲突列表的状态字典。
        """
        if self.is_busy:
            return {"ok": False, "error": f"当前状态为 {self._phase}，请等待任务完成后再上传"}

        # 重置所有状态
        self._phase = PHASE_IDLE
        self._parsed_buckets = []
        self._conflicts = []
        self._import_model = ""
        self._import_model_dim = 0
        self._import_backend = ""
        self._has_embeddings = False
        self._zip_db_bytes = None
        self._apply_errors = []
        self._apply_imported = 0
        self._apply_skipped = 0
        self._apply_total = 0
        self._apply_done = 0
        self._buckets_to_reindex = []
        self._reindex_total = 0
        self._reindex_done = 0
        self._reindex_errors = 0
        self._error_message = ""

        # ---- 在线程中解析 zip（避免阻塞事件循环）----
        try:
            parsed = await asyncio.to_thread(self._parse_zip_sync, zip_bytes)
        except zipfile.BadZipFile as e:
            self._phase = PHASE_ERROR
            self._error_message = f"无效的 zip 文件: {e}"
            return {"ok": False, "error": self._error_message}
        except Exception as e:
            self._phase = PHASE_ERROR
            self._error_message = f"zip 解析失败: {e}"
            logger.error(f"[migrate] parse_zip error: {e}", exc_info=True)
            return {"ok": False, "error": self._error_message}

        self._parsed_buckets = parsed["buckets"]
        self._import_model = parsed["import_model"]
        self._import_model_dim = parsed["import_model_dim"]
        self._import_backend = parsed["import_backend"]
        self._has_embeddings = parsed["has_embeddings"]
        self._zip_db_bytes = parsed.get("db_bytes")

        if not self._parsed_buckets:
            self._phase = PHASE_ERROR
            self._error_message = "zip 内未找到任何 bucket markdown 文件（期望路径前缀：buckets/）"
            return {"ok": False, "error": self._error_message}

        # ---- 识别冲突（需要异步查当前桶） ----
        await self._identify_conflicts()

        self._phase = PHASE_PARSED
        return {
            "ok": True,
            **self.get_status(),
        }

    def _parse_zip_sync(self, zip_bytes: bytes) -> dict:
        """同步解析 zip（在 to_thread 中执行）。"""
        buf = io.BytesIO(zip_bytes)
        buckets: list[_ParsedBucket] = []
        import_model = ""
        import_model_dim = 0
        import_backend = ""
        has_embeddings = False
        db_bytes: Optional[bytes] = None

        with zipfile.ZipFile(buf, "r") as zf:
            names = set(zf.namelist())

            # 1) 读取 export_meta.json → 获取 embedding 模型信息
            if "export_meta.json" in names:
                try:
                    raw_meta = zf.read("export_meta.json")
                    meta = json.loads(raw_meta.decode("utf-8"))
                    emb_info = meta.get("embedding", {})
                    import_model = emb_info.get("model", "")
                    import_model_dim = int(emb_info.get("dim") or 0)
                    import_backend = emb_info.get("backend", "")
                except Exception as e:
                    logger.warning(f"[migrate] export_meta.json 解析失败，将跳过向量恢复: {e}")

            # 2) 检查是否包含 embeddings.db
            if "embeddings.db" in names:
                try:
                    db_bytes = zf.read("embeddings.db")
                    has_embeddings = bool(db_bytes)
                except Exception as e:
                    logger.warning(f"[migrate] embeddings.db 读取失败: {e}")

            # 3) 遍历 bucket markdown 文件
            for arc_path in sorted(names):
                # 期望格式：buckets/<subdir>/[domain/]filename.md
                if not arc_path.startswith("buckets/") or not arc_path.endswith(".md"):
                    continue
                try:
                    raw = zf.read(arc_path)
                    meta, _content = _parse_md_meta(raw)

                    # 从 frontmatter 里取 bucket_id；没有则从文件名推断
                    bucket_id = meta.get("id") or meta.get("bucket_id") or ""
                    if not bucket_id:
                        # 文件名格式：name_bucketid.md 或 bucketid.md
                        stem = os.path.splitext(os.path.basename(arc_path))[0]
                        parts = stem.rsplit("_", 1)
                        bucket_id = parts[-1] if len(parts) > 1 else stem

                    if not bucket_id:
                        logger.warning(f"[migrate] skip {arc_path}: 无法确定 bucket_id")
                        continue

                    buckets.append(_ParsedBucket(
                        bucket_id=bucket_id,
                        arc_path=arc_path,
                        md_bytes=raw,
                        name=_safe_str(meta.get("name", bucket_id), 200),
                        bucket_type=_safe_str(meta.get("type", "dynamic"), 32),
                        domain=meta.get("domain") or [],
                        created=_safe_str(meta.get("created", ""), 32),
                    ))
                except Exception as e:
                    logger.warning(f"[migrate] skip {arc_path}: {e}")

        return {
            "buckets": buckets,
            "import_model": import_model,
            "import_model_dim": import_model_dim,
            "import_backend": import_backend,
            "has_embeddings": has_embeddings,
            "db_bytes": db_bytes,
        }

    async def _identify_conflicts(self) -> None:
        """遍历解析到的 bucket，查询当前系统，找出 ID 冲突。"""
        conflicts: list[ConflictInfo] = []
        for pb in self._parsed_buckets:
            existing = await self._bucket_mgr.get(pb.bucket_id)
            if existing is not None:
                emeta = existing.get("metadata", {})
                conflicts.append(ConflictInfo(
                    bucket_id=pb.bucket_id,
                    import_name=pb.name,
                    import_created=pb.created,
                    current_name=_safe_str(emeta.get("name", pb.bucket_id), 200),
                    current_created=_safe_str(emeta.get("created", ""), 32),
                ))
        self._conflicts = conflicts

    # ----------------------------------------------------------
    # 第二步：执行导入（带冲突决策）
    # ----------------------------------------------------------

    async def apply(self, decisions: dict[str, str]) -> None:
        """执行导入。

        decisions: {bucket_id: "skip" | "overwrite" | "keep_both"}
        冲突但未出现在 decisions 中的 bucket → 默认 skip（安全优先）。
        无冲突的 bucket 直接导入，无需决策。
        """
        if self._phase != PHASE_PARSED:
            raise RuntimeError(f"当前状态为 {self._phase}，apply 需要先完成 parse_zip")

        self._phase = PHASE_APPLYING
        self._apply_total = len(self._parsed_buckets)
        self._apply_done = 0
        self._apply_imported = 0
        self._apply_skipped = 0
        self._apply_errors = []
        self._buckets_to_reindex = []

        conflict_ids = {c.bucket_id for c in self._conflicts}
        embedding_matches = self._embedding_match()
        buckets_dir = self._config.get("buckets_dir", "buckets")

        try:
            for pb in self._parsed_buckets:
                try:
                    is_conflict = pb.bucket_id in conflict_ids
                    decision = decisions.get(pb.bucket_id, "skip") if is_conflict else "import"

                    if is_conflict and decision == "skip":
                        self._apply_skipped += 1
                        self._apply_done += 1
                        continue

                    if is_conflict and decision == "overwrite":
                        # 删除旧桶文件（含 embedding），再写入新文件
                        await self._bucket_mgr.delete(pb.bucket_id)
                        target_id = pb.bucket_id
                    elif is_conflict and decision == "keep_both":
                        # 分配新 ID，两个桶共存
                        target_id = str(uuid.uuid4())
                    else:
                        # 无冲突，直接用原 ID
                        target_id = pb.bucket_id

                    # 写入 markdown 文件
                    content = await asyncio.to_thread(
                        self._write_bucket_file, pb, target_id, buckets_dir
                    )
                    self._apply_imported += 1

                    # 记录需要重新向量化的桶（仅当 embedding 不匹配时）
                    if not embedding_matches and content.strip():
                        self._buckets_to_reindex.append((target_id, content))

                except Exception as e:
                    err_msg = f"[{pb.bucket_id}] {pb.name[:60]}: {e}"
                    logger.error(f"[migrate] apply error: {err_msg}", exc_info=True)
                    self._apply_errors.append(err_msg)
                    self._apply_skipped += 1

                self._apply_done += 1

            # ---- 向量数据处理 ----
            if embedding_matches and self._has_embeddings and self._zip_db_bytes:
                # 模型一致 → 从 zip db 合并向量（只合并我们实际导入的 bucket）
                imported_ids = {
                    pb.bucket_id
                    for pb in self._parsed_buckets
                    if pb.bucket_id not in conflict_ids
                    or decisions.get(pb.bucket_id, "skip") != "skip"
                }
                try:
                    await asyncio.to_thread(
                        self._merge_embeddings,
                        self._zip_db_bytes,
                        imported_ids,
                    )
                except Exception as e:
                    logger.warning(f"[migrate] 向量合并失败（不影响文本导入）: {e}")

            # ---- 重新向量化（仅 embedding 不匹配时） ----
            if not embedding_matches and self._buckets_to_reindex:
                self._phase = PHASE_REINDEXING
                self._reindex_total = len(self._buckets_to_reindex)
                self._reindex_done = 0
                self._reindex_errors = 0
                await self._reindex_all()
            else:
                self._phase = PHASE_DONE

        except Exception as e:
            self._phase = PHASE_ERROR
            self._error_message = str(e)
            logger.error(f"[migrate] apply failed: {e}", exc_info=True)

    def _write_bucket_file(
        self, pb: _ParsedBucket, target_id: str, buckets_dir: str
    ) -> str:
        """（在线程中执行）写入 bucket markdown 文件，返回正文内容。"""
        meta, content = _parse_md_meta(pb.md_bytes)

        # 若 ID 变更（keep_both），更新 frontmatter 中的 id 字段
        if target_id != pb.bucket_id:
            meta["id"] = target_id

        # 确定目标目录（按类型 + domain）
        btype = meta.get("type") or pb.bucket_type or "dynamic"
        subdir = _TYPE_SUBDIR.get(btype, _DEFAULT_SUBDIR)

        # 获取主 domain（与 bucket_manager 保持一致）
        domain = meta.get("domain") or pb.domain or []
        if btype == "feel":
            primary_domain = "沉淀物"
        elif btype == "plan":
            primary_domain = meta.get("status", "active")
        elif btype == "letter":
            primary_domain = "history"
        elif isinstance(domain, list) and domain:
            primary_domain = domain[0]
        elif isinstance(domain, str) and domain:
            primary_domain = domain
        else:
            primary_domain = "general"

        target_dir = os.path.join(buckets_dir, subdir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)

        # 确定文件名
        orig_filename = os.path.basename(pb.arc_path)
        if target_id != pb.bucket_id:
            # 把文件名中的旧 ID 替换为新 ID
            if pb.bucket_id in orig_filename:
                orig_filename = orig_filename.replace(pb.bucket_id, target_id, 1)
            else:
                # 兜底：直接用 name_newid.md
                safe_name = pb.name[:40].replace("/", "_").replace("\x00", "")
                orig_filename = f"{safe_name}_{target_id}.md" if safe_name else f"{target_id}.md"

        target_path = os.path.join(target_dir, orig_filename)

        # 重新序列化 frontmatter + 正文
        post = frontmatter.Post(content, **meta)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

        logger.debug(f"[migrate] wrote {target_path} (id={target_id})")
        return content

    def _merge_embeddings(self, db_bytes: bytes, imported_ids: set[str]) -> None:
        """（在线程中执行）把 zip 内 embeddings.db 的向量合并进当前 db。

        只合并实际导入的 bucket（imported_ids）。
        已存在的 bucket_id 不覆盖（INSERT OR IGNORE）。
        """
        current_db = getattr(self._embedding_engine, "db_path", "")
        if not current_db or not os.path.isfile(current_db):
            logger.warning("[migrate] 当前 embeddings.db 路径无效，跳过向量合并")
            return

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            tf.write(db_bytes)
            tmp_path = tf.name

        try:
            src = sqlite3.connect(tmp_path)
            dst = sqlite3.connect(current_db)
            try:
                # 检查表结构是否存在
                tables = {row[0] for row in src.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
                if "embeddings" not in tables:
                    logger.warning("[migrate] 导入包 embeddings.db 缺少 embeddings 表，跳过")
                    return

                # 只取我们实际导入的 bucket 的向量
                if not imported_ids:
                    return

                placeholders = ",".join("?" * len(imported_ids))
                rows = src.execute(
                    f"SELECT id, vector FROM embeddings WHERE id IN ({placeholders})",
                    tuple(imported_ids),
                ).fetchall()

                if rows:
                    dst.executemany(
                        "INSERT OR IGNORE INTO embeddings (id, vector) VALUES (?, ?)",
                        rows,
                    )
                    dst.commit()
                    logger.info(f"[migrate] 合并了 {len(rows)} 条 embedding 向量")
            finally:
                src.close()
                dst.close()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    async def _reindex_all(self) -> None:
        """对 embedding 不匹配时导入的 bucket 重新生成向量。"""
        emb = self._embedding_engine
        if not getattr(emb, "enabled", False):
            logger.warning("[migrate] embedding engine 未启用，跳过重新向量化")
            self._phase = PHASE_DONE
            return

        for bucket_id, content in self._buckets_to_reindex:
            if not content.strip():
                self._reindex_done += 1
                continue
            try:
                await emb.generate_and_store(bucket_id, content)
            except Exception as e:
                logger.warning(f"[migrate] reindex {bucket_id[:12]}: {e}")
                self._reindex_errors += 1
            self._reindex_done += 1

        logger.info(
            f"[migrate] 重新向量化完成: "
            f"{self._reindex_done - self._reindex_errors} 成功, "
            f"{self._reindex_errors} 失败"
        )
        self._phase = PHASE_DONE
