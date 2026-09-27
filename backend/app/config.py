"""Settings loaded from backend/.env (or real environment variables).

API keys can be given directly (NEBIUS_API_KEY=...) or, preferably, as a path to a file
holding only the key (NEBIUS_API_KEY_FILE=~/.config/info_checker/key_nebius.txt). "~" is
expanded; other relative paths resolve against the project root. Key values never appear
in logs or error messages.
"""

import logging
import os
import stat
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import PrivateAttr, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent

KeyName = Literal["nebius_api_key", "tavily_api_key", "google_factcheck_api_key"]
KEY_NAMES: tuple[KeyName, ...] = ("nebius_api_key", "tavily_api_key", "google_factcheck_api_key")


class ConfigError(Exception):
    """Invalid configuration. Messages name settings and paths, never key values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    # Use get_key("nebius_api_key") etc. rather than reading these fields directly.
    nebius_api_key: SecretStr | None = None
    nebius_api_key_file: Path | None = None
    tavily_api_key: SecretStr | None = None
    tavily_api_key_file: Path | None = None
    google_factcheck_api_key: SecretStr | None = None
    google_factcheck_api_key_file: Path | None = None

    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    extraction_model: str = ""
    verification_model: str = ""
    llm_temperature: float = 0.1

    # Comma-separated, e.g. "chrome-extension://abcdef...,http://localhost:3000"
    allowed_origins: str = ""
    max_claims: int = 8
    max_text_chars: int = 10_000
    max_concurrency: int = 4

    # Time limits in seconds. The extension gives up at 90s, so the whole request must
    # finish before that; claims still running at the budget come back "unverifiable".
    extraction_timeout_s: float = 30
    claim_timeout_s: float = 45
    request_budget_s: float = 80

    log_level: str = "INFO"  # DEBUG logs each claim's evidence list and raw verdict

    sources_file: Path = BACKEND_DIR / "sources.yaml"
    cache_db: Path = BACKEND_DIR / "cache.db"
    cache_ttl_hours: int = 24

    _keys: dict[str, str | None] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _no_conflicting_keys(self) -> "Settings":
        for name in KEY_NAMES:
            if _is_set(getattr(self, name)) and getattr(self, f"{name}_file"):
                env = name.upper()
                # Not a ValueError, so pydantic won't wrap it and echo input values.
                raise ConfigError(f"Both {env} and {env}_FILE are set. Set only one of them.")
        return self

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    def get_key(self, name: KeyName) -> str | None:
        """Return the key value, reading *_FILE on first use. None if neither is set."""
        if name not in self._keys:
            self._keys[name] = self._resolve_key(name)
        return self._keys[name]

    def require_key(self, name: KeyName) -> str:
        key = self.get_key(name)
        if not key:
            env = name.upper()
            raise ConfigError(f"{env} is not configured. Set {env}_FILE (or {env}) in backend/.env.")
        return key

    def _resolve_key(self, name: KeyName) -> str | None:
        plain: SecretStr | None = getattr(self, name)
        if _is_set(plain):
            return plain.get_secret_value().strip()

        file_setting: Path | None = getattr(self, f"{name}_file")
        if not file_setting:
            return None
        env = f"{name.upper()}_FILE"
        path = file_setting.expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise ConfigError(f"{env} points to {path}, but that file does not exist.") from None
        except OSError as e:
            raise ConfigError(f"{env}: cannot read {path} ({e.strerror}).") from None
        if not value:
            raise ConfigError(f"{env} points to {path}, but that file is empty.")
        _warn_if_readable_by_others(env, path)
        return value


def _is_set(secret: SecretStr | None) -> bool:
    return secret is not None and bool(secret.get_secret_value().strip())


def _warn_if_readable_by_others(env: str, path: Path) -> None:
    if os.name != "posix":
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        log.warning(
            "%s: key file %s is readable by group or others (mode %s). Run: chmod 600 %s",
            env, path, oct(mode & 0o777), path,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
