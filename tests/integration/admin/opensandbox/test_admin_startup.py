import sys

from fastapi.testclient import TestClient


def test_admin_starts_with_opensandbox_and_skips_worker_scheduler(tmp_path, monkeypatch):
    config_path = tmp_path / "rock-opensandbox.yml"
    config_path.write_text(
        f"""
runtime:
  operator_type: opensandbox
  python_env_path: {sys.prefix}
  envhub_db_url: sqlite:////tmp/rock-opensandbox-envhub.db
opensandbox:
  endpoint: opensandbox.example.com
aes_encrypt_key: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
scheduler:
  enabled: true
""".lstrip()
    )
    monkeypatch.setenv("ROCK_ADMIN_ENV", "test")
    monkeypatch.setenv("ROCK_ADMIN_ROLE", "admin")
    monkeypatch.setenv("ROCK_CONFIG", str(config_path))

    from rock.admin import main

    def fail_if_called(*args, **kwargs):
        raise AssertionError("OpenSandbox admin must not initialize Ray or the worker scheduler")

    monkeypatch.setattr(main, "RayService", fail_if_called)
    monkeypatch.setattr(main, "SchedulerThread", fail_if_called)
    monkeypatch.setattr(main, "_init_scheduler_metrics", fail_if_called)
    monkeypatch.setattr(main, "set_archive_sandbox_table_provider", lambda provider: None)
    monkeypatch.setattr(main, "set_archive_rock_config_provider", lambda provider: None)
    monkeypatch.setattr(main, "set_archive_main_loop_provider", lambda provider: None)

    with TestClient(main.create_app()) as client:
        response = client.get("/")

    assert response.status_code == 200
