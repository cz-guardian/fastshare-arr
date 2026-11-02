"""Configuration, read from environment variables (Docker-friendly)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


@dataclass
class Config:
    host: str
    port: int
    api_key: str
    config_dir: Path
    incomplete_dir: Path
    complete_dir: Path
    categories: list
    max_active_jobs: int
    extract: bool
    delete_archives: bool
    fs_user: str
    fs_pass: str
    fs_api_base: str
    fs_adult: bool
    retries: int
    user_agent: str
    speed_limit_kbps: int
    debug: bool


def load() -> Config:
    config_dir = Path(_env("CONFIG_DIR", "/config"))
    config_dir.mkdir(parents=True, exist_ok=True)

    api_key = _env("API_KEY")
    if not api_key:
        key_file = config_dir / "api_key"
        if key_file.exists():
            api_key = key_file.read_text().strip()
        else:
            api_key = secrets.token_hex(16)
            key_file.write_text(api_key)

    cats = [c.strip() for c in _env("CATEGORIES", "tv,movies,music").split(",") if c.strip()]

    return Config(
        host=_env("HOST", "0.0.0.0"),
        port=_int("PORT", 8085),
        api_key=api_key,
        config_dir=config_dir,
        incomplete_dir=Path(_env("INCOMPLETE_DIR", "/downloads/incomplete")),
        complete_dir=Path(_env("COMPLETE_DIR", "/downloads/complete")),
        categories=cats,
        max_active_jobs=max(1, _int("MAX_ACTIVE_JOBS", 1)),
        extract=_bool("EXTRACT", True),
        delete_archives=_bool("DELETE_ARCHIVES", True),
        fs_user=_env("FASTSHARE_USER"),
        fs_pass=os.environ.get("FASTSHARE_PASS", ""),
        fs_api_base=_env("FASTSHARE_API_BASE", "https://fastshare.cz").rstrip("/"),
        fs_adult=_bool("FASTSHARE_ADULT", False),
        retries=_int("RETRIES", 4),
        user_agent=_env(
            "USER_AGENT",
            "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        ),
        speed_limit_kbps=_int("SPEED_LIMIT_KBPS", 0),
        debug=_bool("DEBUG", False),
    )
