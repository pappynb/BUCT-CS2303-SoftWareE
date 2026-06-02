# -*- coding: utf-8 -*-
"""
爬取 CSV 数据清洗：字段标准化、去重、图片链接有效性检测。
"""

from __future__ import annotations

import csv
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from museum_crawler.config import BASE_DIR, CSV_FIELDS
from museum_crawler.date_format import normalize_row_dates
from museum_crawler.io_csv import write_csv
from museum_crawler.material_normalize import clean_material_text
from museum_crawler.record_build import supplement_record
from museum_crawler.text_geography import parse_period_years, resolve_dynasty

log = logging.getLogger("spider")

# 文物类型 → 标准大类（用于统一三馆 type 字段）
_TYPE_CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("vessel", "ceramic", "pottery", "porcelain", "earthenware", "stoneware"), "Ceramics"),
    (("painting", "scroll", "album leaf"), "Paintings"),
    (("sculpture", "statuette", "figurine", "carving"), "Sculpture"),
    (("print", "woodblock"), "Prints"),
    (("drawing", "sketch"), "Drawings"),
    (("textile", "silk", "embroid", "tapestry", "lace", "costume"), "Textiles"),
    (("coin", "currency"), "Coins"),
    (("jade", "nephrite"), "Jade"),
    (("bronze", "metalwork", "metal", "iron", "gold", "silver"), "Metalwork"),
    (("ritual", "implement"), "Ritual Implements"),
    (("tool", "equipment", "weapon"), "Tools & Weapons"),
    (("jewelry", "ornament"), "Jewelry & Ornaments"),
    (("book", "manuscript", "calligraphy"), "Books & Calligraphy"),
    (("photograph", "albumen"), "Photographs"),
    (("furniture", "architectural"), "Architecture & Furniture"),
    (("fragment",), "Fragments"),
]


def _norm_key(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[，,。.;；:：!！?？\"'()（）\[\]{}]", "", t)
    return t


def standardize_type(raw_type: str) -> str:
    """将馆方 type 映射为标准大类；无法映射时保留原文。"""
    t = (raw_type or "").strip()
    if not t:
        return ""
    tl = t.lower()
    for keys, cat in _TYPE_CATEGORY_RULES:
        if any(k in tl for k in keys):
            return cat
    # 已是哈佛式短标签则保留
    if len(t) <= 40 and ";" not in t:
        return t
    return t.split(";")[0].strip()[:80]


def format_year_cn(year: int) -> str:
    """公元年 → 中文展示：-206 → 公元前206年，618 → 618年。"""
    if year < 0:
        return f"公元前{abs(year)}年"
    return f"{year}年"


def standardize_period_display(period: str, start: Optional[int], end: Optional[int]) -> str:
    """
    在保留馆方 period 原文的同时，若可解析起止年则生成统一后缀说明。
    写入 CSV 时不覆盖 period 原文；供报告/展示用。
    """
    base = (period or "").strip()
    if start is None and end is None:
        return base
    if start is not None and end is not None and start != end:
        return f"{base} [{format_year_cn(start)}-{format_year_cn(end)}]" if base else f"{format_year_cn(start)}-{format_year_cn(end)}"
    y = start if start is not None else end
    if y is None:
        return base
    suffix = format_year_cn(y)
    return f"{base} [{suffix}]" if base else suffix


def standardize_material(raw: str) -> str:
    """材质清洗：仅去掉尺寸，保留原文描述（品类/品牌/工艺等）。"""
    return clean_material_text(raw or "")


def standardize_row(row: dict[str, str]) -> dict[str, str]:
    """单条记录字段标准化（日期、朝代、年代、类型、材质）。"""
    out = {k: str(row.get(k, "") or "").strip() for k in CSV_FIELDS}
    out = normalize_row_dates(out)

    period = out.get("period") or ""
    ps, pe = parse_period_years(period)
    if ps is not None:
        out["period_start_year"] = str(ps)
    if pe is not None:
        out["period_end_year"] = str(pe)
    if not out.get("dynasty"):
        out["dynasty"] = resolve_dynasty(period, ps, pe)

    std_type = standardize_type(out.get("type") or "")
    if std_type:
        out["type"] = std_type

    out["material"] = standardize_material(out.get("material") or "")

    supplemented = supplement_record(dict(out))
    return {k: str(supplemented.get(k, "") or "").strip() for k in CSV_FIELDS}


@dataclass
class DuplicateRecord:
    museum_id: str
    object_id: str
    duplicate_of_museum_id: str
    duplicate_of_object_id: str
    reason: str


def find_duplicates(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[DuplicateRecord]]:
    """
    去重并返回 (保留行, 重复说明)。

    规则：
    1. ``museum_id + object_id`` 完全重复 → 保留首条
    2. 同馆 ``detail_url`` 相同
    3. 同馆 ``accession_number`` 相同（非空）
    """
    kept: list[dict[str, str]] = []
    dups: list[DuplicateRecord] = []
    seen_primary: set[tuple[str, str]] = set()
    seen_url: dict[tuple[str, str], tuple[str, str]] = {}
    seen_acc: dict[tuple[str, str], tuple[str, str]] = {}

    for row in rows:
        mid = row.get("museum_id", "")
        oid = row.get("object_id", "")
        pk = (mid, oid)
        if pk in seen_primary:
            dups.append(DuplicateRecord(mid, oid, mid, oid, "duplicate_object_id"))
            continue
        seen_primary.add(pk)

        url = (row.get("detail_url") or "").strip()
        if url:
            uk = (mid, url)
            if uk in seen_url:
                om, oo = seen_url[uk]
                dups.append(DuplicateRecord(mid, oid, om, oo, "duplicate_detail_url"))
                continue
            seen_url[uk] = (mid, oid)

        acc = (row.get("accession_number") or "").strip()
        if acc:
            ak = (mid, acc)
            if ak in seen_acc:
                om, oo = seen_acc[ak]
                dups.append(DuplicateRecord(mid, oid, om, oo, "duplicate_accession_number"))
                continue
            seen_acc[ak] = (mid, oid)

        kept.append(row)
    return kept, dups


def _safe_image_url(url: str) -> str:
    """IIIF 链接中的 ``!800,800`` 等字符需编码后再请求。"""
    u = (url or "").strip()
    if not u.startswith("http"):
        return u
    parts = urlsplit(u)
    path = quote(parts.path, safe="/:@%")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _resolve_local_image(path_str: str) -> Optional[Path]:
    lp = (path_str or "").strip()
    if not lp:
        return None
    for cand in (
        Path(lp),
        BASE_DIR / lp,
        BASE_DIR / "output" / lp,
    ):
        try:
            if cand.is_file() and cand.stat().st_size > 512:
                return cand
        except OSError:
            continue
    return None


def _local_images_ok(row: dict[str, str]) -> bool:
    for key in ("image_path", "image_paths"):
        raw = (row.get(key) or "").strip()
        if not raw:
            continue
        for part in re.split(r"\s*\|\s*", raw):
            if _resolve_local_image(part):
                return True
    return False


def _head_ok(sess: requests.Session, url: str, timeout: float) -> tuple[bool, int, str]:
    safe = _safe_image_url(url)
    if not safe or not safe.startswith("http"):
        return False, 0, "empty_or_invalid"
    try:
        r = sess.head(safe, allow_redirects=True, timeout=timeout)
        if r.status_code in (405, 403, 501):
            r = sess.get(safe, stream=True, timeout=timeout)
            next(r.iter_content(256), None)
        ct = (r.headers.get("Content-Type") or "").lower()
        ok = r.status_code < 400 and "text/html" not in ct
        if r.status_code < 400 and not ct:
            ok = True
        return ok, r.status_code, ct or "unknown"
    except Exception as exc:
        return False, 0, str(exc)[:120]


def validate_images(
    rows: list[dict[str, str]],
    *,
    timeout: float = 12.0,
    workers: int = 8,
    limit: int = 0,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """
    检测 ``image_url`` / ``image_urls`` 是否可访问；无效则清空对应字段。

    返回 (更新后的 rows, 检测报告行)。
    """
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; MuseumDataCleaner/1.0)",
        "Accept": "image/*,*/*;q=0.8",
    })

    unique_urls: list[str] = []
    seen_u: set[str] = set()
    for row in rows:
        for u in (row.get("image_url") or "").split("|"):
            u = u.strip()
            if u and u not in seen_u:
                seen_u.add(u)
                unique_urls.append(u)
        for u in re.split(r"\s*\|\s*", row.get("image_urls") or ""):
            u = u.strip()
            if u and u not in seen_u:
                seen_u.add(u)
                unique_urls.append(u)
    if limit:
        unique_urls = unique_urls[:limit]

    results: dict[str, tuple[bool, int, str]] = {}

    def _check(u: str) -> tuple[str, tuple[bool, int, str]]:
        return u, _head_ok(sess, u, timeout)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(_check, u) for u in unique_urls]
        for fut in as_completed(futs):
            u, res = fut.result()
            results[u] = res

    report: list[dict[str, str]] = []
    invalid_main: set[int] = set()

    for i, row in enumerate(rows):
        mid = row.get("museum_id", "")
        oid = row.get("object_id", "")
        main = (row.get("image_url") or "").strip()
        if not main:
            continue
        ok, code, note = results.get(main, (False, 0, "not_checked"))
        local_ok = _local_images_ok(row)
        valid = ok or local_ok
        report.append({
            "museum_id": mid,
            "object_id": oid,
            "image_url": main,
            "http_ok": "1" if ok else "0",
            "status_code": str(code),
            "content_type": note,
            "local_file_ok": "1" if local_ok else "0",
            "valid": "1" if valid else "0",
        })
        if not valid:
            invalid_main.add(i)

    new_rows: list[dict[str, str]] = []
    for i, row in enumerate(rows):
        r = dict(row)
        if i in invalid_main:
            r["image_url"] = ""
            valid_parts = []
            for u in (r.get("image_urls") or "").split("|"):
                u = u.strip()
                if not u:
                    continue
                ok, _, _ = results.get(u, (False, 0, "not_checked"))
                lp_ok = False
                if u == (row.get("image_url") or "").strip():
                    pass
                if ok:
                    valid_parts.append(u)
            if valid_parts:
                r["image_urls"] = " | ".join(valid_parts)
                r["image_url"] = valid_parts[0]
                r["image_count"] = str(len(valid_parts))
            else:
                r["image_urls"] = ""
                r["image_count"] = "0"
        new_rows.append(r)
    return new_rows, report


@dataclass
class CleanReport:
    source: str
    input_rows: int = 0
    output_rows: int = 0
    standardized_fields: int = 0
    duplicates_removed: int = 0
    images_checked: int = 0
    images_invalid: int = 0
    images_dropped: int = 0
    duplicate_records: list[DuplicateRecord] = field(default_factory=list)
    image_reports: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "input_rows": self.input_rows,
            "output_rows": self.output_rows,
            "standardized_fields": self.standardized_fields,
            "duplicates_removed": self.duplicates_removed,
            "images_checked": self.images_checked,
            "images_invalid": self.images_invalid,
            "images_dropped": self.images_dropped,
            "duplicates": [
                {
                    "museum_id": d.museum_id,
                    "object_id": d.object_id,
                    "duplicate_of_museum_id": d.duplicate_of_museum_id,
                    "duplicate_of_object_id": d.duplicate_of_object_id,
                    "reason": d.reason,
                }
                for d in self.duplicate_records
            ],
        }


def clean_csv_file(
    csv_path: Path,
    out_path: Path,
    *,
    check_images: bool = False,
    drop_no_image: bool = False,
    image_workers: int = 8,
    image_limit: int = 0,
    image_timeout: float = 12.0,
) -> CleanReport:
    report = CleanReport(source=csv_path.name)
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        raw_rows = list(csv.DictReader(fh))
    report.input_rows = len(raw_rows)

    cleaned: list[dict[str, str]] = []
    changed = 0
    for raw in raw_rows:
        before = {k: str(raw.get(k, "") or "").strip() for k in CSV_FIELDS}
        after = standardize_row(before)
        if after != before:
            changed += 1
        cleaned.append(after)
    report.standardized_fields = changed

    kept, dups = find_duplicates(cleaned)
    report.duplicates_removed = len(dups)
    report.duplicate_records = dups

    if check_images:
        kept, img_rep = validate_images(
            kept,
            timeout=image_timeout,
            workers=image_workers,
            limit=image_limit,
        )
        report.image_reports = img_rep
        report.images_checked = len(img_rep)
        report.images_invalid = sum(1 for r in img_rep if r.get("valid") != "1")

        if drop_no_image:
            before = len(kept)
            kept = [
                r for r in kept
                if (r.get("image_url") or "").strip() or _local_images_ok(r)
            ]
            report.images_dropped = before - len(kept)

    report.output_rows = len(kept)
    write_csv(out_path, kept)
    return report


def apply_material_standardization(
    csv_path: Path,
    out_path: Path | None = None,
) -> tuple[int, int]:
    """
    仅将 ``material`` 列去掉尺寸片段，其余列不变。

    返回 (总行数, 材质字段变更行数)。
    """
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    changed = 0
    for row in rows:
        old = (row.get("material") or "").strip()
        new = standardize_material(old)
        if new != old:
            changed += 1
        row["material"] = new
    dest = out_path or csv_path
    write_csv(dest, rows)
    return len(rows), changed
