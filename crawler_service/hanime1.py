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


async def _fetch_one_download(context, video_id: str) -> str | int:
    """Open hanime1 download page, return mp4 url or 0."""
    url = f"{HANIME1_BASE}/download?v={video_id}"
    try:
        page, _ = await open_page(context, url, goto_timeout_ms=30000)
    except Exception as e:
        logger.debug(f"hanime1 download page failed v={video_id}: {e}")
        return 0

    try:
        html = await page.content()
    finally:
        await page.close()

    try:
        tree = lxml_html.fromstring(html)
        links = tree.xpath(
            '//*[@id="content-div"]/div[1]/div[4]/div/div/table/tbody/tr[2]/td[5]/a/@data-url'
        )
    except Exception:
        return 0

    if not links:
        logger.warning(f"hanime1 download link not found for v={video_id}")
        return 0
    durl = str(links[0])
    if durl and not durl.startswith("http"):
        durl = "https:" + durl
    return durl


async def crawl_hanime1(cfg: dict) -> dict:
    """Return dict per plan §2.2.

    cfg: at least {"page": int, "limit": int, "sort": "...", "skip_ids": list[int]}
    """
    page_num = int(cfg.get("page", 1))
    limit = int(cfg.get("limit", 30))
    sort_key = cfg.get("sort", "today-popular")
    skip_ids: set[str] = set(str(s) for s in cfg.get("skip_ids", []))

    list_url = f"{HANIME1_BASE}/?page={page_num}&sort={sort_key}"

    started = datetime.now()
    items: list[dict[str, Any]] = []

    try:
        async with open_browser() as context:
            # ── 1) list page ─────────────────────────────────────────────
            try:
                list_page, _ = await open_page(context, list_url, goto_timeout_ms=60000)
            except Exception as e:
                return {
                    "ok": False, "source": "hanime1", "error": "list_page_failed",
                    "message": str(e),
                    "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
                }

            try:
                html = await list_page.content()
            finally:
                await list_page.close()

            try:
                doc = lxml_html.fromstring(html)
                anchors = doc.xpath('//a[contains(@href, "?v=")]')
            except Exception as e:
                return {
                    "ok": False, "source": "hanime1", "error": "parse_failed",
                    "message": f"xpath parse: {e}",
                    "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
                }

            seen_vids: set[str] = set()
            for a in anchors:
                href = a.get("href", "")
                m = re.search(r"\?v=(\d+)", href)
                if not m:
                    continue
                vid = m.group(1)
                if vid in seen_vids:
                    continue
                # NOTE: do NOT skip here — same trick as iwara: keep all 30 items
                # so US bot can build full preview-top5, but only call the
                # download page for vid_ids NOT in skip_ids.

                # title from parent container
                title = ""
                parent = a.getparent()
                while parent is not None:
                    cls = parent.get("class", "")
                    t_attr = parent.get("title", "")
                    if t_attr:
                        title = t_attr.strip()[:200]
                        break
                    parent = parent.getparent()

                if not title:
                    text = " ".join(a.text_content().split())
                    text = re.sub(r"\b(thumb_up|thumb_down|%|次|views?)\b", "", text, flags=re.I)
                    title = text.strip()[:200]

                seen_vids.add(vid)

                if href.startswith("/"):
                    url = HANIME1_BASE + href
                elif not href.startswith("http"):
                    url = HANIME1_BASE + "/" + href.lstrip("/")
                else:
                    url = href

                items.append({
                    "rank": len(items) + 1,
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

            # ── 2) resolve download URLs (batched in-browser) ────────────
            for batch_start in range(0, len(items), _DL_BATCH_SIZE):
                batch_end = min(batch_start + _DL_BATCH_SIZE, len(items))
                for i in range(batch_start, batch_end):
                    vid = items[i]["id"]
                    if vid in skip_ids:
                        items[i]["download_url"] = "0"  # already in US db, reuse ch_id
                        continue
                    durl = await _fetch_one_download(context, vid)
                    items[i]["download_url"] = durl if isinstance(durl, str) else str(durl)
                if batch_end < len(items):
                    await asyncio.sleep(_DL_BATCH_DELAY)

            # filter: empty download_url → 0
            for it in items:
                if not it.get("download_url"):
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
