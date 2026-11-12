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
                # 2026-11-12: last_sent_date 迁移 —— 缩略图 TTL 清理需要
                # 知道每个视频最后一次被发送的日期(跨天复发的视频不该被
                # 当成过期删掉;实测 54% 的视频会跨天重发,最长 13 天)。
                for table in ("iwara_info", "hanime1_info"):
                    cursor = await conn.execute(f"PRAGMA table_info({table})")
                    cols = [row[1] for row in await cursor.fetchall()]
                    if "last_sent_date" not in cols:
                        await conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN last_sent_date TEXT"
                        )
                        logger.info(f"数据库迁移: {table} 增加 last_sent_date 列")
                await conn.commit()
                logger.info(f"数据库 {self.db_file} 初始化成功")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}", exc_info=True)
            raise

# --- Iwara 表操作 ---

    async def insert_iwara_info(self, video_url: str, title: str,ch_id, last_sent_date: str | None = None):
        """插入或更新 Iwara 视频信息"""
        await self.ensure_initialized()
        async with self.edit_lock:
            try:
                async with aiosqlite.connect(self.db_file) as conn:
                    await conn.execute(
                        "INSERT OR REPLACE INTO iwara_info (video_url, title, ch_id, last_sent_date) VALUES (?, ?, ?, ?)",
                        (video_url, title, ch_id, last_sent_date)
                    )
                    await conn.commit()
                    logger.debug(f"Iwara 信息已存入数据库: {title}")
            except Exception as e:
                logger.error(f"写入 Iwara 数据库失败: {e}")

    async def update_iwara_last_sent(self, video_url: str, date: str):
        """记录 iwara 视频最后一次成功发送的日期(缩略图 TTL 清理依据)"""
        await self.ensure_initialized()
        async with self.edit_lock:
            try:
                async with aiosqlite.connect(self.db_file) as conn:
                    await conn.execute(
                        "UPDATE iwara_info SET last_sent_date = ? WHERE video_url = ?",
                        (date, video_url)
                    )
                    await conn.commit()
            except Exception as e:
                logger.error(f"更新 iwara last_sent_date 失败 video_url={video_url!r}: {e}")

    async def get_iwara_last_sent_map(self) -> dict:
        """返回 {video_url: last_sent_date or None} —— 供缩略图清扫器使用"""
        await self.ensure_initialized()
        try:
            async with aiosqlite.connect(self.db_file) as conn:
                async with conn.execute(
                        "SELECT video_url, last_sent_date FROM iwara_info"
                ) as cursor:
                    return {row[0]: row[1] for row in await cursor.fetchall()}
        except Exception as e:
            logger.error(f"查询 iwara last_sent 失败: {e}")
            return {}

    async def purge_iwara_rows(self, video_urls: list):
        """删除 iwara 记录 —— TTL 过期后让视频回到"新视频"状态重新下载。

        2026-11-12 用户拍板:不维护"复用已发消息"的路径,过期即释放行。
        """
        await self.ensure_initialized()
        async with self.edit_lock:
            try:
                async with aiosqlite.connect(self.db_file) as conn:
                    await conn.executemany(
                        "DELETE FROM iwara_info WHERE video_url = ?",
                        [(u,) for u in video_urls],
                    )
                    await conn.commit()
                    logger.info(f"已清理 {len(video_urls)} 条 iwara 记录(TTL)")
            except Exception as e:
                logger.error(f"清理 iwara 记录失败: {e}")

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
                    hit = result if result is not None else 0
                    logger.debug(f"get_iwara_info: video_url={video_url!r} hit={hit}")
                    return hit
        except Exception as e:
            logger.error(f"查询 Iwara 数据库失败: {e}", exc_info=True)
            return 0

    async def get_all_iwara_ids(self) -> set[str]:
        """Return all iwara video_ids in db — used to skip re-crawling."""
        await self.ensure_initialized()
        try:
            async with aiosqlite.connect(self.db_file) as conn:
                async with conn.execute("SELECT video_url FROM iwara_info") as cursor:
                    rows = await cursor.fetchall()
                    return {r[0] for r in rows}
        except Exception as e:
            logger.error(f"查询 Iwara id 列表失败: {e}")
            return set()

    # --- Hanime1 表操作 ---

    async def insert_hanime1_info(self, video_id: int, title: str,ch_id: int, last_sent_date: str | None = None):
        """插入或更新 Hanime1 视频信息"""
        await self.ensure_initialized()
        async with self.edit_lock:
            try:
                async with aiosqlite.connect(self.db_file) as conn:
                    await conn.execute(
                        "INSERT OR REPLACE INTO hanime1_info (video_id, title, ch_id, last_sent_date) VALUES (?, ?, ?, ?)",
                        (video_id, title, ch_id, last_sent_date)
                    )
                    await conn.commit()
                    logger.debug(f"Hanime1 信息已存入数据库: {title}")
            except Exception as e:
                logger.error(f"写入 Hanime1 数据库失败: {e}")

    async def update_hanime1_last_sent(self, video_id: int, date: str):
        """记录 hanime1 视频最后一次成功发送的日期(缩略图 TTL 清理依据)"""
        await self.ensure_initialized()
        async with self.edit_lock:
            try:
                async with aiosqlite.connect(self.db_file) as conn:
                    await conn.execute(
                        "UPDATE hanime1_info SET last_sent_date = ? WHERE video_id = ?",
                        (date, video_id)
                    )
                    await conn.commit()
            except Exception as e:
                logger.error(f"更新 hanime1 last_sent_date 失败 video_id={video_id}: {e}")

    async def get_hanime1_last_sent_map(self) -> dict:
        """返回 {video_id: last_sent_date or None} —— 供缩略图清扫器使用"""
        await self.ensure_initialized()
        try:
            async with aiosqlite.connect(self.db_file) as conn:
                async with conn.execute(
                        "SELECT video_id, last_sent_date FROM hanime1_info"
                ) as cursor:
                    return {row[0]: row[1] for row in await cursor.fetchall()}
        except Exception as e:
            logger.error(f"查询 hanime1 last_sent 失败: {e}")
            return {}

    async def purge_hanime1_rows(self, video_ids: list):
        """删除 hanime1 记录 —— TTL 过期后让视频回到"新视频"状态重新下载"""
        await self.ensure_initialized()
        async with self.edit_lock:
            try:
                async with aiosqlite.connect(self.db_file) as conn:
                    await conn.executemany(
                        "DELETE FROM hanime1_info WHERE video_id = ?",
                        [(i,) for i in video_ids],
                    )
                    await conn.commit()
                    logger.info(f"已清理 {len(video_ids)} 条 hanime1 记录(TTL)")
            except Exception as e:
                logger.error(f"清理 hanime1 记录失败: {e}")

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
                    hit = result if result is not None else 0
                    logger.debug(f"get_hanime1_info: video_id={video_id} hit={hit}")
                    return hit
        except Exception as e:
            logger.error(f"查询 Hanime1 数据库失败: {e}", exc_info=True)
            return 0

    async def get_all_hanime1_ids(self) -> set[int]:
        """Return all hanime1 video_ids in db — used to skip re-crawling."""
        await self.ensure_initialized()
        try:
            async with aiosqlite.connect(self.db_file) as conn:
                async with conn.execute("SELECT video_id FROM hanime1_info") as cursor:
                    rows = await cursor.fetchall()
                    return {r[0] for r in rows}
        except Exception as e:
            logger.error(f"查询 Hanime1 id 列表失败: {e}")
            return set()
