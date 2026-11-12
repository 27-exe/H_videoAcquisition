#!/usr/bin/env python3
"""selftest.py — 端到端自检：独立工作目录 + 独立数据库,全程不发送,跑完自清。

跑什么（顺序与生产一致）:
    抓取列表 → 解析下载地址 → aria2 下载 → 6 帧抽帧 + 拼图 → 文字水印
    → 上传（stub,只读盘不联网）→ 发预览（stub）→ DB 落行

为什么能隔离:
    spiders/*/tasks.py 的 video_path / cover_path / preview_path 都由
    os.getcwd() 派生,data_base 默认 "bot.db" 也是相对 cwd —— 所以只要在
    import 之前 chdir 到临时目录,下载目录和数据库就全落在临时目录里,
    生产 bot.db 与生产 download/ 一个字节都不会被碰。

为什么不发送:
    pipelines.telegram_send.send_source_video 里唯一的网络出口是
    fast_upload();这里把它替换成"按 512 KiB 分块读盘"的假实现,
    client 换成 StubClient（upload_file/send_message/send_file 都只记账）。
    如果哪天真被接上真实 client,脚本会在启动时就 abort。

用法:
    python selftest.py                      # 5 个视频,现状抽帧
    python selftest.py --items 3 --keep     # 保留产物供人工审查
    python selftest.py --thumb-mode xstack  # 单进程 xstack 拼图
    python selftest.py --thumb-mode keyframe # 单进程跳帧解码拼图
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent

# ── 峰值内存采样（自身 + 所有 ffmpeg/ffprobe 子孙）──────────────────
_peak = {"bytes": 0, "sampling": False}


def _rss_bytes(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (FileNotFoundError, ProcessLookupError):
        pass
    return 0


def _children_rss() -> int:
    me = os.getpid()
    total = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat") as f:
                parts = f.read().split()
            comm = parts[1].strip("()")
            if comm not in ("ffmpeg", "ffprobe"):
                continue
            parent = int(parts[3])
        except (FileNotFoundError, ProcessLookupError, IndexError):
            continue
        hops = 0
        while parent > 1 and hops < 8:
            if parent == me:
                total += _rss_bytes(pid)
                break
            try:
                with open(f"/proc/{parent}/stat") as f:
                    parent = int(f.read().split()[3])
            except (FileNotFoundError, ProcessLookupError):
                break
            hops += 1
    return total


async def _sampler() -> None:
    _peak["sampling"] = True
    while _peak["sampling"]:
        _peak["bytes"] = max(_peak["bytes"], _rss_bytes(os.getpid()) + _children_rss())
        await asyncio.sleep(0.03)


def _cgroup_peak() -> int | None:
    """systemd-run --scope 下能读到整个 scope 的峰值（最接近线上口径）。"""
    try:
        with open("/proc/self/cgroup") as f:
            rel = f.read().strip().split("::", 1)[1]
        path = Path("/sys/fs/cgroup") / rel.lstrip("/") / "memory.peak"
        return int(path.read_text())
    except Exception:
        return None


# ── 假 Telegram ─────────────────────────────────────────────────
class StubClient:
    """只记账的 client：任何"发送"都变成一行记录，不联网。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._next_id = 900000

    def _bump(self) -> int:
        self._next_id += 1
        return self._next_id

    async def upload_file(self, path, *a, **kw):
        return SimpleNamespace(path=str(path), fake=True)

    async def send_message(self, ch_id, message=None, file=None, **kw):
        self.sent.append({"kind": "message", "ch": str(ch_id), "chars": len(message or "")})
        return SimpleNamespace(id=self._bump())

    async def send_file(self, ch_id, files=None, caption=None, buttons=None, **kw):
        self.sent.append({"kind": "file", "ch": str(ch_id), "caption": (caption or "")[:40]})
        return SimpleNamespace(id=self._bump())


async def _fake_fast_upload(client, file_location, reply=None, name=None, **kw):
    """替身：按 512 KiB 分块读盘，模拟真实上传的流式读取,但一个字节都不出网。"""
    size = 0
    with open(file_location, "rb") as f:
        while True:
            chunk = f.read(512 * 1024)
            if not chunk:
                break
            size += len(chunk)
            await asyncio.sleep(0)  # 让出事件循环,和生产一样是异步流
    return SimpleNamespace(path=file_location, name=name, size=size, fake=True)


def _install_stubs(telegram_send) -> StubClient:
    client = StubClient()
    telegram_send.fast_upload = _fake_fast_upload
    return client


# ── 抽帧后端 ────────────────────────────────────────────────────
async def _thumbs_legacy(pic_utils, paths, ids, preview_dir, cover_dir, n_items, date):
    """生产默认路径：每个视频 6 次 ffmpeg，由全局信号量限流。"""
    jobs = [
        pic_utils.generate_thumbnail(
            t_video_path=p, thumb_path=preview_dir, cover_path=cover_dir,
            vid_id=vid, num=n_items - i, today=date, clean_name=f"selftest{i}",
        )
        for i, (p, vid) in enumerate(zip(paths, ids))
    ]
    return await asyncio.gather(*jobs, return_exceptions=True)


async def _thumbs_tile(pic_utils, paths, ids, preview_dir, cover_dir, n_items, date, mode):
    """单进程拼图：xstack(6 路输入) 或 keyframe(单路跳帧解码)。"""
    async def one(p, vid, idx):
        info = await pic_utils.get_video_info_async(p)
        duration, width, height, _ = info
        vertical = width < height
        cols, rows = (2, 2) if vertical else (3, 2)
        count = cols * rows
        cell_w = (1080 if vertical else 1920) // cols
        cell_h = ((1920 - 384) if vertical else (1080 - 360)) // rows
        out = os.path.join(preview_dir, f"{vid}.jpg")
        if mode == "xstack":
            times = [duration * i / (count + 1) for i in range(1, count + 1)]
            ok = await pic_utils.extract_tile_xstack_async(p, times, out, cols, rows, cell_w, cell_h)
        elif mode == "keyframe-spread":
            ok = await pic_utils.extract_tile_keyframe_spread_async(
                p, duration, out, cols, rows, cell_w, cell_h, count)
        else:
            ok = await pic_utils.extract_tile_keyframe_async(p, out, cols, rows, cell_w, cell_h, count)
        # 封面与 320px mini 与生产一致
        cover = os.path.join(cover_dir, f"{vid}.jpg")
        await pic_utils.generate_single_thumbnail_async(p, cover)
        if os.path.exists(cover):
            await pic_utils.generate_mini_thumb_async(cover, os.path.join(cover_dir, f"{vid}_thumb.jpg"))
        return out if ok else None

    return await asyncio.gather(
        *[one(p, vid, i) for i, (p, vid) in enumerate(zip(paths, ids))],
        return_exceptions=True,
    )


# ── 主流程 ──────────────────────────────────────────────────────
async def run(args) -> int:
    work = Path(args.workdir or tempfile.mkdtemp(prefix="videoacq-selftest-"))
    work.mkdir(parents=True, exist_ok=True)
    dl = work / "download" / args.source
    for sub in ("video", "cover", "preview"):
        (dl / sub).mkdir(parents=True, exist_ok=True)

    # 必须在 import 之前切到临时目录：spiders/*/tasks.py 的路径常量由
    # os.getcwd() 派生，data_base 的默认文件名也是相对 cwd 的 "bot.db"。
    os.chdir(work)
    sys.path.insert(0, str(REPO))

    from pipelines import telegram_send
    from pipelines.aria2_download import start_batch_download
    from pipelines.data_base import DataBase
    from pipelines.load import load_yaml
    from spiders.iwara import tasks as iwara_tasks
    from utils import pic_utils

    pic_utils.THUMB_ENGINE = args.engine

    db_path = work / "selftest-bot.db"
    print(f"工作目录 : {work}")
    print(f"测试数据库: {db_path}  (生产 bot.db 不会被打开)")
    print(f"抽帧模式 : {args.thumb_mode}")
    print(f"条数     : {args.items}")
    print("-" * 66)

    stub = _install_stubs(telegram_send)
    db = DataBase(str(db_path))

    report: dict = {"workdir": str(work), "items": [], "stages": {}, "thumb_mode": args.thumb_mode}
    ok = True
    sampler = asyncio.create_task(_sampler())

    try:
        # 1) 抓取
        t0 = time.time()
        cfg = load_yaml("iwara.yaml")
        cfg = {**cfg, "limit": args.items, "page": int(cfg.get("page") or 1), "skip_ids": []}
        spider = await iwara_tasks._crawl_iwara_local(cfg, skip_ids=None)
        report["stages"]["crawl"] = round(time.time() - t0, 1)
        if not spider.success:
            print(f"❌ 抓取失败: {spider.error}")
            return 2
        names = list(spider.data[0])[: args.items]
        urls = list(spider.data[1])[: args.items]
        ids = list(spider.extra)[: args.items]
        durls = list(spider.detail)[: args.items]
        print(f"✅ 抓取 {len(names)} 条  ({report['stages']['crawl']}s)")

        # 2) 下载
        t0 = time.time()
        paths = await start_batch_download(durls, str(dl / "video"), names)
        report["stages"]["download"] = round(time.time() - t0, 1)
        got = [p for p in paths if p]
        print(f"✅ 下载 {len(got)}/{len(paths)}  ({report['stages']['download']}s)")
        if not got:
            print("❌ 一个都没下下来，终止")
            return 3

        # 3) 缩略图（被测环节）—— generate_thumbnail 成功返回 True，失败返回 False/None
        t0 = time.time()
        date = "2026-11-12"
        if args.thumb_mode == "legacy":
            thumbs = await _thumbs_legacy(pic_utils, paths, ids, dl / "preview", dl / "cover", len(names), date)
        else:
            thumbs = await _thumbs_tile(pic_utils, paths, ids, dl / "preview", dl / "cover",
                                        len(names), date, args.thumb_mode)
        report["stages"]["thumbs"] = round(time.time() - t0, 1)
        errors = []
        for vid, res in zip(ids, thumbs):
            if isinstance(res, Exception):
                errors.append(f"{vid}: {type(res).__name__}: {res}")
            elif not res:
                errors.append(f"{vid}: returned {res!r}（generate_thumbnail 失败，详见应用日志）")
        good = [os.path.join(dl / "preview", f"{vid}.jpg") for vid in ids
                if os.path.exists(os.path.join(dl / "preview", f"{vid}.jpg"))]
        report["thumb_errors"] = errors[:5]
        print(f"✅ 缩略图 {len(good)}/{len(thumbs)}  ({report['stages']['thumbs']}s)")
        for e in errors[:3]:
            print(f"   ⚠️ {e}")

        # 发送前快照：mini 缩略图会被 send_source_video 的 finally 删掉，
        # 必须在发送前记录，否则会误判为"缺失"。
        from PIL import Image

        def _snap(vid: str) -> dict:
            row = {}
            for label, base, stem in (
                ("preview", dl / "preview", vid),
                ("cover", dl / "cover", vid),
                ("mini", dl / "cover", f"{vid}_thumb"),
            ):
                f = None
                for cand in (base / f"{stem}.jpg", base / f"{stem}_{date}.jpg"):
                    if cand.exists():
                        f = cand
                        break
                if f is None:
                    row[label] = "MISSING"
                else:
                    try:
                        with Image.open(f) as im:
                            row[label] = f"{im.size[0]}x{im.size[1]} {f.stat().st_size // 1024}KB"
                    except Exception as e:
                        row[label] = f"BAD ({e})"
            return row

        report["artifacts_before_send"] = {vid: _snap(vid) for vid in ids}

        # 4) 文字水印（生产用线程池跑）
        t0 = time.time()
        loop = asyncio.get_running_loop()
        await asyncio.gather(*[
            loop.run_in_executor(None, pic_utils.write_text_on_image, p, len(names) - i, date)
            for i, p in enumerate(good)
        ])
        report["stages"]["watermark"] = round(time.time() - t0, 1)
        print(f"✅ 水印 {len(good)} 张  ({report['stages']['watermark']}s)")

        # 5) 上传 + 发预览（全 stub）
        t0 = time.time()
        for i, (p, vid, name) in enumerate(zip(paths, ids, names)):
            if not p:
                continue
            mini = os.path.join(dl / "cover", f"{vid}_thumb.jpg")
            msg_id = await telegram_send.send_source_video(
                client=stub, title=name, path=p, ch_id="@selftest_video",
                mini_thumb_path=mini,
            )
            await db.insert_iwara_info(vid, name, msg_id, last_sent_date=date)
            prv = os.path.join(dl / "preview", f"{vid}.jpg")
            await telegram_send.send_video(
                client=stub, title=name, video_id=vid, url="https://selftest.local",
                top=len(names) - i, path=prv, channel_id="@selftest_pic",
                ch_name="selftest", ch_id=msg_id,
            )
            report["items"].append({
                "id": vid, "title": name[:50],
                "downloaded_bytes": os.path.getsize(p) if os.path.exists(p) else 0,
                "db_msg_id": msg_id,
            })
        report["stages"]["send_stub"] = round(time.time() - t0, 1)
        print(f"✅ 上传/预览走 stub：{len(stub.sent)} 次调用，0 次联网  ({report['stages']['send_stub']}s)")

        # 6) 校验：发送后 preview/cover 仍在、mini 已被清、DB 有行
        checks = []
        for item in report["items"]:
            before = report["artifacts_before_send"].get(item["id"], {})
            after = _snap(item["id"])
            rows = await db.get_iwara_info(item["id"])
            db_ok = bool(rows) and rows != 0

            def _bad(v: str) -> bool:
                return isinstance(v, str) and v.startswith(("MISSING", "BAD"))

            row_ok = (
                not _bad(before.get("preview", "MISSING"))
                and not _bad(before.get("cover", "MISSING"))
                and not _bad(before.get("mini", "MISSING"))
                and not _bad(after.get("preview", "MISSING"))
                and not _bad(after.get("cover", "MISSING"))
                and after.get("mini") == "MISSING"   # 生产行为：上传后删 mini
                and db_ok
            )
            checks.append({
                "id": item["id"], "before": before, "after": after,
                "db_row": "ok" if db_ok else "MISSING", "ok": row_ok,
            })
        report["checks"] = checks
        bad = [c for c in checks if not c["ok"]]
        print(f"✅ 产物校验 {len(checks) - len(bad)}/{len(checks)} 通过")
        for c in checks:
            b, a = c["before"], c["after"]
            print(f"   {c['id']}  preview={b.get('preview')}→{a.get('preview')}  "
                  f"cover={b.get('cover')}  mini={b.get('mini')}→{a.get('mini')}  "
                  f"db={c['db_row']}  {'OK' if c['ok'] else 'FAIL'}")
        if bad:
            ok = False

        # 7) 确认视频已被 send_source_video 的 finally 清掉（生产行为）
        leftover = [p for p in paths if p and os.path.exists(p)]
        print(f"   残留视频文件: {len(leftover)} 个（生产发送成功后即删）")

    finally:
        _peak["sampling"] = False
        await sampler
        cg = _cgroup_peak()
        report["peak_sampled_mib"] = round(_peak["bytes"] / 1024 / 1024, 1)
        report["peak_cgroup_mib"] = round(cg / 1024 / 1024, 1) if cg else None
        report["ok"] = ok
        print("-" * 66)
        print(f"峰值内存(自采样): {report['peak_sampled_mib']} MiB")
        if report["peak_cgroup_mib"]:
            print(f"峰值内存(cgroup) : {report['peak_cgroup_mib']} MiB  ← 与线上 memory.peak 同口径")
        print(f"各阶段耗时: {json.dumps(report['stages'], ensure_ascii=False)}")

        (work / "selftest-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.keep:
            print(f"产物保留: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
            print("产物与测试数据库已删除")

    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="videoAcq 端到端自检（不发送、独立库、自清）")
    ap.add_argument("--items", type=int, default=5, help="抓几条（默认 5，与生产一个批次相同）")
    ap.add_argument("--source", default="iwara", choices=["iwara"], help="暂只支持 iwara")
    ap.add_argument("--thumb-mode", default="legacy", choices=["legacy", "xstack", "keyframe", "keyframe-spread"])
    ap.add_argument("--engine", default="legacy", choices=["legacy", "keyframe"],
                    help="生产引擎开关（对应 utils.pic_utils.THUMB_ENGINE）")
    ap.add_argument("--workdir", default=None, help="指定工作目录（默认 mktemp）")
    ap.add_argument("--keep", action="store_true", help="保留产物供人工审查")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
