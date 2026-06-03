# -*- coding: utf-8 -*-
"""
增量抓取与运行日志辅助。

职责：
- 读取上次输出的 CSV 作为本地快照；
- 按 ``object_id`` 行级比对，判断新增 / 更新 / 未变化；
- 生成稳定指纹与变更字段列表；
- 追加运行日志与变更日志；
- 保存轻量状态文件，便于审计与后续增量判断。
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from museum_crawler.config import CSV_FIELDS

log = logging.getLogger("spider")

_HASH_IGNORE_FIELDS = frozenset({"crawl_date", "image_path", "image_paths"})


def _norm_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    return str(v).strip()


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows: list[dict[str, str]] = []
        for raw in csv.DictReader(fh):
            row: dict[str, str] = {}
            for k in CSV_FIELDS:
                row[k] = _norm_cell(raw.get(k, ""))
            rows.append(row)
    return rows


def stable_row_payload(row: dict[str, Any]) -> dict[str, str]:
    """生成用于指纹计算的稳定载荷。"""
    payload: dict[str, str] = {}
    for k in CSV_FIELDS:
        if k in _HASH_IGNORE_FIELDS:
            continue
        payload[k] = _norm_cell(row.get(k, ""))
    return payload


def row_fingerprint(row: dict[str, Any]) -> str:
    blob = json.dumps(
        stable_row_payload(row),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def diff_fields(
    old_row: dict[str, Any],
    new_row: dict[str, Any],
    *,
    ignore_fields: Iterable[str] | None = None,
) -> list[str]:
    ignored = set(ignore_fields or ())
    ignored.update(_HASH_IGNORE_FIELDS)
    changes: list[str] = []
    for k in CSV_FIELDS:
        if k in ignored:
            continue
        if _norm_cell(old_row.get(k, "")) != _norm_cell(new_row.get(k, "")):
            changes.append(k)
    return changes


@dataclass
class IncrementalCsvStore:
    """CSV 行级快照与增量写回。"""

    path: Path
    rows: list[dict[str, str]] = field(default_factory=list)
    index: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "IncrementalCsvStore":
        rows = load_csv_rows(path)
        store = cls(path=path, rows=rows)
        for idx, row in enumerate(rows):
            oid = row.get("object_id", "").strip()
            if oid and oid not in store.index:
                store.index[oid] = idx
        return store

    def get(self, object_id: str) -> dict[str, str] | None:
        idx = self.index.get((object_id or "").strip())
        if idx is None:
            return None
        return self.rows[idx]

    def upsert(self, row: dict[str, Any]) -> tuple[str, list[str]]:
        """
        写入一条记录。

        返回 (change_type, changed_fields)。
        change_type: new | updated | unchanged | skipped
        """
        oid = _norm_cell(row.get("object_id", ""))
        if not oid:
            return "skipped", []
        normalized = {k: _norm_cell(row.get(k, "")) for k in CSV_FIELDS}
        idx = self.index.get(oid)
        if idx is None:
            self.index[oid] = len(self.rows)
            self.rows.append(normalized)
            return "new", list(CSV_FIELDS)
        old = self.rows[idx]
        changed = diff_fields(old, normalized)
        if changed:
            self.rows[idx] = normalized
            return "updated", changed
        return "unchanged", []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in self.rows:
                writer.writerow({k: _norm_cell(row.get(k, "")) for k in CSV_FIELDS})

    def snapshot(self) -> dict[str, str]:
        return {row["object_id"]: row_fingerprint(row) for row in self.rows if row.get("object_id")}


def state_path(state_dir: Path, museum_key: str) -> Path:
    return state_dir / f"{museum_key}.json"


def save_state(
    state_dir: Path,
    museum_key: str,
    store: IncrementalCsvStore,
    *,
    summary: dict[str, Any],
) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "museum": museum_key,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "csv": str(store.path),
        "record_count": len(store.rows),
        "summary": summary,
        "records": store.snapshot(),
    }
    path = state_path(state_dir, museum_key)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def append_run_log(
    path: Path,
    *,
    run_at: str,
    params: dict[str, Any],
    museums: dict[str, dict[str, Any]],
    kg: dict[str, Any] | None = None,
) -> None:
    append_jsonl(
        path,
        {
            "run_at": run_at,
            "params": params,
            "museums": museums,
            "kg": kg or {},
        },
    )


def append_change_log(
    path: Path,
    *,
    run_at: str,
    museum: str,
    csv_name: str,
    changes: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    append_jsonl(
        path,
        {
            "run_at": run_at,
            "museum": museum,
            "csv": csv_name,
            "summary": summary,
            "changes": changes,
        },
    )

