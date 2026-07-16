import logging,os
import traceback
from telethon.tl.custom import Button
from FastTelethonhelper import fast_upload
from datetime import datetime, timezone, timedelta
from utils.pic_utils import get_video_info_async
from telethon.tl.types import InputMediaUploadedDocument, DocumentAttributeVideo, DocumentAttributeFilename
from pipelines.load import load_yaml

logger = logging.getLogger(__name__)


def _load_promotion() -> dict | None:
    """\u8bfb config/telegram_send.local.yaml \u4e2d promotion \u5757\u3002

    \u8fd4\u56de {"text":..., "url":..., "enabled": bool} \u6216 None (\u6587\u4ef6\u4e0d\u5b58\u5728 / \u89e3\u6790\u5931\u8d25).
    \u8be5 yaml \u5728 .gitignore \u4e2d (\u5168\u673a\u672c\u5730\u4ee3\u4e0d\u5165 git),\u4e5f\u4e0d\u8fdb\u516c\u5171\u4ed3\u5e93.
    """
    try:
        data = load_yaml("telegram_send.local.yaml")
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug(f"promotion local yaml not loaded: {e}")
        return None
    if not isinstance(data, dict):
        return None
    promo = data.get("promotion", {})
    if not isinstance(promo, dict):
        return None
    return {
        "text": (promo.get("text") or "").strip(),
        "url": (promo.get("url") or "").strip(),
        "enabled": bool(promo.get("enabled", False)),
    }

async def delete_messages(client, channel_id, message_ids):
    """Delete the given message_ids from channel_id.

    Returns the number of messages actually deleted (0..len(message_ids)).
    Falsy / zero entries in message_ids are skipped.
    """
    if not message_ids:
        return 0
    valid = [m for m in message_ids if m and m != 0]
    if not valid:
        return 0
    deleted = 0
    failed: list[int] = []
    try:
        for msg_id in valid:
            try:
                await client.delete_messages(channel_id, msg_id)
                deleted += 1
            except Exception as e:
                logger.warning(
                    f"delete_messages: failed to delete msg_id={msg_id} in {channel_id}: {e}"
                )
                failed.append(msg_id)
        logger.info(
            f"delete_messages: {deleted}/{len(valid)} deleted in {channel_id}, "
            f"failed={failed if failed else 'none'}"
        )
    except Exception as e:
        logger.error(f"delete_messages: unexpected error during batch delete: {e}")
    return deleted

async def send_source_video(client, title, path, ch_id, mini_thumb_path,semaphore=None):  # 新增 semaphore 参数
    try:
        if path == 0:
            return 0
        cap = f'日期:{datetime.now(timezone(timedelta(hours=8))).date().isoformat()}\n标题:{title}'


        # 用信号量包裹发送（如果传了 semaphore）
        if semaphore:
            async with semaphore:
                video_file = await fast_upload(client, path, name=f"{title}.mp4")

            if os.path.exists(mini_thumb_path):
                thumb_file = await client.upload_file(mini_thumb_path)
            else:
                thumb_file = None
        else:
            video_file = await fast_upload(client, path, name=title)
            if os.path.exists(mini_thumb_path):
                thumb_file = await client.upload_file(mini_thumb_path)
            else:
                thumb_file = None
        try:
            duration_float, width, height, _ = await get_video_info_async(path)
            duration_sec = int(round(duration_float))  # Telegram 通常要整数秒

            if width <= 0 or height <= 0:
                width, height = 1280, 720  # 合理默认值

            media = InputMediaUploadedDocument(
                file=video_file,  # 直接用原来的 uploaded 对象
                mime_type='video/mp4',  # ← 强制指定，绕过自动推断
                attributes=[
                    DocumentAttributeVideo(
                        duration=duration_sec,
                        w=width,
                        h=height,
                        supports_streaming=True,
                        nosound=False
                    ),
                    DocumentAttributeFilename(f"{title}.mp4")  # 显示文件名（可保留日文）

                ],
                force_file=False,  # ← 关键：强制为媒体模式（非文档）
                thumb=thumb_file
            )
        except Exception as e:
            logger.warning(
                f"send_source_video: failed to probe metadata for {path!r} "
                f"title={title!r}: {e}"
            )
            return 0

        try:
            vid_msg = await client.send_message(
                ch_id,
                message=cap,
                file=media,
            )
        except Exception as e:
            logger.error(
                f"send_source_video: Telegram upload failed title={title!r} "
                f"ch_id={ch_id} path={path!r} size={os.path.getsize(path) if os.path.exists(path) else '?'} bytes: {e}"
            )
            return 0

        logger.info(
            f"send_source_video: uploaded title={title!r} ch={ch_id} "
            f"msg_id={vid_msg.id} size={os.path.getsize(path) if os.path.exists(path) else '?'} bytes"
        )
        return vid_msg.id

    except Exception as e:
        logger.error(f"send_source_video: top-level error title={title!r}: {e}", exc_info=True)
        return 0
    finally:
        if path and path != 0:
            try:
                if os.path.exists(path):
                    os.remove(path)
                    logger.debug(f"send_source_video: cleaned {path}")
                if os.path.exists(mini_thumb_path):
                    os.remove(mini_thumb_path)
                    logger.debug(f"send_source_video: cleaned thumb {mini_thumb_path}")
            except Exception as e:
                logger.error(
                    f"send_source_video: cleanup error path={path!r} thumb={mini_thumb_path!r}: {e}"
                )


async def send_video(client,title,video_id,url,top,path,channel_id,ch_name,ch_id):
    try:
        if not path or not os.path.exists(path):
            logger.warning(
                f"send_video: preview missing video_id={video_id} top={top} path={path!r}"
            )
            return 0
        cap = f'日期:{datetime.now(timezone(timedelta(hours=8))).date().isoformat()}\n位次: {top}\n标题:{title}'
        buttons = [
            [
                Button.url('源链接', f'{url}'),
                Button.url('点击播放视频', f'https://t.me/{ch_name}/{ch_id}'),
            ]
        ]
        msg = await client.send_file(f'{channel_id}', path, caption=cap, buttons=buttons)
        logger.info(
            f"send_video: sent video_id={video_id} top={top} "
            f"msg_id={msg.id} size={os.path.getsize(path)} bytes"
        )
        return msg.id  # 返回预览图消息ID
    except Exception as e:
        logger.error(
            f"send_video: failed video_id={video_id} top={top} "
            f"path={path!r} channel={channel_id}: {e}",
            exc_info=True,
        )
        return 0  # 发送失败返回0
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
                logger.debug(f"send_video: cleaned {path}")
            except OSError as e:
                logger.error(
                    f"send_video: cleanup error path={path!r}: {e}"
                )



async def send_top5(client,ch_id,ranks,source,paths,ext = None):
    try:
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        cap = f'本日 {source} top5\n日期:{today}\n'
        cap_1 = f'点击按钮跳转到对应视频🥰'
        buttons = [
            [
                Button.url('Top1', f'{ranks[0]}'),
                Button.url('Top2', f'{ranks[1]}'),
            ],
            [
                Button.url('Top3', f'{ranks[2]}'),
                Button.url('Top4', f'{ranks[3]}'),
                Button.url('Top5', f'{ranks[4]}'),
            ],
        ]
        # \u53ef\u9009 promotion row\uff1a\u4ece config/telegram_send.local.yaml \u8bfb\uff0c
        # enabled=true \u4e14 text/url \u90fd\u975e\u7a7a \u2192 \u8ffd\u52a0\u4e00\u4e2a button row\u3002
        promo = _load_promotion()
        if promo and promo["enabled"] and promo["text"] and promo["url"]:
            buttons.append([Button.url(promo["text"], promo["url"])])
            logger.info(f"send_top5 \u8ffd\u52a0 promotion row: text={promo['text']!r}")
        # Always log which top5 ranks we're sending, even on success.
        # Without this you can't reconstruct "which 5 ranks went to which
        # channel" from bot.log alone.
        valid_ranks = [r for r in ranks if r]
        if len(valid_ranks) != 5:
            logger.warning(
                f"send_top5: ranks has {len(valid_ranks)} non-empty entries "
                f"(expected 5): {ranks}"
            )
        existing_paths = [p for p in paths if p and os.path.exists(p)]
        missing_paths = [p for p in paths if p and not os.path.exists(p)]
        if missing_paths:
            logger.warning(
                f"send_top5: {len(missing_paths)}/{len(paths)} cover files missing: "
                f"{missing_paths}"
            )
        logger.info(
            f"send_top5: sending ch_id={ch_id} source={source} "
            f"5 ranks, {len(existing_paths)}/{len(paths)} cover files present"
        )

        await client.send_file(ch_id, paths, caption=cap)
        await client.send_message(f'{ch_id}', message=cap_1, buttons=buttons)
        logger.info(
            f"send_top5: done ch_id={ch_id} source={source} date={today} "
            f"buttons={len(buttons)} rows"
        )
    except Exception as e:
        logger.error(
            f"send_top5: failed ch_id={ch_id} source={source} "
            f"ranks={ranks} paths_count={len(paths) if paths else 0}: {e}",
            exc_info=True,
        )


        
