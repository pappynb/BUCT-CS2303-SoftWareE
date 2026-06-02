# -*- coding: utf-8 -*-
"""将数据库 image_path / image_paths 解析为磁盘上的真实文件。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Sequence


def split_image_paths(raw: str) -> List[str]:
    if not raw or not str(raw).strip():
        return []
    return [p.strip() for p in str(raw).split("|") if p.strip()]


def build_image_roots() -> List[Path]:
    """从环境变量构建查找目录列表（优先级从高到低）。"""
    roots: List[Path] = []

    # 单目录：哈佛绝对路径（Administrator 部署机）
    harvard = os.environ.get("HARVARD_IMAGE_DIR", "").strip()
    if harvard:
        roots.append(Path(harvard))

    # 通用根：crawler/output/images 或自定义
    base = os.environ.get("IMAGE_BASE_DIR", "").strip()
    if base:
        roots.append(Path(base))

    # 默认：项目内 crawler/output/images
    project_root = Path(__file__).resolve().parent.parent
    defaults = [
        project_root / "crawler" / "output" / "images",
        project_root / "crawler" / "images",
    ]
    for d in defaults:
        if d not in roots:
            roots.append(d)

    # IMAGE_EXTRA_DIRS=D:\a;D:\b
    extra = os.environ.get("IMAGE_EXTRA_DIRS", "").strip()
    if extra:
        for part in extra.split(";"):
            part = part.strip()
            if part:
                roots.append(Path(part))

    return roots


def resolve_image_file(path_str: str, *, roots: Sequence[Path]) -> Optional[Path]:
    """
    解析本地图片路径。
    支持：绝对路径、images/harvard/xxx.jpg、仅文件名。
    """
    if not path_str or not str(path_str).strip():
        return None

    p = Path(str(path_str).strip())
    if p.is_file():
        return p.resolve()

    # 统一斜杠
    rel = Path(str(path_str).replace("\\", "/"))

    for root in roots:
        candidate = (root / rel).resolve()
        if candidate.is_file():
            return candidate
        candidate = (root / rel.name).resolve()
        if candidate.is_file():
            return candidate
        # images/harvard/xxx → root 已是 output/images 时
        parts = rel.parts
        if len(parts) >= 2 and parts[0] == "images":
            candidate = (root / Path(*parts[1:])).resolve()
            if candidate.is_file():
                return candidate

    return None
