from __future__ import annotations

import unittest
from unittest.mock import patch

from tinker_backend.services.runtime_lifecycle import resolve_runtime_log_path


class RuntimeLogPathTest(unittest.TestCase):
    def test_runtime_log_path_uses_tinker_log_dir(self) -> None:
        with patch.dict("os.environ", {"TINKER_LOG_DIR": "/tmp/tinker-job"}):
            self.assertEqual(
                str(resolve_runtime_log_path("rt_abc123")),
                "/tmp/tinker-job/runtime_rt_abc123.log",
            )

    def test_runtime_log_path_falls_back_to_state_dir(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                str(resolve_runtime_log_path("rt_abc123", state_dir="/tmp/runtime-state")),
                "/tmp/runtime-state/rt_abc123/runtime.log",
            )


if __name__ == "__main__":
    unittest.main()
