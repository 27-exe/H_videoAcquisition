import logging
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.job import Job
from datetime import datetime, timedelta
from spiders.hanime1.tasks import do_hanime1
from spiders.iwara.tasks import do_iwara

logger = logging.getLogger(__name__)

def add_iwara(scheduler: AsyncIOScheduler,client,db) -> Job:
    """加入每天 12:00 的 iwara 爬虫"""
    return scheduler.add_job(
        do_iwara,
        args=(client, db),
        trigger=CronTrigger(hour=12, minute=0, timezone="Asia/Shanghai"),
        id="daily_iwara_12pm",
        name="每日 iwara 任務 - 12:00",
        replace_existing=True,
        misfire_grace_time=1800,   # 允許延遲最多 30 分鐘
    )


def add_hanime1(scheduler: AsyncIOScheduler,client,db) -> Job:
    """加入每隔一天 16:00 的 hanime1 爬虫"""

    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)

    # 計算下一個 16:00
    next_16 = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if next_16 <= now:
        next_16 += timedelta(days=1)

    return scheduler.add_job(
        do_hanime1,
        args=(client, db),
        trigger=IntervalTrigger(
            days=2,
            start_date=next_16,
            timezone=tz,
        ),
        id="hanime1_16pm",
        name="hanime1隔日任務 - 16:00",
        replace_existing=True,
        misfire_grace_time=3600,
    )


class TaskManager:
    """
    只负责调度控制：启动 / 暂停 / 立即执行一次
    """
    def __init__(self,client,db):
        self.scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Shanghai"))
        self.scheduler_started = False
        self.iwara: Job | None = None
        self.hanime1: Job | None = None
        self.client = client
        self.db = db


    def start_all(self):
        """启动定时器,加入所有任务"""
        if self.scheduler_started:
            logger.info("排程器已在運行")
            return

        self.iwara = add_iwara(self.scheduler,self.client,self.db)
        self.hanime1 = add_hanime1(self.scheduler,self.client,self.db)

        self.scheduler.start()
        self.scheduler_started = True

        logger.info("定时器")
        logger.info("  • iwara : 每天 12:00")
        logger.info("  • hanime1 : 每隔一天 16:00")

    def pause_hanime1(self):
        if self.hanime1:
            self.hanime1.pause()

        logger.info("hanime1任务已暂停")

    def pause_iwara(self):
        if self.iwara:
            self.iwara.pause()

        logger.info("iwara任务已暂停")


    def resume_hanime1(self):
        if self.hanime1:
            self.hanime1.resume()

        logger.info("hanime1任务已恢复")



    def resume_iwara(self):
        if self.iwara:
            self.iwara.resume()

        logger.info("iwara任务已恢复")



    def shutdown(self):
        if self.scheduler_started:
            self.scheduler.shutdown(wait=False)
            self.scheduler_started = False
            logger.info("调度器已关闭")