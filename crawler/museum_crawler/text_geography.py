# -*- coding: utf-8 -*-
"""
文本地理与年代：省份抽取、年代解析、按公元年映射朝代。

朝代推断优先级
--------------
1. ``period_start_year`` / ``period_end_year``（或从 ``period`` 解析出的起止年）→ 与朝代年表求重叠，取重叠最大者。
2. 若无可用年份，再从 ``period`` 原文做朝代关键词匹配（兜底）。
"""

from __future__ import annotations

import re
from typing import Any, Optional

_PROVINCES_EN = [
    "Anhui", "Fujian", "Gansu", "Guangdong", "Guangxi", "Guizhou",
    "Hainan", "Hebei", "Heilongjiang", "Henan", "Hubei", "Hunan",
    "Jiangsu", "Jiangxi", "Jilin", "Liaoning", "Qinghai",
    "Shaanxi", "Shandong", "Shanxi", "Sichuan", "Yunnan", "Zhejiang",
    "Ningxia", "Tibet", "Xinjiang", "Inner Mongolia",
    "Beijing", "Tianjin", "Shanghai", "Chongqing",
]
_PROVINCES_ZH = [
    "安徽", "福建", "甘肃", "广东", "广西", "贵州", "海南",
    "河北", "黑龙江", "河南", "湖北", "湖南", "江苏", "江西",
    "吉林", "辽宁", "青海", "陕西", "山东", "山西", "四川",
    "云南", "浙江", "宁夏", "西藏", "新疆", "内蒙古",
    "北京", "天津", "上海", "重庆",
]
_ALL_PROVINCES = _PROVINCES_EN + _PROVINCES_ZH

# 中国史朝代公元纪年（起止年均为 inclusive；公元前为负数）
# 并列王朝按区间细分，映射时用「与时间范围重叠最长」选取
_DYNASTY_YEAR_RANGES: list[tuple[str, str, int, int]] = [
    ("Neolithic", "新石器时代", -5000, -2000),
    ("Shang", "商", -1600, -1046),
    ("Zhou", "周", -1046, -256),
    ("Spring and Autumn", "春秋", -770, -476),
    ("Warring States", "战国", -475, -221),
    ("Qin", "秦", -221, -206),
    ("Western Han", "西汉", -206, 8),
    ("Xin", "新", 9, 23),
    ("Eastern Han", "东汉", 25, 220),
    ("Three Kingdoms", "三国", 220, 280),
    ("Western Jin", "西晋", 265, 316),
    ("Eastern Jin", "东晋", 317, 420),
    ("Northern and Southern", "南北朝", 420, 589),
    ("Sui", "隋", 581, 618),
    ("Tang", "唐", 618, 907),
    ("Five Dynasties", "五代", 907, 960),
    ("Liao", "辽", 916, 1125),
    ("Northern Song", "北宋", 960, 1127),
    ("Southern Song", "南宋", 1127, 1279),
    ("Jin", "金", 1115, 1234),
    ("Yuan", "元", 1271, 1368),
    ("Ming", "明", 1368, 1644),
    ("Qing", "清", 1644, 1911),
    ("Republic", "民国", 1912, 1949),
]

_DYNASTY_MAP: list[tuple[str, str]] = [
    ("Neolithic", "新石器时代"),
    ("Shang", "商"), ("Zhou", "周"),
    ("Spring and Autumn", "春秋"), ("Warring States", "战国"),
    ("Western Han", "西汉"), ("Eastern Han", "东汉"),
    ("Three Kingdoms", "三国"),
    ("Northern and Southern", "南北朝"),
    ("Northern Song", "北宋"), ("Southern Song", "南宋"),
    ("Five Dynasties", "五代"),
    ("Qin", "秦"),
    ("Han", "汉"),
    ("Jin", "晋"),
    ("Sui", "隋"), ("Tang", "唐"),
    ("Liao", "辽"),
    ("Song", "宋"),
    ("Yuan", "元"),
    ("Ming", "明"),
    ("Qing", "清"),
    ("Republic", "民国"),
]


def join_listish(x: Any) -> str:
    """API 中常见 list 或标量，统一成 ``; `` 拼接字符串。"""
    if isinstance(x, list):
        return "; ".join(str(i) for i in x if i)
    return str(x or "")


def _fmt_dynasty(en: str, zh: str) -> str:
    return f"{en}（{zh}）"


def _dynasty_prefer_rank(en: str) -> int:
    """中点落多个朝代时，优先中原主流王朝（宋唐宋明等）。"""
    if "Song" in en:
        return 0
    if en in ("Tang", "Ming", "Qing", "Han", "Yuan", "Sui", "Qin", "Zhou", "Shang"):
        return 1
    if en in ("Jin", "Liao", "Five Dynasties", "Three Kingdoms"):
        return 3
    return 2


def _year_overlap(art_start: int, art_end: int, dyn_start: int, dyn_end: int) -> int:
    """两闭区间重叠年数（至少按 1 年计）。"""
    lo = max(art_start, dyn_start)
    hi = min(art_end, dyn_end)
    if hi < lo:
        return 0
    return hi - lo + 1


def dynasty_from_years(
    start_year: int,
    end_year: Optional[int] = None,
) -> str:
    """
    根据公元起止年映射朝代。

    - 区间宽度 ≤80 年：与各朝代年表求重叠，取重叠最长者（并列取区间更窄者）。
    - 区间较宽（如整世纪）：取区间中点，落在哪个朝代区间内（并列取最窄区间）。

    ``start_year`` / ``end_year``：公元年，公元前为负。
    """
    if end_year is None:
        end_year = start_year
    if start_year > end_year:
        start_year, end_year = end_year, start_year

    width = end_year - start_year + 1
    if width > 80:
        mid = (start_year + end_year) // 2
        containing: list[tuple[int, str, str]] = []
        for en, zh, ds, de in _DYNASTY_YEAR_RANGES:
            if ds <= mid <= de:
                containing.append((de - ds + 1, en, zh))
        if containing:
            _, en, zh = min(containing, key=lambda x: (_dynasty_prefer_rank(x[1]), x[0]))
            return _fmt_dynasty(en, zh)
        return ""

    best: tuple[int, int, str, str] | None = None  # overlap, span, en, zh
    for en, zh, ds, de in _DYNASTY_YEAR_RANGES:
        ov = _year_overlap(start_year, end_year, ds, de)
        if ov <= 0:
            continue
        span = de - ds + 1
        key = (ov, -span, en, zh)
        if best is None or key[:2] > (best[0], -best[1]):
            best = (ov, span, en, zh)

    if best:
        return _fmt_dynasty(best[2], best[3])
    return ""


def _parse_year_token(s: str) -> Optional[int]:
    s = s.strip()
    if not s:
        return None
    bce = bool(re.search(r"B\.?\s*C\.?|BCE|BC|公元前", s, re.I))
    m = re.search(r"(-?\d{1,4})", s)
    if not m:
        return None
    y = int(m.group(1))
    if bce and y > 0:
        y = -y
    return y


def parse_period_years(period: str) -> tuple[Optional[int], Optional[int]]:
    """
    从 period 原文解析起止年（公元；公元前为负数）。
    无法解析时返回 (None, None)。
    """
    if not period or not str(period).strip():
        return None, None
    t = str(period).strip()

    # 明确起止：618-907 / 618–907 CE / 206 BCE - 220 CE
    range_m = re.search(
        r"(?P<a>[\d\s,BCEbc.\-]+?)\s*[-–—]\s*(?P<b>[\d\s,BCEbc.\-]+)",
        t,
        re.I,
    )
    if range_m and not re.search(r"century", t, re.I):
        ya = _parse_year_token(range_m.group("a"))
        yb = _parse_year_token(range_m.group("b"))
        if ya is not None and yb is not None:
            # 「c. 2600-2000 BCE」：BCE 常写在整段末尾，首年会被误解析为正数，
            # 中点落到西晋/北宋等。整段无 CE/AD 时，将范围内未标纪元的正年视为公元前。
            has_bce = bool(re.search(r"B\.?\s*C\.?|BCE|BC|公元前", t, re.I))
            has_ce = bool(
                re.search(r"(?<!B)C\.?\s*E\.|(?<!B)\bCE\b|AD|公元(?!前)", t, re.I)
            )
            if has_bce and not has_ce:
                if ya > 0:
                    ya = -ya
                if yb > 0:
                    yb = -yb
            return (ya, yb) if ya <= yb else (yb, ya)

    # 单一年份：1965 / 618 CE / 221 BCE
    if not re.search(r"century", t, re.I):
        single = _parse_year_token(t)
        if single is not None and re.fullmatch(
            r"[\d\s,BCEbc.\-]+", t.replace("公元前", "BCE"), re.I
        ):
            return single, single
        m = re.search(
            r"(?P<y>\d{3,4})\s*(?P<bce>B\.?\s*C\.?|BCE|BC|公元前)?",
            t,
            re.I,
        )
        if m and not re.search(r"\d+\s*[-–]\s*\d+", t):
            y = int(m.group("y"))
            if m.group("bce"):
                y = -y
            return y, y

    # 世纪：7th century / 11th-early 12th century
    cents = re.findall(
        r"(?:(?:early|late|mid(?:dle)?)\s+)?(\d{1,2})(?:st|nd|rd|th)\s+century",
        t,
        re.I,
    )
    if cents:
        nums = [int(c) for c in cents]
        start = (min(nums) - 1) * 100 + 1
        end = max(nums) * 100
        if re.search(r"\bB\.?\s*C\.?|BCE|BC|公元前", t, re.I):
            start, end = -end, -start
        return start, end

    # 年代：late 1700s / early 1900s
    decades = re.findall(r"(?:early|late|mid)?\s*(\d{3,4})s\b", t, re.I)
    if decades:
        nums = [int(d) for d in decades]
        start = (min(nums) // 100) * 100
        end = (max(nums) // 100) * 100 + 99
        return start, end

    return None, None


def extract_dynasty_from_period(period: str) -> str:
    """从 period 原文关键词推断朝代（无年份时的兜底）。"""
    if not period or not str(period).strip():
        return ""
    t = str(period).strip()
    tl = t.lower()

    multi = [(en, zh) for en, zh in _DYNASTY_MAP if " " in en.strip()]
    for en, zh in sorted(multi, key=lambda p: len(p[0]), reverse=True):
        if en.lower() in tl:
            return _fmt_dynasty(en, zh)

    single = [(en, zh) for en, zh in _DYNASTY_MAP if " " not in en.strip()]
    for en, zh in sorted(single, key=lambda p: len(p[0]), reverse=True):
        en_l = en.lower()
        if re.search(rf"\b{re.escape(en_l)}\b", tl):
            return _fmt_dynasty(en, zh)

    for en, zh in sorted(_DYNASTY_MAP, key=lambda p: len(p[1]), reverse=True):
        if zh and zh in t:
            return _fmt_dynasty(en, zh)

    return ""


def resolve_dynasty(
    period: str,
    period_start_year: Optional[int] = None,
    period_end_year: Optional[int] = None,
) -> str:
    """
    解析朝代：有具体年份则按年表映射；否则从 period 文本兜底。
    """
    ps, pe = period_start_year, period_end_year
    if ps is None and pe is None:
        ps, pe = parse_period_years(period)
    elif ps is not None and pe is None:
        pe = ps

    # period 已写明文化/朝代时优先文本（避免年份误解析盖掉 Neolithic 等）
    from_text = extract_dynasty_from_period(period)
    if from_text:
        tl = (period or "").lower()
        if any(
            k in tl
            for k in (
                "neolithic",
                "yangshao",
                "longshan",
                "liangzhu",
                "majiayao",
                "banpo",
                "hongshan",
                "dawenkou",
            )
        ):
            return from_text

    if ps is not None and pe is not None:
        by_year = dynasty_from_years(int(ps), int(pe))
        if by_year:
            return by_year

    return from_text


def extract_province(text: str) -> str:
    """在自由文本中匹配中外省份/直辖市名。"""
    if not text:
        return ""
    for p in _ALL_PROVINCES:
        if p.lower() in text.lower():
            return p
    m = re.search(r"(\w[\w ]+?)\s+[Pp]rovince", text)
    return m.group(1).strip() if m else ""
