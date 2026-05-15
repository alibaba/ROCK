"""Tests for rock.cli.command.storage — StorageCommand CLI."""

import argparse
from unittest.mock import AsyncMock, patch

import pytest

from rock.cli.command.storage import StorageCommand


@pytest.fixture
def storage_cmd():
    return StorageCommand()


class TestStorageGet:
    @pytest.mark.asyncio
    async def test_get_success(self, storage_cmd, capsys):
        """Successful download prints OK and extraction hint."""
        args = argparse.Namespace(
            storage_action="get",
            sandbox_id="sandbox-abc123",
            out="./downloads",
        )
        with patch("rock.cli.command.storage.OssArchiver") as mock_archiver:
            mock_archiver.build_sandbox_log_key.return_value = "rock-archives/sandbox-logs/sandbox-abc123.tar.gz"
            mock_archiver.get_object = AsyncMock(return_value=True)

            await storage_cmd.arun(args)

            captured = capsys.readouterr()
            assert "OK:" in captured.out
            assert "sandbox-abc123.tar.gz" in captured.out
            assert "tar -xzf" in captured.out

    @pytest.mark.asyncio
    async def test_get_failure(self, storage_cmd, capsys):
        """Failed download prints FAILED."""
        args = argparse.Namespace(
            storage_action="get",
            sandbox_id="sandbox-xyz",
            out=".",
        )
        with patch("rock.cli.command.storage.OssArchiver") as mock_archiver:
            mock_archiver.build_sandbox_log_key.return_value = "rock-archives/sandbox-logs/sandbox-xyz.tar.gz"
            mock_archiver.get_object = AsyncMock(return_value=False)

            await storage_cmd.arun(args)

            captured = capsys.readouterr()
            assert "FAILED" in captured.out

    @pytest.mark.asyncio
    async def test_unknown_action_raises(self, storage_cmd):
        """Unknown storage_action raises ValueError."""
        args = argparse.Namespace(storage_action="delete")
        with pytest.raises(ValueError, match="Unknown storage action"):
            await storage_cmd.arun(args)

    @pytest.mark.asyncio
    async def test_missing_action_raises(self, storage_cmd):
        """No storage_action raises ValueError."""
        args = argparse.Namespace(storage_action=None)
        with pytest.raises(ValueError, match="storage action is required"):
            await storage_cmd.arun(args)
