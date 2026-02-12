import logging,os
from telethon.tl.custom import Button
from FastTelethonhelper import fast_upload
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

async def send_source_video(client,title,path,ch_id):        #接受消息client,标题,发送日期,视频路径.返回值为发送目标频道的消息id,不包含频道用户名
    try:
        video_file = await fast_upload(client,path,name=title)
        cap = f'日期:{datetime.now(timezone(timedelta(hours=8))).date().isoformat()}\n标题:{title}'

        vid_msg = await client.send_message(f'{ch_id}',file= video_file,caption=cap,supports_streaming = True,force_document=False)
        logger.debug(f'发送视频{title}到频道成功')

        vid_id = vid_msg.id

        return vid_id
    except Exception as e:
        logger.error(f"发送视频时出错: {str(e)}")
    finally:
        # 清理本地图片文件
        if path:  # 显式检查paths 非空
            logger.debug("开始清理文件")
            try:
                if os.path.exists(path):
                    os.remove(path)
                    logger.debug(f"删除文件{path}")
            except FileNotFoundError as e:
                logger.error(f"Error deleting directory {path}: {e}")


async def send_video(client,title,video_id,url,top,path,channel_id,ch_name):
    try:
        cap = f'日期:{datetime.now(timezone(timedelta(hours=8))).date().isoformat()}\n位次: {top}\n标题:{title}'
        buttons = [
            [
                Button.url('源链接', f'{url}'),
                Button.url('点击播放视频', f'https://t.me/{ch_name}/{video_id}'),
            ]
        ]
        await client.send_file(f'{channel_id}', path, caption=cap,buttons=buttons)
        logger.debug(f'成功发送视频{title}的预览图到频道')
    except Exception as e:
        logger.error(f"发送预览时出错: {str(e)}")
    finally:
        # 清理本地图片文件
        if path:  
            logger.debug("开始清理文件")
            try:
                if os.path.exists(path):
                    os.remove(path)
                    logger.debug(f"删除文件{path}")
            except FileNotFoundError as e:
                logger.error(f"Error deleting directory {path}: {e}")


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


        
