from __future__ import annotations

import pytest

from tinker.server_job.config_types import (
    LocalServerJobConfig,
    load_server_job_config,
)


def test_load_local_server_job_config_from_runtime_yaml(tmp_path) -> None:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        """
tinker_backend:
  platform: local
  backend_config:
    repo_path: /root/tinker-backend
    port: 19000
    host: 0.0.0.0
    client_host: 127.0.0.1
    startup_timeout: 3
runtime:
  workdir: /root/ROLL
""".lstrip(),
        encoding="utf-8",
    )

    config = load_server_job_config(config_path)

    assert isinstance(config, LocalServerJobConfig)
    assert config.platform == "local"
    assert config.repo_path == "/root/tinker-backend"
    assert config.port == 19000
    assert config.host == "0.0.0.0"
    assert config.client_host == "127.0.0.1"
    assert config.startup_timeout == 3


def test_unknown_server_job_platform_fails(tmp_path) -> None:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        """
tinker_backend:
  platform: unsupported
  backend_config: {}
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(Exception):
        load_server_job_config(config_path)


def test_local_server_job_config_rejects_reuse_existing(tmp_path) -> None:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        """
tinker_backend:
  platform: local
  backend_config:
    reuse_existing: true
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(Exception) as exc_info:
        load_server_job_config(config_path)

    assert "reuse_existing" in str(exc_info.value)
