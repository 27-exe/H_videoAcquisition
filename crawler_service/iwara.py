"""iwara crawler (HK side). Per plan §1: zero persistence, no DB.

Re-implementation note: this module used to inline a slimmed-down CF-bypass
loop that proved much weaker than the original single-VPS `fuck_cf()`.
We now call the ported `fuck_cf()` from `crawler_service.fuck_cf` so the HK
end matches the single-VPS production behaviour (proxy support, 60s page
timeout, per-URL retry with exponential backoff, Cloudflare interstitial
detection + ClickSolver fallback).

Output contract kept: returns the same JSON shape US bot already consumes:
  {
    "ok": bool,
    "source": "iwara",
    "items": [{"rank", "id", "title", "source_url", "download_url"}, ...],
    ...
  }
"""
import asyncio
import hashlib
import html as _html
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any

import aiohttp
from lxml import html as lxml_html

from .browser import open_browser, open_page
from .fuck_cf import fuck_cf, preprocess_iwara_list

logger = logging.getLogger(__name__)

IWARA_BASE = "https://www.iwara.tv"
IWARA_API = "https://apiq.iwara.tv/video"
IWARA_OBFUSCATION_SUFFIX = "_mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"

# Path to the iwara storage_state file. We do NOT ship a real auth file in
# the HK repo; the operator can rsync one over (or have it auto-regenerated
# via the standalone login() helper). The single-VPS path tolerated a missing
# file, so we keep that behaviour: only pass it to fuck_cf() if present.
_DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "config", "auth", "iwara_auth.json"
)


def _now_iso() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).date().isoformat()


def _parse_api_json(text: str) -> list[dict[str, Any]]:
    """Parse iwara API response (raw JSON or HTML-wrapped <pre>{...}</pre>)."""
    import json
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except Exception:
        pass
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
    m = re.search(r"(\[.*\])", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, list):
                return data
        except Exception:
            pass
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
    session: aiohttp.ClientSession, api_file: dict[str, Any], timeout: float = 10.0
) -> str | int:
    """Deobfuscate one iwara API file object → real mp4 URL or 0."""
    if not api_file:
        return 0
    try:
        file_url = api_file.get("fileUrl", "")
        file_id = str(api_file.get("file", {}).get("id", ""))
    except Exception:
        return 0
    if not file_url or not file_id:
        return 0

    file_url = _html.unescape(file_url)
    m = re.search(r"[?&]expires=(\d+)", file_url)
    if not m:
        return 0
    expires = m.group(1)

    sha_key = file_id + "_" + expires + IWARA_OBFUSCATION_SUFFIX
    t_hash = hashlib.sha1(sha_key.encode()).hexdigest()

    try:
        async with session.get(
            file_url,
            headers={"X-Version": t_hash},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except Exception as e:
        logger.warning(
            f"_resolve_one_download: deobf HTTP failed id={file_id} "
            f"url={file_url[:80]!r} err={type(e).__name__}: {e}"
        )
        return 0

    if not isinstance(data, list):
        return 0

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


def _load_storage_state(path: str):
    if not os.path.exists(path):
        return None
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "cookies" in data:
            return data
    except Exception:
        return None
    return None


def _load_proxy_from_cfg(cfg: dict):
    """Resolve proxy settings from cfg (iwara.yaml on US bot side).

    Falls back to env vars CRAWLER_PROXY_URL / CRAWLER_PROXY_NAME /
    CRAWLER_PROXY_PASS if the cfg does not include them — that lets the HK
    operator set the proxy independently of the US yaml.
    """
    proxy_url = cfg.get("proxy_url")
    pro_name = cfg.get("proxy_name")
    pro_word = cfg.get("proxy_pass")
    if not proxy_url:
        proxy_url = os.environ.get("CRAWLER_PROXY_URL")
        pro_name = os.environ.get("CRAWLER_PROXY_NAME") or pro_name
        pro_word = os.environ.get("CRAWLER_PROXY_PASS") or pro_word
    if proxy_url:
        # Uncomment to debug proxy resolution.
        # logger.info(f"iwara: using proxy {proxy_url}")
        pass
    else:
        logger.info("iwara: no proxy configured, using geoip-only CF bypass")
    return proxy_url, pro_name, pro_word


async def crawl_iwara(cfg: dict) -> dict:
    """Original single-VPS iwara pipeline, ported to HK crawler service.

    cfg: at least {"keywords", "page", "limit", "skip_ids", optional proxy_*}.
    """
    keywords = cfg.get("keywords", "trending")
    page_num = int(cfg.get("page", 1))
    limit = int(cfg.get("limit", 30))
    skip_ids: set[str] = set(cfg.get("skip_ids", []))

    list_url = f"{IWARA_BASE}/videos?sort={keywords}&page={page_num}"
    proxy_url, pro_name, pro_word = _load_proxy_from_cfg(cfg)
    storage_state = _load_storage_state(_DEFAULT_STATE_PATH)

    started = datetime.now()
    logger.info(
        f"iwara start: url={list_url} limit={limit} skip_ids={len(skip_ids)} "
        f"proxy={'yes' if proxy_url else 'no'} state={'yes' if storage_state else 'no'}"
    )

    # ── 1) list page via the ported preprocess_iwara_list ───────────────
    # This brings over the original retry/backoff/CF-solver behaviour and
    # the >=20-items success threshold. Empty list → failure, not "ok=true".
    pairs = await preprocess_iwara_list(
        list_url,
        proxy_str=proxy_url,
        pro_name=pro_name,
        pro_word=pro_word,
        storage_state=storage_state,
        max_retries=5,
        min_items=20,
    )
    if not pairs:
        return {
            "ok": False,
            "source": "iwara",
            "error": "list_page_failed",
            "message": "preprocess_iwara_list returned empty after retries",
            "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
        }
    logger.info(f"iwara: list page parsed {len(pairs)} items")

    items: list[dict[str, Any]] = []
    for rank, (title, source_url) in enumerate(pairs[:limit], start=1):
        m = re.match(r"https?://www\.iwara\.tv/video/([a-zA-Z0-9_-]+)/?", source_url)
        if not m:
            continue
        vid_id = m.group(1)
        items.append(
            {
                "rank": rank,
                "id": vid_id,
                "title": title,
                "source_url": source_url,
                "download_url": "",  # resolved below (0 if vid_id in skip_ids)
            }
        )

    if not items:
        return {
            "ok": False,
            "source": "iwara",
            "error": "parse_failed",
            "message": "no video ids recovered from list page",
            "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
        }

    # ── 2) resolve download URLs via the ported fuck_cf() ────────────────
    # Single-VPS pattern: batch 5 apiq URLs, pass each batch through
    # fuck_cf(...) so the CF bypass + per-URL retry behaviour matches.
    # skip_ids ids don't need an api call — leave download_url=0 for the
    # US bot to look them up in its own db.
    download_url_list: list[Any] = [""] * len(items)
    api_needed: list[tuple[int, str]] = [
        (i, it["id"]) for i, it in enumerate(items) if it["id"] not in skip_ids
    ]
    logger.info(
        f"iwara: resolving download urls for {len(api_needed)} vids "
        f"(skipped {len(items) - len(api_needed)} via skip_ids)"
    )

    # Batch 5 URLs at a time, same as the original spider's parse() loop.
    api_batch_size = 5
    api_ok = 0
    api_fail = 0
    api_skip = len(items) - len(api_needed)

    for batch_start in range(0, max(len(api_needed), 1), api_batch_size):
        batch = api_needed[batch_start:batch_start + api_batch_size]
        if not batch:
            continue
        urls = [f"{IWARA_API}/{vid}" for _, vid in batch]
        logger.info(f"iwara api batch [{batch_start}-{batch_start + len(batch) - 1}] ids={[v for _, v in batch]}")

        results = await fuck_cf(
            urls,
            proxy_str=proxy_url,
            pro_name=pro_name,
            pro_word=pro_word,
            storage_state=storage_state,
            need_resp=True,  # apiq.iwara.tv responds with JSON
            select=None,
            max_retries=3,
        )

        for (idx, vid), content in zip(batch, results):
            if content == 0 or content == "" or isinstance(content, Exception):
                api_fail += 1
                logger.warning(f"iwara api fail: vid={vid} result={type(content).__name__}")
                download_url_list[idx] = ""
                continue
            # fuck_cf(need_resp=True) returns the parsed JSON object directly
            # (dict for a single video, list for an array response). Only fall
            # back to the JSON-string parser when the content is a string.
            if isinstance(content, (dict, list)):
                parsed = [content] if isinstance(content, dict) else content
            else:
                parsed = _parse_api_json(str(content))
            if not parsed:
                api_fail += 1
                logger.warning(f"iwara api parse empty: vid={vid}")
                download_url_list[idx] = ""
                continue
            api_ok += 1
            # stash the first parsed dict for the deobf stage
            items[idx]["_api_file"] = parsed[0]

        # Mirror the original 10s sleep between api batches.
        if batch_start + api_batch_size < len(api_needed):
            await asyncio.sleep(10)

    logger.info(
        f"iwara api done: skip={api_skip} ok={api_ok} fail={api_fail} total={len(items)}"
    )

    # ── 3) deobfuscate (aiohttp only, no browser) ───────────────────────
    # Same as before: aiohttp with SHA1, capped at 2 concurrent, fail-fast
    # timeout. The deobf semantics didn't change — only the upstream API
    # fetch was weak, and that's now handled by fuck_cf().
    _DEOBF_TIMEOUT = 10
    _SEM = asyncio.Semaphore(2)
    deobf_ok = 0
    deobf_fail = 0
    deobf_skip = sum(1 for i, it in enumerate(items) if not it.get("_api_file"))

    async def _deobf_one(api_file, idx):
        async with _SEM:
            return await _resolve_one_download(
                session=http_session, api_file=api_file, timeout=_DEOBF_TIMEOUT
            )

    async with aiohttp.ClientSession() as http_session:
        for i, it in enumerate(items):
            if not it.get("_api_file"):
                continue
            url = await _deobf_one(it["_api_file"], i)
            if isinstance(url, str) and url:
                download_url_list[i] = url
                deobf_ok += 1
                logger.info(f"iwara deobf ok: idx={i} url={url[:60]}...")
            else:
                deobf_fail += 1
                logger.warning(f"iwara deobf fail: idx={i}")

    logger.info(
        f"iwara deobf done: ok={deobf_ok} skip={deobf_skip} fail={deobf_fail}"
    )

    # ── 4) cleanup and return ──────────────────────────────────────────
    for it in items:
        it.pop("_api_file", None)
        it["download_url"] = download_url_list[items.index(it)] if False else None

    # Rebuild items in rank order with the correct download_url mapping.
    # (The list-comprehension above using items.index() is unreliable, so we
    # rebuild explicitly.)
    out_items: list[dict[str, Any]] = []
    for i, it in enumerate(items):
        out_items.append(
            {
                "rank": it["rank"],
                "id": it["id"],
                "title": it["title"],
                "source_url": it["source_url"],
                "download_url": download_url_list[i],
            }
        )
    items = out_items

    # Items without a valid download_url become 0 (US bot will reuse ch_id
    # from db when 0 is hit, or skip otherwise — exactly the original
    # behaviour).
    for it in items:
        if not it.get("download_url"):
            it["download_url"] = 0

    zero_count = sum(1 for it in items if it.get("download_url") == 0)
    logger.info(f"iwara: items without download_url: {zero_count}/{len(items)}")
    for i, it in enumerate(items):
        if it.get("download_url") == 0:
            logger.info(f"  zero-idx={i} id={it['id']} title={(it.get('title') or '')[:40]}")

    return {
        "ok": True,
        "source": "iwara",
        "crawled_at": _now_iso(),
        "items": items,
        "warnings": [],
        "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
    }


# Keep `open_browser` and `open_page` re-exported for any external call sites
# (hanime1 still imports them directly).
__all__ = ["crawl_iwara", "open_browser", "open_page"]