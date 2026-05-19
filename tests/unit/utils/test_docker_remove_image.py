"""Regression tests for DockerUtil.remove_image cls bug fix."""

import subprocess
from unittest.mock import patch

import pytest

from rock.utils.docker import DockerUtil


class TestRemoveImageClsFix:
    """Regression: remove_image previously had `@classmethod` without `cls`,
    so the first positional arg was swallowed as `cls` and the actual image
    name was lost."""

    def test_remove_image_passes_image_to_subprocess(self):
        with patch("subprocess.check_output", return_value=b"") as mock_run:
            DockerUtil.remove_image("nginx:latest")
        cmd = mock_run.call_args.args[0]
        assert cmd == ["docker", "rmi", "nginx:latest"]

    def test_remove_image_propagates_error(self):
        with patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "docker"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                DockerUtil.remove_image("nginx:latest")
