"""缩略图/媒体 TTL 清扫器 (2026-11-12)

背景:cover/{vid}.jpg 与 preview/{vid}.jpg 每个视频各一个,发送后从不
删除 —— 设计如此,因为 54% 的视频会跨天重发(实测最长 13 天),删早了
下次复发时预览就发不出去(send_video 报 preview missing → 计数不足 →
整轮重试)。代价是无界增长:4 个月 2452 个视频攒了 1.4GB。

TTL 语义(2026-11-12 用户拍板方案一):
  - last_sent_date 距今超过 THUMB_TTL_DAYS(默认 21 天,为实测最长
    复发间隔 13 天的 1.6 倍)的视频:删文件 **并删数据库行**。
  - 删行是刻意的:行不在 skip_ids 里 → 复发时 download_url 恢复非零
    → 当作新视频重新下载/重传/重发。不维护"复用已发消息"的复杂路径。
  - 行在但文件已被删(残留孤儿行)也一并清掉,同上逻辑重新下载。

判定顺序(从硬到软):
  1. db 有 last_sent_date → 以它为准(跨天重发会自动续期);
  2. db 无日期(迁移前老行)→ 该视频现存文件的最大 mtime;
  3. 无 db 记录(孤儿文件)→ 文件 mtime。
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DOWNLOAD_ROOT = Path(__file__).resolve().parent.parent / "download"
CST = timezone(timedelta(hours=8))

SOURCES = {
    "hanime1": "int",
    "iwara": "str",
}


def _ttl_days() -> int:
    return int(os.environ.get("THUMB_TTL_DAYS", "21"))


def _today():
    return datetime.now(CST).date()


def _mtime_date(path):
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), CST).date()
    except OSError:
        return None


async def sweep_old_thumbs(db, ttl_days=None, root=None) -> dict:
    """按 last_sent_date 的 TTL 清理 cover/preview 文件 + 过期 db 行。

    db 需提供 get_hanime1_last_sent_map / get_iwara_last_sent_map /
    purge_hanime1_rows / purge_iwara_rows。
    返回统计 dict;单文件失败只记 warning,不中断。
    """
    root = Path(root) if root else DOWNLOAD_ROOT
    if ttl_days is None:
        ttl_days = _ttl_days()
    today = _today()
    cutoff = today - timedelta(days=ttl_days)

    stats = {
        "root": str(root),
        "ttl_days": ttl_days,
        "cutoff": cutoff.isoformat(),
        "deleted_files": 0,
        "freed_bytes": 0,
        "kept_files": 0,
        "purged_rows": 0,
        "per_source": {},
    }

    for source, key_type in SOURCES.items():
        base = root / source
        if not base.is_dir():
            continue
        try:
            if source == "hanime1":
                last_sent = await db.get_hanime1_last_sent_map()
            else:
                last_sent = await db.get_iwara_last_sent_map()
        except Exception as e:
            logger.warning("thumb_sweeper: 读取 %s last_sent 失败,跳过该源: %s", source, e)
            continue

        # 收集该源所有文件: vid -> [(path, mtime_date)]
        files = {}
        for sub in ("cover", "preview"):
            d = base / sub
            if not d.is_dir():
                continue
            for fname in os.listdir(d):
                if not fname.endswith(".jpg"):
                    continue
                vid_str = fname[:-4]
                if vid_str.endswith("_thumb"):  # mini thumb: cover/{vid}_thumb.jpg
                    vid_str = vid_str[: -len("_thumb")]
                try:
                    vid = int(vid_str) if key_type == "int" else vid_str
                except ValueError:
                    vid = None
                if vid is None:
                    continue
                files.setdefault(vid, []).append(
                    (d / fname, _mtime_date(d / fname))
                )

        # 逐 db 行判定
        purge = set()
        for vid, raw in last_sent.items():
            sent_date = None
            if raw:
                try:
                    sent_date = datetime.fromisoformat(raw).date()
                except ValueError:
                    sent_date = None
            if sent_date is None:
                # 迁移前老行:用现存文件的最大 mtime 当最后活跃日
                mdates = [md for _p, md in files.get(vid, []) if md]
                sent_date = max(mdates) if mdates else None
            if sent_date is None or sent_date < cutoff:
                purge.add(vid)

        deleted = kept = freed = 0
        for vid, entries in files.items():
            if vid in purge:
                expired = True
            elif vid not in last_sent:
                # 孤儿文件(无 db 行):mtime 规则
                mdates = [md for _p, md in entries if md]
                expired = not mdates or max(mdates) < cutoff
            else:
                expired = False

            for path, _md in entries:
                if not expired:
                    kept += 1
                    continue
                try:
                    size = os.path.getsize(path)
                    os.remove(path)
                    deleted += 1
                    freed += size
                except OSError as e:
                    logger.warning("thumb_sweeper: 删除失败 %s: %s", path, e)

        # 删 db 行(必须文件先删:崩溃时残留孤儿文件由 mtime 规则兜底)
        purged = 0
        if purge:
            try:
                ids = list(purge)
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    if source == "hanime1":
                        await db.purge_hanime1_rows(chunk)
                    else:
                        await db.purge_iwara_rows(chunk)
                    purged += len(chunk)
            except Exception as e:
                logger.error("thumb_sweeper: 删除 %s db 行失败: %s", source, e)

        stats["per_source"][source] = {
            "deleted_files": deleted,
            "kept_files": kept,
            "freed_mb": round(freed / 2**20, 1),
            "purged_rows": purged,
        }
        stats["deleted_files"] += deleted
        stats["freed_bytes"] += freed
        stats["kept_files"] += kept
        stats["purged_rows"] += purged

    logger.info(
        "thumb_sweeper: TTL %d 天 (cutoff %s), 删除文件 %d 个 (%.1f MB), "
        "保留文件 %d 个, 清理 db 行 %d 条",
        ttl_days,
        cutoff.isoformat(),
        stats["deleted_files"],
        stats["freed_bytes"] / 2**20,
        stats["kept_files"],
        stats["purged_rows"],
    )
    return stats
