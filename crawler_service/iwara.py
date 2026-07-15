"""iwara crawler (HK side). Per plan §1: zero persistence, no DB."""
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any
from lxml import html as lxml_html

from .browser import open_browser, open_page

logger = logging.getLogger(__name__)

IWARA_BASE = "https://www.iwara.tv"


def _now_iso() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).date().isoformat()


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
            # 1) 列表页: 用 lxml 解析 .page-videoList__item 节点
            try:
                list_page, _ = await open_page(context, list_url, goto_timeout_ms=60000)
            except Exception as e:
                return {
                    "ok": False,
                    "source": "iwara",
                    "error": "list_page_failed",
                    "message": str(e),
                    "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
                }

            try:
                html = await list_page.content()
            finally:
                await list_page.close()

            try:
                doc = lxml_html.fromstring(html)
                # iwara 列表项 class: page-videoList__item
                items_el = doc.xpath(
                    '//*[contains(concat(" ", normalize-space(@class), " "), " page-videoList__item ")]'
                )
            except Exception as e:
                return {
                    "ok": False,
                    "source": "iwara",
                    "error": "parse_failed",
                    "message": f"xpath parse: {e}",
                    "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
                }

            for el in items_el:
                href_list = el.xpath('.//a[contains(@href, "/video/")]/@href')
                if not href_list:
                    continue
                # 从 /video/{ID}/{slug} 抽 ID
                m = re.match(r"/video/([a-zA-Z0-9_-]+)/.*", href_list[0])
                if not m:
                    continue
                vid_id = m.group(1)
                url = IWARA_BASE + href_list[0]
                title_el = el.xpath('.//*[contains(@class, "videoTeaser__title")]')
                title = (
                    title_el[0].text_content().strip()[:100]
                    if title_el
                    else ""
                )
                items.append({
                    "rank": len(items) + 1,
                    "id": vid_id,
                    "title": title,
                    "source_url": url,
                    # Phase 1 placeholder; 真实生产中此 id 在 source.video 详情页拿
                    # .details.playlist[0].src.download
                    "download_url": f"{IWARA_BASE}/video/{vid_id}",  # 仍用详情页 URL 占位
                })
                if len(items) >= limit:
                    break

            if not items:
                # "no videos" \u662f\u5408\u6cd5\u7ed3\u679c (\u4eca\u5929\u5217\u8868\u9875\u7a7a\u3001\u6216\u5173\u952e\u8bcd\u8fc7\u51b6),
                # \u8fd4 ok=true + items=[],\u4e0d\u5e94\u4f7f US \u7aef fallback \u672c\u5730 (\u672c\u5730\u540c\u6837\u62a2\u4e0d\u5230) \u6216 \u5916\u5c42 catch\u3002
                logger.info("iwara list page parsed empty, returning ok=true with empty items")
                items = []

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
