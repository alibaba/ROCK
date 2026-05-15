import logging
from unittest.mock import patch

import pytest

from rock.utils.concurrent_helper import StageTimer


class TestStageTimer:
    def test_logs_duration_on_exit(self, caplog):
        with caplog.at_level(logging.INFO):
            logger = logging.getLogger("test_stage_timer")
            with StageTimer("startup_timing", "[sandbox-abc] Check availability", logger):
                pass

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "[startup_timing]" in record.message
        assert "[sandbox-abc] Check availability" in record.message
        assert "took" in record.message
        assert "s" in record.message

    @patch("rock.utils.concurrent_helper.time.perf_counter", side_effect=[100.0, 102.5])
    def test_duration_calculation(self, mock_perf_counter, caplog):
        with caplog.at_level(logging.INFO):
            logger = logging.getLogger("test_stage_timer")
            with StageTimer("startup_timing", "[sandbox-abc] Image pull", logger):
                pass

        assert "took 2.500 s" in caplog.records[0].message

    def test_exception_still_logs(self, caplog):
        with caplog.at_level(logging.INFO):
            logger = logging.getLogger("test_stage_timer")
            with pytest.raises(ValueError, match="boom"):
                with StageTimer("startup_timing", "[sandbox-abc] Docker run", logger):
                    raise ValueError("boom")

        assert len(caplog.records) == 1
        assert "[startup_timing]" in caplog.records[0].message
        assert "[sandbox-abc] Docker run" in caplog.records[0].message

    def test_log_format(self, caplog):
        with caplog.at_level(logging.INFO):
            logger = logging.getLogger("test_stage_timer")
            with StageTimer("my_phase", "my description", logger):
                pass

        record = caplog.records[0]
        assert record.message.startswith("[my_phase] my description took ")
        assert record.message.endswith(" s")
