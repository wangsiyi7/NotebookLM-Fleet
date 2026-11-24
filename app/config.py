"""Application configuration and resolved local storage paths."""

from __future__ import annotations

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .core.win_runtime import (
    get_app_home,
    get_bundle_root,
    get_default_data_dir,
    get_default_drive_paths,
    is_frozen,
)

PROJECT_ROOT = get_bundle_root()
APP_HOME = get_app_home()


def _resolve_env_file() -> str:
    for candidate in (APP_HOME / ".env", get_default_data_dir() / ".env", PROJECT_ROOT / ".env"):
        if candidate.exists():
            return str(candidate)
    return str(APP_HOME / ".env")


def _default_workspace_root() -> str:
    if is_frozen():
        docs = Path.home() / "Documents"
        return str(docs if docs.exists() else Path.home())
    return str(APP_HOME.parent)


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8765)
    workspace_root: str = Field(default_factory=_default_workspace_root)
    drive_hub_root: str = Field(default_factory=lambda: get_default_drive_paths(get_default_data_dir())[1])
    drive_library_root: str = Field(default_factory=lambda: get_default_drive_paths(get_default_data_dir())[0])
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
    data_dir = Path(cfg.fleet_data_dir) if cfg.fleet_data_dir else get_default_data_dir()
    (data_dir / "oauth_tokens").mkdir(parents=True, exist_ok=True)
    (data_dir / "chrome_profiles").mkdir(parents=True, exist_ok=True)
    (data_dir / "bridge_tmp").mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
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


def get_baidupan_auth_path(settings: Settings | None = None) -> Path:
    """Return the path where Baidu Netdisk session credentials live."""
    return get_data_dir(settings) / "baidupan_auth.json"


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
