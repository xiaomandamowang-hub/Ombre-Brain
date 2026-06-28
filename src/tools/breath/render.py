"""Fast, dependency-free rendering for breath results.

Reading memory must stay available even when the optional dehydration LLM is
slow, rate-limited, or unavailable.  Keep LLM-based compression in write-time
flows; breath renders a bounded local excerpt instead.
"""

from utils import count_tokens_approx, strip_wikilinks


def render_local(content: str, metadata: dict | None, max_tokens: int) -> str:
    """Render metadata plus as much raw content as fits in ``max_tokens``."""
    metadata = metadata or {}
    name = str(metadata.get("name") or "").strip()
    domains = ",".join(metadata.get("domain") or [])
    header_parts = []
    if name:
        header_parts.append(f"[{name}]")
    if domains:
        header_parts.append(f"[主题:{domains}]")
    header = " ".join(header_parts)

    raw = strip_wikilinks(content or "").strip() or "（空记忆）"
    prefix = f"{header}\n" if header else ""
    budget = max(1, int(max_tokens or 1))

    if count_tokens_approx(prefix + raw) <= budget:
        return prefix + raw

    # Binary search avoids assumptions about Chinese/English token ratios.
    low, high = 0, len(raw)
    while low < high:
        mid = (low + high + 1) // 2
        if count_tokens_approx(prefix + raw[:mid] + "…") <= budget:
            low = mid
        else:
            high = mid - 1
    excerpt = raw[:low].rstrip()
    return prefix + (excerpt + "…" if low < len(raw) else excerpt)
