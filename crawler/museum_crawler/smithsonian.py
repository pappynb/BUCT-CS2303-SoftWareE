# -*- coding: utf-8 -*-
"""
史密森尼学会 Open Access API + S3 元数据爬虫。

策略要点
--------
- 搜索对齐 ``si.edu/search/images?edan_q=…``：仅 ``q`` + ``online_media_type:Images``，
  **不使用** ``type=edanmdm`` / ``row_group=objects``（否则前列全是无图图书馆书目）。
- 详情用 Content API，依次尝试 ``id`` 与 ``url``（``edanmdm:…``）。
- 图片只认 ``ids.si.edu`` 交付链；官网上的馆藏卡片图很多不在 Open Access 里，会跳过。
- API 结果不足时，从 Smithsonian Open Access S3 按馆别（FSG/CHNDM 等）扫中国相关元数据。
"""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Optional
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

from tqdm import tqdm

from museum_crawler.http_client import (
    download_image,
    ext_from_url,
    jitter,
    make_session,
    retry_get,
    safe_filename_fragment,
)
from museum_crawler.io_csv import append_csv
from museum_crawler.config import MUSEUM_ID_SMITHSONIAN
from museum_crawler.incremental import (
    IncrementalCsvStore,
    append_change_log,
    diff_fields,
    save_state,
)
from museum_crawler.record_build import finalize_record
from museum_crawler.text_geography import (
    extract_province,
    join_listish,
    parse_period_years,
    resolve_dynasty,
)

if TYPE_CHECKING:
    from museum_crawler.db import MySQLWriter

log = logging.getLogger("spider")

SI_SEARCH = "https://api.si.edu/openaccess/api/v1.0/search"
SI_CONTENT = "https://api.si.edu/openaccess/api/v1.0/content/{}/"
SI_S3_EDAN = "https://smithsonian-open-access.s3-us-west-2.amazonaws.com/metadata/edan"
SI_IMAGE_FQ = 'online_media_type:"Images"'
SI_CC0_FQ = 'metadata_usage:"CC0" OR media_usage:"CC0"'
SI_IMAGE_SEP = " | "
SI_MAX_IMAGES = 8

# 与官网「馆藏图」更相关的馆别（S3 元数据目录名小写）
SI_ART_UNITS = ("fsg", "chndm", "nmah", "saam", "nmaahc", "nmnhanthro")

# API 搜索：先宽（对齐官网未勾 CC0），再窄（只要 CC0 可下图）
SI_SEARCH_PROFILES: list[dict[str, Any]] = [
    {"q": "chinese", "fq": [SI_IMAGE_FQ]},
    {"q": "china", "fq": [SI_IMAGE_FQ]},
    {"q": "Chinese art", "fq": [SI_IMAGE_FQ]},
    {"q": "chinese", "fq": [SI_IMAGE_FQ, SI_CC0_FQ]},
    {"q": "china", "fq": [SI_IMAGE_FQ, SI_CC0_FQ]},
]

_CHINA_RE = re.compile(
    r"\b(china|chinese|qing|ming|tang|song|yuan|han|taiwan|tibet|manchu)\b",
    re.I,
)


def _si_content_dict(row: dict[str, Any]) -> dict[str, Any]:
    c = row.get("content")
    if isinstance(c, str):
        try:
            c = json.loads(c.strip() or "{}")
        except Exception:
            c = {}
    return c if isinstance(c, dict) else {}


def _si_upgrade_url(url: str) -> str:
    if not url:
        return url
    if "ids.si.edu" in url and "deliveryService" in url:
        parsed = urlparse(url)
        qs = {
            k: v[0]
            for k, v in parse_qs(parsed.query).items()
            if k.lower() != "max"
        }
        return urlunparse(parsed._replace(query=urlencode(qs)))
    return url


def _si_collect_urls(obj: Any, acc: list[tuple[str, str]]) -> None:
    if isinstance(obj, dict):
        lbl = str(obj.get("label") or obj.get("type") or "")
        for key in ("url", "href", "IDS_url", "content"):
            v = obj.get(key)
            if isinstance(v, str):
                u = v.strip()
                if u.startswith("//"):
                    u = "https:" + u
                if u.startswith("http"):
                    acc.append((lbl, u))
        for v in obj.values():
            _si_collect_urls(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _si_collect_urls(v, acc)


def _si_image_score(lbl: str, u: str) -> int:
    ul, ll = u.lower(), lbl.lower()
    s = 0
    if "thumb" in ul or "thumb" in ll or "small" in ll or "150" in ul:
        s -= 70
    if "deliveryservice" in ul:
        s += 50
    if any(x in ll for x in ("high", "full", "original", "large", "tiff", "master")):
        s += 40
    if ul.endswith((".jpg", ".jpeg", ".png")):
        s += 20
    if "/download?" in ul and (".tif" in ul or ul.endswith((".tif", ".tiff"))):
        s += 5
    return s


def _si_url_dedup_key(url: str) -> str:
    if "ids.si.edu" in url:
        qs = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        raw_id = qs.get("id") or ""
        if raw_id:
            return re.sub(r"\.(jpe?g|png|tiff?|webp)$", "", raw_id, flags=re.I)
    return url.split("?")[0].lower()


def _si_all_image_urls(row: dict[str, Any]) -> list[str]:
    """从 Content 行收集全部 ids.si.edu 可下图链（去重后按质量降序）。"""
    content = _si_content_dict(row)
    dnr = content.get("descriptiveNonRepeating") or {}
    om = dnr.get("online_media") or {}
    pairs: list[tuple[str, str]] = []
    _si_collect_urls(om, pairs)
    pairs = [(lbl, _si_upgrade_url(u)) for lbl, u in pairs if "ids.si.edu" in u]
    if not pairs:
        return []

    best_by_key: dict[str, tuple[int, str]] = {}
    for lbl, url in pairs:
        key = _si_url_dedup_key(url)
        score = _si_image_score(lbl, url)
        prev = best_by_key.get(key)
        if prev is None or score > prev[0]:
            best_by_key[key] = (score, url)

    ordered = sorted(best_by_key.values(), key=lambda x: x[0], reverse=True)
    urls = [u for _s, u in ordered]
    return _si_cap_image_urls(urls)


def _si_cap_image_urls(urls: list[str]) -> list[str]:
    """优先 Web 可下图格式，限制张数，避免补爬时拖入超大 TIFF。"""
    if not urls:
        return []
    webish = [
        u
        for u in urls
        if "deliveryService" in u
        or re.search(r"\.(jpe?g|png|webp)(\?|$)", u, re.I)
    ]
    picked = webish if webish else urls[:1]
    return picked[:SI_MAX_IMAGES]


def _si_best_image(row: dict[str, Any]) -> Optional[str]:
    """只返回 ids.si.edu 可下载图链（Open Access 与爬虫下图通道一致）。"""
    urls = _si_all_image_urls(row)
    return urls[0] if urls else None


def _si_content_keys(row: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for k in (str(row.get("id") or "").strip(), str(row.get("url") or "").strip()):
        if k and k not in keys:
            keys.append(k)
    return keys


def _si_fetch_full_row(
    sess: Any,
    api_key: str,
    row: dict[str, Any],
    api_delay: float,
) -> dict[str, Any]:
    """拉 Content 详情；搜索行里的 content 往往不含 online_media。"""
    for key in _si_content_keys(row):
        jitter(api_delay, 0.3, 0.8)
        try:
            cr = retry_get(
                sess,
                SI_CONTENT.format(quote(key, safe="")),
                params={"api_key": api_key},
                timeout=60,
            )
            if cr.status_code != 200:
                continue
            full = (cr.json() or {}).get("response") or {}
            if isinstance(full, dict) and full.get("content"):
                return full
        except Exception:
            continue
    return row


def _si_china_related(row: dict[str, Any]) -> bool:
    content = _si_content_dict(row)
    idx = content.get("indexedStructured") or {}
    parts = [
        str(row.get("title") or ""),
        join_listish(idx.get("culture")),
        join_listish(idx.get("topic")),
        join_listish(idx.get("place")),
    ]
    blob = " ".join(parts)
    return bool(_CHINA_RE.search(blob))


def _si_parse(row: dict[str, Any]) -> dict[str, Any]:
    content = _si_content_dict(row)
    dnr = content.get("descriptiveNonRepeating") or {}
    idx = content.get("indexedStructured") or {}
    ft = content.get("freetext") or {}

    def ftext(key: str) -> str:
        v = ft.get(key) or []
        if isinstance(v, list):
            parts = []
            for item in v:
                if isinstance(item, dict):
                    t = item.get("content") or item.get("text") or ""
                else:
                    t = str(item)
                if t:
                    parts.append(t.strip())
            return " ".join(parts)
        return str(v).strip()

    oid = str(row.get("id") or "").strip()
    title = (row.get("title") or ftext("title") or "").strip()
    dated_alt = str(dnr.get("object_date") or "").strip()
    period = join_listish(idx.get("date")) or dated_alt
    obj_type = join_listish(idx.get("object_type") or idx.get("object_type_name"))
    material = (
        join_listish(idx.get("physical_style"))
        or join_listish(idx.get("material"))
        or ftext("physicalDescription")
    )
    culture = join_listish(
        idx.get("topic") or idx.get("culture") or idx.get("culture_name")
    )
    provenance = ftext("provenance") or join_listish(idx.get("provenance"))
    desc_parts = [ftext(k) for k in ("notes", "label", "description") if ftext(k)]
    desc = " ".join(desc_parts).strip()
    raw_phys = ftext("physicalDescription")
    dim_m = re.search(
        r"[\d.,]+\s*[×xX]\s*[\d.,]+(?:\s*[×xX]\s*[\d.,]+)?"
        r"(?:\s*(?:cm|mm|in|ft|m))?",
        raw_phys,
        re.I,
    )
    dimensions = dim_m.group(0).strip() if dim_m else ""
    names = idx.get("name") or []
    artist = join_listish(names)
    places = join_listish(idx.get("place") or [])
    all_text = " ".join([places, provenance, desc, culture, artist])
    artist_province = extract_province(all_text)
    ps, pe = parse_period_years(period)
    if ps is None:
        ps, pe = parse_period_years(dated_alt)
    dynasty = resolve_dynasty(period or dated_alt, ps, pe)

    detail = ""
    for k in ("guid",):
        v = dnr.get(k)
        if isinstance(v, str) and v.startswith("http"):
            detail = v
            if detail.startswith("http://"):
                detail = "https://" + detail[7:]
            break
    rec_id = str(dnr.get("record_ID") or dnr.get("catalogedID") or "").strip()
    if not detail and rec_id:
        detail = (
            f"https://collections.si.edu/search/results.htm"
            f"?q=record_ID%3A{quote(rec_id, safe='')}"
        )
    elif not detail:
        detail = (
            f"https://collections.si.edu/search/results.htm"
            f"?q=record_ID%3A{quote(oid, safe='')}"
        )
    credit = ftext("creditLine") or str(dnr.get("data_source") or "")
    bibliography = ftext("bibliography") or ftext("publication") or ""
    acc = str(
        dnr.get("accessionNumber")
        or dnr.get("accession_number")
        or dnr.get("catalogedID")
        or rec_id
        or ""
    ).strip()

    partial: dict[str, Any] = {
        "object_id": oid,
        "title": title or "(untitled)",
        "artist": artist,
        "artist_province": artist_province,
        "dynasty": dynasty,
        "period": period,
        "period_start_year": str(ps) if ps is not None else "",
        "period_end_year": str(pe) if pe is not None else "",
        "type": obj_type,
        "material": material,
        "culture": culture or "Chinese",
        "description": desc,
        "provenance": provenance,
        "bibliography": bibliography,
        "dimensions": dimensions,
        "detail_url": detail,
        "credit_line": credit,
        "accession_number": acc,
        "iiif_manifest_url": "",
    }
    return finalize_record(partial, MUSEUM_ID_SMITHSONIAN)


def _iter_s3_unit(sess: Any, unit: str) -> Iterator[dict[str, Any]]:
    index_url = f"{SI_S3_EDAN}/{unit}/index.txt"
    try:
        ir = retry_get(sess, index_url, timeout=60)
    except Exception as exc:
        log.warning("[SI] S3 索引失败 %s: %s", unit, exc)
        return
    shard_urls = [u.strip() for u in ir.text.splitlines() if u.strip()]
    for shard_url in shard_urls:
        try:
            sr = retry_get(sess, shard_url, timeout=120)
        except Exception as exc:
            log.debug("[SI] S3 分片失败 %s: %s", shard_url, exc)
            continue
        for line in sr.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _si_try_write_one(
    *,
    row: dict[str, Any],
    sess: Any,
    img_sess: Any,
    api_key: str,
    api_delay: float,
    img_delay: float,
    img_root: Path,
    crawl_day: str,
    limit: int,
    total_written: int,
    rows_batch: list[dict[str, Any]],
    store: IncrementalCsvStore | None = None,
    fetch_content: bool = True,
) -> tuple[Optional[dict[str, Any]], str, list[str]]:
    """
    处理单条：返回 (record, status, changed_fields)。
    status: ok | dup | noimg | dlfail | limit | unchanged
    """
    already = total_written + len(rows_batch)
    if limit and already >= limit:
        return None, "limit", []

    oid = str(row.get("id") or "").strip()
    if not oid:
        return None, "dup", []

    preview_row = _si_parse(row)
    preview_row["crawl_date"] = crawl_day
    img_url = _si_best_image(row)
    if not img_url and fetch_content:
        row = _si_fetch_full_row(sess, api_key, row, api_delay)
        img_url = _si_best_image(row)
        preview_row = _si_parse(row)
        preview_row["crawl_date"] = crawl_day

    if not img_url:
        return None, "noimg", []

    sid = safe_filename_fragment(oid)
    ext = ext_from_url(img_url)
    preview_row["image_url"] = img_url
    preview_row["image_path"] = str(Path("images") / "smithsonian" / f"{sid}.{ext}")
    old = store.get(oid) if store else None
    if old:
        changed = diff_fields(old, preview_row)
        if not changed:
            return None, "unchanged", []

    dest = img_root / "smithsonian" / f"{sid}.{ext}"
    jitter(img_delay, 0.5, 2.0)
    if not download_image(img_sess, img_url, dest):
        return None, "dlfail", []

    rec = preview_row
    rec["image_url"] = img_url
    rec["image_path"] = str(Path("images") / "smithsonian" / f"{sid}.{ext}")
    return rec, "ok", diff_fields(old or {}, rec) if old else list(rec.keys())


def crawl_smithsonian(
    api_key: str,
    out_csv: Path,
    img_root: Path,
    limit: int,
    rows_per_page: int,
    api_delay: float,
    img_delay: float = 3.0,
    db_writer: Optional["MySQLWriter"] = None,
    *,
    s3_only: bool = False,
    s3_units: Optional[tuple[str, ...]] = None,
    api_max_pages: int = 40,
) -> dict[str, int]:
    sess = make_session()
    img_sess = make_session()
    rows_batch: list[dict[str, Any]] = []
    total_written = 0
    total_scanned = 0
    total_new = 0
    total_updated = 0
    total_unchanged = 0
    total_failed = 0
    total_skipped = 0
    change_rows: list[dict[str, Any]] = []
    store = IncrementalCsvStore.load(out_csv)
    crawl_day = date.today().isoformat()
    img_ok = 0

    units = tuple(u.strip().lower() for u in (s3_units or SI_ART_UNITS) if u.strip())
    if not units:
        units = SI_ART_UNITS

    log.info(
        "[SI] 开始：先 S3 馆别 %s（可下图）；%s",
        ",".join(units),
        "跳过 API" if s3_only else f"API 每档最多 {api_max_pages} 页",
    )
    pbar = tqdm(
        desc="Smithsonian",
        unit="条",
        total=limit if limit else None,
        dynamic_ncols=True,
    )

    if store.rows:
        log.info("[SI] 本地快照：%d 条", len(store.rows))

    def _si_pbar_postfix(*, page_i: int = 0, page_n: int = 0, phase: str = "") -> None:
        written = total_written + len(rows_batch)
        tail = f" 本页{page_i}/{page_n}" if page_n else ""
        if phase:
            tail += phase
        pbar.set_postfix_str(f"写入{written} 图{img_ok}{tail}", refresh=True)

    def flush() -> None:
        nonlocal total_written, rows_batch
        if rows_batch:
            if store:
                if db_writer:
                    db_writer.upsert_batch(rows_batch)
            else:
                append_csv(out_csv, rows_batch, db_writer)
                total_written += len(rows_batch)
            rows_batch = []

    try:
        # —— 阶段 1：S3 开放元数据（含 ids.si.edu，Freer/设计馆等中国藏品）——
        for unit in units:
            if limit and total_written + len(rows_batch) >= limit:
                break
            log.info("[SI] S3 扫描馆别 %s", unit.upper())
            n_ok = n_skip = 0
            for row in _iter_s3_unit(sess, unit):
                if limit and total_written + len(rows_batch) >= limit:
                    break
                if not _si_china_related(row):
                    continue
                rec, st, _ = _si_try_write_one(
                    row=row,
                    sess=sess,
                    img_sess=img_sess,
                    api_key=api_key,
                    api_delay=api_delay,
                    img_delay=img_delay,
                    img_root=img_root,
                    crawl_day=crawl_day,
                    limit=limit,
                    total_written=total_written,
                    rows_batch=rows_batch,
                    store=store,
                    fetch_content=False,
                )
                total_scanned += 1
                if st == "ok" and rec:
                    change_type, changed_fields = store.upsert(rec)
                    if change_type == "new":
                        total_new += 1
                    elif change_type == "updated":
                        total_updated += 1
                    elif change_type == "unchanged":
                        total_unchanged += 1
                        continue
                    change_rows.append(
                        {
                            "object_id": rec.get("object_id", ""),
                            "change_type": change_type,
                            "changed_fields": changed_fields,
                            "title": rec.get("title", ""),
                            "detail_url": rec.get("detail_url", ""),
                        }
                    )
                    rows_batch.append(rec)
                    img_ok += 1
                    n_ok += 1
                    pbar.update(1)
                    _si_pbar_postfix(phase=f"·S3 {unit}")
                    if len(rows_batch) >= 10:
                        flush()
                elif st in ("noimg", "dlfail", "dup"):
                    n_skip += 1
                elif st == "unchanged":
                    total_unchanged += 1
            log.info("[SI] S3 %s 完成：写入 %d，跳过 %d", unit.upper(), n_ok, n_skip)
            flush()
            jitter(api_delay, 0.5, 1.0)

        # —— 阶段 2：Open Access Search（补充；官网 Kite 等多数仍无 ids 链）——
        if not s3_only:
            for profile in SI_SEARCH_PROFILES:
                if limit and total_written + len(rows_batch) >= limit:
                    break
                q_term = str(profile.get("q") or "chinese")
                fq = profile.get("fq") or [SI_IMAGE_FQ]
                start = 0
                profile_pages = 0
                while profile_pages < api_max_pages:
                    already = total_written + len(rows_batch)
                    if limit and already >= limit:
                        break
                    n = min(rows_per_page, limit - already) if limit else rows_per_page
                    params: dict[str, Any] = {
                        "q": q_term,
                        "fq": fq,
                        "start": start,
                        "rows": n,
                        "sort": "relevancy",
                        "api_key": api_key,
                    }
                    log.info(
                        "[SI] API q=%s start=%d rows=%d (页 %d/%d)",
                        q_term, start, n, profile_pages + 1, api_max_pages,
                    )
                    _si_pbar_postfix(phase=f"·API {q_term}@{start}")
                    try:
                        r = retry_get(sess, SI_SEARCH, params=params, timeout=90)
                    except Exception as exc:
                        log.error("[SI] search 失败: %s", exc)
                        break

                    batch = (r.json().get("response") or {}).get("rows") or []
                    if not batch:
                        break

                    n_dup = n_noimg = n_dl_fail = n_ok_page = 0
                    page_n = len(batch)
                    for page_i, row in enumerate(batch, 1):
                        rec, st, _ = _si_try_write_one(
                            row=row,
                            sess=sess,
                            img_sess=img_sess,
                            api_key=api_key,
                            api_delay=api_delay,
                            img_delay=img_delay,
                            img_root=img_root,
                            crawl_day=crawl_day,
                            limit=limit,
                            total_written=total_written,
                            rows_batch=rows_batch,
                            store=store,
                            fetch_content=True,
                        )
                        if st == "limit":
                            break
                        total_scanned += 1
                        if st == "dup":
                            n_dup += 1
                        elif st == "noimg":
                            n_noimg += 1
                        elif st == "dlfail":
                            n_dl_fail += 1
                        elif st == "unchanged":
                            total_unchanged += 1
                        elif st == "ok" and rec:
                            change_type, changed_fields = store.upsert(rec)
                            if change_type == "new":
                                total_new += 1
                            elif change_type == "updated":
                                total_updated += 1
                            elif change_type == "unchanged":
                                total_unchanged += 1
                                continue
                            change_rows.append(
                                {
                                    "object_id": rec.get("object_id", ""),
                                    "change_type": change_type,
                                    "changed_fields": changed_fields,
                                    "title": rec.get("title", ""),
                                    "detail_url": rec.get("detail_url", ""),
                                }
                            )
                            rows_batch.append(rec)
                            img_ok += 1
                            n_ok_page += 1
                            pbar.update(1)
                        _si_pbar_postfix(page_i=page_i, page_n=page_n)

                    log.info(
                        "[SI] API 小结 q=%s start=%d | 返回 %d | 成功 %d | 无图 %d | 累计 %d",
                        q_term, start, len(batch), n_ok_page, n_noimg,
                        total_written + len(rows_batch),
                    )
                    flush()
                    profile_pages += 1
                    start += len(batch)
                    if len(batch) < n:
                        break
                    jitter(api_delay, 1.0, 2.0)

    finally:
        flush()
        pbar.close()
        if store and rows_batch and db_writer:
            db_writer.upsert_batch(rows_batch)
        store.save()
        total_written = total_new + total_updated
        summary = {
            "records": total_written,
            "images_downloaded": img_ok,
            "new": total_new,
            "updated": total_updated,
            "unchanged": total_unchanged,
            "failed": total_failed,
            "skipped": total_skipped,
            "scanned": total_scanned,
            "changes": len(change_rows),
        }
        append_change_log(
            out_csv.parent / "crawl_changes.jsonl",
            run_at=datetime.now().isoformat(timespec="seconds"),
            museum="Smithsonian",
            csv_name=out_csv.name,
            changes=change_rows,
            summary=summary,
        )
        save_state(
            out_csv.parent / ".crawl_state",
            "smithsonian",
            store,
            summary=summary,
        )
        log.info(
            "[SI] 完成：写入 %d 条（新 %d / 更 %d / 未变 %d），图片 %d 张 → %s",
            total_written, total_new, total_updated, total_unchanged, img_ok, out_csv,
        )
        if total_written == 0:
            log.warning(
                "[SI] 0 条。官网 Kite/NMAH 卡片多数无 ids 开放链；请用 "
                "--si-s3-only --si-units fsg,chndm --limit 20 试跑。确认 SI_DATA_GOV_API_KEY。"
            )

    return {
        "records": total_written,
        "images_downloaded": img_ok,
        "new": total_new,
        "updated": total_updated,
        "unchanged": total_unchanged,
        "failed": total_failed,
        "skipped": total_skipped,
        "scanned": total_scanned,
        "changes": len(change_rows),
    }


def _si_local_path(img_root: Path, oid: str, idx: int, ext: str) -> Path:
    sid = safe_filename_fragment(oid)
    if idx <= 1:
        return img_root / "smithsonian" / f"{sid}.{ext}"
    return img_root / "smithsonian" / f"{sid}_{idx}.{ext}"


def _si_rel_path(path: Path, img_root: Path) -> str:
    try:
        rel = path.relative_to(img_root.parent)
    except ValueError:
        rel = path
    return str(rel).replace("/", "\\")


def _si_row_images_backfilled(row: dict[str, Any]) -> bool:
    """``image_urls`` 非空视为本脚本已补过多图字段，默认跳过。"""
    return bool(str(row.get("image_urls") or "").strip())


def backfill_smithsonian_images(
    csv_path: Path,
    img_root: Path,
    api_key: str,
    *,
    api_delay: float = 1.0,
    img_delay: float = 2.0,
    limit: int = 0,
    skip_download: bool = False,
    skip_filled: bool = True,
    db_writer: Optional["MySQLWriter"] = None,
) -> dict[str, int]:
    """
    仅补爬/补全多图字段，不改动其它列。

    更新 ``image_url`` / ``image_urls`` / ``image_path`` / ``image_paths`` / ``image_count``。
    首张图尽量保留已有本地路径；新增图命名为 ``{object_id}_2.ext`` …

    ``skip_filled=True``（默认）时，``image_urls`` 已有内容的行不再请求 API。
    """
    from museum_crawler.io_csv import write_csv

    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        log.warning("[SI] 补图：CSV 无数据 %s", csv_path)
        return {"rows": 0, "updated": 0, "multi": 0, "images_added": 0}

    sess = make_session()
    img_sess = make_session()
    img_root.mkdir(parents=True, exist_ok=True)
    (img_root / "smithsonian").mkdir(parents=True, exist_ok=True)

    updated = multi = images_added = failed = skipped = 0
    todo = rows[:limit] if limit else rows
    pbar = tqdm(todo, desc="SI 补多图", unit="条")

    for row in pbar:
        oid = str(row.get("object_id") or "").strip()
        if not oid:
            continue
        if skip_filled and _si_row_images_backfilled(row):
            skipped += 1
            pbar.set_postfix_str(
                f"更新{updated} 跳过{skipped} 多图{multi} 无图{failed}"
            )
            continue

        stub = {"id": oid, "url": oid}
        full = _si_fetch_full_row(sess, api_key, stub, api_delay)
        urls = _si_all_image_urls(full)
        if not urls:
            failed += 1
            pbar.set_postfix_str(f"更新{updated} 多图{multi} 无图{failed}")
            continue

        rel_paths: list[str] = []
        used_urls: list[str] = []
        old_path = (row.get("image_path") or "").strip()
        old_file = (img_root.parent / old_path) if old_path else None

        for idx, url in enumerate(urls, 1):
            ext = ext_from_url(url)
            dest = _si_local_path(img_root, oid, idx, ext)

            if idx == 1 and old_file and old_file.is_file() and old_file.stat().st_size > 0:
                dest = old_file
            elif not skip_download:
                if not dest.is_file() or dest.stat().st_size <= 0:
                    jitter(img_delay, 0.3, 1.0)
                    if not download_image(img_sess, url, dest):
                        if idx == 1:
                            break
                        continue
                    images_added += 1
            elif not dest.is_file():
                if idx == 1 and old_path:
                    rel_paths.append(old_path)
                    used_urls.append(url)
                continue

            if dest.is_file() and dest.stat().st_size > 0:
                rel_paths.append(_si_rel_path(dest, img_root))
                used_urls.append(url)

        if not used_urls:
            failed += 1
            continue

        row["image_url"] = used_urls[0]
        row["image_urls"] = SI_IMAGE_SEP.join(used_urls)
        row["image_path"] = rel_paths[0]
        row["image_paths"] = SI_IMAGE_SEP.join(rel_paths)
        row["image_count"] = str(len(used_urls))
        updated += 1
        if len(used_urls) > 1:
            multi += 1
        pbar.set_postfix_str(f"更新{updated} 多图{multi} 无图{failed}")

        if updated % 20 == 0:
            write_csv(csv_path, rows)

    write_csv(csv_path, rows)
    if db_writer and updated:
        from museum_crawler.csv_db_sync import import_csv_to_mysql

        import_csv_to_mysql(csv_path, chunk_size=80)

    stats = {
        "rows": len(todo),
        "updated": updated,
        "skipped": skipped,
        "multi": multi,
        "images_added": images_added,
        "failed": failed,
    }
    log.info(
        "[SI] 补多图完成 %s → 更新 %d（多图 %d），跳过 %d，新下图 %d，无图 %d",
        csv_path.name,
        updated,
        multi,
        skipped,
        images_added,
        failed,
    )
    return stats
