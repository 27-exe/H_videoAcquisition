import aiosqlite
import asyncio
import logging

logger = logging.getLogger(__name__)

class DataBase:
    _instance = None
    _initialized = False
    _initializing = False

    def __new__(cls, db_file="bot.db"):
        if cls._instance is None:
            logger.debug("创建新的 DataBase 实例")
            cls._instance = super(DataBase, cls).__new__(cls)
            cls._instance.db_file = db_file
            cls._instance.edit_lock = asyncio.Lock()
        return cls._instance

    def __init__(self, db_file="bot.db"):
        if not hasattr(self, '_init_called'):
            logger.debug(f"初始化 DataBase，db_file={db_file}")
            self._init_called = True

    async def ensure_initialized(self):
        if not self._initialized and not self._initializing:
            self._initializing = True
            try:
                logger.debug("开始数据库初始化")
                await self._init_db()
                self.__class__._initialized = True
                logger.debug("数据库初始化完成")
            finally:
                self._initializing = False
        elif self._initializing:
            while self._initializing:
                await asyncio.sleep(0.1)

    async def _init_db(self):
        try:
            async with aiosqlite.connect(self.db_file) as conn:
                # 创建或更新 video_info 表，包含 title 字段
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS iwara_info
                    (
                        video_url TEXT PRIMARY KEY,
                        title TEXT,
                        ch_id INTEGER
                    )
                """)
                await conn.execute("""
                   CREATE TABLE IF NOT EXISTS hanime1_info
                   (
                        video_id INTEGER PRIMARY KEY,
                        title TEXT,
                        ch_id INTEGER
                   )
                   """)
                await conn.commit()
                logger.info(f"数据库 {self.db_file} 初始化成功")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}", exc_info=True)
            raise

# --- Iwara 表操作 ---

    async def insert_iwara_info(self, video_url: str, title: str,ch_id):
        """插入或更新 Iwara 视频信息"""
        await self.ensure_initialized()
        async with self.edit_lock:
            try:
                async with aiosqlite.connect(self.db_file) as conn:
                    await conn.execute(
                        "INSERT OR REPLACE INTO iwara_info (video_url, title,ch_id) VALUES (?, ?, ?)",
                        (video_url, title,ch_id)
                    )
                    await conn.commit()
                    logger.debug(f"Iwara 信息已存入数据库: {title}")
            except Exception as e:
                logger.error(f"写入 Iwara 数据库失败: {e}")

    async def get_iwara_info(self, video_url: str):
        """根据 URL 查询 Iwara 信息，返回 ( title) 或 0"""
        await self.ensure_initialized()
        try:
            async with aiosqlite.connect(self.db_file) as conn:
                async with conn.execute(
                        "SELECT title, ch_id FROM iwara_info WHERE video_url = ?",
                        (video_url,)
                ) as cursor:
                    result = await cursor.fetchone()
                    return result if result is not None else 0
        except Exception as e:
            logger.error(f"查询 Iwara 数据库失败: {e}")
            return 0
    # --- Hanime1 表操作 ---

    async def insert_hanime1_info(self, video_id: int, title: str,ch_id: int):
        """插入或更新 Hanime1 视频信息"""
        await self.ensure_initialized()
        async with self.edit_lock:
            try:
                async with aiosqlite.connect(self.db_file) as conn:
                    await conn.execute(
                        "INSERT OR REPLACE INTO hanime1_info (video_id, title, ch_id) VALUES (?, ?, ?)",
                        (video_id, title,ch_id)
                    )
                    await conn.commit()
                    logger.debug(f"Hanime1 信息已存入数据库: {title}")
            except Exception as e:
                logger.error(f"写入 Hanime1 数据库失败: {e}")

    async def get_hanime1_info(self, video_id: int):
        """根据 ID 查询 Hanime1 信息，返回 ( title等) 或 0"""
        await self.ensure_initialized()
        try:
            async with aiosqlite.connect(self.db_file) as conn:
                async with conn.execute(
                        "SELECT  title, ch_id FROM hanime1_info WHERE video_id = ?",
                        (video_id,)
                ) as cursor:
                    result = await cursor.fetchone()
                    return result if result is not None else 0
        except Exception as e:
            logger.error(f"查询 Hanime1 数据库失败: {e}")
            return 0
