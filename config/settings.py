"""
config/settings.py
──────────────────
Standalone settings for the data_importer repo.

data_importer's modules resolve `config.settings` relative to the
process's CWD at import time (see base.py's `sys.path.append(os.getcwd())`).
When data_importer runs standalone (its own tests, its own CI), CWD is
this repo's root, so this local config/ package is what gets imported.
When data_importer runs as a submodule inside the main Portfolio Insight
repo, CWD is the main repo's root instead, so its own (larger)
config/settings.py is used — this file is not read in that case.

Only the settings fields data_importer itself reads are defined here.
All fields are loaded from the .env file (or environment variables).
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Standalone data_importer configuration loaded from .env / env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── ClickHouse ───────────────────────────────────────────────────────────
    clickhouse_host: str = Field(default="localhost", description="ClickHouse host")
    clickhouse_port: int = Field(default=8123, description="ClickHouse HTTP port")
    clickhouse_database: str = Field(default="market_data", description="ClickHouse database")
    clickhouse_user: str = Field(default="default", description="ClickHouse username")
    clickhouse_password: str = Field(default="", description="ClickHouse password")

    # ── Market / ticker conventions ──────────────────────────────────────────
    nse_suffix: str = Field(default=".NS", description="Yahoo Finance NSE ticker suffix")
    bse_suffix: str = Field(default=".BO", description="Yahoo Finance BSE ticker suffix")
    market_timezone: str = Field(default="Asia/Kolkata", description="Market timezone")

    # ── Scraping ──────────────────────────────────────────────────────────────
    scrape_delay_seconds: float = Field(default=2.0, description="Delay between scrape requests")

    # ── News ──────────────────────────────────────────────────────────────────
    newsapi_key: str = Field(default="", description="NewsAPI.org API key")
    news_articles_per_stock: int = Field(default=5, description="Articles per stock")
    news_lookback_days: int = Field(default=7, description="News lookback window in days")
    newsapi_cache_ttl_seconds: int = Field(
        default=3600, description="NewsAPI response cache TTL in seconds (default 1 hour)"
    )

    # ── Gold / COMEX API ──────────────────────────────────────────────────────
    gold_api_key: str = Field(default="", description="gold-api.com API key")
    comex_cache_ttl_seconds: int = Field(
        default=3600, description="COMEX API response cache TTL in seconds (default 1 hour)"
    )


settings = Settings()
