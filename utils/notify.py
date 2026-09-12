"""Operator alerting — push failure reports to the admin's Telegram account.

Motivation
----------
The crawler pipeline has several silent-failure modes: the HK crawler can fall
back from the fast HTTP path to the browser path (slow but working), the
cookie re-mint can fail, and the US bot's uploads can break. Previously all of
that only ever reached the log files. This module makes it reach the operator.

Design rules (all four are load-bearing)
----------------------------------------
1. **Never raise.** An alerting path that can throw turns a reportable failure
   into a second, unreportable one. Every public call swallows and logs.
2. **Deduplicate + rate-limit per key.** A persistently broken state that is
   retried every few minutes must not spam the operator. Same ``dedup_key``
   inside the window is dropped.
3. **Always carry the diagnosis**: component/stage, exception type + message,
   structured context (platform / url / counts), and a truncated stack trace.
4. **Credentials stay on the US bot.** Resolution order is env
   (``TG_API_ID`` / ``TG_API_HASH`` / ``TG_BOT_TOKEN`` / ``TG_ADMIN_ID``) then
   the repo's ``config/token.json`` + ``config/bot_cfg.json``. The HK crawler
   deliberately does NOT hold the bot token: it reports rich errors in its HTTP
   response and the bot (which already owns a live client) does the sending.

``NOTIFY_DISABLED=1`` turns everything off.

A bot can only message a user who has started it at least once — verify with a
live probe, never assume.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Dedup bookkeeping: dedup_key -> monotonic ts of last successful send.
_LAST_SENT: dict[str, float] = {}

DEFAULT_DEDUP_WINDOW_S = 1800
DEFAULT_MIN_INTERVAL_S = 30
_STACK_MAX_LINES = 18
_TG_MAX_CHARS = 4000

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ────────────────────────────────────────────────────────────── config

def notify_enabled() -> bool:
    return os.environ.get("NOTIFY_DISABLED", "0") not in ("1", "true", "True", "yes")


def _load_creds() -> Optional[dict[str, Any]]:
    """env first, then the repo's token.json + bot_cfg.json."""
    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    bot_token = os.environ.get("TG_BOT_TOKEN")
    admin_id = os.environ.get("TG_ADMIN_ID")

    if not all((api_id, api_hash, bot_token, admin_id)):
        import json
        try:
            tok = json.load(open(os.path.join(_REPO_ROOT, "config", "token.json")))
            cfg = json.load(open(os.path.join(_REPO_ROOT, "config", "bot_cfg.json")))
            api_id = api_id or tok.get("api_id")
            api_hash = api_hash or tok.get("api_hash")
            bot_token = bot_token or tok.get("bot_token")
            admin_id = admin_id or cfg.get("admin_id")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"notify: 读取本地 TG 配置失败: {type(e).__name__}: {e}")
            return None

    if not all((api_id, api_hash, bot_token, admin_id)):
        return None
    try:
        return {
            "api_id": int(api_id),
            "api_hash": str(api_hash),
            "bot_token": str(bot_token),
            "admin_id": int(admin_id),
        }
    except (TypeError, ValueError) as e:
        logger.warning(f"notify: TG 配置格式错误: {e}")
        return None


def notify_available() -> bool:
    return notify_enabled() and _load_creds() is not None


# ──────────────────────────────────────────────────────── formatting

def _now_str() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S CST")


def _format_stack(exc: Optional[BaseException]) -> str:
    if exc is None:
        return ""
    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:  # noqa: BLE001
        try:
            tb = "".join(traceback.format_exc())
        except Exception:  # noqa: BLE001
            return "<无法格式化堆栈>"
    lines = tb.rstrip().splitlines()
    if len(lines) > _STACK_MAX_LINES:
        lines = [f"... (省略前 {len(lines) - _STACK_MAX_LINES} 行) ..."] + lines[-_STACK_MAX_LINES:]
    return "\n".join(lines)


# Public alias: the HK crawler formats its own traceback with this before
# shipping it in the HTTP response (it holds no Telegram credentials on purpose).
format_stack = _format_stack


def build_alert(
    title: str,
    detail: str = "",
    context: Optional[dict] = None,
    exc: Optional[BaseException] = None,
    dedup_key: str = "",
) -> dict:
    """Package a diagnosis for transport from the HK crawler to the US bot.

    HK owns the real traceback but deliberately holds no Telegram credentials,
    so it serializes the stack here and ships it in the crawl response; the bot
    (which owns a live client) is what actually sends.
    """
    return {
        "title": title,
        "detail": detail,
        "context": context or {},
        "dedup_key": dedup_key,
        "stack": format_stack(exc) if exc is not None else "",
    }


def format_report(
    title: str,
    *,
    detail: str = "",
    exc: Optional[BaseException] = None,
    context: Optional[dict] = None,
    component: str = "",
    stack: str = "",
) -> str:
    parts = [f"🚨 {title}", ""]
    parts.append(f"时间: {_now_str()}")
    if component:
        parts.append(f"来源: {component}")
    if detail:
        parts += ["", f"说明: {detail}"]
    if context:
        parts.append("")
        parts.append("上下文:")
        for k, v in context.items():
            sv = str(v)
            if len(sv) > 300:
                sv = sv[:300] + "…"
            parts.append(f"  • {k}: {sv}")
    if exc is not None:
        parts += ["", f"异常: {type(exc).__name__}: {exc}"]
        exc_stack = _format_stack(exc)
        if exc_stack and not stack:
            stack = exc_stack
    if stack:
        # `stack` may be transported from the HK crawler, whose process owns the
        # real traceback; keep it verbatim so the diagnosis is not lost.
        lines = stack.rstrip().splitlines()
        if len(lines) > _STACK_MAX_LINES:
            lines = [f"... (省略前 {len(lines) - _STACK_MAX_LINES} 行) ..."] + lines[-_STACK_MAX_LINES:]
        parts += ["", "堆栈:", "\n".join(lines)]
    text = "\n".join(parts)
    if len(text) > _TG_MAX_CHARS:
        text = text[: _TG_MAX_CHARS - 30] + "\n… (消息超长已截断)"
    return text


# ────────────────────────────────────────────────────────── sending

def _dedup_ok(key: Optional[str], window_s: int) -> bool:
    if not key:
        return True
    now = time.monotonic()
    last = _LAST_SENT.get(key)
    if last is not None and (now - last) < window_s:
        logger.info(
            f"notify: 抑制重复告警 key={key} (距上次 {int(now - last)}s < {window_s}s)"
        )
        return False
    _LAST_SENT[key] = now
    return True


def _global_rate_ok() -> bool:
    now = time.monotonic()
    last = _LAST_SENT.get("__global__")
    if last is not None and (now - last) < DEFAULT_MIN_INTERVAL_S:
        return False
    _LAST_SENT["__global__"] = now
    return True


async def _send_via_client(client, admin_id: int, text: str) -> None:
    await client.send_message(admin_id, text)


async def _send_standalone(creds: dict, text: str) -> None:
    """Short-lived bot session. Bots allow several concurrent sessions, and the
    session file lives in a temp dir so it can never clash with the bot's own."""
    from telethon import TelegramClient

    tmp = tempfile.mkdtemp(prefix="hermes_notify_")
    try:
        client = TelegramClient(os.path.join(tmp, "notify"), creds["api_id"], creds["api_hash"])
        await client.start(bot_token=creds["bot_token"])
        try:
            await _send_via_client(client, creds["admin_id"], text)
        finally:
            await client.disconnect()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def forward_hk_alerts(alerts, source: str, client=None) -> None:
    """Forward HK-side structured alerts to the operator's Telegram.

    HK owns the real traceback but deliberately holds no Telegram credentials,
    so it serializes each diagnosis into the crawl response; sending happens on
    the US bot, which already holds a live client. Dedup keys come from HK, so a
    recurring fault notifies once per window instead of on every crawl.
    """
    for a in alerts or []:
        if not isinstance(a, dict):
            continue
        try:
            await notify_admin(
                a.get("title") or f"{source}:HK 侧告警",
                detail=a.get("detail", ""),
                context={**(a.get("context") or {}), "forwarded_from": "hk_crawler"},
                stack=a.get("stack", ""),
                dedup_key=a.get("dedup_key") or f"{source}:hk_alert",
                client=client,
            )
        except Exception as e:  # noqa: BLE001 — alerting must never break a crawl
            logger.warning(f"forward hk alert failed: {e!r}")


async def notify_admin(
    title: str,
    detail: str = "",
    *,
    exc: Optional[BaseException] = None,
    context: Optional[dict] = None,
    component: str = "",
    dedup_key: Optional[str] = None,
    dedup_window_s: int = DEFAULT_DEDUP_WINDOW_S,
    client=None,
    force: bool = False,
    stack: str = "",
) -> bool:
    """Send an alert to the admin's Telegram. Returns True if actually sent.

    Swallows every exception — see design rule 1. ``client`` is an optional live
    TelegramClient (the bot passes its own); otherwise a short-lived bot session
    is created from the resolved credentials.
    """
    try:
        if not notify_enabled():
            logger.info(f"notify: 已禁用(NOTIFY_DISABLED),丢弃告警: {title}")
            return False

        if not force:
            if not _dedup_ok(dedup_key, dedup_window_s):
                return False
            if not _global_rate_ok():
                logger.info(f"notify: 全局速率限制,丢弃告警: {title}")
                return False

        creds = _load_creds()
        if creds is None and client is None:
            logger.warning(f"notify: 无可用 TG 凭据,无法告警: {title}")
            return False

        text = format_report(
            title, detail=detail, exc=exc, context=context, component=component, stack=stack
        )

        # Prefer the caller's live client (the bot already holds one), but fall
        # back to a short-lived session instead of giving up.
        if client is not None:
            target = creds["admin_id"] if creds else os.environ.get("TG_ADMIN_ID")
            if target is not None:
                try:
                    await _send_via_client(client, int(target), text)
                    logger.info(f"notify: 告警已发送(现存 client): {title}")
                    return True
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"notify: 用现存 client 发送失败,转独立会话: {type(e).__name__}: {e}"
                    )
            else:
                logger.warning("notify: 有 client 但缺 admin_id,转独立会话")

        if creds is None:
            logger.warning(f"notify: 无可用 TG 凭据,无法告警: {title}")
            return False

        await _send_standalone(creds, text)
        logger.info(f"notify: 告警已发送: {title}")
        return True

    except Exception as e:  # noqa: BLE001
        logger.error(f"notify: 告警发送流程自身出错(已吞掉,不影响主流程): {type(e).__name__}: {e}")
        return False
