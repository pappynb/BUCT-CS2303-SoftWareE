# -*- coding: utf-8 -*-
"""将旧 CSV 表头迁移为当前 CSV_FIELDS（30 列）。"""
import argparse
from pathlib import Path

from museum_crawler.config import BASE_DIR, setup_logging
from museum_crawler.io_csv import migrate_csv_to_current_fields

if __name__ == "__main__":
    setup_logging()
    p = argparse.ArgumentParser(description="迁移 CSV 至统一字段表头")
    p.add_argument("--csv", type=Path, required=True, help="CSV 路径")
    p.add_argument("--no-backup", action="store_true")
    args = p.parse_args()
    csv_path = args.csv if args.csv.is_absolute() else BASE_DIR / args.csv
    n = migrate_csv_to_current_fields(csv_path, backup=not args.no_backup)
    print(f"已迁移 {n} 行 → {csv_path}")
