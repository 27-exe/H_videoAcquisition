"""Port of the original single-VPS `fuck_cf()` from
utils/request_utils.py, plus the iwara list-page preprocessing that used to
live in `spiders/iwara/crawler.py:preprocess_response`.

Kept behavior identical to the original (semaphore, per-URL retries, CF
detection, ClickSolver fallback, exponential backoff). The only change is
that the caller no longer needs to start_requests / login manually — that
scaffolding lives in the HK crawler module.

The state file (storage_state) is read once per call from disk. Persisting
the file back to disk on every call is unnecessary; the caller can refresh
it via the standalone `login()` if it expires.
"""
import asyncio
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Optional

from camoufox import AsyncCamoufox
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType
from playwright_captcha.utils.camoufox_add_init_script.add_init_script import get_addon_path

logger = logging.getLogger(__name__)

ADDON_PATH = get_addon_path()
shot_dir = Path("error_shot")
shot_dir.mkdir(exist_ok=True)

# Mirrors the original MAX_CONCURRENT_BROWSERS — matches the single-VPS cap.
MAX_CONCURRENT_BROWSERS = 2
_BROWSER_SEMAPHORE: Optional[asyncio.Semaphore] = None


def _get_browser_semaphore() -> asyncio.Semaphore:
    global _BROWSER_SEMAPHORE
    if _BROWSER_SEMAPHORE is None:
        _BROWSER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)
    return _BROWSER_SEMAPHORE


def _build_proxy(proxy_str: Optional[str], pro_name: Optional[str], pro_word: Optional[str]):
    if proxy_str is None:
        return None
    proxy = {"server": proxy_str}
    if pro_name:
        proxy["username"] = pro_name
    if pro_word:
        proxy["password"] = pro_word
    return proxy


async def fuck_cf(
    urls: str | list[str],
    proxy_str: Optional[str] = None,
    pro_name=None,
    pro_word=None,
    storage_state=None,
    need_resp: bool = False,
    select: Optional[str] = None,
    max_retries: int = 3,
):
    """Original single-VPS CF bypass + retry loop, ported as-is.

    Returns either a single value (when urls is a str) or a list mirroring
    the input order. Each list slot is either:
      - the page.content() / response.json() / response.text() on success
      - "" on terminal failure
      - 0 if the URL was the literal 0 (kept for compatibility with the
        old spider's `if url == 0: results.append(0); continue` pattern)
    """
    proxy = _build_proxy(proxy_str, pro_name, pro_word)
    sem = _get_browser_semaphore()

    url_list = [urls] if isinstance(urls, str) else urls
    results: list = []

    async with sem:
        async with AsyncCamoufox(
            headless=True,
            geoip=True,
            humanize=True,
            i_know_what_im_doing=True,
            config={"forceScopeAccess": True},
            disable_coop=True,
            main_world_eval=True,
            proxy=proxy,
            addons=[os.path.abspath(ADDON_PATH)],
        ) as browser:
            context = await browser.new_context(storage_state=storage_state)

            for i, url in enumerate(url_list):
                page = None
                final_result = ""

                if url == 0:
                    results.append(0)
                    continue

                for attempt in range(1, max_retries + 1):
                    try:
                        page = await context.new_page()
                        logger.debug(
                            f"[{i + 1}/{len(url_list)}] 第 {attempt}/{max_retries} 次尝试访问: {url}"
                        )

                        response = await page.goto(
                            url, wait_until="domcontentloaded", timeout=60000
                        )

                        try:
                            await page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass

                        target_rendered = False
                        if select is not None:
                            try:
                                await page.wait_for_selector(select, state="visible", timeout=30000)
                                logger.debug("目标元素已成功渲染")
                                target_rendered = True
                            except Exception:
                                logger.warning("未检测到目标元素卡片，准备检查是否被 CF 拦截...")

                        is_cf_page = False
                        if not target_rendered:
                            page_title = await page.title()
                            if response and response.status in (403, 429):
                                is_cf_page = True
                            elif "Attention Required" in page_title or "Just a moment" in page_title:
                                is_cf_page = True

                        if is_cf_page:
                            logger.debug("检测到 CF 验证，准备处理...")
                            max_cf_retries = 3
                            await page.wait_for_timeout(2000)
                            for cf_attempt in range(max_cf_retries):
                                try:
                                    async with ClickSolver(
                                        framework=FrameworkType.CAMOUFOX,
                                        page=page,
                                        max_attempts=3,
                                        attempt_delay=2,
                                    ) as solver:
                                        await solver.solve_captcha(
                                            captcha_container=page,
                                            captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
                                        )
                                    logger.debug("CF 验证完成")
                                    break
                                except Exception as e:
                                    logger.warning(f"CF 尝试 [{cf_attempt + 1}/{max_cf_retries}] 失败: {e}")
                                    if cf_attempt < max_cf_retries - 1:
                                        await asyncio.sleep(3)
                                        await page.reload()
                                        await asyncio.sleep(5)

                            await asyncio.sleep(5)
                            if select is not None:
                                try:
                                    await page.wait_for_selector(
                                        select, state="visible", timeout=30000
                                    )
                                except Exception:
                                    pass

                        if need_resp:
                            try:
                                res_data = await response.json()
                            except Exception:
                                res_data = await response.text()
                            final_result = res_data
                        else:
                            final_result = await page.content()

                        logger.debug(f"[{i + 1}/{len(url_list)}] 第 {attempt} 次成功")
                        break

                    except Exception as e:
                        err_msg = str(e).split("\n")[0]
                        logger.warning(
                            f"[{i + 1}/{len(url_list)}] 第 {attempt}/{max_retries} 次失败: {err_msg}"
                        )

                        if "Timeout" in err_msg and "goto" in err_msg:
                            logger.warning(
                                f"⚠️ 检测到 Page.goto 超时（常见于 apiq.iwara.tv），准备重试..."
                            )

                        if page:
                            try:
                                await page.close()
                            except Exception:
                                pass
                            page = None

                        if attempt == max_retries:
                            logger.error(f"[{i + 1}/{len(url_list)}] 已达最大重试次数，仍失败")
                            final_result = ""
                        else:
                            sleep_time = 3 * (2 ** (attempt - 1)) + random.uniform(1, 4)
                            logger.info(f"等待 {sleep_time:.1f} 秒后重试...")
                            await asyncio.sleep(sleep_time)

                results.append(final_result)

                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass

    return results[0] if isinstance(urls, str) else results


async def preprocess_iwara_list(
    list_url: str,
    proxy_str: Optional[str] = None,
    pro_name=None,
    pro_word=None,
    storage_state=None,
    max_retries: int = 5,
    min_items: int = 20,
    select: str = ".page-videoList .col-12.col-lg-9.order-2.order-lg-1 > div > div > div > a>img",
):
    """Port of `IwaraSpider.preprocess_response()`.

    Returns a list of (title, source_url) pairs in source order, or [] when
    the max retry budget is exhausted.
    """
    from lxml import html as lxml_html

    v_name: list[str] = []
    v_url: list[str] = []
    base_sleep = 5

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"[预处理] 第 {attempt}/{max_retries} 次尝试访问首页...")

            results = await fuck_cf(
                list_url,
                proxy_str=proxy_str,
                pro_name=pro_name,
                pro_word=pro_word,
                storage_state=storage_state,
                select=select,
            )
            # Inline `make_result()` + `clean_filename()` from the original
            # utils/parse_utils.py — they are tiny pure functions and we
            # don't want HK to depend on a US-only utils path.
            processed = []
            for url, result in zip([list_url], results):
                if url == 0 or result == 0:
                    processed.append(0)
                elif isinstance(result, Exception):
                    processed.append({
                        "status": "error",
                        "content": str(result) + type(result).__name__,
                    })
                else:
                    processed.append({
                        "status": "success",
                        "content": result,
                    })

            if not processed:
                raise Exception("fuck_cf 返回为空")

            v_name.clear()
            v_url.clear()

            success_count = 0
            for data in processed:
                if data == 0:
                    continue
                if isinstance(data, dict) and data.get("status") == "success":
                    page_html = data.get("content", "")
                    if not page_html:
                        continue

                    tree = lxml_html.fromstring(page_html)
                    video_urls = tree.xpath('//a[@class="videoTeaser__thumbnail"]/@href')[0:30][::-1]
                    video_names = tree.xpath('//a[@class="videoTeaser__title"]/@title')[0:30][::-1]

                    for i in range(min(len(video_urls), len(video_names))):
                        raw_name = video_names[i] or "未命名文件"
                        # Inline clean_filename:
                        cleaned = re.sub(r'[\x00-\x1F\x7F<>:"/\\|?*]', '_', raw_name).strip()
                        cleaned = cleaned.rstrip('.')
                        if not cleaned or cleaned.isspace():
                            cleaned = "未命名文件"
                        v_name.append(cleaned[:200])
                        v_url.append("https://www.iwara.tv" + video_urls[i])
                    success_count += 1
                elif isinstance(data, dict) and data.get("status") == "error":
                    logger.warning(f"页面返回错误: {data.get('content')}")

            if len(v_url) >= min_items:
                logger.info(f"✅ 第 {attempt} 次尝试成功！共提取 {len(v_url)} 个视频")
                return list(zip(v_name, v_url))

            logger.warning(
                f"⚠️ 第 {attempt} 次尝试只提取到 {len(v_url)} 个视频，准备重试..."
            )

        except Exception as e:
            logger.warning(f"❌ 第 {attempt} 次预处理失败: {e}")

        if attempt == max_retries:
            break

        sleep_time = base_sleep * (2 ** (attempt - 1)) + random.uniform(1, 3)
        logger.info(f"等待 {sleep_time:.1f} 秒后进行第 {attempt + 1} 次重试...")
        await asyncio.sleep(sleep_time)

    logger.error(f"❌ 已重试 {max_retries} 次，仍未能成功提取足够视频！")
    return []


async def login(
    url: str,
    username: str,
    password: str,
    username_selector: str,
    password_selector: str,
    proxy_str: Optional[str] = None,
    pro_name=None,
    pro_word=None,
    save_state_path: str = "auth_state.json",
):
    """Port of the original `login()` helper. Saves cookies + LocalStorage."""
    proxy = _build_proxy(proxy_str, pro_name, pro_word)
    sem = _get_browser_semaphore()

    async with sem:
        async with AsyncCamoufox(
            headless=True,
            geoip=True,
            humanize=True,
            i_know_what_im_doing=True,
            config={"forceScopeAccess": True},
            disable_coop=True,
            main_world_eval=True,
            proxy=proxy,
            addons=[os.path.abspath(ADDON_PATH)],
        ) as browser:
            context = await browser.new_context()
            page = None
            try:
                page = await context.new_page()
                logger.debug(f"准备登录，正在访问: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(3)

                logger.debug(f"等待账号输入框出现: {username_selector}")
                await page.wait_for_selector(username_selector, state="visible", timeout=15000)
                await page.fill(username_selector, username)
                await asyncio.sleep(random.uniform(0.5, 1.5))

                logger.debug(f"等待密码输入框出现: {password_selector}")
                await page.wait_for_selector(password_selector, state="visible", timeout=10000)
                await page.fill(password_selector, password)
                await asyncio.sleep(random.uniform(0.5, 1.5))

                logger.debug("点击登录按钮")
                login_button = page.get_by_role("button", name="Submit")
                await login_button.wait_for(state="visible", timeout=10000)
                await login_button.click()

                logger.debug("等待登录状态响应...")
                try:
                    await page.wait_for_url(lambda url: "login" not in url.lower(), timeout=15000)
                except Exception:
                    logger.warning("登录后页面未跳转，尝试继续保存状态...")

                await context.storage_state(path=save_state_path)
                logger.info(f"登录状态已成功提取并保存至: {save_state_path}")
                return save_state_path

            except Exception as e:
                err_msg = str(e).split("\n")[0]
                logger.warning(f"登录过程出现异常: {err_msg}")
                timestamp = int(time.time())
                screenshot_path = f"error_shot/login_fail_{timestamp}.png"
                try:
                    await page.screenshot(path=screenshot_path)
                    logger.warning(f"已保存登录失败截图以供调试: {screenshot_path}")
                except Exception:
                    pass
                return None

            finally:
                logger.debug("登录任务结束")
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass