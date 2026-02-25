import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    ROCK_LOGGING_PATH: str | None = None
    ROCK_LOGGING_FILE_NAME: str | None = None
    ROCK_LOGGING_LEVEL: str | None = None
    ROCK_SERVICE_STATUS_DIR: str | None = None
    ROCK_SCHEDULER_STATUS_DIR: str | None = None
    ROCK_CONFIG: str | None = None
    ROCK_CONFIG_DIR_NAME: str | None = None
    ROCK_BASE_URL: str | None = "http://localhost:8080"
    ROCK_WORKER_ROCKLET_PORT: int | None = None
    ROCK_SANDBOX_STARTUP_TIMEOUT_SECONDS: int = 180
    ROCK_CODE_SANDBOX_BASE_URL: str | None = None
    ROCK_ENVHUB_BASE_URL: str | None = "http://localhost:8081"
    ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE: str | None = "python:3.11"
    ROCK_ENVHUB_DB_URL: str | None = f"sqlite:///{Path.home() / '.rock' / 'rock_envs.db'}"
    ROCK_DEFAULT_AUTO_CLEAR_TIME_MINUTES: int = 60 * 6  # 6 hours
    ROCK_RAY_NAMESPACE: str | None = "xrl-sandbox"
    ROCK_SANDBOX_EXPIRE_TIME_KEY: str | None = "expire_time"
    ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY: str | None = "auto_clear_time"
    ROCK_TIME_ZONE: str = "Asia/Shanghai"

    # Scheduler
    ROCK_DOCUUM_INSTALL_URL: str | None = None

    # OSS Config
    ROCK_OSS_ENABLE: bool = False
    ROCK_OSS_BUCKET_ENDPOINT: str | None = None
    ROCK_OSS_BUCKET_NAME: str | None = None
    ROCK_OSS_BUCKET_REGION: str | None = None

    ROCK_PIP_INDEX_URL: str | None = "https://mirrors.aliyun.com/pypi/simple/"
    ROCK_MONITOR_ENABLE: bool = False
    ROCK_PROJECT_ROOT: str | None = None
    ROCK_WORKER_ENV_TYPE: str | None = "local"
    ROCK_PYTHON_ENV_PATH: str | None = None
    ROCK_ADMIN_ENV: str | None = "dev"
    ROCK_ADMIN_ROLE: str | None = "write"
    ROCK_CLI_LOAD_PATHS: str = str(Path(__file__).parent / "cli" / "command")
    ROCK_CLI_DEFAULT_CONFIG_PATH: str

    # Model Service Config
    ROCK_MODEL_SERVICE_DATA_DIR: str
    ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE: bool | None = None

    # RuntimeEnv
    ROCK_RTENV_PYTHON_V31114_INSTALL_CMD: str
    ROCK_RTENV_PYTHON_V31212_INSTALL_CMD: str
    ROCK_RTENV_NODE_V22180_INSTALL_CMD: str

    # Agentic
    ROCK_AGENT_PRE_INIT_BASH_CMD_LIST: list[str] = []

    ROCK_AGENT_IFLOW_CLI_INSTALL_CMD: str

    ROCK_MODEL_SERVICE_INSTALL_CMD: str


environment_variables: dict[str, Callable[[], Any]] = {
    "ROCK_LOGGING_PATH": lambda: os.getenv("ROCK_LOGGING_PATH"),
    "ROCK_LOGGING_FILE_NAME": lambda: os.getenv("ROCK_LOGGING_FILE_NAME", "rocklet.log"),
    "ROCK_LOGGING_LEVEL": lambda: os.getenv("ROCK_LOGGING_LEVEL", "INFO"),
    "ROCK_SERVICE_STATUS_DIR": lambda: os.getenv("ROCK_SERVICE_STATUS_DIR", "/data/service_status"),
    "ROCK_SCHEDULER_STATUS_DIR": lambda: os.getenv("ROCK_SCHEDULER_STATUS_DIR", "/data/scheduler_status"),
    "ROCK_CONFIG": lambda: os.getenv("ROCK_CONFIG"),
    "ROCK_CONFIG_DIR_NAME": lambda: os.getenv("ROCK_CONFIG_DIR_NAME", "rock-conf"),
    "ROCK_BASE_URL": lambda: os.getenv("ROCK_BASE_URL", "http://localhost:8080"),
    "ROCK_WORKER_ROCKLET_PORT": lambda: int(val) if (val := os.getenv("ROCK_WORKER_ROCKLET_PORT")) else None,
    "ROCK_SANDBOX_STARTUP_TIMEOUT_SECONDS": lambda: int(os.getenv("ROCK_SANDBOX_STARTUP_TIMEOUT_SECONDS", "180")),
    "ROCK_CODE_SANDBOX_BASE_URL": lambda: os.getenv("ROCK_CODE_SANDBOX_BASE_URL", ""),
    "ROCK_ENVHUB_BASE_URL": lambda: os.getenv("ROCK_ENVHUB_BASE_URL", "http://localhost:8081"),
    "ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE": lambda: os.getenv("ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE", "python:3.11"),
    "ROCK_ENVHUB_DB_URL": lambda: os.getenv(
        "ROCK_ENVHUB_DB_URL", f"sqlite:///{Path.home() / '.rock' / 'rock_envs.db'}"
    ),
    "ROCK_DEFAULT_AUTO_CLEAR_TIME_MINUTES": lambda: int(os.getenv("ROCK_DEFAULT_AUTO_CLEAR_TIME_MINUTES", "360")),
    "ROCK_RAY_NAMESPACE": lambda: os.getenv("ROCK_RAY_NAMESPACE", "xrl-sandbox"),
    "ROCK_SANDBOX_EXPIRE_TIME_KEY": lambda: os.getenv("ROCK_SANDBOX_EXPIRE_TIME_KEY", "expire_time"),
    "ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY": lambda: os.getenv("ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY", "auto_clear_time"),
    "ROCK_OSS_ENABLE": lambda: os.getenv("ROCK_OSS_ENABLE", "false").lower() == "true",
    "ROCK_OSS_BUCKET_ENDPOINT": lambda: os.getenv("ROCK_OSS_BUCKET_ENDPOINT"),
    "ROCK_OSS_BUCKET_NAME": lambda: os.getenv("ROCK_OSS_BUCKET_NAME"),
    "ROCK_OSS_BUCKET_REGION": lambda: os.getenv("ROCK_OSS_BUCKET_REGION"),
    "ROCK_PIP_INDEX_URL": lambda: os.getenv("ROCK_PIP_INDEX_URL", "https://mirrors.aliyun.com/pypi/simple/"),
    "ROCK_MONITOR_ENABLE": lambda: os.getenv("ROCK_MONITOR_ENABLE", "false").lower() == "true",
    "ROCK_PROJECT_ROOT": lambda: os.getenv("ROCK_PROJECT_ROOT", str(Path(__file__).resolve().parents[1])),
    "ROCK_WORKER_ENV_TYPE": lambda: os.getenv("ROCK_WORKER_ENV_TYPE", "local"),
    "ROCK_PYTHON_ENV_PATH": lambda: os.getenv("ROCK_PYTHON_ENV_PATH", sys.base_prefix),
    "ROCK_ADMIN_ENV": lambda: os.getenv("ROCK_ADMIN_ENV", "dev"),
    "ROCK_ADMIN_ROLE": lambda: os.getenv("ROCK_ADMIN_ROLE", "write"),
    "ROCK_CLI_LOAD_PATHS": lambda: os.getenv("ROCK_CLI_LOAD_PATHS", str(Path(__file__).parent / "cli" / "command")),
    "ROCK_CLI_DEFAULT_CONFIG_PATH": lambda: os.getenv(
        "ROCK_CLI_DEFAULT_CONFIG_PATH", Path.home() / ".rock" / "config.ini"
    ),
    "ROCK_MODEL_SERVICE_DATA_DIR": lambda: os.getenv("ROCK_MODEL_SERVICE_DATA_DIR", "/data/logs"),
    "ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE": lambda: os.getenv("ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE", "false").lower()
    == "true",
    "ROCK_RTENV_PYTHON_V31114_INSTALL_CMD": lambda: os.getenv(
        "ROCK_RTENV_PYTHON_V31114_INSTALL_CMD",
        "[ -f cpython31114.tar.gz ] && rm cpython31114.tar.gz; [ -d python ] && rm -rf python; wget -q -O cpython31114.tar.gz https://github.com/astral-sh/python-build-standalone/releases/download/20251120/cpython-3.11.14+20251120-x86_64-unknown-linux-gnu-install_only.tar.gz && tar -xzf cpython31114.tar.gz && mv python runtime-env",
    ),
    "ROCK_RTENV_PYTHON_V31212_INSTALL_CMD": lambda: os.getenv(
        "ROCK_RTENV_PYTHON_V31212_INSTALL_CMD",
        "[ -f cpython-31212.tar.gz ] && rm cpython-31212.tar.gz; [ -d python ] && rm -rf python; wget -q -O cpython-31212.tar.gz https://github.com/astral-sh/python-build-standalone/releases/download/20251217/cpython-3.12.12+20251217-x86_64-unknown-linux-gnu-install_only.tar.gz && tar -xzf cpython-31212.tar.gz && mv python runtime-env",
    ),
    "ROCK_RTENV_NODE_V22180_INSTALL_CMD": lambda: os.getenv(
        "ROCK_RTENV_NODE_V22180_INSTALL_CMD",
        "[ -f node.tar.xz ] && rm node.tar.xz; [ -d node ] && rm -rf node; wget -q -O node.tar.xz --tries=10 --waitretry=2 https://nodejs.org/dist/v22.18.0/node-v22.18.0-linux-x64.tar.xz && tar -xf node.tar.xz && mv node-v22.18.0-linux-x64 runtime-env",
    ),
    "ROCK_AGENT_PRE_INIT_BASH_CMD_LIST": lambda: json.loads(os.getenv("ROCK_AGENT_PRE_INIT_BASH_CMD_LIST", "[]")),
    "ROCK_AGENT_IFLOW_CLI_INSTALL_CMD": lambda: os.getenv(
        "ROCK_AGENT_IFLOW_CLI_INSTALL_CMD",
        "npm i -g @iflow-ai/iflow-cli@latest",
    ),
    "ROCK_MODEL_SERVICE_INSTALL_CMD": lambda: os.getenv(
        "ROCK_MODEL_SERVICE_INSTALL_CMD",
        "pip install rl_rock[model-service]",
    ),
    "ROCK_TIME_ZONE": lambda: os.getenv("ROCK_TIME_ZONE", "Asia/Shanghai"),
    "ROCK_DOCUUM_INSTALL_URL": lambda: os.getenv(
        "ROCK_DOCUUM_INSTALL_URL", "https://raw.githubusercontent.com/stepchowfun/docuum/main/install.sh"
    ),
}


def __getattr__(name: str):
    if name in environment_variables:
        return environment_variables[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def is_set(name: str):
    """Check if an environment variable is explicitly set."""
    if name in environment_variables:
        return name in os.environ
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
