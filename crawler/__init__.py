"""crawler: 进程内抓取引擎（iwara / hanime1）。

2026-10-27 单 VPS 合并前的身份是 `crawler_service`——跑在 HK VPS 上的
FastAPI 服务，由 US bot 经 SSH 反向隧道 HTTP 调用。HK 退役后，同一套抓取
逻辑改为被 bot 进程直接 import 调用（见 spiders/{iwara,hanime1}/tasks.py
的 `_crawl_*_local`），FastAPI/uvicorn/bearer-auth 层随之删除。

抓取路径（2026-09-12 起）：
  HTTP 优先 —— curl_cffi(impersonate=firefox135)，iwara 走 apiq JSON API
  （复用 cf_clearance），hanime1 走服务端渲染 HTML（无 cookie）。
  浏览器(camoufox)只在两种情况下启用：iwara 的 cf_clearance 需要重新
  mint，以及 HTTP 路径被拦后的罕见兜底。
"""
