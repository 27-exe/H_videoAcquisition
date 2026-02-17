import logging
import logging.handlers
from queue import Queue
import sys
import atexit


def setup_logging(
        log_file="bot.log",  # 日志文件名
        max_bytes=5 * 1024 * 1024,  # 单个日志文件最大 5MB
        backup_count=3,  # 保留 3 个历史文件（bot.log.1, bot.log.2 等）
        level=logging.INFO,  # 默认 INFO 级别
):

    root_logger = logging.getLogger()

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    root_logger.setLevel(level)

    # 日志格式
    formatter = logging.Formatter(
        '%(asctime)s | %(name)-12s | %(levelname)-5s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 创建一个有界队列
    log_queue = Queue(maxsize=2000)
    queue_handler = logging.handlers.QueueHandler(log_queue)

    # 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.DEBUG)

    # 文件输出 + 自动按大小轮转
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    # QueueListener：真正异步消费队列并写到 console 和 file 的组件
    listener = logging.handlers.QueueListener(
        log_queue,
        console_handler,
        file_handler,
        respect_handler_level=True  # 尊重各个 handler 的 level 设置
    )

    listener.start()

    root_logger.addHandler(queue_handler)

    # 降低常见第三方库的日志噪音
    noisy_modules = [
        'telethon', 'aiosqlite', 'apscheduler',
        'aiohttp', 'playwright', 'urllib3', 'asyncio'
    ]
    for mod in noisy_modules:
        logging.getLogger(mod).setLevel(logging.WARNING)
    silenced_modules = [
        'playwright_captcha',
        'playwright_captcha.solvers.base_solver',
        'playwright_captcha.solvers.click.click_solver'
    ]

    for mod in silenced_modules:
        l = logging.getLogger(mod)
        l.setLevel(logging.CRITICAL)  # 只有 CRITICAL 级别才能通过（屏蔽 ERROR）
        l.propagate = False  # 禁止向上传播给 root logger (关键)
        l.handlers = []  # 清空它可能自带的 handler

    # 注册程序退出时自动停止 listener（防止警告或资源泄漏）
    def stop_listener():
        try:
            listener.stop()
        except Exception:
            pass  # 退出时出错就忽略

    atexit.register(stop_listener)

    # 输出一条确认日志
    logging.getLogger(__name__).info(
        "异步日志系统已启动 | 文件: %s ",
        log_file
    )



if __name__ == '__main__':
    # 调用配置（可以自定义参数）
    setup_logging(
        log_file="bot.log",
        max_bytes=10 * 1024 * 1024,
        backup_count=5,
        level=logging.INFO
    )
