"""Bearer-token auth check.

Reads expected token from env (HK service) or comparison file.
US sends via Authorization header; missing/wrong header → 401.

Per plan §7 risk register: token 不入 yaml 不入 git; env-only.
"""
import os
from typing import Optional

from fastapi import Header, HTTPException, status


def _expected_token() -> Optional[str]:
    """读 env。env 未设直接 raise(防止脚本开机跑没 token 暴露 401 全过)。"""
    token = os.environ.get("CRAWLER_BEARER_TOKEN")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CRAWLER_BEARER_TOKEN not set in service env",
        )
    return token


async def require_bearer(
    authorization: Optional[str] = Header(default=None),
) -> None:
    expected = _expected_token()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization.split(None, 1)[1].strip()
    if presented != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bad token",
            headers={"WWW-Authenticate": "Bearer"},
        )
