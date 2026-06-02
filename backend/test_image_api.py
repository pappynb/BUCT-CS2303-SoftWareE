# -*- coding: utf-8 -*-
"""端到端测试：MySQL 路径 → 本地磁盘 → HTTP 图片接口。"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path(__file__).resolve().parent / "test_output"
OUT.mkdir(exist_ok=True)


def get(path: str) -> tuple[int, bytes, str]:
    req = urllib.request.Request(f"{BASE.rstrip('/')}{path}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), ""


def main() -> int:
    print(f"=== API 测试 base={BASE} ===\n")

    # 1. health
    code, body, _ = get("/api/health")
    health = json.loads(body) if code == 200 else {}
    print(f"[1] GET /api/health -> {code}")
    print(f"    mysql={health.get('mysql')} roots={health.get('image_roots')}")

    # 2. list
    code, body, _ = get("/api/artifacts?museum_id=2&size=5")
    print(f"\n[2] GET /api/artifacts?museum_id=2&size=5 -> {code}")
    if code != 200:
        print("    FAIL:", body[:200])
        return 1
    data = json.loads(body)
    print(f"    total={data['total']} returned={len(data['list'])}")

    ok_items = [x for x in data["list"] if x.get("has_local_image")]
    fail_items = [x for x in data["list"] if not x.get("has_local_image")]
    print(f"    has_local_image: {len(ok_items)} ok, {len(fail_items)} missing")

    if not ok_items:
        print("\n    无可用本地图，请检查 HARVARD_IMAGE_DIR / IMAGE_BASE_DIR")
        return 1

    item = ok_items[0]
    print(f"\n    样例: object_id={item['object_id']} title={item['title'][:50]}...")
    print(f"    img_web={item['img_web']}")

    # 3. detail
    mid, oid = item["museum_id"], item["object_id"]
    code, body, _ = get(f"/api/artifacts/{mid}/{oid}")
    print(f"\n[3] GET /api/artifacts/{mid}/{oid} -> {code}")

    # 4. primary image
    img_path = item["img_web"]
    code, img_bytes, ctype = get(img_path)
    out_file = OUT / f"{oid}_primary.jpg"
    print(f"\n[4] GET {img_path} -> {code} type={ctype} size={len(img_bytes)} bytes")
    if code == 200 and len(img_bytes) > 1000:
        out_file.write_bytes(img_bytes)
        print(f"    已保存: {out_file}")
    else:
        print("    FAIL 图片未成功下载")
        return 1

    # 5. multi image if any
    if len(item.get("imgs_web") or []) > 1:
        code2, img2, _ = get(item["imgs_web"][1])
        print(f"\n[5] GET {item['imgs_web'][1]} -> {code2} size={len(img2)} bytes")

    # 6. batch stats
    print("\n[6] 批量抽检 20 条...")
    code, body, _ = get("/api/artifacts?museum_id=2&size=20")
    batch = json.loads(body)["list"]
    hit = sum(1 for x in batch if x.get("has_local_image"))
    print(f"    20 条中可出图: {hit}/20")

    print("\n=== 结论: 图片可从 API 读取，Web 组可用 img_web 字段 ===")
    print(f"    前端示例: <img src=\"{BASE}{item['img_web']}\" />")
    return 0


if __name__ == "__main__":
    sys.exit(main())
