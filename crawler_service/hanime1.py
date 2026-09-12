"""hanime1 crawler (HK side). Per plan §1: zero persistence, no DB.

Download URL resolution:
  After building the items list, this crawler fetches
  hanime1.me/download?v={id} for each video and extracts
  the real mp4 download link via XPath (matching old
  single-VPS Hanime1spider behavior).

Two paths (2026-09-12 architecture change, same pattern as iwara):
  HTTP     -- curl_cffi impersonating Firefox against the server-rendered
              HTML. No cookie needed. list 0.44s + 30 pages 1.27s (conc 5).
  browser  -- camoufox + fuck_cf, batched in groups of 5. Kept as fallback
              and for HANIME_HTTP_MODE=0; ~500-1500s for the same work.

env:
  HANIME_HTTP_MODE=1/0      prefer HTTP (default 1)
  HANIME_HTTP_CONCURRENCY=5 HTTP concurrency
  HANIME_HTTP_IMPERSONATE   curl_cffi fingerprint (default firefox135)
  HANIME_HTTP_TIMEOUT       per-request timeout seconds (default 25)
"""
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from lxml import html as lxml_html

from .browser import open_browser, open_page
# 2026-09-12: 浏览器无关的 HTTP 路径(服务端渲染 HTML + curl_cffi)。
# 浏览器仅作 fallback,见 hanime1_http.py 顶部说明与 L2 handoff 文档。
# 2026-09-12: 失败告警。HK 不持有 TG 凭据,只打包结构化诊断塞进响应,由 US bot 发送。
from utils.notify import build_alert as _build_alert
from .hanime1_http import (
    HanimeHTTPBlocked,
    concurrency_limit,
    fetch_download as http_fetch_download,
    fetch_list as http_fetch_list,
    hanime1_http_mode_enabled,
    new_session as http_new_session,
)

logger = logging.getLogger(__name__)

HANIME1_BASE = "https://hanime1.me"
_DL_BATCH_SIZE = 5
_DL_BATCH_DELAY = 5  # seconds
_TODAY_SORT = "%E6%9C%AC%E6%97%A5%E6%8E%92%E8%A1%8C"  # "今日排行"


def _now_iso() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).date().isoformat()


async def _fetch_list_browser(list_url: str) -> tuple[list[tuple[str, str]], Optional[dict]]:
    """List page via camoufox.

    Returns (pairs, error_dict). error_dict mimics the original failure return
    (without elapsed_ms, which the caller fills in); None means success.
    """
    async with open_browser() as context:
        logger.info("hanime1: browser context opened")
        # ── 1) list page ─────────────────────────────────────────────
        try:
            list_page, _ = await open_page(context, list_url, goto_timeout_ms=60000)
            logger.info(f"hanime1: list page loaded: {list_url}")
        except Exception as e:
            logger.warning(f"hanime1: list_page_failed: {e}")
            return [], {
                "ok": False, "source": "hanime1", "error": "list_page_failed",
                "message": str(e),
            }

        try:
            html = await list_page.content()
            logger.info(f"hanime1: list html extracted: {len(html)} bytes")
        finally:
            await list_page.close()

        try:
            doc = lxml_html.fromstring(html)
            anchors = doc.xpath('//a[contains(@href, "?v=")]')
            logger.info(f"hanime1: xpath matched {len(anchors)} anchor tags")
        except Exception as e:
            logger.warning(f"hanime1: xpath parse_failed: {e}")
            return [], {
                "ok": False, "source": "hanime1", "error": "parse_failed",
                "message": f"xpath parse: {e}",
            }

    # Original single-VPS xpath — pulls rank 30..1 in descending
    # order, which is how Hanime1spider.preprocess_response does it.
    # (Currently this only works on the home-rows-wrapper block, so
    # the request must be against the search URL above.)
    pairs: list[tuple[str, str]] = []
    tree = lxml_html.fromstring(html)
    for i in range(30, 0, -1):
        xpath_tpl = (
            "//*[@id='home-rows-wrapper']/div[3]/div/div/div[{i}]/div/a/@href"
        )
        hrefs = tree.xpath(xpath_tpl.format(i=i))
        titles = tree.xpath(
            "//*[@id='home-rows-wrapper']/div[3]/div/div/div[{i}]/@title"
            .format(i=i)
        )
        if hrefs and titles:
            href = hrefs[0]
            if href.startswith("/"):
                url = HANIME1_BASE + href
            elif not href.startswith("http"):
                url = HANIME1_BASE + "/" + href.lstrip("/")
            else:
                url = href
            pairs.append((titles[0], url))
    return pairs, None


async def _resolve_downloads_browser(items: list[dict], skip_ids: set[str]) -> tuple[int, int, int]:
    """Original batched fuck_cf path. Mutates items[*]['download_url']."""
    from .fuck_cf import fuck_cf
    from lxml import html as lxml_html_dl

    dl_ok_count = 0
    dl_skip_count = 0
    dl_fail_count = 0
    logger.info(
        f"hanime1: starting download-url resolution for {len(items)} items, "
        f"skip_ids={len(skip_ids)}, batch={_DL_BATCH_SIZE}"
    )

    for batch_start in range(0, len(items), _DL_BATCH_SIZE):
        batch = items[batch_start:batch_start + _DL_BATCH_SIZE]
        batch_vids = [it["id"] for it in batch]
        logger.info(f"hanime1 dl batch [{batch_start}-{batch_start + len(batch) - 1}] ids={batch_vids}")

        # Build the URL list fuck_cf expects.  skip_ids entries
        # become the literal 0 so fuck_cf keeps alignment.
        cycle_urls: list[str | int] = []
        for it in batch:
            if it["id"] in skip_ids:
                cycle_urls.append(0)
            else:
                cycle_urls.append(
                    f"https://hanime1.me/download?v={it['id']}"
                )

        # fuck_cf opens its own AsyncCamoufox per call, so the outer
        # list-page context can stay open.  Both browsers run
        # concurrently without contention.
        try:
            results = await fuck_cf(
                cycle_urls,
                proxy_str=None,
                pro_name=None,
                pro_word=None,
                storage_state=None,
                select=None,        # hanime1 has no CF interstitial
                max_retries=3,
            )
        except Exception as e:
            logger.warning(f"  hanime1 dl batch exception: {e}")
            for it in batch:
                it["download_url"] = "0"
                dl_fail_count += 1
            continue

        # Original parsing path: take page.content() and xpath it.
        for it, url, content in zip(batch, cycle_urls, results):
            if url == 0:
                it["download_url"] = "0"
                dl_skip_count += 1
                logger.info(f"  hanime1 dl skip: idx={batch.index(it)} vid={it['id']}")
                continue
            if content == 0 or content == "" or isinstance(content, Exception):
                it["download_url"] = "0"
                dl_fail_count += 1
                logger.warning(f"  hanime1 dl fail (raw): vid={it['id']} content={type(content).__name__}")
                continue
            try:
                tree = lxml_html_dl.fromstring(content)
                d_url_list = tree.xpath(
                    "//*[@id='content-div']/div[1]/div[4]/div/div/table"
                    "/tbody/tr[2]/td[5]/a/@data-url"
                )
            except Exception as e:
                it["download_url"] = "0"
                dl_fail_count += 1
                logger.warning(f"  hanime1 dl parse fail: vid={it['id']} err={e}")
                continue
            if d_url_list:
                durl = d_url_list[0]
                if isinstance(durl, str) and not durl.startswith("http"):
                    durl = "https:" + durl
                it["download_url"] = durl
                dl_ok_count += 1
                logger.info(f"  hanime1 dl ok: vid={it['id']} url={str(durl)[:60]}...")
            else:
                it["download_url"] = "0"
                dl_fail_count += 1
                logger.warning(f"  hanime1 dl no-link: vid={it['id']}")

        if batch_start + _DL_BATCH_SIZE < len(items):
            await asyncio.sleep(_DL_BATCH_DELAY)

    return dl_ok_count, dl_skip_count, dl_fail_count


async def _resolve_downloads_http(items: list[dict], skip_ids: set[str]) -> tuple[int, int, int]:
    """Concurrent curl_cffi path. Raises HanimeHTTPBlocked if the site blocks us."""
    concurrency = concurrency_limit()
    sem = asyncio.Semaphore(concurrency)
    targets = [(i, it) for i, it in enumerate(items) if it["id"] not in skip_ids]
    skip_count = len(items) - len(targets)
    ok_count = 0
    fail_count = 0
    blocked: list[Exception] = []

    logger.info(
        f"hanime1 dl plan (HTTP): {len(targets)} vids, concurrency={concurrency}, "
        f"skip={skip_count}"
    )

    async with http_new_session() as session:
        async def _one(idx: int, it: dict):
            async with sem:
                try:
                    url = await http_fetch_download(session, it["id"])
                    return idx, it, url, None
                except Exception as e:  # noqa: BLE001
                    return idx, it, None, e

        tasks = [asyncio.create_task(_one(i, it)) for i, it in targets]
        for fut in asyncio.as_completed(tasks):
            idx, it, url, err = await fut
            if err is not None:
                if isinstance(err, HanimeHTTPBlocked):
                    blocked.append(err)
                    continue
                it["download_url"] = "0"
                fail_count += 1
                logger.warning(f"  hanime1 dl http fail: vid={it['id']} {err!r}")
                continue
            if url:
                it["download_url"] = url
                ok_count += 1
                logger.info(f"  hanime1 dl http ok: vid={it['id']} url={str(url)[:60]}...")
            else:
                it["download_url"] = "0"
                fail_count += 1
                logger.warning(f"  hanime1 dl http no-link: vid={it['id']}")

    if blocked:
        raise HanimeHTTPBlocked(f"{len(blocked)} download pages blocked, first: {blocked[0]}")

    # skip_ids keep the "0" contract (US bot reuses the existing ch_id from db)
    for it in items:
        if it["id"] in skip_ids:
            it["download_url"] = "0"

    return ok_count, skip_count, fail_count


async def crawl_hanime1(cfg: dict) -> dict:
    """Return dict per plan §2.2.

    cfg: at least {"page": int, "limit": int, "sort": "...", "skip_ids": list[int]}
    """
    page_num = int(cfg.get("page", 1))
    limit = int(cfg.get("limit", 30))
    keywords = cfg.get("keywords", "全部")  # used as `genre` query param
    skip_ids: set[str] = set(str(s) for s in cfg.get("skip_ids", []))

    # Original single-VPS URL: `search?genre=<kw>&sort=今日排行&page=<n>`
    # The `sort` is hard-coded to "今日排行" because that is the only sort
    # the home page uses; the US bot reads this page to get the daily ranking.
    list_url = (
        f"{HANIME1_BASE}/search?genre={keywords}"
        f"&sort={_TODAY_SORT}&page={page_num}"
    )

    started = datetime.now()
    items: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    warnings: list[str] = []
    fallback_used = False

    logger.info(f"hanime1 start: url={list_url} limit={limit} skip_ids={len(skip_ids)}")
    try:
        http_ok = hanime1_http_mode_enabled()

        # ── 1) list page ────────────────────────────────────────────────
        # Preferred: curl_cffi against the server-rendered HTML (no browser).
        # Fallback: camoufox + the ported preprocess xpath.
        pairs: list[tuple[str, str]] = []
        if http_ok:
            try:
                async with http_new_session() as session:
                    pairs = await http_fetch_list(session, list_url)
                logger.info(f"hanime1: list via HTTP ok, n={len(pairs)}")
            except HanimeHTTPBlocked as e:
                logger.warning(f"hanime1: HTTP list blocked ({e}) → browser fallback")
                fallback_used = True
                alerts.append(_build_alert(
                    "hanime1:HTTP 列表被拦,已回落浏览器路径",
                    "curl_cffi(impersonate=firefox135)拿列表页被拦(403/挑战页)。"
                    "hanime1 无 cookie,被拦通常意味着指纹策略收紧或 IP 信誉变化。"
                    "本次已回落浏览器路径,功能不受影响但会明显变慢。",
                    context={"platform": "hanime1", "stage": "list"},
                    exc=e, dedup_key="hanime1:http_blocked:list",
                ))
                http_ok = False
            except Exception as e:  # noqa: BLE001
                logger.warning(f"hanime1: HTTP list error ({e!r}) → browser fallback")
                warnings.append(f"http_list_error:{type(e).__name__}")
                http_ok = False

        if not http_ok:
            pairs, err = await _fetch_list_browser(list_url)
            if err is not None:
                err["warnings"] = warnings
                err["fallback_used"] = True
                err["alerts"] = alerts + [_build_alert(
                    "hanime1:列表页彻底失败(HTTP + 浏览器双路均挂)",
                    "HTTP 路径被拦后已回落浏览器路径;浏览器路径也未能取到列表页。"
                    "本次爬取无数据返回。",
                    context={"platform": "hanime1", "stage": "list", "path": "browser"},
                    exc=None, dedup_key="hanime1:list_page_failed",
                )]
                err["elapsed_ms"] = int((datetime.now() - started).total_seconds() * 1000)
                return err

        # Map pairs (title, href) into our items dict.
        for rank, (title, href) in enumerate(pairs, start=1):
            m = re.search(r"\?v=(\d+)", href)
            if not m:
                continue
            vid = m.group(1)
            items.append({
                "rank": rank,
                "id": vid,
                "title": title,
                "source_url": href,
                # placeholder; resolved below
                "download_url": "",
            })
            if len(items) >= limit:
                break

        if not items:
            logger.info("hanime1 list page parsed empty, returning ok=true with empty items")
            return {
                "ok": True, "source": "hanime1", "crawled_at": _now_iso(),
                "items": [], "warnings": warnings + ["list_parsed_empty"],
                "alerts": alerts, "fallback_used": fallback_used,
                "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
            }

        # ── 2) resolve download URLs ────────────────────────────────────
        # HTTP first (concurrent), then the original batched browser path for
        # anything the HTTP path could not resolve. skip_ids get a 0 in the
        # result so the US bot reuses the existing ch_id from db.
        dl_ok_count = 0
        dl_skip_count = 0
        dl_fail_count = 0

        if http_ok:
            try:
                dl_ok_count, dl_skip_count, dl_fail_count = await _resolve_downloads_http(
                    items, skip_ids
                )
                logger.info(
                    f"hanime1 dl (HTTP) done: ok={dl_ok_count} "
                    f"skip={dl_skip_count} fail={dl_fail_count}"
                )
            except HanimeHTTPBlocked as e:
                logger.warning(
                    f"hanime1: HTTP dl blocked ({e}) → browser fallback for all items"
                )
                fallback_used = True
                alerts.append(_build_alert(
                    "hanime1:HTTP 详情阶段被拦,已回落浏览器路径",
                    "列表页 HTTP 正常但批量 /download 页被拦,已整批回落浏览器路径重做。",
                    context={"platform": "hanime1", "stage": "api"},
                    exc=e, dedup_key="hanime1:http_blocked:api",
                ))
                http_ok = False
                for it in items:
                    it["download_url"] = ""
                dl_ok_count = dl_skip_count = dl_fail_count = 0

        if not http_ok:
            dl_ok_count, dl_skip_count, dl_fail_count = await _resolve_downloads_browser(
                items, skip_ids
            )
            logger.info(f"hanime1 dl done: ok={dl_ok_count} skip={dl_skip_count} fail={dl_fail_count}")

        # summary of items without valid download_url
        zero_count = sum(1 for it in items
                          if not it.get("download_url") or it["download_url"] in (0, "0"))
        logger.info(f"hanime1: items without download_url: {zero_count}/{len(items)}")
        for i, it in enumerate(items):
            if not it.get("download_url") or it["download_url"] in (0, "0"):
                logger.info(f"  zero-idx={i} id={it['id']} title={it.get('title','')[:40]}")
                it["download_url"] = 0

        return {
            "ok": True,
            "source": "hanime1",
            "crawled_at": _now_iso(),
            "items": items,
            "warnings": warnings,
            "alerts": alerts,
            "fallback_used": fallback_used,
            "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
        }

    except Exception as e:
        logger.exception("hanime1 全局异常")
        return {
            "ok": False,
            "source": "hanime1",
            "error": "internal_error",
            "message": str(e),
            "warnings": warnings,
            "alerts": alerts + [_build_alert(
                "hanime1:爬取抛出未捕获异常",
                "hanime1 爬取过程中抛出未捕获异常,本次无数据返回。堆栈见下。",
                context={"platform": "hanime1"},
                exc=e, dedup_key="hanime1:internal_error",
            )],
            "fallback_used": fallback_used,
            "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
        }
