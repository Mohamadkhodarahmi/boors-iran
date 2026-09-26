"""HTTP client factories with the proxy split this deployment needs.

Iranian sources (TSETMC, CODAL) block foreign IPs, so they must never go through the
outbound proxy. Foreign services (Cloudflare Worker, Telegram) may need it.
"""

from __future__ import annotations

import os

import httpx

from bors_analyser.config import get_settings

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


def iran_client(**kwargs) -> httpx.AsyncClient:
    """Client for *.tsetmc.com / *.codal.ir — direct connection, proxy env ignored."""
    s = get_settings()
    return httpx.AsyncClient(
        headers={"User-Agent": BROWSER_UA},
        timeout=httpx.Timeout(s.http_timeout_s, connect=10.0),
        trust_env=False,
        follow_redirects=True,
        transport=httpx.AsyncHTTPTransport(retries=s.http_retries),
        **kwargs,
    )


def outbound_proxy() -> str | None:
    s = get_settings()
    if s.outbound_proxy is not None:
        return s.outbound_proxy or None
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None


def foreign_client(**kwargs) -> httpx.AsyncClient:
    """Client for Cloudflare / Telegram — goes through the configured outbound proxy."""
    s = get_settings()
    return httpx.AsyncClient(
        timeout=httpx.Timeout(s.http_timeout_s, connect=15.0),
        trust_env=False,
        proxy=outbound_proxy(),
        **kwargs,
    )
