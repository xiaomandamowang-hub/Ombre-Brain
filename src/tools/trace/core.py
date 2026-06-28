"""
========================================
tools/trace/core.py — trace 主路径（修改 / 删除 / 重生 embedding）
========================================

trace 是 OB 唯一的「写元数据」入口，承接所有桶字段更新和删除。模型
传什么字段，就改什么字段；-1 / 空串 表示「不改」。

关键行为：
- delete=True → 删 markdown + 同步删 embedding
- 收集传入字段构造 updates dict（含 status/weight/dont_surface/
  why_remembered/pinned/digested/resolved/content/tags/domain 等）
- pinned=1 时强制 importance=10 并做配额检查；pinned=0 仅取消标记
- content 改写时同步重建 embedding，并对 plan 桶追加 change_log
- resolved/digested 切换会附中文语义提示

不做什么（边界）：
- 不创建桶（那是 hold/grow/plan/letter 的事）
- 不返回结构化数据，统一中文短句

对外暴露：trace_core(bucket_id, name, domain, valence, arousal, importance,
                     tags, resolved, pinned, digested, content, delete,
                     status, weight, dont_surface, why_remembered) → str
========================================
"""

from typing import Optional

from .. import _runtime as rt
from .._common import check_content_size, check_pinned_quota


async def trace_core(
    bucket_id: str,
    name: Optional[str] = "",
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    importance: Optional[int] = -1,
    tags: Optional[str] = "",
    resolved: Optional[int] = -1,
    pinned: Optional[int] = -1,
    digested: Optional[int] = -1,
    content: Optional[str] = "",
    delete: Optional[bool] = False,
    status: Optional[str] = "",
    weight: Optional[float] = -1,
    dont_surface: Optional[int] = -1,
    why_remembered: Optional[str] = "",
) -> str:
    if name is None: name = ""
    if domain is None: domain = ""
    if valence is None: valence = -1
    if arousal is None: arousal = -1
    if importance is None: importance = -1
    if tags is None: tags = ""
    if resolved is None: resolved = -1
    if pinned is None: pinned = -1
    if digested is None: digested = -1
    if content is None: content = ""
    if delete is None: delete = False
    if status is None: status = ""
    if weight is None: weight = -1
    if dont_surface is None: dont_surface = -1
    if why_remembered is None: why_remembered = ""
    if rt.mark_op:
        rt.mark_op("trace")

    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"

    # --- Delete 模式（F-10：软删除，移入 archive/ + 标 deleted_at）---
    if delete:
        success = await rt.bucket_mgr.delete(bucket_id)
        return f"已将记忆桶存入档案（不可在日常召回中浮现）: {bucket_id}" if success else f"未找到记忆桶: {bucket_id}"

    bucket = await rt.bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

    updates: dict = {}
    if name:
        updates["name"] = name
    if domain:
        updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
    if 0 <= valence <= 1:
        updates["valence"] = valence
    if 0 <= arousal <= 1:
        updates["arousal"] = arousal
    if 1 <= importance <= 10:
        updates["importance"] = importance
    if tags:
        updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if resolved in (0, 1):
        updates["resolved"] = bool(resolved)
    if pinned in (0, 1):
        updates["pinned"] = bool(pinned)
        if pinned == 1:
            if not bucket.get("metadata", {}).get("pinned"):
                err = await check_pinned_quota()
                if err:
                    return err
            updates["importance"] = 10
    if digested in (0, 1):
        updates["digested"] = bool(digested)
    if content:
        size_err = check_content_size(content)
        if size_err:
            return size_err
        updates["content"] = content
    if status:
        s = status.strip().lower()
        if s in ("active", "resolved", "abandoned"):
            updates["status"] = s
    if 0 <= weight <= 1:
        updates["weight"] = float(weight)
    if dont_surface in (0, 1):
        updates["dont_surface"] = bool(dont_surface)
    why_remembered = str(why_remembered).strip()
    if why_remembered == "\\clear":
        updates["why_remembered"] = ""
    elif why_remembered:
        updates["why_remembered"] = why_remembered[:500]

    if not updates:
        return "没有任何字段需要修改。"

    # --- plan 桶：status / content 改变时追加 change_log ---
    if bucket.get("metadata", {}).get("type") == "plan" and ("status" in updates or "content" in updates):
        from .._common import append_plan_change_log
        old_meta = bucket.get("metadata", {})
        history = list(old_meta.get("change_log") or [])
        if "status" in updates and updates["status"] != old_meta.get("status"):
            history = append_plan_change_log(
                history, "status",
                **{"from": old_meta.get("status"), "to": updates["status"]},
            )
        if "content" in updates:
            history = append_plan_change_log(history, "edit")
        updates["change_log"] = history

    success = await rt.bucket_mgr.update(bucket_id, **updates)
    if not success:
        return f"修改失败: {bucket_id}"

    if "content" in updates:
        try:
            await rt.embedding_engine.generate_and_store(bucket_id, updates["content"])
        except Exception:
            pass

    # --- plan 桶人工/Claude 显式 resolve → 联动 related_bucket / resolved_by ---
    # rule.md §1：plan 是承诺，承诺被显式放下，承载它的事件桶也不该再浮上来。
    # 仅在 trace 把 plan.status 改成 resolved 时触发；其他路径（自动二判）不联动。
    cascaded: list[str] = []
    if (
        bucket.get("metadata", {}).get("type") == "plan"
        and updates.get("status") == "resolved"
    ):
        from .._common import cascade_plan_resolved_to_buckets
        # 用更新后的 metadata 视图，确保 related_bucket / resolved_by 是最新值
        merged_meta = {**bucket.get("metadata", {}), **{k: v for k, v in updates.items() if k != "change_log"}}
        try:
            cascaded = await cascade_plan_resolved_to_buckets(merged_meta, bucket_id)
        except Exception as e:
            rt.logger.warning(f"trace plan cascade outer error: {e}")

    changed = ", ".join(f"{k}={v}" for k, v in updates.items() if k != "content")
    if "content" in updates:
        changed += (", content=已替换" if changed else "content=已替换")
    if "resolved" in updates:
        if updates["resolved"]:
            changed += " → 已沉底，只在关键词触发时重新浮现"
        else:
            changed += " → 已重新激活，将参与浮现排序"
    if "digested" in updates:
        if updates["digested"]:
            changed += " → 已隐藏，保留但不再浮现"
        else:
            changed += " → 已取消隐藏，重新参与浮现"
    if cascaded:
        changed += f" → 同步把 {len(cascaded)} 个关联事件桶也标为已放下（{', '.join(cascaded)}）"
    return f"已修改记忆桶 {bucket_id}: {changed}"
