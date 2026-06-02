# -*- coding: utf-8 -*-
"""
MySQL 持久化：连接配置来自环境变量，便于部署时替换密码等敏感项。

表约定
------
- 主键：``(museum_id, object_id)``。``museum_id`` 为数值馆别（1/2/3），与 CSV 一致，
  避免不同博物馆的 ``object_id`` 字符串重复导致合并表冲突。
- 若表不存在，可选 ``ensure_table()`` 创建与 ``CSV_FIELDS`` 对齐的列。
- 若表为旧版仅有 ``object_id`` 主键、无 ``museum_id``，启动写库时会尝试
  ``ensure_museum_id_schema()`` 自动补列并升级主键（默认旧行 ``museum_id=1``）。
- 若表曾使用误拼列名 ``author_province``，会重命名为 ``artist_province``（与 CSV 英文列一致）。
- 若表缺 ``artist`` 等列（与 CSV 演进不一致），会 ``ensure_missing_csv_columns()``
  按 ``CSV_FIELDS`` 自动 ``ALTER TABLE`` 补列。
- ``material`` / ``image_path`` / 长 URL 列过窄时，由 ``ensure_loosen_overflow_prone_columns()``
  将易触发 1406 的 VARCHAR 改为 ``TEXT``。

环境变量（均在 ``.env`` 中配置即可）
----------------------------------
MYSQL_HOST          必填（非空则启用写库）
MYSQL_PORT          默认 3306
MYSQL_USER          默认 root
MYSQL_PASSWORD      默认空（请后续改为你的密码）
MYSQL_DATABASE      必填
MYSQL_TABLE         默认 artifact
MYSQL_CHARSET       默认 utf8mb4
"""

from __future__ import annotations

import sys
from pathlib import Path

# 直接 ``python museum_crawler/db.py`` 时，须把上级 ``crawler/`` 加入 path 才能 ``import museum_crawler.*``
# 推荐仍从 ``crawler`` 目录运行：``python museum_spider.py`` 或 ``python -m museum_crawler.cli``
if __package__ in (None, ""):
    _crawler_dir = Path(__file__).resolve().parent.parent
    if str(_crawler_dir) not in sys.path:
        sys.path.insert(0, str(_crawler_dir))

import logging
import os
from datetime import date, datetime
from typing import Any, Optional

from museum_crawler.config import CSV_FIELDS

log = logging.getLogger("spider")

# 与 ``CSV_FIELDS`` 键集合必须一致；用于 CREATE TABLE 与旧表 ALTER 补列
ARTIFACT_COL_DDL: dict[str, str] = {
    "object_id": "VARCHAR(255) NOT NULL",
    "museum_id": "INT NOT NULL",
    "title": "VARCHAR(500) NOT NULL",
    "artist": "VARCHAR(500)",
    "artist_province": "VARCHAR(100)",
    "dynasty": "VARCHAR(200)",
    "artist_wikidata_id": "VARCHAR(32) NOT NULL DEFAULT ''",
    "artist_birth": "VARCHAR(120) NOT NULL DEFAULT ''",
    "artist_death": "VARCHAR(120) NOT NULL DEFAULT ''",
    "artist_bio": "VARCHAR(4000) NOT NULL DEFAULT ''",
    "artist_wikipedia_summary": "VARCHAR(4000) NOT NULL DEFAULT ''",
    "artist_enriched_at": "VARCHAR(32) NOT NULL DEFAULT ''",
    "period": "VARCHAR(200) NOT NULL",
    "period_start_year": "SMALLINT",
    "period_end_year": "SMALLINT",
    "type": "VARCHAR(100) NOT NULL",
    "material": "TEXT",
    "culture": "VARCHAR(300)",
    "description": "TEXT NOT NULL",
    "provenance": "TEXT",
    "bibliography": "TEXT",
    "dimensions": "TEXT",
    "museum": "VARCHAR(300) NOT NULL",
    "location": "VARCHAR(300) NOT NULL",
    "detail_url": "TEXT NOT NULL",
    "image_url": "TEXT NOT NULL",
    "image_urls": "TEXT",
    "iiif_manifest_url": "TEXT",
    "image_path": "TEXT NOT NULL",
    "image_paths": "TEXT",
    "image_count": "SMALLINT NOT NULL DEFAULT 0",
    "credit_line": "VARCHAR(500)",
    "accession_number": "VARCHAR(200)",
    "crawl_date": "DATE NOT NULL",
}
if set(ARTIFACT_COL_DDL) != set(CSV_FIELDS):
    raise RuntimeError(
        "db.ARTIFACT_COL_DDL 与 config.CSV_FIELDS 键集合不一致，请同步两处定义。"
    )


def mysql_configured() -> bool:
    """是否已配置主机与库名（用于自动启用写库）。"""
    host = os.environ.get("MYSQL_HOST", "").strip()
    db = os.environ.get("MYSQL_DATABASE", "").strip()
    return bool(host and db)  # 二者缺一不可，避免连到半空配置


class MySQLWriter:
    """
    轻量封装：每次批量 UPSERT 时建立短连接，避免长连接在爬虫中断线后状态异常。

    生产环境可改为连接池（如 SQLAlchemy）；当前实现优先简单可靠。
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        table: str = "artifact",
        charset: str = "utf8mb4",
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database
        self._table = table
        self._charset = charset

    @classmethod
    def from_env(cls) -> "MySQLWriter":
        """从环境变量构造；缺省项使用代码内默认值。"""
        return cls(
            host=os.environ["MYSQL_HOST"].strip(),
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            user=os.environ.get("MYSQL_USER", "root").strip(),
            password=os.environ.get("MYSQL_PASSWORD", ""),
            database=os.environ["MYSQL_DATABASE"].strip(),
            table=os.environ.get("MYSQL_TABLE", "artifact").strip() or "artifact",
            charset=os.environ.get("MYSQL_CHARSET", "utf8mb4").strip(),
        )

    def _connect(self) -> Any:
        import mysql.connector  # 延迟导入：未装驱动时仍可只跑 CSV

        return mysql.connector.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            database=self._database,
            charset=self._charset,
            collation="utf8mb4_unicode_ci",
            autocommit=True,  # 每条 UPSERT 立即提交，中断时少丢已写批次
        )

    def ensure_table(self) -> None:
        """
        若尚未建表，创建与爬虫 CSV 列一致的 ``artifact`` 表（可改表名环境变量）。

        若你已在库中建好表且结构不同，请勿调用本方法，或调整表结构与之对齐。
        """
        cols_sql = ",\n  ".join(f"`{c}` {ARTIFACT_COL_DDL[c]}" for c in CSV_FIELDS)
        ddl = f"""
CREATE TABLE IF NOT EXISTS `{self._table}` (
  {cols_sql},
  PRIMARY KEY (`museum_id`, `object_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""
        # 整型馆别 + 馆方编号：无需前缀索引，跨馆唯一性清晰
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(ddl)
            cur.close()
            log.info("MySQL 表已就绪: %s.%s", self._database, self._table)
        finally:
            conn.close()

    def ensure_museum_id_schema(self) -> None:
        """
        兼容旧库：表已存在但无 ``museum_id`` 或主键仍为单列 ``object_id`` 时，自动
        ``ADD COLUMN`` 并尽量将主键改为 ``(museum_id, object_id)``。

        新列默认 ``museum_id=1``（史密森尼）；若表中实为哈佛/MFA 旧数据，请自行
        ``UPDATE ... SET museum_id=2`` 等后再跑爬虫，避免馆别错误。

        若主键不是仅 ``object_id``（例如另有自增 id），则只补列并打 WARNING，避免误删主键。
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """,
                (self._database, self._table),
            )
            if cur.fetchone()[0] == 0:
                return
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = 'museum_id'
                """,
                (self._database, self._table),
            )
            if cur.fetchone()[0] > 0:
                return

            log.info(
                "检测到表 `%s`.`%s` 缺少 museum_id，正在执行结构升级…",
                self._database,
                self._table,
            )
            cur.execute(
                f"ALTER TABLE `{self._table}` "
                f"ADD COLUMN `museum_id` INT NOT NULL DEFAULT 1 COMMENT '1=SI 2=HAM 3=MFA' "
                f"AFTER `object_id`"
            )

            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                  AND CONSTRAINT_NAME = 'PRIMARY'
                ORDER BY ORDINAL_POSITION
                """,
                (self._database, self._table),
            )
            pk_cols = [str(r[0]) for r in cur.fetchall()]
            pk_lower = [c.lower() for c in pk_cols]
            if pk_lower == ["object_id"]:
                cur.execute(f"ALTER TABLE `{self._table}` DROP PRIMARY KEY")
                cur.execute(
                    f"ALTER TABLE `{self._table}` "
                    f"ADD PRIMARY KEY (`museum_id`, `object_id`)"
                )
                log.info(
                    "主键已升级为 (`museum_id`, `object_id`)，旧行默认 museum_id=1，请按需 UPDATE"
                )
            elif "museum_id" in pk_lower and "object_id" in pk_lower:
                pass
            else:
                log.warning(
                    "已添加 museum_id，当前主键列为 %s；请手动改为 (museum_id, object_id) 以匹配 UPSERT。",
                    pk_cols,
                )
            cur.close()
        finally:
            conn.close()

    def ensure_legacy_author_province_renamed(self) -> None:
        """
        旧库误用 ``author_province`` 时，重命名为程序与 CSV 使用的 ``artist_province``。
        若两列同时存在则只打 WARNING，避免误删数据。
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """,
                (self._database, self._table),
            )
            if cur.fetchone()[0] == 0:
                cur.close()
                return

            def _has_column(name: str) -> bool:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
                    """,
                    (self._database, self._table, name),
                )
                return cur.fetchone()[0] > 0

            has_old = _has_column("author_province")
            has_new = _has_column("artist_province")
            if has_old and not has_new:
                cur.execute(
                    f"ALTER TABLE `{self._table}` "
                    f"CHANGE COLUMN `author_province` `artist_province` VARCHAR(100)"
                )
                log.info(
                    "MySQL 列已重命名: `%s`.`author_province` → `artist_province`",
                    self._table,
                )
            elif has_old and has_new:
                log.warning(
                    "表 `%s` 同时存在 author_province 与 artist_province，请合并数据后手动删旧列。",
                    self._table,
                )
            cur.close()
        finally:
            conn.close()

    def ensure_missing_csv_columns(self) -> None:
        """
        旧表列少于当前 ``CSV_FIELDS`` 时（如缺 ``artist``、``dynasty``），逐个
        ``ALTER TABLE ... ADD COLUMN``，与 UPSERT 字段列表对齐。
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """,
                (self._database, self._table),
            )
            if cur.fetchone()[0] == 0:
                cur.close()
                return
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """,
                (self._database, self._table),
            )
            existing = {str(r[0]) for r in cur.fetchall()}
            for i, col in enumerate(CSV_FIELDS):
                if col in existing:
                    continue
                ddl = ARTIFACT_COL_DDL[col]
                prev: Optional[str] = None
                for j in range(i - 1, -1, -1):
                    p = CSV_FIELDS[j]
                    if p in existing:
                        prev = p
                        break
                after_sql = f" AFTER `{prev}`" if prev else ""
                try:
                    cur.execute(
                        f"ALTER TABLE `{self._table}` ADD COLUMN `{col}` {ddl}{after_sql}"
                    )
                    log.info("MySQL 表 `%s` 已增加列 `%s`", self._table, col)
                    existing.add(col)
                except Exception as exc:
                    log.error(
                        "MySQL 无法增加列 `%s`（请检查权限或手工 ALTER）: %s",
                        col,
                        exc,
                    )
            cur.close()
        finally:
            conn.close()

    def ensure_loosen_overflow_prone_columns(self) -> None:
        """
        将易超长列（``material``、``image_path``、``image_url``、``detail_url``）在仍为
        短 VARCHAR 时改为 ``TEXT``，避免 1406 Data too long。
        """
        cols = (
            "material", "image_path", "image_paths", "image_url", "image_urls",
            "detail_url", "dimensions", "credit_line",
            "provenance", "bibliography", "iiif_manifest_url", "description",
        )
        conn = self._connect()
        try:
            cur = conn.cursor()
            for column in cols:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
                    """,
                    (self._database, self._table, column),
                )
                if cur.fetchone()[0] == 0:
                    continue
                cur.execute(
                    """
                    SELECT DATA_TYPE, COALESCE(CHARACTER_MAXIMUM_LENGTH, 0)
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
                    """,
                    (self._database, self._table, column),
                )
                row = cur.fetchone()
                if not row:
                    continue
                dt = (row[0] or "").lower()
                maxlen = int(row[1] or 0)
                if dt in ("tinytext", "text", "mediumtext", "longtext"):
                    continue
                if dt == "varchar" and maxlen >= 16000:
                    continue
                try:
                    cur.execute(
                        f"ALTER TABLE `{self._table}` MODIFY COLUMN `{column}` TEXT"
                    )
                    log.info(
                        "MySQL 列 `%s` 已改为 TEXT（原类型 %s，避免 Data too long）",
                        column,
                        row[0],
                    )
                except Exception as exc:
                    log.warning("MySQL 放宽列 `%s` 失败: %s", column, exc)
            cur.close()
        finally:
            conn.close()

    def ensure_loosen_material_column(self) -> None:
        """兼容旧名：等价于 ``ensure_loosen_overflow_prone_columns()``。"""
        self.ensure_loosen_overflow_prone_columns()

    def ensure_artist_enrichment_columns(self) -> None:
        """兼容旧名：等价于 ``ensure_missing_csv_columns()``。"""
        self.ensure_missing_csv_columns()

    def ensure_artifact_id_column(self) -> None:
        """若表存在且无 ``artifact_id``，在 ``object_id`` 后增加图谱 ID 列。"""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """,
                (self._database, self._table),
            )
            if cur.fetchone()[0] == 0:
                cur.close()
                return
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = 'artifact_id'
                """,
                (self._database, self._table),
            )
            if cur.fetchone()[0] > 0:
                cur.close()
                return
            cur.execute(
                f"""
                ALTER TABLE `{self._table}`
                ADD COLUMN `artifact_id` VARCHAR(255) NOT NULL DEFAULT ''
                AFTER `object_id`
                """
            )
            log.info("MySQL 表 `%s` 已增加列 `artifact_id`", self._table)
            cur.close()
        finally:
            conn.close()

    def backfill_artifact_ids(
        self,
        mappings: list[tuple[int, str, str]],
        *,
        chunk_size: int = 200,
    ) -> tuple[int, int]:
        """
        按 ``(museum_id, object_id, artifact_id)`` 批量 UPDATE 已有行。

        返回 (映射条数, 实际更新行数)。
        """
        if not mappings:
            return 0, 0
        sql = (
            f"UPDATE `{self._table}` SET `artifact_id` = %s "
            f"WHERE `museum_id` = %s AND `object_id` = %s"
        )
        updated = 0
        conn = self._connect()
        try:
            cur = conn.cursor()
            for i in range(0, len(mappings), chunk_size):
                batch = mappings[i : i + chunk_size]
                cur.executemany(
                    sql,
                    [(aid, mid, oid) for mid, oid, aid in batch],
                )
                updated += cur.rowcount
            cur.close()
        finally:
            conn.close()
        return len(mappings), updated

    @staticmethod
    def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in CSV_FIELDS:
            v = row.get(k, "")
            out[k] = "" if v is None else v

        # 馆别编号：CSV 里多为 "1"/"2" 字符串，入库需 Python int
        try:
            mid = out.get("museum_id", "")
            out["museum_id"] = int(mid) if str(mid).strip() != "" else 0
        except (ValueError, TypeError):
            out["museum_id"] = 0

        for year_col in ("period_start_year", "period_end_year"):
            raw_y = str(out.get(year_col, "") or "").strip()
            if not raw_y:
                out[year_col] = None
            else:
                try:
                    out[year_col] = int(raw_y)
                except ValueError:
                    out[year_col] = None

        raw_ic = str(out.get("image_count", "") or "").strip()
        try:
            out["image_count"] = int(raw_ic) if raw_ic else 0
        except ValueError:
            out["image_count"] = 0

        for k in CSV_FIELDS:
            if k in ("crawl_date", "museum_id", "period_start_year", "period_end_year"):
                continue
            if not isinstance(out[k], str):
                out[k] = str(out[k])
        cd = out.get("crawl_date")
        if isinstance(cd, date) and not isinstance(cd, datetime):
            pass  # 已是 date 对象，直接用于 executemany
        elif isinstance(cd, str) and cd.strip():
            try:
                out["crawl_date"] = datetime.strptime(cd.strip()[:10], "%Y-%m-%d").date()
            except ValueError:
                out["crawl_date"] = date.today()
        else:
            out["crawl_date"] = date.today()  # 缺省则用当天，满足 DATE NOT NULL
        mid = out.get("museum_id") or 0
        oid = str(out.get("object_id") or "").strip()
        out["artifact_id"] = f"entity:artifact:{mid}:{oid}" if oid else ""
        return out

    def _table_column_names(self) -> set[str]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """,
                (self._database, self._table),
            )
            names = {str(r[0]) for r in cur.fetchall()}
            cur.close()
            return names
        finally:
            conn.close()

    def _upsert_columns(self) -> list[str]:
        table_cols = self._table_column_names()
        cols = list(CSV_FIELDS)
        if "artifact_id" in table_cols and "artifact_id" not in cols:
            cols.insert(cols.index("object_id") + 1, "artifact_id")
        return cols

    def upsert_batch(self, rows: list[dict[str, Any]]) -> None:
        """批量 INSERT ... ON DUPLICATE KEY UPDATE。"""
        if not rows:
            return
        cols = self._upsert_columns()
        placeholders = ", ".join(["%s"] * len(cols))
        col_list = ", ".join(f"`{c}`" for c in cols)
        # 主键列不参与 UPDATE，避免无意义自写
        update_parts = [
            f"`{c}`=VALUES(`{c}`)" for c in cols
            if c not in ("museum_id", "object_id")
        ]
        update_sql = ", ".join(update_parts)
        sql = (
            f"INSERT INTO `{self._table}` ({col_list}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {update_sql}"
        )

        conn = self._connect()
        try:
            cur = conn.cursor()
            batch: list[tuple[Any, ...]] = []
            for raw in rows:
                n = self._normalize_row(raw)
                batch.append(tuple(n[c] for c in cols))
            cur.executemany(sql, batch)  # 比循环单条 insert 少往返
            cur.close()
        finally:
            conn.close()


if __name__ == "__main__":
    print(
        "museum_crawler.db 为库模块，请从 crawler 目录运行入口脚本，例如：\n"
        "  cd crawler\n"
        "  python museum_spider.py --help\n"
        "  python csv_mysql_sync.py import --csv output/harvard_art_museums.csv\n"
        "（直接运行本文件仅用于验证 import 与 ARTIFACT_COL_DDL 校验已通过。）",
        flush=True,
    )
