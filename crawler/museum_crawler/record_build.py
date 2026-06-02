# -*- coding: utf-8 -*-
"""
三馆爬虫统一落库记录：字段名与《数据库字段设计-三馆平台》《MySQL 设计方案》对齐。

- ``empty_record`` / ``finalize_record``：保证每条 CSV/MySQL 行包含全部 CSV_FIELDS。
- ``supplement_record``：用同条记录内其它列 + 规则推断补全空字段（非跨馆联网）。
- 作者 Wikidata 仍由 ``enrich_wikidata.py`` 增量写入。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from museum_crawler.config import CSV_FIELDS, blank_artist_enrichment
from museum_crawler.text_geography import (
    extract_province,
    parse_period_years,
    resolve_dynasty,
)

MUSEUM_META: dict[int, dict[str, str]] = {
    1: {
        "museum": "Smithsonian Institution",
        "location": "Washington, DC, USA",
    },
    2: {
        "museum": "Harvard Art Museums",
        "location": "Cambridge, MA, USA",
    },
    3: {
        "museum": "Museum of Fine Arts, Boston",
        "location": "Boston, MA, USA",
    },
}


def empty_record(museum_id: int) -> dict[str, str]:
    """返回全部 CSV 列均为空串的模板（含馆别默认 museum/location）。"""
    meta = MUSEUM_META.get(museum_id, {})
    row: dict[str, str] = {k: "" for k in CSV_FIELDS}
    row["museum_id"] = str(museum_id)
    row["museum"] = meta.get("museum", "")
    row["location"] = meta.get("location", "")
    row.update(blank_artist_enrichment())
    return row


def _s(v: Any) -> str:
    if v is None:
        return ""
    t = str(v).strip()
    return t if t.lower() not in ("none", "null", "nan") else ""


def _merge_description(parts: list[str], title: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        t = _s(p)
        if not t or t == title or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return " ".join(out)[:8000]


def supplement_record(row: dict[str, Any]) -> dict[str, Any]:
    """用本行已有信息补全空字段（不发起网络请求）。"""
    title = _s(row.get("title"))
    period = _s(row.get("period"))
    material = _s(row.get("material"))
    obj_type = _s(row.get("type"))
    artist = _s(row.get("artist"))
    culture = _s(row.get("culture"))
    provenance = _s(row.get("provenance"))
    desc = _s(row.get("description"))

    ps, pe = parse_period_years(period)
    if not _s(row.get("period_start_year")) and ps is not None:
        row["period_start_year"] = str(ps)
    if not _s(row.get("period_end_year")) and pe is not None:
        row["period_end_year"] = str(pe)

    try:
        y0 = int(str(row.get("period_start_year") or "").strip()) if _s(row.get("period_start_year")) else None
    except ValueError:
        y0 = None
    try:
        y1 = int(str(row.get("period_end_year") or "").strip()) if _s(row.get("period_end_year")) else None
    except ValueError:
        y1 = None
    row["dynasty"] = resolve_dynasty(period, y0, y1)

    if not _s(row.get("artist_province")):
        row["artist_province"] = extract_province(
            " ".join([culture, provenance, desc, artist, _s(row.get("location"))])
        )

    if not desc:
        row["description"] = _merge_description(
            [title, period, obj_type, material, artist, culture, provenance],
            title,
        ) or title
    elif len(desc) < 40 and title and title not in desc:
        row["description"] = _merge_description([desc, period, material, culture], title)

    if not obj_type and material:
        row["type"] = _infer_type_from_material(material)
    if not _s(row.get("material")) and obj_type:
        row["material"] = obj_type

    if not _s(row.get("culture")) and _s(row.get("artist_province")):
        row["culture"] = "China"

    if not _s(row.get("accession_number")):
        oid = _s(row.get("object_id"))
        if oid and re.search(r"[\d.]+", oid):
            row["accession_number"] = oid

    return row


def _infer_type_from_material(material: str) -> str:
    ml = material.lower()
    rules = [
        (("bronze", "铜"), "Metalwork / Bronze"),
        (("ceramic", "porcelain", "earthenware", "pottery", "瓷", "陶"), "Ceramics"),
        (("jade", "玉"), "Jade"),
        (("silk", "textile", "embroider", "织", "绣"), "Textiles"),
        (("ink", "paper", "painting", "scroll", "画"), "Paintings"),
        (("lacquer", "漆"), "Lacquer"),
        (("gold", "silver", "gilt"), "Metalwork"),
        (("wood", "木"), "Sculpture / Wood"),
        (("stone", "marble", "石"), "Sculpture / Stone"),
    ]
    for keys, label in rules:
        if any(k in ml for k in keys):
            return label
    return ""


def finalize_record(partial: dict[str, Any], museum_id: int) -> dict[str, str]:
    """合并解析结果 → 补全 → 仅保留 CSV_FIELDS 字符串字典。"""
    base = empty_record(museum_id)
    for k, v in partial.items():
        if k in base and v is not None:
            base[k] = _s(v)
    base["museum_id"] = str(museum_id)
    meta = MUSEUM_META.get(museum_id, {})
    if not base["museum"]:
        base["museum"] = meta.get("museum", "")
    if not base["location"]:
        base["location"] = meta.get("location", "")
    if not base["title"]:
        base["title"] = "(untitled)"
    supplemented = supplement_record(dict(base))
    return {k: _s(supplemented.get(k, "")) for k in CSV_FIELDS}
