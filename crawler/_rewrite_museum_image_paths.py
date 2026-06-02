# -*- coding: utf-8 -*-
"""将 CSV 中 images/{museum}/ 相对路径改为服务器绝对路径。"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from museum_crawler.config import BASE_DIR, CSV_FIELDS
from museum_crawler.io_csv import write_csv

SERVER_BASE = r"C:\Users\Administrator\Desktop"

MUSEUMS: tuple[tuple[str, str], ...] = (
    ("harvard", "harvard"),
    ("smithsonian", "smithsonian"),
    ("mfa", "mfa"),
)

CSV_FILES: tuple[Path, ...] = (
    BASE_DIR / "output" / "harvard_art_museums.fixed.csv",
    BASE_DIR / "output" / "clean" / "harvard_art_museums.fixed.cleaned.csv",
    BASE_DIR / "output" / "smithsonian_institution.csv",
    BASE_DIR / "output" / "clean" / "smithsonian_institution.cleaned.csv",
    BASE_DIR / "output" / "museum_of_fine_arts_boston.csv",
    BASE_DIR / "output" / "museum_of_fine_arts_boston.from_db.csv",
    BASE_DIR / "output" / "clean" / "museum_of_fine_arts_boston.cleaned.csv",
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(rf"images[/\\]{re.escape(folder)}[/\\]", re.I),
        f"{SERVER_BASE}\\{name}\\{name}\\",
    )
    for folder, name in MUSEUMS
]


def rewrite_path(value: str) -> str:
    if not value:
        return value
    out = value
    for pattern, prefix in _PATTERNS:
        out = pattern.sub(lambda _: prefix, out)
    return out


def rewrite_file(path: Path) -> tuple[int, int]:
    if not path.exists():
        print(f"跳过（不存在）: {path}")
        return 0, 0
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    changed_rows = 0
    changed_cells = 0
    for row in rows:
        row_changed = False
        for col in ("image_path", "image_paths"):
            old = row.get(col) or ""
            new = rewrite_path(old)
            if new != old:
                row[col] = new
                changed_cells += 1
                row_changed = True
        if row_changed:
            changed_rows += 1
    normalized = [
        {k: str(row.get(k, "") or "").strip() for k in CSV_FIELDS}
        for row in rows
    ]
    write_csv(path, normalized)
    return changed_rows, changed_cells


def main() -> None:
    for path in CSV_FILES:
        rows, cells = rewrite_file(path)
        print(f"{path.name}: 更新 {rows} 行, {cells} 个路径字段")


if __name__ == "__main__":
    main()
