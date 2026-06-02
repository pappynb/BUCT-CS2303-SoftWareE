#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从三馆 CSV 导出规范化知识图谱（三元组 / 实体 / N-Triples）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from museum_crawler.config import BASE_DIR, setup_logging
from museum_crawler.kg_export import export_knowledge_graph

log = setup_logging()


def _resolve_csv_paths(out_dir: Path, explicit: list[Path] | None) -> list[Path]:
    if explicit:
        return [p if p.is_absolute() else (BASE_DIR / p) for p in explicit]
    clean_dir = out_dir / "clean"
    candidates = [
        clean_dir / "smithsonian_institution.cleaned.csv",
        clean_dir / "harvard_art_museums.fixed.cleaned.csv",
        clean_dir / "harvard_art_museums.cleaned.csv",
        out_dir / "smithsonian_institution.csv",
        out_dir / "harvard_art_museums.fixed.csv",
        out_dir / "harvard_art_museums.csv",
        out_dir / "museum_of_fine_arts_boston.csv",
    ]
    seen: set[str] = set()
    paths: list[Path] = []
    for p in candidates:
        key = p.name.replace(".fixed", "").replace(".cleaned", "")
        if key in seen:
            continue
        if p.exists() and p.stat().st_size > 0:
            seen.add(key)
            paths.append(p)
    return paths


def main() -> int:
    ap = argparse.ArgumentParser(description="导出规范化知识图谱三元组")
    ap.add_argument(
        "--csv",
        type=Path,
        nargs="*",
        help="指定 CSV（默认自动选 output 下三馆；哈佛优先 fixed 版）",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=BASE_DIR / "output",
        help="输出目录（默认 crawler/output）",
    )
    ap.add_argument(
        "--nt",
        action="store_true",
        help="（已弃用）N-Triples 导出不再支持，保留参数仅为兼容",
    )
    args = ap.parse_args()

    out_dir = args.out_dir if args.out_dir.is_absolute() else BASE_DIR / args.out_dir
    csv_paths = _resolve_csv_paths(out_dir, list(args.csv) if args.csv else None)
    if not csv_paths:
        log.error("未找到可导出的 CSV，请先爬取或指定 --csv")
        return 1

    log.info("[KG] 输入 CSV: %s", ", ".join(p.name for p in csv_paths))
    stats = export_knowledge_graph(
        csv_paths,
        out_dir,
        export_nt=args.nt,
    )
    log.info(
        "[KG] 完成 → %s/kg/ (artifacts=%d, relations=%d, properties=%d)",
        out_dir,
        stats.get("artifact_count", 0),
        stats.get("relation_count", 0),
        stats.get("property_count", 0),
    )
    print(
        f"KG → {out_dir / 'kg'}  对齐表 → {out_dir / 'kg' / 'align'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
