#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨馆实体对齐：生成 entity_master / entity_alias / entity_source。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from museum_crawler.config import BASE_DIR, setup_logging
from museum_crawler.entity_align import DEFAULT_URI_BASE, build_registry_from_csvs

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
    ap = argparse.ArgumentParser(description="跨馆实体对齐")
    ap.add_argument("--csv", type=Path, nargs="*")
    ap.add_argument("--out-dir", type=Path, default=BASE_DIR / "output" / "kg" / "align")
    ap.add_argument("--uri-base", default=DEFAULT_URI_BASE)
    args = ap.parse_args()

    out_dir = BASE_DIR / "output"
    inputs = _resolve_csv_paths(out_dir, list(args.csv) if args.csv else None)
    if not inputs:
        log.error("未找到 CSV")
        return 1

    log.info("[ALIGN] 输入: %s", ", ".join(p.name for p in inputs))
    reg = build_registry_from_csvs(inputs, uri_base=args.uri_base)
    align_dir = args.out_dir if args.out_dir.is_absolute() else BASE_DIR / args.out_dir
    stats = reg.write_tables(align_dir)

    shared = [m for m in reg.masters.values() if m["entity_type"] != "Artifact"]
    artists = [m for m in shared if m["entity_type"] == "Artist"]
    summary = {
        "inputs": [p.name for p in inputs],
        "shared_entities": len(shared),
        "artists": len(artists),
        "artists_with_wikidata": sum(1 for m in artists if m.get("external_id")),
        "aliases": stats["entity_alias"],
        "sources": stats["entity_source"],
        **stats,
    }
    summary_path = align_dir / "align_summary.json"
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    log.info(
        "[ALIGN] 完成 → %s  master=%d alias=%d source=%d 作者(Wikidata)=%d/%d",
        align_dir,
        stats["entity_master"],
        stats["entity_alias"],
        stats["entity_source"],
        summary["artists_with_wikidata"],
        summary["artists"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
