import os,logging,re,asyncio
from pipelines.telegram_send import send_source_video,send_video,send_top5,delete_messages
from spiders.hanime1.crawler import Hanime1spider
from pipelines.aria2_download import start_batch_download
from pipelines.load import load_yaml
from spiders.base_spider import CrawlResult
from utils.pic_utils import generate_thumbnail,write_text_on_image
from datetime import datetime, timezone, timedelta
from pipelines.data_base import DataBase
from utils.hk_crawler_client import fetch_via_hk_crawler, HKCrawlerError

logger = logging.getLogger(__name__)

base_path = os.getcwd()
spider_path = os.path.join(base_path, "download", "hanime1")
video_path = os.path.join(spider_path, "video")
cover_path = os.path.join(spider_path, "cover")
preview_path = os.path.join(spider_path, "preview")


def _dual_mode_enabled() -> bool:
    """True when both CRAWLER_URL and CRAWLER_BEARER_TOKEN env vars are set.

    See spiders/iwara/tasks.py for the same gate. Without both env vars, bot
    runs the original local AsyncCamoufox path only — preserving the
    historical single-VPS behavior.
    """
    return bool(os.environ.get("CRAWLER_URL")) and bool(os.environ.get("CRAWLER_BEARER_TOKEN"))


def _fallback_local_allowed() -> bool:
    """Whether to run local AsyncCamoufox if HK crawler is unreachable.

    Default True (US bot has local browser installed for legacy path).
    Set FALLBACK_LOCAL_BROWSER=0 to disable fallback and skip the task on
    HK failure (next cron tick retries).
    """
    val = os.environ.get("FALLBACK_LOCAL_BROWSER", "1")
    return val not in ("0", "false", "False", "no")


async def _fetch_hanime1_via_hk(cfg, skip_ids: list[int] | None = None) -> "CrawlResult | None":
    """Phase 2 dual-VPS entry point: ask HK crawler for hanime1 items.

    skip_ids: list of int video_ids already in db — HK will skip these.

    Returns CrawlResult whose .data is shaped like Hanime1spider.do_job(): a
    list of (title, source_url) tuples (NOT a [name_list, source_url_list]
    like iwara). .detail = download_urls, .extra = id list (parsed by
    do_hanime1 via re.search on the source_url).
    """
    try:
        items = fetch_via_hk_crawler(
            "hanime1",
            {
                "page": int(cfg.get("page", 1)),
                "limit": int(cfg.get("limit", 30)),
                "sort": cfg.get("sort", "today-popular"),
                **({"skip_ids": skip_ids} if skip_ids is not None else {}),
            },
        )
    except HKCrawlerError as e:
        logger.warning(
            f"hk crawler hanime1 unavailable: {e}; "
            f"fallback_local_browser={_fallback_local_allowed()}"
        )
        return None

    data_list = [
        (it.get("title", ""), it.get("source_url", ""))
        for it in items
    ]
    download_urls = [it.get("download_url", 0) for it in items]
    return CrawlResult(
        success=True,
        data=data_list,
        detail=download_urls,
        extra=None,
        crawled_at=datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
        page_url="<hk-crawler>",
    )


async def do_hanime1(client, db: DataBase, max_retries: int = 3, retry_wait_minutes: int = 10):
    """
    爬取和发送 Hanime1 视频，支持重试机制

    Args:
        client: Telegram 客户端
        db: 数据库实例
        max_retries: 最大重试次数
        retry_wait_minutes: 重试等待分钟数
    """
    cfg = load_yaml('hanime1.yaml')
    video_ch = cfg['video_channel']
    pic_ch = cfg['pic_channel']
    vid_name = re.sub(r'^@', '', video_ch)

    dual_mode = _dual_mode_enabled()

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"=========== Hanime1 爬取任务 - 第 {attempt}/{max_retries} 次尝试 ===========")

            spider: CrawlResult | None = None
            error: str | None = None

            if dual_mode:
                # Pass db skip_ids to HK so it can filter out already-sent
                # videos before making download_url page requests.
                try:
                    skip_ids = list(await db.get_all_hanime1_ids())
                except Exception as e:
                    logger.warning(f"get_all_hanime1_ids failed: {e}; sending empty skip list")
                    skip_ids = []
                spider = await _fetch_hanime1_via_hk(cfg, skip_ids=skip_ids)
                if spider is None:
                    if _fallback_local_allowed():
                        logger.info("falling back to local AsyncCamoufox for hanime1")
                        hm = Hanime1spider(cfg, db)
                        spider = await hm.do_job()
                    else:
                        logger.error(
                            "HK crawler unreachable and FALLBACK_LOCAL_BROWSER=0; "
                            "skipping this attempt"
                        )
                        error = "hk_unreachable_no_fallback"
            else:
                hm = Hanime1spider(cfg, db)
                spider = await hm.do_job()

            if spider is None:
                if dual_mode and error is None:
                    error = "hk_crawler_returned_none"
                if error is None:
                    error = "spider_returned_none"
                logger.warning(f'第 {attempt} 次：未能正确爬取，原因: {error}')
                if attempt < max_retries:
                    wait_seconds = retry_wait_minutes * 60
                    logger.info(f"将在 {retry_wait_minutes} 分钟后进行第 {attempt + 1} 次重试...")
                    await asyncio.sleep(wait_seconds)
                    continue
                else:
                    logger.error('已达到最大重试次数，任务失败')
                    return False

            if not spider.success:
                logger.warning(f'第 {attempt} 次：未能正确爬取，原因: {spider.error}')
                if attempt < max_retries:
                    wait_seconds = retry_wait_minutes * 60
                    logger.info(f"将在 {retry_wait_minutes} 分钟后进行第 {attempt + 1} 次重试...")
                    await asyncio.sleep(wait_seconds)
                    continue
                else:
                    logger.error('已达到最大重试次数，任务失败')
                    return False

            send_semaphore = asyncio.Semaphore(1)
            data_list = spider.data
            name_list = [item[0] for item in data_list]
            source_url_list = [item[1] for item in data_list]
            id_lists = []

            # 用于记录本轮发送的消息信息
            video_ch_ids = []  # 视频频道消息ID（来自send_source_video或DB查询）
            preview_ch_ids = []  # 预览图频道消息ID（来自send_video）

            date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()

            for url in source_url_list:
                match = re.search(r"\?v=(\d+)", url)
                if match:
                    id_lists.append(int(match.group(1)))

            try:
                for i in range(0, 30, 5):
                    na_list = name_list[i:i+5]
                    url_list = source_url_list[i:i+5]
                    id_list = id_lists[i:i+5]
                    video_batch_ids = []  # 本批次的视频消息ID
                    down_url_list = spider.detail[i:i+5]

                    video_paths = await start_batch_download(down_url_list, video_path, na_list)
                    mini_thumbs = [os.path.join(cover_path, f"{vid_id}_thumb.jpg") for vid_id in id_list]

                    pic_list = [generate_thumbnail(t_video_path=vi_paths, thumb_path=preview_path, cover_path=cover_path,
                                                 vid_id=vid, num=top, today=date, clean_name=na_ls)
                               for vi_paths, vid, top, na_ls in zip(video_paths, id_list, range(30-i, 25-i, -1), na_list)]
                    await asyncio.gather(*pic_list)

                    for ti, vid_path, mini_path, v_id in zip(na_list, video_paths, mini_thumbs, id_list):
                        if vid_path == 0:
                            # 视频已存在，从数据库获取之前保存的频道消息 ID
                            info = await db.get_hanime1_info(v_id)
                            ch_id = info[1] if info != 0 else 0
                        else:
                            # 视频是新下载的，上传并保存
                            ch_id = await send_source_video(client=client, title=ti, path=vid_path, ch_id=video_ch,
                                                           mini_thumb_path=mini_path, semaphore=send_semaphore)
                            if ch_id != 0:
                                await db.insert_hanime1_info(v_id, ti, ch_id)

                        video_batch_ids.append(ch_id)
                        video_ch_ids.append(ch_id)

                    date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()

                    prv_path = [os.path.join(preview_path, f"{video_id}.jpg") for video_id in id_list]

                    loop = asyncio.get_running_loop()
                    tasks = []
                    for path, num in zip(prv_path, range(30 - i, 25 - i, -1)):
                        tasks.append(loop.run_in_executor(None, write_text_on_image, path, num, date))

                    text_path = await asyncio.gather(*tasks)

                    for vid, urls, num, prv_pa, tit, ch__id in zip(id_list, url_list, range(30 - i, 25 - i, -1),
                                                                   text_path, na_list, video_batch_ids):
                        preview_msg_id = await send_video(client=client, video_id=vid, url=urls, top=num, path=prv_pa, channel_id=pic_ch,
                                       title=tit, ch_name=vid_name, ch_id=ch__id)
                        if preview_msg_id != 0:
                            preview_ch_ids.append(preview_msg_id)

                # 检查本轮是否成功发送了30条消息
                successful_preview_count = len([msg_id for msg_id in preview_ch_ids if msg_id != 0])

                if successful_preview_count < 30:
                    logger.warning(f"第 {attempt} 次尝试只成功发送了 {successful_preview_count} 条预览图（目标：30条）")
                    logger.info(f"注意：已发送的 {len([ch_id for ch_id in video_ch_ids if ch_id != 0])} 条视频消息和数据库记录将保留")

                    # 只删除预览图频道的消息ID
                    if preview_ch_ids:
                        logger.info(f"开始删除 {len([msg_id for msg_id in preview_ch_ids if msg_id != 0])} 条预览图消息...")
                        await delete_messages(client, pic_ch, [msg_id for msg_id in preview_ch_ids if msg_id != 0])

                    if attempt < max_retries:
                        wait_seconds = retry_wait_minutes * 60
                        logger.info(f"将在 {retry_wait_minutes} 分钟后进行第 {attempt + 1} 次重试...")
                        await asyncio.sleep(wait_seconds)
                        continue
                    else:
                        logger.error('已达到最大重试次数，但仍未成功发送30条预览图')
                        return False

                # 本轮成功，发送 Top 5
                logger.info(f"✅ 第 {attempt} 次尝试成功！已发送 {successful_preview_count} 条消息")
                rank_list = [f'https://t.me/{vid_name}/{video_id}' for video_id in video_ch_ids if video_id != 0][::-1]
                cover_paths = [os.path.join(cover_path, f"{video_id}.jpg") for video_id in id_lists[-5:]][::-1]
                await send_top5(client, ch_id=pic_ch, ranks=rank_list, source='hanime1', paths=cover_paths)

                logger.info("=========== Hanime1 爬取任务完成 ===========")
                return True

            except Exception as batch_error:
                logger.error(f"第 {attempt} 次尝试中的批处理出错: {batch_error}", exc_info=True)

                # 批处理异常时，删除预览图频道的消息，保留视频频道的消息和数据库记录
                logger.warning(f"批处理异常，已发送的 {len([ch_id for ch_id in video_ch_ids if ch_id != 0])} 条视频消息和数据库记录将保留")
                if preview_ch_ids:
                    logger.info(f"删除异常前发送的 {len([msg_id for msg_id in preview_ch_ids if msg_id != 0])} 条预览图消息...")
                    await delete_messages(client, pic_ch, [msg_id for msg_id in preview_ch_ids if msg_id != 0])

                if attempt < max_retries:
                    wait_seconds = retry_wait_minutes * 60
                    logger.info(f"将在 {retry_wait_minutes} 分钟后进行第 {attempt + 1} 次重试...")
                    await asyncio.sleep(wait_seconds)
                    continue
                else:
                    logger.error('已达到最大重试次数，任务失败')
                    return False

        except Exception as e:
            logger.error(f"第 {attempt} 次尝试发生未预期的错误: {e}", exc_info=True)

            if attempt < max_retries:
                wait_seconds = retry_wait_minutes * 60
                logger.info(f"将在 {retry_wait_minutes} 分钟后进行第 {attempt + 1} 次重试...")
                await asyncio.sleep(wait_seconds)
                continue
            else:
                logger.error('已达到最大重试次数，任务失败')
                return False

    return False



