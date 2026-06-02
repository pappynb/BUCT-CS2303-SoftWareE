# -*- coding: utf-8 -*-
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent / ".env")
import mysql.connector

conn = mysql.connector.connect(
    host=os.environ["MYSQL_HOST"],
    port=int(os.environ.get("MYSQL_PORT", 3306)),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
)
cur = conn.cursor()
cur.execute("SHOW DATABASES")
print("数据库列表:", [r[0] for r in cur.fetchall()])
for db in ["overseas_chinese_artifacts", "overseas_artifacts"]:
    print(f"\n=== {db} ===")
    cur.execute(f"USE `{db}`")
    cur.execute("SHOW TABLES")
    tables = [r[0] for r in cur.fetchall()]
    print("表:", tables or "(空)")
    for t in tables:
        cur.execute(f"SELECT COUNT(*) FROM `{t}`")
        print(f"  {t}: {cur.fetchone()[0]} 行")
cur.close()
conn.close()
