# -*- coding: utf-8 -*-
"""测试远程服务器图片 API。"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

API = sys.argv[1] if len(sys.argv) > 1 else "http://47.96.152.190:8000"


def req(path: str, timeout: float = 25) -> tuple[bool, int, bytes, str]:
    url = API.rstrip("/") + path
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
            return True, r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return False, e.code, e.read(), e.headers.get("Content-Type", "")
    except Exception as e:
        return False, 0, str(e).encode(), ""


def main() -> int:
    print(f"API: {API}\n")

    print("=== 1. /api/health ===")
    ok, code, body, _ = req("/api/health")
    print(f"  可达: {ok}  HTTP {code}")
    if ok:
        print(json.dumps(json.loads(body), ensure_ascii=False, indent=2))
    else:
        print(" ", body.decode("utf-8", "replace")[:300])
        print("\n结论: API 不可达，请确认服务器已启动 uvicorn 且安全组放行 8000")
        return 1

    def stat_museum(mid: int, name: str) -> None:
        print(f"\n=== {name} (museum_id={mid}) ===")
        page, items = 1, []
        total = 0
        while True:
            o, c, b, _ = req(f"/api/artifacts?museum_id={mid}&page={page}&size=100")
            if not o:
                print(f"  列表失败 HTTP {c}: {b[:120]}")
                return
            data = json.loads(b)
            total = data["total"]
            items.extend(data["list"])
            if len(items) >= total:
                break
            page += 1
        hit = sum(1 for x in items if x.get("has_local_image"))
        print(f"  总记录: {total}")
        print(f"  has_local_image=True: {hit}")
        print(f"  has_local_image=False: {total - hit}")
        sample_ok = next((x for x in items if x.get("has_local_image")), None)
        sample_bad = next((x for x in items if not x.get("has_local_image")), None)
        if sample_ok:
            p = sample_ok["img_web"]
            o, c, b, ct = req(p)
            print(f"  有图样例 {sample_ok['object_id']}: GET {p} -> {c} {ct} {len(b)} bytes")
        if sample_bad:
            print(f"  无图样例 {sample_bad['object_id']}: {sample_bad.get('title','')[:45]}")

    stat_museum(3, "波士顿 MFA")
    stat_museum(2, "哈佛")
    stat_museum(1, "史密森尼")

    print("\n=== 指定 MFA 98.12 ===")
    o, c, b, ct = req("/api/img/3/98.12")
    print(f"  GET /api/img/3/98.12 -> {c} {ct} {len(b)} bytes")

    print("\n=== 结论 ===")
    o, _, b, _ = req("/api/health")
    health = json.loads(b) if o else {}
    roots = health.get("image_roots", [])
    print(f"  API 服务: 正常")
    print(f"  图片根目录: {roots}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
