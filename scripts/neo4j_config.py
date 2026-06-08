import os

# 默认连接远程 Neo4j 服务器；仍可通过环境变量或命令行参数覆盖
DEFAULT_NEO4J_URI = os.getenv("NEO4J_URI", "bolt://47.96.152.190:7687")
DEFAULT_NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
DEFAULT_NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "!software2303")
