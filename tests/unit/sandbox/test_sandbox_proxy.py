import logging
import uuid
from unittest.mock import MagicMock, patch

import pytest

from rock.actions.sandbox.response import State
from rock.config import OssConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from tests.unit.conftest import check_sandbox_status_until_alive


@pytest.mark.need_docker
@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_batch_get_sandbox_status(sandbox_manager: SandboxManager, sandbox_proxy_service: SandboxProxyService):
    sandbox_ids = []
    sandbox_count = 3
    for _ in range(sandbox_count):
        response = await sandbox_manager.start_async(DockerDeploymentConfig(cpus=0.5, memory="1g"))
        sandbox_ids.append(response.sandbox_id)
        await check_sandbox_status_until_alive(sandbox_manager, response.sandbox_id)
    # batch get status
    batch_response = await sandbox_proxy_service.batch_get_sandbox_status(sandbox_ids)

    assert len(batch_response) == sandbox_count
    response_sandbox_ids = [status.sandbox_id for status in batch_response]
    for sandbox_id in sandbox_ids:
        assert sandbox_id in response_sandbox_ids

    for status in batch_response:
        assert status.sandbox_id in sandbox_ids
        assert status.is_alive is True
        assert status.state == State.RUNNING

    invalid_ids = sandbox_ids + ["invalid_sandbox_id_1", "invalid_sandbox_id_2"]
    batch_response_with_invalid = await sandbox_proxy_service.batch_get_sandbox_status(invalid_ids)
    assert len(batch_response_with_invalid) == len(sandbox_ids)
    for sandbox_id in sandbox_ids:
        await sandbox_manager.stop(sandbox_id)


@pytest.mark.need_docker
@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_list_sandbox(sandbox_manager: SandboxManager, sandbox_proxy_service: SandboxProxyService):
    # create two sandbox
    random_user_id = uuid.uuid4().hex[:8]
    user_info1 = {"user_id": random_user_id, "experiment_id": "exp1", "rock_authorization": "rock_authorization"}
    user_info2 = {"user_id": "user2", "experiment_id": "exp2"}
    response1 = await sandbox_manager.start_async(DockerDeploymentConfig(cpus=0.5, memory="1g"), user_info=user_info1)
    response2 = await sandbox_manager.start_async(DockerDeploymentConfig(cpus=0.5, memory="1g"), user_info=user_info2)
    sandbox_id1 = response1.sandbox_id
    sandbox_id2 = response2.sandbox_id
    await check_sandbox_status_until_alive(sandbox_manager, sandbox_id1)
    await check_sandbox_status_until_alive(sandbox_manager, sandbox_id2)

    # empty query
    result = await sandbox_proxy_service.list_sandboxes({})
    assert len(result.items) >= 2
    sandbox_ids = [s.sandbox_id for s in result.items]
    assert sandbox_id1 in sandbox_ids
    assert sandbox_id2 in sandbox_ids

    # query with params
    result = await sandbox_proxy_service.list_sandboxes(
        {"user_id": user_info1["user_id"], "experiment_id": user_info1["experiment_id"]}
    )
    assert len(result.items) >= 1
    for sandbox_data in result.items:
        assert sandbox_data.user_id == user_info1["user_id"]
        assert sandbox_data.experiment_id == user_info1["experiment_id"]
    sandbox_ids = [s.sandbox_id for s in result.items]
    assert sandbox_id1 in sandbox_ids

    rock_auth_encrypted = result.items[0].rock_authorization_encrypted
    assert rock_auth_encrypted is not None
    assert rock_auth_encrypted != user_info1["rock_authorization"]
    assert sandbox_manager._aes_encrypter.decrypt(rock_auth_encrypted) == user_info1["rock_authorization"]

    # assert empty list
    result = await sandbox_proxy_service.list_sandboxes(
        {"user_id": user_info1["user_id"], "experiment_id": user_info2["experiment_id"]}
    )
    assert len(result.items) == 0
    await sandbox_manager.stop(sandbox_id1)
    await sandbox_manager.stop(sandbox_id2)


class TestGenOssStsToken:
    @pytest.fixture
    def sandbox_proxy_service(self):
        # Build a minimal SandboxProxyService without going through __init__
        # (which requires real Redis / metrics / RAM-Acs client setup).
        service = SandboxProxyService.__new__(SandboxProxyService)
        service.oss_config = OssConfig(role_arn="test_role_arn")
        service.sts_client = MagicMock()
        return service

    def test_success_returns_dict_with_extra_fields(self, sandbox_proxy_service):
        sandbox_proxy_service.oss_config.endpoint = "ep"
        sandbox_proxy_service.oss_config.bucket = "bk"

        fake_token_body = (
            b'{"Credentials": {"AccessKeyId":"ak","AccessKeySecret":"sk",'
            b'"SecurityToken":"tok","Expiration":"2099-01-01T00:00:00Z"}}'
        )
        with (
            patch.object(sandbox_proxy_service.sts_client, "do_action_with_exception", return_value=fake_token_body),
            patch("rock.sandbox.service.sandbox_proxy_service.env_vars") as mock_env,
        ):
            mock_env.ROCK_OSS_BUCKET_REGION = "rg"
            result = sandbox_proxy_service.gen_oss_sts_token()

        assert result["AccessKeyId"] == "ak"
        assert result["Endpoint"] == "ep"
        assert result["Bucket"] == "bk"
        assert result["Region"] == "rg"

    def test_sts_failure_returns_none(self, sandbox_proxy_service):
        with patch.object(
            sandbox_proxy_service.sts_client, "do_action_with_exception", side_effect=Exception("sts fail")
        ):
            result = sandbox_proxy_service.gen_oss_sts_token()
        assert result is None

    def test_partial_oss_config_returns_creds_with_none_extras(self, sandbox_proxy_service):
        # endpoint 没配，但 STS 仍工作
        sandbox_proxy_service.oss_config.endpoint = ""
        sandbox_proxy_service.oss_config.bucket = "bk"

        fake_token_body = (
            b'{"Credentials": {"AccessKeyId":"ak","AccessKeySecret":"sk",'
            b'"SecurityToken":"tok","Expiration":"2099-01-01T00:00:00Z"}}'
        )
        with (
            patch.object(sandbox_proxy_service.sts_client, "do_action_with_exception", return_value=fake_token_body),
            patch("rock.sandbox.service.sandbox_proxy_service.env_vars") as mock_env,
        ):
            mock_env.ROCK_OSS_BUCKET_REGION = ""
            result = sandbox_proxy_service.gen_oss_sts_token()

        assert result["AccessKeyId"] == "ak"  # STS 仍正常返回
        assert result["Endpoint"] is None
        assert result["Bucket"] == "bk"
        assert result["Region"] is None


class TestStartupOssConfigWarning:
    def _make_rock_config(self, oss_config: OssConfig):
        rock_config = MagicMock()
        rock_config.oss = oss_config
        rock_config.runtime.metrics_endpoint = "http://localhost:9090/metrics"
        rock_config.runtime.user_defined_tags = {}
        rock_config.proxy_service.timeout = 10
        rock_config.proxy_service.max_connections = 10
        rock_config.proxy_service.max_keepalive_connections = 5
        rock_config.proxy_service.batch_get_status_max_count = 100
        return rock_config

    def test_warning_when_partially_configured(self, caplog, monkeypatch):
        # rock 的 logger propagate=False，强制开启以让 caplog 捕获到
        from rock.sandbox.service import sandbox_proxy_service as sps_mod

        monkeypatch.setattr(sps_mod.logger, "propagate", True)

        oss_config = OssConfig(
            endpoint="ep",
            bucket="",  # 缺
            access_key_id="ak",
            access_key_secret="sk",
            role_arn="arn",
        )
        rock_config = self._make_rock_config(oss_config)

        with (
            patch("rock.sandbox.service.sandbox_proxy_service.MetricsMonitor"),
            patch("rock.sandbox.service.sandbox_proxy_service.client.AcsClient"),
            patch("rock.sandbox.service.sandbox_proxy_service.httpx"),
            patch("rock.sandbox.service.sandbox_proxy_service.env_vars") as mock_env,
        ):
            mock_env.ROCK_OSS_BUCKET_REGION = "rg"
            with caplog.at_level(logging.WARNING):
                _ = SandboxProxyService(rock_config=rock_config, meta_store=MagicMock())

        assert any(
            "partially set" in r.message.lower() or "partially configured" in r.message.lower() for r in caplog.records
        ), f"Expected partial-config warning, got: {[r.message for r in caplog.records]}"

    def test_no_warning_when_fully_configured(self, caplog, monkeypatch):
        from rock.sandbox.service import sandbox_proxy_service as sps_mod

        monkeypatch.setattr(sps_mod.logger, "propagate", True)

        oss_config = OssConfig(
            endpoint="ep",
            bucket="bk",
            access_key_id="ak",
            access_key_secret="sk",
            role_arn="arn",
        )
        rock_config = self._make_rock_config(oss_config)

        with (
            patch("rock.sandbox.service.sandbox_proxy_service.MetricsMonitor"),
            patch("rock.sandbox.service.sandbox_proxy_service.client.AcsClient"),
            patch("rock.sandbox.service.sandbox_proxy_service.httpx"),
            patch("rock.sandbox.service.sandbox_proxy_service.env_vars") as mock_env,
        ):
            mock_env.ROCK_OSS_BUCKET_REGION = "rg"
            with caplog.at_level(logging.WARNING):
                _ = SandboxProxyService(rock_config=rock_config, meta_store=MagicMock())

        assert not any("oss" in r.message.lower() and "partial" in r.message.lower() for r in caplog.records)
