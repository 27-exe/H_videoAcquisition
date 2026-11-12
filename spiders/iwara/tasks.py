import os,logging,re,asyncio
from pipelines.telegram_send import send_source_video,send_video,send_top5,delete_messages
from pipelines.aria2_download import start_batch_download
from pipelines.load import load_yaml
from spiders.base_spider import CrawlResult
from utils.pic_utils import generate_thumbnail,write_text_on_image
from datetime import datetime, timezone, timedelta
from pipelines.data_base import DataBase
# 2026-10-27: 单 VPS 合并 —— 抓取引擎(crawler 包)与 bot 同进程直调,
# 不再经 HK crawler_service 的 HTTP + SSH 反向隧道。
from crawler.iwara import crawl_iwara as _crawl_iwara
# 失败告警:crawler 包把结构化诊断(含堆栈)放进爬取返回值的 alerts 字段,
# 由这里转发到 admin 的 Telegram —— 本进程已经握着一个活着的 client。
from utils.notify import forward_crawl_alerts as _forward_crawl_alerts, notify_admin

logger = logging.getLogger(__name__)

base_path = os.getcwd()
spider_path = os.path.join(base_path, "download", "iwara")
video_path = os.path.join(spider_path, "video")
cover_path = os.path.join(spider_path, "cover")
preview_path = os.path.join(spider_path, "preview")


async def _crawl_iwara_local(
    cfg, skip_ids: list[str] | None = None,
) -> CrawlResult:
    """单 VPS 抓取入口(2026-10-27 起,HK crawler_service 已退役)。

    进程内调用 crawler.iwara.crawl_iwara,三段式路径:
      1. HTTP(curl_cffi + apiq JSON API,复用 cf_clearance)—— 主力,约 10 秒
      2. 被拦 → camoufox 重新 mint cookie → 重试一次
      3. 仍被拦 → 浏览器兜底(慢,罕见降级态)
    全程 async 非阻塞,不会冻住 event loop(watchdog 心跳不受影响)。

    skip_ids: 已在 db 中的 vid_id,抓取前过滤,避免重复解析与下载。
    None = 不过滤(仅调试用;生产路径必须传,否则 30 个全部当新视频重下)。

    返回 CrawlResult(形状兼容原 IwaraSpider.do_job 的下游消费);
    crawler 返回 ok=False 时 success=False,由 do_iwara 外层重试处理。
    """
    crawl_cfg = dict(cfg)
    crawl_cfg["page"] = int(cfg.get("page", 1))
    crawl_cfg["limit"] = int(cfg.get("limit", 30))
    if skip_ids is not None:
        crawl_cfg["skip_ids"] = skip_ids

    result = await _crawl_iwara(crawl_cfg)

    # crawler 包内的降级/兜底诊断(重新 mint 过 cookie、回落浏览器等)。
    # 正常爬取时 alerts 为空 → 这里不发任何消息。
    await _forward_crawl_alerts(result.get("alerts"), "iwara")
    if result.get("fallback_used") or result.get("remint_used"):
        logger.warning(
            f"iwara:降级路径(remint_used={result.get('remint_used')}, "
            f"fallback_used={result.get('fallback_used')})"
        )

    if not result.get("ok"):
        return CrawlResult(
            success=False,
            error=result.get("error") or result.get("message") or "crawl_failed",
        )

    # Map crawler items to legacy CrawlResult shape:
    #   data   = [name_list, source_url_list]
    #   detail = download_urls
    #   extra  = id_list
    # The downstream do_iwara consumer only reads these four.
    items = result.get("items") or []
    name_list = [it.get("title", "") for it in items]
    source_url_list = [it.get("source_url", "") for it in items]
    download_urls = [it.get("download_url", 0) for it in items]
    id_list = [it.get("id") for it in items]

    zero_count = sum(1 for d in download_urls if not d)
    logger.info(
        f"_crawl_iwara_local: got {len(items)} items, "
        f"{zero_count} download_url=0, "
        f"skip_ids={len(skip_ids) if skip_ids else 0}"
    )

    return CrawlResult(
        success=True,
        data=[name_list, source_url_list],
        detail=download_urls,
        extra=id_list,
        crawled_at=result.get("crawled_at") or datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
        page_url="<crawler>",
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
    # 2026-09-12: 3 → 5。历史基线是每次运行 1-6 个 CDN 偶发失败(与代码无关),
    # 阈值 3 会让正常波动就触发整轮重试(白跑 30 分钟)。env 可调。
    max_download_failures = int(os.environ.get("IWARA_MAX_DOWNLOAD_FAILURES", "5"))

    logger.info(
        f"do_iwara: starting max_retries={max_retries} "
        f"max_download_failures={max_download_failures}"
    )

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"=========== Iwara 爬取任务 - 第 {attempt}/{max_retries} 次尝试 ===========")

            spider: CrawlResult | None = None
            error: str | None = None

            # db skip_ids:抓取前过滤已发送视频,避免重复解析与下载。
            try:
                skip_ids = list(await db.get_all_iwara_ids())
            except Exception as e:
                # 拿不到 skip 集就不能安全抓取(会把 30 个全部当新视频重下重发),
                # 放弃本轮,交给外层重试。
                logger.error(
                    f"get_all_iwara_ids failed: {e!r}; skipping this attempt "
                    f"because we cannot determine skip set"
                )
                error = "db_lookup_failed"
                if attempt < max_retries:
                    wait_seconds = retry_wait_minutes * 60
                    logger.info(f"将在 {retry_wait_minutes} 分钟后进行第 {attempt + 1} 次重试...")
                    await asyncio.sleep(wait_seconds)
                    continue
                else:
                    logger.error('已达到最大重试次数，任务失败')
                    return False

            spider = await _crawl_iwara_local(cfg, skip_ids=skip_ids)

            if spider is None:
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
                                await db.insert_iwara_info(v_id, ti, ch_id, last_sent_date=date)

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
                            # 2026-11-12: 记录最后发送日期 —— 缩略图 TTL 清理的依据,
                            # 跨天复发的视频每次发送都会续期
                            try:
                                await db.update_iwara_last_sent(vid, date)
                            except Exception as e:
                                logger.warning(f"update_iwara_last_sent 失败 vid={vid}: {e}")

                # 检查本轮是否成功发送了30条消息
                successful_preview_count = len([msg_id for msg_id in preview_ch_ids if msg_id != 0])
                expected_preview_count = 30 - len(download_failures)
                video_ok_count = len([v for v in video_ch_ids if v and v != 0])
                logger.info(
                    f"do_iwara attempt {attempt}: "
                    f"video uploaded={video_ok_count}/30, "
                    f"preview sent={successful_preview_count}/{expected_preview_count}, "
                    f"download_failures={len(download_failures)}"
                )

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
                        await notify_admin(
                            "iwara:发送数量不足,任务最终失败",
                            f"重试 {max_retries} 次后仍只发出 {successful_preview_count}/"
                            f"{expected_preview_count} 条预览图。",
                            context={
                                "platform": "iwara", "stage": "send_preview",
                                "attempt": attempt, "sent": successful_preview_count,
                                "expected": expected_preview_count,
                                "download_failures": len(download_failures),
                            },
                            dedup_key="iwara:preview_shortfall",
                            client=client,
                        )
                        return False

                # 本轮成功，发送 Top 5
                logger.info(f"✅ 第 {attempt} 次尝试成功！已发送 {successful_preview_count} 条消息，跳过 {len(download_failures)} 个下载失败视频")
                rank_list = [f'https://t.me/{vid_name}/{video_id}' for video_id in video_ch_ids if video_id != 0][::-1]
                cover_paths = [os.path.join(cover_path, f"{video_id}.jpg") for video_id in id_lists[-5:]][::-1]
                await send_top5(client, ch_id=pic_ch, ranks=rank_list, source='iwara', paths=cover_paths)
                # Top5 封面也算"发送",续期最后发送日期(与预览更新互为兜底)
                for _cid in id_lists[-5:]:
                    try:
                        await db.update_iwara_last_sent(_cid, date)
                    except Exception as e:
                        logger.warning(f"update_iwara_last_sent(top5) 失败 id={_cid}: {e}")

                logger.info("=========== Iwara 爬取任务完成 ===========")
                return True

            except Exception as batch_error:
                logger.error(f"第 {attempt} 次尝试中的批处理出错: {batch_error}", exc_info=True)

                await notify_admin(
                    f"iwara:发送阶段出错(第 {attempt}/{max_retries} 次)",
                    "下载/上传/发帖批处理抛异常。已发出的视频消息与数据库记录会保留,"
                    "预览图消息已回滚。若是 FloodWait/账号限制类错误请优先处理。",
                    exc=batch_error,
                    context={
                        "platform": "iwara",
                        "stage": "send_batch",
                        "attempt": attempt,
                        "video_uploaded": len([v for v in video_ch_ids if v and v != 0]),
                        "preview_sent": len([m for m in preview_ch_ids if m]),
                        "download_failures": len(download_failures),
                    },
                    dedup_key=f"iwara:send_batch_fail:{type(batch_error).__name__}",
                    client=client,
                )

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
                await notify_admin(
                    "iwara:任务重试耗尽,最终失败",
                    f"每次尝试都抛出未预期异常,已重试 {max_retries} 次,任务放弃。",
                    exc=e,
                    context={"platform": "iwara", "stage": "task", "attempt": attempt},
                    dedup_key="iwara:task_exhausted",
                    client=client,
                )
                return False

    return False

