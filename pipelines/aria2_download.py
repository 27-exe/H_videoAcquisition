import logging,os,asyncio
from contextlib import asynccontextmanager
from aioaria2 import Aria2HttpClient

from pipelines.load import load_json

logger = logging.getLogger(__name__)

@asynccontextmanager
async def aria2_session(uri: str, token: str):

    client = Aria2HttpClient(uri, token=token)
    # 模拟进入 aioaria2 的异步上下文
    async with client as aria:
        logger.debug("Aria2 会话已启动")
        try:
            yield aria
        finally:
            # 此处可以放置额外的清理代码
            logger.debug("Aria2 会话已关闭")

async def _single_download(aria, url: str, dst: str, video_name: str, max_retries: int = 3):
    if url == 0:
        return 0

    # 可选：文件已存在直接跳过（防止重复下载）
    if os.path.exists(dst):
        logger.info(f"[{video_name}] 文件已存在，跳过下载 -> {dst}")
        return dst

    options = {
        "dir": os.path.dirname(dst),
        "out": os.path.basename(dst),
        "max-connection-per-server": "16",
        "split": "16",
        "min-split-size": "1M",
        "continue": "true",
        # 让 aria2 自己先内部重试几次
        "max-tries": str(max_retries + 2),
        "retry-wait": "3",
    }

    for attempt in range(1, max_retries + 1):
        gid = None
        try:
            gid = await aria.addUri([url], options)
            logger.info(f"[{video_name}] 第 {attempt}/{max_retries} 次尝试启动，GID: {gid}")
            last_completed = 0
            stuck_count = 0  # 计数器：记录进度不动的次数

            while True:
                status = await aria.tellStatus(gid)
                st = status.get("status")
                completed = int(status.get("completedLength", 0))

                if st == "complete":
                    return dst

                if st in ("error", "removed"):
                    break  # 触发外层 for 循环重试

                # --- 新增：卡住检测逻辑 ---
                if completed > 0 and completed == last_completed:
                    stuck_count += 1
                else:
                    stuck_count = 0  # 进度有变化，重置计数器

                last_completed = completed

                # 如果连续 5 次检查（约 25 秒）进度都没动，且还在 active 状态
                if stuck_count >= 5:
                    logger.warning(f"[{video_name}] 检测到下载卡住，强制重试...")
                    await aria.forceRemove(gid)
                    break  # 跳出 while 循环，触发外层 attempt 重试
                # -----------------------

                await asyncio.sleep(5)

        except Exception as e:
            logger.warning(f"[{video_name}] 第 {attempt} 次请求异常: {e}")
            if gid:
                try:
                    await aria.forceRemove(gid)
                except:
                    pass  # 清理失败也无所谓

        if attempt < max_retries:
            logger.info(f"[{video_name}] 等待 3 秒后进行第 {attempt + 1} 次重试...")
            await asyncio.sleep(3)

    logger.error(f"[{video_name}] 已达到最大重试次数 {max_retries}，最终失败。")
    return False

async def start_batch_download(urls: list[str], download_dir: str, names: list[str]):
    cfg =load_json('aria2.json')
    rpc_url = cfg['rpc_url']
    rpc_token = cfg['rpc_token']

    # 使用装饰好的上下文管理器
    async with aria2_session(rpc_url, rpc_token) as aria:
        tasks = []
        for url, name in zip(urls, names):
            # 拼接下载路径
            full_dst = os.path.join(download_dir, f"{name}.mp4")
            tasks.append(_single_download(aria, url, full_dst, name))

        results = await asyncio.gather(*tasks)
        return results