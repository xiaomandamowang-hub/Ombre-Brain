#!/usr/bin/env python3
"""
Demote stale "permanent" buckets back to dynamic/ — one-time repair.
一次性修复：把「已取消钉选但仍卡在 permanent/」的桶降级回 dynamic/。

背景 / Why
----------
v2.3.x 之前 trace(bucket_id, pinned=0) 只翻 pinned 标记，却没有把桶移出
permanent/、也没把 type 改回 dynamic。后果：
  * calculate_score 仍走 type=="permanent" 分支 → 权重恒 999、永不衰减
  * count_pinned 仍把它算进固化配额 → pulse 固化数虚高、钉不了新桶

bucket_manager.update() 现已对称地在 unpin 时自动降级，但存量「幽灵固化桶」
（pinned != True 却还在 permanent/）需要这个脚本一次性清理。

判定 / Criteria
---------------
permanent/ 目录下、metadata.pinned 不为 True 且 protected 不为 True 的桶
即为幽灵桶，降级为 type=dynamic 并移动到 dynamic/<域>/。

Usage
-----
    OMBRE_BUCKETS_DIR=/data python tools/fix_unpinned_permanent.py [--dry-run]

默认 --dry-run=False 直接执行；先跑 --dry-run 看清单更稳。
"""

import asyncio
import argparse
import os
import sys

import frontmatter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from utils import load_config            # noqa: E402
from bucket_manager import BucketManager  # noqa: E402


async def fix(dry_run: bool) -> None:
    config = load_config()
    mgr = BucketManager(config)

    perm_dir = mgr.permanent_dir
    if not os.path.exists(perm_dir):
        print(f"permanent/ 不存在：{perm_dir}")
        return

    ghosts = []
    for root, _, files in os.walk(perm_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(root, fname)
            try:
                post = frontmatter.load(fpath)
            except Exception as e:
                print(f"⚠️ 跳过无法解析的桶 {fpath}: {e}")
                continue
            meta = post.metadata
            pinned = bool(meta.get("pinned"))
            protected = bool(meta.get("protected"))
            if not pinned and not protected:
                ghosts.append((fpath, post))

    print(f"permanent/ 扫描完成：发现 {len(ghosts)} 个已取消钉选但仍卡在固化区的幽灵桶。")
    for fpath, post in ghosts:
        bid = post.metadata.get("id") or os.path.splitext(os.path.basename(fpath))[0]
        name = post.metadata.get("name") or ""
        print(f"  - {bid} 《{name}》  ({fpath})")

    if dry_run:
        print("\n[dry-run] 未做任何改动。去掉 --dry-run 执行降级。")
        return
    if not ghosts:
        return

    moved = 0
    for fpath, post in ghosts:
        domain = post.metadata.get("domain") or ["未分类"]
        post.metadata["type"] = "dynamic"
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            mgr._move_bucket(fpath, mgr.dynamic_dir, domain)
            moved += 1
        except OSError as e:
            print(f"⚠️ 降级失败 {fpath}: {e}")

    print(f"\n✅ 已降级 {moved} 个幽灵桶到 dynamic/。现在 pulse 的固化数应与实际 pinned 数一致。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印将被降级的桶，不改动")
    args = ap.parse_args()
    asyncio.run(fix(args.dry_run))


if __name__ == "__main__":
    main()
