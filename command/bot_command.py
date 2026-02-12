import logging
from telethon import events
from telethon.events import StopPropagation
from pipelines.load import load_json
from scheduled.task import TaskManager
from spiders.hanime1.tasks import do_hanime1

logger = logging.getLogger(__name__)

async def register_order_handlers(client,db,ts:TaskManager):     #所有命令的列表
    logger.debug("命令捕获启动")
    cfg = load_json('bot_cfg.json')

    BOT_USERNAME = cfg['bot_username']
    ADMIN_ID = cfg['admin_id']

    @client.on(events.NewMessage(pattern=f'/start({BOT_USERNAME})?'))
    async def start(event):
        try:
            if event.is_private:
                await event.reply('你发现我啦!这里是私人bot捏.不开放使用')
            elif event.is_group:
                pass
        finally:
            raise StopPropagation

    @client.on(events.NewMessage(pattern=f'/help({BOT_USERNAME})?'))
    async def get_help(event):
        try:
            if event.is_private:
                await event.reply('这里是小27的色色下载bot(施工中,部分功能白名单制')
            elif event.is_group:
                pass
        finally:
            raise StopPropagation

    @client.on(events.NewMessage(pattern='/update'))
    async def start_updates(event):
        try:
            if event.sender_id == int(ADMIN_ID):
                ts.start_all()
                await event.reply('全部开始更新')
        except Exception as e:
            logger.error(f'出现错误{e}')
        finally:
            raise StopPropagation


    @client.on(events.NewMessage(pattern='/i_resume'))
    async def end_updates(event):
        try:
            if event.sender_id == int(ADMIN_ID):
                ts.resume_iwara()
                await event.reply('更新iwara')
        except Exception as e:
            logger.error(f'出现错误{e}')
        finally:
            raise StopPropagation

    @client.on(events.NewMessage(pattern='/h_resume'))
    async def end_updates(event):
        try:
            if event.sender_id == int(ADMIN_ID):
                ts.resume_hanime1()
                await event.reply('更新hanime1')
        except Exception as e:
            logger.error(f'出现错误{e}')
        finally:
            raise StopPropagation

    @client.on(events.NewMessage(pattern=f'/i_stop'))
    async def end_updates(event):
        try:
            if event.sender_id == int(ADMIN_ID):
                ts.pause_iwara()
                await event.reply('暂停更新iwara')
        except Exception as e:
            logger.error(f'出现错误{e}')
        finally:
            raise StopPropagation

    @client.on(events.NewMessage(pattern=f'/h_stop'))
    async def end_updates(event):
        try:
            if event.sender_id == int(ADMIN_ID):
                ts.pause_hanime1()
                await event.reply('暂停更新hanime1')
        except Exception as e:
            logger.error(f'出现错误{e}')
        finally:
            raise StopPropagation


    @client.on(events.NewMessage(pattern=r'^/run_iwara'))
    async def start_once(event):
        try:
            if event.sender_id == int(ADMIN_ID):


                await event.reply('立即更新iwara')
        finally:
            raise StopPropagation

    @client.on(events.NewMessage(pattern=r'^/run_hanime1'))
    async def start_once(event):
        try:
            if event.sender_id == int(ADMIN_ID):
                await event.reply('立即更新hanime1')
                await do_hanime1(client,db)
        finally:
            raise StopPropagation

    @client.on(events.NewMessage(pattern='/bye27'))
    async def stop_bot(event):
        # 仅允许特定用户（例如机器人管理员）执行关闭命令
        # 替换 YOUR_ADMIN_ID 为管理员的 Telegram 用户 ID
        if event.sender_id == int(ADMIN_ID):
            await event.respond('正在关闭机器人...')
            logger.critical("收到 终止 命令，机器人正在关闭...")

            # 断开客户端连接
            await client.disconnect()
            logger.info("机器人已断开连接")
        else:
            await event.respond('你没有权限关闭机器人！')
            logger.warning(f"用户{event.sender_id}尝试关闭")

