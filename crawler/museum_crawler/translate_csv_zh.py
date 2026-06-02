# -*- coding: utf-8 -*-
"""将 CSV 指定列翻译为中文，输出到新文件（不修改源文件）。"""

from __future__ import annotations

import csv
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("spider")

TYPE_ZH: dict[str, str] = {
    "Architecture & Furniture": "建筑与家具",
    "Archival Material": "档案文献",
    "Books & Calligraphy": "书籍与书法",
    "Boxes": "盒",
    "Ceramics": "陶瓷",
    "Coins": "钱币",
    "Drawings": "绘画稿",
    "Fragments": "残片",
    "Furnishings": "陈设",
    "Jewelry & Ornaments": "珠宝与饰物",
    "Lighting Devices": "照明器",
    "Material Specimens": "材质标本",
    "Mirrors": "镜",
    "Musical Instruments": "乐器",
    "Paintings": "绘画",
    "Photographs": "照片",
    "Plaques": "匾牌",
    "Prints": "版画",
    "Recreational Artifacts": "娱乐用品",
    "Ritual Implements": "礼器",
    "Rubbings": "拓片",
    "Sculpture": "雕塑",
    "Seals": "印玺",
    "Tablets": "碑帖",
    "Textiles": "纺织品",
    "Tools & Weapons": "工具与武器",
}

CULTURE_ZH: dict[str, str] = {
    "Chinese": "中国",
    "Chinese?": "中国",
}

MUSEUM_ZH: dict[str, str] = {
    "Harvard Art Museums": "哈佛艺术博物馆",
    "Smithsonian Institution": "史密森尼学会",
}

LOCATION_ZH: dict[str, str] = {
    "Cambridge, MA, USA": "美国马萨诸塞州剑桥",
    "Washington, DC, USA": "美国华盛顿特区",
}

DYNASTY_PREFIX_ZH: dict[str, str] = {
    "Neolithic": "新石器时代",
    "Shang": "商",
    "Zhou": "周",
    "Qin": "秦",
    "Han": "汉",
    "Western Han": "西汉",
    "Eastern Han": "东汉",
    "Three Kingdoms": "三国",
    "Western Jin": "西晋",
    "Eastern Jin": "东晋",
    "Northern and Southern": "南北朝",
    "Sui": "隋",
    "Tang": "唐",
    "Five Dynasties": "五代",
    "Northern Song": "北宋",
    "Southern Song": "南宋",
    "Yuan": "元",
    "Ming": "明",
    "Qing": "清",
    "Republic": "民国",
}

TRANSLATE_FIELDS = (
    "title",
    "type",
    "material",
    "dynasty",
    "description",
    "culture",
    "museum",
    "location",
)

_CHUNK_SIZE = 4500


def dynasty_to_zh(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    m = re.search(r"（([^）]+)）", text)
    if m:
        return m.group(1).strip()
    for en, zh in sorted(DYNASTY_PREFIX_ZH.items(), key=lambda x: len(x[0]), reverse=True):
        if text.startswith(en):
            return zh
    return text


def map_or_empty(raw: str, mapping: dict[str, str]) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    return mapping.get(text, "")


class TranslationCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, str] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.data = {}

    def get(self, key: str) -> str | None:
        with self._lock:
            return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self.data[key] = value

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=0),
                encoding="utf-8",
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self.data)


def _translate_once(text: str, delay: float) -> str:
    from deep_translator import GoogleTranslator

    translator = GoogleTranslator(source="auto", target="zh-CN")
    for attempt in range(4):
        try:
            out = translator.translate(text)
            time.sleep(delay)
            return out or text
        except Exception as exc:
            wait = delay * (2**attempt) + 0.5
            log.warning("翻译重试 (%s/4): %s", attempt + 1, exc)
            time.sleep(wait)
    return text


def _translate_long(text: str, delay: float) -> str:
    if len(text) <= _CHUNK_SIZE:
        return _translate_once(text, delay)

    parts: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + _CHUNK_SIZE, len(text))
        if end < len(text):
            split = max(text.rfind(". ", start, end), text.rfind(" ", start, end))
            if split > start:
                end = split + 1
        parts.append(_translate_once(text[start:end], delay))
        start = end
    return "".join(parts)


def _prefetch_machine_translations(
    texts: list[str],
    cache: TranslationCache,
    *,
    delay: float,
    workers: int,
) -> None:
    pending = [t for t in texts if not cache.get(t)]
    if not pending:
        return

    log.info("待机翻 %d 条（缓存已有 %d 条）", len(pending), len(cache))

    def job(text: str) -> tuple[str, str]:
        return text, _translate_long(text, delay)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(job, t): t for t in pending}
        for fut in as_completed(futures):
            src, out = fut.result()
            cache.set(src, out)
            done += 1
            if done % 20 == 0 or done == len(pending):
                cache.save()
                log.info("机翻进度 %d / %d", done, len(pending))
    cache.save()


def translate_csv_fields(
    src: Path,
    dst: Path,
    *,
    fields: tuple[str, ...] = TRANSLATE_FIELDS,
    cache_path: Path | None = None,
    delay: float = 0.05,
    workers: int = 4,
    limit: int | None = None,
    skip_prefetch: bool = False,
) -> dict[str, int]:
    cache_file = cache_path or (dst.parent / ".translate_cache.json")
    cache = TranslationCache(cache_file)

    with open(src, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    if limit is not None:
        rows = rows[:limit]

    machine_fields = {"title", "material", "description"}
    unique_texts: set[str] = set()
    for row in rows:
        for f in machine_fields:
            if f in fields:
                t = (row.get(f) or "").strip()
                if t:
                    unique_texts.add(t)

    if not skip_prefetch:
        _prefetch_machine_translations(
            sorted(unique_texts, key=len),
            cache,
            delay=delay,
            workers=workers,
        )

    mappers: dict[str, Callable[[str], str]] = {
        "type": lambda s: map_or_empty(s, TYPE_ZH) or cache.get(s.strip()) or s,
        "culture": lambda s: map_or_empty(s, CULTURE_ZH) or s,
        "museum": lambda s: map_or_empty(s, MUSEUM_ZH) or s,
        "location": lambda s: map_or_empty(s, LOCATION_ZH) or s,
        "dynasty": dynasty_to_zh,
        "title": lambda s: cache.get(s.strip()) or s,
        "material": lambda s: cache.get(s.strip()) or s,
        "description": lambda s: cache.get(s.strip()) or s,
    }

    out_rows: list[dict[str, Any]] = []
    for row in rows:
        new_row = dict(row)
        for f in fields:
            if f not in new_row:
                continue
            raw = (new_row.get(f) or "").strip()
            if not raw:
                continue
            fn = mappers.get(f)
            if fn:
                new_row[f] = fn(raw)
        out_rows.append(new_row)

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(out_rows)

    return {
        "input_rows": len(rows),
        "output_rows": len(out_rows),
        "cache_entries": len(cache),
        "machine_fields": len(unique_texts),
    }
