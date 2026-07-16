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

    cfg: at least {"keywords": "...", "page": int, "limit": int, "skip_ids": list[str]}
    """
    keywords = cfg.get("keywords", "trending")
    page_num = int(cfg.get("page", 1))
    limit = int(cfg.get("limit", 30))
    skip_ids: set[str] = set(cfg.get("skip_ids", []))

    list_url = f"{IWARA_BASE}/videos?sort={keywords}&page={page_num}"
    items: list[dict[str, Any]] = []

    started = datetime.now()
    logger.info(f"iwara start: url={list_url} limit={limit} skip_ids={len(skip_ids)}")
    try:
        async with open_browser() as context:
            logger.info("iwara: browser context opened")
            # ── 1) list page ─────────────────────────────────────────────
            try:
                list_page, _ = await open_page(context, list_url, goto_timeout_ms=60000)
                logger.info(f"iwara: list page loaded: {list_url}")
            except Exception as e:
                logger.warning(f"iwara: list_page_failed: {e}")
                return {
                    "ok": False, "source": "iwara", "error": "list_page_failed",
                    "message": str(e),
                    "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
                }

            try:
                html = await list_page.content()
                logger.info(f"iwara: list html extracted: {len(html)} bytes")
            finally:
                await list_page.close()

            try:
                doc = lxml_html.fromstring(html)
                items_el = doc.xpath(
                    '//*[contains(concat(" ", normalize-space(@class), " "), " page-videoList__item ")]'
                )
                logger.info(f"iwara: xpath matched {len(items_el)} video cards on page")
            except Exception as e:
                logger.warning(f"iwara: xpath parse_failed: {e}")
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
                    # placeholder; resolved below (0 if vid_id in skip_ids)
                    "download_url": "",
                })
                if len(items) >= limit:
                    break

            logger.info(f"iwara: list_page parsed {len(items)}/{limit} items")

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
            # API fetch is also gated by a Semaphore(2) since iwara CDN may
            # rate-limit otherwise.
            # skip_ids: vid_ids already in US db — skip API+deobf for them, leave
            # download_url=0 so US bot reuses old ch_id.
            _API_SEM = asyncio.Semaphore(2)
            api_skip_count = 0
            api_ok_count = 0
            api_fail_count = 0

            async def _fetch_api(vid: str) -> dict | None:
                nonlocal api_skip_count, api_ok_count, api_fail_count
                if vid in skip_ids:
                    api_skip_count += 1
                    return None  # already in db, no need to call api
                async with _API_SEM:
                    api_url = f"{IWARA_API}/{vid}"
                    try:
                        api_page, _ = await open_page(context, api_url, goto_timeout_ms=30000)
                        content = await api_page.content()
                        await api_page.close()
                        parsed = _parse_api_json(content)
                        if parsed:
                            api_ok_count += 1
                            return parsed[0]
                        api_fail_count += 1
                        logger.warning(f"iwara api fetch: empty result vid={vid}")
                        return None
                    except Exception as e:
                        api_fail_count += 1
                        logger.warning(f"iwara api fetch exception vid={vid} {type(e).__name__}: {e}")
                        return None

            logger.info(f"iwara: starting API fetch for {len(vid_ids)} vids, "
                        f"skip={len(skip_ids)}, batch={_API_BATCH_SIZE}")
            for batch_start in range(0, len(vid_ids), _API_BATCH_SIZE):
                batch_end = min(batch_start + _API_BATCH_SIZE, len(vid_ids))
                batch_ids = vid_ids[batch_start:batch_end]
                logger.info(f"iwara api batch [{batch_start}-{batch_end-1}] ids={batch_ids}")
                results = await asyncio.gather(
                    *[_fetch_api(vid) for vid in batch_ids],
                    return_exceptions=True,
                )
                for i, res in zip(range(batch_start, batch_end), results):
                    if isinstance(res, Exception):
                        logger.warning(f"DL_FETCH_API_EXC: vid_idx={i} {type(res).__name__}: {res}")
                        api_results[i] = None
                    elif isinstance(res, dict):
                        api_results[i] = res
                    else:
                        api_results[i] = None

                if batch_end < len(vid_ids):
                    await asyncio.sleep(_API_BATCH_DELAY)

            logger.info(f"iwara api fetch done: skip={api_skip_count} ok={api_ok_count} "
                        f"fail={api_fail_count} total={len(vid_ids)}")

            # 2b) deobfuscate with aiohttp (pure HTTP, no browser) in parallel
            # batched 5-at-a-time, but capped at 2 concurrent per session
            # (same as old spider's asyncio.Semaphore(2) to avoid CDN rate-limiting)
            _DEOBF_TIMEOUT = 10  # seconds per fileUrl HTTP request
            _SEM = asyncio.Semaphore(2)  # original concurrency cap
            deobf_skip_count = 0
            deobf_ok_count = 0
            deobf_fail_count = 0

            async def _deobf_one(api_file, idx):
                return await _resolve_one_download(http_session, api_file, timeout=_DEOBF_TIMEOUT)

            async with aiohttp.ClientSession() as http_session:
                deobf_to_run = [
                    (i, api_results[i]) for i in range(len(api_results))
                    if api_results[i] is not None
                ]
                logger.info(f"iwara: deobf to run {len(deobf_to_run)} items "
                            f"(skipped {len(api_results) - len(deobf_to_run)} with no api result)")

                for batch_start in range(0, len(deobf_to_run), _API_BATCH_SIZE):
                    batch = deobf_to_run[batch_start:batch_start + _API_BATCH_SIZE]
                    tasks = [_deobf_one(api_file, idx) for idx, api_file in batch]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for (i, _), res in zip(batch, results):
                        if isinstance(res, Exception):
                            deobf_fail_count += 1
                            logger.warning(f"DL_DEOBF_EXC: vid_idx={i} {type(res).__name__}: {res}")
                            items[i]["download_url"] = ""
                        elif isinstance(res, str) and res:
                            deobf_ok_count += 1
                            items[i]["download_url"] = res
                            logger.info(f"iwara deobf ok: vid_idx={i} url={res[:60]}...")
                        else:
                            deobf_fail_count += 1
                            items[i]["download_url"] = ""
                            logger.warning(f"iwara deobf fail: vid_idx={i} result={res!r}")
                    if batch_start + _API_BATCH_SIZE < len(deobf_to_run):
                        await asyncio.sleep(_API_BATCH_DELAY)

                logger.info(f"iwara deobf done: ok={deobf_ok_count} fail={deobf_fail_count}")

            # summary of which items ended up with download_url=0
            zero_count = sum(1 for it in items if not it.get("download_url"))
            logger.info(f"iwara: items without download_url: {zero_count}/{len(items)}")
            for i, it in enumerate(items):
                if not it.get("download_url"):
                    logger.info(f"  zero-idx={i} id={it['id']} title={it.get('title','')[:40]}")
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
