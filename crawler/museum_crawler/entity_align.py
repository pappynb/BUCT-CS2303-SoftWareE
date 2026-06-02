# -*- coding: utf-8 -*-
"""
跨馆实体对齐：主实体表、别名表、溯源表 + canonical_id / URI。

作者优先用 Wikidata Q 号合并；朝代/材质/馆别等用 norm_label 合并。
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from museum_crawler.wikidata_enrich import (
    _norm_artist_key,
    _pick_artist_search_query,
    _split_artist_segments,
)

DEFAULT_URI_BASE = "https://kg.overseas-chinese-artifacts.local"

# 地点别名 → 规范 norm（跨馆统一）
_LOCATION_ALIASES: dict[str, str] = {
    "washington dc usa": "washington-dc-usa",
    "washington d c usa": "washington-dc-usa",
    "cambridge ma usa": "cambridge-ma-usa",
    "boston ma usa": "boston-ma-usa",
}


def norm_entity_key(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[，,。.;；:：!！?？\"'()（）\[\]{}]", "", t)
    return t


def slug_key(text: str) -> str:
    t = norm_entity_key(text)
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", t)
    return t.strip("-") or "unknown"


def build_uri(uri_base: str, canonical_id: str) -> str:
    """``entity:artist:Q123`` → ``https://…/entity/artist/Q123``。"""
    path = canonical_id.replace(":", "/")
    return f"{uri_base.rstrip('/')}/{quote(path, safe='/')}"


@dataclass
class EntityRegistry:
    """全局实体注册表：对齐、别名、溯源。"""

    uri_base: str = DEFAULT_URI_BASE
    masters: dict[str, dict[str, Any]] = field(default_factory=dict)
    aliases: list[dict[str, str]] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)
    _alias_index: dict[tuple[str, str], str] = field(default_factory=dict)
    _artist_q_index: dict[str, str] = field(default_factory=dict)

    def _touch_master(
        self,
        canonical_id: str,
        *,
        entity_type: str,
        label: str,
        norm_label: str,
        external_id: str = "",
    ) -> None:
        item = self.masters.get(canonical_id)
        if not item:
            self.masters[canonical_id] = {
                "canonical_id": canonical_id,
                "entity_type": entity_type,
                "label": label,
                "norm_label": norm_label,
                "uri": build_uri(self.uri_base, canonical_id),
                "external_id": external_id,
                "source_count": 1,
            }
        else:
            item["source_count"] += 1
            if external_id and not item.get("external_id"):
                item["external_id"] = external_id

    def _add_alias(
        self,
        canonical_id: str,
        alias: str,
        *,
        match_method: str,
        confidence: str = "1.0",
    ) -> None:
        norm_a = norm_entity_key(alias)
        if not norm_a:
            return
        key = (self.masters[canonical_id]["entity_type"], norm_a)
        self._alias_index[key] = canonical_id
        self.aliases.append({
            "canonical_id": canonical_id,
            "alias": alias,
            "norm_alias": norm_a,
            "match_method": match_method,
            "confidence": confidence,
        })

    def _add_source(
        self,
        canonical_id: str,
        *,
        museum_id: str,
        object_id: str,
        field_name: str,
        raw_value: str,
        source_file: str = "",
    ) -> None:
        if not raw_value.strip():
            return
        self.sources.append({
            "canonical_id": canonical_id,
            "museum_id": museum_id,
            "object_id": object_id,
            "field_name": field_name,
            "raw_value": raw_value,
            "source_file": source_file,
        })

    def register(
        self,
        entity_type: str,
        label: str,
        *,
        norm_key: Optional[str] = None,
        external_id: str = "",
        museum_id: str = "",
        object_id: str = "",
        field_name: str = "",
        source_file: str = "",
        match_method: str = "norm_label",
        confidence: str = "1.0",
    ) -> str:
        """注册共享实体，返回 ``canonical_id``。"""
        label = (label or "").strip()
        norm = norm_key or norm_entity_key(label)
        if not norm and not external_id:
            return ""

        prefix = f"entity:{entity_type.lower()}"
        if entity_type == "Artist" and external_id.startswith("Q"):
            canonical_id = f"{prefix}:{external_id}"
            self._artist_q_index[external_id] = canonical_id
        elif entity_type == "Artist":
            q_cid = self._artist_q_index.get(external_id) if external_id.startswith("Q") else None
            if not q_cid:
                idx_key = ("Artist", norm)
                q_cid = self._alias_index.get(idx_key)
            if q_cid:
                canonical_id = q_cid
            else:
                canonical_id = f"{prefix}:{slug_key(label)}"
        elif entity_type == "Museum":
            canonical_id = f"{prefix}:{norm_key or label}"
        elif entity_type == "Location":
            loc_slug = _LOCATION_ALIASES.get(norm, slug_key(label))
            canonical_id = f"{prefix}:{loc_slug}"
        else:
            idx_key = (entity_type, norm)
            existing = self._alias_index.get(idx_key)
            canonical_id = existing or f"{prefix}:{slug_key(norm)}"

        self._touch_master(
            canonical_id,
            entity_type=entity_type,
            label=label or norm,
            norm_label=norm,
            external_id=external_id,
        )
        self._add_alias(canonical_id, label or norm, match_method=match_method, confidence=confidence)
        self._alias_index[(entity_type, norm)] = canonical_id
        if field_name:
            self._add_source(
                canonical_id,
                museum_id=museum_id,
                object_id=object_id,
                field_name=field_name,
                raw_value=label,
                source_file=source_file,
            )
        return canonical_id

    def register_artist(
        self,
        artist_name: str,
        *,
        artist_field_raw: str = "",
        wikidata_id: str = "",
        museum_id: str = "",
        object_id: str = "",
        source_file: str = "",
    ) -> str:
        """
        注册作者；仅当与行级 ``artist_wikidata_id`` 对应的主作者段一致时使用 Q 号。
        """
        name = (artist_name or "").strip()
        if not name:
            return ""
        wd = (wikidata_id or "").strip()
        primary = _pick_artist_search_query(artist_field_raw or name)
        use_q = ""
        if wd.startswith("Q") and primary:
            if _norm_artist_key(name) == _norm_artist_key(primary):
                use_q = wd
            elif len(_split_artist_segments(artist_field_raw or name)) == 1:
                use_q = wd
        return self.register(
            "Artist",
            name,
            external_id=use_q,
            museum_id=museum_id,
            object_id=object_id,
            field_name="artist",
            source_file=source_file,
            match_method="wikidata" if use_q else "norm_label",
            confidence="0.95" if use_q else "0.8",
        )

    def register_artifact(
        self,
        museum_id: str,
        object_id: str,
        title: str,
        *,
        source_file: str = "",
    ) -> str:
        cid = f"entity:artifact:{museum_id}:{object_id}"
        self._touch_master(
            cid,
            entity_type="Artifact",
            label=title or object_id,
            norm_label=norm_entity_key(title or object_id),
        )
        self._add_source(
            cid,
            museum_id=museum_id,
            object_id=object_id,
            field_name="title",
            raw_value=title,
            source_file=source_file,
        )
        return cid

    def write_tables(self, out_dir: Path) -> dict[str, int]:
        out_dir.mkdir(parents=True, exist_ok=True)

        def _w(name: str, fields: tuple[str, ...], rows: list[dict[str, str]]) -> int:
            path = out_dir / name
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fields)
                w.writeheader()
                for r in rows:
                    w.writerow(r)
            return len(rows)

        master_rows = sorted(
            [
                {
                    "canonical_id": m["canonical_id"],
                    "entity_type": m["entity_type"],
                    "label": m["label"],
                    "norm_label": m["norm_label"],
                    "uri": m["uri"],
                    "external_id": m.get("external_id") or "",
                    "source_count": str(m["source_count"]),
                }
                for m in self.masters.values()
                if m["entity_type"] != "Artifact"
            ],
            key=lambda x: x["canonical_id"],
        )
        # 去重 alias（同一 canonical + norm_alias）
        seen_a: set[tuple[str, str]] = set()
        alias_rows: list[dict[str, str]] = []
        for a in self.aliases:
            key = (a["canonical_id"], a["norm_alias"])
            if key in seen_a:
                continue
            seen_a.add(key)
            alias_rows.append(a)
        alias_rows.sort(key=lambda x: (x["canonical_id"], x["norm_alias"]))

        return {
            "entity_master": _w(
                "entity_master.csv",
                ("canonical_id", "entity_type", "label", "norm_label", "uri", "external_id", "source_count"),
                master_rows,
            ),
            "entity_alias": _w(
                "entity_alias.csv",
                ("canonical_id", "alias", "norm_alias", "match_method", "confidence"),
                alias_rows,
            ),
            "entity_source": _w(
                "entity_source.csv",
                ("canonical_id", "museum_id", "object_id", "field_name", "raw_value", "source_file"),
                sorted(self.sources, key=lambda x: (x["canonical_id"], x["museum_id"], x["object_id"])),
            ),
        }


def _split_artists(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"\s*(?:;|,|、| and |&|/)\s*", raw.strip(), flags=re.I)
    return [p for p in (x.strip() for x in parts) if p]


def _split_cultures(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    if ";" in text:
        parts = [p.strip() for p in text.split(";")]
    elif " / " in text:
        parts = [p.strip() for p in text.split(" / ")]
    else:
        parts = [text]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        key = norm_entity_key(p)
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out[:12]


def _resolve_row_dynasty(row: dict[str, str]) -> str:
    from museum_crawler.text_geography import parse_period_years, resolve_dynasty

    dynasty = (row.get("dynasty") or "").strip()
    if dynasty:
        return dynasty
    period = (row.get("period") or "").strip()
    y0 = y1 = None
    for key in ("period_start_year", "period_end_year"):
        v = (row.get(key) or "").strip()
        if not v:
            continue
        try:
            if key == "period_start_year":
                y0 = int(v)
            else:
                y1 = int(v)
        except ValueError:
            pass
    if y0 is None and y1 is None:
        y0, y1 = parse_period_years(period)
    return resolve_dynasty(period, y0, y1)


def build_registry_from_csvs(csv_paths: list[Path], *, uri_base: str = DEFAULT_URI_BASE) -> EntityRegistry:
    """扫描 CSV，预注册所有共享实体（供 ``align_entities.py``）。"""
    from museum_crawler.material_normalize import extract_canonical_materials

    reg = EntityRegistry(uri_base=uri_base)
    for csv_path in csv_paths:
        if not csv_path.exists():
            continue
        with open(csv_path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                mid = str(row.get("museum_id") or "").strip()
                oid = str(row.get("object_id") or "").strip()
                if not mid or not oid:
                    continue
                sf = csv_path.name
                reg.register_artifact(mid, oid, (row.get("title") or oid).strip(), source_file=sf)
                reg.register(
                    "Museum",
                    (row.get("museum") or f"Museum-{mid}").strip(),
                    norm_key=mid,
                    museum_id=mid, object_id=oid, field_name="museum", source_file=sf,
                )
                loc = (row.get("location") or "").strip()
                if loc:
                    reg.register(
                        "Location", loc,
                        museum_id=mid, object_id=oid, field_name="location", source_file=sf,
                    )
                dynasty = _resolve_row_dynasty(row)
                if dynasty:
                    reg.register(
                        "Dynasty", dynasty,
                        museum_id=mid, object_id=oid, field_name="dynasty", source_file=sf,
                    )
                obj_type = (row.get("type") or "").strip()
                if obj_type:
                    reg.register(
                        "ArtifactType", obj_type,
                        museum_id=mid, object_id=oid, field_name="type", source_file=sf,
                    )
                for mat in extract_canonical_materials(row.get("material") or ""):
                    reg.register(
                        "Material", mat,
                        museum_id=mid, object_id=oid, field_name="material", source_file=sf,
                    )
                for culture in _split_cultures(row.get("culture") or ""):
                    reg.register(
                        "Culture", culture,
                        museum_id=mid, object_id=oid, field_name="culture", source_file=sf,
                    )
                wd = (row.get("artist_wikidata_id") or "").strip()
                raw_artist = (row.get("artist") or "").strip()
                for artist in _split_artists(raw_artist):
                    reg.register_artist(
                        artist,
                        artist_field_raw=raw_artist,
                        wikidata_id=wd,
                        museum_id=mid,
                        object_id=oid,
                        source_file=sf,
                    )
    return reg
