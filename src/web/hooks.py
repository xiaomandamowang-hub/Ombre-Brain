"""
========================================
web/hooks.py — breath / dream 浮现挂载点（webhook，公开 GET）
========================================

- /breath-hook：对话开头由外部 hook 拉取，返回应浮现的记忆（pinned + 未解决采样）
- /dream-hook：dream 专用，返回最近窗口内可做梦的候选

给外部 SessionStart hook / 自动化用，无需登录鉴权；通过 sh.fire_webhook 推送事件。

对外暴露：register(mcp)。
========================================
"""

import random

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

logger = sh.logger

try:
    from utils import strip_wikilinks, count_tokens_approx  # type: ignore
except ImportError:  # pragma: no cover
    from ..utils import strip_wikilinks, count_tokens_approx  # type: ignore


def register(mcp) -> None:

    @mcp.custom_route("/breath-hook", methods=["GET"])
    async def breath_hook(request):
        from starlette.responses import PlainTextResponse
        try:
            all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
            # pinned
            pinned = [b for b in all_buckets if b["metadata"].get("pinned") or b["metadata"].get("protected")]
            # top 2 unresolved by score
            unresolved = [b for b in all_buckets
                          if not b["metadata"].get("resolved", False)
                          and b["metadata"].get("type") not in ("permanent", "feel", "plan", "letter", "self", "i")
                          and not b["metadata"].get("pinned")
                          and not b["metadata"].get("protected")
                          and not b["metadata"].get("dont_surface", False)]
            scored = sorted(unresolved, key=lambda b: sh.decay_engine.calculate_score(b["metadata"]), reverse=True)

            parts = []
            token_budget = 10000
            for b in pinned:
                summary = await sh.dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
                parts.append(f"📌 [核心准则] {summary}")
                token_budget -= count_tokens_approx(summary)

            # Diversity: top-1 fixed + shuffle rest from top-20
            candidates = list(scored)
            if len(candidates) > 1:
                top1 = [candidates[0]]
                pool = candidates[1:min(20, len(candidates))]
                random.shuffle(pool)
                candidates = top1 + pool + candidates[min(20, len(candidates)):]
            # Hard cap: max 20 surfacing buckets in hook
            candidates = candidates[:20]

            for b in candidates:
                if token_budget <= 0:
                    break
                summary = await sh.dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
                summary_tokens = count_tokens_approx(summary)
                if summary_tokens > token_budget:
                    break
                parts.append(summary)
                token_budget -= summary_tokens

            if not parts:
                await sh.fire_webhook("breath_hook", {"surfaced": 0})
                return PlainTextResponse("")
            body_text = "[Ombre Brain - 记忆浮现]\n" + "\n---\n".join(parts)

            # --- Append latest letter from each side (iter 1.4) ---
            # --- 附带双方各最新一封 letter ---
            try:
                letters = [b for b in all_buckets if b["metadata"].get("type") == "letter"]
                if letters:
                    def _latest(author: str) -> dict | None:
                        pool = [letter for letter in letters if letter["metadata"].get("author") == author]
                        if not pool:
                            return None
                        pool.sort(key=lambda b: b["metadata"].get("letter_date") or b["metadata"].get("created", ""), reverse=True)
                        return pool[0]
                    latest_user = _latest("user")
                    latest_claude = _latest("claude")
                    letter_lines = []
                    for tag, letter in (("user→你", latest_user), ("你→user", latest_claude)):
                        if letter is None:
                            continue
                        d = letter["metadata"].get("letter_date") or letter["metadata"].get("created", "")[:10]
                        title = letter["metadata"].get("title") or letter["metadata"].get("name", "")
                        excerpt = strip_wikilinks(letter["content"])[:400]
                        letter_lines.append(
                            f"💌 [{tag}] {d}{(' · ' + title) if title else ''}\n{excerpt}"
                        )
                    if letter_lines:
                        body_text += "\n\n=== 最近的信 ===\n" + "\n\n".join(letter_lines)
            except Exception as e:
                logger.warning(f"breath_hook letter section failed: {e}")

            # --- Append recent self-knowledge (I tool) ---
            try:
                self_buckets = [
                    b for b in all_buckets
                    if b["metadata"].get("type") == "i"
                    or "__i__" in (b["metadata"].get("tags") or [])
                ]
                if self_buckets:
                    self_buckets.sort(
                        key=lambda b: b["metadata"].get("created", ""), reverse=True
                    )
                    self_lines = []
                    for b in self_buckets[:3]:
                        meta = b["metadata"]
                        ts = (meta.get("created") or "")[:10]
                        tags_list = meta.get("tags") or []
                        aspect_tag = next(
                            (t.replace("aspect:", "") for t in tags_list if t.startswith("aspect:")), ""
                        )
                        aspect_label = f" [{aspect_tag}]" if aspect_tag else ""
                        excerpt = strip_wikilinks(b["content"])[:300]
                        self_lines.append(f"🪞{ts}{aspect_label}\n{excerpt}")
                    if self_lines:
                        body_text += "\n\n=== I ===\n" + "\n\n".join(self_lines)
            except Exception as e:
                logger.warning(f"breath_hook I section failed: {e}")

            await sh.fire_webhook("breath_hook", {"surfaced": len(parts), "chars": len(body_text)})
            return PlainTextResponse(body_text)
        except Exception as e:
            logger.warning(f"Breath hook failed: {e}")
            return PlainTextResponse("")


    # =============================================================
    # /dream-hook endpoint: Dedicated hook for Dreaming
    # Dreaming 专用挂载点
    # =============================================================
    @mcp.custom_route("/dream-hook", methods=["GET"])
    async def dream_hook(request):
        from starlette.responses import PlainTextResponse
        try:
            all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
            candidates = [
                b for b in all_buckets
                if b["metadata"].get("type") not in ("permanent", "feel", "plan", "letter", "self", "i")
                and not b["metadata"].get("pinned", False)
                and not b["metadata"].get("protected", False)
                and not b["metadata"].get("dont_surface", False)
            ]
            candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            recent = candidates[:10]

            if not recent:
                return PlainTextResponse("")

            parts = []
            for b in recent:
                meta = b["metadata"]
                resolved_tag = "[已解决]" if meta.get("resolved", False) else "[未解决]"
                parts.append(
                    f"{meta.get('name', b['id'])} {resolved_tag} "
                    f"V{float(meta.get('valence') or 0.5):.1f}/A{float(meta.get('arousal') or 0.3):.1f}\n"
                    f"{strip_wikilinks(b['content'][:200])}"
                )

            body_text = "[Ombre Brain - Dreaming]\n" + "\n---\n".join(parts)
            await sh.fire_webhook("dream_hook", {"surfaced": len(parts), "chars": len(body_text)})
            return PlainTextResponse(body_text)
        except Exception as e:
            logger.warning(f"Dream hook failed: {e}")
            return PlainTextResponse("")
