# US-side deployment

This directory contains US-side systemd unit files for the dual-VPS bot
deployment.

| File | Purpose |
|---|---|
| `ssh-tunnel.service` | US → HK reverse tunnel on port 17650. Required for bot to reach HK crawler. |
| `videoAcq-bot.service` | The async videoAcq bot. Reads `CRAWLER_URL` etc. from `/etc/videoAcq/bot.env`. |
| `bot.env.template` | Template for bot env file. Operator fills in `CRAWLER_BEARER_TOKEN` from HK side. |
| `README.md` | this file |

## Layered state

```
┌─────────────────────────────┐  US bot env /etc/videoAcq/bot.env
│ Layer 1  bot logic          │  reads CRAWLER_URL=http://127.0.0.1:17650
│ Layer 2  ssh-tunnel service │  US-localhost :17650 → HK :8765 via SSH
│ Layer 3  HK crawler         │  bind 127.0.0.1:8765 on HK VPS
└─────────────────────────────┘
```

If **ANY** of layers 1, 2, 3 fails, US bot falls back to local
AsyncCamoufox path (legacy single-VPS) unless `FALLBACK_LOCAL_BROWSER=0`.

## Manual install (not run by script)

```bash
# 1. ssh-tunnel service
sudo cp deploy/us/ssh-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ssh-tunnel.service     # tunnel is up at boot
sudo systemctl start  ssh-tunnel.service
# verify: ss -tlnp | grep 17650

# 2. bot env file (NOT under deploy/us/ — protected by /etc/videoAcq)
sudo mkdir -p /etc/videoAcq
sudo chown 27exe:27exe /etc/videoAcq
sudo cp deploy/us/bot.env.template /etc/videoAcq/bot.env
sudo chown 27exe:27exe /etc/videoAcq/bot.env
sudo chmod 600 /etc/videoAcq/bot.env
# Edit and paste the bearer token copied from HK side (/etc/videoAcq/crawler.env):
sudo -u 27exe $EDITOR /etc/videoAcq/bot.env

# 3. bot service (NOT enabled per "configure but don't start")
sudo cp deploy/us/videoAcq-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
# deliberately not running `systemctl enable` or `start` — operator's choice.
```

## After Phase 2 commitment

When ready to flip the bot from single-VPS to dual-VPS:

```bash
# Verify tunnel
sudo systemctl status ssh-tunnel.service
ss -tlnp | grep 17650

# Verify HK crawler (over tunnel)
curl -s http://127.0.0.1:17650/healthcheck

# Verify bot env
sudo -u 27exe cat /etc/videoAcq/bot.env

# Activate dual-VPS for the bot service:
sudo systemctl enable --now videoAcq-bot.service

# Verify dual mode in logs (after bot's next tick):
journalctl -u videoAcq-bot.service | grep -E "dual|hk crawler"
```

## Rollback

If dual-VPS causes problems, simply unset the two env vars in
`/etc/videoAcq/bot.env`:

```bash
sudo sed -i \
  -e 's|^CRAWLER_URL=|#CRAWLER_URL=|' \
  -e 's|^CRAWLER_BEARER_TOKEN=|#CRAWLER_BEARER_TOKEN=|' \
  /etc/videoAcq/bot.env
sudo systemctl restart videoAcq-bot.service
```

Bot logs `dual_mode=False, falling back to legacy single-VPS path` on next tick.
