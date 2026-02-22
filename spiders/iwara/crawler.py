import asyncio,re,logging,os,aiohttp
from lxml import html
import hashlib
import copy,json
from pathlib import Path
from utils.request_utils import fuck_cf,login
from utils.parse_utils import clean_filename,make_result
from spiders.base_spider import BaseSpider, CrawlResult
from pipelines.data_base import DataBase

logger = logging.getLogger(__name__)


async def if_exit(url_list,db:DataBase):
    download_lists = []
    find_task = [db.get_iwara_info(vid_url) for vid_url in url_list]
    find = await asyncio.gather(*find_task)
    for i,vid_id in enumerate(find):
        if vid_id == 0: #不存在,原样
            download_lists.append(url_list[i])
        else:       #存在,置0
            download_lists.append(0)
    return download_lists

class IwaraSpider(BaseSpider):
    name: str = "iwara"
    version: str = "1.0"
    enable_proxy: bool = True      #启用代理
    need_login: bool = False        #需要登录
    default_timeout: int = 15
    default_retries: int = 3
    run_interval_days = 3
    success = True
    auth_dir = Path("config/auth")
    auth_dir.mkdir(parents=True, exist_ok=True)  # 确保文件夹存在
    state_path = str(auth_dir / "iwara_auth.json")

    def __init__(self, config: dict,db:DataBase):
        super().__init__(config)
        self.config = config
        self.name = config.get("name", "未命名爬虫")
        self.headers = config.get("headers", {})
        self.keywords = config.get("keywords", {})
        self.base_url = config.get("base_url", None)
        self.page = config.get("page", 1)
        self.username  = config.get("username", None)
        self.password  = config.get("password", None)
        self.proxy_url = config.get("proxy_url", None)  if self.enable_proxy else None
        self.pro_name = config.get("proxy_name", None) if self.enable_proxy else None
        self.pro_word = config.get("proxy_pass", None) if self.enable_proxy else None
        self.error = None
        self.db = db

    async def start_requests(self):

        if not os.path.exists(self.state_path):
            logger.info("未检测到登录状态，开始执行自动登录...")
            await login("https://www.iwara.tv/login",
                        self.username,
                        self.password,
                        proxy_str=self.proxy_url,
                        pro_name=self.pro_name,
                        pro_word=self.pro_word,
                        username_selector='input[name="email"]',
                        password_selector='input[name="password"]',
                        save_state_path=self.state_path)
        if not self.base_url:
            self.success = False
            raise ValueError("传入的值不能为空")
        urls = []
        for page in range(0, self.page + 1):
            url = self.base_url + f"videos?sort={self.keywords}&page={self.page}"
            logger.debug(f"拼接的首页链接为\n{url}")
            urls.append(url)
        return urls

    async def preprocess_response(self, urls:list) :       #一次访问预处理拿到下载页面链接和标题
        # 有 CF
        v_name = []
        v_url = []
        results = await fuck_cf(urls,  proxy_str=self.proxy_url,pro_name=self.pro_name,pro_word=self.pro_word,storage_state=self.state_path,select='.page-videoList .col-12.col-lg-9.order-2.order-lg-1 > div > div > div')

        processed = make_result(urls, results)

        try:
            if not processed:
                logger.warning("未能获取到任何页面结果")
                return None

            for data in processed:
                if data == 0: continue  # 跳过占位符
                if isinstance(data, dict) and data.get("status") == "success":
                    _html = data["content"]
                    if not _html: continue
                    tree = html.fromstring(_html)
                    video_urls = tree.xpath(f'//a[@class="videoTeaser__thumbnail"]/@href')[0:30][::-1]
                    video_names = tree.xpath(f'//a[@class="videoTeaser__title"]/@title')[0:30][::-1]

                    for i in range(0,30):
                        v_name.append(clean_filename(video_names[i]))
                        v_url.append('https://www.iwara.tv'+ video_urls[i])

                    logger.debug(f'完成首页爬取{v_name}')
                elif data.get("status") == "error":
                    logger.warning(f'访问页面失败!{data["content"]}')
                    raise Exception(f"Request failed: {data['content']}")
                else:
                    logger.warning(f"跳过无效数据 (类型: {type(data)}): {data}")

            return [v_name, v_url]

        except Exception as e:
            self.error = e
            logger.error(f'获取视频详细时出错: {e}', exc_info=True)
            self.success = False
            return []  # 出错返回空列表而不是 None，防止后续 crash

    async def parse(self,dig):
        v_id = []
        api_url = []
        json_files = []
        source_json = []
        download_url = []
        try:
            for i in range(0,30):
                v_id.append(self.get_video_id(dig[1][i]))
            need_url = await if_exit(v_id, self.db)
            for j, data in enumerate(need_url):
                if data != 0:
                    api_url.append('https://apiq.iwara.tv/video/'+ v_id[j] )
                else:
                    api_url.append(0)

            for j in range(0,30,5):
                api_res = await fuck_cf(api_url[j:j+5],self.proxy_url,storage_state= self.state_path,need_resp=True)
                for data in api_res:
                    if data != 0:
                        json_files.append(data)
                    else:
                        json_files.append({})
            async with aiohttp.ClientSession() as session:
                for y in range(0,30,5):
                    down_task = [self.deobfuscation(file,session=session)for file in json_files[y:y+5]]
                    source_json.extend(await asyncio.gather(*down_task))
                    await asyncio.sleep(10)
            for x in range(0,30):
                if self.get_source_url(source_json[x]) ==0:
                    download_url.append(0)
                else:
                    download_url.append("https:" + self.get_source_url(source_json[x]))

        except Exception as e:
            self.success = False
            self.error = str(e)
            logger.warning(f'处理下载链接失败{e}', exc_info=True)
            return CrawlResult(
                success=self.success,
                data=dig,
                detail=download_url,
                error=self.error,
                page_url=self.base_url,
                pages_count=len(api_url)
            )

        return CrawlResult(
            success=self.success,
            data=dig,  # 包含标题,直通链接
            detail=download_url,  # 包含所有视频的原始下载链接
            extra= v_id,                   #包含视频频道的对应视频的消息id
            error=self.error,
            page_url=self.base_url,
            pages_count=len(api_url)
        )



    @staticmethod
    def get_video_id(video_url):
        pattern = r"https?://(?:www\.)?iwara\.tv/video/([^/]+)"
        match = re.match(pattern, video_url)
        if match:
            return match.group(1)
        return None

    async def deobfuscation(self,file: dict, session):
        if not file:
            return 0
        file_url = file["fileUrl"]
        file_id = file['file']['id']
        expires = file_url.split('/')[4].split('?')[1].split('&')[0].split('=')[1]
        sha_postfix = "_mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"

        # WARN: IMPORTANT: This might change in the future.
        #       in https://www.iwara.tv/main.2d42d059f9603ed484c4.js ,can find by searching "new URL"


        sha_key = file_id + "_" + expires + sha_postfix
        t_hash = hashlib.sha1(sha_key.encode('utf-8')).hexdigest()
        headers = copy.copy(self.headers)
        headers.update({"X-Version": t_hash})
        async with asyncio.Semaphore(2):
            async with session.get(file_url, headers=headers) as resp:
                resp.raise_for_status()
                text = await resp.text()
                resources = json.loads(text)
            return resources

    @staticmethod
    def get_source_url(url_json):
        if url_json == 0:
            return 0
        download_url = None
        fallback_url = None
        for item in url_json:
            if item["name"] == "Source":
                download_url = item["src"]["download"]
            elif item["name"] == "360":
                fallback_url = item["src"]["download"]
        if download_url is None and fallback_url is None:
            logger.warning("未找到下载链接")
            return None
        elif download_url is None:
            logger.warning("未找到原画质,使用360p")
            return fallback_url
        return download_url


    async def do_job(self):

        url = await self.start_requests()

        dig = await self.preprocess_response(url)

        res = await self.parse(dig)
        return res


