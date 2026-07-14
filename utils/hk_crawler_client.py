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
    """Raised when HK crawler endpoint is unreachable or returns failure."""
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

    timeout = int(os.environ.get("CRAWLER_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC))

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.Timeout as e:
        raise HKCrawlerError(f"timeout after {timeout}s: {e}")
    except requests.ConnectionError as e:
        raise HKCrawlerError(f"connection error: {e}")
    except requests.RequestException as e:
        raise HKCrawlerError(f"request error: {e}")

    if resp.status_code != 200:
        raise HKCrawlerError(
            f"http {resp.status_code} from HK: {resp.text[:200]}"
        )

    try:
        data = resp.json()
    except ValueError as e:
        raise HKCrawlerError(f"non-JSON response: {e}")

    if not isinstance(data, dict) or not data.get("ok"):
        err = data.get("error") if isinstance(data, dict) else None
        msg = data.get("message") if isinstance(data, dict) else None
        raise HKCrawlerError(f"hk crawler ok=false (err={err}, msg={msg})")

    items = data.get("items")
    if not isinstance(items, list):
        raise HKCrawlerError(f"items not a list: {type(items)}")

    logger.info(f"hk crawler {src}: got {len(items)} items in {data.get('elapsed_ms')}ms")
    return items
