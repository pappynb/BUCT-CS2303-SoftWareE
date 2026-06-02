# -*- coding: utf-8 -*-
"""一次性检测 .env 中 MySQL 是否可连。"""
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from museum_crawler.db import MySQLWriter, mysql_configured, ARTIFACT_COL_DDL
from museum_crawler.config import CSV_FIELDS

def main() -> int:
    if not mysql_configured():
        print("FAIL: 未配置 MYSQL_HOST 或 MYSQL_DATABASE")
        return 1

    w = MySQLWriter.from_env()
    print("配置:")
    print(f"  HOST={w._host}:{w._port}")
    print(f"  USER={w._user}")
    print(f"  DATABASE={w._database}")
    print(f"  TABLE={w._table}")

    try:
        import mysql.connector  # noqa: F401
    except ImportError:
        print("FAIL: 未安装 mysql-connector-python，请执行: pip install mysql-connector-python")
        return 1

    try:
        conn = w._connect()
        cur = conn.cursor()
        cur.execute("SELECT VERSION(), DATABASE(), USER()")
        ver, db, user = cur.fetchone()
        print("连接: OK")
        print(f"  MySQL 版本: {ver}")
        print(f"  当前库: {db}")
        print(f"  当前用户: {user}")

        cur.execute(
            """
            SELECT COUNT(*) FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            """,
            (w._database, w._table),
        )
        exists = cur.fetchone()[0] > 0
        print(f"  表 `{w._table}` 存在: {'是' if exists else '否'}")

        if exists:
            cur.execute(f"SELECT COUNT(*) FROM `{w._table}`")
            n = cur.fetchone()[0]
            print(f"  表内行数: {n}")
            cur.execute(
                """
                SELECT museum_id, COUNT(*) AS c
                FROM `{t}` GROUP BY museum_id ORDER BY museum_id
                """.format(t=w._table)
            )
            for mid, c in cur.fetchall():
                print(f"    museum_id={mid}: {c} 条")
        else:
            print("  提示: 表不存在时可运行 museum_spider.py --ensure-mysql-table 自动创建")

        cur.close()
        conn.close()
        return 0
    except Exception as e:
        print(f"连接: FAIL")
        print(f"  错误: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
