"""HTTP (browserless) path for hanime1.

Unlike iwara there is no JSON API here to prefer -- hanime1.me serves **fully
server-rendered HTML** (a ~154 KB list page, ~32 KB ``/download?v=`` pages).
No JavaScript is needed and **no cookie is involved**; the only gate is a
TLS-fingerprint check:

    plain requests / curl            -> HTTP 403
    curl_cffi (no impersonation)     -> HTTP 403
    curl_cffi impersonate=firefox135 -> HTTP 200, full HTML

So the browser was pure overhead for this site. It is kept only as a fallback.

Measured on HK (2026-09-12), same 30 items:
    browser path : list page + 30 /download pages   ~500-1500 s
    HTTP path    : list 0.44 s + 30 pages 1.27 s (concurrency 5) ~= 1.7 s

XPath notes
-----------
* The **list** xpath is the very same hard-coded one the browser path uses
  (``#home-rows-wrapper/div[3]/div/div/div[i]``, i = 30..1) -- it matches the
  raw HTML as well, 30/30.
* The **detail** xpath differs. The browser path's
  ``//*[@id='content-div']/div[1]/div[4]/div/div/table/tbody/tr[2]/td[5]/a/@data-url``
  is written against the *rendered* DOM and misses 0/30 in raw HTML. HTTP mode
  instead takes the first ``a[@data-url]`` under ``#content-div``, which is the
  same 1080p row the browser path picks (verified identical).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from curl_cffi.requests import AsyncSession
from lxml import html as lxml_html

logger = logging.getLogger(__name__)

HANIME1_BASE = "https://hanime1.me"

# Must be a Firefox-family fingerprint. Chrome impersonation is not required
# and plain (no impersonation) is rejected with 403 -- see module docstring.
IMPERSONATE = os.environ.get("HANIME_HTTP_IMPERSONATE", "firefox135")

DEFAULT_TIMEOUT = float(os.environ.get("HANIME_HTTP_TIMEOUT", "25"))

# Raw-HTML-safe detail xpath: first download link (1080p) inside #content-div.
DL_XPATH = "(//*[@id='content-div']//a[@data-url])[1]/@data-url"


class HanimeHTTPBlocked(Exception):
    """Raised when hanime1 answers with a block page instead of real HTML."""


def hanime1_http_mode_enabled() -> bool:
    return os.environ.get("HANIME_HTTP_MODE", "1") not in ("0", "false", "False", "no")


def new_session() -> AsyncSession:
    return AsyncSession(impersonate=IMPERSONATE)


def _looks_blocked(status: int, text: str) -> bool:
    if status in (401, 403, 429, 503):
        return True
    low = text[:3000].lower()
    return ("just a moment" in low) or ("cf-chl" in low) or ("attention required" in low)


def parse_list(html_text: str) -> list[tuple[str, str]]:
    """Extract (title, absolute source_url) pairs, rank 30..1 -- same as browser path."""
    tree = lxml_html.fromstring(html_text)
    pairs: list[tuple[str, str]] = []
    for i in range(30, 0, -1):
        hrefs = tree.xpath(
            "//*[@id='home-rows-wrapper']/div[3]/div/div/div[{i}]/div/a/@href".format(i=i)
        )
        titles = tree.xpath(
            "//*[@id='home-rows-wrapper']/div[3]/div/div/div[{i}]/@title".format(i=i)
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
    return pairs


def parse_download(html_text: str) -> Optional[str]:
    """Return the mp4 data-url from a /download page, or None."""
    tree = lxml_html.fromstring(html_text)
    found = tree.xpath(DL_XPATH)
    if not found:
        return None
    durl = found[0]
    if isinstance(durl, str) and not durl.startswith("http"):
        durl = "https:" + durl
    return durl or None


async def fetch_list(session: AsyncSession, list_url: str) -> list[tuple[str, str]]:
    r = await session.get(list_url, timeout=DEFAULT_TIMEOUT)
    text = r.text or ""
    if _looks_blocked(r.status_code, text):
        raise HanimeHTTPBlocked(f"list HTTP {r.status_code}")
    pairs = parse_list(text)
    if not pairs:
        # A well-formed page with no rows is a soft failure: let the caller
        # fall back to the browser rather than silently returning nothing.
        raise HanimeHTTPBlocked(f"list parsed 0 rows (len={len(text)})")
    return pairs


async def fetch_download(session: AsyncSession, vid: str) -> Optional[str]:
    r = await session.get(
        f"{HANIME1_BASE}/download?v={vid}", timeout=DEFAULT_TIMEOUT
    )
    text = r.text or ""
    if _looks_blocked(r.status_code, text):
        raise HanimeHTTPBlocked(f"download {vid} HTTP {r.status_code}")
    return parse_download(text)


def concurrency_limit() -> int:
    try:
        return max(1, int(os.environ.get("HANIME_HTTP_CONCURRENCY", "5")))
    except ValueError:
        return 5
