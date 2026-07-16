"""hanime1 crawler (HK side). Per plan §1: zero persistence, no DB.

Download URL resolution:
  After building the items list, this crawler opens
  hanime1.me/download?v={id} for each video and extracts
  the real mp4 download link via XPath (matching old
  single-VPS Hanime1spider behavior).

  Batched in groups of 5 to avoid hammering the site.
"""
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

from lxml import html as lxml_html

from .browser import open_browser, open_page

logger = logging.getLogger(__name__)

HANIME1_BASE = "https://hanime1.me"
_DL_BATCH_SIZE = 5
_DL_BATCH_DELAY = 5  # seconds


def _now_iso() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).date().isoformat()


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
    _TODAY_SORT = "%E6%9C%AC%E6%97%A5%E6%8E%92%E8%A1%8C"  # "今日排行"
    list_url = (
        f"{HANIME1_BASE}/search?genre={keywords}"
        f"&sort={_TODAY_SORT}&page={page_num}"
    )

    started = datetime.now()
    items: list[dict[str, Any]] = []

    logger.info(f"hanime1 start: url={list_url} limit={limit} skip_ids={len(skip_ids)}")
    try:
        async with open_browser() as context:
            logger.info("hanime1: browser context opened")
            # ── 1) list page ─────────────────────────────────────────────
            try:
                list_page, _ = await open_page(context, list_url, goto_timeout_ms=60000)
                logger.info(f"hanime1: list page loaded: {list_url}")
            except Exception as e:
                logger.warning(f"hanime1: list_page_failed: {e}")
                return {
                    "ok": False, "source": "hanime1", "error": "list_page_failed",
                    "message": str(e),
                    "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
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
                return {
                    "ok": False, "source": "hanime1", "error": "parse_failed",
                    "message": f"xpath parse: {e}",
                    "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
                }

            # Original single-VPS xpath — pulls rank 30..1 in descending
            # order, which is how Hanime1spider.preprocess_response does it.
            # (Currently this only works on the home-rows-wrapper block, so
            # the request must be against the search URL above.)
            detail_msg: list[list[str]] = []
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
                    detail_msg.append([titles[0], hrefs[0]])

            # Map detail_msg (the original shape) into our items dict.
            for rank, (title, href) in enumerate(detail_msg, start=1):
                m = re.search(r"\?v=(\d+)", href)
                if not m:
                    continue
                vid = m.group(1)
                if href.startswith("/"):
                    url = HANIME1_BASE + href
                elif not href.startswith("http"):
                    url = HANIME1_BASE + "/" + href.lstrip("/")
                else:
                    url = href
                items.append({
                    "rank": rank,
                    "id": vid,
                    "title": title,
                    "source_url": url,
                    # placeholder; resolved below
                    "download_url": "",
                })
                if len(items) >= limit:
                    break

            if not items:
                logger.info("hanime1 list page parsed empty, returning ok=true with empty items")
                return {
                    "ok": True, "source": "hanime1", "crawled_at": _now_iso(),
                    "items": [], "warnings": [],
                    "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
                }

            # ── 2) resolve download URLs via the ported fuck_cf() ───────────
            # Mirrors Hanime1spider.parse: feed the list of
            # /download?v=<id> URLs to fuck_cf in batches of 5 and pull
            # @data-url out of the response body. skip_ids get a 0 in
            # the result so the US bot reuses the existing ch_id from db.
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
                "warnings": [],
                "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
            }

    except Exception as e:
        logger.exception("hanime1 全局异常")
        return {
            "ok": False,
            "source": "hanime1",
            "error": "internal_error",
            "message": str(e),
            "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
        }
