"""HTTP (browserless) path for iwara, via apiq.iwara.tv's JSON APIs.

Why this module exists
----------------------
The browser path (camoufox + `fuck_cf`) is only needed to *solve Cloudflare's
JS challenge and mint the `cf_clearance` cookie*. Once that cookie exists,
apiq.iwara.tv happily serves plain JSON to any client whose **TLS fingerprint
matches a real browser** — and curl_cffi impersonates Firefox, i.e. the same
browser family as camoufox, so the cookie is accepted.

The ordinary `requests`/`urllib` clients fail here (403 "Just a moment...")
even *with* the cookie, because Cloudflare binds `cf_clearance` to the TLS/JA3
fingerprint; that is why this module must use curl_cffi rather than aiohttp.

Measured on the HK box (2026-09-12, same 30 videos):
    browser path : list 15-51s  +  29 apiq fetches 300-1500s  (86% success,
                   each blocked URL burns 60s x 3 retries ~ 180s)
    this module  : list  0.28s  +  30 apiq fetches  1.5s       (30/30 success)

Endpoints used
--------------
    list   : GET https://apiq.iwara.tv/videos?sort=<sort>&page=<n>  -> JSON
    detail : GET https://apiq.iwara.tv/video/<id>                   -> JSON

Both need the cf_clearance cookie + a Firefox-shaped TLS handshake. When either
answers with a challenge, `IwaraHTTPBlocked` is raised so the caller can fall
back to the (slower but self-sufficient) browser path.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)

IWARA_BASE = "https://www.iwara.tv"
IWARA_APIQ = "https://apiq.iwara.tv"

# Must stay in the Firefox family: the cf_clearance cookie we reuse was minted
# by camoufox (Firefox). A Chrome-shaped handshake would be rejected by CF.
IMPERSONATE = os.environ.get("IWARA_HTTP_IMPERSONATE", "firefox135")

DEFAULT_TIMEOUT = float(os.environ.get("IWARA_HTTP_TIMEOUT", "25"))


class IwaraHTTPBlocked(Exception):
    """apiq answered with a Cloudflare challenge / non-JSON instead of data.

    Caller should fall back to the browser path (and ideally re-mint
    cf_clearance via camoufox).
    """


def http_mode_enabled() -> bool:
    """env IWARA_HTTP_MODE: '1' (default) = prefer HTTP, '0' = browser only."""
    return os.environ.get("IWARA_HTTP_MODE", "1") not in ("0", "false", "False", "no")


def http_concurrency() -> int:
    return max(1, int(os.environ.get("IWARA_HTTP_CONCURRENCY", "5")))


def cookies_from_state(storage_state: Optional[dict]) -> dict[str, str]:
    """Playwright storage_state dict -> flat {name: value} for curl_cffi."""
    if not storage_state:
        return {}
    out: dict[str, str] = {}
    for c in storage_state.get("cookies") or []:
        name = c.get("name")
        if name:
            out[name] = c.get("value", "") or ""
    return out


def new_session(storage_state: Optional[dict]) -> AsyncSession:
    return AsyncSession(
        impersonate=IMPERSONATE,
        cookies=cookies_from_state(storage_state),
    )


def _looks_blocked(status: int, text: str) -> bool:
    if status in (401, 403, 429, 503):
        return True
    low = (text or "")[:2000].lower()
    return ("just a moment" in low) or ("cf-chl" in low) or ("attention required" in low)


async def fetch_list(session: AsyncSession, sort: str, page: int) -> list[dict[str, Any]]:
    """GET apiq/videos -> [{id, title, source_url}, ...] keeping source order."""
    url = f"{IWARA_APIQ}/videos?sort={sort}&page={page}"
    r = await session.get(url, timeout=DEFAULT_TIMEOUT)
    text = r.text or ""
    if _looks_blocked(r.status_code, text):
        raise IwaraHTTPBlocked(f"list HTTP {r.status_code} (cf challenge?)")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise IwaraHTTPBlocked(f"list non-JSON: {e}") from e
    if not isinstance(data, dict):
        raise IwaraHTTPBlocked("list payload not an object")
    results = data.get("results")
    if not isinstance(results, list):
        raise IwaraHTTPBlocked("list payload missing 'results'")

    out: list[dict[str, Any]] = []
    for it in results:
        if not isinstance(it, dict):
            continue
        vid = it.get("id")
        if not vid:
            continue
        slug = it.get("slug") or ""
        out.append(
            {
                "id": vid,
                "title": it.get("title") or "",
                "source_url": (
                    f"{IWARA_BASE}/video/{vid}/{slug}" if slug else f"{IWARA_BASE}/video/{vid}"
                ),
            }
        )
    return out


async def fetch_video(session: AsyncSession, vid: str) -> dict[str, Any]:
    """GET apiq/video/<id> -> parsed JSON object (feeds the deobf stage)."""
    url = f"{IWARA_APIQ}/video/{vid}"
    r = await session.get(url, timeout=DEFAULT_TIMEOUT)
    text = r.text or ""
    if _looks_blocked(r.status_code, text):
        raise IwaraHTTPBlocked(f"video {vid} HTTP {r.status_code} (cf challenge?)")
    try:
        data = json.loads(text)
    except ValueError as e:
        raise IwaraHTTPBlocked(f"video {vid} non-JSON: {e}") from e
    if not isinstance(data, dict):
        raise IwaraHTTPBlocked(f"video {vid} payload not an object")
    return data
