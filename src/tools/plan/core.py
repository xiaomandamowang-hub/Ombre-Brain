"""
========================================
tools/plan/core.py — plan / letter_write / letter_read 实现
========================================

plan 桶记录我答应过、答应自己或想完成的事；letter 桶是她/他与 OB
之间的长信件。它们都是独立类型，永久保存、不衰减、不出现在普通
breath 中。

关键行为：
- plan_create：去重（同正文 + status=active 已存在 → 直接返回原 ID），
  写入 type=plan + status + weight + change_log 起点
- letter_write：原文永久保存，author 必须是 user/claude，写入
  type=letter + author/title/letter_date 元数据
- letter_read：默认按时间倒序；带 query 时走向量近邻；支持 author /
  date_from / date_to 过滤；返回完整原文

不做什么（边界）：
- plan 不做向量去重，只做精确文本去重
- letter 永不合并、永不压缩、永不被衰减归档

对外暴露：plan_create / letter_write / letter_read
========================================
"""

from typing import Optional

from .. import _runtime as rt
from utils import strip_wikilinks


async def plan_create(
    content: str,
    status: Optional[str] = "active",
    related_bucket: Optional[str] = "",
    weight: Optional[float] = 0.5,
    why_remembered: Optional[str] = "",
) -> str:
    if status is None: status = "active"
    if related_bucket is None: related_bucket = ""
    if weight is None: weight = 0.5
    if why_remembered is None: why_remembered = ""
    weight = max(0.0, min(1.0, float(weight)))
    why_remembered = str(why_remembered).strip()[:500]
    await rt.decay_engine.ensure_started()
    if not content or not content.strip():
        return "内容为空，无法登记计划。"
    status = status.strip().lower()
    if status not in ("active", "resolved", "abandoned"):
        status = "active"

    norm = content.strip()
    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
        for b in all_buckets:
            m = b.get("metadata", {})
            if (
                m.get("type") == "plan"
                and m.get("status", "active") == "active"
                and (b.get("content") or "").strip() == norm
            ):
                return f"跟原有 active plan 完全重复→{b['id']}（未重复登记）"
    except Exception as e:
        rt.logger.warning(f"plan() dedup scan failed: {e}")

    bucket_id = await rt.bucket_mgr.create(
        content=content.strip(),
        tags=["__plan__"],
        importance=7,
        domain=["plan"],
        valence=0.5,
        arousal=0.4,
        name=None,
        bucket_type="plan",
        why_remembered=why_remembered,
        weight=weight,
        source_tool="plan",
    )
    from .._common import append_plan_change_log
    initial_log = append_plan_change_log([], "created", to=status)
    update_kwargs = {"status": status, "change_log": initial_log}
    if related_bucket.strip():
        update_kwargs["related_bucket"] = related_bucket.strip()
    try:
        await rt.bucket_mgr.update(bucket_id, **update_kwargs)
    except Exception as e:
        rt.logger.warning(f"plan() failed to set status/related: {e}")
    try:
        await rt.embedding_engine.generate_and_store(bucket_id, content)
    except Exception:
        pass
    return f"📋plan→{bucket_id} [{status}]"


async def letter_write(
    author: str,
    content: str,
    user_name: Optional[str] = "",
    title: Optional[str] = "",
    date: Optional[str] = "",
) -> str:
    if user_name is None: user_name = ""
    if title is None: title = ""
    if date is None: date = ""
    a = author.strip().lower()
    if a not in ("user", "claude"):
        return "author 必须是 'user' 或 'claude'。"
    if not content or not content.strip():
        return "信件内容不能为空。"

    extra_meta = {"author": a}
    if user_name.strip():
        extra_meta["user_name"] = user_name.strip()
    if title.strip():
        extra_meta["title"] = title.strip()[:120]
    if date.strip():
        extra_meta["letter_date"] = date.strip()

    bucket_id = await rt.bucket_mgr.create(
        content=content.strip(),
        tags=["__letter__"],
        importance=10,
        domain=["letter"],
        valence=0.5,
        arousal=0.3,
        name=(title.strip()[:60] or f"{a}_{date.strip() or 'letter'}"),
        bucket_type="letter",
        source_tool="letter",
    )
    try:
        await rt.bucket_mgr.update(bucket_id, **extra_meta)
    except Exception as e:
        rt.logger.warning(f"letter_write update meta failed: {e}")
    try:
        await rt.embedding_engine.generate_and_store(bucket_id, content)
    except Exception:
        pass
    return f"💌letter→{bucket_id} [{a}]"


async def letter_read(
    query: Optional[str] = "",
    limit: Optional[int] = 10,
    author: Optional[str] = "",
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> str:
    if query is None: query = ""
    if limit is None: limit = 10
    if author is None: author = ""
    if date_from is None: date_from = ""
    if date_to is None: date_to = ""
    limit = max(1, min(50, limit))
    try:
        all_b = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        return f"读取信件失败: {e}"
    letters = [b for b in all_b if b["metadata"].get("type") == "letter"]
    if author.strip().lower() in ("user", "claude"):
        letters = [b for b in letters if b["metadata"].get("author") == author.strip().lower()]

    def _within(b):
        d = b["metadata"].get("letter_date") or b["metadata"].get("created", "")
        if date_from and d and d < date_from: return False
        if date_to and d and d > date_to: return False
        return True

    letters = [b for b in letters if _within(b)]

    if query and query.strip() and rt.embedding_engine and getattr(rt.embedding_engine, "enabled", False):
        try:
            sims = await rt.embedding_engine.search_similar(query, top_k=limit * 3)
            id_score = {bid: sc for bid, sc in sims}
            letters.sort(key=lambda b: id_score.get(b["id"], 0.0), reverse=True)
        except Exception as e:
            rt.logger.warning(f"letter_read vector search failed: {e}")
            letters.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
    else:
        letters.sort(key=lambda b: b["metadata"].get("letter_date") or b["metadata"].get("created", ""), reverse=True)

    letters = letters[:limit]
    if not letters:
        return "没有找到匹配的信件。"
    parts = []
    for b in letters:
        m = b["metadata"]
        a = m.get("author", "?")
        d = (m.get("letter_date") or m.get("created", ""))[:10]
        title = m.get("title") or m.get("name", "")
        parts.append(
            f"[{b['id']}] {a} · {d}{(' · ' + title) if title else ''}\n"
            + strip_wikilinks(b["content"])
        )
    return "=== 信件 ===\n" + "\n\n---\n\n".join(parts)
