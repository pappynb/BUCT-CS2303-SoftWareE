import argparse

from neo4j_config import DEFAULT_NEO4J_PASSWORD, DEFAULT_NEO4J_URI, DEFAULT_NEO4J_USER


def get_counts(tx):
    result = tx.run(
        """
        CALL {
            MATCH (n)
            RETURN count(n) AS nodes
        }
        CALL {
            MATCH ()-[r]->()
            RETURN count(r) AS relationships
        }
        RETURN nodes, relationships
        """
    )
    record = result.single()
    return {
        "nodes": record["nodes"],
        "relationships": record["relationships"],
    }

# 批次导入
def delete_batch(tx, batch_size):
    result = tx.run(
        """
        MATCH (n)
        WITH n
        LIMIT $batch_size
        DETACH DELETE n
        RETURN count(n) AS deleted
        """,
        batch_size=batch_size,
    )
    return result.single()["deleted"]


def delete_all(session, batch_size):
    total_deleted = 0
    while True:
        deleted = session.execute_write(delete_batch, batch_size)
        if deleted == 0:
            return total_deleted
        total_deleted += deleted


def main():
    parser = argparse.ArgumentParser(description="Delete all imported graph data from Neo4j.")
    parser.add_argument("--uri", default=DEFAULT_NEO4J_URI)
    parser.add_argument("--user", default=DEFAULT_NEO4J_USER)
    parser.add_argument("--password", default=DEFAULT_NEO4J_PASSWORD)
    parser.add_argument(
        "--database",
        help="Neo4j database name. Defaults to the server's configured default database.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Number of nodes to delete per transaction.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt and delete immediately.",
    )
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
        with driver.session(database=args.database) as session:
            counts = session.execute_read(get_counts)
            print(
                "Current graph: "
                f"nodes={counts['nodes']}, relationships={counts['relationships']}"
            )

            if not args.yes:
                answer = input("Delete all nodes and relationships from the graph? [y/N]: ").strip().lower()
                if answer not in {"y", "yes"}:
                    print("Aborted.")
                    return

            deleted = delete_all(session, args.batch_size)
            print(f"Deleted all nodes and relationships. nodes_deleted={deleted}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
