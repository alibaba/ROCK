import argparse
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.cli.command.datasets import DatasetsCommand
from rock.sdk.bench.models.job.config import OssRegistryInfo
from rock.sdk.envhub.datasets.models import DatasetSpec, TaskFile, UploadResult


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
        depth=2,
        offset=0,
        limit=None,
        output=None,
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
    args = make_base_args(
        bucket="cli-bucket", endpoint="https://oss.example.com", access_key_id="kid", access_key_secret="ksec"
    )

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
    assert ns.output is None


@pytest.mark.parametrize(
    "flag",
    ["-o", "--output", "--ouput"],
)
def test_datasets_subcommands_accept_json_output(flag):
    parser = _build_parser()

    ns = parser.parse_args(["datasets", "tasks", "--org", "qwen", "--dataset", "my-bench", flag, "json"])

    assert ns.output == "json"


def test_datasets_parser_accepts_json_output_before_subcommand():
    parser = _build_parser()

    ns = parser.parse_args(["datasets", "-o", "json", "list"])

    assert ns.output == "json"


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


def test_arun_dispatches_fs_aliases():
    for command in ("fs", "files"):
        cmd = DatasetsCommand()
        args = make_base_args(datasets_command=command, fs_command="ls", org="qwen", dataset="my-bench", task="task-001")

        with patch.object(DatasetsCommand, "_fs", new_callable=AsyncMock, create=True) as mock_fs:
            asyncio.run(cmd.arun(args))

        mock_fs.assert_awaited_once_with(args)


@pytest.mark.parametrize("command", ["fs", "files"])
def test_fs_parser_accepts_ls_get_download(command):
    parser = _build_parser()

    ls_ns = parser.parse_args(
        ["datasets", command, "ls", "--org", "qwen", "--dataset", "my-bench", "--task", "task-001"]
    )
    get_ns = parser.parse_args(
        [
            "datasets",
            command,
            "get",
            "--org",
            "qwen",
            "--dataset",
            "my-bench",
            "--task",
            "task-001",
            "--path",
            "tests/input.json",
        ]
    )
    download_ns = parser.parse_args(
        [
            "datasets",
            command,
            "download",
            "--org",
            "qwen",
            "--dataset",
            "my-bench",
            "--task",
            "task-001",
            "--path",
            "tests/",
            "--dest",
            "./tests",
        ]
    )

    assert ls_ns.datasets_command == command
    assert ls_ns.fs_command == "ls"
    assert ls_ns.split == "test"
    assert ls_ns.path == ""
    assert get_ns.fs_command == "get"
    assert get_ns.path == "tests/input.json"
    assert download_ns.fs_command == "download"
    assert download_ns.dest == "./tests"


def test_fs_ls_outputs_recursive_task_files(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(
        datasets_command="fs",
        fs_command="ls",
        org="qwen",
        dataset="my-bench",
        split="test",
        task="task-001",
        path="tests",
    )
    mock_client = MagicMock()
    mock_client.list_task_files.return_value = [
        TaskFile(path="tests/test_api.py", size=10),
        TaskFile(path="tests/fixtures/input.json", size=20),
    ]

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._fs(args))

    mock_client.list_task_files.assert_called_once_with("qwen", "my-bench", "test", "task-001", "tests")
    out = capsys.readouterr().out
    assert "tests/test_api.py" in out
    assert "tests/fixtures/input.json" in out


def test_fs_ls_outputs_json(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(
        datasets_command="fs",
        fs_command="ls",
        org="qwen",
        dataset="my-bench",
        split="test",
        task="task-001",
        path="",
        output="json",
    )
    mock_client = MagicMock()
    mock_client.list_task_files.return_value = [TaskFile(path="task.yaml", size=42)]

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._fs(args))

    assert json.loads(capsys.readouterr().out) == {
        "dataset": "qwen/my-bench",
        "split": "test",
        "task": "task-001",
        "path": "",
        "files": [{"path": "task.yaml", "size": 42}],
    }


def test_fs_get_writes_file_content(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(
        datasets_command="fs",
        fs_command="get",
        org="qwen",
        dataset="my-bench",
        split="test",
        task="task-001",
        path="task.yaml",
    )
    mock_client = MagicMock()
    mock_client.get_task_file.return_value = b"instruction: fix bug\n"

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._fs(args))

    mock_client.get_task_file.assert_called_once_with("qwen", "my-bench", "test", "task-001", "task.yaml")
    assert capsys.readouterr().out == "instruction: fix bug\n"


def test_fs_get_without_path_reads_single_task_file(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(
        datasets_command="fs",
        fs_command="get",
        org="qwen",
        dataset="my-bench",
        split="test",
        task="task-001",
        path=None,
    )
    mock_client = MagicMock()
    mock_client.list_task_files.return_value = [TaskFile(path="task-001.json", size=42)]
    mock_client.get_task_file.return_value = b'{"instruction": "fix bug"}\n'

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._fs(args))

    mock_client.list_task_files.assert_called_once_with("qwen", "my-bench", "test", "task-001", "")
    mock_client.get_task_file.assert_called_once_with("qwen", "my-bench", "test", "task-001", "task-001.json")
    assert capsys.readouterr().out == '{"instruction": "fix bug"}\n'


def test_fs_get_without_path_requires_path_when_task_has_multiple_files():
    cmd = DatasetsCommand()
    args = make_base_args(
        datasets_command="fs",
        fs_command="get",
        org="qwen",
        dataset="my-bench",
        split="test",
        task="task-001",
        path=None,
    )
    mock_client = MagicMock()
    mock_client.list_task_files.return_value = [
        TaskFile(path="task.yaml", size=42),
        TaskFile(path="tests/test_api.py", size=10),
    ]

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            with pytest.raises(ValueError, match="--path is required"):
                asyncio.run(cmd._fs(args))


def test_fs_download_file_writes_destination(tmp_path):
    cmd = DatasetsCommand()
    dest = tmp_path / "task.yaml"
    args = make_base_args(
        datasets_command="fs",
        fs_command="download",
        org="qwen",
        dataset="my-bench",
        split="test",
        task="task-001",
        path="task.yaml",
        dest=str(dest),
    )
    mock_client = MagicMock()
    mock_client.get_task_file.return_value = b"instruction: fix bug\n"

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._fs(args))

    assert dest.read_bytes() == b"instruction: fix bug\n"
    mock_client.list_task_files.assert_not_called()


def test_fs_download_directory_strips_requested_prefix(tmp_path):
    cmd = DatasetsCommand()
    args = make_base_args(
        datasets_command="fs",
        fs_command="download",
        org="qwen",
        dataset="my-bench",
        split="test",
        task="task-001",
        path="tests/",
        dest=str(tmp_path / "downloaded-tests"),
    )
    mock_client = MagicMock()
    mock_client.get_task_file.return_value = None
    mock_client.list_task_files.return_value = [
        TaskFile(path="tests/test_api.py", size=10),
        TaskFile(path="tests/fixtures/input.json", size=20),
    ]
    mock_client.get_task_file.side_effect = [None, b"assert True\n", b"{}\n"]

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._fs(args))

    assert (tmp_path / "downloaded-tests" / "test_api.py").read_text() == "assert True\n"
    assert (tmp_path / "downloaded-tests" / "fixtures" / "input.json").read_text() == "{}\n"


def test_fs_rejects_unsafe_relative_path():
    cmd = DatasetsCommand()
    args = make_base_args(
        datasets_command="fs",
        fs_command="get",
        org="qwen",
        dataset="my-bench",
        split="test",
        task="task-001",
        path="../other-task/task.yaml",
    )

    with pytest.raises(ValueError, match="relative task path"):
        asyncio.run(cmd._fs(args))


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
        task_ids=["task-002", "task-003"],
    )

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._tasks(args))

    mock_client.list_dataset_tasks.assert_called_once_with(
        "qwen", "my-bench", "test", offset=1, limit=2, task_filter=None
    )
    out = capsys.readouterr().out
    assert "Dataset: qwen/my-bench" in out
    assert "Split: test" in out
    assert "task-002" in out
    assert "task-003" in out
    assert "task-001" not in out
    assert "Shown: 2" in out
    assert "#Task name" in out


def test_list_outputs_json(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(datasets_command="list", output="json")
    mock_client = MagicMock()
    mock_client.list_datasets.return_value = [
        DatasetSpec(id="qwen/bench-b", split="test", task_ids=["b-1"]),
        DatasetSpec(id="qwen/bench-a", split="train", task_ids=["a-1", "a-2"]),
    ]

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._list(args))

    data = json.loads(capsys.readouterr().out)
    assert data == {
        "datasets": [
            {"id": "qwen/bench-a", "split": "train", "task_ids": ["a-1", "a-2"], "task_count": 2},
            {"id": "qwen/bench-b", "split": "test", "task_ids": ["b-1"], "task_count": 1},
        ]
    }


def test_tasks_outputs_json_with_pagination(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(
        datasets_command="tasks",
        org="qwen",
        dataset="my-bench",
        split="test",
        offset=1,
        limit=2,
        output="json",
    )
    mock_client = MagicMock()
    mock_client.list_dataset_tasks.return_value = DatasetSpec(
        id="qwen/my-bench",
        split="test",
        task_ids=["task-002", "task-003"],
    )

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._tasks(args))

    data = json.loads(capsys.readouterr().out)
    assert data == {
        "dataset": "qwen/my-bench",
        "split": "test",
        "total": 2,
        "offset": 1,
        "limit": 2,
        "task_ids": ["task-002", "task-003"],
    }


def test_tasks_outputs_empty_json_when_not_found(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(
        datasets_command="tasks",
        org="qwen",
        dataset="my-bench",
        split="test",
        offset=0,
        limit=None,
        output="json",
    )
    mock_client = MagicMock()
    mock_client.list_dataset_tasks.return_value = None

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._tasks(args))

    data = json.loads(capsys.readouterr().out)
    assert data == {
        "dataset": "qwen/my-bench",
        "split": "test",
        "total": 0,
        "offset": 0,
        "limit": None,
        "task_ids": [],
    }


def test_upload_outputs_json(capsys, tmp_path):
    cmd = DatasetsCommand()
    args = make_base_args(
        datasets_command="upload",
        org="qwen",
        dataset="my-bench",
        split="test",
        dir=str(tmp_path),
        overwrite=False,
        concurrency=4,
        output="json",
    )
    mock_client = MagicMock()
    mock_client.upload_dataset.return_value = UploadResult(
        id="qwen/my-bench",
        split="test",
        uploaded=2,
        skipped=1,
        failed=0,
    )

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._upload(args))

    data = json.loads(capsys.readouterr().out)
    assert data == {
        "id": "qwen/my-bench",
        "split": "test",
        "uploaded": 2,
        "skipped": 1,
        "failed": 0,
    }


def test_upload_accepts_single_file_source(capsys, tmp_path):
    task_file = tmp_path / "task-001.json"
    task_file.write_text("{}")
    cmd = DatasetsCommand()
    args = make_base_args(
        datasets_command="upload",
        org="qwen",
        dataset="my-bench",
        split="test",
        dir=str(task_file),
        overwrite=False,
        concurrency=4,
    )
    mock_client = MagicMock()
    mock_client.upload_dataset.return_value = UploadResult(
        id="qwen/my-bench",
        split="test",
        uploaded=1,
        skipped=0,
        failed=0,
    )

    with patch.object(cmd, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient", return_value=mock_client):
            asyncio.run(cmd._upload(args))

    source = mock_client.upload_dataset.call_args.args[0]
    assert source.path == task_file
    assert "Uploading to oss://b/datasets/qwen/my-bench/test/" in capsys.readouterr().out


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


# ---------------------------------------------------------------------------
# list subcommand tests (depth + --org rewrite)
# ---------------------------------------------------------------------------


def test_list_default_depth_calls_list_all_datasets_and_renders_two_columns(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(datasets_command="list", depth=None, org=None)

    with patch.object(DatasetsCommand, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient") as MockClient:
            MockClient.return_value.list_all_datasets.return_value = [
                ("alibaba", "pinch"),
                ("qwen", "bench-1"),
            ]
            asyncio.run(cmd._list(args))

    MockClient.return_value.list_all_datasets.assert_called_once_with()
    out = capsys.readouterr().out
    assert "Organization" in out
    assert "Dataset" in out
    assert "alibaba" in out and "pinch" in out
    assert "qwen" in out and "bench-1" in out
    assert "2 datasets in 2 organizations." in out


def test_list_depth_1_calls_list_organizations_and_renders_one_column(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(datasets_command="list", depth=1, org=None)

    with patch.object(DatasetsCommand, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient") as MockClient:
            MockClient.return_value.list_organizations.return_value = ["alibaba", "qwen"]
            asyncio.run(cmd._list(args))

    MockClient.return_value.list_organizations.assert_called_once_with()
    MockClient.return_value.list_all_datasets.assert_not_called()
    out = capsys.readouterr().out
    assert "Organization" in out
    assert "alibaba" in out
    assert "qwen" in out
    assert "2 organizations." in out


def test_list_with_org_calls_list_org_datasets(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(datasets_command="list", depth=2, org="alibaba")

    with patch.object(DatasetsCommand, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient") as MockClient:
            MockClient.return_value.list_org_datasets.return_value = ["pinch", "webdev"]
            asyncio.run(cmd._list(args))

    MockClient.return_value.list_org_datasets.assert_called_once_with("alibaba")
    MockClient.return_value.list_all_datasets.assert_not_called()
    MockClient.return_value.list_organizations.assert_not_called()
    out = capsys.readouterr().out
    assert "alibaba" in out and "pinch" in out and "webdev" in out
    assert "2 datasets in 1 organizations." in out


def test_list_empty_prints_no_datasets_message(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(datasets_command="list", depth=2, org=None)

    with patch.object(DatasetsCommand, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient") as MockClient:
            MockClient.return_value.list_all_datasets.return_value = []
            asyncio.run(cmd._list(args))

    out = capsys.readouterr().out
    assert "No datasets found." in out


def test_list_depth_1_empty_prints_no_organizations_message(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(datasets_command="list", depth=1, org=None)

    with patch.object(DatasetsCommand, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient") as MockClient:
            MockClient.return_value.list_organizations.return_value = []
            asyncio.run(cmd._list(args))

    out = capsys.readouterr().out
    assert "No organizations found." in out


def test_list_parser_depth_and_org_mutually_exclusive():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["datasets", "list", "--depth", "2", "--org", "alibaba"])


def test_list_parser_depth_default_is_deferred_to_runtime():
    parser = _build_parser()
    parsed = parser.parse_args(["datasets", "list"])
    assert parsed.depth is None
    assert parsed.org is None


def test_list_parser_rejects_invalid_depth():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["datasets", "list", "--depth", "3"])


# ---------------------------------------------------------------------------
# splits subcommand tests
# ---------------------------------------------------------------------------


def test_splits_lists_split_names(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(datasets_command="splits", org="alibaba", dataset="pinch")

    with patch.object(DatasetsCommand, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient") as MockClient:
            MockClient.return_value.list_dataset_splits.return_value = ["test", "train"]
            asyncio.run(cmd._splits(args))

    MockClient.return_value.list_dataset_splits.assert_called_once_with("alibaba", "pinch")
    out = capsys.readouterr().out
    assert "Split" in out
    assert "test" in out
    assert "train" in out
    assert "2 splits." in out


def test_splits_empty_prints_no_splits_message(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(datasets_command="splits", org="alibaba", dataset="missing")

    with patch.object(DatasetsCommand, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient") as MockClient:
            MockClient.return_value.list_dataset_splits.return_value = []
            asyncio.run(cmd._splits(args))

    out = capsys.readouterr().out
    assert "No splits found for dataset 'alibaba/missing'." in out


def test_splits_singular_footer_for_one_split(capsys):
    cmd = DatasetsCommand()
    args = make_base_args(datasets_command="splits", org="alibaba", dataset="pinch")

    with patch.object(DatasetsCommand, "_build_oss_registry_info", return_value=make_registry_info()):
        with patch("rock.cli.command.datasets.DatasetClient") as MockClient:
            MockClient.return_value.list_dataset_splits.return_value = ["test"]
            asyncio.run(cmd._splits(args))

    out = capsys.readouterr().out
    assert "1 split." in out


def test_splits_parser_requires_org_and_dataset():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["datasets", "splits"])
    with pytest.raises(SystemExit):
        parser.parse_args(["datasets", "splits", "--org", "alibaba"])


def test_splits_parser_accepts_org_and_dataset():
    parser = _build_parser()
    parsed = parser.parse_args(["datasets", "splits", "--org", "alibaba", "--dataset", "pinch"])
    assert parsed.org == "alibaba"
    assert parsed.dataset == "pinch"
