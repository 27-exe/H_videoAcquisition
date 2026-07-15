"""iwara crawler (HK side). Per plan §1: zero persistence, no DB.

Download URL resolution:
  After building the items list, this crawler fetches
  apiq.iwara.tv/video/{id} in-browser (CF bypass), then
  deobfuscates the response with aiohttp + SHA1 to extract
  the real mp4 download URL (matching old single-VPS
  IwaraSpider behavior).

  Batched in groups of 5 to avoid overwhelming the iwara API.
"""
import asyncio
import hashlib
import html as _html
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

import aiohttp
from lxml import html as lxml_html

from .browser import open_browser, open_page

logger = logging.getLogger(__name__)

IWARA_BASE = "https://www.iwara.tv"
IWARA_API = "https://apiq.iwara.tv/video"
IWARA_OBFUSCATION_SUFFIX = "_mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"
# 5 items per batch — mirrors original IwaraSpider.parse to not hammer the API
_API_BATCH_SIZE = 5
_API_BATCH_DELAY = 10  # seconds, same as old spider


def _now_iso() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).date().isoformat()


def _parse_api_json(text: str) -> list[dict[str, Any]]:
    """Parse iwara API response (may be raw JSON or HTML-wrapped <pre>{...}</pre>).

    Returns a list of dicts — always a list so caller can index [0].
    """
    import json

    # 1) try raw JSON (text is clean)
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except Exception:
        pass

    # 2) extract from <pre>...</pre> (iwara serves JSON inside HTML pre tag)
    m = re.search(r"<pre>(.*?)</pre>", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
        except Exception:
            pass

    # 3) try array regex
    m = re.search(r"(\[.*\])", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, list):
                return data
        except Exception:
            pass

    # 4) try object regex
    m = re.search(r"(\{.*\})", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict):
                return [data]
        except Exception:
            pass

    return []


async def _resolve_one_download(
    session: aiohttp.ClientSession, api_file: dict[str, Any],
    timeout: float = 10.0,
) -> str | int:
    """Deobfuscate a single iwara API file object, return download URL or 0.

    timeout (seconds) caps the fileUrl HTTP request — bad/expired items
    fail fast and return 0 instead of hanging the whole batch.
    """
    if not api_file:
        return 0
    try:
        file_url = api_file.get("fileUrl", "")
        file_id = str(api_file.get("file", {}).get("id", ""))
    except Exception:
        return 0
    if not file_url or not file_id:
        return 0

    # unescape HTML entities (&amp; → &) leaked into JSON <pre> content
    file_url = _html.unescape(file_url)

    # extract expires from fileUrl (e.g. "...?expires=1234567890&..." )
    m = re.search(r"[?&]expires=(\d+)", file_url)
    if not m:
        return 0
    expires = m.group(1)

    sha_key = file_id + "_" + expires + IWARA_OBFUSCATION_SUFFIX
    t_hash = hashlib.sha1(sha_key.encode()).hexdigest()

    try:
        async with session.get(
            file_url, headers={"X-Version": t_hash},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except Exception as e:
        print(f"DL_DEOBF_REQ_FAIL: id={file_id} {type(e).__name__}: {e}", flush=True)
        return 0

    if not isinstance(data, list):
        return 0

    # find the "Source" quality item
    download = None
    fallback = None
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        src = item.get("src", {})
        if isinstance(src, dict) and src.get("download"):
            if name == "Source":
                download = src["download"]
            elif name == "360":
                fallback = src["download"]
    result = download or fallback
    if result and not result.startswith("http"):
        result = "https:" + result
    return result or 0


async def crawl_iwara(cfg: dict) -> dict:
    """Return dict per plan §2.2.

    cfg: at least {"keywords": "...", "page": int, "limit": int}
    """
    keywords = cfg.get("keywords", "trending")
    page_num = int(cfg.get("page", 1))
    limit = int(cfg.get("limit", 30))

    list_url = f"{IWARA_BASE}/videos?sort={keywords}&page={page_num}"
    items: list[dict[str, Any]] = []

    started = datetime.now()
    try:
        async with open_browser() as context:
            # ── 1) list page ─────────────────────────────────────────────
            try:
                list_page, _ = await open_page(context, list_url, goto_timeout_ms=60000)
            except Exception as e:
                return {
                    "ok": False, "source": "iwara", "error": "list_page_failed",
                    "message": str(e),
                    "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
                }

            try:
                html = await list_page.content()
            finally:
                await list_page.close()

            try:
                doc = lxml_html.fromstring(html)
                items_el = doc.xpath(
                    '//*[contains(concat(" ", normalize-space(@class), " "), " page-videoList__item ")]'
                )
            except Exception as e:
                return {
                    "ok": False, "source": "iwara", "error": "parse_failed",
                    "message": f"xpath parse: {e}",
                    "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
                }

            for el in items_el:
                href_list = el.xpath('.//a[contains(@href, "/video/")]/@href')
                if not href_list:
                    continue
                m = re.match(r"/video/([a-zA-Z0-9_-]+)/.*", href_list[0])
                if not m:
                    continue
                vid_id = m.group(1)
                url = IWARA_BASE + href_list[0]
                title_el = el.xpath('.//*[contains(@class, "videoTeaser__title")]')
                title = title_el[0].text_content().strip()[:100] if title_el else ""
                items.append({
                    "rank": len(items) + 1,
                    "id": vid_id,
                    "title": title,
                    "source_url": url,
                    # placeholder; resolved below
                    "download_url": "",
                })
                if len(items) >= limit:
                    break

            if not items:
                logger.info("iwara list page parsed empty, returning ok=true with empty items")
                return {
                    "ok": True, "source": "iwara", "crawled_at": _now_iso(),
                    "items": [], "warnings": [],
                    "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
                }

            # ── 2) resolve download URLs (batched) ──────────────────────
            vid_ids = [it["id"] for it in items]

            api_results: list[dict | None] = [None] * len(vid_ids)

            # 2a) fetch apiq.iwara.tv/video/{id} in-browser (CF bypass).
            # Each call opens its own page, but we launch 5-at-a-time concurrently
            # within one browser context — original spider's pattern.
            async def _fetch_api(vid: str) -> dict | None:
                api_url = f"{IWARA_API}/{vid}"
                try:
                    api_page, _ = await open_page(context, api_url, goto_timeout_ms=30000)
                    content = await api_page.content()
                    await api_page.close()
                    parsed = _parse_api_json(content)
                    return parsed[0] if parsed else None
                except Exception:
                    return None

            for batch_start in range(0, len(vid_ids), _API_BATCH_SIZE):
                batch_end = min(batch_start + _API_BATCH_SIZE, len(vid_ids))
                batch_ids = vid_ids[batch_start:batch_end]
                results = await asyncio.gather(
                    *[_fetch_api(vid) for vid in batch_ids],
                    return_exceptions=True,
                )
                for i, res in zip(range(batch_start, batch_end), results):
                    if isinstance(res, Exception):
                        print(f"DL_FETCH_API_EXC: vid_idx={i} {type(res).__name__}: {res}", flush=True)
                        api_results[i] = None
                    elif isinstance(res, dict):
                        api_results[i] = res
                    else:
                        api_results[i] = None

                if batch_end < len(vid_ids):
                    await asyncio.sleep(_API_BATCH_DELAY)

            # 2b) deobfuscate with aiohttp (pure HTTP, no browser) in parallel
            # batched 5-at-a-time with aiohttp timeout to fail fast on bad items
            _DEOBF_TIMEOUT = 10  # seconds per fileUrl HTTP request
            async with aiohttp.ClientSession() as http_session:
                for batch_start in range(0, len(api_results), _API_BATCH_SIZE):
                    batch_end = min(batch_start + _API_BATCH_SIZE, len(api_results))
                    tasks = [
                        _resolve_one_download(http_session, api_file, timeout=_DEOBF_TIMEOUT)
                        for api_file in api_results[batch_start:batch_end]
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for i, res in zip(range(batch_start, batch_end), results):
                        if isinstance(res, Exception):
                            print(f"DL_DEOBF_EXC: vid_idx={i} {type(res).__name__}: {res}", flush=True)
                            items[i]["download_url"] = ""
                        else:
                            items[i]["download_url"] = res if isinstance(res, str) else ""
                    if batch_end < len(api_results):
                        await asyncio.sleep(_API_BATCH_DELAY)

            # filter: items without a download_url become 0 (US bot will skip)
            for it in items:
                if not it.get("download_url"):
                    it["download_url"] = 0

            return {
                "ok": True,
                "source": "iwara",
                "crawled_at": _now_iso(),
                "items": items,
                "warnings": [],
                "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
            }

    except Exception as e:
        logger.exception("iwara 全局异常")
        return {
            "ok": False,
            "source": "iwara",
            "error": "internal_error",
            "message": str(e),
            "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
        }
