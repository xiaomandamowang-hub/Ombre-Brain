"""一次性迁移：把旧 feel 桶 domain=[] 修成 domain=["feel"]。

背景：iter 1.6 之前 hold(feel=True) 创建桶时 domain 留空，
导致 dashboard 显示「未分类」，分组与新 feel 桶不一致。

用法：
    python migrate_feel_domain.py            # 默认读 config.yaml 里的 buckets_dir
    python migrate_feel_domain.py --dry      # 仅扫描不写

跑完后该脚本可以删除（rule.md §10 迁移脚本规范）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from utils import load_config  # noqa: E402
from bucket_manager import BucketManager  # noqa: E402


async def main(dry: bool) -> None:
    config = load_config()
    mgr = BucketManager(config)
    all_buckets = await mgr.list_all(include_archive=True)
    fixed = 0
    skipped = 0
    for b in all_buckets:
        meta = b.get("metadata", {})
        if meta.get("type") != "feel":
            continue
        domain = meta.get("domain", []) or []
        if domain == ["feel"]:
            skipped += 1
            continue
        bid = b["id"]
        print(f"  [fix] {bid}: domain={domain!r} -> ['feel']")
        if not dry:
            await mgr.update(bid, domain=["feel"])
        fixed += 1
    print(f"\n完成：修复 {fixed} 个 feel 桶，跳过 {skipped} 个已正确的。")
    if dry:
        print("（dry-run，未写盘）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="仅扫描不写")
    asyncio.run(main(ap.parse_args().dry))
