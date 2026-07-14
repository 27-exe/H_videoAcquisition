# videoAcq HK deployment files

This directory contains HK-side deployment artifacts:

| File | Purpose |
|---|---|
| `swap-setup.sh` | One-shot, idempotent. Adds 4G swapfile + swappiness=10. |
| `videoAcq-crawler.service` | systemd unit for the FastAPI crawler. **Not enabled by default.** |
| `README.md` | this file |

## Install (manual reference; not run by script)

```bash
# 1. swap
sudo bash deploy/hk/swap-setup.sh

# 2. venv + deps + camoufox firefox binary
cd /home/a27exe/Projects/code/project/videoAcquisition-hk
uv venv --python 3.13 .venv
uv pip install -r requirements.txt
.venv/bin/python -m camoufox fetch

# 3. /etc/videoAcq/crawler.env (TOKEN here)
sudo mkdir -p /etc/videoAcq
sudo chown a27exe:a27exe /etc/videoAcq
# Generate CRAWLER_BEARER_TOKEN via: openssl rand -hex 32
sudo tee /etc/videoAcq/crawler.env > /dev/null <<EOF
CRAWLER_BEARER_TOKEN=<paste>
PYTHONUNBUFFERED=1
HOME=/home/a27exe
PATH=/home/a27exe/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EOF
sudo chmod 600 /etc/videoAcq/crawler.env

# 4. systemd unit
sudo cp deploy/hk/videoAcq-crawler.service /etc/systemd/system/
sudo systemctl daemon-reload

# 5. (NOT enabled; per user "配置但不启动")
# sudo systemctl enable videoAcq-crawler.service
# sudo systemctl start videoAcq-crawler.service
```

## Verify (after manual start)

```bash
systemctl status videoAcq-crawler.service
curl -s http://127.0.0.1:8765/healthcheck | jq .
curl -s -X POST http://127.0.0.1:8765/crawl/iwara \
  -H "Authorization: Bearer $CRAWLER_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"headless":true,"limit":30}' | jq '.items | length'
```
