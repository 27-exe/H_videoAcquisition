"""hanime1 crawler (HK side). Per plan §1: zero persistence, no DB."""
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

from lxml import html as lxml_html

from .browser import open_browser, open_page

logger = logging.getLogger(__name__)

HANIME1_BASE = "https://hanime1.me"


def _now_iso() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).date().isoformat()


async def crawl_hanime1(cfg: dict) -> dict:
    """Return dict per plan §2.2.

    cfg: at least {"page": int, "limit": int, "sort": "..."}
    """
    page_num = int(cfg.get("page", 1))
    limit = int(cfg.get("limit", 30))
    sort_key = cfg.get("sort", "today-popular")

    list_url = f"{HANIME1_BASE}/?page={page_num}&sort={sort_key}"

    started = datetime.now()
    items: list[dict[str, Any]] = []

    try:
        async with open_browser() as context:
            try:
                list_page, _ = await open_page(context, list_url, goto_timeout_ms=60000)
            except Exception as e:
                return {
                    "ok": False,
                    "source": "hanime1",
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
                anchors = doc.xpath('//a[contains(@href, "?v=")]')
            except Exception as e:
                return {
                    "ok": False,
                    "source": "hanime1",
                    "error": "parse_failed",
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

                # 标题: 父级 .video-item-container 的 title 属性
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
                    # fallback: 爬 anchor 的 text_content() 扣掉点赞/时长的噪声
                    text = " ".join(a.text_content().split())
                    # 常见噪声词
                    text = re.sub(r"\b(thumb_up|thumb_down|%|次|views?)\b", "", text, flags=re.I)
                    title = text.strip()[:200]

                seen_vids.add(vid)
                # 转绝对 URL
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
                    "download_url": url,  # Phase 1 placeholder
                })
                if len(items) >= limit:
                    break

            if not items:
                # "no videos" \u662f\u5408\u6cd5\u7ed3\u679c, ok=true + items=[], \u4e0d\u8d70 fallback / catch
                logger.info("hanime1 list page parsed empty, returning ok=true with empty items")
                items = []

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
