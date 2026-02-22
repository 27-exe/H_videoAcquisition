from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta


@dataclass
class CrawlResult:
    """标准化爬取结果结构"""
    success: bool = False
    data: List[list] = None          # 主数据列表(目标链接,图片,视频,文字)
    detail: List = None                        # 详情信息(爬取设置细节)
    extra: List = None               # 其他附加信息(爬取时产生细节)
    error: Optional[str] = None                # 错误描述
    crawled_at: str = None                     # 爬取日期
    page_url: Optional[str] = None             # 本次请求的URL
    pages_count: Optional[int] = None          # 总共爬取了多少

    def __post_init__(self):
        if self.crawled_at is None:
            tz = timezone(timedelta(hours=8))
            self.crawled_at = datetime.now(tz).date().isoformat()



class BaseSpider(ABC):

    name: str = "未命名爬虫"
    version: str = "1.0"
    enable_proxy: bool = False      #启用代理
    need_login: bool = False        #需要登录
    default_timeout: int = 15
    default_retries: int = 3

    def __init__(self, config: dict):
        """
        config 来自 config/sites.yaml
        建议至少包含以下字段：
        {
            "name": "xxx",
            "base_url": "...",
            "cookies": {...},
            "headers": {...},
            ...
        }
        """
        self.config = config
        self.name = config.get("name", self.name)

    # ─── 必须实现的抽象方法 ───

    @abstractmethod
    def start_requests(self) -> List[Union[str, Dict]]:
        """
        返回要爬取的初始请求
        可以返回：
        1. URL 字符串列表
        2. 请求字典列表（支持 method、headers、data、cookies 等）
        """
        pass

    @abstractmethod
    def preprocess_response(self, response: Any) -> Any:
        """在 parse 之前可以做统一预处理（如解密、去广告、转码等）"""
        return response

    @abstractmethod
    def parse(self, response) -> CrawlResult:
        """
        核心解析方法
        参数 response 由 run() 统一传入，可能是：
        - str (html文本)
        - dict (json)
        - Response 对象（requests/selenium/playwright等）

        必须返回 CrawlResult 实例
        """
        pass


    # ─── 可选但强烈推荐实现的方法 ───

    def handle_error(self, exception: Exception, request_info: dict) -> CrawlResult:
        """统一错误处理"""
        return CrawlResult(
            success=False,
            error=str(exception),
            page_url=request_info.get("url"),
            extra={"request": request_info}
        )

    # ─── 框架/调度层可能会调用的钩子（可选） ───

    def before_run(self):
        """爬虫启动前钩子（可用于登录、初始化session等）"""
        pass

    def after_run(self):
        """爬虫结束后钩子（清理、统计等）"""
        pass