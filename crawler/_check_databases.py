# -*- coding: utf-8 -*-
"""检查 MySQL 与 Neo4j 数据是否还在。"""
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

print("=" * 60)
print("MySQL")
print("=" * 60)
try:
    from museum_crawler.db import MySQLWriter
    w = MySQLWriter.from_env()
    conn = w._connect()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT COUNT(*) AS c FROM artifact")
    total = cur.fetchone()["c"]
    print(f"artifact 总行数: {total}")
    cur.execute(
        "SELECT museum_id, COUNT(*) AS c FROM artifact GROUP BY museum_id ORDER BY museum_id"
    )
    for r in cur.fetchall():
        names = {1: "史密森尼", 2: "哈佛", 3: "MFA"}
        print(f"  museum_id={r['museum_id']} ({names.get(r['museum_id'], '?')}): {r['c']} 条")
    cur.execute(
        "SELECT object_id, title FROM artifact ORDER BY museum_id, object_id LIMIT 3"
    )
    print("  样例:", [f"{r['object_id']}: {r['title'][:30]}" for r in cur.fetchall()])
    cur.close()
    conn.close()
except Exception as e:
    print(f"MySQL 失败: {e}")

print()
print("=" * 60)
print("Neo4j")
print("=" * 60)
try:
    import os
    from neo4j import GraphDatabase
    uri = os.environ.get("NEO4J_URI", "")
    user = os.environ.get("NEO4J_USER", "neo4j")
    pwd = os.environ.get("NEO4J_PASSWORD", "")
    driver = GraphDatabase.driver(uri, auth=(user, pwd))
    driver.verify_connectivity()
    with driver.session() as s:
        rec = s.run(
            """
            MATCH (a:Artifact) WITH count(a) AS artifacts
            MATCH (n) WITH artifacts, count(n) AS nodes
            MATCH ()-[r]->() WITH artifacts, nodes, count(r) AS rels
            RETURN artifacts, nodes, rels
            """
        ).single()
        print(f"Artifact 节点: {rec['artifacts']}")
        print(f"全部节点: {rec['nodes']}")
        print(f"全部关系: {rec['rels']}")
        labels = s.run(
            "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS c ORDER BY c DESC LIMIT 10"
        ).data()
        print("  节点分布:", labels)
        sample = s.run(
            "MATCH (a:Artifact) RETURN a.id AS id, a.title AS title LIMIT 3"
        ).data()
        print("  样例:", sample)
    driver.close()
except Exception as e:
    print(f"Neo4j 失败: {e}")
