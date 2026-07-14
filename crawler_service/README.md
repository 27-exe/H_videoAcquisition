# crawler_service — HK-side per-request browser API

参见 `~/.hermes/plans/feat-split-crawler-hk.md` (itinerarium) 整体设计。

## 简介
- US bot 通过 HTTP POST 调 HK :8765 端点, 启动 AsyncCamoufox 浏览器,
  返回榜单 30 条 URL。
- **零持久化**: 不写 DB, 不留 state 文件, 不读旧 download_url 缓存。
- 每次请求 spawn 1 个浏览器实例, 跑完即关。

## 端点 (per plan §2)
- `POST /healthcheck`  无需 auth
- `POST /crawl/iwara`    bearer-auth
- `POST /crawl/hanime1`  bearer-auth
- `POST /shutdown`       仅 dev (env `CRAWLER_ENABLE_DEV_SHUTDOWN=1`)

## 本地试运行 (Phase 1 only, 不到 HK)

### 步骤 1: 起 tmux 长跑
```bash
TOKEN=$(openssl rand -hex 32)
tmux new-session -d -s videoacq-crawler \
  "CRAWLER_BEARER_TOKEN=$TOKEN \
   python -m uvicorn crawler_service.app:app \
     --host 127.0.0.1 --port 8765 --workers 1 --log-level info"
tmux ls
```

### 步骤 2: 验证
```bash
# healthcheck (无 auth)
curl -s http://127.0.0.1:8765/healthcheck | jq .

# auth fail
curl -i -X POST http://127.0.0.1:8765/crawl/iwara -d '{}'

# auth pass + crawl (placeholder; 真实 Phase 2 接 download_url 解析)
curl -s -X POST http://127.0.0.1:8765/crawl/iwara \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"headless":true,"limit":30}' | jq '.items | length'
```

## docker (debug 镜)
```bash
docker build -t videoacq-crawler .
docker run --rm -p 127.0.0.1:8765:8765 \
  -e CRAWLER_BEARER_TOKEN=$(openssl rand -hex 32) \
  videoacq-crawler
```
