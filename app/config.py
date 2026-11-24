"""Application configuration and resolved local storage paths."""

from __future__ import annotations

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8765)
    workspace_root: str = Field(default_factory=lambda: str(PROJECT_ROOT.parent))
    drive_hub_root: str = Field(default_factory=lambda: str(PROJECT_ROOT / "data" / "NLM_Hub"))
    drive_library_root: str = Field(default_factory=lambda: str(PROJECT_ROOT / "data" / "Drive_Library"))
    chrome_path: str = Field(default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    default_master_email: str = Field(default="ultra-1@example.com")
    safety_buffer_pct: float = Field(default=10.0)
    poll_interval_min: int = Field(default=12)
    fleet_data_dir: str | None = Field(default=None)
    fleet_db_path: str | None = Field(default=None)
    fleet_disable_workers: bool = Field(default=False)


def get_settings() -> Settings:
    """Return a fresh Settings instance (respects env overrides in tests)."""
    return Settings()


def get_data_dir(settings: Settings | None = None) -> Path:
    """Return the runtime data directory and ensure subdirectories exist."""
    cfg = settings or get_settings()
    data_dir = Path(cfg.fleet_data_dir) if cfg.fleet_data_dir else (PROJECT_ROOT / "data")
    (data_dir / "oauth_tokens").mkdir(parents=True, exist_ok=True)
    (data_dir / "chrome_profiles").mkdir(parents=True, exist_ok=True)
    return data_dir


def get_db_path(settings: Settings | None = None) -> Path:
    """Resolve the SQLite database path."""
    cfg = settings or get_settings()
    if cfg.fleet_db_path:
        path = Path(cfg.fleet_db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return get_data_dir(cfg) / "fleet.db"


def get_client_secret_path(settings: Settings | None = None) -> Path:
    """Return the path where Google Cloud Desktop OAuth client_secret.json lives."""
    return get_data_dir(settings) / "client_secret.json"


def get_oauth_token_path(alias: str, settings: Settings | None = None) -> Path:
    """Return the per-account OAuth token JSON path for Official API."""
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in alias)
    return get_data_dir(settings) / "oauth_tokens" / f"{safe}.json"


def get_chrome_profile_dir(alias: str, settings: Settings | None = None) -> Path:
    """Return the isolated Chrome user-data-dir path for an account."""
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in alias)
    path = get_data_dir(settings) / "chrome_profiles" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path
