import asyncio,re,logging
from lxml import html
from utils.request_utils import creat_session,fetch,get_proxy
from utils.parse_utils import clean_filename,make_result
from spiders.base_spider import BaseSpider, CrawlResult

logger = logging.getLogger(__name__)


class Hanime1spider(BaseSpider):
    name: str = "hanime1"
    version: str = "1.0"
    enable_proxy: bool = True      #启用代理
    need_login: bool = False        #需要登录
    default_timeout: int = 15
    default_retries: int = 3
    run_interval_days = 3
    success = True

    def __init__(self, config: dict):
        super().__init__(config)
        self.config = config
        self.name = config.get("name", "未命名爬虫")
        self.headers = config.get("headers", {})
        self.keywords = config.get("keywords", {})
        self.base_url = config.get("base_url", None)
        self.page = config.get("page", 1)
        self.proxy_url = config.get("proxy_url", None)  if self.enable_proxy else None
        self.error = None

    def start_requests(self):
        if not self.base_url:
            raise ValueError("传入的值不能为空")
        self.success = False
        urls = []
        for page in range(1, self.page + 1):
            url = self.base_url + f"search?genre={self.keywords}&sort=%E6%9C%AC%E6%97%A5%E6%8E%92%E8%A1%8C&page={self.page}"
            logger.debug(f"拼接的首页链接为\n{url}")
            urls.append(url)
        return urls

    async def preprocess_response(self, urls:list) -> list | None:       #一次访问预处理拿到下载页面链接和标题
        proxy = await get_proxy(self.proxy_url)
        async with creat_session(header= self.headers,proxy= proxy) as session:
            tasks = [fetch(session,url) for url in urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        processed =  make_result(urls, results)
        detail_msg = []
        try:
            for hl in processed:
                if hl["status"] == "success":
                    _html = hl["content"]
                    tree = html.fromstring(_html)
                    for i in range(30,0,-1):       #仅爬取30个视频,倒序
                        detail = []
                        video_url = tree.xpath(f"//*[@id='home-rows-wrapper']/div[3]/div/div/div[{i}]/div/a/@href")
                        video_name = tree.xpath(f"//*[@id='home-rows-wrapper']/div[3]/div/div/div[{i}]/@title")
                        detail.append(clean_filename(video_name))
                        detail.append(video_url)
                        detail_msg.append(detail)
                        logging.debug(f'完成top{i}的爬取')
            return detail_msg
        except Exception as e:
            self.error = e
            logger.error(f'获取视频详细时出错{e}',exc_info=True)
            self.success = False


    async def parse(self, detail_msg:list):

        post_url = []
        download_urls = []
        for i in detail_msg:
            id_match = re.search(r"\?v=(\d+)", i)
            if id_match:
               url = "https://hanime1.me/download?v=" + id_match.group(1)
               post_url.append(url)
            else:
                logger.warning(f"从页面中提取视频id失败,可能链接发生更改 {i}")
                self.success = False
                post_url.append("")

        for j in range(0,30,5):
            cycle_urls = post_url[j:j+5]
            try:
                proxy = await get_proxy(self.proxy_url)
                async with creat_session(header= self.headers,proxy= proxy) as session:
                    tasks = [fetch(session,url) for url in cycle_urls]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    processed =  make_result(cycle_urls, results)
                for dn in processed:
                    if dn["status"] == "success":
                        _html = dn["content"]
                        tree = html.fromstring(_html)
                        download_url = tree.xpath("//*[@id='content-div']/div[1]/div[4]/div/div/table/tbody/tr[2]/td[5]/a/@data-url")
                        download_urls.append(download_url)
                await asyncio.sleep(6)

            except Exception as e:
                self.success = False
                self.error = str(e)
                logger.warning(f'处理下载链接失败{e}', exc_info=True)
                return CrawlResult(
                    success=self.success,
                    data=detail_msg,
                    detail=download_urls,
                    error=self.error,
                    page_url=self.base_url,
                    pages_count=len(post_url)
                )

        return CrawlResult(
            success= self.success,
            data= detail_msg,           #包含标题,直通链接(id)
            detail= download_urls,      #包含所有视频的原始下载链接
            #extra= ,                   #包含视频频道的对应视频的消息id
            error= self.error,
            page_url = self.base_url,
            pages_count=len(post_url)
        )

    async def do(self):
        url = self.start_requests()
        dig = await self.preprocess_response(url)
        res = await self.parse(dig)
        return res