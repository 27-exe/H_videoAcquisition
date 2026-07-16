import os,logging,re,asyncio
from pipelines.telegram_send import send_source_video,send_video,send_top5,delete_messages
from spiders.iwara.crawler import  IwaraSpider
from pipelines.aria2_download import start_batch_download
from pipelines.load import load_yaml
from spiders.base_spider import CrawlResult
from utils.pic_utils import generate_thumbnail,write_text_on_image
from datetime import datetime, timezone, timedelta
from pipelines.data_base import DataBase
from utils.hk_crawler_client import fetch_via_hk_crawler, HKCrawlerError, HKUnreachable, HKCrawlFailure

logger = logging.getLogger(__name__)

base_path = os.getcwd()
spider_path = os.path.join(base_path, "download", "iwara")
video_path = os.path.join(spider_path, "video")
cover_path = os.path.join(spider_path, "cover")
preview_path = os.path.join(spider_path, "preview")


def _dual_mode_enabled() -> bool:
    """True when both CRAWLER_URL and CRAWLER_BEARER_TOKEN env vars are set.

    Operator's gate to switch from single-VPS (legacy) to dual-VPS (Phase 2)
    without modifying code. Without both env vars, bot runs the original
    local AsyncCamoufox path only — preserving the historical behavior.
    """
    return bool(os.environ.get("CRAWLER_URL")) and bool(os.environ.get("CRAWLER_BEARER_TOKEN"))


def _fallback_local_allowed() -> bool:
    """Whether to run local AsyncCamoufox if HK crawler is unreachable.

    Default True (FALLBACK_LOCAL_BROWSER not set or "1") because US bot has
    local browser already (legacy single-VPS path). Operators can disable
    fallback by setting FALLBACK_LOCAL_BROWSER=0; then bot returns False on
    HK failure and the next cron tick retries.
    """
    val = os.environ.get("FALLBACK_LOCAL_BROWSER", "1")
    return val not in ("0", "false", "False", "no")


async def _fetch_iwara_via_hk(
    cfg, skip_ids: list[str] | None = None,
    hk_max_retries: int = 3, hk_retry_delay: float = 5.0,
) -> "CrawlResult | None":
    """Phase 2 dual-VPS entry point: ask HK crawler for iwara items.

    skip_ids: list of vid_ids already in db — HK will skip these to save
    CDN requests.  None = no skip list (back-compat).

    Fallback semantics:
      - HKUnreachable  → return None immediately (caller falls back to local
                         AsyncCamoufox directly; matches old single-VPS retry
                         policy which never retried unreachable tunnels).
      - HKCrawlFailure → retry up to hk_max_retries times (HK reachable, payload
                         bad — could be iwara 5xx, parse failure, etc.).
                         After hk_max_retries failures, return None so caller
                         falls back to local browser (single attempt).

    Returns:
        CrawlResult on success, shape compatible with IwaraSpider.do_job().
        None if HK unreachable or HK crawl failed hk_max_retries times —
        caller should fall back to local IwaraSpider.do_job() once.
    """
    import asyncio
    last_err = None
    for attempt in range(1, hk_max_retries + 1):
        try:
            items = fetch_via_hk_crawler(
                "iwara",
                {
                    "keywords": cfg.get("keywords", "trending"),
                    "page": int(cfg.get("page", 1)),
                    "limit": int(cfg.get("limit", 30)),
                    **({"skip_ids": skip_ids} if skip_ids is not None else {}),
                },
            )
            break  # success
        except HKUnreachable as e:
            # Network-level failure: no point retrying — fall back immediately.
            logger.warning(
                f"hk crawler iwara unreachable (attempt {attempt}/{hk_max_retries}): {e}; "
                f"falling back to local browser immediately"
            )
            return None
        except HKCrawlFailure as e:
            last_err = e
            logger.warning(
                f"hk crawler iwara crawl failure (attempt {attempt}/{hk_max_retries}): {e}"
            )
            if attempt < hk_max_retries:
                logger.info(
                    f"retrying HK iwara after {hk_retry_delay}s..."
                )
                await asyncio.sleep(hk_retry_delay)
    else:
        # exhausted hk_max_retries; HK reachable but kept failing
        logger.warning(
            f"hk crawler iwara failed after {hk_max_retries} attempts "
            f"(last err: {last_err}); falling back to local browser"
        )
        return None

    # Map HK JSON items to legacy CrawlResult shape:
    #   data   = [name_list, source_url_list]
    #   detail = download_urls
    #   extra  = id_list
    # The downstream do_iwara consumer only reads these four.
    name_list = [it.get("title", "") for it in items]
    source_url_list = [it.get("source_url", "") for it in items]
    download_urls = [it.get("download_url", 0) for it in items]
    id_list = [it.get("id") for it in items]

    return CrawlResult(
        success=True,
        data=[name_list, source_url_list],
        detail=download_urls,
        extra=id_list,
        crawled_at=datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
        page_url="<hk-crawler>",
    )

async def do_iwara(client, db: DataBase, max_retries: int = 3, retry_wait_minutes: int = 10):
    """
    爬取和发送 Iwara 视频，支持重试机制

    Args:
        client: Telegram 客户端
        db: 数据库实例
        max_retries: 最大重试次数
        retry_wait_minutes: 重试等待分钟数
    """
    cfg = load_yaml('iwara.yaml')
    video_ch = cfg['video_channel']
    pic_ch = cfg['pic_channel']
    vid_name = re.sub(r'^@', '', video_ch)
    max_download_failures = 3

    dual_mode = _dual_mode_enabled()

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"=========== Iwara 爬取任务 - 第 {attempt}/{max_retries} 次尝试 ===========")

            spider: CrawlResult | None = None
            error: str | None = None

            if dual_mode:
                # Pass db skip_ids to HK so it can filter out already-sent
                # videos before making API + deobfuscation calls.
                try:
                    skip_ids = list(await db.get_all_iwara_ids())
                except Exception as e:
                    logger.warning(f"get_all_iwara_ids failed: {e}; sending empty skip list")
                    skip_ids = []
                spider = await _fetch_iwara_via_hk(cfg, skip_ids=skip_ids)
                if spider is None:
                    # HK crawler unreachable. Decide what to do next.
                    if _fallback_local_allowed():
                        logger.info("falling back to local AsyncCamoufox for iwara")
                        iwara = IwaraSpider(cfg, db)
                        spider = await iwara.do_job()
                    else:
                        logger.error(
                            "HK crawler unreachable and FALLBACK_LOCAL_BROWSER=0; "
                            "skipping this attempt"
                        )
                        error = "hk_unreachable_no_fallback"
                # else: HK succeeded, spider is set; do not call local.
            else:
                # legacy single-VPS path (no HK env vars).
                iwara = IwaraSpider(cfg, db)
                spider = await iwara.do_job()

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

            # 数据校验
            send_semaphore = asyncio.Semaphore(1)
            data_list = spider.data
            name_list = [item for item in data_list[0]]
            source_url_list = [item for item in data_list[1]]
            id_lists = spider.extra
            download_urls = spider.detail

            # 用于记录本轮发送的消息信息
            video_ch_ids = []  # 视频频道消息ID（来自send_source_video或DB查询）
            preview_ch_ids = []  # 预览图频道消息ID（来自send_video）
            download_failures = []

            date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()

            try:
                for i in range(0, 30, 5):
                    na_list = name_list[i:i + 5]
                    url_list = source_url_list[i:i + 5]
                    id_list = id_lists[i:i + 5]
                    video_batch_ids = []  # 本批次的视频消息ID
                    down_url_list = download_urls[i:i + 5]

                    # 下载视频
                    video_paths = await start_batch_download(down_url_list, video_path, na_list)
                    batch_failures = [
                        (vid, title)
                        for down_url, vid_path, vid, title in zip(down_url_list, video_paths, id_list, na_list)
                        if down_url != 0 and vid_path == 0
                    ]
                    download_failures.extend(batch_failures)

                    if batch_failures:
                        logger.warning(
                            f"本批次有 {len(batch_failures)} 个 Iwara 视频下载失败，"
                            f"本轮累计 {len(download_failures)}/{max_download_failures}"
                        )

                    if len(download_failures) > max_download_failures:
                        raise RuntimeError(f"Iwara 本轮下载失败超过 {max_download_failures} 个，触发任务重试")

                    mini_thumbs = [os.path.join(cover_path, f"{vid_id}_thumb.jpg") for vid_id in id_list]

                    # 生成缩略图
                    pic_list = [
                        generate_thumbnail(t_video_path=vi_paths, thumb_path=preview_path, cover_path=cover_path,
                                         vid_id=vid, num=top, today=date, clean_name=na_ls)
                        for vi_paths, vid, top, na_ls in
                        zip(video_paths, id_list, range(30 - i, 25 - i, -1), na_list)]
                    await asyncio.gather(*pic_list)

                    # 上传源视频
                    for ti, vid_path, mini_path, v_id, down_url in zip(na_list, video_paths, mini_thumbs, id_list, down_url_list):
                        if down_url != 0 and vid_path == 0:
                            logger.warning(f"跳过下载失败的 Iwara 视频: {v_id} - {ti}")
                            ch_id = 0
                        elif vid_path == 0:
                            # 视频已存在，从数据库获取之前保存的频道消息 ID
                            info = await db.get_iwara_info(v_id)
                            ch_id = info[1] if info != 0 else 0
                        else:
                            # 视频是新下载的，上传并保存
                            ch_id = await send_source_video(client=client, title=ti, path=vid_path, ch_id=video_ch,
                                                            mini_thumb_path=mini_path, semaphore=send_semaphore)
                            if ch_id != 0:
                                await db.insert_iwara_info(v_id, ti, ch_id)

                        video_batch_ids.append(ch_id)
                        video_ch_ids.append(ch_id)

                    date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()

                    # 生成预览图
                    prv_path = [os.path.join(preview_path, f"{video_id}.jpg") for video_id in id_list]

                    loop = asyncio.get_running_loop()
                    tasks = []

                    for path, num in zip(prv_path, range(30 - i, 25 - i, -1)):
                        tasks.append(loop.run_in_executor(None, write_text_on_image, path, num, date))

                    text_path = await asyncio.gather(*tasks)

                    # 发送预览图
                    for vid, urls, num, prv_pa, tit, ch__id in zip(id_list, url_list, range(30 - i, 25 - i, -1),
                                                                    text_path, na_list, video_batch_ids):
                        if not prv_pa or ch__id == 0:
                            logger.warning(f"跳过预览发送: {vid} - {tit}")
                            continue
                        preview_msg_id = await send_video(client=client, video_id=vid, url=urls, top=num, path=prv_pa, channel_id=pic_ch,
                                       title=tit, ch_name=vid_name, ch_id=ch__id)
                        if preview_msg_id != 0:
                            preview_ch_ids.append(preview_msg_id)

                # 检查本轮是否成功发送了30条消息
                successful_preview_count = len([msg_id for msg_id in preview_ch_ids if msg_id != 0])
                expected_preview_count = 30 - len(download_failures)

                if successful_preview_count < expected_preview_count:
                    logger.warning(f"第 {attempt} 次尝试只成功发送了 {successful_preview_count} 条预览图（目标：{expected_preview_count}条）")


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
                        logger.error(f'已达到最大重试次数，但仍未成功发送足够预览图（目标：{expected_preview_count}条）')
                        return False

                # 本轮成功，发送 Top 5
                logger.info(f"✅ 第 {attempt} 次尝试成功！已发送 {successful_preview_count} 条消息，跳过 {len(download_failures)} 个下载失败视频")
                rank_list = [f'https://t.me/{vid_name}/{video_id}' for video_id in video_ch_ids if video_id != 0][::-1]
                cover_paths = [os.path.join(cover_path, f"{video_id}.jpg") for video_id in id_lists[-5:]][::-1]
                await send_top5(client, ch_id=pic_ch, ranks=rank_list, source='iwara', paths=cover_paths)

                logger.info("=========== Iwara 爬取任务完成 ===========")
                return True

            except Exception as batch_error:
                logger.error(f"第 {attempt} 次尝试中的批处理出错: {batch_error}", exc_info=True)

                # 批处理异常时，删除预览图频道的消息，保留视频频道的消息和数据库记录
                logger.warning(f"处理异常，已发送的 {len([ch_id for ch_id in video_ch_ids if ch_id != 0])} 条视频消息和数据库记录将保留")
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

