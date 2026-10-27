# US-side deployment (single-VPS)

2026-10-27 起为**唯一节点**:抓取引擎(crawler 包)与 bot 同进程运行,
不再有 HK VPS、SSH 反向隧道与 bearer token。

| File | Purpose |
|---|---|
| `videoAcq-bot.service` | The async videoAcq bot. Reads env from `/etc/videoAcq/bot.env`. |
| `bot.env.template` | Template for the bot env file. |
| `README.md` | this file |

## Architecture

```
┌──────────────────────────────────────────────┐
│ US VPS (198.12.116.69, 1.9 GiB RAM + swap)  │
│                                              │
│  videoAcq-bot.service (Type=notify)          │
│  ├─ Telethon bot + APScheduler + SQLite      │
│  ├─ crawler 包(进程内抓取)                    │
│  │   ├─ HTTP 主力:curl_cffi(firefox135)      │
│  │   │   iwara→apiq JSON API(复用 cf_clearance)│
│  │   │   hanime1→服务端渲染 HTML(无 cookie)   │
│  │   └─ 浏览器(camoufox):仅 cookie re-mint    │
│  │      与 HTTP 被拦后的罕见兜底              │
│  ├─ aria2 + ffmpeg + 下载/上传/发帖           │
│  └─ notify.py 失败告警(本进程直发 TG)         │
└──────────────────────────────────────────────┘
```

抓取路径任何阶段被拦都有降级链:`*_HTTP_MODE=0` 一键回退纯浏览器路径
(代码保留,慢但自足)。

## Manual install

```bash
# 1. repo + venv
uv venv --python 3.11 .venv        # 本机实测 3.11 可跑(3.13 亦可)
uv pip install -r requirements.txt
.venv/bin/python -m camoufox fetch

# 2. /etc/videoAcq/bot.env
sudo mkdir -p /etc/videoAcq
sudo chown 27exe:27exe /etc/videoAcq
sudo cp deploy/us/bot.env.template /etc/videoAcq/bot.env
sudo chown 27exe:27exe /etc/videoAcq/bot.env
sudo chmod 600 /etc/videoAcq/bot.env
sudo -u 27exe $EDITOR /etc/videoAcq/bot.env

# 3. systemd unit
sudo cp deploy/us/videoAcq-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now videoAcq-bot.service
```

## Verify

```bash
systemctl status videoAcq-bot.service
journalctl -u videoAcq-bot.service -f      # 期望见:
#   "机器人已成功启动并运行" / "scheduler auto-installed" / watchdog 心跳
```

iwara 首次爬取会自动 mint `cf_clearance`(约 15s, camoufox 解 CF);
之后 cookie 长期有效(实测单枚 45 天+),失效时自动重新 mint。

## Rollback

回退到双 VPS 架构需要 git revert 到 `eca25ea` 之前的提交并恢复
`deploy/hk/` + `deploy/us/ssh-tunnel.service` + `utils/hk_crawler_client.py`,
在 bot.env 恢复 `CRAWLER_URL` / `CRAWLER_BEARER_TOKEN` 后重启 bot 与隧道。
