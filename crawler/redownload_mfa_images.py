# -*- coding: utf-8 -*-
"""
按 CSV 重新下载 MFA 本地图片（Playwright 过 WAF）。

命名规则（固定）：``{object_id}.jpg``（馆藏号，不用 title slug）
重爬结束后按磁盘文件 **整表回写** CSV 的 ``image_path`` / ``image_count``。

示例：
  # 全量重爬 + 相对路径（本机 output/images/mfa）
  python redownload_mfa_images.py --csv output/museum_of_fine_arts_boston.from_db.csv --force

  # 全量重爬 + 服务器绝对路径（与 MySQL/后端一致）
  python redownload_mfa_images.py --csv output/museum_of_fine_arts_boston.from_db.csv --force --server-path

  # 图已下好，仅根据磁盘同步 CSV 路径（不重开浏览器）
  python redownload_mfa_images.py --sync-paths-only --server-path

  # 同步路径后写入 MySQL
  python redownload_mfa_images.py --sync-paths-only --server-path --sync-mysql
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from museum_crawler.config import BASE_DIR, LOG_PATH, setup_logging
from museum_crawler.io_csv import write_csv
from museum_crawler.mfa_boston import (
    _MFA_SERVER_IMAGE_DIR,
    reconcile_mfa_csv_image_paths,
    repair_mfa_images,
    wipe_mfa_image_dir,
)

log = logging.getLogger("spider")


def _clear_csv_image_fields(csv_path: Path) -> int:
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row["image_path"] = ""
        row["image_paths"] = ""
        row["image_count"] = "0"
    write_csv(csv_path, rows)
    return len(rows)


def _wipe_extra_dirs(paths: list[Path]) -> int:
    total = 0
    for root in paths:
        if not root.is_dir():
            continue
        for p in root.iterdir():
            if p.is_file():
                try:
                    p.unlink()
                    total += 1
                except OSError as exc:
                    log.warning("[MFA] 额外目录删图失败 %s: %s", p, exc)
        log.info("[MFA] 已清空额外目录 %s", root)
    return total


def _sync_paths_only(
    csv_path: Path,
    img_root: Path,
    *,
    path_mode: str,
    server_dir: str,
) -> tuple[int, int]:
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    updated, on_disk = reconcile_mfa_csv_image_paths(
        rows, img_root, path_mode=path_mode, server_dir=server_dir
    )
    write_csv(csv_path, rows)
    return updated, on_disk


def _sync_mysql(csv_path: Path) -> int:
    from museum_crawler.csv_db_sync import import_csv_to_mysql
    from museum_crawler.db import mysql_configured

    if not mysql_configured():
        log.error("未配置 MYSQL_*，无法同步 MySQL")
        return 0
    return import_csv_to_mysql(csv_path)


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser(
        description="MFA 重下图 / 回写 CSV image_path（文件名=object_id）",
    )
    ap.add_argument(
        "--csv",
        type=Path,
        default=BASE_DIR / "output" / "museum_of_fine_arts_boston.from_db.csv",
        help="源 CSV（含 detail_url / image_url）",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=BASE_DIR / "output",
        help="图片保存根目录（实际为 output/images/mfa/）",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="全量重爬：先清空 mfa 图片目录与 CSV 中 image_path，再全部重下",
    )
    ap.add_argument(
        "--sync-paths-only",
        action="store_true",
        help="不下载，仅按磁盘 images/mfa/{object_id}.* 回写 CSV 路径",
    )
    ap.add_argument(
        "--server-path",
        action="store_true",
        help=f"CSV 写服务器绝对路径（默认 {_MFA_SERVER_IMAGE_DIR}）",
    )
    ap.add_argument(
        "--server-dir",
        type=str,
        default=_MFA_SERVER_IMAGE_DIR,
        help="与 --server-path 合用，自定义服务器图片目录",
    )
    ap.add_argument(
        "--extra-wipe-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="--force 时额外清空的目录",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="最多处理条数，0=全部",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="详情页间隔（秒）",
    )
    ap.add_argument(
        "--headless",
        action="store_true",
        help="无头浏览器（易被 WAF 拦截）",
    )
    ap.add_argument(
        "--sync-mysql",
        action="store_true",
        help="完成后将 CSV UPSERT 到 MySQL",
    )
    args = ap.parse_args()

    csv_path = args.csv.resolve()
    if not csv_path.is_file():
        log.error("CSV 不存在: %s", csv_path)
        return 1

    img_root = args.output / "images"
    img_root.mkdir(parents=True, exist_ok=True)
    path_mode = "server" if args.server_path else "relative"

    if args.sync_paths_only:
        updated, on_disk = _sync_paths_only(
            csv_path,
            img_root,
            path_mode=path_mode,
            server_dir=args.server_dir,
        )
        print(f"路径回写：更新 {updated} 行，磁盘有图 {on_disk} 行 → {csv_path}")
        if args.sync_mysql:
            n = _sync_mysql(csv_path)
            print(f"MySQL 导入：{n} 行")
        return 0

    if args.force:
        n_removed = wipe_mfa_image_dir(img_root)
        extra = [Path(p).resolve() for p in args.extra_wipe_dir]
        n_extra = _wipe_extra_dirs(extra)
        n_rows = _clear_csv_image_fields(csv_path)
        log.info(
            "[MFA] --force：已删本地图 %d + 额外 %d，清空 CSV 图片字段 %d 行",
            n_removed,
            n_extra,
            n_rows,
        )
        print(
            f"已清空旧图：{img_root / 'mfa'}（{n_removed} 个文件）"
            + (f"，额外目录 {n_extra} 个" if n_extra else "")
        )

    total, ok = repair_mfa_images(
        csv_path,
        img_root,
        max(args.delay, 0.3),
        browser_headless=args.headless,
        limit=args.limit,
        force_redownload=args.force,
        path_mode=path_mode,
        server_image_dir=args.server_dir,
    )
    path_label = args.server_dir if path_mode == "server" else str(img_root / "mfa")
    print(f"完成：新下载 {ok} 张；CSV 已按磁盘回写 image_path（{path_mode}）")
    print(f"  图片目录：{img_root / 'mfa'}")
    print(f"  CSV 路径样式：{path_label}\\{{object_id}}.jpg")
    print(f"  CSV 文件：{csv_path}")

    if args.sync_mysql:
        n = _sync_mysql(csv_path)
        print(f"MySQL 导入：{n} 行")

    log.info("[MFA] 日志: %s", LOG_PATH)
    return 0 if ok > 0 or args.force else 2


if __name__ == "__main__":
    sys.exit(main())
