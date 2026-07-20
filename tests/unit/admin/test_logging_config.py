import pytest

import rock.logger as logger_module
from rock.admin import main as admin_main
from rock.config import LoggingConfig, RockConfig


@pytest.mark.parametrize("enabled", [True, False])
def test_apply_logging_config_uses_yaml_value(monkeypatch, enabled):
    monkeypatch.delenv("ROCK_LOGGING_EXCEPTION_TRACEBACK_ENABLE", raising=False)
    logger_module.configure_logging(exception_traceback_enabled=not enabled)
    rock_config = RockConfig(logging=LoggingConfig(exception_traceback_enabled=enabled))

    try:
        admin_main._apply_logging_config(rock_config)

        assert logger_module.is_exception_traceback_enabled() is enabled
    finally:
        logger_module.configure_logging(exception_traceback_enabled=True)
