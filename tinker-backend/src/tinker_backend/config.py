from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def find_repo_dotenv(start: str | Path) -> Path | None:
    """Find the backend repository .env without relying on process cwd."""

    path = Path(start).resolve()
    current = path.parent if path.is_file() else path
    for parent in (current, *current.parents):
        dotenv_path = parent / ".env"
        if dotenv_path.exists() and (parent / "pyproject.toml").exists():
            return dotenv_path
    return None


def load_repo_dotenv(start: str | Path = __file__) -> Path | None:
    """Load repo-local .env into os.environ as early process defaults."""

    dotenv_path = find_repo_dotenv(start)
    if dotenv_path is None:
        return None
    load_dotenv(dotenv_path, override=False)
    return dotenv_path


load_repo_dotenv()


def _secret_configured(value: SecretStr | None) -> bool:
    return value is not None and bool(value.get_secret_value())


class Settings(BaseSettings):
    """Runtime settings loaded from environment or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "dev"
    log_level: str = "INFO"
    tinker_platform: Literal["local"] = "local"
    public_base_url: str | None = None
    host: str = "0.0.0.0"
    port: int = 9000

    database_url: SecretStr | None = None
    postgres_host: str | None = None
    postgres_port: int = 5432
    postgres_database: str | None = None
    postgres_user: str | None = None
    postgres_password: SecretStr | None = None
    sql_echo: bool = False

    tinker_internal_auth_token: SecretStr | None = None
    runtime_allowed_workdirs: str = Field(default="")
    runtime_state_dir: str = "/var/lib/tinker-backend/runtimes"
    oss_access_key_id: SecretStr | None = None
    oss_access_key_secret: SecretStr | None = None
    oss_region: str | None = None
    oss_endpoint: str | None = None
    oss_bucket: str | None = None
    oss_dataset_path: str | None = None

    def oss_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if _secret_configured(self.oss_access_key_id):
            env["OSS_ACCESS_KEY_ID"] = self.oss_access_key_id.get_secret_value()
        if _secret_configured(self.oss_access_key_secret):
            env["OSS_ACCESS_KEY_SECRET"] = self.oss_access_key_secret.get_secret_value()
        for name, value in {
            "OSS_REGION": self.oss_region,
            "OSS_ENDPOINT": self.oss_endpoint,
            "OSS_BUCKET": self.oss_bucket,
            "OSS_DATASET_PATH": self.oss_dataset_path,
        }.items():
            if value:
                env[name] = str(value)
        return env

    def readiness_checks(self) -> dict[str, bool]:
        checks = {
            "database_configured": True,
        }
        return checks

    def redacted_view(self) -> dict[str, Any]:
        return {
            "app": {
                "env": self.app_env,
                "log_level": self.log_level,
                "platform": self.tinker_platform,
                "public_base_url": self.public_base_url,
                "host": self.host,
                "port": self.port,
            },
            "database": {
                "database_url_configured": _secret_configured(self.database_url),
                "legacy_postgres_configured": bool(
                    self.postgres_host
                    and self.postgres_database
                    and self.postgres_user
                    and _secret_configured(self.postgres_password)
                ),
                "sql_echo": self.sql_echo,
            },
            "security": {
                "internal_auth_configured": _secret_configured(self.tinker_internal_auth_token),
            },
            "runtime": {
                "allowed_workdirs_configured": bool(self.runtime_allowed_workdirs.strip()),
                "state_dir": self.runtime_state_dir,
            },
            "rock": {
                "oss_access_key_id_configured": _secret_configured(self.oss_access_key_id),
                "oss_access_key_secret_configured": _secret_configured(self.oss_access_key_secret),
                "oss_region": self.oss_region,
                "oss_endpoint": self.oss_endpoint,
                "oss_bucket": self.oss_bucket,
                "oss_dataset_path_configured": bool(self.oss_dataset_path),
            },
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
