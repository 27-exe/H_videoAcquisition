import logging
import os
import asyncio,time,random
from typing import Optional
from pathlib import Path
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


async def fuck_cf(urls: str | list[str], proxy_str: Optional[str] = None,pro_name = None,pro_word = None,storage_state = None,need_resp = False,select = None):
    """
    支持传入单个 URL 或 URL 列表。
    如果是列表，将复用同一个 Context (共享 Cookie)，仅在必要时点击 CF。
    """

    if proxy_str is not None:

        proxy = {
            "server": proxy_str,
            "username": pro_name,
            "password": pro_word
        }
    else:
        proxy = None
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
            context = await browser.new_context(storage_state = storage_state)

            for i, url in enumerate(url_list):
                page = None
                try:
                    if url == 0:
                        results.append(0)
                        continue
                    page = await context.new_page()
                    logger.debug(f"[{i + 1}/{len(url_list)}] 正在访问: {url}")
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)

                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except:
                        pass

                    target_rendered = False
                    if select is not None:
                        try:
                            await page.wait_for_selector(select, state="visible", timeout=30000)
                            logger.debug("目标元素已成功渲染")
                            target_rendered = True
                           # timestamp = int(time.time())
                           # screenshot_path = f"error_shot/success_{i}_{timestamp}.png"  # 改个名字区分成功
                           # await page.screenshot(path=screenshot_path)
                        except Exception:
                            logger.warning("未检测到目标元素卡片，准备检查是否被 CF 拦截...")

                    # 检查是否触发了 CF

                    is_cf_page = False
                    if not target_rendered:
                        page_title = await page.title()
                        if response and response.status in [403, 429]:  # 顺便加上 429 防限流判定
                            is_cf_page = True
                        elif "Attention Required" in page_title or "Just a moment" in page_title:
                            is_cf_page = True

                    if is_cf_page:
                        logger.debug(f"检测到 CF 验证，准备开始处理...")
                        # --- 重试逻辑开始 ---
                        max_cf_retries = 3
                        await page.wait_for_timeout(2000)
                        for attempt in range(max_cf_retries):
                            try:
                                async with ClickSolver(
                                        framework=FrameworkType.CAMOUFOX,
                                        page=page,
                                        max_attempts=3,
                                        attempt_delay=2
                                ) as solver:
                                    await solver.solve_captcha(
                                        captcha_container=page,
                                        captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
                                    )
                                logger.debug("CF 验证流程执行完毕")
                                break

                            except Exception as e:
                                log_msg = str(e).split('\n')[0]
                                logger.warning(f"CF 尝试 [{attempt + 1}/{max_cf_retries}] 失败: {log_msg}")

                                timestamp = int(time.time())
                                screenshot_path = f"error_shot/cf_fail_{i}_{attempt}_{timestamp}.png"
                                try:
                                    await page.screenshot(path=screenshot_path)
                                except Exception:
                                    pass

                                if attempt < max_cf_retries - 1:
                                    await asyncio.sleep(3)
                                    try:
                                        await page.reload()
                                        await asyncio.sleep(5)
                                    except:
                                        pass
                        # --- 重试逻辑结束 ---
                        await asyncio.sleep(5)

                        # 💡 核心修改二：在 CF 可能引发的重载之后，再次等待目标元素渲染
                        if select is not None:
                            try:
                                await page.wait_for_selector(select, state="visible", timeout=30000)
                                logger.debug("CF 处理后，目标元素已成功渲染")
                            except Exception:
                                logger.warning("CF 处理后，依然未检测到目标元素")

                    if need_resp:
                        try:
                            res_data = await response.json()
                        except:
                            res_data = await response.text()
                        results.append(res_data)
                    else:
                        # 此时再获取 content，确保是在最终渲染状态下提取
                        results.append(await page.content())
                except Exception as e:
                    # 外层的大异常捕获
                    err_msg = str(e).split('\n')[0]
                    logger.warning(f"访问 {url} 出现未知错误: {err_msg}")

                    # 错误截图
                    timestamp = int(time.time())
                    screenshot_path = f"error_shot/error_global_{i}_{timestamp}.png"
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



async def login(
        url: str,
        username: str,
        password: str,
        username_selector: str,
        password_selector: str,
        proxy_str: Optional[str] = None,
        pro_name = None,
        pro_word = None,
        save_state_path: str = "auth_state.json"
) -> Optional[str]:
    """
    通用自动登录函数，并将登录后的状态 (Cookies 和 Local Storage) 保存到本地。
    """

    if proxy_str is not None:

        proxy = {
            "server": proxy_str,
            "username": pro_name,
            "password": pro_word
        }
    else:
        proxy = None

    global _BROWSER_SEMAPHORE
    if _BROWSER_SEMAPHORE is None:
        _BROWSER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)

    async with _BROWSER_SEMAPHORE:
        # 测试期间建议 headless=False
        async with AsyncCamoufox(
                headless=True,
                geoip=True,
                humanize=True,
                i_know_what_im_doing=True,
                config={'forceScopeAccess': True},
                disable_coop=True,
                main_world_eval=True,
                proxy=proxy,
                addons=[os.path.abspath(ADDON_PATH)]
        ) as browser:
            context = await browser.new_context()
            page = None
            try:
                page = await context.new_page()
                logger.debug(f"准备登录，正在访问: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(3)  # 等待页面初始加载

                # 1. 填写账号
                logger.debug(f"等待账号输入框出现: {username_selector}")
                await page.wait_for_selector(username_selector, state="visible", timeout=15000)
                await page.fill(username_selector, username)
                await asyncio.sleep(random.uniform(0.5, 1.5))  # 模拟人类输入停顿

                # 2. 填写密码
                logger.debug(f"等待密码输入框出现: {password_selector}")
                await page.wait_for_selector(password_selector, state="visible", timeout=10000)
                await page.fill(password_selector, password)
                await asyncio.sleep(random.uniform(0.5, 1.5))

                # 3. 点击登录按钮
                logger.debug(f"点击登录按钮")
                login_button = page.get_by_role("button", name="Submit")

                # 等待并点击
                await login_button.wait_for(state="visible", timeout=10000)
                await login_button.click()

                # 4. 等待登录成功跳转或接口响应
                logger.debug("等待登录状态响应...")
                try:
                    # 等待 URL 不再包含 "login" 字样，或者等待某个只有登录后才有的元素
                    await page.wait_for_url(lambda url: "login" not in url.lower(), timeout=15000)
                except:
                    logger.warning("登录后页面未跳转，尝试继续保存状态...")

                # 5. 提取并保存全局状态 (包含 Cookie 和 LocalStorage)
                await context.storage_state(path=save_state_path)
                logger.info(f"登录状态已成功提取并保存至: {save_state_path}")

                return save_state_path

            except Exception as e:
                err_msg = str(e).split('\n')[0]
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
                    except:
                        pass