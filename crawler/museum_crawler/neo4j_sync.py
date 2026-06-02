# -*- coding: utf-8 -*-
"""
将 ``output/kg/`` CSV 同步到 Neo4j（MERGE 增量写入）。

环境变量（crawler/.env）：
  NEO4J_URI=bolt://47.96.152.190:7687
  NEO4J_USER=neo4j
  NEO4J_PASSWORD=***
"""
from __future__ import annotations

import csv
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("spider")

REL_TYPE_MAP: dict[str, str] = {
    "belongsToMuseum": "BELONGS_TO_MUSEUM",
    "belongsToDynasty": "BELONGS_TO_DYNASTY",
    "createdBy": "CREATED_BY",
    "hasCulture": "HAS_CULTURE",
    "hasPrimaryMaterial": "HAS_PRIMARY_MATERIAL",
    "hasType": "HAS_TYPE",
    "locatedIn": "LOCATED_IN",
    "usesMaterial": "USES_MATERIAL",
}

DIM_FILES: list[tuple[str, str, str, str]] = [
    ("Museum", "museums.csv", "museum_id", "museum"),
    ("Dynasty", "dynasties.csv", "dynasty_id", "dynasty"),
    ("Artist", "artists.csv", "artist_id", "artist"),
    ("Material", "materials.csv", "material_id", "material"),
    ("Location", "locations.csv", "location_id", "location"),
    ("ArtifactType", "types.csv", "type_id", "type_name"),
    ("Culture", "cultures.csv", "culture_id", "culture"),
]

# properties/*.csv 中跳过的文件（title 已在 artifacts.csv 导入时写入）
PROP_SKIP_FILES: frozenset[str] = frozenset({"title"})

# 可选别名：CSV 文件名 → Neo4j 属性名（默认文件名 stem 即属性名）
PROP_ALIASES: dict[str, str] = {}


def label_for_id(entity_id: str) -> str:
    eid = (entity_id or "").strip()
    if eid.startswith("entity:artifact:"):
        return "Artifact"
    if eid.startswith("entity:museum:"):
        return "Museum"
    if eid.startswith("entity:dynasty:"):
        return "Dynasty"
    if eid.startswith("entity:artist:"):
        return "Artist"
    if eid.startswith("entity:material:"):
        return "Material"
    if eid.startswith("entity:location:"):
        return "Location"
    if eid.startswith("entity:artifacttype:"):
        return "ArtifactType"
    if eid.startswith("entity:culture:"):
        return "Culture"
    return "Entity"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class Neo4jKgSync:
    def __init__(self, driver: Any, *, batch_size: int = 500) -> None:
        self._driver = driver
        self._batch_size = batch_size

    def _run(self, cypher: str, params: dict | None = None) -> None:
        with self._driver.session() as session:
            session.run(cypher, params or {})

    def _run_write(self, cypher: str, params: dict | None = None, *, retries: int = 3) -> None:
        import time

        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                with self._driver.session() as session:
                    session.execute_write(lambda tx: tx.run(cypher, params or {}))
                return
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                if "EntityNotFound" in msg or "MemoryPoolOutOfMemory" in msg:
                    wait = 2 ** attempt
                    log.warning("Neo4j 写入重试 %d/%d（%ds）: %s", attempt + 1, retries, wait, msg[:120])
                    time.sleep(wait)
                    continue
                raise
        if last_exc:
            raise last_exc

    def ensure_constraints(self) -> None:
        labels = [
            "Artifact",
            "Museum",
            "Dynasty",
            "Artist",
            "Material",
            "Location",
            "ArtifactType",
            "Culture",
            "Entity",
        ]
        for label in labels:
            cypher = (
                f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.id IS UNIQUE"
            )
            try:
                self._run(cypher)
            except Exception as exc:
                if "EquivalentSchemaRuleAlreadyExists" in str(exc) or "already exists" in str(exc).lower():
                    continue
                log.warning("约束 %s 可能已存在: %s", label, exc)

    def _graph_counts(self) -> tuple[int, int]:
        with self._driver.session() as session:
            rec = session.run(
                """
                MATCH (n) WITH count(n) AS nodes
                MATCH ()-[r]->() WITH nodes, count(r) AS rels
                RETURN nodes, rels
                """
            ).single()
            if not rec:
                return 0, 0
            return int(rec["nodes"]), int(rec["rels"])

    def _delete_batch(self, cypher: str, *, limit: int) -> int:
        with self._driver.session() as session:
            result = session.execute_write(
                lambda tx, c=cypher, lim=limit: tx.run(c, {"limit": lim}).single()
            )
            return int(result["n"]) if result and result.get("n") else 0

    def wipe_graph(self, delete_batch: int = 2000) -> None:
        """先删关系再删节点，分批执行并验证为空。"""
        log.warning("清空 Neo4j 全图（每批 %d）…", delete_batch)
        rel_cypher = """
            MATCH ()-[r]->()
            WITH r LIMIT $limit
            WITH collect(r) AS batch
            UNWIND batch AS rel
            DELETE rel
            RETURN size(batch) AS n
        """
        node_cypher = """
            MATCH (n)
            WITH n LIMIT $limit
            WITH collect(n) AS batch
            UNWIND batch AS node
            DETACH DELETE node
            RETURN size(batch) AS n
        """
        deleted_rels = deleted_nodes = 0
        while True:
            n = self._delete_batch(rel_cypher, limit=delete_batch)
            if n == 0:
                break
            deleted_rels += n
        while True:
            n = self._delete_batch(node_cypher, limit=delete_batch)
            if n == 0:
                break
            deleted_nodes += n
            if deleted_nodes % (delete_batch * 10) < delete_batch:
                log.info("[Neo4j] 已删除节点 %d", deleted_nodes)

        nodes, rels = self._graph_counts()
        if nodes or rels:
            log.warning(
                "[Neo4j] 首轮清空后仍剩 nodes=%d rels=%d，缩小批次重试…",
                nodes,
                rels,
            )
            small = max(100, delete_batch // 4)
            for _ in range(500):
                if rels:
                    self._delete_batch(rel_cypher, limit=small)
                if nodes:
                    self._delete_batch(node_cypher, limit=small)
                nodes, rels = self._graph_counts()
                if nodes == 0 and rels == 0:
                    break
        nodes, rels = self._graph_counts()
        if nodes or rels:
            raise RuntimeError(
                f"Neo4j 未能完全清空（剩余 nodes={nodes}, rels={rels}）。"
                "请在 Neo4j Browser 执行 CALL apoc.periodic.iterate 或调大服务器内存后重试。"
            )
        log.info(
            "[Neo4j] 全图已清空（删关系约 %d，删节点约 %d）",
            deleted_rels,
            deleted_nodes,
        )

    def import_artifacts(self, kg_dir: Path) -> int:
        rows = _read_csv(kg_dir / "artifacts.csv")
        n = 0
        for batch in _chunks(rows, self._batch_size):
            payload = [
                {
                    "id": r["artifact_id"],
                    "museumId": int(r["museum_id"]) if str(r.get("museum_id", "")).isdigit() else None,
                    "objectId": r.get("object_id", ""),
                    "title": r.get("title", ""),
                }
                for r in batch
                if r.get("artifact_id")
            ]
            if not payload:
                continue
            self._run_write(
                """
                UNWIND $rows AS row
                MERGE (a:Artifact {id: row.id})
                SET a.museumId = row.museumId,
                    a.objectId = row.objectId,
                    a.title = row.title
                """,
                {"rows": payload},
            )
            n += len(payload)
        log.info("[Neo4j] Artifact 节点: %d", n)
        return n

    def import_dimensions(self, kg_dir: Path) -> int:
        total = 0
        for label, fname, id_col, name_col in DIM_FILES:
            rows = _read_csv(kg_dir / fname)
            for batch in _chunks(rows, self._batch_size):
                payload = [
                    {"id": r[id_col], "name": r.get(name_col, "")}
                    for r in batch
                    if r.get(id_col)
                ]
                if not payload:
                    continue
                self._run_write(
                    f"""
                    UNWIND $rows AS row
                    MERGE (n:{label} {{id: row.id}})
                    SET n.name = row.name
                    """,
                    {"rows": payload},
                )
                total += len(payload)
            log.info("[Neo4j] %s: %d", label, len(rows))
        return total

    def import_relations(self, kg_dir: Path) -> int:
        rel_dir = kg_dir / "relations"
        if not rel_dir.is_dir():
            return 0
        total = 0
        for csv_path in sorted(rel_dir.glob("*.csv")):
            rel_key = csv_path.stem
            rel_type = REL_TYPE_MAP.get(rel_key, rel_key.upper())
            rows = _read_csv(csv_path)
            count = 0
            for batch in _chunks(rows, self._batch_size):
                payload = [
                    {
                        "fromId": r["from"],
                        "toId": r["to"],
                        "fromLabel": label_for_id(r["from"]),
                        "toLabel": label_for_id(r["to"]),
                    }
                    for r in batch
                    if r.get("from") and r.get("to")
                ]
                if not payload:
                    continue
                # 动态 label 的 MERGE 需分类型；用 APOC 替代较复杂，这里用通用 Entity 兜底 + 按批分组
                by_pair: dict[tuple[str, str], list[dict]] = {}
                for item in payload:
                    key = (item["fromLabel"], item["toLabel"])
                    by_pair.setdefault(key, []).append(item)
                for (fl, tl), items in by_pair.items():
                    cypher = f"""
                    UNWIND $rows AS row
                    MERGE (a:{fl} {{id: row.fromId}})
                    MERGE (b:{tl} {{id: row.toId}})
                    MERGE (a)-[:{rel_type}]->(b)
                    """
                    for sub in _chunks(items, min(100, self._batch_size)):
                        try:
                            self._run_write(cypher, {"rows": sub})
                        except Exception as exc:
                            if "EntityNotFound" not in str(exc) or len(sub) <= 1:
                                raise
                            log.warning(
                                "[Neo4j] 关系 %s 批次失败，逐条重试 (%d 条)",
                                rel_key,
                                len(sub),
                            )
                            for one in sub:
                                self._run_write(cypher, {"rows": [one]})
                count += len(payload)
            log.info("[Neo4j] 关系 %s (%s): %d", rel_key, rel_type, count)
            total += count
        return total

    def _iter_property_files(self, prop_dir: Path) -> list[tuple[str, str]]:
        """扫描 properties/*.csv，文件名（stem）即 Neo4j 属性名。"""
        items: list[tuple[str, str]] = []
        for csv_path in sorted(prop_dir.glob("*.csv")):
            fname = csv_path.stem
            if fname in PROP_SKIP_FILES:
                continue
            prop_key = PROP_ALIASES.get(fname, fname)
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", prop_key):
                log.warning("[Neo4j] 跳过非法属性文件名: %s", csv_path.name)
                continue
            items.append((fname, prop_key))
        return items

    def import_properties(self, kg_dir: Path) -> int:
        prop_dir = kg_dir / "properties"
        if not prop_dir.is_dir():
            log.warning("[Neo4j] 未找到 properties 目录: %s", prop_dir)
            return 0
        prop_files = self._iter_property_files(prop_dir)
        if not prop_files:
            log.warning("[Neo4j] properties 目录下无可导入 CSV")
            return 0
        total = 0
        # 长文本属性用小批次，降低 Neo4j 内存压力
        heavy_props = frozenset({"description", "bibliography", "provenance", "artistBio", "artistWikipediaSummary"})
        for fname, prop_key in prop_files:
            rows = _read_csv(prop_dir / f"{fname}.csv")
            if not rows:
                continue
            batch_size = min(100, self._batch_size) if prop_key in heavy_props else self._batch_size
            count = 0
            for batch in _chunks(rows, batch_size):
                payload = [
                    {"id": r["from"], "value": r.get("to", "")[:8000]}
                    for r in batch
                    if r.get("from") and r.get("to")
                ]
                if not payload:
                    continue
                self._run_write(
                    f"""
                    UNWIND $rows AS row
                    MATCH (a:Artifact {{id: row.id}})
                    SET a.`{prop_key}` = row.value
                    """,
                    {"rows": payload},
                )
                count += len(payload)
            if count:
                log.info("[Neo4j] 属性 %s (%s.csv): %d", prop_key, fname, count)
            else:
                log.warning("[Neo4j] 属性 %s (%s.csv): 0 条（可能 Artifact 节点尚未导入）", prop_key, fname)
            total += count
        log.info("[Neo4j] 属性合计写入: %d 条（%d 个文件）", total, len(prop_files))
        return total

    def import_entity_master(self, kg_dir: Path) -> int:
        rows = _read_csv(kg_dir / "align" / "entity_master.csv")
        if not rows:
            return 0
        count = 0
        for batch in _chunks(rows, self._batch_size):
            payload = [
                {
                    "id": r["canonical_id"],
                    "label": label_for_id(r["canonical_id"]),
                    "name": r.get("label", ""),
                    "uri": r.get("uri", ""),
                    "entityType": r.get("entity_type", ""),
                }
                for r in batch
                if r.get("canonical_id")
            ]
            if not payload:
                continue
            by_label: dict[str, list[dict]] = {}
            for item in payload:
                by_label.setdefault(item["label"], []).append(item)
            for lbl, items in by_label.items():
                self._run_write(
                    f"""
                    UNWIND $rows AS row
                    MERGE (n:{lbl} {{id: row.id}})
                    SET n.name = coalesce(n.name, row.name),
                        n.uri = row.uri,
                        n.entityType = row.entityType
                    """,
                    {"rows": items},
                )
            count += len(payload)
        log.info("[Neo4j] entity_master 补充: %d", count)
        return count

    def stats(self) -> dict[str, int]:
        with self._driver.session() as session:
            rec = session.run(
                """
                MATCH (a:Artifact) WITH count(a) AS artifacts
                MATCH ()-[r]->() WITH artifacts, count(r) AS rels
                RETURN artifacts, rels
                """
            ).single()
            if rec:
                return {"artifacts": rec["artifacts"], "relationships": rec["rels"]}
        return {}


def neo4j_configured() -> bool:
    return bool(os.environ.get("NEO4J_URI", "").strip() and os.environ.get("NEO4J_PASSWORD", "").strip())


def connect_neo4j():
    from neo4j import GraphDatabase

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687").strip()
    user = os.environ.get("NEO4J_USER", "neo4j").strip()
    password = os.environ.get("NEO4J_PASSWORD", "")
    return GraphDatabase.driver(uri, auth=(user, password))


def sync_kg_to_neo4j(
    kg_dir: Path,
    *,
    wipe: bool = False,
    skip_properties: bool = False,
    properties_only: bool = False,
    batch_size: int = 500,
    wipe_batch_size: int = 2000,
) -> dict[str, int]:
    driver = connect_neo4j()
    try:
        driver.verify_connectivity()
        log.info("[Neo4j] 连接成功: %s", os.environ.get("NEO4J_URI"))
    except Exception as exc:
        driver.close()
        raise RuntimeError(f"Neo4j 连接失败: {exc}") from exc

    sync = Neo4jKgSync(driver, batch_size=batch_size)
    sync.ensure_constraints()
    if wipe:
        sync.wipe_graph(delete_batch=wipe_batch_size)

    if properties_only:
        result: dict[str, int] = {"properties": sync.import_properties(kg_dir)}
        result.update(sync.stats())
        driver.close()
        return result

    result = {
        "artifacts": sync.import_artifacts(kg_dir),
        "dimensions": sync.import_dimensions(kg_dir),
        "entity_master": sync.import_entity_master(kg_dir),
        "relations": sync.import_relations(kg_dir),
    }
    if not skip_properties:
        result["properties"] = sync.import_properties(kg_dir)
    result.update(sync.stats())
    driver.close()
    return result
