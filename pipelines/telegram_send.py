import logging,os
import traceback
from telethon.tl.custom import Button
from FastTelethonhelper import fast_upload
from datetime import datetime, timezone, timedelta
from utils.pic_utils import get_video_info_async
from telethon.tl.types import InputMediaUploadedDocument, DocumentAttributeVideo, DocumentAttributeFilename
logger = logging.getLogger(__name__)

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

            logger.warning(f"获取视频元数据失败: {e}")
            return 0

        vid_msg = await client.send_message(
            ch_id,
            message=cap,
            file=media
        )

        logger.debug(f'发送视频{title}到频道成功')
        vid_id = vid_msg.id
        return vid_id

    except Exception as e:
        logger.error(f"发送视频时出错: {str(e)}",traceback.format_exc())
        return 0
    finally:
        if path != 0:
            logger.debug("开始清理文件")
            try:
                if os.path.exists(path) :
                    os.remove(path)
                    os.remove(mini_thumb_path)
                    logger.debug(f"删除文件{path}{mini_thumb_path}")
            except Exception as e:
                logger.error(f"删除文件失败 {path}: {e}")


async def send_video(client,title,video_id,url,top,path,channel_id,ch_name,ch_id):
    try:
        cap = f'日期:{datetime.now(timezone(timedelta(hours=8))).date().isoformat()}\n位次: {top}\n标题:{title}'
        buttons = [
            [
                Button.url('源链接', f'{url}'),
                Button.url('点击播放视频', f'https://t.me/{ch_name}/{ch_id}'),
            ]
        ]
        await client.send_file(f'{channel_id}', path, caption=cap,buttons=buttons)
        logger.debug(f'成功发送视频{title}的预览图到频道')
    except Exception as e:
        logger.error(f"发送预览时出错: {str(e)}")



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
            #[
            #    Button.url('额外内容', f'{ext}'),  可在此添加自定义宣传内容
            #]
        ]
        await client.send_file(ch_id,paths,caption=cap)
        await client.send_message(f'{ch_id}', message=cap_1, buttons=buttons)
        logger.info(f'成功发送{today},top5消息')
    except Exception as e:
        logger.error(f"发送top5时出错: {e}", exc_info=True)


        
