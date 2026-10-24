"""Configuration read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))  # type: ignore[arg-type]
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("MP_DATA_DIR", "./data")))
    # Key used to encrypt credentials, SSH keys and notification channel settings.
    secret_key: str | None = field(default_factory=lambda: _env("MP_SECRET_KEY"))
    admin_user: str = field(default_factory=lambda: _env("MP_ADMIN_USER", "admin"))
    admin_password: str | None = field(default_factory=lambda: _env("MP_ADMIN_PASSWORD"))
    poll_interval: int = field(default_factory=lambda: _int("MP_POLL_INTERVAL", 30))
    retention_days: int = field(default_factory=lambda: _int("MP_RETENTION_DAYS", 14))
    slow_op_seconds: int = field(default_factory=lambda: _int("MP_SLOW_OP_SECONDS", 5))
    poll_concurrency: int = field(default_factory=lambda: _int("MP_POLL_CONCURRENCY", 8))
    public_url: str = field(default_factory=lambda: _env("MP_PUBLIC_URL", "http://localhost:8080"))
    # SMTP for email notifications
    smtp_host: str | None = field(default_factory=lambda: _env("MP_SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: _int("MP_SMTP_PORT", 587))
    smtp_user: str | None = field(default_factory=lambda: _env("MP_SMTP_USER"))
    smtp_password: str | None = field(default_factory=lambda: _env("MP_SMTP_PASSWORD"))
    smtp_from: str = field(default_factory=lambda: _env("MP_SMTP_FROM", "mongopilot@localhost"))
    smtp_starttls: bool = field(default_factory=lambda: _env("MP_SMTP_STARTTLS", "true").lower() == "true")
    # MongoDB downloads: release index and optional mirror for isolated networks
    mongodb_release_index: str = field(
        default_factory=lambda: _env("MP_MONGODB_RELEASE_INDEX", "https://downloads.mongodb.org/full.json")
    )
    mongodb_mirror: str | None = field(default_factory=lambda: _env("MP_MONGODB_MIRROR"))
    ssh_binary: str = field(default_factory=lambda: _env("MP_SSH_BINARY", "ssh"))
    # MongoDB tools used by the Tools pages (installed in the Docker image)
    mongosh_binary: str = field(default_factory=lambda: _env("MP_MONGOSH", "mongosh"))
    mongodump_binary: str = field(default_factory=lambda: _env("MP_MONGODUMP", "mongodump"))
    mongoexport_binary: str = field(default_factory=lambda: _env("MP_MONGOEXPORT", "mongoexport"))
    mongorestore_binary: str = field(default_factory=lambda: _env("MP_MONGORESTORE", "mongorestore"))
    mongoimport_binary: str = field(default_factory=lambda: _env("MP_MONGOIMPORT", "mongoimport"))
    max_upload_mb: int = field(default_factory=lambda: _int("MP_MAX_UPLOAD_MB", 20480))
    schema_max_sample: int = field(default_factory=lambda: _int("MP_SCHEMA_MAX_SAMPLE", 10000))
    # Demo mode: simulated clusters and servers, no real connections
    demo: bool = field(default_factory=lambda: _env("MP_DEMO", "false").lower() == "true")
    disable_background: bool = field(default_factory=lambda: _env("MP_DISABLE_BACKGROUND", "false") == "true")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mongopilot.sqlite3"


settings = Settings()
