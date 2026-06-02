# -*- coding: utf-8 -*-
"""
哈佛大学艺术博物馆 REST API 爬虫。

筛选条件：``culture=Chinese|China``、``hasimage=1``。

入库规则（严格多图）：API ``images[]`` 中每个图位各下载一张；**全部图位**
均需成功落盘才写入（仅 1 个图位时下载 1 张即可）。可用 ``--ham-allow-no-image``
改为无图也入库；``--ham-relaxed-multi`` 允许多图藏品只下到部分图也入库。

多图字段：``image_urls`` / ``image_paths``（`` | `` 分隔）、``image_count``；
``image_url`` / ``image_path`` 仍为主图（第一张）。
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from tqdm import tqdm

from museum_crawler.http_client import (
    download_image_first,
    jitter,
    make_session,
    retry_get,
    safe_filename_fragment,
)
from museum_crawler.bibliography import format_harvard_publications
from museum_crawler.config import MUSEUM_ID_HARVARD
from museum_crawler.io_csv import append_csv, write_csv
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

HAM_API = "https://api.harvardartmuseums.org/object"
HAM_FIELDS = ",".join([
    "id", "title", "dated", "period", "classification",
    "medium", "description", "dimensions", "creditline",
    "objectnumber", "url", "primaryimageurl", "images",
    "people", "places", "culture", "provenance", "publicationcount",
])


# 先小图再全图；候选过多时 IIIF 202 轮询会把单条拖到数分钟
_HAM_IIIF_SIZES = (
    "/full/!800,800/0/default.jpg",
    "/full/full/0/default.jpg",
)
_HAM_IMAGE_SEP = " | "
_HAM_MAX_SLOTS = 24


def _ham_iiif_variants(base: str) -> list[str]:
    """由 IIIF base 生成多种尺寸；保留 ``_dynmc`` 并增加去后缀备选。"""
    u = (base or "").strip().rstrip("/")
    if not u.startswith("http"):
        return []
    if re.search(r"\.(jpe?g|png|webp)(\?|$)", u, re.I):
        return [u]
    if re.search(r"/full/", u, re.I):
        return [u]

    bases: list[str] = [u]
    stripped = re.sub(r"_dynmc$", "", u)
    if stripped != u and stripped not in bases:
        bases.append(stripped)

    out: list[str] = []
    seen: set[str] = set()
    for b in bases:
        for suffix in _HAM_IIIF_SIZES:
            url = b + suffix
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out


def _ham_norm_base(url: str) -> str:
    return _ham_iiif_base_from_url(url).lower()


def _ham_image_slots(rec: dict[str, Any]) -> list[tuple[str, str]]:
    """
    每个 API 图位一条 ``(slot_id, base_url)``，不因全局去重而丢掉副图。

    顺序：primaryimageurl → images[] 各条（优先 iiifbaseurl）。
    """
    slots: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(slot_id: str, base: Any) -> None:
        if not isinstance(base, str) or not base.strip().startswith("http"):
            return
        nb = _ham_norm_base(base)
        if nb in seen:
            return
        seen.add(nb)
        slots.append((slot_id, base.strip()))

    add("primary", rec.get("primaryimageurl"))
    for im in rec.get("images") or []:
        if not isinstance(im, dict):
            continue
        if len(slots) >= _HAM_MAX_SLOTS:
            break
        iid = str(im.get("id") or "").strip() or f"img{len(slots)}"
        for key in ("iiifbaseurl", "baseimageurl", "imageurl"):
            v = im.get(key)
            if isinstance(v, str) and v.strip():
                add(iid, v)
                break
    return slots


def _ham_image_candidates(rec: dict[str, Any]) -> list[str]:
    """单图位候选 URL（兼容旧逻辑 / 无图占位）。"""
    out: list[str] = []
    seen: set[str] = set()
    for _sid, base in _ham_image_slots(rec):
        for url in _ham_iiif_variants(base):
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out[:16]


def _ham_apply_multi_images(
    parsed: dict[str, str],
    urls: list[str],
    paths: list[str],
) -> None:
    n = len(urls)
    parsed["image_count"] = str(n)
    parsed["image_urls"] = _HAM_IMAGE_SEP.join(urls)
    parsed["image_paths"] = _HAM_IMAGE_SEP.join(paths)
    if n:
        parsed["image_url"] = urls[0]
        parsed["image_path"] = paths[0]
    else:
        parsed["image_url"] = ""
        parsed["image_path"] = ""
        parsed["image_urls"] = ""
        parsed["image_paths"] = ""


def _ham_download_all_slot_images(
    sess: Any,
    rec: dict[str, Any],
    img_root: Path,
    oid: str,
    delay: float,
) -> tuple[list[str], list[str], int, int]:
    """按图位下载；返回 (urls, paths, 成功数, 图位数)。"""
    slots = _ham_image_slots(rec)
    if not slots:
        return [], [], 0, 0
    sid = safe_filename_fragment(oid)
    urls: list[str] = []
    paths: list[str] = []
    for idx, (_slot_id, base) in enumerate(slots, 1):
        cands = _ham_iiif_variants(base)
        if not cands:
            continue
        dest = img_root / "harvard" / f"{sid}_{idx}.jpg"
        jitter(delay * 0.35, 0.1, 0.35)
        ok, used = download_image_first(
            sess,
            cands[:6],
            dest,
            iiif_poll_first=2,
            iiif_poll_rest=1,
        )
        if ok and used:
            urls.append(used)
            paths.append(str(Path("images") / "harvard" / f"{sid}_{idx}.jpg"))
    return urls, paths, len(urls), len(slots)


def _ham_row_images_complete(row: dict[str, Any], csv_parent: Path) -> bool:
    """断点：多图字段与本地文件均齐全才跳过。"""
    oid = (row.get("object_id") or "").strip()
    if not oid:
        return False
    try:
        expect = int(str(row.get("image_count") or "0").strip() or "0")
    except ValueError:
        expect = 0
    paths_raw = (row.get("image_paths") or "").strip()
    if expect > 0 and paths_raw:
        parts = [p.strip() for p in paths_raw.split(_HAM_IMAGE_SEP) if p.strip()]
        if len(parts) != expect:
            return False
        for rel in parts:
            disk = csv_parent / rel.replace("\\", "/")
            if not disk.is_file() or disk.stat().st_size < 2048:
                return False
        return True
    # 旧 CSV：仅主图路径
    rel = (row.get("image_path") or "").strip()
    if not rel:
        return False
    disk = csv_parent / rel.replace("\\", "/")
    return disk.is_file() and disk.stat().st_size >= 2048


def _ham_load_seen_ids(out_csv: Path) -> set[str]:
    seen: set[str] = set()
    if not out_csv.exists() or out_csv.stat().st_size == 0:
        return seen
    base = out_csv.parent
    try:
        with open(out_csv, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                oid = (row.get("object_id") or "").strip()
                if oid and _ham_row_images_complete(row, base):
                    seen.add(oid)
    except Exception:
        pass
    return seen


def _ham_period_string(val: Any) -> str:
    """哈佛 API ``period`` 可能为字符串、dict 或 list。"""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        return (val.get("name") or val.get("period") or val.get("displayname") or "").strip()
    if isinstance(val, list):
        parts = [_ham_period_string(x) for x in val]
        return "; ".join(p for p in parts if p)
    return str(val).strip()


def _ham_pick_period(rec: dict[str, Any]) -> tuple[str, str]:
    """
    官网 Date / Period 两行对应 API ``dated`` / ``period``。

    优先使用带朝代的 ``period``（如 ``Tang dynasty, 618-907``），
    仅当 ``period`` 为空时才用 ``dated``（如 ``7th century``）。

    返回 (写入 CSV 的 period 原文, dated 备用)。
    """
    dated = str(rec.get("dated") or "").strip()
    period_api = _ham_period_string(rec.get("period"))
    if period_api:
        return period_api, dated
    return dated, dated


def _ham_fetch_publications(
    sess: Any,
    oid: str,
    api_key: str,
) -> str:
    """列表 API 不含 publications 正文，按 id 补拉参考文献。"""
    try:
        r = retry_get(
            sess,
            f"{HAM_API}/{oid}",
            params={"apikey": api_key, "fields": "publications"},
            timeout=45,
        )
        data = r.json() if r.ok else {}
        return format_harvard_publications(data.get("publications"))
    except Exception as exc:
        log.debug("[HAM] publications %s: %s", oid, exc)
        return ""


def _ham_iiif_base_from_url(url: str) -> str:
    """从已带 /full/... 的 IIIF 图链还原 base。"""
    u = (url or "").strip().rstrip("/")
    u = re.sub(r"/full/[^/]+/\d+/default\.(jpe?g|png|webp)$", "", u, flags=re.I)
    u = re.sub(r"_dynmc$", "", u)
    return u


def _ham_iiif_manifest(rec: dict[str, Any], piu: Optional[str]) -> str:
    """
    写入 ``iiif_manifest_url`` 的可打开链接。

    - ``ids.lib.harvard.edu/mps/`` 仅为 IIIF **Image API**，无 ``/manifest``（浏览器会 404）。
      改为 ``{base}/info.json``；看图请用 ``image_url``（.jpg）。
    - API 若返回真实 presentation manifest 字段则优先使用。
    - ``nrs.harvard.edu`` 仍尝试 ``.../manifest``（部分藏品有效）。
    """
    for im in rec.get("images") or []:
        if not isinstance(im, dict):
            continue
        for key in ("iiifmanifesturl", "manifesturl", "manifest"):
            v = (im.get(key) or "").strip()
            if v.startswith("http"):
                return v
        base = (im.get("iiifbaseurl") or im.get("baseimageurl") or "").strip()
        if not base.startswith("http"):
            continue
        b = base.rstrip("/")
        if "ids.lib.harvard.edu/mps/" in b:
            return b + "/info.json"
        if "nrs.harvard.edu" in b:
            return re.sub(r"_dynmc$", "", b) + "/manifest"
    if not piu:
        return ""
    u = _ham_iiif_base_from_url(piu)
    if not u.startswith("http"):
        return ""
    if "ids.lib.harvard.edu/mps/" in u:
        return u + "/info.json"
    if "nrs.harvard.edu" in u or "iiif" in u.lower():
        return u + "/manifest"
    return ""


def _ham_parse(rec: dict[str, Any]) -> dict[str, str]:
    oid = str(rec.get("id") or "")
    title = (rec.get("title") or "(untitled)").strip()
    period_text, dated_alt = _ham_pick_period(rec)
    period_api = _ham_period_string(rec.get("period"))
    cl = rec.get("classification")
    obj_type = (cl.get("name") if isinstance(cl, dict) else str(cl or "")).strip()
    material = (rec.get("medium") or "").strip()
    desc = (rec.get("description") or "").strip()
    dimensions = (rec.get("dimensions") or "").strip()
    credit = (rec.get("creditline") or "").strip()
    acc = (rec.get("objectnumber") or "").strip()
    detail = (rec.get("url") or "").strip()
    if detail.startswith("http://"):
        detail = "https://" + detail[7:]

    culture_val = rec.get("culture")
    if isinstance(culture_val, list):
        culture = join_listish(culture_val)
    elif isinstance(culture_val, dict):
        culture = culture_val.get("name") or culture_val.get("displayname") or ""
    else:
        culture = str(culture_val or "").strip()

    provenance = str(rec.get("provenance") or "").strip()
    bibliography = format_harvard_publications(rec.get("publications"))

    people = rec.get("people") or []
    artist_names = []
    artist_cultures = []
    for p in people:
        if not isinstance(p, dict):
            continue
        name = p.get("displayname") or p.get("name") or ""
        if name:
            artist_names.append(name)
        c = p.get("culture") or ""
        if c:
            artist_cultures.append(str(c))
    artist = "; ".join(artist_names)

    place_texts = []
    for pl in (rec.get("places") or []):
        if isinstance(pl, dict):
            place_texts.append(pl.get("displayname") or pl.get("name") or "")
    all_text = " ".join(artist_cultures + place_texts + [provenance, desc, culture])
    artist_province = extract_province(all_text)
    # 起止年：先解析带朝代的 period（618-907），再回退 dated（7th century）
    ps, pe = parse_period_years(period_api)
    if ps is None:
        ps, pe = parse_period_years(dated_alt)
    dynasty = resolve_dynasty(period_api or period_text, ps, pe)
    piu = rec.get("primaryimageurl")
    iiif_manifest = _ham_iiif_manifest(rec, piu if isinstance(piu, str) else None)

    partial: dict[str, Any] = {
        "object_id": oid,
        "title": title,
        "artist": artist,
        "artist_province": artist_province,
        "dynasty": dynasty,
        "period": period_text,
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
        "iiif_manifest_url": iiif_manifest,
    }
    return finalize_record(partial, MUSEUM_ID_HARVARD)


def _ham_clear_local_images(img_root: Path, oid: str) -> None:
    """重爬多图前删除该藏品旧主图/多图文件。"""
    sid = safe_filename_fragment(oid)
    folder = img_root / "harvard"
    if not folder.is_dir():
        return
    for p in list(folder.glob(f"{sid}*.jpg")) + list(folder.glob(f"{sid}*.jpg.part")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def repair_harvard_multi_images(
    api_key: str,
    out_csv: Path,
    img_root: Path,
    delay: float = 1.0,
    *,
    limit: int = 0,
    strict_multi: bool = True,
    force_all: bool = False,
) -> tuple[int, int]:
    """
    按 CSV 的 object_id 回拉 API，补全/重爬多图并只更新图片相关列。

    ``force_all=True``：每一行都重拉多图链接（其余 CSV 列不动）。
    """
    if not out_csv.exists() or out_csv.stat().st_size == 0:
        log.warning("[HAM] 补多图：CSV 不存在 %s", out_csv)
        return 0, 0
    with open(out_csv, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return 0, 0

    base = out_csv.parent
    if force_all:
        todo = [r for r in rows if (r.get("object_id") or "").strip()]
        log.info("[HAM] 强制重爬全部多图链接（仅更新图片列，其余字段不动）")
    else:
        todo = [
            r for r in rows
            if (r.get("object_id") or "").strip()
            and not _ham_row_images_complete(r, base)
        ]
    if limit > 0:
        todo = todo[:limit]
    if not todo:
        log.info("[HAM] 补多图：无需处理")
        return len(rows), sum(
            int(str(r.get("image_count") or "0") or "0") for r in rows
        )

    log.info("[HAM] 补多图：待处理 %d / %d 行", len(todo), len(rows))
    sess = make_session()
    sess.headers.setdefault("Referer", "https://www.harvardartmuseums.org/")
    by_id = {(r.get("object_id") or "").strip(): dict(r) for r in rows}
    img_files = 0
    pbar = tqdm(todo, desc="HAM 补多图", unit="条", dynamic_ncols=True)
    try:
        for row in pbar:
            oid = (row.get("object_id") or "").strip()
            try:
                r = retry_get(
                    sess,
                    f"{HAM_API}/{oid}",
                    params={"apikey": api_key, "fields": HAM_FIELDS},
                    timeout=90,
                )
                rec = r.json() if r.ok else {}
            except Exception as exc:
                log.debug("[HAM] 补多图 API 失败 %s: %s", oid, exc)
                continue
            if not rec.get("id"):
                continue
            if force_all:
                _ham_clear_local_images(img_root, oid)
            try:
                urls, paths, ok_n, expect_n = _ham_download_all_slot_images(
                    sess, rec, img_root, oid, delay
                )
            except OSError as exc:
                log.warning("[HAM] 补多图下载异常 %s: %s", oid, exc)
                continue
            if ok_n == 0:
                continue
            if (
                strict_multi
                and not force_all
                and expect_n >= 1
                and ok_n < expect_n
            ):
                log.warning(
                    "[HAM] 补多图未齐 %s：期望 %d 张仅 %d 张，跳过写回",
                    oid, expect_n, ok_n,
                )
                continue
            parsed = _ham_parse(rec)
            _ham_apply_multi_images(parsed, urls, paths)
            by_id[oid].update(
                {k: parsed.get(k, "") for k in (
                    "image_url", "image_urls", "image_path", "image_paths",
                    "image_count", "iiif_manifest_url",
                )}
            )
            img_files += ok_n
            pbar.set_postfix_str(f"图{img_files}", refresh=True)
    finally:
        pbar.close()

    write_csv(out_csv, list(by_id.values()))
    log.info("[HAM] 补多图完成：新落盘约 %d 张", img_files)
    return len(rows), img_files


def crawl_harvard(
    api_key: str,
    out_csv: Path,
    img_root: Path,
    limit: int,
    page_size: int,
    delay: float,
    db_writer: Optional["MySQLWriter"] = None,
    *,
    allow_no_image: bool = False,
    strict_multi: bool = True,
) -> tuple[int, int]:
    sess = make_session()
    sess.headers.setdefault("Referer", "https://www.harvardartmuseums.org/")
    crawl_day = date.today().isoformat()
    img_ok = 0
    page = 1
    rows_batch: list[dict[str, Any]] = []
    total_written = 0

    log.info(
        "[HAM] 开始：culture=Chinese|China, hasimage=1 | 严格多图=%s",
        strict_multi,
    )
    pbar = tqdm(desc="Harvard", unit="条",
                total=limit if limit else None, dynamic_ncols=True)

    seen_ids = _ham_load_seen_ids(out_csv)
    if seen_ids:
        log.info("[HAM] 断点：多图已齐全 ID %d 个", len(seen_ids))

    def flush_h() -> None:
        nonlocal total_written, rows_batch
        if rows_batch:
            append_csv(out_csv, rows_batch, db_writer)
            total_written += len(rows_batch)
            rows_batch = []

    try:
        while True:
            already = total_written + len(rows_batch)
            if limit and already >= limit:
                break
            size = min(page_size, limit - already) if limit else page_size
            params = {
                "apikey": api_key,
                "culture": "Chinese|China",
                "hasimage": 1,
                "size": size,
                "page": page,
                "fields": HAM_FIELDS,
            }
            log.info("[HAM] page=%d size=%d", page, size)
            try:
                r = retry_get(sess, HAM_API, params=params, timeout=90)
            except Exception as exc:
                log.error("[HAM] 请求失败: %s", exc)
                break
            if r.status_code == 401:
                log.error("[HAM] API Key 无效")
                break
            data = r.json()
            records = data.get("records") or []
            if not records:
                break

            n_dup = n_noimg = n_dl_fail = n_partial = 0
            n_ok_page = 0
            for rec in records:
                already = total_written + len(rows_batch)
                if limit and already >= limit:
                    break
                oid = str(rec.get("id") or "")
                if not oid or oid in seen_ids:
                    n_dup += 1
                    continue
                slots = _ham_image_slots(rec)
                if not slots:
                    n_noimg += 1
                    continue
                jitter(delay, 0.2, 0.8)
                urls, paths, ok_n, expect_n = _ham_download_all_slot_images(
                    sess, rec, img_root, oid, delay
                )
                if ok_n == 0:
                    n_dl_fail += 1
                    if allow_no_image:
                        parsed = _ham_parse(rec)
                        parsed["crawl_date"] = crawl_day
                        cands = _ham_image_candidates(rec)
                        parsed["image_url"] = cands[0] if cands else ""
                        _ham_apply_multi_images(parsed, [], [])
                    else:
                        log.info(
                            "[HAM] 跳过 object %s：全部图位下载失败（%d 位）",
                            oid, expect_n,
                        )
                        continue
                elif strict_multi and ok_n < expect_n:
                    n_partial += 1
                    log.info(
                        "[HAM] 跳过 object %s：严格多图未齐（%d/%d 张）",
                        oid, ok_n, expect_n,
                    )
                    continue
                else:
                    parsed = _ham_parse(rec)
                    parsed["crawl_date"] = crawl_day
                    _ham_apply_multi_images(parsed, urls, paths)
                    img_ok += ok_n

                try:
                    pub_n = int(rec.get("publicationcount") or 0)
                except (TypeError, ValueError):
                    pub_n = 0
                if pub_n > 0 and not parsed.get("bibliography"):
                    jitter(delay, 0.1, 0.3)
                    parsed["bibliography"] = _ham_fetch_publications(sess, oid, api_key)

                n_ok_page += 1
                seen_ids.add(oid)
                rows_batch.append(parsed)
                pbar.update(1)
                pbar.set_postfix_str(
                    f"写入{total_written + len(rows_batch)} 图{img_ok}",
                    refresh=True,
                )
                if len(rows_batch) >= 10:
                    flush_h()

            log.info(
                "[HAM] 本页小结 page=%d | API 返回 %d 条 | 本页成功 %d 条 | "
                "跳过重复 %d | 无图位 %d | 全失败 %d | 多图未齐 %d | 累计已写入 %d",
                page, len(records), n_ok_page, n_dup, n_noimg, n_dl_fail, n_partial,
                total_written + len(rows_batch),
            )
            flush_h()
            page += 1
            info = data.get("info") or {}
            if not info.get("next") or len(records) < size:
                break
            jitter(delay, 0.5, 1.5)
    finally:
        flush_h()
        pbar.close()
        log.info("[HAM] 完成：写入 %d 条，图片 %d 张 → %s",
                 total_written, img_ok, out_csv)

    return total_written, img_ok
