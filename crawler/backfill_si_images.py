#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
史密森尼多图增量补爬：只更新 image_* 字段，不重爬元数据。

默认更新：
  output/clean/smithsonian_institution.cleaned.csv
  output/smithsonian_institution.csv
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from museum_crawler.config import BASE_DIR, setup_logging
from museum_crawler.db import MySQLWriter, mysql_configured
from museum_crawler.smithsonian import backfill_smithsonian_images

log = setup_logging()


def main() -> int:
    ap = argparse.ArgumentParser(description="史密森尼 CSV 多图字段增量补全")
    ap.add_argument(
        "--csv",
        type=Path,
        action="append",
        help="要更新的 CSV（可多次指定；默认 cleaned + 原始各一份）",
    )
    ap.add_argument(
        "--img-root",
        type=Path,
        default=BASE_DIR / "output" / "images",
    )
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 条（0=全部）")
    ap.add_argument("--api-delay", type=float, default=0.8)
    ap.add_argument("--img-delay", type=float, default=2.0)
    ap.add_argument(
        "--skip-download",
        action="store_true",
        help="只写 URL 字段，不下载新增本地图",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="强制重跑所有行（默认跳过 image_urls 已填的行）",
    )
    ap.add_argument(
        "--mysql",
        action="store_true",
        help="完成后 UPSERT 到 MySQL（需 .env 配置）",
    )
    args = ap.parse_args()

    api_key = os.environ.get("SI_DATA_GOV_API_KEY", "").strip()
    if not api_key:
        log.error("未设置 SI_DATA_GOV_API_KEY")
        return 1

    csv_paths = args.csv or [
        BASE_DIR / "output" / "clean" / "smithsonian_institution.cleaned.csv",
        BASE_DIR / "output" / "smithsonian_institution.csv",
    ]
    img_root = args.img_root if args.img_root.is_absolute() else BASE_DIR / args.img_root
    db_writer = None
    if args.mysql:
        if not mysql_configured():
            log.error("未配置 MySQL，去掉 --mysql 或填写 .env")
            return 1
        db_writer = MySQLWriter.from_env()
        db_writer.ensure_legacy_author_province_renamed()
        db_writer.ensure_missing_csv_columns()
        db_writer.ensure_loosen_overflow_prone_columns()

    limit = args.limit or None
    exit_code = 0
    for p in csv_paths:
        path = p if p.is_absolute() else BASE_DIR / p
        if not path.exists():
            log.warning("跳过不存在的文件: %s", path)
            continue
        log.info("补多图 → %s", path)
        stats = backfill_smithsonian_images(
            path,
            img_root,
            api_key,
            api_delay=args.api_delay,
            img_delay=args.img_delay,
            limit=limit,
            skip_download=args.skip_download,
            skip_filled=not args.force,
            db_writer=db_writer if args.mysql else None,
        )
        log.info("统计 %s: %s", path.name, stats)
        if stats.get("updated", 0) == 0 and stats.get("rows", 0) > 0:
            exit_code = 2

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
