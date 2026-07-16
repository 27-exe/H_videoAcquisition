"""FastAPI app for HK crawler_service.

Endpoints per plan §2:
- POST /healthcheck     (no auth, returns {ok: true})
- POST /crawl/iwara     (bearer-auth, body per §2.1)
- POST /crawl/hanime1   (bearer-auth, body per §2.1)
- POST /shutdown        (bearer-auth; dev only; gated behind env)

Each crawl endpoint opens one AsyncCamoufox per request, returns CrawlResult.

Concurrency model:
- Single uvicorn worker (--workers 1) to keep this single-process.
- Module-level asyncio.Semaphore(MAX_CONCURRENT_BROWSERS) caps the number
  of in-flight camoufox instances in this process.  Requests beyond the
  cap await the semaphore (queue at FastAPI/ASGI layer).
- Per-process memory ~620MB per active firefox; with cap=3 and HK's 3.8G
  RAM, this is tight but workable.  swap 4G (set up in phase A) absorbs spikes.
"""
import asyncio
import logging
import os
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .auth import require_bearer
from .iwara import crawl_iwara
from .hanime1 import crawl_hanime1
from .browser import MAX_CONCURRENT_BROWSERS

logger = logging.getLogger(__name__)

# Module-level cap on in-flight camoufox instances.  Read once at import;
# changing MAX_CONCURRENT_BROWSERS at runtime has no effect.
_BROWSER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)

app = FastAPI(
    title="videoAcq crawler (HK)",
    description="Per-request browser endpoint. Bearer-auth required.",
    version="0.1.0",
)


# Ensure INFO+ logs are emitted to stdout for journalctl visibility.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s | %(message)s",
    force=True,
)
logging.getLogger("crawler_service").setLevel(logging.INFO)


# ─── Public, unauthenticated ──────────────────────────────────────────────
@app.get("/healthcheck")
async def healthcheck() -> dict[str, Any]:
    """No auth. Returns app uptime + browser cache status."""
    cache = os.path.expanduser("~/.cache/camoufox")
    return {
        "ok": True,
        "uptime_s": int(time.time() - _START_TIME),
        "camoufox_cache": os.path.exists(cache),
        "client_geoip_db": os.path.exists(
            os.path.expanduser("~/.cache/camoufox/GeoLite2-City.mmdb")
        ),
    }


# ─── Bearer-auth required ────────────────────────────────────────────────
@app.post("/crawl/iwara", dependencies=[Depends(require_bearer)])
async def crawl_iwara_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    async with _BROWSER_SEMAPHORE:
        logger.info(f"crawl/iwara acquired semaphore, request: {body}")
        result = await crawl_iwara(body)
    logger.info(
        f"crawl/iwara done ok={result.get('ok')} items={len(result.get('items', []))} "
        f"elapsed={result.get('elapsed_ms')}ms"
    )
    return JSONResponse(content=result, status_code=200 if result.get("ok") else 502)


@app.post("/crawl/hanime1", dependencies=[Depends(require_bearer)])
async def crawl_hanime1_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    async with _BROWSER_SEMAPHORE:
        logger.info(f"crawl/hanime1 acquired semaphore, request: {body}")
        result = await crawl_hanime1(body)
    logger.info(
        f"crawl/hanime1 done ok={result.get('ok')} items={len(result.get('items', []))} "
        f"elapsed={result.get('elapsed_ms')}ms"
    )
    return JSONResponse(content=result, status_code=200 if result.get("ok") else 502)


# ─── Dev-only, gated behind env ──────────────────────────────────────────
@app.post("/shutdown", dependencies=[Depends(require_bearer)])
async def shutdown() -> dict[str, Any]:
    if os.environ.get("CRAWLER_ENABLE_DEV_SHUTDOWN", "0") != "1":
        raise HTTPException(status_code=403, detail="dev shutdown disabled")
    logger.warning("dev shutdown endpoint hit — terminating in 0.5s")
    import signal
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM))
    return {"ok": True, "shutting_down": True}


_START_TIME = time.time()
