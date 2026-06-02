# -*- coding: utf-8 -*-
"""MySQL 连接（PyMySQL，服务器上比 mysql-connector-python 更易安装）。"""
from __future__ import annotations

import os
from typing import Any

import pymysql
from pymysql.cursors import DictCursor


def mysql_configured() -> bool:
    return bool(os.environ.get("MYSQL_HOST", "").strip() and os.environ.get("MYSQL_DATABASE", "").strip())


def db_connect():
    if not mysql_configured():
        raise RuntimeError("MySQL 未配置：请设置 MYSQL_HOST 与 MYSQL_DATABASE")
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "root"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", ""),
        charset=os.environ.get("MYSQL_CHARSET", "utf8mb4"),
        cursorclass=DictCursor,
    )


def fetch_one(sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> dict | None:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()
    finally:
        conn.close()


def fetch_all(sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> list[dict]:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return list(cur.fetchall())
    finally:
        conn.close()
