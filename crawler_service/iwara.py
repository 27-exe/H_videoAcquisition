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
# 2026-09-12: 浏览器无关的 HTTP 路径(apiq JSON API + curl_cffi 复用 cf_clearance)。
# 浏览器仅用于 mint/兜底,见 iwara_http.py 顶部说明与 L2 handoff 文档。
from .iwara_http import (
    IwaraHTTPBlocked,
    fetch_list as http_fetch_list,
    fetch_video as http_fetch_video,
    http_concurrency as http_concurrency_limit,
    http_mode_enabled,
    new_session as http_new_session,
)

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

    # ── 1) list page ────────────────────────────────────────────────────
    # Preferred: apiq JSON API over HTTP (no browser). Fallback: the ported
    # preprocess_iwara_list (camoufox + CF solver) — kept for the case where
    # cf_clearance is missing/expired, or iwara changes its API shape.
    http_ok = http_mode_enabled()
    list_rows: list[tuple[str, str]] = []  # (title, source_url)

    if http_ok:
        try:
            async with http_new_session(storage_state) as hs:
                got = await http_fetch_list(hs, keywords, page_num)
            if got:
                list_rows = [(r["title"], r["source_url"]) for r in got]
                logger.info(f"iwara: list via HTTP API ok, n={len(list_rows)}")
            else:
                logger.warning("iwara: HTTP list returned 0 items → browser fallback")
                http_ok = False
        except IwaraHTTPBlocked as e:
            logger.warning(f"iwara: HTTP list blocked ({e}) → browser fallback")
            http_ok = False
        except Exception as e:  # noqa: BLE001 — never fail the crawl on HTTP issues
            logger.warning(f"iwara: HTTP list error ({e!r}) → browser fallback")
            http_ok = False

    if not http_ok:
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
        logger.info(f"iwara: list page parsed {len(pairs)} items (browser path)")
        list_rows = list(pairs)

    items: list[dict[str, Any]] = []
    for rank, (title, source_url) in enumerate(list_rows[:limit], start=1):
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

    # 两条路径(2026-09-12 架构改造):
    #   HTTP   —— curl_cffi 仿冒 Firefox + 复用 cf_clearance 打 apiq JSON API。
    #             实测 30 个 URL 共 1.5s、成功率 30/30(浏览器路径需 300-1500s/86%)。
    #   浏览器 —— camoufox + fuck_cf;仅在 HTTP 被 CF 拦 / IWARA_HTTP_MODE=0 时启用,
    #             也是 cf_clearance 失效时的自救路径。
    # env:
    #   IWARA_HTTP_MODE=1|0        优先 HTTP(默认 1)
    #   IWARA_HTTP_CONCURRENCY=5   HTTP 并发度
    #   IWARA_API_BATCH_SIZE=5     浏览器路径每批 URL 数(每批 = 1 次浏览器冷启动)
    #   IWARA_API_CONCURRENCY=1    浏览器路径并发批次数(>1 会撞上游限流,慎用)
    #   IWARA_API_BATCH_SLEEP=10   浏览器路径批次间礼貌间隔(仅串行生效)
    api_batch_size = max(1, int(os.environ.get("IWARA_API_BATCH_SIZE", "5")))
    api_concurrency = max(1, int(os.environ.get("IWARA_API_CONCURRENCY", "1")))
    api_batch_sleep = float(os.environ.get("IWARA_API_BATCH_SLEEP", "10"))

    api_ok = 0
    api_fail = 0
    api_skip = len(items) - len(api_needed)

    def _consume_api_content(idx: int, vid: str, content: Any) -> bool:
        """Turn one apiq payload into items[idx]['_api_file'] / download_url_list[idx]."""
        if content == 0 or content == "" or content is None or isinstance(content, Exception):
            logger.warning(f"iwara api fail: vid={vid} result={type(content).__name__}")
            download_url_list[idx] = ""
            return False
        # fuck_cf(need_resp=True) 返回已解析对象;HTTP 路径同样直接给 dict。
        # 仅在拿到字符串时才走 JSON 字符串解析(兼容旧行为)。
        if isinstance(content, (dict, list)):
            parsed = [content] if isinstance(content, dict) else content
        else:
            parsed = _parse_api_json(str(content))
        if not parsed:
            logger.warning(f"iwara api parse empty: vid={vid}")
            download_url_list[idx] = ""
            return False
        items[idx]["_api_file"] = parsed[0]
        return True

    # ── 2a) HTTP path(preferred) ────────────────────────────────────────
    if http_ok and api_needed:
        _http_conc = http_concurrency_limit()
        _http_sem = asyncio.Semaphore(_http_conc)
        logger.info(
            f"iwara api plan (HTTP): {len(api_needed)} vids, concurrency={_http_conc}"
        )
        try:
            async with http_new_session(storage_state) as hs:

                async def _one_http(idx: int, vid: str) -> tuple[int, str, Any, Any]:
                    async with _http_sem:
                        try:
                            data = await http_fetch_video(hs, vid)
                            return idx, vid, data, None
                        except Exception as e:  # noqa: BLE001
                            return idx, vid, None, e

                tasks = [asyncio.create_task(_one_http(i, v)) for i, v in api_needed]
                blocked_err: Any = None
                for fut in asyncio.as_completed(tasks):
                    idx, vid, data, err = await fut
                    if err is not None:
                        if isinstance(err, IwaraHTTPBlocked):
                            blocked_err = blocked_err or err
                            continue
                        api_fail += 1
                        download_url_list[idx] = ""
                        logger.warning(f"iwara http api error: vid={vid} {err!r}")
                        continue
                    if _consume_api_content(idx, vid, data):
                        api_ok += 1
                    else:
                        api_fail += 1
                if blocked_err is not None:
                    raise IwaraHTTPBlocked(str(blocked_err))
            logger.info(
                f"iwara api (HTTP) done: ok={api_ok} fail={api_fail} of {len(api_needed)}"
            )
        except IwaraHTTPBlocked as e:
            logger.warning(
                f"iwara: HTTP api blocked ({e}) → browser fallback for the rest "
                f"(ok={api_ok} fail={api_fail} so far)"
            )
            http_ok = False
        except Exception as e:  # noqa: BLE001 — never fail the whole crawl on HTTP issues
            logger.warning(f"iwara: HTTP api error ({e!r}) → browser fallback")
            http_ok = False

    # ── 2b) browser path(fallback / IWARA_HTTP_MODE=0) ──────────────────
    if not http_ok:
        remaining = [(i, v) for (i, v) in api_needed if "_api_file" not in items[i]]
        logger.info(
            f"iwara api plan (browser): {len(remaining)}/{len(api_needed)} vids remain"
        )
        batches = [
            remaining[i:i + api_batch_size]
            for i in range(0, len(remaining), api_batch_size)
        ]
        logger.info(
            f"iwara api plan: {len(batches)} batch(es) of <= {api_batch_size}, "
            f"concurrency={api_concurrency}, inter-batch sleep={api_batch_sleep}s"
        )
        _api_sem = asyncio.Semaphore(api_concurrency)

        async def _resolve_api_batch(bi: int, batch: list[tuple[int, str]]) -> None:
            """Run one fuck_cf() batch; fill download_url_list / items[*]._api_file."""
            nonlocal api_ok, api_fail
            urls: list = [f"{IWARA_API}/{vid}" for _, vid in batch]
            logger.info(
                f"iwara api batch #{bi} [{batch[0][0]}-{batch[-1][0]}] "
                f"n={len(batch)} ids={[v for _, v in batch]}"
            )
            async with _api_sem:
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
                if _consume_api_content(idx, vid, content):
                    api_ok += 1
                else:
                    api_fail += 1

        if api_concurrency == 1:
            # 串行:保留原有批次间 sleep(对 iwara 礼貌,避免风控)
            for bi, batch in enumerate(batches):
                await _resolve_api_batch(bi, batch)
                if bi < len(batches) - 1:
                    await asyncio.sleep(api_batch_sleep)
        else:
            # 并发:批量同时投递;不再额外 sleep(并发本身已摊开请求)
            await asyncio.gather(*[_resolve_api_batch(bi, b) for bi, b in enumerate(batches)])

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