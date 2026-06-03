# -*- coding: utf-8 -*-
"""
海外藏中国文物 — Web 图片与藏品 API

浏览器不能直接使用数据库里的本地路径（如 C:\\... 或 images\\harvard\\xxx.jpg），
须通过本服务读取磁盘文件并以 HTTP 返回。

启动：
  cd backend
  pip install -r requirements.txt
  uvicorn main:app --reload --host 0.0.0.0 --port 8000

接口文档：http://47.96.152.190:8000/docs
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# 加载 .env（优先 backend/.env，其次上级 crawler/.env）
load_dotenv(Path(__file__).resolve().parent / ".env")
load_dotenv(Path(__file__).resolve().parent.parent / "crawler" / ".env")

from image_resolver import build_image_roots, resolve_image_file, split_image_paths  # noqa: E402
from db_helper import mysql_configured, db_connect  # noqa: E402

app = FastAPI(title="海外藏中国文物 API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

IMAGE_ROOTS = build_image_roots()
TABLE = os.environ.get("MYSQL_TABLE", "artifact").strip() or "artifact"


def _db_conn():
    if not mysql_configured():
        raise HTTPException(503, "MySQL 未配置，请检查 backend/.env")
    try:
        return db_connect()
    except Exception as e:
        raise HTTPException(503, f"MySQL 连接失败: {e}") from e


def _img_api_url(museum_id: int | str, object_id: str, index: int = 0) -> str:
    oid = str(object_id).strip()
    if index == 0:
        return f"/api/img/{museum_id}/{oid}"
    return f"/api/img/{museum_id}/{oid}/{index}"


def _resolve_paths_from_row(row: dict[str, Any]) -> list[Path]:
    paths_raw = split_image_paths(row.get("image_paths") or "")
    if not paths_raw:
        ip = (row.get("image_path") or "").strip()
        if ip:
            paths_raw = [ip]
    resolved: list[Path] = []
    for ps in paths_raw:
        fp = resolve_image_file(ps, roots=IMAGE_ROOTS)
        if fp is not None:
            resolved.append(fp)
    return resolved


def _row_to_api(row: dict[str, Any], *, include_detail: bool = False) -> dict[str, Any]:
    # 前端要的json
    museum_id = row.get("museum_id")
    object_id = row.get("object_id")
    local_files = _resolve_paths_from_row(row)

    imgs_web = [_img_api_url(museum_id, object_id, i) for i in range(len(local_files))]
    out: dict[str, Any] = {
        "museum_id": museum_id,
        "object_id": object_id,
        "title": row.get("title") or "",
        "artist": row.get("artist") or "",
        "dynasty": row.get("dynasty") or "",
        "material": row.get("material") or "",
        "type": row.get("type") or "",
        "museum": row.get("museum") or "",
        "image_count": int(row.get("image_count") or len(local_files) or 0),
        "img_web": imgs_web[0] if imgs_web else None,
        "imgs_web": imgs_web,
        "has_local_image": bool(local_files),
    }
    if include_detail:
        for k in (
            "period", "culture", "description", "provenance", "dimensions",
            "location", "detail_url", "credit_line", "accession_number", "crawl_date",
        ):
            if k in row:
                out[k] = row[k]
    return out


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "mysql": mysql_configured(),
        "image_roots": [str(p) for p in IMAGE_ROOTS],
    }

# 所有的文物
@app.get("/api/artifacts")
def list_artifacts(
    museum_id: Optional[int] = Query(None, description="馆别 1=史密森尼 2=哈佛 3=MFA"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    dynasty: Optional[str] = None,
    material: Optional[str] = None,
    q: Optional[str] = Query(None, description="标题关键词"),
):
    conn = _db_conn()
    cur = conn.cursor()

    where = ["1=1"]
    params: list[Any] = []
    if museum_id is not None:
        where.append("museum_id = %s")
        params.append(museum_id)
    if dynasty:
    # 模糊查询
        where.append("dynasty LIKE %s")
        params.append(f"%{dynasty}%")
    if material:
        where.append("material LIKE %s")
        params.append(f"%{material}%")
    if q:
        where.append("title LIKE %s")
        params.append(f"%{q}%")

    wsql = " AND ".join(where)
    cur.execute(f"SELECT COUNT(*) AS c FROM `{TABLE}` WHERE {wsql}", params)
    total = int(cur.fetchone()["c"])

    offset = (page - 1) * size
    cur.execute(
        f"""
        SELECT museum_id, object_id, title, artist, dynasty, material, type, museum,
               image_path, image_paths, image_count
        FROM `{TABLE}`
        WHERE {wsql}
        ORDER BY museum_id, object_id
        LIMIT %s OFFSET %s
        """,
        params + [size, offset],
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    return {
        "page": page,
        "size": size,
        "total": total,
        "list": [_row_to_api(r) for r in rows],
    }

# 特定的文物
@app.get("/api/artifacts/{museum_id}/{object_id}")
def get_artifact(museum_id: int, object_id: str):
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT * FROM `{TABLE}` WHERE museum_id = %s AND object_id = %s",
        (museum_id, object_id),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise HTTPException(404, "藏品不存在")
    return _row_to_api(row, include_detail=True)


@app.get("/api/img/{museum_id}/{object_id}")
def get_image(museum_id: int, object_id: str, index: int = Query(0, ge=0)):
    return _serve_artifact_image(museum_id, object_id, index)


@app.get("/api/img/{museum_id}/{object_id}/{index}")
def get_image_by_index(museum_id: int, object_id: str, index: int):
    if index < 0:
        raise HTTPException(400, "index 须 >= 0")
    return _serve_artifact_image(museum_id, object_id, index)


def _serve_artifact_image(museum_id: int, object_id: str, index: int):
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT image_path, image_paths FROM `{TABLE}`
        WHERE museum_id = %s AND object_id = %s
        """,
        (museum_id, object_id),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise HTTPException(404, "藏品不存在")

    files = _resolve_paths_from_row(row)
    if not files:
        raise HTTPException(
            404,
            f"本地图片未找到（object_id={object_id}）。"
            f"请确认 IMAGE_BASE_DIR / HARVARD_IMAGE_DIR 配置正确。",
        )
    if index >= len(files):
        raise HTTPException(404, f"图片索引 {index} 不存在（共 {len(files)} 张）")

    fp = files[index]
    media = "image/jpeg"
    if fp.suffix.lower() == ".png":
        media = "image/png"
    elif fp.suffix.lower() == ".webp":
        media = "image/webp"
    return FileResponse(fp, media_type=media, filename=fp.name)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
