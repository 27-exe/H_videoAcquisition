import asyncio,re,logging
from lxml import html
from utils.request_utils import creat_session,fetch
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
        async with creat_session(header=self.headers) as session:
            tasks = [fetch(session, url,proxy= self.proxy_url) for url in urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        processed = make_result(urls, results)
        detail_msg = []

        try:
            if not processed:
                logger.warning("未能获取到任何页面结果")
                return None

            for data in processed.values():
                if isinstance(data, dict) and data.get("status") == "success":
                    _html = data["content"]
                    if not _html: continue

                    tree = html.fromstring(_html)
                    for i in range(30, 0, -1):
                        video_urls = tree.xpath(f"//*[@id='home-rows-wrapper']/div[3]/div/div/div[{i}]/div/a/@href")
                        video_names = tree.xpath(f"//*[@id='home-rows-wrapper']/div[3]/div/div/div[{i}]/@title")

                        if video_urls and video_names:
                            # 提取字符串
                            v_url = video_urls[0]
                            v_name = clean_filename(video_names[0])


                            detail_msg.append([v_name, v_url])
                            logger.debug(f'完成top{i}的爬取: {v_name}')
                elif data.get("status") == "error":
                    logger.warning(f'访问页面失败!{data["content"]}')
                    raise Exception(f"Request failed: {data['content']}")
                else:
                    logger.warning(f"跳过无效数据 (类型: {type(data)}): {data}")

            return detail_msg

        except Exception as e:
            self.error = e
            logger.error(f'获取视频详细时出错: {e}', exc_info=True)
            self.success = False
            return []  # 出错返回空列表而不是 None，防止后续 crash

    async def parse(self, detail_msg: list):
        post_url = []
        download_urls = []

        for item in detail_msg:
            if not item or len(item) < 2:
                continue

            # 取出 url 字符串 (item[1])
            video_url_str = item[1]
            id_match = re.search(r"\?v=(\d+)", video_url_str)

            if id_match:
                url = "https://hanime1.me/download?v=" + id_match.group(1)
                post_url.append(url)
            else:
                logger.warning(f"提取视频ID失败: {video_url_str}")
                self.success = False
                post_url.append("")


        for j in range(0,30,5):
            cycle_urls = post_url[j:j+5]
            try:
                async with creat_session(header= self.headers) as session:
                    tasks = [fetch(session,url,proxy= self.proxy_url) for url in cycle_urls]
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