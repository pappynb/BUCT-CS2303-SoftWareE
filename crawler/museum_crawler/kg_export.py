# -*- coding: utf-8 -*-
"""
知识图谱导出：维表 + 实体对齐（canonical_id / URI）+ 关系三元组。

输出目录 ``output/kg/``::

    align/entity_master.csv   跨馆唯一主实体 + URI
    align/entity_alias.csv    别名 → 主实体
    align/entity_source.csv   溯源（馆别 / object_id / 原文字段）
    artifacts.csv / materials.csv / …
    relations/*.csv           from(文物) relation to(对齐后 canonical_id)
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import Any

from museum_crawler.entity_align import DEFAULT_URI_BASE, EntityRegistry, norm_entity_key
from museum_crawler.material_normalize import (
    extract_canonical_materials,
    extract_primary_material,
    format_material_base,
)
from museum_crawler.text_geography import parse_period_years, resolve_dynasty

log = logging.getLogger("spider")

_DIM_TABLES: dict[str, tuple[str, str, str]] = {
    "Material": ("materials.csv", "material_id", "material"),
    "Museum": ("museums.csv", "museum_id", "museum"),
    "Dynasty": ("dynasties.csv", "dynasty_id", "dynasty"),
    "Artist": ("artists.csv", "artist_id", "artist"),
    "Location": ("locations.csv", "location_id", "location"),
    "ArtifactType": ("types.csv", "type_id", "type_name"),
    "Culture": ("cultures.csv", "culture_id", "culture"),
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


def _add_relation(
    relations: set[tuple[str, str, str]],
    *,
    art_id: str,
    relation: str,
    to_id: str,
) -> None:
    if to_id:
        relations.add((art_id, relation, to_id))


def _write_split_by_relation(
    out_dir: Path,
    rows: list[dict[str, str]],
    *,
    remove_mixed: list[Path] | None = None,
) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    buckets: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        buckets.setdefault(row["relation"], []).append(row)
    counts: dict[str, int] = {}
    for rel, rel_rows in sorted(buckets.items()):
        path = out_dir / f"{rel}.csv"
        _write_csv(path, ("from", "relation", "to"), rel_rows)
        counts[rel] = len(rel_rows)
    if remove_mixed:
        for p in remove_mixed:
            if p.exists():
                p.unlink()
    return counts


def _write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _write_dim_tables(kg_dir: Path, registry: EntityRegistry) -> dict[str, int]:
    counts: dict[str, int] = {}
    by_type: dict[str, list[dict[str, Any]]] = {}
    for item in registry.masters.values():
        et = item["entity_type"]
        if et == "Artifact":
            continue
        by_type.setdefault(et, []).append(item)

    for etype, (filename, id_col, name_col) in _DIM_TABLES.items():
        items = sorted(by_type.get(etype, []), key=lambda x: x["canonical_id"])
        rows = [{id_col: it["canonical_id"], name_col: it["label"]} for it in items]
        _write_csv(kg_dir / filename, (id_col, name_col), rows)
        counts[etype] = len(rows)
    return counts


def export_knowledge_graph(
    csv_paths: list[Path],
    out_dir: Path,
    *,
    export_nt: bool = False,
    uri_base: str = DEFAULT_URI_BASE,
) -> dict[str, int]:
    """从博物馆 CSV 导出对齐后的 ``output/kg/``。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    kg_dir = out_dir / "kg"
    kg_dir.mkdir(parents=True, exist_ok=True)

    registry = EntityRegistry(uri_base=uri_base)
    relations: set[tuple[str, str, str]] = set()
    properties: set[tuple[str, str, str]] = set()
    artifact_rows: list[dict[str, str]] = []

    for csv_path in csv_paths:
        if not csv_path.exists() or csv_path.stat().st_size == 0:
            continue
        source_file = csv_path.name
        with open(csv_path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                oid = (row.get("object_id") or "").strip()
                mid = str(row.get("museum_id") or "").strip()
                if not oid or not mid:
                    continue

                title = (row.get("title") or oid).strip() or oid
                art_id = registry.register_artifact(
                    mid, oid, title, source_file=source_file,
                )
                artifact_rows.append({
                    "artifact_id": art_id,
                    "museum_id": mid,
                    "object_id": oid,
                    "title": title,
                })

                museum_cid = registry.register(
                    "Museum",
                    (row.get("museum") or f"Museum-{mid}").strip(),
                    norm_key=mid,
                    museum_id=mid,
                    object_id=oid,
                    field_name="museum",
                    source_file=source_file,
                )
                _add_relation(relations, art_id=art_id, relation="belongsToMuseum", to_id=museum_cid)

                location = (row.get("location") or "").strip()
                if location:
                    loc_cid = registry.register(
                        "Location", location,
                        museum_id=mid, object_id=oid, field_name="location", source_file=source_file,
                    )
                    _add_relation(relations, art_id=art_id, relation="locatedIn", to_id=loc_cid)

                dynasty = _resolve_row_dynasty(row)
                if dynasty:
                    dy_cid = registry.register(
                        "Dynasty", dynasty,
                        museum_id=mid, object_id=oid, field_name="dynasty", source_file=source_file,
                    )
                    _add_relation(relations, art_id=art_id, relation="belongsToDynasty", to_id=dy_cid)

                obj_type = (row.get("type") or "").strip()
                if obj_type:
                    ty_cid = registry.register(
                        "ArtifactType", obj_type,
                        museum_id=mid, object_id=oid, field_name="type", source_file=source_file,
                    )
                    _add_relation(relations, art_id=art_id, relation="hasType", to_id=ty_cid)

                material_raw = (row.get("material") or "").strip()
                mats = extract_canonical_materials(material_raw)
                primary = extract_primary_material(material_raw, mats)
                for mat in mats:
                    mat_cid = registry.register(
                        "Material", mat,
                        museum_id=mid, object_id=oid, field_name="material", source_file=source_file,
                    )
                    _add_relation(relations, art_id=art_id, relation="usesMaterial", to_id=mat_cid)
                if primary:
                    prim_cid = registry.register(
                        "Material", primary,
                        museum_id=mid, object_id=oid, field_name="material", source_file=source_file,
                    )
                    _add_relation(relations, art_id=art_id, relation="hasPrimaryMaterial", to_id=prim_cid)

                if material_raw:
                    properties.add((art_id, "materialSummary", material_raw))
                base_str = format_material_base(material_raw)
                if base_str:
                    properties.add((art_id, "materialBase", base_str))
                if primary:
                    properties.add((art_id, "materialPrimary", primary))

                for culture in _split_cultures(row.get("culture") or ""):
                    cu_cid = registry.register(
                        "Culture", culture,
                        museum_id=mid, object_id=oid, field_name="culture", source_file=source_file,
                    )
                    _add_relation(relations, art_id=art_id, relation="hasCulture", to_id=cu_cid)

                raw_artist = (row.get("artist") or "").strip()
                wd = (row.get("artist_wikidata_id") or "").strip()
                for artist in _split_artists(raw_artist):
                    ar_cid = registry.register_artist(
                        artist,
                        artist_field_raw=raw_artist,
                        wikidata_id=wd,
                        museum_id=mid,
                        object_id=oid,
                        source_file=source_file,
                    )
                    _add_relation(relations, art_id=art_id, relation="createdBy", to_id=ar_cid)

                for pred, key in (
                    ("title", "title"),
                    ("period", "period"),
                    ("periodStartYear", "period_start_year"),
                    ("periodEndYear", "period_end_year"),
                    ("description", "description"),
                    ("provenance", "provenance"),
                    ("bibliography", "bibliography"),
                    ("dimensions", "dimensions"),
                    ("detailUrl", "detail_url"),
                    ("imageUrl", "image_url"),
                    ("imageUrls", "image_urls"),
                    ("iiifManifestUrl", "iiif_manifest_url"),
                    ("imagePath", "image_path"),
                    ("imagePaths", "image_paths"),
                    ("imageCount", "image_count"),
                    ("creditLine", "credit_line"),
                    ("accessionNumber", "accession_number"),
                    ("sourceUpdatedAt", "source_updated_at"),
                    ("crawlDate", "crawl_date"),
                    ("artistProvince", "artist_province"),
                    ("artistWikidataId", "artist_wikidata_id"),
                    ("artistBirth", "artist_birth"),
                    ("artistDeath", "artist_death"),
                    ("artistBio", "artist_bio"),
                    ("artistWikipediaSummary", "artist_wikipedia_summary"),
                    ("artistEnrichedAt", "artist_enriched_at"),
                ):
                    v = (row.get(key) or "").strip()
                    if v:
                        properties.add((art_id, pred, v))

    align_stats = registry.write_tables(kg_dir / "align")

    _write_csv(
        kg_dir / "artifacts.csv",
        ("artifact_id", "museum_id", "object_id", "title"),
        sorted(artifact_rows, key=lambda x: x["artifact_id"]),
    )
    dim_counts = _write_dim_tables(kg_dir, registry)

    rel_rows = [{"from": f, "relation": r, "to": t} for f, r, t in sorted(relations)]
    rel_counts = _write_split_by_relation(
        kg_dir / "relations",
        rel_rows,
        remove_mixed=[kg_dir / "relations.csv", out_dir / "kg_triples.csv"],
    )

    prop_rows = [{"from": f, "relation": r, "to": t} for f, r, t in sorted(properties)]
    _write_split_by_relation(
        kg_dir / "properties",
        prop_rows,
        remove_mixed=[kg_dir / "properties.csv"],
    )

    _write_csv(
        out_dir / "kg_artifact_map.csv",
        ("museum_id", "object_id", "artifact_id"),
        [
            {"museum_id": r["museum_id"], "object_id": r["object_id"], "artifact_id": r["artifact_id"]}
            for r in artifact_rows
        ],
    )

    if export_nt:
        log.warning("[KG] N-Triples 已不再生成")
    stale_nt = out_dir / "kg_triples.nt"
    if stale_nt.exists():
        stale_nt.unlink()

    shared_masters = [m for m in registry.masters.values() if m["entity_type"] != "Artifact"]
    artist_masters = [m for m in shared_masters if m["entity_type"] == "Artist"]
    stats = {
        "entity_count": len(registry.masters),
        "shared_entity_count": len(shared_masters),
        "artist_entity_count": len(artist_masters),
        "artist_with_wikidata": sum(1 for m in artist_masters if m.get("external_id")),
        "relation_count": len(relations),
        "property_count": len(properties),
        "artifact_count": len(artifact_rows),
        **{f"align_{k}": v for k, v in align_stats.items()},
        **{f"dim_{k}": v for k, v in dim_counts.items()},
        **{f"rel_{k}": v for k, v in rel_counts.items()},
    }
    log.info(
        "[KG] 导出完成 → %s  artifacts=%d shared=%d artists(wd)=%d/%d relations=%d",
        kg_dir,
        stats["artifact_count"],
        stats["shared_entity_count"],
        stats["artist_with_wikidata"],
        stats["artist_entity_count"],
        stats["relation_count"],
    )
    return stats
