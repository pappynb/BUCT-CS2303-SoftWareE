# -*- coding: utf-8 -*-
"""
爬取结果简单质检：检查关键列是否为空（课程 PDF 附录思路）。
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

log = logging.getLogger("spider")


def quality_check(csv_path: Path) -> None:
    if not csv_path.exists():
        return
    with open(csv_path, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        log.warning("[QC] %s 无数据行", csv_path.name)
        return
    # 与入库/展示强相关的最小字段集（非 PDF 全量校验）
    required = ["object_id", "museum_id", "title", "detail_url", "image_url", "crawl_date"]
    for field_name in required:
        empty = sum(1 for r in rows if not r.get(field_name, "").strip())
        if empty:
            log.warning("[QC] %s: 必填字段 %s 有 %d 空值",
                        csv_path.name, field_name, empty)
        else:
            log.info("[QC] %s: %s ✓ 全部填写", csv_path.name, field_name)
    log.info("[QC] %s: 共 %d 条记录", csv_path.name, len(rows))
