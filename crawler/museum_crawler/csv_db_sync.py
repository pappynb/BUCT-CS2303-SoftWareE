# -*- coding: utf-8 -*-
"""
CSV 与 MySQL ``artifact`` 表双向同步（列与 ``CSV_FIELDS`` 对齐）。

- ``import``：读 UTF-8-BOM CSV，按主键 ``(museum_id, object_id)`` 批量 UPSERT。
- ``export``：按 ``CSV_FIELDS`` 顺序导出为 UTF-8-BOM CSV。

依赖 ``.env`` 中 ``MYSQL_*``，与爬虫写库配置相同。
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    _crawler_dir = Path(__file__).resolve().parent.parent
    if str(_crawler_dir) not in sys.path:
        sys.path.insert(0, str(_crawler_dir))

import argparse
import csv
import logging
from datetime import date, datetime
from typing import Any, Optional

from museum_crawler.config import CSV_FIELDS, LOG_PATH, setup_logging
from museum_crawler.db import MySQLWriter, mysql_configured
from museum_crawler.io_csv import write_csv

log = logging.getLogger("spider")


def _cell_to_csv_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def load_csv_rows(path: Path) -> list[dict[str, Any]]:
    """读取 CSV，行字典键与 ``CSV_FIELDS`` 对齐（缺列补空串）。"""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows: list[dict[str, Any]] = []
        for raw in reader:
            row: dict[str, Any] = {}
            for k in CSV_FIELDS:
                v = raw.get(k, "")
                row[k] = "" if v is None else str(v).strip()
            rows.append(row)
    return rows


def import_csv_to_mysql(
    csv_path: Path,
    *,
    chunk_size: int = 80,
) -> int:
    """
    将 CSV 全量 UPSERT 到 MySQL。返回成功写入的行数（按批次累计，失败则中断）。
    """
    rows = load_csv_rows(csv_path)
    if not rows:
        log.warning("[sync] CSV 无数据行: %s", csv_path)
        return 0
    writer = MySQLWriter.from_env()
    writer.ensure_legacy_author_province_renamed()
    writer.ensure_missing_csv_columns()
    writer.ensure_loosen_overflow_prone_columns()
    ok = 0
    for i in range(0, len(rows), chunk_size):
        batch = rows[i : i + chunk_size]
        try:
            writer.upsert_batch(batch)
            ok += len(batch)
        except Exception as exc:
            log.error("[sync] UPSERT 失败（offset=%d）: %s", i, exc)
            break
    log.info("[sync] 导入完成 %d / %d 行 → %s", ok, len(rows), csv_path.name)
    return ok


def export_mysql_to_csv(
    out_path: Path,
    *,
    museum_id: Optional[int] = None,
) -> int:
    """
    从 MySQL 导出为 CSV。``museum_id`` 非空时只导该馆（如哈佛 ``2``）。
    """
    writer = MySQLWriter.from_env()
    conn = writer._connect()
    table = writer._table
    try:
        cur = conn.cursor()
        col_sql = ", ".join(f"`{c}`" for c in CSV_FIELDS)
        sql = f"SELECT {col_sql} FROM `{table}`"
        params: list[Any] = []
        if museum_id is not None:
            sql += " WHERE `museum_id` = %s"
            params.append(museum_id)
        cur.execute(sql, params)
        rows: list[dict[str, Any]] = []
        for tup in cur.fetchall():
            rows.append(
                {CSV_FIELDS[i]: _cell_to_csv_str(tup[i]) for i in range(len(CSV_FIELDS))}
            )
        cur.close()
    finally:
        conn.close()
    write_csv(out_path, rows)
    log.info("[sync] 导出 %d 行 → %s", len(rows), out_path)
    return len(rows)


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(
        description="CSV 与 MySQL 双向同步（列与 config.CSV_FIELDS 一致）",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_in = sub.add_parser("import", help="CSV 导入/覆盖 UPSERT 到数据库")
    p_in.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="源 CSV 路径（UTF-8-BOM，表头列名与程序一致）",
    )
    p_in.add_argument(
        "--chunk",
        type=int,
        default=80,
        help="每批 UPSERT 行数",
    )

    p_out = sub.add_parser("export", help="从数据库导出为 CSV")
    p_out.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="输出 CSV 路径",
    )
    p_out.add_argument(
        "--museum-id",
        type=int,
        default=None,
        help="仅导出该 museum_id（如哈佛 2）；省略则全表",
    )

    args = ap.parse_args()

    if not mysql_configured():
        log.error("[sync] 未配置 MYSQL_HOST + MYSQL_DATABASE，请在 .env 中填写")
        raise SystemExit(2)

    if args.cmd == "import":
        import_csv_to_mysql(args.csv, chunk_size=args.chunk)
    elif args.cmd == "export":
        export_mysql_to_csv(args.csv, museum_id=args.museum_id)
    else:
        ap.error("unknown command")

    log.info("[sync] 日志: %s", LOG_PATH)


if __name__ == "__main__":
    main()
