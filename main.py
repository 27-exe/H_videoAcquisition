from telethon import TelegramClient
import asyncio,logging,os
from utils.logging_setup import setup_logging
from pipelines.load import load_json
from command.bot_command import register_order_handlers
from pipelines.data_base import DataBase
from scheduled.task import TaskManager

logger = logging.getLogger(__name__)


def _sd_notify(msg: str) -> bool:
    """Best-effort sd_notify — notify systemd (READY/WATCHDOG/STOPPING).

    Silently no-ops if NOTIFY_SOCKET is unset (i.e. not running under systemd
    Type=notify), or if libsystemd is missing. Never raises — this is a
    diagnostic signal, not a control flow.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    try:
        import ctypes
        lib = ctypes.CDLL("libsystemd.so.0", use_errno=True)
        lib.sd_notify.restype = ctypes.c_int
        rc = lib.sd_notify(0, msg.encode("utf-8"))
        if rc < 0:
            err = ctypes.get_errno()
            logger.debug("sd_notify(%r) failed errno=%d", msg.strip(), err)
            return False
        return True
    except Exception as e:
        logger.debug("sd_notify unavailable: %s", e)
        return False


async def _watchdog_loop(period: int = 30) -> None:
    """Periodically send WATCHDOG=1 to systemd.

    WatchdogSec=600 in the unit means systemd will SIGKILL the process if it
    does not receive WATCHDOG=1 within 10 minutes. We tick every 30s so a
    single missed tick still leaves 9.5 minutes of slack. Catches Telethon
    internal deadlocks (where the asyncio loop is wedged but the process
    hasn't exited).
    """
    try:
        while True:
            await asyncio.sleep(period)
            _sd_notify("WATCHDOG=1\n")
    except asyncio.CancelledError:
        # normal shutdown
        pass
    except Exception as e:
        logger.warning("watchdog tick failed: %s", e)


setup_logging(log_file='bot.log',level=logging.INFO)

async def main():
    ts = None
    logger.info("正在启动机器人...")
    try:
        token = load_json('token.json')
        api_id = token['api_id']
        api_hash = token['api_hash']
        bot_token = token['bot_token']

        client = TelegramClient('bot', api_id, api_hash)
        await client.start(bot_token=bot_token)
        logger.info("机器人已成功启动并运行")

        # 2026-08-24: Type=notify 必需 —— 告诉 systemd 我们已就绪,
        # 否则它会等满 TimeoutStartSec(1m30s) 把我们当启动失败杀掉
        _sd_notify("READY=1\n")

        db = DataBase()
        ts = TaskManager(client,db)
        await register_order_handlers(client,db,ts)

        # 2026-08-24: 自动装 scheduler —— 此前需要手动 /update,
        # bot 重启后无人发 /update 就一直"活死"等死,任务几天不跑
        ts.start_all()
        logger.info("scheduler auto-installed by systemd (no /update needed)")

        # 2026-08-24: 启动 watchdog 心跳,防 Telethon 内部死循环
        asyncio.create_task(_watchdog_loop(period=30))
        logger.info("watchdog notify started (period=30s, WatchdogSec=600)")

        await client.run_until_disconnected()


    except Exception as ex:
        logger.error(f"机器人运行出错: {str(ex)}", exc_info=True)
        raise
    finally:
        # 2026-08-24: 让 systemd 知道我们在退出,别等 TimeoutStopSec
        _sd_notify("STOPPING=1\n")
        logger.info("正在关闭机器人...")
        if ts :
            ts.shutdown()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("\n[!] 机器人已由用户手动停止")
    except Exception as e:
        logging.error(f"程序异常终止: {str(e)}", exc_info=True)
        exit(1)
