# -*- coding: utf-8 -*-
"""CSV 日期字段格式统一为 ISO ``YYYY-MM-DD``。"""

from __future__ import annotations

import re

_DATE_FULL = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATE_YEAR = re.compile(r"^(\d{4})$")
_DATE_YEAR_MONTH = re.compile(r"^(\d{4})-(\d{2})$")

# 负年份（公元前）如 -0519 → -0519-01-01
_DATE_YEAR_BCE = re.compile(r"^-(\d{1,4})$")


def normalize_iso_date(val: str, *, year_default_month_day: str = "01-01") -> str:
    """
    将日期规范为 ``YYYY-MM-DD``。

    - 已是 ``YYYY-MM-DD``：原样返回
    - 仅 ``YYYY``：补 ``-01-01``（表示年精度）
    - ``YYYY-MM``：补 ``-01``
    - 空串：返回空串
    """
    v = (val or "").strip()
    if not v:
        return ""
    if _DATE_FULL.match(v):
        return v
    m = _DATE_YEAR.match(v)
    if m:
        return f"{m.group(1)}-{year_default_month_day}"
    m = _DATE_YEAR_MONTH.match(v)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01"
    m = _DATE_YEAR_BCE.match(v)
    if m:
        y = m.group(1).zfill(4)
        return f"-{y}-{year_default_month_day}"
    # 含时间戳时只取日期部分
    if len(v) >= 10 and v[4] == "-" and v[7] == "-":
        return v[:10]
    return v


def normalize_row_dates(row: dict[str, str]) -> dict[str, str]:
    """统一一行中与日期相关的列。"""
    out = dict(row)
    for key in ("crawl_date", "artist_enriched_at", "artist_birth", "artist_death"):
        if key in out:
            out[key] = normalize_iso_date(str(out.get(key) or ""))
    return out
