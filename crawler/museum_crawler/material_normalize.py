# -*- coding: utf-8 -*-
"""从馆方 ``medium`` / ``material`` 原文抽取规范材质词，供知识图谱 ``usesMaterial`` 使用。"""

from __future__ import annotations

import re
from typing import Iterable

# (规范名, 英文别名…) — 按专名长度降序匹配，避免 jade 先于 nephrite 误伤
_CANONICAL_ALIASES: list[tuple[str, tuple[str, ...]]] = [
    ("earthenware", ("earthenware", "stoneware", "terracotta", "pottery", "erathenware", "low-fired ware", "toneware")),
    ("porcelain", ("porcelain", "china", "celadon", "jun ware", "cizhou ware", "yue ware", "ding ware")),
    ("ceramic", ("ceramic",)),
    ("bronze", ("bronze",)),
    ("iron", ("iron", "wrought iron", "cast iron")),
    ("gold", ("gold", "gilt", "gilded", "gold leaf", "parcel gilding")),
    ("silver", ("silver", "albumen silver")),
    ("copper", ("copper", "brass")),
    ("nephrite", ("nephrite", "jadelike")),
    ("jadeite", ("jadeite",)),
    ("jade", ("jade",)),
    ("rhinoceros horn", ("rhinoceros horn", "rhino horn", "rhinocerous horn")),
    ("ivory", ("ivory",)),
    ("lacquer", ("lacquer", "urushi")),
    ("glass", ("glass",)),
    ("enamel", ("enamel", "cloisonne", "cloisonné")),
    ("silk", ("silk",)),
    ("paper", ("paper", "parchment", "rice paper")),
    ("ink", ("ink", "brush-written")),
    ("pigment", ("pigment", "pigments", "cold-painted", "cinnabar", "tempera", "color", "colored")),
    ("oil", ("oil",)),
    ("canvas", ("canvas",)),
    ("wood", ("wood", "woodblock", "hardwood")),
    ("bamboo", ("bamboo",)),
    (
        "stone",
        (
            "stone",
            "marble",
            "limestone",
            "sandstone",
            "schist",
            "serpentine",
            "calcite",
            "malachite",
            "agate",
            "alabaster",
            "turquoise",
            "hardstone",
            "hard stone",
            "rock crystal",
            "rose quartz",
            "quartz",
        ),
    ),
    ("slate", ("slate",)),
    ("bone", ("bone",)),
    ("shell", ("shell", "mother-of-pearl", "mother of pearl", "nacre", "pearl inlay")),
    ("amber", ("amber",)),
    ("leather", ("leather",)),
    (
        "textile",
        (
            "textile",
            "textiles",
            "fabric",
            "cloth",
            "embroidered",
            "wool",
            "gauze",
            "damask",
            "fur",
            "fiber",
            "filament",
            "needlework",
        ),
    ),
    ("cotton", ("cotton",)),
    ("hemp", ("hemp", "ramie")),
    ("linen", ("linen",)),
    ("metal", ("metal", "pewter", "lead")),
    ("clay", ("clay",)),
    ("plaster", ("plaster",)),
    ("wax", ("wax", "encaustic")),
    ("horn", ("horn",)),
]

# 载体优先作为主材质
_SUPPORT_PRIORITY: tuple[str, ...] = (
    "silk",
    "paper",
    "canvas",
    "wood",
    "bamboo",
    "textile",
    "cotton",
    "hemp",
    "linen",
    "leather",
    "earthenware",
    "porcelain",
    "ceramic",
    "bronze",
    "jade",
    "nephrite",
    "jadeite",
    "stone",
    "lacquer",
    "metal",
    "glass",
)

# 馆方 medium 字段中的「类型/品类」误填，非材质
_NON_MATERIAL_LABELS: frozenset[str] = frozenset(
    {
        "albums",
        "architectural elements",
        "archival material",
        "boxes",
        "calligraphy",
        "cover",
        "drawings",
        "fragments",
        "graphic arts",
        "handscroll",
        "hanging scroll",
        "jewelry",
        "paintings",
        "prints",
        "ritual implements",
        "sculpture",
        "sheet",
        "tools equipment",
        "vessels",
    }
)

_NOISE_RE = re.compile(
    r"\b(?:album|leaf|leaves|page|pages|one|from|mounted|molded|moulded|"
    r"printed|book|sketch|pasted|accordion|fold|woodblock|originally|"
    r"traces|blackened|black-surfaced|pale|white|gray|grey|"
    r"compressed|ovoid|decorated|decoration|with|without|and|the|a|an|of|on|in|"
    r"probably|possibly|reputedly|recovered|recently|modified|cut down|"
    r"variegated|speckled|translucent|opaque|mottled|polished|bluish|greenish|"
    r"grayish|yellowish|dark|light|black|brown|red|blue|purple|magenta|apple-green)\b",
    re.I,
)
_ON_SUPPORT_RE = re.compile(
    r"\bon\s+(?P<sup>[a-z][a-z\s-]{1,40}?)(?:\s*[;,.]|$)",
    re.I,
)
_DIMENSION_RE = re.compile(
    r"\b(?:H|W|D|L|Diam)(?:\s*[x×]\s*(?:H|W|D|L|Diam))*\s*(?:\(image\)\s*)?:?\s*"
    r"(?:[\d.]+\s*(?:×|x)\s*)+[\d.]+"
    r"(?:\s*(?:mm|cm|m|in\.?|inch|ft))?"
    r"(?:\s*\([^)]*(?:mm|cm|m|in\.?|inch|ft)[^)]*\))?"
    r"|\boverall\s*:\s*[\d.]+\s*(?:mm|cm|m|in\.?|inch|ft|feet)"
    r"|\([^)]*(?:\d+\s*)?[\d./\s×x]+(?:mm|cm|m|in\.?|inch|ft)[^)]*\)"
    r"|\b\d+(?:\.\d+)?\s*(?:mm|cm|m|in\.?|inch|ft)\b"
    r"|\b\d+\s+\d+/\d+\s+in\b"
    r"|\(\s*image\s*\)",
    re.I,
)
_ORPHAN_UNIT_RE = re.compile(r"\b(?:mm|cm|m|in\.?|inch|ft)\b", re.I)
_INSCRIPTION_RE = re.compile(
    r"\b(?:inscription|inscribed|dedication|seal mark|hallmark|numeral|"
    r"integrally cast|impressed|incised mark|spurious|signature reading|"
    r"spur marks|underglaze|slip painted|dated to)\b",
    re.I,
)
_GEOGRAPHY_RE = re.compile(
    r"\b(?:province|gansu|qinghai|ningxia|shanxi|hebei|hunan|yunnan|changsha|"
    r"longmen|yungang|dingzhou|niche outside|philippines|japanese mounting)\b",
    re.I,
)
_STYLE_ONLY_RE = re.compile(
    r"\b(?:gandhara type|longmen style|yungang style|erligang type|zhengzhou phase)\b",
    re.I,
)


def _dedupe_keep_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip().lower())
    return out


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _split_segments(raw: str) -> list[str]:
    """按分号拆段，并保留整段原文（史密森尼常见 ``材质; 尺寸/铭文``）。"""
    text = _normalize_whitespace(raw)
    if not text:
        return []
    parts = [_normalize_whitespace(p) for p in text.split(";") if p.strip()]
    if text not in parts:
        parts.insert(0, text)
    return parts


def _strip_dimensions(text: str) -> str:
    cleaned = _DIMENSION_RE.sub(" ", text)
    cleaned = _ORPHAN_UNIT_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;.")
    return cleaned


def _is_dimension_only(text: str) -> bool:
    norm = _normalize_whitespace(text).lower()
    if not norm:
        return True
    if re.fullmatch(r"[\d./\s×x]+(?:mm|cm|m|in|ft)?", norm, re.I):
        return True
    if re.fullmatch(r"[\d./\s]+", norm):
        return True
    return not _strip_dimensions(text).strip()


def clean_material_text(raw: str) -> str:
    """
    清洗 material 原文：仅去掉尺寸，保留品类/品牌/工艺等描述；不做规范词映射或 `` | `` 拼接。
    """
    text = _normalize_whitespace(raw or "")
    if not text:
        return ""

    def _dedupe_join(parts: list[str]) -> str:
        seen: set[str] = set()
        out: list[str] = []
        for part in parts:
            key = part.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(part)
        return "; ".join(out)

    if ";" in text:
        kept: list[str] = []
        for part in (_normalize_whitespace(p) for p in text.split(";") if p.strip()):
            cleaned = _strip_dimensions(part)
            if cleaned and not _is_dimension_only(cleaned):
                kept.append(cleaned)
        if kept:
            return _dedupe_join(kept)

    return _strip_dimensions(text)


def _is_non_material_segment(text: str) -> bool:
    norm = _normalize_whitespace(text).lower()
    if not norm:
        return True
    if norm in _NON_MATERIAL_LABELS:
        return True
    if _GEOGRAPHY_RE.search(norm):
        return True
    if _STYLE_ONLY_RE.search(norm) and not _match_aliases(norm):
        return True
    # 纯铭文/款识片段
    if _INSCRIPTION_RE.search(norm) and not _match_aliases(norm):
        return True
    # 尺寸残留
    if re.fullmatch(r"[\d./\s]+(?:mm|cm|m|in|ft)?", norm, re.I):
        return True
    return False


def _match_aliases(text: str) -> list[str]:
    tl = text.lower()
    found: list[str] = []
    used_spans: list[tuple[int, int]] = []

    alias_pairs: list[tuple[str, str]] = []
    for canon, aliases in _CANONICAL_ALIASES:
        for alias in aliases:
            alias_pairs.append((canon, alias.lower()))
    alias_pairs.sort(key=lambda x: len(x[1]), reverse=True)

    for canon, alias in alias_pairs:
        pattern = rf"\b{re.escape(alias)}\b"
        for m in re.finditer(pattern, tl):
            span = m.span()
            if any(not (span[1] <= s0 or span[0] >= s1) for s0, s1 in used_spans):
                continue
            found.append(canon)
            used_spans.append(span)
            break
    return found


def _map_color_token(text: str) -> list[str]:
    out: list[str] = []
    if re.search(r"\bcolor(?:s|ed)?\b", text, re.I):
        out.append("pigment")
    return out


def _extract_from_segment(segment: str) -> list[str]:
    if _is_non_material_segment(segment):
        return []

    materials: list[str] = []
    text = _strip_dimensions(segment)

    m = _ON_SUPPORT_RE.search(text)
    if m:
        support = m.group("sup").strip().lower()
        support = re.sub(_NOISE_RE, " ", support)
        support = _normalize_whitespace(support)
        materials.extend(_match_aliases(support))
        text = text[: m.start()] + text[m.end() :]

    text = re.sub(_NOISE_RE, " ", text.lower())
    text = _normalize_whitespace(text)
    materials.extend(_match_aliases(text))
    materials.extend(_map_color_token(segment))
    return materials


def extract_canonical_materials(raw: str) -> list[str]:
    """
    从 material 原文提取规范材质列表（小写英文单数/固定短语）。

    例::

        Album leaf; ink, color and gold on silk → ['ink', 'pigment', 'gold', 'silk']
        Ink on paper → ['ink', 'paper']
        Gilt bronze → ['gold', 'bronze']
    """
    if not (raw or "").strip():
        return []

    materials: list[str] = []
    for segment in _split_segments(raw):
        materials.extend(_extract_from_segment(segment))

    return _dedupe_keep_order(materials)[:12]


def extract_primary_material(raw: str, materials: list[str] | None = None) -> str:
    """主材质：优先载体（silk/paper/…），否则列表首项。"""
    mats = materials if materials is not None else extract_canonical_materials(raw)
    if not mats:
        return ""
    for sup in _SUPPORT_PRIORITY:
        if sup in mats:
            return sup
    return mats[0]


def format_material_base(raw: str) -> str:
    """``material_base`` 列：规范词用 `` | `` 连接。"""
    return " | ".join(extract_canonical_materials(raw))
