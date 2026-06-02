# -*- coding: utf-8 -*-
"""
CSV 落盘与可选 MySQL 同步。

``append_csv`` 在断点续爬时每批写入；若传入 ``db_writer``，同一批会 UPSERT 到数据库。
"""

from __future__ import annotations

import csv
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from museum_crawler.config import CSV_FIELDS
from museum_crawler.record_build import finalize_record

if TYPE_CHECKING:
    from museum_crawler.db import MySQLWriter

log = logging.getLogger("spider")

_warned_header_mismatch: set[str] = set()


def _open_with_retry(path: Path, mode: str, *, retries: int = 8, wait_s: float = 1.0):
    """
    处理 Windows 下常见文件占用（Excel/预览器打开 CSV）导致的 PermissionError。
    """
    last_exc: Exception | None = None
    for i in range(retries):
        try:
            return open(path, mode, encoding="utf-8-sig", newline="")
        except PermissionError as exc:
            last_exc = exc
            if i == retries - 1:
                break
            log.warning(
                "CSV 文件被占用，%ss 后重试（%d/%d）: %s",
                wait_s, i + 1, retries, path.name
            )
            time.sleep(wait_s)
    raise last_exc if last_exc else PermissionError(f"无法打开文件: {path}")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """覆盖写入整张表（全量导出场景）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _open_with_retry(path, "w") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            # 全部转 str，避免 DictWriter 对 int/date 类型报错
            w.writerow({k: str(row.get(k, "") or "") for k in CSV_FIELDS})


def append_csv(
    path: Path,
    rows: list[dict[str, Any]],
    db_writer: Optional["MySQLWriter"] = None,
) -> None:
    """
    追加行；新文件自动写表头。
    若 ``db_writer`` 非空，在 CSV 写入成功后对同一批执行数据库 UPSERT。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    key = str(path.resolve())
    if not is_new and key not in _warned_header_mismatch:
        try:
            with open(path, encoding="utf-8-sig", newline="") as rf:
                first = rf.readline()
            old = next(csv.reader([first]))
            if tuple(old) != tuple(CSV_FIELDS):
                log.warning(
                    "CSV 表头与当前 CSV_FIELDS 不一致（列数/列名已变）。"
                    "请备份后删除该文件重爬，或对之运行 python enrich_wikidata.py --csv … 以整表重写：%s",
                    path.name,
                )
                _warned_header_mismatch.add(key)
        except Exception:
            pass
    with _open_with_retry(path, "a") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if is_new:
            w.writeheader()  # 新文件写 BOM+表头，Excel 友好
        for row in rows:
            # extrasaction=ignore：行里多出的键不写进 CSV，防止列错位
            w.writerow({k: str(row.get(k, "") or "") for k in CSV_FIELDS})

    if db_writer and rows:
        try:
            db_writer.upsert_batch(rows)  # 与 CSV 同一批，保证文件与库尽量一致
        except Exception as exc:
            log.error("MySQL 写入失败（CSV 已保存）: %s", exc)


def migrate_csv_to_current_fields(path: Path, *, backup: bool = True) -> int:
    """
    将旧表头 CSV 重写为当前 ``CSV_FIELDS``（补空列并 ``finalize_record``）。
    返回迁移行数。
    """
    if not path.exists() or path.stat().st_size == 0:
        return 0
    with open(path, encoding="utf-8-sig", newline="") as fh:
        old_rows = list(csv.DictReader(fh))
    if not old_rows:
        return 0
    new_rows: list[dict[str, str]] = []
    for row in old_rows:
        try:
            mid = int(str(row.get("museum_id") or "2").strip())
        except ValueError:
            mid = 2
        merged = finalize_record(row, mid)
        for k in (
            "image_url",
            "image_urls",
            "image_path",
            "image_paths",
            "image_count",
            "crawl_date",
            "iiif_manifest_url",
        ):
            if row.get(k):
                merged[k] = str(row[k]).strip()
        new_rows.append(merged)
    if backup:
        bak = path.with_suffix(path.suffix + ".bak")
        if not bak.exists():
            import shutil
            shutil.copy2(path, bak)
            log.info("已备份旧 CSV → %s", bak.name)
    write_csv(path, new_rows)
    log.info("CSV 已迁移为新表头（%d 列）: %s", len(CSV_FIELDS), path.name)
    return len(new_rows)
