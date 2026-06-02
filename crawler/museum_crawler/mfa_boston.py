# -*- coding: utf-8 -*-
"""
波士顿美术馆 collections.mfa.org（eMuseum）爬虫。

默认用 Playwright（Chromium）过 AWS WAF；requests 仅作备用。
字段经 finalize_record 与史密森尼/哈佛对齐（config.CSV_FIELDS）。
"""

from __future__ import annotations

import csv
import logging
import re
import time
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from tqdm import tqdm

from museum_crawler.config import MUSEUM_ID_MFA_BOSTON
from museum_crawler.record_build import finalize_record
from museum_crawler.http_client import (
    UA_POOL,
    _body_looks_like_html,
    _safe_unlink,
    download_image_first,
    ext_from_url,
    jitter,
    make_session,
    retry_get,
    safe_filename_fragment,
)
from museum_crawler.io_csv import append_csv, write_csv
from museum_crawler.text_geography import (
    extract_province,
    parse_period_years,
    resolve_dynasty,
)

if TYPE_CHECKING:
    from museum_crawler.db import MySQLWriter

log = logging.getLogger("spider")

MFA_BASE = "https://collections.mfa.org"
MFA_LIST_URLS = [
    # 主入口：与你浏览器一致的 CHINESE 文化检索（覆盖面最大）
    f"{MFA_BASE}/search/Objects/cultures%3ACHINESE/*",
    # 兼容别名路径（部分页面/地区会回落到这些 URL）
    f"{MFA_BASE}/search/Objects/culture%3AChinese/*/images",
    f"{MFA_BASE}/search/Objects/culture%3AChina/*/images",
    # 馆方专题页作为补充来源
    f"{MFA_BASE}/collections/314122/chinese-art/objects/images",
    f"{MFA_BASE}/collections/449580/chinese-collection-highlights/objects/images",

]

_OBJECT_HREF_RE = re.compile(r"/objects?/\d+", re.I)
_DISPATCHER_PREVIEW_RE = re.compile(
    r"/internal/media/dispatcher/(\d+)/preview",
    re.I,
)
_MFA_DETAIL_IMAGE_WAIT_MS = 30_000
# MFA dispatcher 常 202+HTML，需多轮等待；补图时适当加长
_MFA_IMAGE_POLL_MAX = 5
_MFA_PLAYWRIGHT_POLL_MAX = 8

# 列表页 WAF：尚无链接时多等几次；已有链接则短等后直接开始爬详情
_MFA_LIST_WAIT_FULL_S = 75.0
_MFA_LIST_WAIT_QUICK_S = 22.0
_MFA_LIST_ATTEMPTS_FULL = 3
_MFA_LIST_ATTEMPTS_QUICK = 1


def _mfa_html_has_objects(html: str) -> bool:
    return bool(_OBJECT_HREF_RE.search(html))


def _mfa_safe_page_content(page: Any) -> str:
    """读取页面 HTML；导航瞬间异常时返回空串，避免中断全流程。"""
    try:
        return page.content() or ""
    except Exception:
        return ""


def _mfa_launch_browser(pw: Any, *, headless: bool) -> Any:
    """启动 Chromium；无头模式常被 MFA 的 AWS WAF 拦截。

    优先 Playwright 内置 Chromium；若未下载则回退本机 Chrome / Edge。
    """
    launch_kw = {
        "headless": headless,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    }
    last_err: Exception | None = None
    for channel in (None, "chrome", "msedge"):
        try:
            if channel is None:
                return pw.chromium.launch(**launch_kw)
            return pw.chromium.launch(**launch_kw, channel=channel)
        except Exception as e:
            last_err = e
            if channel is None:
                log.warning("[MFA] Playwright Chromium 不可用，尝试本机浏览器…")
            continue
    raise RuntimeError(
        "无法启动浏览器：请运行 playwright install chromium，"
        "或确保本机已安装 Google Chrome / Microsoft Edge"
    ) from last_err


def _mfa_new_context(browser: Any) -> Any:
    return browser.new_context(
        user_agent=UA_POOL[0],
        viewport={"width": 1366, "height": 900},
        locale="en-US",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )


def _mfa_wait_objects_on_page(page: Any, timeout_ms: int = 120_000) -> bool:
    """等待 AWS WAF 通过，列表页出现 /objects/ 链接（有界面约 15–30s）。"""
    try:
        page.wait_for_function(
            "() => document.body && /\\/objects?\\/\\d+/i.test(document.body.innerHTML)",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return _mfa_html_has_objects(_mfa_safe_page_content(page))


def _link_cap_for_limit(limit: int) -> int:
    """有 --limit 时只扫够用的链接数，避免深翻列表反复等 WAF。"""
    if limit <= 0:
        return 0
    return limit + max(10, limit // 4)


def _mfa_iter_list_pages(max_pages_per_list: int):
    """
    列表页码迭代。``max_pages_per_list <= 0`` 表示不设上限，直至某页无新链接为止。
    （MFA CHINESE 检索约 762 页 / 9000+ 条，旧版硬编码 40 页约 480 条。）
    """
    if max_pages_per_list > 0:
        return range(1, max_pages_per_list + 1)
    page_no = 1
    while True:
        yield page_no
        page_no += 1


def _mfa_use_all_list_urls(limit: int, link_cap: int) -> bool:
    """小批量提速可只扫首个列表；中/大批量必须扫全部列表来源。"""
    if link_cap <= 0:
        return True
    if limit <= 0:
        return True
    return limit > 60


def _mfa_load_list_page(
    page: Any,
    url: str,
    *,
    max_wait_s: float = 90,
    max_attempts: int = 3,
    on_waiting: Optional[Any] = None,
) -> Optional[str]:
    """打开列表页并轮询直至出现藏品链接；失败时按 max_attempts 刷新重试。"""
    attempts = max(1, int(max_attempts))
    for attempt in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as exc:
            log.warning("[MFA] 打开列表失败 (%d/3): %s", attempt + 1, exc)
            continue
        deadline = time.time() + max_wait_s
        last_log = 0.0
        while time.time() < deadline:
            html = _mfa_safe_page_content(page)
            if not html:
                page.wait_for_timeout(1200)
                continue
            if _mfa_html_has_objects(html):
                return html
            title = (page.title() or "").lower()
            elapsed = int(time.time() - (deadline - max_wait_s))
            if time.time() - last_log >= 8:
                log.info(
                    "[MFA] 等待 WAF/列表加载… %ds | %s",
                    elapsed,
                    url.split("page=")[-1][:20] if "page=" in url else url[-40:],
                )
                last_log = time.time()
                if on_waiting:
                    on_waiting(elapsed)
            if "human verification" in title:
                page.wait_for_timeout(2000)
                continue
            if _mfa_wait_objects_on_page(page, timeout_ms=5_000):
                html_after_wait = _mfa_safe_page_content(page)
                if html_after_wait:
                    return html_after_wait
            page.wait_for_timeout(2000)
        if attempt < attempts - 1:
            log.info("[MFA] WAF 未通过，刷新重试 (%d/%d)…", attempt + 2, attempts)
            try:
                page.reload(wait_until="domcontentloaded", timeout=90_000)
            except Exception:
                pass
    return None


def _mfa_wait_detail_on_page(page: Any, timeout_ms: int = 60_000) -> bool:
    """详情页：等 og:image 或主图出现（过 WAF 后才有）。"""
    try:
        page.wait_for_function(
            """() => {
              const og = document.querySelector("meta[property='og:image']");
              if (og && og.content) return true;
              const img = document.querySelector(
                ".object-image img, #mainimage img, .primary-image img"
              );
              return !!(img && (img.src || img.dataset.src));
            }""",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        html = _mfa_safe_page_content(page)
        return not _mfa_html_blocked(html) and "og:image" in html.lower()


def _mfa_settle_detail_page(page: Any, delay: float) -> None:
    """主图节点出现且 naturalHeight 就绪后再稍等，避免截图/下载半张图。"""
    try:
        page.wait_for_selector(
            "meta[property='og:image'], .object-image img, #mainimage img",
            timeout=12_000,
        )
    except Exception:
        pass
    try:
        page.wait_for_function(
            """() => {
              const sels = [
                '.object-image img', '#mainimage img', '.itemimage img',
                "img[itemprop='image']", '.primary-image img'
              ];
              for (const sel of sels) {
                const img = document.querySelector(sel);
                if (!img) continue;
                if (img.complete && img.naturalWidth >= 120 && img.naturalHeight >= 120)
                  return true;
              }
              const og = document.querySelector("meta[property='og:image']");
              return !!(og && og.content);
            }""",
            timeout=18_000,
        )
    except Exception:
        pass
    time.sleep(min(max(delay, 0.5), 2.0))


def _mfa_wait_main_image_loaded(page: Any, *, timeout_ms: int = 20_000) -> bool:
    try:
        page.wait_for_function(
            """() => {
              const sels = [
                '.object-image img', '#mainimage img', '.itemimage img',
                "img[itemprop='image']", '.primary-image img'
              ];
              for (const sel of sels) {
                const img = document.querySelector(sel);
                if (!img) continue;
                if (img.complete && img.naturalWidth >= 120 && img.naturalHeight >= 120)
                  return true;
              }
              return false;
            }""",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def _html_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _mfa_html_blocked(html: str) -> bool:
    """无藏品链接且像 WAF/空壳页时视为拦截（有 /objects/ 则一律视为通过）。"""
    if not html or len(html.strip()) < 100:
        return True
    if _mfa_html_has_objects(html):
        return False
    lower = html[:16000].lower()
    if "human verification" in lower and len(html) < 20_000:
        return True
    if "access denied" in lower:
        return True
    if "<a " not in lower and len(html) < 12_000:
        return True
    return False


def _mfa_page_blocked(resp: Any) -> bool:
    if resp is None:
        return True
    code = getattr(resp, "status_code", 0) or 0
    if code in (403, 405, 429, 503):
        return True
    text = getattr(resp, "text", None) or ""
    if not text.strip() and code in (202, 204):
        return True
    return _mfa_html_blocked(text)


def _mfa_links_from_html(html: str, seen: set[str]) -> list[str]:
    out: list[str] = []
    soup = _html_soup(html)
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if _OBJECT_HREF_RE.search(href):
            full = urljoin(MFA_BASE, href.split(";")[0].split("?")[0])
            if full not in seen:
                seen.add(full)
                out.append(full)
    return out


def _mfa_collect_links_requests(
    sess: Any, delay: float, *, max_pages_per_list: int = 0
) -> list[str]:
    sess.headers.setdefault("Referer", "https://www.mfa.org/collections/")
    seen: set[str] = set()
    links: list[str] = []
    pbar = tqdm(desc="MFA 收集链接(requests)", unit="页", dynamic_ncols=True)
    try:
        for base_url in MFA_LIST_URLS:
            for page_no in _mfa_iter_list_pages(max_pages_per_list):
                sep = "&" if "?" in base_url else "?"
                url = f"{base_url}{sep}page={page_no}"
                try:
                    r = retry_get(sess, url, timeout=45, retries=2)
                except Exception as exc:
                    log.debug("[MFA] list 失败 p%d: %s", page_no, exc)
                    break
                if _mfa_page_blocked(r):
                    log.warning(
                        "[MFA] requests 被 WAF 拦截 HTTP %s: %s",
                        getattr(r, "status_code", "?"),
                        url[:90],
                    )
                    break
                found = _mfa_links_from_html(r.text, seen)
                links.extend(found)
                pbar.update(1)
                pbar.set_postfix_str(f"已收集{len(links)}", refresh=True)
                if not found:
                    break
                jitter(delay, 0.2, 0.5)
    finally:
        pbar.close()
    return links


def _mfa_collect_links_on_page(
    page: Any,
    delay: float,
    *,
    crawl_limit: int = 0,
    max_pages_per_list: int = 0,
    link_cap: int = 0,
) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    pbar = tqdm(desc="MFA 收集链接", unit="页", dynamic_ncols=True)
    if max_pages_per_list > 0:
        log.info("[MFA] 每个列表源最多翻 %d 页", max_pages_per_list)
    else:
        log.info("[MFA] 列表翻页无上限（直至某页无新链接；CHINESE 检索约 762 页）")

    def _on_wait(sec: int) -> None:
        pbar.set_postfix_str(f"过WAF {sec}s 已{len(links)}", refresh=True)

    try:
        try:
            page.goto(
                "https://www.mfa.org/collections",
                wait_until="domcontentloaded",
                timeout=90_000,
            )
            page.wait_for_timeout(2500)
        except Exception:
            pass
        list_urls = (
            MFA_LIST_URLS
            if _mfa_use_all_list_urls(crawl_limit, link_cap)
            else MFA_LIST_URLS[:1]
        )
        for base_url in list_urls:
            for page_no in _mfa_iter_list_pages(max_pages_per_list):
                if link_cap and len(links) >= link_cap:
                    log.info(
                        "[MFA] 已收集 %d 条链接（达到 limit 余量），停止翻页",
                        len(links),
                    )
                    break
                sep = "&" if "?" in base_url else "?"
                url = f"{base_url}{sep}page={page_no}"
                pbar.set_postfix_str(f"p{page_no} 已{len(links)}", refresh=True)
                have_links = len(links) > 0
                try:
                    html = _mfa_load_list_page(
                        page,
                        url,
                        max_wait_s=(
                            _MFA_LIST_WAIT_QUICK_S
                            if have_links
                            else _MFA_LIST_WAIT_FULL_S
                        ),
                        max_attempts=(
                            _MFA_LIST_ATTEMPTS_QUICK
                            if have_links
                            else _MFA_LIST_ATTEMPTS_FULL
                        ),
                        on_waiting=_on_wait,
                    )
                except Exception as exc:
                    log.warning("[MFA] 列表页读取异常，跳过该页继续: %s", exc)
                    if have_links:
                        log.warning(
                            "[MFA] 已有 %d 条链接，停止翻页并开始下载详情",
                            len(links),
                        )
                        return links
                    continue
                if not html:
                    if have_links:
                        log.warning(
                            "[MFA] 列表页长时间未加载（已有 %d 条链接），"
                            "停止收集并开始下载详情: %s",
                            len(links),
                            url[:90],
                        )
                        return links
                    log.warning("[MFA] 列表页未通过 WAF: %s", url[:90])
                    break
                found = _mfa_links_from_html(html, seen)
                links.extend(found)
                pbar.update(1)
                pbar.set_postfix_str(f"已收集{len(links)}", refresh=True)
                if not found:
                    break
                if link_cap and len(links) >= link_cap:
                    break
                jitter(delay, 0.3, 0.8)
            if link_cap and len(links) >= link_cap:
                break
    finally:
        pbar.close()
    log.info("[MFA] 共收集 %d 个对象链接", len(links))
    return links


def _mfa_collect_links_playwright(
    delay: float,
    *,
    crawl_limit: int = 0,
    headless: bool = False,
    max_pages_per_list: int = 0,
    link_cap: int = 0,
) -> tuple[list[str], Any, Any, Any]:
    """返回 (links, playwright, browser, context) 供后续详情复用同一会话。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "MFA 需要 Playwright：pip install playwright && playwright install chromium"
        ) from exc

    pw = sync_playwright().start()
    browser = _mfa_launch_browser(pw, headless=headless)
    context = _mfa_new_context(browser)
    page = context.new_page()
    if headless:
        log.info("[MFA] 无头模式（若失败将自动切换有界面浏览器）…")
    else:
        log.info("[MFA] 有界面浏览器过 WAF（首次列表约需 15–30 秒，请勿关闭窗口）…")
    links = _mfa_collect_links_on_page(
        page,
        delay,
        crawl_limit=crawl_limit,
        max_pages_per_list=max_pages_per_list,
        link_cap=link_cap,
    )
    return links, pw, browser, context, page


def _sync_playwright_cookies_to_session(context: Any, sess: Any) -> None:
    try:
        for c in context.cookies():
            sess.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain") or "",
                path=c.get("path") or "/",
            )
    except Exception:
        pass


def _mfa_write_image_bytes(dest: Path, body: bytes, *, min_bytes: int = 2048) -> bool:
    if not body or len(body) < min_bytes:
        return False
    if _body_looks_like_html(body[:400]):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return _mfa_validate_saved_image(dest, min_bytes=min_bytes)


def _mfa_validate_saved_image(dest: Path, *, min_bytes: int = 2048) -> bool:
    """
    拒绝 HTML 伪装、过小文件，以及「上半有图、下半大面积纯白」的半张图。
    """
    if not dest.is_file() or dest.stat().st_size < min_bytes:
        return False
    head = dest.read_bytes()[:400]
    if _body_looks_like_html(head):
        return False
    try:
        from PIL import Image
    except ImportError:
        return True

    try:
        with Image.open(dest) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w < 80 or h < 80:
                log.warning("[MFA] 图片尺寸过小 %s (%dx%d)", dest.name, w, h)
                return False
            band = max(8, h // 8)
            top = im.crop((0, 0, w, band))
            mid = im.crop((0, h // 2 - band // 2, w, h // 2 + band // 2))
            bot = im.crop((0, h - band, w, h))

            def _mean_px(region) -> float:
                data = list(region.getdata())
                if not data:
                    return 255.0
                return sum(sum(p) / 3.0 for p in data) / len(data)

            top_m, mid_m, bot_m = _mean_px(top), _mean_px(mid), _mean_px(bot)
            # 典型半张图：中上区域有内容，底部近纯白
            if bot_m > 248 and top_m < 235 and (bot_m - mid_m) > 25:
                log.warning(
                    "[MFA] 图片疑似未加载完整（底部留白）%s top=%.0f mid=%.0f bot=%.0f",
                    dest.name,
                    top_m,
                    mid_m,
                    bot_m,
                )
                return False
            # 网页整页截图：顶部黑/灰条 + 下方内容（顶暗底亮且差异大）
            if top_m < 90 and mid_m > 160 and bot_m > 160 and h > w * 1.1:
                log.warning("[MFA] 图片疑似页面截图而非藏品图 %s", dest.name)
                return False
    except Exception as exc:
        log.debug("[MFA] 图片校验跳过 %s: %s", dest.name, exc)
        return True
    return True


def _mfa_image_url_variants(url: str) -> list[str]:
    """原 URL + 至多一个 dispatcher 备选（preview/full），不展开 IIIF 多尺寸。"""
    u = (url or "").strip()
    if not u:
        return []
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str) -> None:
        x = urljoin(MFA_BASE, raw.strip()) if raw.strip() else ""
        if x and x not in seen:
            seen.add(x)
            out.append(x)

    add(u)
    m = _DISPATCHER_PREVIEW_RE.search(u)
    if m:
        mid = m.group(1)
        base = f"{MFA_BASE}/internal/media/dispatcher/{mid}"
        if "/preview" in u.lower():
            add(f"{base}/full")
        elif "/full" not in u.lower():
            add(f"{base}/preview")
    return out


def _mfa_order_image_urls(urls: list[str]) -> list[str]:
    """非 dispatcher 直链优先；dispatcher 同一 media id 先 /full 再 /preview。"""
    direct: list[str] = []
    by_mid: dict[str, str] = {}
    seen: set[str] = set()
    for raw in urls:
        u = (raw or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        m = _DISPATCHER_PREVIEW_RE.search(u)
        if m:
            mid = m.group(1)
            if mid not in by_mid:
                by_mid[mid] = u
        else:
            direct.append(u)
    dispatcher: list[str] = []
    for mid in sorted(by_mid.keys()):
        variants = _mfa_image_url_variants(by_mid[mid])
        fulls = [v for v in variants if "/full" in v.lower()]
        rest = [v for v in variants if v not in fulls]
        for v in fulls + rest:
            if v not in seen:
                seen.add(v)
                dispatcher.append(v)
    return direct + dispatcher


def _mfa_screenshot_main_image(page: Any, dest: Path, *, min_bytes: int = 2048) -> bool:
    """最后手段：仅截主图 img 节点（非整页），且须等 naturalHeight 就绪。"""
    _mfa_wait_main_image_loaded(page, timeout_ms=25_000)
    selectors = (
        ".object-image img",
        "#mainimage img",
        ".itemimage img",
        "img[itemprop='image']",
        ".primary-image img",
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.scroll_into_view_if_needed(timeout=10_000)
            loc.screenshot(path=str(dest), timeout=45_000)
            if _mfa_validate_saved_image(dest, min_bytes=min_bytes):
                return True
            _safe_unlink(dest)
        except Exception as exc:
            log.debug("[MFA] 主图截图失败 %s: %s", sel, exc)
            _safe_unlink(dest)
    return False


def _mfa_merge_image_candidates(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for raw in group:
            for v in _mfa_image_url_variants(raw):
                if v not in seen:
                    seen.add(v)
                    out.append(v)
    return out


def _mfa_image_urls_from_html(html: str, primary: str) -> list[str]:
    found: list[str] = [primary] if primary else []
    if not html:
        return _mfa_merge_image_candidates(found)
    soup = _html_soup(html)
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        found.append(og["content"].strip())
    for img in soup.select(
        ".object-image img, #mainimage img, .itemimage img, "
        "img[itemprop='image'], .primary-image img"
    ):
        for attr in ("src", "data-src", "data-full-src", "data-original"):
            v = img.get(attr)
            if v:
                found.append(v.strip())
    return _mfa_merge_image_candidates(found)


def _mfa_image_urls_from_page(page: Any) -> list[str]:
    try:
        raw = page.evaluate(
            """() => {
              const out = [];
              const push = (u) => {
                if (u && typeof u === 'string' && !u.startsWith('data:'))
                  out.push(u);
              };
              const og = document.querySelector("meta[property='og:image']");
              if (og && og.content) push(og.content);
              document.querySelectorAll('img').forEach((img) => {
                push(img.currentSrc);
                push(img.src);
                ['data-src','data-full-src','data-original'].forEach((a) => {
                  push(img.getAttribute(a));
                });
              });
              return out;
            }"""
        )
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return _mfa_merge_image_candidates([str(x) for x in raw if x])


def _mfa_download_image_page_fetch(
    page: Any,
    urls: list[str],
    dest: Path,
    *,
    max_polls: int = _MFA_PLAYWRIGHT_POLL_MAX,
) -> bool:
    """详情页已打开时，用同页 Cookie + Referer 走 Playwright request 下图。"""
    referer = ""
    try:
        referer = page.url or ""
    except Exception:
        pass
    return _mfa_download_image_playwright(
        page.context,
        urls,
        dest,
        referer=referer,
        max_polls=max_polls,
    )


def _mfa_download_image_playwright(
    context: Any,
    urls: list[str],
    dest: Path,
    *,
    referer: str = "",
    max_polls: int = _MFA_PLAYWRIGHT_POLL_MAX,
) -> bool:
    """用 Playwright 请求上下文（含 WAF Cookie）拉图；202 仅短等一次。"""
    headers: dict[str, str] = {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer
    req = context.request
    for img_url in urls:
        for attempt in range(max_polls):
            try:
                resp = req.get(img_url, headers=headers, timeout=60_000)
            except Exception as exc:
                log.debug("[MFA] Playwright 下图失败 %s: %s", img_url[:90], exc)
                break
            status = resp.status
            ctype = (resp.headers.get("content-type") or "").lower()
            body = resp.body()
            pending = status == 202 or (
                "text/html" in ctype and len(body) < 80_000
            )
            if pending:
                if attempt < max_polls - 1:
                    time.sleep(min(2.0 * (attempt + 1), 10.0))
                    continue
                break
            if status >= 400:
                break
            if _mfa_write_image_bytes(dest, body):
                return True
            break
    return False


def _mfa_download_record_image(
    *,
    image_url: str,
    html: str,
    detail_url: str,
    dest: Path,
    img_sess: Any,
    context: Optional[Any] = None,
    page: Optional[Any] = None,
) -> bool:
    """
    落盘一张有效图：优先 Playwright/requests 拉真实图片 URL；
    页面元素截图仅作最后手段（避免半张图、带网页 UI）。
    """
    candidates = _mfa_order_image_urls(
        _mfa_merge_image_candidates(
            _mfa_image_urls_from_html(html, image_url),
            _mfa_image_urls_from_page(page) if page is not None else [],
        )
    )
    if not candidates:
        return False

    def _try_ok() -> bool:
        return dest.is_file() and _mfa_validate_saved_image(dest)

    _safe_unlink(dest)

    if page is not None:
        if _mfa_download_image_page_fetch(
            page, candidates, dest, max_polls=_MFA_PLAYWRIGHT_POLL_MAX
        ) and _try_ok():
            return True
        _safe_unlink(dest)
    if context is not None:
        _sync_playwright_cookies_to_session(context, img_sess)
        if _mfa_download_image_playwright(
            context,
            candidates,
            dest,
            referer=detail_url,
            max_polls=_MFA_PLAYWRIGHT_POLL_MAX,
        ) and _try_ok():
            return True
        _safe_unlink(dest)

    ok, _used = download_image_first(
        img_sess,
        candidates,
        dest,
        iiif_poll_first=_MFA_IMAGE_POLL_MAX,
        iiif_poll_rest=_MFA_IMAGE_POLL_MAX,
        timeout=90.0,
    )
    if ok and _try_ok():
        return True
    _safe_unlink(dest)

    if page is not None and _mfa_screenshot_main_image(page, dest) and _try_ok():
        return True
    _safe_unlink(dest)
    return False


_MFA_UI_LINE_RE = re.compile(
    r"^(?:×|✓|choose collection|create new collection|copy link|thanks for sharing!|"
    r"find any service|addtoany|more…|more\.\.\.|share|add to any)$",
    re.I,
)
_MFA_SECTION_STOP_RE = re.compile(
    r"^(?:Provenance|Publication History|Credit Line|Accession|Classification|"
    r"Medium|Dimensions?|Date|Culture|Country|Artist|Maker|Creator|Bibliography|"
    r"Collections?|Exhibition History|Label|Department|Description|Objects?)\s*:?\s*$",
    re.I,
)


def _mfa_detail_soup(soup: BeautifulSoup) -> BeautifulSoup:
    """尽量只保留藏品详情主区域，并剔除分享/收藏夹等 UI 节点。"""
    root = soup
    for sel in (
        "#emuseum",
        ".detail-view",
        ".object-detail",
        "#main-content",
        "main",
        "#content",
    ):
        el = soup.select_one(sel)
        if el:
            root = el
            break
    for tag in root.select(
        "script, style, nav, footer, iframe, "
        "[class*='addtoany'], [id*='addtoany'], [class*='a2a_'], "
        "[class*='share'], [class*='collection-widget']"
    ):
        tag.decompose()
    return root


def _mfa_flat_text(soup: BeautifulSoup) -> str:
    return _mfa_detail_soup(soup).get_text("\n", strip=True)


def _mfa_clean_field_value(val: str, *, max_len: int = 2000, multiline: bool = False) -> str:
    if not val:
        return ""
    parts: list[str] = []
    for line in val.splitlines():
        t = line.strip()
        if not t or _MFA_UI_LINE_RE.match(t):
            continue
        parts.append(t)
    if multiline:
        out = "\n".join(parts)
    else:
        out = " ".join(parts)
    if len(out) > max_len:
        out = out[: max_len - 1] + "…"
    return out


def _mfa_normalize_detail_url(url: str) -> str:
    u = (url or "").strip().split(";")[0].split("?")[0].rstrip("/")
    return u


def _mfa_detail_field_value(
    soup: BeautifulSoup, *class_fragments: str, multiline: bool = False
) -> str:
    """读取 ``.detailField.{frag} .detailFieldValue``（避免 ``webDescriptionField`` 误匹配）。"""
    for frag in class_fragments:
        el = soup.select_one(f".detailField.{frag} .detailFieldValue")
        if el:
            return _mfa_clean_field_value(
                el.get_text("\n", strip=True), multiline=multiline
            )
    return ""


def _mfa_detail_plain_block(soup: BeautifulSoup, *class_fragments: str) -> str:
    """读取无 ``detailFieldValue`` 的块（peopleField/cultureField/periodField 等）。"""
    for frag in class_fragments:
        block = soup.select_one(f".detailField.{frag}")
        if not block:
            continue
        val = block.select_one(".detailFieldValue")
        if val:
            return _mfa_clean_field_value(val.get_text("\n", strip=True))
        label = block.select_one(".detailFieldLabel")
        text = block.get_text("\n", strip=True)
        if label:
            text = text.replace(label.get_text(strip=True), "", 1).strip()
        h2 = block.select_one("h2")
        if h2:
            text = text.replace(h2.get_text(strip=True), "", 1).strip()
        return _mfa_clean_field_value(text)
    return ""


def _mfa_title_from_soup(soup: BeautifulSoup) -> str:
    h2 = soup.select_one(".detailField.titleField h2")
    if h2:
        t = h2.get_text(strip=True)
        if t:
            return t
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        t = og["content"].strip()
        if t:
            return t
    if soup.title and soup.title.string:
        m = re.match(r"^(.+?)\s*[–—-]\s*Works\b", soup.title.string.strip())
        if m:
            return m.group(1).strip()
    return ""


def _mfa_numeric_id_from_url(url: str) -> str:
    m = re.search(r"/objects/(\d+)(?:/|$)", url or "")
    return m.group(1) if m else ""


def _mfa_parse_metadata_fields(soup: BeautifulSoup, url: str) -> dict[str, str]:
    """从 eMuseum ``detailField`` 块提取结构化元数据。"""
    title = _mfa_title_from_soup(soup)
    acc = _mfa_detail_field_value(soup, "invnolineField")
    artist = _mfa_detail_plain_block(soup, "peopleField")
    culture = _mfa_detail_plain_block(soup, "cultureField")
    dynasty_raw = _mfa_detail_plain_block(soup, "periodField")
    date_raw = _mfa_detail_plain_block(soup, "displayDateField")
    period_parts = [p for p in (dynasty_raw, date_raw) if p]
    period = ", ".join(period_parts)
    material = _mfa_detail_field_value(soup, "mediumField")
    obj_type = _mfa_detail_field_value(soup, "classificationsField")
    dims = _mfa_detail_field_value(soup, "dimensionsField", multiline=True)
    credit = _mfa_detail_field_value(soup, "creditlineField")
    desc = _mfa_detail_field_value(soup, "descriptionField", multiline=True)
    provenance = _mfa_detail_field_value(soup, "provenanceField", multiline=True)
    bibliography = _mfa_detail_field_value(
        soup, "publicationHistoryField", "bibliographyField", multiline=True
    )

    ps, pe = parse_period_years(period or dynasty_raw or date_raw)
    dynasty = resolve_dynasty(dynasty_raw or period, ps, pe)

    numeric_id = _mfa_numeric_id_from_url(url)
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    oid = acc or numeric_id or slug

    if not desc:
        geo = _mfa_detail_plain_block(soup, "objectGeographyField")
        if geo and not geo.lower().startswith("object place"):
            desc = geo
        elif geo:
            desc = re.sub(r"^Object Place:\s*", "", geo, flags=re.I).strip()

    return {
        "object_id": oid,
        "title": title or "(untitled)",
        "artist": artist,
        "culture": culture or "Chinese",
        "dynasty": dynasty,
        "period": period,
        "period_start_year": str(ps) if ps is not None else "",
        "period_end_year": str(pe) if pe is not None else "",
        "type": obj_type,
        "material": material,
        "description": desc,
        "provenance": provenance,
        "bibliography": bibliography,
        "dimensions": dims,
        "credit_line": credit,
        "accession_number": acc,
    }


def _mfa_extract_section(text: str, header: str, *, max_lines: int = 10) -> str:
    """取 ``Header`` 与下一栏目之间的正文，跳过分享组件行。"""
    lines = text.splitlines()
    header_pat = re.compile(rf"^{re.escape(header)}\s*:?\s*$", re.I)
    buf: list[str] = []
    in_section = False
    for line in lines:
        t = line.strip()
        if not t:
            if in_section and buf:
                break
            continue
        if _MFA_UI_LINE_RE.match(t):
            continue
        if header_pat.match(t):
            in_section = True
            continue
        if in_section and _MFA_SECTION_STOP_RE.match(t):
            break
        if in_section:
            buf.append(t)
            if len(buf) >= max_lines:
                break
    return _mfa_clean_field_value("\n".join(buf))


def _mfa_parse_detail_html(html: str, url: str) -> Optional[dict[str, Any]]:
    if _mfa_html_blocked(html):
        return None
    soup = _html_soup(html)
    root = _mfa_detail_soup(soup)

    img_url = ""
    og = root.find("meta", property="og:image")
    if og and og.get("content"):
        img_url = og["content"].strip()
        img_url = re.sub(r"[?&]width=\d+", "", img_url)
        img_url = re.sub(r"[?&]height=\d+", "", img_url).rstrip("?&")
    if not img_url:
        for sel in (
            "img[itemprop='image']",
            ".object-image img",
            "#mainimage img",
            ".itemimage img",
            ".primary-image img",
            ".download-image img",
        ):
            el = root.select_one(sel)
            if el:
                src = el.get("data-src") or el.get("src") or ""
                if src:
                    img_url = urljoin(MFA_BASE, src)
                    break
    if not img_url:
        return None

    meta = _mfa_parse_metadata_fields(root, url)
    artist_province = extract_province(
        f"{meta.get('provenance', '')} {meta.get('description', '')} "
        f"{meta.get('material', '')} {meta.get('culture', '')}"
    )

    partial: dict[str, Any] = {
        **meta,
        "artist_province": artist_province,
        "detail_url": _mfa_normalize_detail_url(url),
        "iiif_manifest_url": "",
    }
    desc_before = meta.get("description") or ""
    rec = finalize_record(partial, MUSEUM_ID_MFA_BOSTON)
    if not desc_before:
        rec["description"] = ""
    rec["image_url"] = img_url
    return rec


def _mfa_parse_detail_requests(sess: Any, url: str) -> Optional[dict[str, Any]]:
    try:
        r = retry_get(sess, url, timeout=60, retries=3)
    except Exception as exc:
        log.debug("[MFA] detail 失败 %s: %s", url, exc)
        return None
    if _mfa_page_blocked(r):
        return None
    return _mfa_parse_detail_html(r.text, url)


def _crawl_mfa_details_on_page(
    page: Any,
    context: Any,
    links: list[str],
    out_csv: Path,
    img_root: Path,
    limit: int,
    delay: float,
    crawl_day: str,
    seen_ids: set[str],
    seen_urls: set[str],
    db_writer: Optional["MySQLWriter"],
) -> tuple[int, int]:
    img_ok = 0
    rows_batch: list[dict[str, Any]] = []
    total_written = 0
    flush_every = 5
    n_detail_fail = n_dl_fail = n_ok = 0
    img_sess = make_session()
    _sync_playwright_cookies_to_session(context, img_sess)
    # 预先创建 CSV 表头，便于用户在运行中看到文件出现
    if not out_csv.exists() or out_csv.stat().st_size == 0:
        append_csv(out_csv, [], None)

    pbar = tqdm(links, desc="MFA Boston", unit="条", dynamic_ncols=True)
    try:
        for url in pbar:
            already = total_written + len(rows_batch)
            if limit and already >= limit:
                break
            norm_url = _mfa_normalize_detail_url(url)
            if norm_url in seen_urls:
                continue
            detail_html = ""
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                _mfa_wait_detail_on_page(page, timeout_ms=_MFA_DETAIL_IMAGE_WAIT_MS)
                _mfa_settle_detail_page(page, delay)
                detail_html = page.content()
                rec = _mfa_parse_detail_html(detail_html, url)
            except Exception as exc:
                log.debug("[MFA] 浏览器详情失败 %s: %s", url, exc)
                rec = None
            if not rec:
                n_detail_fail += 1
                continue
            oid = rec.get("object_id", "").strip()
            if oid in seen_ids:
                continue
            rec["crawl_date"] = crawl_day
            ext = ext_from_url(rec["image_url"])
            dest = _mfa_image_dest(img_root, oid, ext)
            rec["image_path"] = ""
            _sync_playwright_cookies_to_session(context, img_sess)
            jitter(delay * 0.3, 0.1, 0.4)
            if _mfa_download_record_image(
                image_url=rec["image_url"],
                html=detail_html,
                detail_url=url,
                dest=dest,
                img_sess=img_sess,
                context=context,
                page=page,
            ):
                img_ok += 1
                n_ok += 1
                rec["image_path"] = _mfa_image_rel_path(oid, ext)
            else:
                n_dl_fail += 1
                if n_dl_fail <= 5 or n_dl_fail % 50 == 0:
                    log.warning(
                        "[MFA] 图片未落盘，跳过该条 %s | %s → %s",
                        oid,
                        rec["image_url"][:90],
                        dest,
                    )
                continue
            seen_ids.add(oid)
            seen_urls.add(norm_url)
            rows_batch.append(rec)
            pbar.set_postfix_str(
                f"写入{total_written + len(rows_batch)} 图{img_ok}",
                refresh=True,
            )
            if len(rows_batch) >= flush_every:
                append_csv(out_csv, rows_batch, db_writer)
                total_written += len(rows_batch)
                rows_batch = []
    finally:
        pbar.close()

    if rows_batch:
        append_csv(out_csv, rows_batch, db_writer)
        total_written += len(rows_batch)

    log.info(
        "[MFA] 小结 | 详情解析失败 %d | 图片下载失败 %d | 成功写入 %d | 累计已写入 %d",
        n_detail_fail, n_dl_fail, n_ok, total_written,
    )
    return total_written, img_ok


def _mfa_image_file_ok(csv_parent: Path, image_path: str, *, min_bytes: int = 2048) -> bool:
    ip = (image_path or "").strip()
    if not ip:
        return False
    disk = csv_parent / ip.replace("\\", "/")
    try:
        return disk.is_file() and disk.stat().st_size >= min_bytes
    except OSError:
        return False


def _mfa_image_dest(img_root: Path, object_id: str, ext: str) -> Path:
    """本地落盘路径：``images/mfa/{object_id}.{ext}``（文件名固定为馆藏号，不用 title slug）。"""
    sid = safe_filename_fragment(object_id)
    return img_root / "mfa" / f"{sid}.{ext}"


def _mfa_image_rel_path(object_id: str, ext: str) -> str:
    sid = safe_filename_fragment(object_id)
    return str(Path("images") / "mfa" / f"{sid}.{ext}")


def _mfa_remove_object_image_files(img_root: Path, object_id: str) -> int:
    """删除该 object_id 在 mfa 目录下各扩展名的旧文件（重下前调用）。"""
    sid = safe_filename_fragment(object_id)
    mfa_dir = img_root / "mfa"
    if not mfa_dir.is_dir():
        return 0
    removed = 0
    for p in mfa_dir.glob(f"{sid}.*"):
        if p.is_file():
            try:
                p.unlink()
                removed += 1
            except OSError as exc:
                log.warning("[MFA] 删除旧图失败 %s: %s", p.name, exc)
    return removed


def wipe_mfa_image_dir(img_root: Path) -> int:
    """清空 ``img_root/mfa`` 下全部图片（二次全量重爬前调用）。"""
    mfa_dir = img_root / "mfa"
    mfa_dir.mkdir(parents=True, exist_ok=True)
    removed = 0
    for p in mfa_dir.iterdir():
        if p.is_file():
            try:
                p.unlink()
                removed += 1
            except OSError as exc:
                log.warning("[MFA] 删除旧图失败 %s: %s", p.name, exc)
    log.info("[MFA] 已清空图片目录 %s（删除 %d 个文件）", mfa_dir, removed)
    return removed


_MFA_SERVER_IMAGE_DIR = r"C:\Users\Administrator\Desktop\mfa\mfa"
_MFA_MIN_IMAGE_BYTES = 2048


def _mfa_find_image_file(img_root: Path, object_id: str) -> Path | None:
    """在 ``img_root/mfa`` 下查找 ``{object_id}.*`` 的有效图片。"""
    sid = safe_filename_fragment(object_id)
    mfa_dir = img_root / "mfa"
    if not mfa_dir.is_dir():
        return None
    candidates = [
        p
        for p in mfa_dir.glob(f"{sid}.*")
        if p.is_file()
        and p.stat().st_size >= _MFA_MIN_IMAGE_BYTES
        and _mfa_validate_saved_image(p, min_bytes=_MFA_MIN_IMAGE_BYTES)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _mfa_csv_image_path(
    object_id: str,
    ext: str,
    *,
    path_mode: str = "relative",
    server_dir: str = _MFA_SERVER_IMAGE_DIR,
) -> str:
    sid = safe_filename_fragment(object_id)
    if path_mode == "server":
        return str(Path(server_dir) / f"{sid}.{ext}")
    return _mfa_image_rel_path(object_id, ext)


def reconcile_mfa_csv_image_paths(
    rows: list[dict[str, Any]],
    img_root: Path,
    *,
    path_mode: str = "relative",
    server_dir: str = _MFA_SERVER_IMAGE_DIR,
) -> tuple[int, int]:
    """
    按磁盘 ``images/mfa/{object_id}.*`` 回写 CSV 的 ``image_path`` / ``image_count``。

    返回 (路径有变化的行数, 磁盘上有图的行数)。
    """
    updated = 0
    with_img = 0
    for row in rows:
        oid = (row.get("object_id") or "").strip()
        if not oid:
            continue
        old_path = (row.get("image_path") or "").strip()
        found = _mfa_find_image_file(img_root, oid)
        if found:
            ext = found.suffix.lstrip(".") or "jpg"
            new_path = _mfa_csv_image_path(
                oid, ext, path_mode=path_mode, server_dir=server_dir
            )
            row["image_path"] = new_path
            row["image_paths"] = ""
            row["image_count"] = "1"
            with_img += 1
        else:
            new_path = ""
            row["image_path"] = ""
            row["image_paths"] = ""
            row["image_count"] = "0"
        if new_path != old_path:
            updated += 1
    return updated, with_img


def _mfa_load_seen_keys(out_csv: Path) -> tuple[set[str], set[str]]:
    """断点：已有有效本地图则跳过；同时按 ``detail_url`` 去重（兼容 object_id 从 slug 改为 accession）。"""
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    if not out_csv.exists() or out_csv.stat().st_size == 0:
        return seen_ids, seen_urls
    base = out_csv.parent
    try:
        with open(out_csv, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                if not _mfa_image_file_ok(base, row.get("image_path") or ""):
                    continue
                oid = (row.get("object_id") or "").strip()
                if oid:
                    seen_ids.add(oid)
                url = _mfa_normalize_detail_url(row.get("detail_url") or "")
                if url:
                    seen_urls.add(url)
    except Exception:
        pass
    return seen_ids, seen_urls


def _mfa_load_seen_ids(out_csv: Path) -> set[str]:
    seen_ids, _ = _mfa_load_seen_keys(out_csv)
    return seen_ids


def repair_mfa_metadata(
    out_csv: Path,
    delay: float = 1.0,
    *,
    browser_headless: bool = False,
    limit: int = 0,
    db_writer: Optional["MySQLWriter"] = None,
) -> tuple[int, int]:
    """
    按 CSV 中 ``detail_url`` 重新解析 eMuseum 元数据并整表回写；保留已有 ``image_path``。
    """
    if not out_csv.exists() or out_csv.stat().st_size == 0:
        log.warning("[MFA] 补字段：CSV 不存在或为空 %s", out_csv)
        return 0, 0
    with open(out_csv, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return 0, 0

    todo = [dict(r) for r in rows if (r.get("detail_url") or "").strip()]
    if limit > 0:
        todo = todo[:limit]
    if not todo:
        log.warning("[MFA] 补字段：无 detail_url 可处理")
        return 0, 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "补字段需要 Playwright：pip install playwright && playwright install chromium"
        ) from exc

    log.info("[MFA] 补字段：待处理 %d / %d 行", len(todo), len(rows))
    updated = 0
    by_url: dict[str, dict[str, Any]] = {}
    for row in rows:
        norm = _mfa_normalize_detail_url(row.get("detail_url") or "")
        if norm:
            by_url[norm] = dict(row)

    pw = sync_playwright().start()
    browser = _mfa_launch_browser(pw, headless=browser_headless)
    context = _mfa_new_context(browser)
    page = context.new_page()
    pbar = tqdm(todo, desc="MFA 补字段", unit="条", dynamic_ncols=True)
    try:
        page.goto(
            "https://www.mfa.org/collections",
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        page.wait_for_timeout(2500)
        page.goto(MFA_LIST_URLS[0], wait_until="domcontentloaded", timeout=90_000)
        _mfa_wait_objects_on_page(page, timeout_ms=120_000)
        page.wait_for_timeout(1500)

        for row in pbar:
            url = (row.get("detail_url") or "").strip()
            norm = _mfa_normalize_detail_url(url)
            if not norm:
                continue
            try:
                page.goto(norm, wait_until="domcontentloaded", timeout=90_000)
                _mfa_wait_detail_on_page(page, timeout_ms=_MFA_DETAIL_IMAGE_WAIT_MS)
                _mfa_settle_detail_page(page, delay)
                rec = _mfa_parse_detail_html(page.content(), norm)
            except Exception as exc:
                log.debug("[MFA] 补字段打开详情失败 %s: %s", norm, exc)
                continue
            if not rec:
                continue

            old = by_url.get(norm, dict(row))
            keep = {
                k: old.get(k, "")
                for k in ("image_path", "image_paths", "image_urls", "image_count", "crawl_date")
                if old.get(k)
            }
            merged = dict(rec)
            merged.update({k: v for k, v in keep.items() if v})
            if not merged.get("image_url") and old.get("image_url"):
                merged["image_url"] = old["image_url"]
            by_url[norm] = merged
            updated += 1
            pbar.set_postfix_str(f"更新{updated}", refresh=True)
            jitter(delay * 0.2, 0.1, 0.3)
    finally:
        pbar.close()
        _mfa_close_browser(pw, browser)

    new_rows: list[dict[str, Any]] = []
    for row in rows:
        norm = _mfa_normalize_detail_url(row.get("detail_url") or "")
        new_rows.append(by_url.get(norm, dict(row)))
    write_csv(out_csv, new_rows)
    if db_writer and updated:
        from museum_crawler.csv_db_sync import import_csv_to_mysql

        import_csv_to_mysql(out_csv, chunk_size=80)
    log.info("[MFA] 补字段完成：更新 %d / %d → %s", updated, len(todo), out_csv)
    return len(new_rows), updated


def repair_mfa_images(
    out_csv: Path,
    img_root: Path,
    delay: float = 1.0,
    *,
    browser_headless: bool = False,
    limit: int = 0,
    force_redownload: bool = False,
    path_mode: str = "relative",
    server_image_dir: str = _MFA_SERVER_IMAGE_DIR,
    save_every: int = 10,
) -> tuple[int, int]:
    """
    按 CSV 中的 detail_url / image_url 补下缺失的本地图片，并回写 image_path。
    不重新收集列表链接。

    ``force_redownload=True``：忽略已有图，每条先删 ``{object_id}.*`` 再重下；
    文件名固定为 ``images/mfa/{object_id}.{ext}``。

    结束后会按磁盘实际文件 **整表回写** ``image_path``（``path_mode=relative|server``）。
    """
    if not out_csv.exists() or out_csv.stat().st_size == 0:
        log.warning("[MFA] 补图：CSV 不存在或为空 %s", out_csv)
        return 0, 0
    with open(out_csv, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return 0, 0

    base = out_csv.parent
    todo: list[dict[str, Any]] = []
    for row in rows:
        oid = (row.get("object_id") or "").strip()
        if not oid:
            continue
        if not force_redownload and _mfa_image_file_ok(base, row.get("image_path") or ""):
            continue
        if not (row.get("detail_url") or row.get("image_url")):
            continue
        todo.append(dict(row))

    if limit > 0:
        todo = todo[:limit]
    if not todo:
        log.info("[MFA] 补图：所有行均已有本地图片")
        return len(rows), sum(
            1 for r in rows if _mfa_image_file_ok(base, r.get("image_path") or "")
        )

    log.info("[MFA] 补图：待处理 %d / %d 行", len(todo), len(rows))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "补图需要 Playwright：pip install playwright && playwright install chromium"
        ) from exc

    img_ok = 0
    pw = sync_playwright().start()
    browser = _mfa_launch_browser(pw, headless=browser_headless)
    context = _mfa_new_context(browser)
    page = context.new_page()
    img_sess = make_session()
    by_id = {(r.get("object_id") or "").strip(): r for r in rows}

    pbar = tqdm(todo, desc="MFA 补图", unit="条", dynamic_ncols=True)
    try:
        for row in pbar:
            oid = (row.get("object_id") or "").strip()
            url = (row.get("detail_url") or "").strip()
            img_url = (row.get("image_url") or "").strip()
            if not url:
                continue
            ext = ext_from_url(img_url)
            if force_redownload:
                _mfa_remove_object_image_files(img_root, oid)
            dest = _mfa_image_dest(img_root, oid, ext)
            detail_html = ""
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                _mfa_wait_detail_on_page(page, timeout_ms=_MFA_DETAIL_IMAGE_WAIT_MS)
                _mfa_settle_detail_page(page, delay)
                detail_html = page.content()
                if not img_url:
                    rec = _mfa_parse_detail_html(detail_html, url)
                    if rec:
                        img_url = rec.get("image_url") or ""
            except Exception as exc:
                log.debug("[MFA] 补图打开详情失败 %s: %s", oid, exc)
                continue
            _sync_playwright_cookies_to_session(context, img_sess)
            if _mfa_download_record_image(
                image_url=img_url,
                html=detail_html,
                detail_url=url,
                dest=dest,
                img_sess=img_sess,
                context=context,
                page=page,
            ):
                if dest.is_file():
                    ext = dest.suffix.lstrip(".") or ext
                by_id[oid]["image_path"] = _mfa_csv_image_path(
                    oid, ext, path_mode=path_mode, server_dir=server_image_dir
                )
                by_id[oid]["image_paths"] = ""
                by_id[oid]["image_count"] = "1"
                by_id[oid]["image_url"] = img_url or by_id[oid].get("image_url", "")
                img_ok += 1
                if save_every > 0 and img_ok % save_every == 0:
                    write_csv(out_csv, list(by_id.values()))
            elif force_redownload:
                by_id[oid]["image_path"] = ""
                by_id[oid]["image_paths"] = ""
                by_id[oid]["image_count"] = "0"
            pbar.set_postfix_str(f"图{img_ok}", refresh=True)
            jitter(delay * 0.3, 0.1, 0.4)
    finally:
        pbar.close()
        _mfa_close_browser(pw, browser)

    final_rows = list(by_id.values())
    n_path_upd, n_on_disk = reconcile_mfa_csv_image_paths(
        final_rows,
        img_root,
        path_mode=path_mode,
        server_dir=server_image_dir,
    )
    write_csv(out_csv, final_rows)
    log.info(
        "[MFA] 补图完成：新下载 %d 张；CSV 路径回写 %d 行，磁盘有图 %d 行 → %s",
        img_ok,
        n_path_upd,
        n_on_disk,
        img_root / "mfa",
    )
    return len(rows), img_ok


def _mfa_close_browser(pw: Any, browser: Any) -> None:
    try:
        browser.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass


def crawl_mfa(
    out_csv: Path,
    img_root: Path,
    limit: int,
    delay: float,
    db_writer: Optional["MySQLWriter"] = None,
    *,
    use_browser: bool = True,
    browser_headless: bool = False,
    max_pages_per_list: int = 0,
) -> tuple[int, int]:
    crawl_day = date.today().isoformat()
    seen_ids, seen_urls = _mfa_load_seen_keys(out_csv)
    if seen_ids or seen_urls:
        log.info(
            "[MFA] 断点：已有本地图片 %d 条（ID %d / URL %d；可用 --mfa-repair-metadata 补字段）",
            len(seen_urls),
            len(seen_ids),
            len(seen_urls),
        )

    links: list[str] = []
    pw = browser = context = page = None

    if use_browser:
        log.info("[MFA] 使用 Playwright 收集链接并爬详情（绕过 AWS WAF）…")
        try:
            cap = _link_cap_for_limit(limit)
            links, pw, browser, context, page = _mfa_collect_links_playwright(
                delay,
                crawl_limit=limit,
                headless=browser_headless,
                link_cap=cap,
                max_pages_per_list=max_pages_per_list,
            )
            if not links and browser_headless:
                log.warning(
                    "[MFA] 无头模式未通过 WAF，自动切换有界面浏览器重试…"
                )
                _mfa_close_browser(pw, browser)
                links, pw, browser, context, page = _mfa_collect_links_playwright(
                    delay,
                    crawl_limit=limit,
                    headless=False,
                    link_cap=cap,
                    max_pages_per_list=max_pages_per_list,
                )
        except Exception as exc:
            log.error("[MFA] Playwright 失败: %s", exc)
            if pw and browser:
                _mfa_close_browser(pw, browser)
            return 0, 0
    elif not use_browser:
        sess = make_session()
        log.info("[MFA] 使用 requests 收集链接…")
        links = _mfa_collect_links_requests(
            sess, delay, max_pages_per_list=max_pages_per_list
        )

    if not links:
        if pw and browser:
            _mfa_close_browser(pw, browser)
        log.error(
            "[MFA] 未收集到链接。请确认已安装："
            "pip install playwright && playwright install chromium；"
            "并保持浏览器窗口直至出现藏品列表（勿用 --mfa-headless）"
        )
        return 0, 0

    if use_browser and page is not None and context is not None:
        log.info(
            "[MFA] 链接就绪 %d 条，开始逐条爬详情（目标写入 %s 条）…",
            len(links),
            limit if limit else "不限",
        )
        try:
            total_written, img_ok = _crawl_mfa_details_on_page(
                page,
                context,
                links,
                out_csv,
                img_root,
                limit,
                delay,
                crawl_day,
                seen_ids,
                seen_urls,
                db_writer,
            )
        finally:
            if pw and browser:
                _mfa_close_browser(pw, browser)
        log.info(
            "[MFA] 完成：写入 %d 条，图片 %d 张 → %s",
            total_written, img_ok, out_csv,
        )
        return total_written, img_ok

    # requests 详情模式（仅当 --mfa-no-browser 且链接非空时）
    sess = make_session()
    img_ok = 0
    rows_batch: list[dict[str, Any]] = []
    total_written = 0
    pbar = tqdm(links, desc="MFA Boston", unit="条", dynamic_ncols=True)
    try:
        n_detail_fail = n_dl_fail = n_ok = 0
        for url in pbar:
            already = total_written + len(rows_batch)
            if limit and already >= limit:
                break
            norm_url = _mfa_normalize_detail_url(url)
            if norm_url in seen_urls:
                continue
            try:
                r = retry_get(sess, url, timeout=60, retries=3)
            except Exception:
                n_detail_fail += 1
                jitter(delay, 0.1, 0.3)
                continue
            if _mfa_page_blocked(r):
                n_detail_fail += 1
                jitter(delay, 0.1, 0.3)
                continue
            detail_html = r.text
            rec = _mfa_parse_detail_html(detail_html, url)
            if not rec:
                n_detail_fail += 1
                jitter(delay, 0.1, 0.3)
                continue
            oid = rec.get("object_id", "").strip()
            if oid in seen_ids:
                continue
            rec["crawl_date"] = crawl_day
            ext = ext_from_url(rec["image_url"])
            dest = _mfa_image_dest(img_root, oid, ext)
            rec["image_path"] = ""
            jitter(delay * 0.3, 0.1, 0.4)
            if _mfa_download_record_image(
                image_url=rec["image_url"],
                html=detail_html,
                detail_url=url,
                dest=dest,
                img_sess=sess,
            ):
                img_ok += 1
                n_ok += 1
                rec["image_path"] = _mfa_image_rel_path(oid, ext)
            else:
                n_dl_fail += 1
                if n_dl_fail <= 5 or n_dl_fail % 50 == 0:
                    log.warning(
                        "[MFA] 图片未落盘，跳过该条 %s | %s → %s",
                        oid,
                        rec["image_url"][:90],
                        dest,
                    )
                continue
            seen_ids.add(oid)
            seen_urls.add(norm_url)
            rows_batch.append(rec)
            pbar.set_postfix_str(
                f"写入{total_written + len(rows_batch)} 图{img_ok}",
                refresh=True,
            )
            if len(rows_batch) >= 10:
                append_csv(out_csv, rows_batch, db_writer)
                total_written += len(rows_batch)
                rows_batch = []
        if rows_batch:
            append_csv(out_csv, rows_batch, db_writer)
            total_written += len(rows_batch)
        log.info(
            "[MFA] 小结 | 详情解析失败 %d | 图片下载失败 %d | 成功写入 %d",
            n_detail_fail, n_dl_fail, total_written,
        )
    finally:
        pbar.close()

    log.info("[MFA] 完成：写入 %d 条，图片 %d 张 → %s", total_written, img_ok, out_csv)
    return total_written, img_ok
