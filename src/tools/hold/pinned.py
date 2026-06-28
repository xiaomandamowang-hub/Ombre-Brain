"""
========================================
tools/hold/pinned.py — hold(pinned=True) 分支
========================================

把这条桶钉为「永久核心准则」：跳过合并，强制 importance=10，写到
permanent 目录，不衰减、不会被合并掉。

关键行为：
- 先做 pinned 数量配额检查（默认 20 个上限），超了就拒绝并提示
- 仍然走 LLM analyze 拿 domain/valence/arousal/tags/suggested_name；
  她/他显式传入的 valence/arousal 优先
- type="permanent" + pinned=True 双重标记
- embedding 由 create() 内置 _sync_embedding 落盘时同步生成（与普通桶一致）；
  这里只探测是否成功，失败把降级提示拼进返回；返回 📌钉选→<id>

不做什么（边界）：
- 不做合并尝试：pinned 桶之间互不合并，分别保留
- 不允许 importance < 10：钉选意味着最高重要度

对外暴露：store_pinned(content, extra_tags, valence, arousal,
                       why_remembered) → str
========================================
"""

from .. import _runtime as rt
from .._common import check_pinned_quota


async def store_pinned(
    content: str,
    extra_tags: list,
    valence: float,
    arousal: float,
    why_remembered: str,
) -> str:
    try:
        analysis = await rt.dehydrator.analyze(content)
    except Exception as e:
        rt.logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "",
        }

    domain = analysis.get("domain") or ["未分类"]
    if not isinstance(domain, list):
        domain = ["未分类"]
    _v = analysis.get("valence", 0.5)
    _a = analysis.get("arousal", 0.3)
    final_valence = valence if 0 <= valence <= 1 else (float(_v) if _v is not None else 0.5)
    final_arousal = arousal if 0 <= arousal <= 1 else (float(_a) if _a is not None else 0.3)
    _raw_tags = analysis.get("tags") or []
    all_tags = list(dict.fromkeys((_raw_tags if isinstance(_raw_tags, list) else []) + extra_tags))
    suggested_name = analysis.get("suggested_name", "")

    err = await check_pinned_quota()
    if err:
        return err

    bucket_id = await rt.bucket_mgr.create(
        content=content,
        tags=all_tags,
        importance=10,
        domain=domain,
        valence=final_valence,
        arousal=final_arousal,
        name=suggested_name or None,
        bucket_type="permanent",
        pinned=True,
        why_remembered=why_remembered,
    )
    # iter 2.1+ 起 create() 内部已调用 _sync_embedding，permanent 桶与普通桶一样
    # 在落盘后立刻向量化，此处无需重复生成（否则每次钉选都多打一次 embedding API）。
    # 只探测上次是否成功，失败时把降级提示拼到返回串——核心准则若不可语义检索，
    # 她/他应当被告知（之前这里静默 except: pass，breath 盲查 permanent 无人知晓）。
    embed_warn = ""
    try:
        if rt.embedding_engine and getattr(rt.embedding_engine, "enabled", False):
            if await rt.embedding_engine.get_embedding(bucket_id) is None:
                embed_warn = (
                    "向量化失败，该核心准则暂不参与语义检索，仅支持关键词匹配。"
                    "请检查 OMBRE_EMBED_API_KEY。"
                )
    except Exception:
        embed_warn = (
            "向量化失败，该核心准则暂不参与语义检索，仅支持关键词匹配。"
            "请检查 OMBRE_EMBED_API_KEY。"
        )
    result = f"📌钉选→{bucket_id} {','.join(str(d) for d in domain if d is not None)}"
    if embed_warn:
        result += f"\n⚠️ {embed_warn}"
    return result
