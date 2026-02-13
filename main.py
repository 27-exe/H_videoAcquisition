from telethon import TelegramClient
import asyncio,logging
from utils.logging_setup import setup_logging
from pipelines.load import load_json
from command.bot_command import register_order_handlers
from pipelines.data_base import DataBase
from scheduled.task import TaskManager


setup_logging(log_file='bot.log',level=logging.DEBUG)

async def main():
    logger = logging.getLogger(__name__)
    logger.info("正在启动机器人...")
    try:
        token = load_json('token.json')
        api_id = token['api_id']
        api_hash = token['api_hash']
        bot_token = token['bot_token']

        client = TelegramClient('bot', api_id, api_hash)
        await client.start(bot_token=bot_token)
        logger.info("机器人已成功启动并运行")

        db = DataBase()
        ts = TaskManager(client,db)
        await register_order_handlers(client,db,ts)

        await client.run_until_disconnected()
        ts.shutdown()

    except Exception as ex:
        logger.error(f"机器人运行出错: {str(ex)}", exc_info=True)
        raise
    finally:
        logger.info("正在关闭机器人...")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] 机器人已由用户手动停止")
    except Exception as e:
        logging.error(f"程序异常终止: {str(e)}", exc_info=True)
        exit(1)