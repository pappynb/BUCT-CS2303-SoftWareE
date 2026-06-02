#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一 output 下 CSV 的日期列为 ISO YYYY-MM-DD。"""

from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

from museum_crawler.config import BASE_DIR, CSV_FIELDS
from museum_crawler.date_format import normalize_row_dates
from museum_crawler.io_csv import write_csv


def _normalize_csv(path: Path, *, backup: bool) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0, 0
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return 0, 0
    changed = 0
    new_rows: list[dict[str, str]] = []
    for raw in rows:
        row = {k: str(raw.get(k, "") or "").strip() for k in CSV_FIELDS}
        normed = normalize_row_dates(row)
        if normed != row:
            changed += 1
        new_rows.append(normed)
    if backup:
        bak = path.with_suffix(path.suffix + ".bak")
        if not bak.exists():
            shutil.copy2(path, bak)
    write_csv(path, new_rows)
    return len(new_rows), changed


def main() -> None:
    ap = argparse.ArgumentParser(description="统一 CSV 日期格式为 YYYY-MM-DD")
    ap.add_argument(
        "--csv",
        type=Path,
        nargs="*",
        help="要处理的 CSV（默认 harvard_art_museums.csv）",
    )
    ap.add_argument("--no-backup", action="store_true", help="不生成 .bak 备份")
    args = ap.parse_args()
    paths = list(args.csv) if args.csv else [BASE_DIR / "output" / "harvard_art_museums.csv"]
    for p in paths:
        total, changed = _normalize_csv(p, backup=not args.no_backup)
        print(f"{p.name}: 已写回 {total} 行，日期格式调整 {changed} 行")


if __name__ == "__main__":
    main()
