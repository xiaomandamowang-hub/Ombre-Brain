"""
========================================
tools/breath/feel.py — feel 通道
========================================

走 breath(domain="feel") 或 breath(tags="feel") 时进入这里。返回我留下
过的所有 feel（按时间倒序），新 feel 全文，老 feel 折叠成一行摘要，
保证总 token 不超出预算。

关键行为：
- 列出所有 type=feel 的桶，按 created 倒序
- 在 max_tokens 预算内逐条放全文，超了就转成 [日期] [bucket_id] 摘要…
- 末尾附「更早的 feel 摘要」段落，提示用 trace 看完整内容

不做什么（边界）：
- 不做语义检索；feel 不通过 query 过滤（feel 数量本身就少）
- 不做 dehydrate 调用，feel 原文短，直接展示

对外暴露：surface_feels(max_tokens) → str
========================================
"""

from .. import _runtime as rt
from utils import strip_wikilinks, count_tokens_approx


async def surface_feels(max_tokens: int) -> str:
    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
        feels = [b for b in all_buckets if b.get("metadata", {}).get("type") == "feel"]
        feels.sort(key=lambda b: b.get("metadata", {}).get("created", ""), reverse=True)
        if not feels:
            return "没有留下过 feel。"
        full_lines: list[str] = []
        collapsed_lines: list[str] = []
        used = 0
        for f in feels:
            created = f["metadata"].get("created", "")
            full_text = strip_wikilinks(f["content"])
            full_entry = f"[{created}] [bucket_id:{f['id']}]\n{full_text}"
            cost = count_tokens_approx(full_entry)
            if used + cost <= max_tokens:
                full_lines.append(full_entry)
                used += cost
            else:
                snippet = full_text.replace("\n", " ").strip()[:60]
                collapsed_lines.append(
                    f"[{created[:10]}] [bucket_id:{f['id']}] {snippet}…"
                )
        out = "=== 你留下的 feel（新→旧）===\n" + "\n---\n".join(full_lines)
        if collapsed_lines:
            out += (
                f"\n\n--- 更早的 feel 摘要（{len(collapsed_lines)} 条，已折叠）---\n"
                + "\n".join(collapsed_lines)
                + "\n（需要看完整可用 trace 或在仪表板查看）"
            )
        return out
    except Exception as e:
        rt.logger.error(f"Feel retrieval failed: {e}")
        return "读取 feel 失败。"
