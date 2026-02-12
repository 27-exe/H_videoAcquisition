import aiohttp,logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

@asynccontextmanager
async def creat_session(header = None,proxy = None):
    async with aiohttp.ClientSession(headers= header,proxy= proxy) as session:
        yield session

async def fetch(session,url:str):
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.text()


async def get_proxy(proxy_url:str):
    """从 API 获取新的代理 IP:Port"""
    try:
        async with creat_session() as session:
            async with session.get(proxy_url) as resp:
                resp.raise_for_status()
                proxy_str = resp.text.strip()
                return proxy_str
    except Exception as e:
        logger.warning(f" 请求代理 API 出错: {e}")
        return None

