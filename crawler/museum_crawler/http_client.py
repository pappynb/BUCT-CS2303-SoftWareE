# -*- coding: utf-8 -*-
"""
HTTP 层：Session、User-Agent 轮换、退避重试、图片下载。

反爬策略
--------
- 多 UA 随机轮换，降低单一指纹特征。
- ``retry_get``：429 尊重 ``Retry-After``，否则至少等待 30s；5xx 指数退避。
- ``download_image``：拒绝 ``text/html`` 伪装；落盘后校验最小字节数，剔除占位图。
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("spider")

# 429/5xx 的单次最大等待秒数（可在 .env 中覆盖）
# 例如：RETRY_MAX_WAIT_SECONDS=120
RETRY_MAX_WAIT_SECONDS = max(5, int(os.environ.get("RETRY_MAX_WAIT_SECONDS", "120")))

# 哈佛 NRS/IIIF 等对「全尺寸」未就绪时常返回 202 + HTML 占位页；同 URL 退避重拉次数（.env 可覆盖）
IIIF_202_HTML_POLL_MAX = max(1, int(os.environ.get("DOWNLOAD_IMAGE_IIIF_202_MAX_POLLS", "15")))

UA_POOL: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]


def make_session() -> requests.Session:
    """新建带合理默认头的 Session；每次请求前可再 ``_rotate_ua``。"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(UA_POOL),  # 初次即随机 UA，降低固定指纹
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "DNT": "1",
    })
    # 提升并发请求时的连接复用能力（尤其是图片下载阶段）
    adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _rotate_ua(sess: requests.Session) -> None:
    sess.headers["User-Agent"] = random.choice(UA_POOL)


def jitter(base: float, lo: float = 0.1, hi: float = 0.6) -> None:
    """基础间隔 + 均匀随机抖动，模拟人工节奏。"""
    time.sleep(base + random.uniform(lo, hi))


def retry_get(
    sess: requests.Session,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    timeout: float = 60.0,
    retries: int = 6,
    backoff: float = 3.0,
    stream: bool = False,
) -> requests.Response:
    """
    带重试的 GET。对 429/5xx 与网络错误做退避，避免瞬时打爆限流接口。
    """
    last_exc: Exception = RuntimeError("未尝试")
    for attempt in range(retries):
        try:
            _rotate_ua(sess)  # 每次重试换 UA，配合服务端限流策略
            r = sess.get(url, params=params, timeout=timeout, stream=stream)
            if r.status_code == 429:
                ra = r.headers.get("Retry-After", "")
                try:
                    # 有 Retry-After 则遵守；否则退避且至少 30s（api.data.gov 常见）
                    raw_wait = max(float(ra), 30.0)
                except (ValueError, TypeError):
                    raw_wait = max(backoff ** (attempt + 2), 30.0)
                # 防止服务端给出超长 Retry-After（如 2800+s）导致任务长时间卡住
                wait = min(raw_wait, float(RETRY_MAX_WAIT_SECONDS))
                if raw_wait > wait:
                    log.warning(
                        "429 Too Many Requests，服务端建议等待 %.0fs，已按上限 %.0fs 重试…",
                        raw_wait, wait
                    )
                else:
                    log.warning("429 Too Many Requests，%.0fs 后重试…", wait)
                time.sleep(wait)
                continue
            if r.status_code in (500, 502, 503, 504):
                # 502/503 多为网关/上游瞬时故障，退避略加长更稳
                wait = min(max(backoff ** (attempt + 1), 5.0), float(RETRY_MAX_WAIT_SECONDS))
                log.warning("HTTP %d，%.0fs 后重试…", r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()  # 其余 4xx 直接失败，避免无意义重试
            return r
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            last_exc = exc
            wait = backoff ** (attempt + 1)
            log.debug("网络错误 %s，%.0fs 后重试…", exc, wait)
            time.sleep(wait)
        except requests.exceptions.HTTPError as exc:
            raise exc
    raise requests.exceptions.RetryError(
        f"URL {url} 在 {retries} 次后仍失败: {last_exc}"
    )


def _response_preview_for_log(raw: bytes, maxlen: int = 200) -> str:
    """将响应体前若干字节转成单行日志预览（UTF-8 替换非法序列，折叠空白）。"""
    if not raw:
        return ""
    chunk = raw[:maxlen]
    # 若几乎无可打印字符，避免把二进制刷满日志
    textish = sum(1 for b in chunk if 32 <= b < 127 or b in (9, 10, 13))
    if textish < max(8, len(chunk) // 8):
        return chunk[:maxlen].hex()
    try:
        s = chunk.decode("utf-8", errors="replace")
    except Exception:
        return chunk.hex()
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > maxlen:
        s = s[:maxlen] + "…"
    return s


def _body_looks_like_html(head: bytes) -> bool:
    """根据首部字节猜测是否为 HTML/XML 错误页（Content-Type 可能仍标 image/*）。"""
    if not head:
        return False
    h = head.lstrip()[:400].lower()
    if h.startswith(b"<!doctype") or h.startswith(b"<html"):
        return True
    if h.startswith(b"<") and b"<html" in h[: min(256, len(h))]:
        return True
    return False


def _drain_response(r: requests.Response) -> None:
    try:
        for _ in r.iter_content(chunk_size=65536):
            pass
    finally:
        r.close()


def _safe_unlink(path: Path) -> None:
    """Windows 上文件被 IDE/预览占用时忽略删除失败。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("无法删除文件（可能被占用）%s: %s", path, exc)


def _commit_part_file(part: Path, dest: Path, *, min_bytes: int = 2048) -> bool:
    """将校验通过的 .part 落到 dest；dest 被占用时若原文件仍有效则视为成功。"""
    if not part.is_file() or part.stat().st_size < min_bytes:
        _safe_unlink(part)
        return False
    _safe_unlink(dest)
    try:
        part.replace(dest)
        return True
    except OSError as exc:
        log.debug("无法覆盖 %s（可能被占用）: %s", dest, exc)
        if dest.is_file() and dest.stat().st_size >= min_bytes:
            _safe_unlink(part)
            return True
        _safe_unlink(part)
        return False


def download_image(
    sess: requests.Session,
    url: str,
    dest: Path,
    timeout: float = 180.0,
    min_bytes: int = 2048,
    *,
    iiif_202_max_polls: Optional[int] = None,
    log_failures: bool = True,
) -> bool:
    """流式下载图片；过小或非图 HTML 视为失败。IIIF/NRS 常见 202+HTML 占位会按退避同 URL 重试。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    poll_max = IIIF_202_HTML_POLL_MAX if iiif_202_max_polls is None else max(1, iiif_202_max_polls)
    r: Optional[requests.Response] = None
    try:
        for attempt in range(poll_max):
            r = retry_get(sess, url, timeout=timeout, stream=True)
            status = r.status_code
            ctype = r.headers.get("Content-Type", "")
            pending_html = status == 202 and "text/html" in ctype.lower()
            if pending_html:
                ra = r.headers.get("Retry-After", "")
                try:
                    wait = min(
                        float(RETRY_MAX_WAIT_SECONDS),
                        max(1.5, float(ra)),
                    )
                except (ValueError, TypeError):
                    wait = min(
                        float(RETRY_MAX_WAIT_SECONDS),
                        max(2.0, min(25.0, 2.0 * (attempt + 1))),
                    )
                head = next(r.iter_content(chunk_size=256), b"")[:200]
                _drain_response(r)
                r = None
                if attempt < poll_max - 1:
                    log.debug(
                        "IIIF/NRS 202+HTML 占位，%.1fs 后重试 (%d/%d) dest=%s",
                        wait, attempt + 1, poll_max, dest.name,
                    )
                    time.sleep(wait)
                    continue
                _log = log.warning if log_failures else log.debug
                _log(
                    "图片下载失败(多次 202+HTML，IIIF 仍未就绪) status=202 Content-Type=%r preview=%r dest=%s url=%s",
                    ctype, _response_preview_for_log(head), dest.name, url,
                )
                return False
            break

        status = r.status_code
        ctype = r.headers.get("Content-Type", "")
        if "text/html" in ctype.lower():
            head = next(r.iter_content(chunk_size=256), b"")[:200]
            _drain_response(r)
            _log = log.warning if log_failures else log.debug
            _log(
                "图片下载失败(Content-Type 为 HTML) status=%s Content-Type=%r preview=%r dest=%s url=%s",
                status, ctype, _response_preview_for_log(head), dest.name, url,
            )
            r = None
            return False

        part = dest.parent / f"{dest.name}.part"
        _safe_unlink(part)
        peek = bytearray()
        with open(part, "wb") as fh:
            for chunk in r.iter_content(chunk_size=131072):  # 128KB 块，省内存
                if not chunk:
                    continue
                if len(peek) < 200:
                    need = 200 - len(peek)
                    peek.extend(chunk[:need])
                fh.write(chunk)

        peek_b = bytes(peek)
        preview = _response_preview_for_log(peek_b)
        total = part.stat().st_size if part.is_file() else 0

        if _body_looks_like_html(peek_b):
            _log = log.warning if log_failures else log.debug
            _log(
                "图片下载失败(内容为 HTML/错误页) status=%s Content-Type=%r preview=%r size=%s dest=%s url=%s",
                status, ctype, preview, total, dest.name, url,
            )
            _safe_unlink(part)
            return False

        if total < min_bytes:
            _log = log.warning if log_failures else log.debug
            _log(
                "图片下载失败(文件过小) status=%s Content-Type=%r size=%d min=%d preview=%r dest=%s url=%s",
                status, ctype, total, min_bytes, preview, dest.name, url,
            )
            _safe_unlink(part)
            return False
        return _commit_part_file(part, dest, min_bytes=min_bytes)
    except Exception as exc:
        _log = log.warning if log_failures else log.debug
        _log(
            "图片下载异常（多为网络/超时/HTTP 错误，无响应体预览）dest=%s url=%s err=%r",
            dest.name, url, exc,
        )
        _safe_unlink(dest.parent / f"{dest.name}.part")
        return False
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def download_image_first(
    sess: requests.Session,
    urls: list[str],
    dest: Path,
    *,
    iiif_poll_first: Optional[int] = None,
    iiif_poll_rest: int = 1,
    **kwargs: Any,
) -> tuple[bool, str]:
    """按顺序尝试多个图片 URL，成功则返回 (True, 实际使用的 URL)。中间失败默认只记 debug。"""
    ordered: list[str] = []
    seen: set[str] = set()
    for u in urls:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        ordered.append(u)
    for i, url in enumerate(ordered):
        polls = iiif_poll_first if i == 0 else iiif_poll_rest
        is_last = i == len(ordered) - 1
        kw = dict(kwargs)
        kw["log_failures"] = is_last
        if download_image(sess, url, dest, iiif_202_max_polls=polls, **kw):
            return True, url
    return False, ""


def ext_from_url(url: str) -> str:
    """从 URL 路径猜扩展名，默认 jpg。"""
    path = urlparse(url).path.lower()
    for e in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".gif"):
        if path.endswith(e):
            return e.lstrip(".")
    return "jpg"


def safe_filename_fragment(s: str, maxlen: int = 160) -> str:
    """文件名安全片段（去路径非法字符）。"""
    return re.sub(r'[<>:"/\\|?*\s\x00-\x1f]', "_", str(s))[:maxlen]
