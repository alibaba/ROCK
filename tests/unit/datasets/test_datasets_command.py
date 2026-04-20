import argparse
from unittest.mock import patch

import pytest

from rock.cli.command.datasets import DatasetsCommand


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
    )
    for k, v in kwargs.items():
        setattr(args, k, v)
    return args


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
