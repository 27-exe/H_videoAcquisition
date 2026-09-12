"""Per-request browser context manager.

Wraps AsyncCamoufox with sane defaults:
- headless=True
- geoip=True (HK egress pretends to be HK-resident for CF filters)
- proxy=None (HK doesn't need proxy; CF-passes via geoip alone)
- each call: fresh browser instance, scoped lifetime
"""
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from camoufox import AsyncCamoufox
from playwright_captcha.utils.camoufox_add_init_script.add_init_script import get_addon_path

logger = logging.getLogger(__name__)

ADDON_PATH = get_addon_path()
MAX_CONCURRENT_BROWSERS = max(1, int(os.environ.get("MAX_CONCURRENT_BROWSERS", "1")))
# 2026-09-10: 原硬编码 3,在 camoufox 0.5.4 / Firefox 152 下会撑爆 unit 的
# MemoryMax=2G → cgroup OOM kill(实测 task="Web Content" anon-rss ~1.15GB)。
# 默认 1 = 单浏览器,内存最省;2026-09-12 改为 env 可调以便做并发加速试验
# (配合 IWARA_API_CONCURRENCY,并同步抬高 unit 的 MemoryMax)。

# Reuse addon across calls (read-only)
_addon_abs = os.path.abspath(ADDON_PATH)


@asynccontextmanager
async def open_browser() -> AsyncIterator:
    """Open one AsyncCamoufox instance, yield a freshly-made context.

    Caller is responsible for closing pages; the async-with cleans up everything.
    """
    async with AsyncCamoufox(
        headless=True,
        geoip=True,
        humanize=True,
        i_know_what_im_doing=True,
        config={"forceScopeAccess": True},
        disable_coop=True,
        main_world_eval=True,
        proxy=None,
        addons=[_addon_abs],
    ) as browser:
        context = await browser.new_context()
        try:
            yield context
        finally:
            await context.close()


async def open_page(context, url: str, goto_timeout_ms: int = 60000) -> object:
    """Open a fresh page, navigate, return page. Caller closes."""
    page = await context.new_page()
    response = await page.goto(url, wait_until="domcontentloaded", timeout=goto_timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass  # best-effort wait
    return page, response
