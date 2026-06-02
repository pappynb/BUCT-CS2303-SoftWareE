# -*- coding: utf-8 -*-
"""连接远程 MySQL 读取 image_path，并尝试通过 API / 本地磁盘可视化。"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "crawler"))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "crawler" / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env")

from museum_crawler.db import MySQLWriter
from image_resolver import build_image_roots, resolve_image_file

SERVER = "47.96.152.190"
API = f"http://{SERVER}:8000"
OUT = Path(__file__).resolve().parent / "test_output"
OUT.mkdir(exist_ok=True)


def try_api(path: str, timeout=12):
    url = API + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return True, r.status, r.read(), r.headers.get("Content-Type", "")
    except Exception as e:
        return False, 0, str(e).encode(), ""


def main():
    print("=" * 60)
    print("1. 远程 API 探测")
    print("=" * 60)
    ok, code, body, _ = try_api("/api/health")
    if ok:
        print(f"   {API}/api/health -> {code}")
        print("  ", body.decode()[:200])
    else:
        print(f"   {API}/api/health -> 不可达")
        print("   ", body.decode()[:200])

    print("\n" + "=" * 60)
    print("2. 远程 MySQL 读取 image_path")
    print("=" * 60)
    w = MySQLWriter.from_env()
    conn = w._connect()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT museum_id, object_id, title, image_path, image_paths
           FROM artifact WHERE museum_id=2 ORDER BY object_id LIMIT 6"""
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    print(f"   MySQL {w._host} 连接 OK，读取 {len(rows)} 条哈佛馆藏")

    roots = build_image_roots()
    print(f"   本地图片查找目录: {[str(p) for p in roots[:3]]}")

    print("\n" + "=" * 60)
    print("3. 路径解析 & 可视化")
    print("=" * 60)

    cards = []
    api_ok_count = 0
    local_ok_count = 0

    for r in rows:
        ip = (r.get("image_path") or "").strip()
        fp = resolve_image_file(ip, roots=roots)
        local_ok = fp is not None and fp.is_file()
        if local_ok:
            local_ok_count += 1

        img_api_ok = False
        img_bytes = b""
        if ok:
            aok, _, img_bytes, ctype = try_api(f"/api/img/{r['museum_id']}/{r['object_id']}")
            img_api_ok = aok and len(img_bytes) > 1000
            if img_api_ok:
                api_ok_count += 1
                (OUT / f"remote_api_{r['object_id']}.jpg").write_bytes(img_bytes)

        status = []
        if local_ok:
            status.append(f"本地磁盘 OK ({fp})")
        else:
            status.append("本地磁盘 无文件（图片在服务器 Administrator 目录）")
        if ok:
            status.append("远程API " + ("OK" if img_api_ok else "FAIL"))
        else:
            status.append("远程API 不可达(8000未开)")

        print(f"\n   [{r['object_id']}] {r['title'][:45]}...")
        print(f"      DB path: {ip}")
        print(f"      {' | '.join(status)}")
        cards.append((r, local_ok, fp))

    # 4. 生成本地 HTML（用本地能找到的图；API 可用则优先 API URL）
    html_path = OUT / "remote_test_gallery.html"
    import base64

    def to_data_uri(fp: Path) -> str:
        b = fp.read_bytes()
        return "data:image/jpeg;base64," + base64.b64encode(b).decode()

    items_html = []
    for r, local_ok, fp in cards:
        title = r["title"] or r["object_id"]
        oid = r["object_id"]
        if ok:
            src = f"{API}/api/img/2/{oid}"
        elif local_ok and fp:
            src = to_data_uri(fp)
        else:
            src = ""
        if src:
            items_html.append(
                f'<article class="card"><img src="{src}" alt="{title}"/>'
                f'<p><b>{title}</b><br/>path: {r["image_path"]}</p></article>'
            )
        else:
            items_html.append(
                f'<article class="card missing"><p><b>{title}</b><br/>'
                f'无法可视化 — 需在服务器启动 backend 并放行 8000<br/>'
                f'path: {r["image_path"]}</p></article>'
            )

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"/>
<title>服务器图片联调结果</title>
<style>
body{{font-family:sans-serif;padding:16px;background:#f5f5f5}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}}
.card{{background:#fff;border-radius:8px;padding:8px;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
.card img{{width:100%;height:200px;object-fit:contain;background:#fafafa}}
.missing{{background:#fff2f0;color:#a8071a}}
.summary{{background:#e6f4ff;padding:12px;border-radius:8px;margin-bottom:16px}}
</style></head><body>
<h1>服务器图片联调测试</h1>
<div class="summary">
<p>MySQL ({SERVER}:3306): 已连接</p>
<p>API ({API}): {"已连通" if ok else "不可达 — 请在服务器执行 uvicorn --host 0.0.0.0 --port 8000 并放行安全组"}</p>
<p>远程 API 出图: {api_ok_count}/{len(rows)} | 本机磁盘出图: {local_ok_count}/{len(rows)}</p>
</div>
<div class="grid">{''.join(items_html)}</div>
</body></html>"""
    html_path.write_text(html, encoding="utf-8")
    print(f"\n   HTML 报告: {html_path}")

    print("\n" + "=" * 60)
    print("结论")
    print("=" * 60)
    if ok and api_ok_count > 0:
        print("  远程服务器 API 可用，Web 组可直接用 img_web 接口。")
    elif local_ok_count > 0:
        print("  MySQL 可读；8000 未通，本机用 crawler/output/images 做了部分可视化。")
        print("  要在 Web 端看服务器图片，须在 47.96.152.190 上启动 backend。")
    else:
        print("  MySQL 可读，但无法从本机读取服务器磁盘图片，且 API 未启动。")
    return 0 if (api_ok_count or local_ok_count) else 1


if __name__ == "__main__":
    sys.exit(main())
