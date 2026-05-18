import json
from unittest.mock import MagicMock, patch

from rock.config import (
    OssAccountConfig,
    OssConfig,
    ProxyServiceConfig,
    RockConfig,
    RuntimeConfig,
    SandboxConfig,
    SandboxFileTransferConfig,
)
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService


def _make_rock_config(
    *,
    legacy_role: str,
    primary_role: str,
    legacy_region: str = "cn-hangzhou",
    transfer_prefix: str = "rock-transfer/",
) -> RockConfig:
    return RockConfig(
        oss=OssConfig(
            endpoint="oss-cn-hangzhou.aliyuncs.com",
            bucket="xrl-sandbox",
            access_key_id="legacy-ak",
            access_key_secret="legacy-sk",
            role_arn=legacy_role,
            region=legacy_region,
            primary=OssAccountConfig(
                endpoint="oss-cn-hangzhou.aliyuncs.com",
                bucket="chatos-rock",
                access_key_id="primary-ak",
                access_key_secret="primary-sk",
                role_arn=primary_role,
                region="cn-hangzhou",
            ),
        ),
        sandbox_config=SandboxConfig(
            file_transfer=SandboxFileTransferConfig(prefix=transfer_prefix),
        ),
        proxy_service=ProxyServiceConfig(),
        runtime=RuntimeConfig(
            python_env_path="/usr/bin/python3",
            envhub_db_url="sqlite:////tmp/rock_envs.db",
        ),
    )


def _fake_assume(ak: str, sk: str, tok: str):
    def _do(req):
        return json.dumps(
            {
                "Credentials": {
                    "AccessKeyId": ak,
                    "AccessKeySecret": sk,
                    "SecurityToken": tok,
                    "Expiration": "2099-01-01T00:00:00Z",
                }
            }
        ).encode()

    return _do


def _build_service(rock_config: RockConfig) -> SandboxProxyService:
    # Each AcsClient() call returns a fresh MagicMock so legacy/primary clients
    # are not aliased — otherwise per-account fakes would leak across accounts.
    with patch(
        "rock.sandbox.service.sandbox_proxy_service.client.AcsClient",
        side_effect=lambda *a, **kw: MagicMock(),
    ):
        return SandboxProxyService(rock_config, meta_store=MagicMock())


def test_legacy_account_uses_legacy_role_and_bucket():
    svc = _build_service(
        _make_rock_config(
            legacy_role="acs:ram::1933967579503727:role/legacy-role",
            primary_role="acs:ram::1771269394322852:role/chatos-rock-sts-role",
        )
    )
    svc._sts_clients["legacy"].do_action_with_exception = _fake_assume("L-AK", "L-SK", "L-TOK")
    svc._sts_clients["primary"].do_action_with_exception = _fake_assume("P-AK", "P-SK", "P-TOK")

    creds = svc.gen_oss_sts_token()  # default account="legacy"
    assert creds["AccessKeyId"] == "L-AK"
    assert creds["Bucket"] == "xrl-sandbox"
    assert creds["Prefix"] is None


def test_primary_account_uses_primary_role_and_bucket_with_prefix():
    svc = _build_service(
        _make_rock_config(
            legacy_role="acs:ram::1933967579503727:role/legacy-role",
            primary_role="acs:ram::1771269394322852:role/chatos-rock-sts-role",
        )
    )
    svc._sts_clients["legacy"].do_action_with_exception = _fake_assume("L-AK", "L-SK", "L-TOK")
    svc._sts_clients["primary"].do_action_with_exception = _fake_assume("P-AK", "P-SK", "P-TOK")

    creds = svc.gen_oss_sts_token(account="primary")
    assert creds["AccessKeyId"] == "P-AK"
    assert creds["Bucket"] == "chatos-rock"
    assert creds["Prefix"] == "rock-transfer/"


def test_primary_returns_none_when_role_arn_empty():
    svc = _build_service(
        _make_rock_config(
            legacy_role="acs:ram::1933967579503727:role/legacy-role",
            primary_role="",
        )
    )
    assert svc.gen_oss_sts_token(account="primary") is None


def test_primary_prefix_is_none_when_yaml_does_not_set_it():
    svc = _build_service(
        _make_rock_config(
            legacy_role="acs:ram::1933967579503727:role/legacy-role",
            primary_role="acs:ram::1771269394322852:role/chatos-rock-sts-role",
            transfer_prefix="",
        )
    )
    svc._sts_clients["primary"].do_action_with_exception = _fake_assume("P-AK", "P-SK", "P-TOK")
    creds = svc.gen_oss_sts_token(account="primary")
    assert creds["Prefix"] is None


def test_unknown_account_returns_none():
    svc = _build_service(
        _make_rock_config(
            legacy_role="acs:ram::1933967579503727:role/legacy-role",
            primary_role="acs:ram::1771269394322852:role/chatos-rock-sts-role",
        )
    )
    assert svc.gen_oss_sts_token(account="bogus") is None


def test_legacy_prefix_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("ROCK_OSS_TRANSFER_PREFIX", "legacy-pref/")
    svc = _build_service(
        _make_rock_config(
            legacy_role="acs:ram::1933967579503727:role/legacy-role",
            primary_role="acs:ram::1771269394322852:role/chatos-rock-sts-role",
        )
    )
    svc._sts_clients["legacy"].do_action_with_exception = _fake_assume("L-AK", "L-SK", "L-TOK")
    creds = svc.gen_oss_sts_token()
    assert creds["Prefix"] == "legacy-pref/"  # env wins for legacy


def test_assume_role_session_names_are_distinct():
    svc = _build_service(
        _make_rock_config(
            legacy_role="acs:ram::1933967579503727:role/legacy-role",
            primary_role="acs:ram::1771269394322852:role/chatos-rock-sts-role",
        )
    )
    captured: dict[str, dict] = {}

    def capture(label: str):
        def _do(req):
            captured[label] = dict(req.get_query_params())
            return json.dumps(
                {
                    "Credentials": {
                        "AccessKeyId": "x",
                        "AccessKeySecret": "y",
                        "SecurityToken": "z",
                        "Expiration": "2099-01-01T00:00:00Z",
                    }
                }
            ).encode()

        return _do

    svc._sts_clients["legacy"].do_action_with_exception = capture("legacy")
    svc._sts_clients["primary"].do_action_with_exception = capture("primary")
    svc.gen_oss_sts_token(account="legacy")
    svc.gen_oss_sts_token(account="primary")

    assert captured["legacy"]["RoleSessionName"] == "rock-sandbox-legacy"
    assert captured["primary"]["RoleSessionName"] == "rock-sandbox-primary"
    assert captured["legacy"]["RoleArn"] == "acs:ram::1933967579503727:role/legacy-role"
    assert captured["primary"]["RoleArn"] == "acs:ram::1771269394322852:role/chatos-rock-sts-role"


def test_legacy_region_from_yaml_overrides_env(monkeypatch):
    monkeypatch.setenv("ROCK_OSS_BUCKET_REGION", "cn-shanghai")
    captured: list[tuple[str, str]] = []

    def fake_acs_client(ak, sk, region):
        captured.append((ak, region))
        return MagicMock()

    with patch(
        "rock.sandbox.service.sandbox_proxy_service.client.AcsClient",
        side_effect=fake_acs_client,
    ):
        SandboxProxyService(
            _make_rock_config(
                legacy_role="acs:ram::1933967579503727:role/legacy-role",
                primary_role="acs:ram::1771269394322852:role/chatos-rock-sts-role",
                legacy_region="cn-hangzhou",
            ),
            meta_store=MagicMock(),
        )

    legacy_ak, legacy_region = captured[0]
    assert legacy_ak == "legacy-ak"
    assert legacy_region == "cn-hangzhou"  # yaml won


def test_legacy_region_falls_back_to_env_when_yaml_empty(monkeypatch):
    monkeypatch.setenv("ROCK_OSS_BUCKET_REGION", "cn-shanghai")
    captured: list[tuple[str, str]] = []

    def fake_acs_client(ak, sk, region):
        captured.append((ak, region))
        return MagicMock()

    with patch(
        "rock.sandbox.service.sandbox_proxy_service.client.AcsClient",
        side_effect=fake_acs_client,
    ):
        SandboxProxyService(
            _make_rock_config(
                legacy_role="acs:ram::1933967579503727:role/legacy-role",
                primary_role="acs:ram::1771269394322852:role/chatos-rock-sts-role",
                legacy_region="",
            ),
            meta_store=MagicMock(),
        )

    legacy_ak, legacy_region = captured[0]
    assert legacy_region == "cn-shanghai"  # env fallback
