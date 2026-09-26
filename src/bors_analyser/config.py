"""Runtime configuration.

Values come from environment variables / `.env` (secrets, paths, network) and from
`config/scoring.yaml` (every weight and threshold used by the decision layer).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="BORS_",
        extra="ignore",
    )

    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'bors.db').as_posix()}"
    log_dir: Path = PROJECT_ROOT / "logs"
    log_level: str = "INFO"
    model_dir: Path = PROJECT_ROOT / "models"
    scoring_config: Path = PROJECT_ROOT / "config" / "scoring.yaml"

    # Outbound proxy for foreign services (Cloudflare, Telegram). Iranian sources never use it.
    # None -> fall back to HTTPS_PROXY from the environment.
    outbound_proxy: str | None = None

    # Instrument universe: TSETMC yVal codes treated as stocks.
    # 300 = Bourse stock, 303 = Farabourse stock, 309 = Farabourse base market (پایه).
    stock_yvals: list[str] = Field(default_factory=lambda: ["300", "303", "309"])

    # Collector behaviour
    http_timeout_s: float = 30.0
    http_retries: int = 3
    tsetmc_concurrency: int = 6
    codal_concurrency: int = 3
    # CODAL's WAF throttles bursts per IP (HTTP 429): search.codal.ir is strict, www.codal.ir less so
    codal_search_interval_s: float = 5.0
    codal_report_interval_s: float = 1.0

    # Session (Tehran local time)
    session_start: str = "08:55"
    session_end: str = "12:35"
    snapshot_interval_min: int = 2

    # Cloudflare publishing
    worker_url: str | None = None
    ingest_token: str | None = None

    # Optional JEV event classifier (disabled when key is empty)
    jev_api_key: str | None = None

    # Local API
    api_host: str = "127.0.0.1"
    api_port: int = 8008


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_scoring_config() -> dict[str, Any]:
    path = get_settings().scoring_config
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
