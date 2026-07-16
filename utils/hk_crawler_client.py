"""
HK crawler HTTP client (US-side).

Reads config from environment only — no token in code, no token in git.

Used by spiders/iwara/tasks.py and spiders/hanime1/tasks.py to replace
the legacy AsyncCamoufox call when dual-VPS deployment is active.

Behavior:
    1. read CRAWLER_URL from env (e.g. http://127.0.0.1:17650 from US local
       ssh-tunnel → HK :8765).
    2. send POST {URL}/crawl/{src} with bearer auth.
    3. parse response, return list of items (rank, id, title, source_url,
       download_url).
    4. on any failure (timeout, connection refused, 5xx, malformed JSON,
       ok=false): raise HKCrawlerError.

Caller: when fallback_local_browser is enabled, caller catches
HKCrawlerError and runs the original AsyncCamoufox path.
"""
import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 60
USER_AGENT = "videoAcq-bot/0.1 (dual-vps; contact: operator-local)"


class HKCrawlerError(Exception):
    """Base class for HK crawler errors.  Two subclasses differentiate
    between unreachable (network-level: must NOT retry, fall back to local
    browser immediately) and crawl failure (HK reachable but returned a bad
    payload: retry up to 3 times, then fall back).
    """
    pass


class HKUnreachable(HKCrawlerError):
    """Network-level error: HK cannot be reached at all (connection refused,
    DNS failure, ssh-tunnel down, timeout, etc.).  Caller should fall back
    to local browser immediately without retry.
    """
    pass


class HKCrawlFailure(HKCrawlerError):
    """HK is reachable but returned a bad payload: HTTP 200 with ok=false,
    non-JSON body, items field malformed, etc.  Caller may retry.
    """
    pass


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise HKCrawlerError(f"env {name} not set")
    return value


def fetch_via_hk_crawler(src: str, body: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """
    POST to HK crawler and return the parsed items list.

    src: 'iwara' or 'hanime1'
    body: optional extra params merged into default request body
          (keywords, page, limit, etc.)

    Returns:
        list of {rank, id, title, source_url, download_url} dicts.

    Raises:
        HKCrawlerError on any failure (timeout, bad status, malformed JSON,
        ok=false in response).
    """
    if src not in ("iwara", "hanime1"):
        raise HKCrawlerError(f"unknown src: {src}")

    base_url = _required_env("CRAWLER_URL").rstrip("/")
    token = _required_env("CRAWLER_BEARER_TOKEN")

    url = f"{base_url}/crawl/{src}"
    if body is not None and not isinstance(body, dict):
        raise HKCrawlerError(f"body must be dict, got {type(body)}")
    payload = {"headless": True, "limit": 30, **(body or {})}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    # \u8bfb env \u4e2d\u7684 timeout\uff1b\u4efb\u4f55\u9519\u8bef (\u4e0d\u662f\u6574\u6570\u3001\u8d85\u51fa\u8303\u56f4) \u5168\u90e8\u56de\u9ed8\u503c\u3002
    # \u907f\u514d \u300cenv CRAWLER_TIMEOUT_SEC='abc'\u300d \u5bfc\u81f4 \u5e26\u6709 ValueError \u5f92\u5f15 \u5916\u5c42\uff0c\u8ba9 bot \u91cd\u8bd5 30 \u5206\u949f\u4e00\u6b21\u3002
    timeout_str = os.environ.get("CRAWLER_TIMEOUT_SEC")
    try:
        timeout = int(timeout_str) if timeout_str else DEFAULT_TIMEOUT_SEC
        if timeout <= 0 or timeout > 600:  # \u8b66\u707e\u4e0a\u9650\uff1a10 \u5206\u949f
            logger.warning(f"CRAWLER_TIMEOUT_SEC={timeout} out of range, using default")
            timeout = DEFAULT_TIMEOUT_SEC
    except (TypeError, ValueError):
        logger.warning(
            f"CRAWLER_TIMEOUT_SEC={timeout_str!r} not a valid int, using default {DEFAULT_TIMEOUT_SEC}s"
        )
        timeout = DEFAULT_TIMEOUT_SEC

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.Timeout as e:
        raise HKUnreachable(f"timeout after {timeout}s: {e}")
    except requests.ConnectionError as e:
        raise HKUnreachable(f"connection error: {e}")
    except requests.RequestException as e:
        raise HKUnreachable(f"request error: {e}")

    if resp.status_code != 200:
        # HK reachable but returned non-200: classify as crawl failure
        # (could be 5xx HK internal, 503 HK overloaded, etc.) — caller may retry.
        raise HKCrawlFailure(
            f"http {resp.status_code} from HK: {resp.text[:200]}"
        )

    try:
        data = resp.json()
    except ValueError as e:
        # HK reachable, 200, but body isn't JSON — caller's parse path; retryable.
        raise HKCrawlFailure(f"non-JSON response: {e}")

    if not isinstance(data, dict) or not data.get("ok"):
        err = data.get("error") if isinstance(data, dict) else None
        msg = data.get("message") if isinstance(data, dict) else None
        # ok=false includes the "list_page_failed" / "parse_failed" / "internal_error"
        # cases — HK is reachable, browser failed inside.  Retry.
        raise HKCrawlFailure(f"hk crawler ok=false (err={err}, msg={msg})")

    items = data.get("items")
    if not isinstance(items, list):
        raise HKCrawlFailure(f"items not a list: {type(items)}")

    logger.info(f"hk crawler {src}: got {len(items)} items in {data.get('elapsed_ms')}ms")
    return items
