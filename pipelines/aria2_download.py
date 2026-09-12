import logging,os,asyncio,time
from contextlib import asynccontextmanager
from aioaria2 import Aria2HttpClient

from pipelines.load import load_json

logger = logging.getLogger(__name__)

# ── 判停/重试参数(2026-09-12,全部 env 可调)────────────────────────────
# START_GRACE_S: 启动宽限期。last_completed 初值是 0,若不区分「还没开始传」
#   和「传到一半停了」,任何启动慢于 25s 的文件都会被 forceRemove 误杀 ——
#   重试又是 0,三次全灭而日志只有 0/0,看不出原因(实测踩过)。
# STALL_CHECKS: 宽限期过后,连续多少次(每次 5s)无进度才算真卡住。
# RETRY_SLEEP_S: 两次 attempt 之间的冷却。原来硬编码 3s —— 被 CDN 限流时
#   3s 根本不够冷却,重试直接掉进同一个窗口。
START_GRACE_S = float(os.environ.get("ARIA2_START_GRACE_S", "90"))
STALL_CHECKS = int(os.environ.get("ARIA2_STALL_CHECKS", "5"))
RETRY_SLEEP_S = float(os.environ.get("ARIA2_RETRY_SLEEP_S", "15"))

@asynccontextmanager
async def aria2_session(uri: str, token: str):

    client = Aria2HttpClient(uri, token=token)
    # 模拟进入 aioaria2 的异步上下文
    async with client as aria:
        logger.debug("Aria2 会话已启动")
        try:
            yield aria
        finally:
            # 此处可以放置额外的清理代码
            logger.debug("Aria2 会话已关闭")

async def _single_download(aria, url: str, dst: str, video_name: str, max_retries: int = 3):
    if url == 0:
        return 0

    # 可选：文件已存在直接跳过（防止重复下载）
    if os.path.exists(dst):
        existing_bytes = os.path.getsize(dst)
        if existing_bytes > 0:
            logger.info(
                f"[{video_name}] 文件已存在 ({existing_bytes} bytes)，跳过下载 -> {dst}"
            )
            return dst
        # 0-byte stub left over from a failed attempt; let aria2 overwrite it
        logger.warning(
            f"[{video_name}] \u53d1\u73b0 0-byte \u5e7b\u4f4d\u6587\u4ef6\uff0c\u5220\u9664\u540e\u91cd\u8bd5 -> {dst}"
        )
        try:
            os.remove(dst)
        except OSError as e:
            logger.error(f"[{video_name}] \u5220\u9664 0-byte \u6587\u4ef6\u5931\u8d25: {e}")
            return 0

    # 2026-09-12 实测(iwara CDN = nginx):单文件 16 并发连接会被 server 端掐断 ——
    # 同一个 URL 用单连接 curl 29s 完整下完(156MB),而 aria2 -x16 会卡在 96-98%
    # 不动,随后重试全部拿到 completed=0/0(连 Content-Length 都取不到,该 CDN 对
    # HEAD 直接返回 405)。3s 的重试间隔又正好撞在限流窗口内,于是 3 次全灭。
    # 吞吐上 16 连接只有 ~7MB/s,单连接就有 ~5.2MB/s —— 多出来的 40% 不值得拿尾部
    # 失败率去换。降到 2 并拉长冷却;可用 env 一键回退到旧值。
    options = {
        "dir": os.path.dirname(dst),
        "out": os.path.basename(dst),
        "max-connection-per-server": os.environ.get("ARIA2_MAX_CONN_PER_SERVER", "2"),
        "split": os.environ.get("ARIA2_SPLIT", "2"),
        "min-split-size": "1M",
        "continue": "true",
        # 让 aria2 自己先内部重试几次
        "max-tries": str(max_retries + 2),
        # 3s 太短:被 CDN 限流后需要更长的冷却窗口才能再连上
        "retry-wait": os.environ.get("ARIA2_RETRY_WAIT", "15"),
    }

    for attempt in range(1, max_retries + 1):
        gid = None
        try:
            gid = await aria.addUri([url], options)
            logger.info(f"[{video_name}] 第 {attempt}/{max_retries} 次尝试启动，GID: {gid}")
            last_completed = 0
            stuck_count = 0  # 计数器：记录进度不动的次数
            start_time = time.monotonic()  # 下载开始时间，用于超时检测
            transferred = False  # 是否已经真的收到过字节

            while True:
                status = await aria.tellStatus(gid)
                st = status.get("status")
                completed = int(status.get("completedLength", 0))
                total = int(status.get("totalLength", 0))

                if st == "complete":
                    logger.info(
                        f"[{video_name}] download complete: "
                        f"{completed} bytes -> {dst}"
                    )
                    return dst

                if st in ("error", "removed"):
                    # Surface the aria2 errorMessage/errorCode so retries
                    # are explainable. Without this, repeated retries all
                    # look identical and you have no idea what's failing.
                    err_code = status.get("errorCode", "?")
                    err_msg = status.get("errorMessage", "(no errorMessage)")
                    logger.warning(
                        f"[{video_name}] attempt {attempt}/{max_retries} failed: "
                        f"status={st} code={err_code} msg={err_msg} "
                        f"completed={completed} total={total}"
                    )
                    break  # 触发外层 for 循环重试

                if completed > 0:
                    transferred = True

                # --- 启动宽限 ---
                # 还没收到任何字节时不判停:否则「25s 内尚未启动」会被误当成
                # 「卡住」而 forceRemove,重试又从头开始,三次全灭只剩 0/0。
                if not transferred and (time.monotonic() - start_time) < START_GRACE_S:
                    await asyncio.sleep(5)
                    continue

                # --- 卡住检测逻辑 ---
                # 检测条件：进度无变化（宽限期后 completed 仍不动）
                if completed == last_completed:
                    stuck_count += 1
                else:
                    stuck_count = 0  # 进度有变化，重置计数器

                last_completed = completed

                # 条件 1：连续 STALL_CHECKS 次检查(约 STALL_CHECKS*5 秒)进度都没动
                if stuck_count >= STALL_CHECKS:
                    speed = int(status.get("downloadSpeed", 0))
                    # 把 aria2 的诊断字段一起打出来 —— 没有它,「卡住」只能靠猜
                    logger.warning(
                        f"[{video_name}] stalled: completed={completed}/{total} "
                        f"speed={speed} B/s, no progress for {STALL_CHECKS * 5}s, forcing retry "
                        f"(errorCode={status.get('errorCode', '-')}, "
                        f"connections={status.get('connections', '-')}, "
                        f"transferred={transferred}, "
                        f"files={str(status.get('files'))[:160]})"
                    )
                    try:
                        await aria.forceRemove(gid)
                    except Exception as fe:
                        logger.warning(f"[{video_name}] forceRemove failed (ignored): {fe}")
                    break  # 跳出 while 循环，触发外层 attempt 重试

                # 条件 2：单文件总耗时超过 10 分钟，强制终止
                elapsed = time.monotonic() - start_time
                if elapsed > 600:
                    logger.warning(
                        f"[{video_name}] download timeout: {elapsed:.0f}s elapsed, " 
                        f"completed={completed}/{total}, forcing retry"
                    )
                    try:
                        await aria.forceRemove(gid)
                    except Exception as fe:
                        logger.warning(f"[{video_name}] forceRemove failed (ignored): {fe}")
                    break
                # -----------------------

                await asyncio.sleep(5)

        except Exception as e:
            logger.warning(f"[{video_name}] 第 {attempt} 次请求异常: {e}")
            if gid:
                try:
                    await aria.forceRemove(gid)
                except:
                    pass  # 清理失败也无所谓

        if attempt < max_retries:
            logger.info(
                f"[{video_name}] 等待 {RETRY_SLEEP_S:.0f} 秒后进行第 {attempt + 1} 次重试..."
            )
            await asyncio.sleep(RETRY_SLEEP_S)

    logger.error(f"[{video_name}] 已达到最大重试次数 {max_retries}，最终失败。")
    # 清理残留的部分下载文件
    for f in [dst, dst + ".aria2"]:
        if os.path.exists(f):
            try:
                os.remove(f)
                logger.debug(f"[{video_name}] 已清理残留文件: {f}")
            except OSError:
                pass
    return 0

async def start_batch_download(urls: list[str], download_dir: str, names: list[str]):
    cfg = load_json('aria2.json')
    rpc_url = cfg['rpc_url']
    rpc_token = cfg['rpc_token']

    # 使用装饰好的上下文管理器
    async with aria2_session(rpc_url, rpc_token) as aria:
        tasks = []
        for url, name in zip(urls, names):
            # 拼接下载路径
            full_dst = os.path.join(download_dir, f"{name}.mp4")
            tasks.append(_single_download(aria, url, full_dst, name))

        # Real download is parallel via aria2's own concurrency; the
        # gather() here just awaits the coroutines that poll aria2.
        # We log before/after so the operator can see total batch time.
        active = sum(1 for u in urls if u and u != 0)
        skip = sum(1 for u in urls if u == 0)
        logger.info(
            f"start_batch_download: dir={download_dir} total={len(urls)} "
            f"active={active} skip={skip} (0 = already in db)"
        )
        results = await asyncio.gather(*tasks)
        ok = sum(1 for r in results if r and r != 0)
        failed = sum(1 for r in results if not r or r == 0)
        logger.info(
            f"start_batch_download: done ok={ok} failed={failed}"
        )
        return results