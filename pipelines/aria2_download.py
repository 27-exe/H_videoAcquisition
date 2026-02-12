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

async def _single_download(aria, url: str, dst: str, video_name: str):
    if url == 0 :
        return 0
    options = {
        "dir": os.path.dirname(dst),
        "out": os.path.basename(dst),
        "max-connection-per-server": "16",
        "split": "16",
        "min-split-size": "1M",
        "continue": "true",
    }

    gid = await aria.addUri([url], options)

    while True:
        status = await aria.tellStatus(gid)
        st = status["status"]

        if st == "complete":
            logger.info(f"[{video_name}] 完成 -> {dst}")
            return dst
        if st in ("error", "removed"):
            logger.error(f"[{video_name}] 失败: {status.get('errorMessage')}")
            return False

        await asyncio.sleep(5)

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