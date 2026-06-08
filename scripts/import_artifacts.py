import argparse
import csv
import hashlib
from datetime import date
from pathlib import Path

from neo4j_config import DEFAULT_NEO4J_PASSWORD, DEFAULT_NEO4J_URI, DEFAULT_NEO4J_USER


ENTITY_LABEL = "Entity"
DEFAULT_BATCH_SIZE = 200

NODE_FILE_SPECS = {
    "artifacts.csv": ("Artifact", "artifact_id", {"artifact_id": "artifact_id", "museum_id": "museum_id", "object_id": "object_id", "title": "title"}),
    "museums.csv": ("Museum", "museum_id", {"museum": "name"}),
    "dynasties.csv": ("Dynasty", "dynasty_id", {"dynasty": "name"}),
    "artists.csv": ("Artist", "artist_id", {"artist": "name"}),
    "materials.csv": ("Material", "material_id", {"material": "name"}),
    "types.csv": ("ArtifactType", "type_id", {"type_name": "name"}),
    "locations.csv": ("Location", "location_id", {"location": "name"}),
    "cultures.csv": ("Culture", "culture_id", {"culture": "name"}),
}

DEFAULT_NODE_IMPORT_ORDER = [
    "museums.csv",
    "dynasties.csv",
    "artists.csv",
    "materials.csv",
    "types.csv",
    "locations.csv",
    "cultures.csv",
    "artifacts.csv",
]

CONSTRAINTS = [
    "CREATE CONSTRAINT entity_uri IF NOT EXISTS FOR (n:Entity) REQUIRE n.uri IS UNIQUE",
    "CREATE CONSTRAINT artifact_uri IF NOT EXISTS FOR (n:Artifact) REQUIRE n.uri IS UNIQUE",
    "CREATE CONSTRAINT museum_uri IF NOT EXISTS FOR (n:Museum) REQUIRE n.uri IS UNIQUE",
    "CREATE CONSTRAINT dynasty_uri IF NOT EXISTS FOR (n:Dynasty) REQUIRE n.uri IS UNIQUE",
    "CREATE CONSTRAINT artist_uri IF NOT EXISTS FOR (n:Artist) REQUIRE n.uri IS UNIQUE",
    "CREATE CONSTRAINT location_uri IF NOT EXISTS FOR (n:Location) REQUIRE n.uri IS UNIQUE",
    "CREATE CONSTRAINT material_uri IF NOT EXISTS FOR (n:Material) REQUIRE n.uri IS UNIQUE",
    "CREATE CONSTRAINT artifact_type_uri IF NOT EXISTS FOR (n:ArtifactType) REQUIRE n.uri IS UNIQUE",
    "CREATE CONSTRAINT culture_uri IF NOT EXISTS FOR (n:Culture) REQUIRE n.uri IS UNIQUE",
    "CREATE CONSTRAINT source_uri IF NOT EXISTS FOR (n:Source) REQUIRE n.uri IS UNIQUE",
    "CREATE CONSTRAINT entity_alias_uri IF NOT EXISTS FOR (n:EntityAlias) REQUIRE n.uri IS UNIQUE",
    "CREATE CONSTRAINT entity_source_uri IF NOT EXISTS FOR (n:EntitySource) REQUIRE n.uri IS UNIQUE",
]


def slug(value):
    # 生成短字符串
    normalized = (value or "").strip().lower()
    normalized = normalized.replace("&", "and")
    chars = []
    previous_dash = False
    for char in normalized:
        if char.isalnum():
            chars.append(char)
            previous_dash = False
        elif not previous_dash:
            chars.append("-")
            previous_dash = True
    return "".join(chars).strip("-") or "unknown"


def clean(value):
    value = (value or "").strip()
    return value if value else None


def compact_props(mapping):
    return {key: value for key, value in mapping.items() if value not in (None, "")}


def read_csv_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            yield row


def batched(items, size=1000):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch

# csv 的一行转换为 node的节点
def row_to_node_params(row, label, key_field, field_map):
    uri = clean(row.get(key_field))
    if not uri:
        return None

    props = {"uri": uri}
    for source_field, target_field in field_map.items():
        props[target_field] = clean(row.get(source_field))
    return {
        "uri": uri,
        "props": compact_props(props),
        "label": label,
    }


def import_nodes_batch(tx, label, rows):
    tx.run(
        f"""
        UNWIND $rows AS row
        MERGE (n:{ENTITY_LABEL} {{uri: row.uri}})
        SET n:{label}
        SET n += row.props
        SET n.updated_at = datetime()
        """,
        rows=rows,
    )


def import_relations_batch(tx, relation_name, rows):
    tx.run(
        f"""
        UNWIND $rows AS row
        MERGE (from:{ENTITY_LABEL} {{uri: row.from}})
        MERGE (to:{ENTITY_LABEL} {{uri: row.to}})
        MERGE (from)-[r:{relation_name}]->(to)
        SET r.updated_at = datetime()
        """,
        rows=rows,
    )


def import_properties_batch(tx, rows):
    # 为节点新增属性
    tx.run(
        f"""
        UNWIND $rows AS row
        MERGE (a:Artifact {{uri: row.from}})
        SET a[row.key] = row.value
        SET a.updated_at = datetime()
        """,
        rows=rows,
    )


def import_entity_master_batch(tx, rows, label):
    tx.run(
        f"""
        UNWIND $rows AS row
        MERGE (e:{ENTITY_LABEL} {{uri: row.uri}})
        SET e:{label}
        SET e += row.props
        SET e.updated_at = datetime()
        """,
        rows=rows,
    )


def import_entity_alias_batch(tx, rows):
    tx.run(
        f"""
        UNWIND $rows AS row
        MERGE (e:{ENTITY_LABEL} {{uri: row.canonical_id}})
        MERGE (alias:{ENTITY_LABEL}:EntityAlias {{uri: row.alias_uri}})
        SET alias.canonical_id = row.canonical_id,
            alias.alias = row.alias,
            alias.norm_alias = row.norm_alias,
            alias.match_method = row.match_method,
            alias.confidence = row.confidence,
            alias.updated_at = datetime()
        MERGE (e)-[r:HAS_ALIAS]->(alias)
        SET r.updated_at = datetime()
        """,
        rows=rows,
    )


def import_entity_source_batch(tx, rows):
    tx.run(
        f"""
        UNWIND $rows AS row
        MERGE (e:{ENTITY_LABEL} {{uri: row.canonical_id}})
        MERGE (src:{ENTITY_LABEL}:EntitySource {{uri: row.source_uri}})
        SET src.canonical_id = row.canonical_id,
            src.museum_id = row.museum_id,
            src.object_id = row.object_id,
            src.field_name = row.field_name,
            src.raw_value = row.raw_value,
            src.source_file = row.source_file,
            src.updated_at = datetime()
        MERGE (e)-[r:HAS_SOURCE_RECORD]->(src)
        SET r.updated_at = datetime()
        """,
        rows=rows,
    )


def import_image_check_batch(tx, rows):
    # 图片来自哪里
    tx.run(
        f"""
        UNWIND $rows AS row
        MERGE (a:{ENTITY_LABEL} {{uri: row.artifact_uri}})
        SET a.image_http_ok = row.http_ok,
            a.image_status_code = row.status_code,
            a.image_content_type = row.content_type,
            a.image_local_file_ok = row.local_file_ok,
            a.image_valid = row.valid,
            a.image_checked_at = date(row.checked_at),
            a.updated_at = datetime()
        """,
        rows=rows,
    )


def import_artifact_map_batch(tx, rows):
    tx.run(
        f"""
        UNWIND $rows AS row
        MERGE (a:Artifact {{uri: row.artifact_id}})
        SET a.museum_id = row.museum_id,
            a.object_id = row.object_id,
            a.updated_at = datetime()
        """,
        rows=rows,
    )


def sanitize_relation_name(name):
    cleaned = clean(name)
    if not cleaned:
        return None
    allowed = []
    for index, char in enumerate(cleaned):
        if char.isalnum() or char == "_":
            allowed.append(char)
        else:
            allowed.append("_")
    relation = "".join(allowed)
    if relation and relation[0].isdigit():
        relation = f"R_{relation}"
    return relation


def hash_uri(*parts):
    digest = hashlib.sha1("||".join(part or "" for part in parts).encode("utf-8")).hexdigest()
    return f"entity:source:{digest}"


def entity_master_label(entity_type):
    normalized = clean(entity_type) or ""
    mapping = {
        "Artist": "Artist",
        "Dynasty": "Dynasty",
        "Material": "Material",
        "ArtifactType": "ArtifactType",
        "Culture": "Culture",
        "Location": "Location",
        "Museum": "Museum",
        "Source": "Source",
    }
    return mapping.get(normalized, ENTITY_LABEL)


def import_data_directory(driver, data_dir):
    return import_data_directory_with_batch_size(driver, data_dir, DEFAULT_BATCH_SIZE)


def import_data_directory_with_batch_size(driver, data_dir, batch_size):
    data_path = Path(data_dir)
    kg_dir = data_path / "kg"
    clean_dir = data_path / "clean"
    stats = {
        "node_rows": 0,
        "relation_rows": 0,
        "property_rows": 0,
        "alias_rows": 0,
        "source_rows": 0,
        "image_rows": 0,
        "artifact_map_rows": 0,
    }

    with driver.session() as session:
        for statement in CONSTRAINTS:
            session.run(statement)

        for file_name in DEFAULT_NODE_IMPORT_ORDER:
            file_path = kg_dir / file_name
            if not file_path.exists():
                continue
            label, key_field, field_map = NODE_FILE_SPECS[file_name]
            rows = []
            for row in read_csv_rows(file_path):
                row_data = row_to_node_params(row, label, key_field, field_map)
                if row_data:
                    rows.append(row_data)
            for batch in batched(rows, batch_size):
                session.execute_write(import_nodes_batch, label, batch)
            stats["node_rows"] += len(rows)

        entity_master_path = kg_dir / "align" / "entity_master.csv"
        # 内容来源
        if entity_master_path.exists():
            rows = []
            for row in read_csv_rows(entity_master_path):
                canonical_id = clean(row.get("canonical_id"))
                if not canonical_id:
                    continue
                entity_type = clean(row.get("entity_type"))
                props = compact_props(
                    {
                        "uri": canonical_id,
                        "canonical_id": canonical_id,
                        "entity_type": entity_type,
                        "label": clean(row.get("label")),
                        "norm_label": clean(row.get("norm_label")),
                        "external_id": clean(row.get("external_id")),
                        "source_count": int(row["source_count"]) if clean(row.get("source_count")) else None,
                    }
                )
                rows.append({"uri": canonical_id, "props": props, "label": entity_master_label(entity_type)})
            for batch in batched(rows, batch_size):
                by_label = {}
                for row in batch:
                    by_label.setdefault(row["label"], []).append({"uri": row["uri"], "props": row["props"]})
                for label, label_rows in by_label.items():
                    session.execute_write(import_entity_master_batch, label_rows, label)
            stats["node_rows"] += len(rows)

        entity_alias_path = kg_dir / "align" / "entity_alias.csv"
        if entity_alias_path.exists():
            rows = []
            for row in read_csv_rows(entity_alias_path):
                canonical_id = clean(row.get("canonical_id"))
                alias = clean(row.get("alias"))
                if not canonical_id or not alias:
                    continue
                alias_uri = hash_uri(canonical_id, alias, row.get("norm_alias"), row.get("match_method"))
                rows.append(
                    compact_props(
                        {
                            "canonical_id": canonical_id,
                            "alias_uri": alias_uri,
                            "alias": alias,
                            "norm_alias": clean(row.get("norm_alias")),
                            "match_method": clean(row.get("match_method")),
                            "confidence": float(row["confidence"]) if clean(row.get("confidence")) else None,
                        }
                    )
                )
            for batch in batched(rows, batch_size):
                session.execute_write(import_entity_alias_batch, batch)
            stats["alias_rows"] += len(rows)

        # 来源补充信息
        entity_source_path = kg_dir / "align" / "entity_source.csv"
        if entity_source_path.exists():
            rows = []
            for row in read_csv_rows(entity_source_path):
                canonical_id = clean(row.get("canonical_id"))
                if not canonical_id:
                    continue
                source_uri = hash_uri(
                    canonical_id,
                    row.get("museum_id"),
                    row.get("object_id"),
                    row.get("field_name"),
                    row.get("raw_value"),
                    row.get("source_file"),
                )
                rows.append(
                    compact_props(
                        {
                            "canonical_id": canonical_id,
                            "source_uri": source_uri,
                            "museum_id": clean(row.get("museum_id")),
                            "object_id": clean(row.get("object_id")),
                            "field_name": clean(row.get("field_name")),
                            "raw_value": clean(row.get("raw_value")),
                            "source_file": clean(row.get("source_file")),
                        }
                    )
                )
            # 先收集所有的行
            # 然后按照批次导入
            for batch in batched(rows, batch_size):
                session.execute_write(import_entity_source_batch, batch)
            stats["source_rows"] += len(rows)

        for file_path in sorted((kg_dir / "relations").glob("*.csv")):
        # 利用文件名的前缀作为边的名字
            relation_name = sanitize_relation_name(file_path.stem)
            if not relation_name:
                continue
            rows = []
            for row in read_csv_rows(file_path):
                from_uri = clean(row.get("from"))
                to_uri = clean(row.get("to"))
                if from_uri and to_uri:
                    rows.append({"from": from_uri, "to": to_uri})
            for batch in batched(rows, batch_size):
                session.execute_write(import_relations_batch, relation_name, batch)
            stats["relation_rows"] += len(rows)
        # 为节点增加属性字段
        for file_path in sorted((kg_dir / "properties").glob("*.csv")):
            property_name = sanitize_relation_name(file_path.stem)
            if not property_name:
                continue
            rows = []
            for row in read_csv_rows(file_path):
                from_uri = clean(row.get("from"))
                value = clean(row.get("to"))
                if from_uri and value is not None:
                    rows.append({"from": from_uri, "key": property_name, "value": value})
            for batch in batched(rows, batch_size):
                session.execute_write(import_properties_batch, batch)
            stats["property_rows"] += len(rows)

        for file_path in sorted(clean_dir.glob("*.image_check.csv")):
            rows = []
            checked_at = str(date.today())
            for row in read_csv_rows(file_path):
                museum_id = clean(row.get("museum_id"))
                object_id = clean(row.get("object_id"))
                if not museum_id or not object_id:
                    continue
                rows.append(
                    compact_props(
                        {
                            "artifact_uri": f"entity:artifact:{museum_id}:{object_id}",
                            "http_ok": int(row["http_ok"]) if clean(row.get("http_ok")) else None,
                            "status_code": int(row["status_code"]) if clean(row.get("status_code")) else None,
                            "content_type": clean(row.get("content_type")),
                            "local_file_ok": int(row["local_file_ok"]) if clean(row.get("local_file_ok")) else None,
                            "valid": int(row["valid"]) if clean(row.get("valid")) else None,
                            "checked_at": checked_at,
                        }
                    )
                )
            for batch in batched(rows, batch_size):
                session.execute_write(import_image_check_batch, batch)
            stats["image_rows"] += len(rows)

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Import structured data into Neo4j."
    )
    parser.add_argument(
        "--data-dir",
        "--output-dir",
        dest="data_dir",
        help="Import the structured data directory (clean/, kg/, kg_artifact_map.csv).",
        required=True,
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Number of rows to send per transaction (default: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument("--uri", default=DEFAULT_NEO4J_URI)
    parser.add_argument("--user", default=DEFAULT_NEO4J_USER)
    parser.add_argument("--password", default=DEFAULT_NEO4J_PASSWORD)
    args = parser.parse_args()

    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be a positive integer.")

    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: neo4j. Install it with `pip install -r requirements.txt`."
        ) from exc

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        if args.data_dir:
            stats = import_data_directory_with_batch_size(driver, args.data_dir, args.batch_size)
            print(
                "Imported data directory: "
                f"nodes={stats['node_rows']}, relations={stats['relation_rows']}, "
                f"properties={stats['property_rows']}, aliases={stats['alias_rows']}, "
                f"sources={stats['source_rows']}, image_checks={stats['image_rows']}, "
                f"artifact_map={stats['artifact_map_rows']}"
            )
    finally:
        driver.close()


if __name__ == "__main__":
    main()
