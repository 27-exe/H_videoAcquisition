"""FastAPI app for HK crawler_service.

Endpoints per plan §2:
- POST /healthcheck     (no auth, returns {ok: true})
- POST /crawl/iwara     (bearer-auth, body per §2.1)
- POST /crawl/hanime1   (bearer-auth, body per §2.1)
- POST /shutdown        (bearer-auth; dev only; gated behind env)

Each crawl endpoint opens one AsyncCamoufox per request, returns CrawlResult.
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

logger = logging.getLogger(__name__)

app = FastAPI(
    title="videoAcq crawler (HK)",
    description="Per-request browser endpoint. Bearer-auth required.",
    version="0.1.0",
)


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
    logger.info(f"crawl/iwara request: {body}")
    result = await crawl_iwara(body)
    return JSONResponse(content=result, status_code=200 if result.get("ok") else 502)


@app.post("/crawl/hanime1", dependencies=[Depends(require_bearer)])
async def crawl_hanime1_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    logger.info(f"crawl/hanime1 request: {body}")
    result = await crawl_hanime1(body)
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
