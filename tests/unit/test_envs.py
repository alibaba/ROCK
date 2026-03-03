import os

from rock import env_vars


def test_default_envs():
    log_dir = "/data/log"
    env_vars.ROCK_LOGGING_PATH = log_dir
    assert log_dir == env_vars.ROCK_LOGGING_PATH


def test_envs_project_root():
    project_root = env_vars.ROCK_PROJECT_ROOT
    assert project_root is not None


def test_service_status_dir_default():
    """ROCK_SERVICE_STATUS_DIR 默认值应为 /tmp"""
    # 清除可能已设置的环境变量和模块属性缓存
    original_env = os.environ.pop("ROCK_SERVICE_STATUS_DIR", None)
    # Delete the cached attribute so __getattr__ re-evaluates the default
    try:
        delattr(env_vars, "ROCK_SERVICE_STATUS_DIR")
    except AttributeError:
        pass
    try:
        status_dir = env_vars.ROCK_SERVICE_STATUS_DIR
        assert status_dir == "/tmp", f"Expected /tmp, got {status_dir}"
    finally:
        if original_env is not None:
            os.environ["ROCK_SERVICE_STATUS_DIR"] = original_env
            env_vars.ROCK_SERVICE_STATUS_DIR = original_env
