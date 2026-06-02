#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 output/kg/ 同步到 Neo4j。

用法：
  cd crawler
  pip install neo4j
  # 在 .env 中配置 NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD

  python sync_neo4j.py                    # 增量 MERGE（推荐）
  python sync_neo4j.py --wipe             # 清空后全量导入
  python sync_neo4j.py --skip-properties  # 仅节点+关系，更快
  python sync_neo4j.py --properties-only  # 仅补写 properties/ 到已有 Artifact 节点
  python sync_neo4j.py --test             # 只测连接

典型流水线（增量爬取后）：
  python export_kg.py
  python sync_neo4j.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from museum_crawler.config import BASE_DIR, setup_logging
from museum_crawler.neo4j_sync import neo4j_configured, sync_kg_to_neo4j, connect_neo4j

log = setup_logging()


def main() -> int:
    ap = argparse.ArgumentParser(description="同步 output/kg 到 Neo4j")
    ap.add_argument(
        "--kg-dir",
        type=Path,
        default=BASE_DIR / "output" / "kg",
        help="KG CSV 目录（默认 output/kg）",
    )
    ap.add_argument("--wipe", action="store_true", help="导入前清空全图（慎用）")
    ap.add_argument("--skip-properties", action="store_true", help="跳过 properties 属性写入")
    ap.add_argument(
        "--properties-only",
        action="store_true",
        help="仅导入 output/kg/properties/（要求 Artifact 节点已存在）",
    )
    ap.add_argument("--batch-size", type=int, default=500, help="导入批大小")
    ap.add_argument(
        "--wipe-batch-size",
        type=int,
        default=2000,
        help="--wipe 时每批删除节点数（内存紧张可调小，如 500）",
    )
    ap.add_argument("--test", action="store_true", help="仅测试 Neo4j 连接")
    args = ap.parse_args()

    if not neo4j_configured():
        log.error("请在 crawler/.env 配置 NEO4J_URI 与 NEO4J_PASSWORD")
        return 1

    if args.test:
        driver = connect_neo4j()
        try:
            driver.verify_connectivity()
            with driver.session() as s:
                ver = s.run("RETURN 1 AS ok").single()
            log.info("Neo4j 连接 OK: %s", ver)
            return 0
        except Exception as exc:
            log.error("Neo4j 连接失败: %s", exc)
            return 1
        finally:
            driver.close()

    kg_dir = args.kg_dir if args.kg_dir.is_absolute() else BASE_DIR / args.kg_dir
    if args.properties_only:
        if args.wipe:
            log.error("--properties-only 与 --wipe 不能同时使用")
            return 1
        if args.skip_properties:
            log.error("--properties-only 与 --skip-properties 冲突")
            return 1
    elif not (kg_dir / "artifacts.csv").is_file():
        log.error("未找到 %s，请先运行 python export_kg.py", kg_dir / "artifacts.csv")
        return 1

    prop_dir = kg_dir / "properties"
    if args.properties_only and not prop_dir.is_dir():
        log.error("未找到 %s", prop_dir)
        return 1

    try:
        stats = sync_kg_to_neo4j(
            kg_dir,
            wipe=args.wipe,
            skip_properties=args.skip_properties,
            properties_only=args.properties_only,
            batch_size=args.batch_size,
            wipe_batch_size=args.wipe_batch_size,
        )
    except Exception as exc:
        log.error("同步失败: %s", exc)
        return 1

    log.info(
        "[Neo4j] 同步完成 → artifacts=%d relations=%d properties=%d (库内: %s)",
        stats.get("artifacts", 0),
        stats.get("relations", 0),
        stats.get("properties", 0),
        {k: stats[k] for k in ("artifacts", "relationships") if k in stats},
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
