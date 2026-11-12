"""download/ 目录清扫器 (2026-11-12)

背景:bot 被硬杀(watchdog SIGKILL / OOM / systemd stop)时,
_single_download 末尾的部分文件清理不会执行;aria2 又出于续传考虑
为未完成下载保留 .aria2 控制文件 —— 两者叠加导致孤儿控制文件和
烂尾 partial 永久堆积(实测 2414 个控制文件 + 16 个共 3.01GB 的
partial,横跨 23 天,把磁盘推到 74%)。本模块在 bot 启动时与每日
定时任务中清扫它们。

竞赛安全(三层):
  1. 先通过 aria2 RPC 取 tellActive + tellWaiting 的真实写入路径,
     正在写的一律不碰;
  2. 年龄阈值 max_age_hours(默认 24h)是第二道闸 —— 单文件下载有
     600s 超时 × 最多 2 次 attempt,任何活着的 attempt 都不可能
     超过 24h;partial mtime 比阈值老即判定死亡;
  3. RPC 不可达时退化为纯年龄判断,依然安全(见上一条)。
"""
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# bot 根目录 = 本文件的上两级(pipelines/ -> repo root)
DOWNLOAD_ROOT = Path(__file__).resolve().parent.parent / "download"


def _max_age_hours() -> float:
    return float(os.environ.get("ARIA2_SWEEP_MAX_AGE_HOURS", "24"))


async def _active_paths() -> set:
    """aria2 正在写入的文件绝对路径集合;RPC 失败返回空集合。"""
    active = set()
    try:
        from aioaria2 import Aria2HttpClient

        from pipelines.load import load_json

        cfg = load_json("aria2.json")
        client = Aria2HttpClient(cfg["rpc_url"], token=cfg["rpc_token"])
        async with client as aria:
            downloads = []
            try:
                downloads += await aria.tellActive() or []
            except Exception as e:
                logger.warning("sweeper: tellActive 失败: %s", e)
            try:
                downloads += await aria.tellWaiting(0, 1000) or []
            except Exception as e:
                logger.warning("sweeper: tellWaiting 失败: %s", e)
            for d in downloads:
                gid = d.get("gid")
                if not gid:
                    continue
                try:
                    for f in await aria.getFiles(gid) or []:
                        p = f.get("path")
                        if p:
                            active.add(os.path.realpath(p))
                except Exception:
                    continue
    except Exception as e:
        logger.warning("sweeper: aria2 RPC 不可达,退化为纯年龄判断: %s", e)
    return active


async def sweep_orphan_downloads(root=None, max_age_hours=None) -> dict:
    """清扫孤儿 .aria2 控制文件与烂尾 partial,顺带剪掉空目录。

    返回统计 dict;任何单文件失败只记 warning,不中断整体清扫。
    """
    root = Path(root) if root else DOWNLOAD_ROOT
    if max_age_hours is None:
        max_age_hours = _max_age_hours()
    if not root.is_dir():
        logger.info("sweeper: %s 不存在,跳过", root)
        return {"skipped": True, "root": str(root)}

    active = await _active_paths()
    cutoff = time.time() - max_age_hours * 3600
    stats = {
        "root": str(root),
        "orphan_ctrl": 0,
        "stale_partial": 0,
        "freed_bytes": 0,
        "skipped_active": 0,
    }

    for dp, _dns, fns in os.walk(root):
        for fn in fns:
            if not fn.endswith(".aria2"):
                continue
            ctrl = os.path.join(dp, fn)
            data = ctrl[: -len(".aria2")]
            if os.path.realpath(data) in active:
                stats["skipped_active"] += 1
                continue
            if os.path.exists(data):
                try:
                    if os.path.getmtime(data) >= cutoff:
                        continue  # 还新鲜,可能是刚启动的下载
                except OSError:
                    continue
                # 烂尾 partial:先删数据再删控制文件
                try:
                    stats["freed_bytes"] += os.path.getsize(data)
                    os.remove(data)
                    stats["stale_partial"] += 1
                except OSError as e:
                    logger.warning("sweeper: 删除 partial 失败 %s: %s", data, e)
                    continue
            else:
                stats["orphan_ctrl"] += 1
            try:
                os.remove(ctrl)
            except OSError as e:
                logger.warning("sweeper: 删除控制文件失败 %s: %s", ctrl, e)

    # 清理因此空掉的目录(标题里的 '/' 曾炸出大量垃圾嵌套目录)
    pruned = 0
    while True:
        n = 0
        for dp, _dns, _fns in os.walk(root, topdown=False):
            if os.path.abspath(dp) != os.path.abspath(str(root)) and not os.listdir(dp):
                try:
                    os.rmdir(dp)
                    n += 1
                except OSError:
                    pass
        pruned += n
        if n == 0:
            break
    stats["pruned_dirs"] = pruned

    logger.info(
        "sweeper: 孤儿控制文件 %d, 烂尾 partial %d (%.2f GB), 空目录 %d, 跳过活动下载 %d",
        stats["orphan_ctrl"],
        stats["stale_partial"],
        stats["freed_bytes"] / 2**30,
        pruned,
        stats["skipped_active"],
    )
    return stats
