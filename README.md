# 海外藏中国文物 Neo4j 图数据库

这个仓库提供“海外藏中国文物”项目的图数据库层，基于 Neo4j 存储文物知识图谱，并配套：

- `data/` 目录的一键导入
- Neo4j Browser 查询示例
- 图谱清空脚本

如果导入时遇到 `dbms.memory.transaction.total.max threshold reached`，先把批大小调小，例如 `--batch-size 100`。

## 目录

```text
graph-db/
  requirements.txt
  docs/graph-database-design.md
  scripts/
    neo4j_config.py
    import_artifacts.py
    queries.cypher
    reset_graph.py
  data/
```

## 环境要求

- Python 3
- 可访问项目组 Neo4j 服务器（脚本已默认配置）

## 1. Neo4j 连接

脚本默认连接远程 Neo4j：

```text
Bolt:    bolt://47.96.152.190:7687
Browser: http://47.96.152.190:7474
用户名:  neo4j
```

连接参数在 `scripts/neo4j_config.py`，也可用环境变量覆盖：`NEO4J_URI`、`NEO4J_USER`、`NEO4J_PASSWORD`。

## 2. 安装 Python 依赖

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3. 导入图谱

`scripts/import_artifacts.py` 导入结构化 `data/` 目录（节点与关系在 `data/kg/` 下）：

```bash
python scripts/import_artifacts.py --data-dir data
```

如果服务器内存比较紧张，可以降低单次事务写入量：

```bash
python scripts/import_artifacts.py --data-dir data --batch-size 100
```

这会加载：

- `clean/` 下的清洗后主表（若有）
- `kg/` 下的节点、关系、属性、对齐表
- `kg_artifact_map.csv`（若有）

导入完成后，脚本会打印节点、关系、属性和映射表的导入统计。

## 4. 查询图谱

在 Neo4j Browser（http://47.96.152.190:7474）中，将 `scripts/queries.cypher` 里的语句复制执行。

常用示例包括：

- 按博物馆统计文物数量
- 查询某朝代文物
- 查询器物和其收藏博物馆
- 展示以文物为中心的局部子图
- 查询作者及其作品

也可直接写 Cypher，例如：

```cypher
MATCH (a:Artifact)-[:belongsToMuseum]->(m:Museum)
RETURN m.name AS museum, count(a) AS artifact_count
ORDER BY artifact_count DESC;
```

> 关系名以 `data/kg/relations/` 中文件名为准（camelCase，如 `belongsToMuseum`）。

## 5. 一键清空图谱

```bash
python scripts/reset_graph.py
```

跳过确认：

```bash
python scripts/reset_graph.py --yes
```

## 6. URI 约定

`data/` 目录里的图谱文件都使用导出时已经写好的 `uri` 字段，导入脚本不会重新拼接 URI。

`kg_artifact_map.csv` 保存业务主键和图谱文物 ID 的映射，方便从 `(museum_id, object_id)` 回查 `Artifact.uri`。

## 7. 设计说明

- [`docs/graph-database-design.md`](docs/graph-database-design.md)
