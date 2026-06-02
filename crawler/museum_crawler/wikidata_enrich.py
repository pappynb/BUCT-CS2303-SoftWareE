# -*- coding: utf-8 -*-
"""
作者信息增量：Wikidata 实体 + 可选维基百科 REST 摘要。

策略（爬虫优先）
----------------
- **不覆盖**爬虫已写入的 ``artist``、``artist_province``。
- 仅当下列字段为空时，用 Wikidata/维基补全：``artist_wikidata_id``、
  ``artist_birth``、``artist_death``、``artist_bio``、``artist_wikipedia_summary``。
- ``--force`` 只强制重拉上述补全列，仍不改动爬虫作者/籍贯。
- 对 ``artist`` 去重后，每个唯一作者名最多请求一次 Wikidata；检索名跳过藏家/交易商
  （如 ``Freer, Charles Lang``），优先画家段（如 ``San, Lee Chiao; Freer, …`` → ``San, Lee Chiao``）。
- 若某作者各补全列均已非空且未 ``--force``，则跳过该作者的 API 请求。

请遵守 https://wikimediafoundation.org/wiki/Policy:User-Agent 使用独立 UA。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 直接运行本文件时须把上级 ``crawler/`` 加入 path（推荐：``python enrich_wikidata.py``）
if __package__ in (None, ""):
    _crawler_dir = Path(__file__).resolve().parent.parent
    if str(_crawler_dir) not in sys.path:
        sys.path.insert(0, str(_crawler_dir))

import argparse
import csv
import json
import logging
import re
import time
from datetime import date
from typing import Any, Optional
from urllib.parse import quote

import requests
from requests import exceptions as req_exc

from museum_crawler.config import BASE_DIR, CSV_FIELDS, LOG_PATH, setup_logging
from museum_crawler.date_format import normalize_iso_date
from museum_crawler.db import MySQLWriter, mysql_configured
from museum_crawler.io_csv import write_csv

log = logging.getLogger("spider")

WD_API = "https://www.wikidata.org/w/api.php"
WD_UA = (
    "OverseasChineseArtifactCourseCrawler/1.0 "
    "(https://github.com/; educational Wikidata+Wikipedia API)"
)

_MAX_FIELD = 3900

# 爬虫/API 优先，Wikidata 不得覆盖
_CRAWLER_PROTECTED = frozenset({"artist", "artist_province"})

# 仅由 Wikidata 增量写入（空才填）
_WD_SUPPLEMENT_FIELDS = (
    "artist_wikidata_id",
    "artist_birth",
    "artist_death",
    "artist_bio",
    "artist_wikipedia_summary",
)


def _norm_artist_key(raw: str) -> str:
    t = (raw or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t[:400]


def _split_artist_segments(raw: str) -> list[str]:
    """多作者按 ``;``、``、``、``/``、``and`` 切分，保留 ``Last, First`` 内的逗号。"""
    s = (raw or "").strip()
    if not s:
        return []
    parts = re.split(r"\s*(?:;|、|/|\band\b)\s*", s, flags=re.I)
    return [(p or "").strip()[:200] for p in parts if (p or "").strip()]


# 史密森尼等馆藏常见藏家、交易商、机构（整段匹配或子串启发）
_KNOWN_COLLECTOR_DEALER_NORM = frozenset(
    _norm_artist_key(x)
    for x in (
        "Freer, Charles Lang",
        "Charles Lang Freer",
        "Yamanaka and Co.",
        "You, Xiaoxi",
        "Li, Wenqing",
        "Bahr, Abel William",
        "Sai, Loon Gu",
        "Lai-Yuan & Company",
        "C. T. Loo & Company",
        "Riu Cheng Chai",
        "Meyer, Agnes E.",
        "Meyer, Eugene and Agnes E.",
        "Meyer, Eugene I.",
        "Graham, Katharine Meyer",
        "Matsuki, Bunkio",
        "Karlbeck, Orvar",
        "Duanfang",
        "Amoy Lace Guild",
        "Cox, John Hadley",
        "Abe, S.",
        "Ge Shang, Ta",
        "Ge Chung, Ta",
        "buddha",
    )
)

_COMPANY_OR_INSTITUTION = re.compile(
    r"(?:&\s*co\.?|company|guild|gallery|museum|dealer|foundation|trust)\b",
    re.I,
)
_FREER_COLLECTOR = re.compile(
    r"^freer(?:\s*,\s*charles\s+lang|\s+gallery)?\b|charles\s+lang\s+freer",
    re.I,
)


def _segment_is_collector_or_dealer(seg: str) -> bool:
    """藏家、捐赠者、交易商、机构名 — 不作为 Wikidata 作者检索。"""
    s = (seg or "").strip()
    if not s or len(s) < 2:
        return True
    norm = _norm_artist_key(s)
    if norm in _KNOWN_COLLECTOR_DEALER_NORM:
        return True
    if _FREER_COLLECTOR.search(s):
        return True
    if _COMPANY_OR_INSTITUTION.search(s):
        return True
    return False


def _segment_priority(seg: str) -> int:
    """越高越像创作作者（含中文、完整人名）。"""
    score = 0
    if re.search(r"[\u4e00-\u9fff]", seg):
        score += 12
    if re.search(r"[A-Za-z]{2,}", seg):
        score += 4
    if re.search(r",\s*[A-Za-z]", seg):
        score += 2
    if len(seg) >= 8:
        score += 1
    return score


def _pick_artist_search_query(raw: str) -> str:
    """
    从 ``artist`` 字段选出用于 Wikidata 检索的作者名。

    - 仅按分号等切分，不把 ``San, Lee Chiao`` 里的逗号拆开。
    - 跳过 Freer、Yamanaka 等藏家/交易商段，优先画家段。
    - 若全部为藏家/机构，返回空串（不请求 API）。
    """
    segments = _split_artist_segments(raw)
    if not segments:
        return ""
    candidates = [s for s in segments if not _segment_is_collector_or_dealer(s)]
    if not candidates:
        return ""
    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        chosen = max(
            candidates,
            key=lambda s: (_segment_priority(s), -segments.index(s)),
        )
    return _strip_attribution_prefix(chosen)[:200]


# 馆藏常见「归属/风格」前缀，去掉后 Wikidata 搜索更易命中
_ATTRIBUTION_PREFIX = re.compile(
    r"^(?:(?:traditionally\s+)?(?:attributed|formerly\s+attributed|possibly|probably)\s+to|"
    r"style\s+of|circle\s+of|workshop\s+of|follower\s+of|copy\s+of|after|before)\s*:?\s*",
    re.I,
)

# MFA/Harvard 常见作者后缀：``Name (Chinese, 1720–1776)``、``(active 16th century)``
_MUSEUM_AUTHOR_PAREN = re.compile(
    r"\s*\((?:Chinese|Sino-Tibetan|active|born|after)[^)]*\)\s*",
    re.I,
)


def _strip_attribution_prefix(s: str) -> str:
    t = (s or "").strip()
    t = _ATTRIBUTION_PREFIX.sub("", t).strip()
    return t[:200]


def _strip_museum_parenthetical(s: str) -> str:
    """去掉馆方 ``(Chinese, …)`` / ``(active … century)`` 等元数据括号。"""
    t = (s or "").strip()
    prev = None
    while prev != t:
        prev = t
        t = _MUSEUM_AUTHOR_PAREN.sub(" ", t).strip()
    return re.sub(r"\s+", " ", t).strip()[:200]


def _search_query_variants(primary: str) -> list[str]:
    """同一作者多种检索串：去前缀、去括号、原串、中文连续片段。"""
    seen: set[str] = set()
    out: list[str] = []
    base = _strip_attribution_prefix(primary)
    for cand in (
        primary,
        base,
        _strip_museum_parenthetical(base),
        _strip_museum_parenthetical(primary),
    ):
        c = (cand or "").strip()
        if len(c) >= 2 and c not in seen:
            seen.add(c)
            out.append(c)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,12}", primary):
        seg = m.group(0)
        if seg not in seen:
            seen.add(seg)
            out.append(seg)
    return out[:6]


def _wd_get(
    sess: requests.Session,
    *,
    params: dict[str, Any],
    timeout: float = 45.0,
    retries: int = 4,
    backoff: float = 2.5,
) -> requests.Response:
    """Wikidata/维基 GET，对 DNS 失败、超时等做有限次退避重试。"""
    import random

    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            r = sess.get(WD_API, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except (
            req_exc.ConnectionError,
            req_exc.Timeout,
            req_exc.ChunkedEncodingError,
        ) as exc:
            last = exc
            wait = backoff * (attempt + 1) + random.uniform(0, 1.2)
            log.warning(
                "[WD] 网络异常 %d/%d（DNS/超时等），%.1fs 后重试: %s",
                attempt + 1,
                retries,
                wait,
                exc,
            )
            time.sleep(wait)
    if last is not None:
        raise last
    raise RuntimeError("Wikidata 请求失败")


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": WD_UA, "Accept": "application/json"})
    return s


def _is_human_entity(entity: dict[str, Any]) -> bool:
    claims = entity.get("claims") or {}
    for stmt in claims.get("P31") or []:
        snak = (stmt.get("mainsnak") or {})
        if snak.get("snaktype") != "value":
            continue
        tid = (snak.get("datavalue") or {}).get("value", {}).get("id")
        if tid == "Q5":
            return True
    return False


def _format_wikidata_time(time_str: str, precision: int) -> str:
    if not time_str or time_str[0] not in "+-":
        return ""
    body = time_str.lstrip("+-").split("T")[0]
    parts = [p for p in body.split("-") if p != ""]
    if not parts:
        return ""
    if precision <= 9:
        return parts[0]
    if precision == 10 and len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return body


def _claim_best_time(claims: dict[str, Any], pid: str) -> str:
    for stmt in claims.get(pid) or []:
        snak = stmt.get("mainsnak") or {}
        if snak.get("snaktype") != "value":
            continue
        val = (snak.get("datavalue") or {}).get("value")
        if not isinstance(val, dict):
            continue
        t = val.get("time") or ""
        prec = int(val.get("precision", 11))
        s = _format_wikidata_time(t, prec)
        if s:
            return s
    return ""


def _truncate(s: str, maxlen: int = _MAX_FIELD) -> str:
    s = (s or "").strip()
    if len(s) <= maxlen:
        return s
    return s[: maxlen - 1] + "…"


def _wbgetentities(sess: requests.Session, qids: list[str]) -> dict[str, Any]:
    r = _wd_get(
        sess,
        params={
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "props": "labels|descriptions|claims|sitelinks",
            "languages": "en|zh",
            "format": "json",
        },
        timeout=45.0,
    )
    return r.json().get("entities") or {}


def _search_entity_ids(sess: requests.Session, query: str, limit: int = 8) -> list[str]:
    r = _wd_get(
        sess,
        params={
            "action": "wbsearchentities",
            "search": query,
            "language": "en",
            "uselang": "zh",
            "type": "item",
            "format": "json",
            "limit": limit,
        },
        timeout=45.0,
    )
    hits = r.json().get("search") or []
    out: list[str] = []
    for h in hits:
        qid = h.get("id")
        if isinstance(qid, str) and qid.startswith("Q"):
            out.append(qid)
    return out


def _pick_human_qid(sess: requests.Session, query: str) -> Optional[str]:
    for qtry in _search_query_variants(query):
        if len(qtry.strip()) < 2:
            continue
        qids = _search_entity_ids(sess, qtry)
        if not qids:
            continue
        chunk = qids[:5]
        entities = _wbgetentities(sess, chunk)
        for qid in chunk:
            ent = entities.get(qid) or {}
            if _is_human_entity(ent):
                return qid
        return chunk[0]
    return None


def _label_desc(entity: dict[str, Any], lang_pref: str) -> tuple[str, str]:
    labels = entity.get("labels") or {}
    descs = entity.get("descriptions") or {}
    for lang in (lang_pref, "en", "zh"):
        if lang in labels:
            lab = labels[lang].get("value") or ""
            de = (descs.get(lang) or {}).get("value") or ""
            return lab.strip(), de.strip()
    return "", ""


def _wikipedia_summary(sess: requests.Session, lang: str, title: str) -> str:
    if not title:
        return ""
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title, safe='')}"
    for attempt in range(2):
        try:
            r = sess.get(url, timeout=30, headers={"User-Agent": WD_UA})
            if r.status_code != 200:
                return ""
            try:
                data = r.json()
            except json.JSONDecodeError:
                return ""
            return _truncate(str(data.get("extract") or ""), 1200)
        except (req_exc.ConnectionError, req_exc.Timeout) as exc:
            if attempt == 0:
                log.debug("[WD] 维基摘要网络重试: %s", exc)
                time.sleep(1.5)
                continue
            return ""
    return ""


def _wiki_summary_from_entity(
    sess: requests.Session,
    entity: dict[str, Any],
    prefer_zh: bool,
) -> str:
    sl = entity.get("sitelinks") or {}
    order: list[tuple[str, str]] = []
    if prefer_zh:
        order = [("zh", "zhwiki"), ("en", "enwiki")]
    else:
        order = [("en", "enwiki"), ("zh", "zhwiki")]
    for lang, key in order:
        if key in sl:
            title = sl[key].get("title") or ""
            if title:
                ex = _wikipedia_summary(sess, lang, title)
                if ex:
                    return ex
    return ""


def fetch_enrichment_bundle(
    sess: requests.Session,
    artist_query: str,
    *,
    use_wikipedia: bool = True,
    prefer_zhwiki: bool = False,
) -> Optional[dict[str, str]]:
    qid = _pick_human_qid(sess, artist_query)
    if not qid:
        return None
    ent = _wbgetentities(sess, [qid]).get(qid) or {}
    claims = ent.get("claims") or {}
    birth = _claim_best_time(claims, "P569")
    death = _claim_best_time(claims, "P570")
    _lab, desc = _label_desc(ent, "zh" if prefer_zhwiki else "en")
    bio = _truncate(desc or _lab)
    wiki_sum = ""
    if use_wikipedia:
        wiki_sum = _wiki_summary_from_entity(sess, ent, prefer_zhwiki)
    today = date.today().isoformat()
    return {
        "artist_wikidata_id": qid,
        "artist_birth": _truncate(normalize_iso_date(birth), 119),
        "artist_death": _truncate(normalize_iso_date(death), 119),
        "artist_bio": bio,
        "artist_wikipedia_summary": _truncate(wiki_sum, _MAX_FIELD),
        "artist_enriched_at": normalize_iso_date(today),
    }


def _is_blank(val: Any) -> bool:
    return not str(val or "").strip()


def row_needs_wikidata(row: dict[str, Any], *, force: bool) -> bool:
    """该行是否仍需 Wikidata 补全（无作者名则不需要）。"""
    if _is_blank(row.get("artist")):
        return False
    if force:
        return True
    return any(_is_blank(row.get(f)) for f in _WD_SUPPLEMENT_FIELDS)


def merge_enrichment_bundle(
    row: dict[str, Any],
    bundle: dict[str, str],
    *,
    force: bool = False,
) -> bool:
    """
    将 Wikidata 结果合并进一行：保留爬虫字段，只填空缺的补全列。

    返回是否写入了至少一个补全字段。
    """
    changed = False
    for key in _WD_SUPPLEMENT_FIELDS:
        new_val = (bundle.get(key) or "").strip()
        if not new_val:
            continue
        if not force and not _is_blank(row.get(key)):
            continue
        if str(row.get(key) or "").strip() != new_val:
            row[key] = new_val
            changed = True
    if changed:
        row["artist_enriched_at"] = (bundle.get("artist_enriched_at") or date.today().isoformat())
    return changed


def load_rows_merged(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows: list[dict[str, Any]] = []
        for raw in reader:
            row: dict[str, Any] = {}
            for k in CSV_FIELDS:
                v = raw.get(k, "")
                row[k] = "" if v is None else str(v).strip()
            rows.append(row)
    return rows


def enrich_csv_file(
    path: Path,
    *,
    delay: float,
    force: bool,
    use_wikipedia: bool,
    prefer_zhwiki: bool,
    db_writer: Optional[MySQLWriter],
) -> tuple[int, int]:
    """返回 (处理的唯一作者数, 实际发起 Wikidata 请求数)。"""
    rows = load_rows_merged(path)
    if not rows:
        log.warning("[WD] %s 无行数据", path.name)
        return 0, 0

    key_to_indices: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        art = row.get("artist", "").strip()
        if not art:
            continue
        key = _norm_artist_key(art)
        key_to_indices.setdefault(key, []).append(i)

    sess = _make_session()
    n_authors = n_requests = 0
    query_cache: dict[str, Optional[dict[str, str]]] = {}
    for key, indices in sorted(key_to_indices.items(), key=lambda x: x[0]):
        sample = rows[indices[0]]
        if not any(row_needs_wikidata(rows[i], force=force) for i in indices):
            continue
        raw_artist = sample.get("artist", "")
        q = _pick_artist_search_query(raw_artist)
        if not q:
            log.debug(
                "[WD] 跳过藏家/交易商（无可检索作者）: %r",
                raw_artist[:160],
            )
            continue
        if q in query_cache:
            bundle = query_cache[q]
        else:
            try:
                bundle = fetch_enrichment_bundle(
                    sess,
                    q,
                    use_wikipedia=use_wikipedia,
                    prefer_zhwiki=prefer_zhwiki,
                )
                query_cache[q] = bundle
                n_requests += 1
                time.sleep(max(0.8, delay))
            except Exception as exc:
                em = str(exc)
                if "getaddrinfo" in em or "NameResolution" in em or "Failed to resolve" in em:
                    log.warning(
                        "[WD] DNS 无法解析 Wikidata 域名（请检查网络、代理、校园网或稍后再试）artist=%r: %s",
                        q,
                        exc,
                    )
                else:
                    log.warning("[WD] 拉取失败 artist=%r: %s", q, exc)
                time.sleep(delay)
                continue
        if not bundle:
            log.info("[WD] 无匹配实体（已尝试去归属前缀与中文名片段）: %r", q)
            continue
        n_authors += 1
        n_rows_updated = 0
        for idx in indices:
            if merge_enrichment_bundle(rows[idx], bundle, force=force):
                n_rows_updated += 1
        if raw_artist != q:
            log.info(
                "[WD] %s → %s（原字段 %r；合并 %d/%d 行，爬虫 artist/籍贯未改）",
                q,
                bundle["artist_wikidata_id"],
                raw_artist[:100],
                n_rows_updated,
                len(indices),
            )
        else:
            log.info(
                "[WD] %s → %s（合并 %d/%d 行，爬虫 artist/籍贯未改）",
                q,
                bundle["artist_wikidata_id"],
                n_rows_updated,
                len(indices),
            )

    write_csv(path, rows)
    log.info("[WD] 已写回 CSV: %s", path)

    if db_writer:
        chunk = 40
        for i in range(0, len(rows), chunk):
            try:
                db_writer.upsert_batch(rows[i : i + chunk])
            except Exception as exc:
                log.error("[WD] MySQL 同步失败: %s", exc)
                break
        else:
            log.info("[WD] MySQL 已按批次同步 %s", path.name)

    return n_authors, n_requests


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(
        description="按 artist 去重，用 Wikidata（+可选维基摘要）增量补全 CSV",
    )
    ap.add_argument(
        "--csv",
        type=Path,
        nargs="*",
        help="要处理的 CSV（默认：output 下三馆合并文件）",
    )
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=BASE_DIR / "output",
        help="默认 CSV 所在目录（与爬虫 --output 一致）",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=1.2,
        help="每次 Wikidata/维基请求后的间隔（秒），建议 ≥1",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="强制重拉 Wikidata 补全列（仍不覆盖爬虫 artist/artist_province）",
    )
    ap.add_argument(
        "--no-wikipedia",
        action="store_true",
        help="不请求维基百科 REST 摘要（仅 Wikidata 描述与生卒）",
    )
    ap.add_argument(
        "--prefer-zh-wiki",
        action="store_true",
        help="维基摘要优先中文条目",
    )
    ap.add_argument(
        "--no-mysql",
        action="store_true",
        help="不同步 MySQL（仅更新 CSV）",
    )
    args = ap.parse_args()

    paths = list(args.csv) if args.csv else [
        args.output_dir / "smithsonian_institution.csv",
        args.output_dir / "harvard_art_museums.csv",
        args.output_dir / "museum_of_fine_arts_boston.csv",
    ]

    db_writer: Optional[MySQLWriter] = None
    if not args.no_mysql and mysql_configured():
        try:
            db_writer = MySQLWriter.from_env()
            db_writer.ensure_legacy_author_province_renamed()
            db_writer.ensure_missing_csv_columns()
            db_writer.ensure_loosen_overflow_prone_columns()
        except Exception as exc:
            log.warning("[WD] MySQL 不可用，仅写 CSV: %s", exc)
            db_writer = None

    total_authors = total_req = 0
    for p in paths:
        if not p.exists() or p.stat().st_size == 0:
            log.info("[WD] 跳过（不存在或空）: %s", p)
            continue
        a, r = enrich_csv_file(
            p,
            delay=args.delay,
            force=args.force,
            use_wikipedia=not args.no_wikipedia,
            prefer_zhwiki=args.prefer_zh_wiki,
            db_writer=db_writer,
        )
        total_authors += a
        total_req += r

    log.info("[WD] 完成：唯一作者补全 %d 人，HTTP 轮次约 %d", total_authors, total_req)
    log.info("[WD] 日志：%s", LOG_PATH)


if __name__ == "__main__":
    main()
