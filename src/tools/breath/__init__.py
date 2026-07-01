"""
========================================
tools/breath/__init__.py — breath 工具的总入口与分支调度
========================================

breath 是「我睁眼看看自己记得什么」。这个文件根据参数把请求路由到
四个分支文件之一：

- feel.py：domain="feel"（或 tags 含 feel/__feel__）→ 拉所有 feel 桶
- importance.py：importance_min >= 1 → 跳过语义，按 importance 拉前 20
- surface.py：query 为空 → 浮现模式（pinned + 加权采样未解决桶 + passive）
- search.py：有 query → 检索模式（关键词 + 向量双通道 + 随机漂浮）

关键行为：
- 入口 dispatch() 做参数 null-safe 兜底、token/result 上限归一化、
  tags/domain 解析，再交给具体分支函数
- 不在这里做实际取桶/调 LLM 的工作

不做什么（边界）：
- 不直接处理 dehydrate/embedding 调用，全部下放到分支模块
- 不做权限校验，MCP 调用方默认是模型自身

对外暴露：dispatch(query, max_tokens, domain, valence, arousal, max_results,
                   importance_min, tags) → str
========================================
"""

from typing import Optional

from .. import _runtime as rt
from .feel import surface_feels
from .importance import surface_by_importance
from .surface import surface_default
from .search import surface_search


async def dispatch(
    query: Optional[str] = "",
    max_tokens: Optional[int] = 0,
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    max_results: Optional[int] = 0,
    importance_min: Optional[int] = -1,
    tags: Optional[str] = "",
) -> dict:
    # --- Null-safe coercion ---
    if query is None: query = ""
    if max_tokens is None: max_tokens = 0
    if domain is None: domain = ""
    if valence is None: valence = -1
    if arousal is None: arousal = -1
    if max_results is None: max_results = 0
    if importance_min is None: importance_min = -1
    if tags is None: tags = ""

    if rt.mark_op:
        rt.mark_op("breath")
    await rt.decay_engine.ensure_started()

    surfacing_cfg = rt.config.get("surfacing", {}) or {}
    default_results = int(surfacing_cfg.get("breath_max_results") or 20)
    default_tokens = int(surfacing_cfg.get("breath_max_tokens") or 10000)
    if max_results <= 0:
        max_results = default_results
    if max_tokens <= 0:
        max_tokens = default_tokens
    max_results = min(max_results, 50)
    max_tokens = min(max_tokens, 20000)

    # --- 解析 tags 过滤；feel/__feel__ 映射到 feel 通道 ---
    tag_filter = [t.strip() for t in tags.split(",") if t.strip()]
    if any(t in ("feel", "__feel__") for t in tag_filter):
        domain = "feel"
        tag_filter = [t for t in tag_filter if t not in ("feel", "__feel__")]

    try:
        # --- Feel 通道优先：即使无 query 也直接拉 feel ---
        if domain.strip().lower() == "feel":
            result = await surface_feels(max_tokens=max_tokens)
        # --- importance_min 模式：跳过语义，按 importance 降序 ---
        elif importance_min >= 1:
            result = await surface_by_importance(
                importance_min=importance_min,
                max_results=max_results,
                max_tokens=max_tokens,
                tag_filter=tag_filter,
            )
        # --- 无 query：浮现模式 ---
        elif not query or not query.strip():
            result = await surface_default(
                max_results=max_results,
                max_tokens=max_tokens,
                tag_filter=tag_filter,
            )
        # --- 有 query：检索模式 ---
        else:
            result = await surface_search(
                query=query,
                max_results=max_results,
                max_tokens=max_tokens,
                domain=domain,
                valence=valence,
                arousal=arousal,
                tag_filter=tag_filter,
            )
    except Exception as e:
        rt.logger.error(f"breath dispatch error: {type(e).__name__}: {e}", exc_info=True)
        return {"text": f"检索记忆时发生错误: {type(e).__name__}: {e}"}

    # Normalise: branch functions return str; wrap into the dict schema that
    # ChatGPT's MCP client requires for tool output validation.
    if result is None:
        return {"text": "未检索到相关记忆。"}
    if isinstance(result, dict):
        # Already wrapped (future-proof if a branch is updated to return dict).
        return result
    return {"text": str(result)}
