import aiohttp
import logging
import os
import asyncio,time
from typing import Optional, Dict
from pathlib import Path
from contextlib import asynccontextmanager
from camoufox import AsyncCamoufox
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType
from playwright_captcha.utils.camoufox_add_init_script.add_init_script import get_addon_path

ADDON_PATH = get_addon_path()
shot_dir = Path("error_shot")
shot_dir.mkdir(exist_ok=True)

# ---------------
# 这里设置最大并发数，建议设置为 2 到 4，取决于你的机器性能
# 每一个实例都是一个浏览器，设置太高会让机器卡死
MAX_CONCURRENT_BROWSERS = 3
_BROWSER_SEMAPHORE = None  # 初始化为 None

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

            for i, url in enumerate(url_list):
                page = None
                try:
                    if url == 0:
                        results.append(0)
                        continue
                    page = await context.new_page()
                    logger.debug(f"[{i + 1}/{len(url_list)}] 正在访问: {url}")
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(3)  # 初始等待页面加载

                    # 检查是否触发了 CF
                    page_title = await page.title()
                    is_cf_page = "Just a moment" in page_title or "请稍等"in page_title

                    if is_cf_page:
                        logger.info(f"检测到 CF 验证，准备开始处理...")

                        # --- 重试逻辑开始 ---
                        max_cf_retries = 3
                        await asyncio.sleep(8)
                        for attempt in range(max_cf_retries):
                            try:
                                async with ClickSolver(
                                        framework=FrameworkType.CAMOUFOX,
                                        page=page,
                                        max_attempts=3,  # 库内部的单次尝试次数
                                        attempt_delay=2
                                ) as solver:
                                    await solver.solve_captcha(
                                        captcha_container=page,
                                        captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
                                    )
                                # 如果执行到这里没有报错，说明可能成功了，或者至少跑完了流程
                                logger.info("CF 验证流程执行完毕")
                                break

                            except Exception as e:
                                # 捕获所有异常，不打印堆栈，只打印 Warning
                                log_msg = str(e).split('\n')[0]  # 只取错误信息的第一行，保持日志整洁
                                logger.warning(f"CF 尝试 [{attempt + 1}/{max_cf_retries}] 失败: {log_msg}")

                                # 截图保存 (修复: 添加 path 参数)
                                timestamp = int(time.time())
                                screenshot_path = f"error_shot/cf_fail_{i}_{attempt}_{timestamp}.png"
                                try:
                                    await page.screenshot(path=screenshot_path)
                                    logger.warning(f"已保存调试截图: {screenshot_path}")
                                except Exception:
                                    pass  # 截图失败就不管了

                                if attempt < max_cf_retries - 1:
                                    # 如果不是最后一次尝试，稍微等待并刷新页面重试
                                    await asyncio.sleep(3)
                                    try:
                                        await page.reload()
                                        await asyncio.sleep(5)  # 等待重载后 CF 出现
                                    except:
                                        pass
                        # --- 重试逻辑结束 ---

                        # 无论成功失败，给一点跳转时间
                        await asyncio.sleep(5)

                    results.append(await page.content())

                except Exception as e:
                    # 外层的大异常捕获
                    err_msg = str(e).split('\n')[0]
                    logger.warning(f"访问 {url} 出现未知错误: {err_msg}")

                    # 错误截图
                    timestamp = int(time.time())
                    screenshot_path = f"screenshots/error_global_{i}_{timestamp}.png"
                    try:
                        await page.screenshot(path=screenshot_path)
                    except:
                        pass

                    results.append("")  # 发生错误时追加空字符串或保留现有内容
                finally:
                    logger.debug(f"[{i + 1}/{len(url_list)}] 访问结束")
                    # 释放资源，防止内存泄漏
                    if page:
                        try:
                            await page.close()
                        except:
                            pass

            return results[0] if isinstance(urls, str) else results