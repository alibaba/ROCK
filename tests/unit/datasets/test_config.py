from rock.cli.config import ConfigManager


def test_dataset_config_from_ini(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text(
        "[rock]\n"
        "base_url = http://localhost:8080\n"
        "\n"
        "[dataset]\n"
        "oss_bucket = my-bucket\n"
        "oss_endpoint = https://oss-cn-hangzhou.aliyuncs.com\n"
        "oss_access_key_id = LTAI5t\n"
        "oss_access_key_secret = secret123\n"
        "oss_region = cn-hangzhou\n"
    )
    ds = ConfigManager(config_file).get_config().dataset_config

    assert ds.oss_bucket == "my-bucket"
    assert ds.oss_endpoint == "https://oss-cn-hangzhou.aliyuncs.com"
    assert ds.oss_access_key_id == "LTAI5t"
    assert ds.oss_access_key_secret == "secret123"
    assert ds.oss_region == "cn-hangzhou"


def test_dataset_config_defaults_when_section_absent(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("[rock]\nbase_url = http://localhost:8080\n")
    ds = ConfigManager(config_file).get_config().dataset_config

    assert ds.oss_bucket is None
    assert ds.oss_endpoint is None
    assert ds.oss_access_key_id is None
    assert ds.oss_access_key_secret is None
    assert ds.oss_region is None


def test_dataset_config_partial_section(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("[dataset]\noss_bucket = only-bucket\n")
    ds = ConfigManager(config_file).get_config().dataset_config

    assert ds.oss_bucket == "only-bucket"
    assert ds.oss_endpoint is None
