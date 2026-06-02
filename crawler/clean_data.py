#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""爬取 CSV 数据清洗：标准化、去重、图片有效性检测。"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from museum_crawler.config import BASE_DIR, setup_logging
from museum_crawler.data_clean import clean_csv_file

log = setup_logging()


def _default_inputs(out_dir: Path) -> list[Path]:
    names = [
        "harvard_art_museums.fixed.csv",
        "harvard_art_museums.csv",
        "smithsonian_institution.csv",
        "museum_of_fine_arts_boston.csv",
    ]
    seen: set[str] = set()
    paths: list[Path] = []
    for name in names:
        key = name.replace(".fixed", "")
        if key in seen:
            continue
        p = out_dir / name
        if p.exists() and p.stat().st_size > 0:
            seen.add(key)
            paths.append(p)
    return paths


def main() -> int:
    ap = argparse.ArgumentParser(description="清洗爬取 CSV：标准化 / 去重 / 图片检测")
    ap.add_argument("--csv", type=Path, nargs="*", help="输入 CSV（默认 output 下三馆）")
    ap.add_argument("--out-dir", type=Path, default=BASE_DIR / "output" / "clean")
    ap.add_argument("--check-images", action="store_true", help="HEAD 检测图片 URL（较慢）")
    ap.add_argument(
        "--drop-no-image",
        action="store_true",
        help="配合 --check-images：删除 URL 与本地图片均无效的记录",
    )
    ap.add_argument("--image-workers", type=int, default=8)
    ap.add_argument("--image-limit", type=int, default=0, help="仅检测前 N 个唯一 URL（0=全部）")
    ap.add_argument("--image-timeout", type=float, default=12.0)
    args = ap.parse_args()

    src_dir = BASE_DIR / "output"
    inputs = list(args.csv) if args.csv else _default_inputs(src_dir)
    if not inputs:
        log.error("未找到输入 CSV")
        return 1

    clean_dir = args.out_dir if args.out_dir.is_absolute() else BASE_DIR / args.out_dir
    clean_dir.mkdir(parents=True, exist_ok=True)

    summaries = []
    for src in inputs:
        src = src if src.is_absolute() else BASE_DIR / src
        dst = clean_dir / src.name.replace(".csv", ".cleaned.csv")
        log.info("[CLEAN] %s → %s", src.name, dst.name)
        rep = clean_csv_file(
            src,
            dst,
            check_images=args.check_images,
            drop_no_image=args.drop_no_image,
            image_workers=args.image_workers,
            image_limit=args.image_limit,
            image_timeout=args.image_timeout,
        )
        summaries.append(rep.to_dict())

        if rep.duplicate_records:
            dup_path = clean_dir / f"{src.stem}.duplicates.csv"
            with open(dup_path, "w", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(
                    fh,
                    fieldnames=[
                        "museum_id", "object_id",
                        "duplicate_of_museum_id", "duplicate_of_object_id",
                        "reason",
                    ],
                )
                w.writeheader()
                for d in rep.duplicate_records:
                    w.writerow({
                        "museum_id": d.museum_id,
                        "object_id": d.object_id,
                        "duplicate_of_museum_id": d.duplicate_of_museum_id,
                        "duplicate_of_object_id": d.duplicate_of_object_id,
                        "reason": d.reason,
                    })

        if rep.image_reports:
            img_path = clean_dir / f"{src.stem}.image_check.csv"
            with open(img_path, "w", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(
                    fh,
                    fieldnames=[
                        "museum_id", "object_id", "image_url",
                        "http_ok", "status_code", "content_type", "local_file_ok", "valid",
                    ],
                )
                w.writeheader()
                w.writerows(rep.image_reports)

        log.info(
            "[CLEAN] %s: %d→%d 行, 标准化 %d, 去重 %d, 图片无效 %d, 无图删除 %d",
            src.name,
            rep.input_rows,
            rep.output_rows,
            rep.standardized_fields,
            rep.duplicates_removed,
            rep.images_invalid,
            rep.images_dropped,
        )

    summary_path = clean_dir / "clean_summary.json"
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summaries, fh, ensure_ascii=False, indent=2)
    log.info("[CLEAN] 汇总 → %s", summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
