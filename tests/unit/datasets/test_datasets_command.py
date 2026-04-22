import argparse
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.cli.command.datasets import DatasetsCommand
from rock.sdk.bench.models.job.config import OssRegistryInfo
from rock.sdk.envhub.datasets.models import DatasetSpec


def make_base_args(**kwargs):
    args = argparse.Namespace(
        config=None,
        datasets_command=None,
        bucket=None,
        endpoint=None,
        access_key_id=None,
        access_key_secret=None,
        region=None,
        org=None,
        dataset=None,
        split=None,
        offset=0,
        limit=None,
    )
    for k, v in kwargs.items():
        setattr(args, k, v)
    return args

def make_registry_info():
    return OssRegistryInfo(oss_bucket="b", oss_access_key_id="k", oss_access_key_secret="s")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rock")
    subparsers = parser.add_subparsers(dest="command")
    asyncio.run(DatasetsCommand.add_parser_to(subparsers))
    return parser


def test_command_name():
    assert DatasetsCommand.name == "datasets"


def test_build_oss_registry_info_from_cli_args():
    cmd = DatasetsCommand()
    args = make_base_args(bucket="cli-bucket", endpoint="https://oss.example.com", access_key_id="kid", access_key_secret="ksec")

    with patch("rock.cli.command.datasets.ConfigManager") as mock_mgr:
        ds_cfg = mock_mgr.return_value.get_config.return_value.dataset_config
        ds_cfg.oss_bucket = None
        ds_cfg.oss_endpoint = None
        ds_cfg.oss_access_key_id = None
        ds_cfg.oss_access_key_secret = None
        ds_cfg.oss_region = None
        info = cmd._build_oss_registry_info(args)

    assert info.oss_bucket == "cli-bucket"
    assert info.oss_endpoint == "https://oss.example.com"
    assert info.oss_access_key_id == "kid"


def test_build_oss_registry_info_cli_overrides_ini():
    cmd = DatasetsCommand()
    args = make_base_args(bucket="cli-bucket", endpoint=None, access_key_id=None, access_key_secret=None)

    with patch("rock.cli.command.datasets.ConfigManager") as mock_mgr:
        ds_cfg = mock_mgr.return_value.get_config.return_value.dataset_config
        ds_cfg.oss_bucket = "ini-bucket"
        ds_cfg.oss_endpoint = "https://ini.example.com"
        ds_cfg.oss_access_key_id = "ini-kid"
        ds_cfg.oss_access_key_secret = "ini-ksec"
        ds_cfg.oss_region = None
        info = cmd._build_oss_registry_info(args)

    assert info.oss_bucket == "cli-bucket"
    assert info.oss_endpoint == "https://ini.example.com"
    assert info.oss_access_key_id == "ini-kid"


def test_build_oss_registry_info_raises_when_bucket_missing():
    cmd = DatasetsCommand()
    args = make_base_args(bucket=None)

    with patch("rock.cli.command.datasets.ConfigManager") as mock_mgr:
        ds_cfg = mock_mgr.return_value.get_config.return_value.dataset_config
        ds_cfg.oss_bucket = None
        ds_cfg.oss_endpoint = None
        ds_cfg.oss_access_key_id = None
        ds_cfg.oss_access_key_secret = None
        ds_cfg.oss_region = None

        with pytest.raises(ValueError, match="bucket"):
            cmd._build_oss_registry_info(args)


def test_tasks_parser_defaults_split_offset_limit():
    parser = _build_parser()
    ns = parser.parse_args(["datasets", "tasks", "--org", "qwen", "--dataset", "my-bench"])

    assert ns.command == "datasets"
    assert ns.datasets_command == "tasks"
    assert ns.org == "qwen"
    assert ns.dataset == "my-bench"
    assert ns.split == "test"
    assert ns.offset == 0
    assert ns.limit is None


@pytest.mark.parametrize(
    "argv",
    [
        ["datasets", "tasks", "--dataset", "my-bench"],
        ["datasets", "tasks", "--org", "qwen"],
    ],
)
def test_tasks_parser_requires_org_and_dataset(argv):
    parser = _build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(argv)

    assert excinfo.value.code == 2


def test_tasks_parser_rejects_negative_offset():
    parser = _build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["datasets", "tasks", "--org", "qwen", "--dataset", "my-bench", "--offset", "-1"])

    assert excinfo.value.code == 2


def test_tasks_parser_rejects_non_positive_limit():
    parser = _build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["datasets", "tasks", "--org", "qwen", "--dataset", "my-bench", "--limit", "0"])

    assert excinfo.value.code == 2


def test_arun_dispatches_tasks():
    cmd = DatasetsCommand()
    args = make_base_args(datasets_command="tasks", org="qwen", dataset="my-bench", split="test")

    with patch.object(DatasetsCommand, "_tasks", new_callable=AsyncMock, create=True) as mock_tasks:
        asyncio.run(cmd.arun(args))

    mock_tasks.assert_awaited_once_with(args)


def test_tasks_outputs_paginated_results(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(
        datasets_command="tasks",
        org="qwen",
        dataset="my-bench",
        split="test",
        offset=1,
        limit=2,
    )
    mock_client = MagicMock()
    mock_client.list_dataset_tasks.return_value = DatasetSpec(
        id="qwen/my-bench",
        split="test",
        task_ids=["task-001", "task-002", "task-003"],
    )

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._tasks(args))

    mock_client.list_dataset_tasks.assert_called_once_with("qwen", "my-bench", "test")
    out = capsys.readouterr().out
    assert "Dataset: qwen/my-bench" in out
    assert "Split: test" in out
    assert "task-002" in out
    assert "task-003" in out
    assert "task-001" not in out
    assert "Total: 3" in out
    assert "Shown: 2" in out
    assert "#Task name" in out


def test_tasks_prints_no_tasks_message_when_not_found(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(
        datasets_command="tasks",
        org="qwen",
        dataset="my-bench",
        split="test",
        offset=0,
        limit=None,
    )
    mock_client = MagicMock()
    mock_client.list_dataset_tasks.return_value = None

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._tasks(args))

    out = capsys.readouterr().out
    assert "No tasks found" in out
