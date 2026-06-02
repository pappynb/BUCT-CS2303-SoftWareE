# -*- coding: utf-8 -*-
"""参考文献 / Publication History 文本整理。"""
from __future__ import annotations

import re
from html import unescape
from typing import Any


def strip_html(text: str) -> str:
    if not text:
        return ""
    t = unescape(str(text))
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def format_harvard_publications(pubs: Any) -> str:
    """哈佛 API ``publications`` 数组 → 参考文献串（多条用 `` | `` 分隔）。"""
    if not pubs or not isinstance(pubs, list):
        return ""
    lines: list[str] = []
    for item in pubs:
        if not isinstance(item, dict):
            continue
        cite = strip_html(item.get("citation") or "")
        pages = (item.get("pagenumbers") or "").strip()
        if pages and pages.lower() not in cite.lower():
            cite = f"{cite} {pages}".strip() if cite else pages
        if cite and cite not in lines:
            lines.append(cite)
    return " | ".join(lines)[:12000]
