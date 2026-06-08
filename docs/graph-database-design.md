# 海外藏中国文物图数据库设计与导入约定

## 1. 设计目标

图数据库用于保存文物知识图谱，支持关系遍历、知识问答、实体检索和可视化展示。

当前仓库只保留 `data/` 结构化导入：它包含节点、关系、属性、对齐表和图片校验结果，全部按文件批量写入 Neo4j。

## 2. 节点类型

`data/` 导入使用 `Entity` 作为统一基类标签，实际会写入以下标签：

| 标签 | 含义 | 说明 |
| --- | --- | --- |
| Artifact | 文物 | 来自 `kg/artifacts.csv` |
| Museum | 博物馆 | 来自 `kg/museums.csv` |
| Dynasty | 朝代 | 来自 `kg/dynasties.csv` |
| Artist | 艺术家 | 来自 `kg/artists.csv` |
| Material | 材质 | 来自 `kg/materials.csv` |
| ArtifactType | 文物类型 | 来自 `kg/types.csv` |
| Location | 地点 | 来自 `kg/locations.csv` |
| Culture | 文化标签 | 来自 `kg/cultures.csv` |
| Source | 数据来源 | 来自对齐或溯源相关节点 |
| EntityAlias | 实体别名 | 对齐表里的别名节点 |
| EntitySource | 实体溯源 | 对齐表里的来源节点 |

## 3. 关系类型

`data/kg/relations/*.csv` 里实际导出的实体关系如下：

| 文件 | 关系名 | 含义 |
| --- | --- | --- |
| `belongsToMuseum.csv` | `belongsToMuseum` | 文物收藏于博物馆 |
| `belongsToDynasty.csv` | `belongsToDynasty` | 文物属于某朝代/时期 |
| `createdBy.csv` | `createdBy` | 文物由某艺术家创作 |
| `usesMaterial.csv` | `usesMaterial` | 文物使用了哪些材质 |
| `hasPrimaryMaterial.csv` | `hasPrimaryMaterial` | 文物主材质 |
| `hasType.csv` | `hasType` | 文物类型 |
| `hasCulture.csv` | `hasCulture` | 文物关联的文化/分类标签 |
| `locatedIn.csv` | `locatedIn` | 文物现藏地点 |

`data/` 导入的实体关系来自 `kg/relations/*.csv`，字面量属性来自 `kg/properties/*.csv`，它们会按文件名批量写入 Neo4j。对齐溯源关系则写成 `HAS_SOURCE_RECORD`，对应 `kg/align/entity_source.csv`。

## 4. URI 规则

`data/` 目录里的图谱文件使用已经导出的 `uri` 字段，不再重新拼接。

`kg_artifact_map.csv` 保存业务主键和图谱文物 ID 的映射：

```text
artifact_id,museum_id,object_id
```

图片校验文件会回写到：

```text
entity:artifact:{museum_id}:{object_id}
```

## 5. 导入流程

`data/` 导入会先创建唯一约束，再批量写入：

1. 节点维表
2. `align/entity_master.csv`
3. `align/entity_alias.csv`
4. `align/entity_source.csv`
5. `kg_artifact_map.csv`
6. `kg/relations/*.csv`
7. `kg/properties/*.csv`
8. `clean/*.image_check.csv`

## 6. 示例查询

统计各博物馆文物数量：

```cypher
MATCH (a:Artifact)-[:COLLECTED_BY]->(m:Museum)
RETURN m.name AS museum, count(a) AS artifact_count
ORDER BY artifact_count DESC;
```

查询明代文物：

```cypher
MATCH (a:Artifact)-[:BELONGS_TO_DYNASTY]->(d:Dynasty {name: "Ming"})
RETURN a.name, a.period_text, a.detail_url;
```

查询文物周边关系：

```cypher
MATCH path = (a:Artifact)-[r]-(n)
RETURN path
LIMIT 50;
```
