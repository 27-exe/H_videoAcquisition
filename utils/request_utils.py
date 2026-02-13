import aiohttp
import logging
import os
import asyncio
from typing import Optional, Dict
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from camoufox import AsyncCamoufox
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType
from playwright_captcha.utils.camoufox_add_init_script.add_init_script import get_addon_path

ADDON_PATH = get_addon_path()

# ---------------
# 这里设置最大并发数，建议设置为 2 到 4，取决于你的机器性能
# 每一个实例都是一个浏览器，设置太高会让机器卡死
MAX_CONCURRENT_BROWSERS = 3
_BROWSER_SEMAPHORE = None  # 初始化为 None
# ----------------

logger = logging.getLogger(__name__)


@asynccontextmanager
async def creat_session(header: Optional[Dict[str, str]] = None):
    async with aiohttp.ClientSession(headers=header) as session:
        yield session


async def fetch(session: aiohttp.ClientSession, url: str, proxy: Optional[str] = None) -> str:
    async with session.get(url, proxy=proxy) as resp:
        resp.raise_for_status()
        return await resp.text()


async def fuck_cf(urls: str | list[str], proxy: Optional[str] = None):
    """
    支持传入单个 URL 或 URL 列表。
    如果是列表，将复用同一个 Context (共享 Cookie)，仅在必要时点击 CF。
    """
    global _BROWSER_SEMAPHORE
    if _BROWSER_SEMAPHORE is None:
        _BROWSER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)
    screen_shot = Path("error_shot")
    screen_shot.mkdir(exist_ok=True)

    # 统一转为列表处理
    url_list = [urls] if isinstance(urls, str) else urls
    results = []

    async with _BROWSER_SEMAPHORE:
        # 启动浏览器实例
        async with AsyncCamoufox(
                headless=True,
                geoip=True,
                humanize=True,
                i_know_what_im_doing=True,
                config={'forceScopeAccess': True},
                disable_coop=True,
                main_world_eval=True,
                proxy=proxy,  # 添加代理
                addons=[os.path.abspath(ADDON_PATH)]
        ) as browser:
            # 创建同一个 Context，后续所有的 page.goto 都会携带相同的 Cookie
            context = await browser.new_context()
            page = await context.new_page()

            for i, url in enumerate(url_list):
                try:
                    logger.debug(f"[{i + 1}/{len(url_list)}] 正在访问: {url}")
                    await page.goto(url)
                    await asyncio.sleep(5)  # 初始等待页面加载

                    # 检查是否触发了 CF
                    # 如果页面标题包含 'Just a moment' 或检测到特定的 iframe，则执行 solver
                    page_title = await page.title()
                    if "Just a moment" in page_title or await page.query_selector(
                            'iframe[src*="challenges.cloudflare.com"]'):
                        logger.info(f"检测到 CF 验证，正在尝试点击...")
                        async with ClickSolver(
                                framework=FrameworkType.CAMOUFOX,
                                page=page,
                                max_attempts=5,
                                attempt_delay=3
                        ) as solver:
                            await solver.solve_captcha(
                                captcha_container=page,
                                captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
                            )
                        # 点击后给一点时间让跳转完成
                        await asyncio.sleep(8)

                        # 获取结果
                    results.append(await page.content())

                except Exception as e:
                    # 报错时截图
                    timestamp = datetime.now().strftime("%H%M%S")
                    shot_path = os.path.join(screen_shot, f"err_{timestamp}.png")
                    await page.screenshot(path=shot_path)
                    logger.warning(f"访问 {url} 失败: {e} | 截图已保存")
                    results.append(None)  # 占位符，保证列表长度一致

            # 如果只传入了一个 URL，返回单个字符串；否则返回列表
            return results[0] if isinstance(urls, str) else results