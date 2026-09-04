"""Core image-resolution behavior through E2B sandbox start."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from rock.admin.service.acr_image_resolver import AcrRegionImageResolver
from rock.admin.service.e2b_service import E2BService
from rock.deployments.config import DockerDeploymentConfig


def _challenge(region="cn-zhangjiakou"):
    return {"www-authenticate": f'Bearer service="registry.aliyuncs.com:{region}:china:cri-test"'}


@pytest.fixture
async def make_service():
    clients = []

    def build(handler, template=None, **options):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        templates = AsyncMock()
        templates.get_ready_template.return_value = template
        manager = AsyncMock()
        resolver = AcrRegionImageResolver(
            client=client,
            source_registries=["sg.example.com", "sh.example.com"],
            region_registry_mapping={
                "cn-zhangjiakou": "zjk.example.com",
                "cn-shanghai": "sh-target.example.com",
                "cn-beijing": "bj.example.com",
            },
            **options,
        )
        return E2BService(manager, templates, image_resolver=resolver), manager

    yield build
    for client in clients:
        await client.aclose()


@pytest.mark.parametrize(
    ("source", "region", "target", "repository"),
    [
        ("sg.example.com", "cn-zhangjiakou", "zjk.example.com", "team/task:version"),
        ("sh.example.com", "cn-shanghai", "sh-target.example.com", "team/task:version"),
        ("sg.example.com", "cn-beijing", "bj.example.com", "team/task@sha256:" + "a" * 64),
    ],
)
async def test_start_maps_template_image_and_preserves_suffix(make_service, source, region, target, repository):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(401, headers=_challenge(region))

    image = f"{source}/{repository}"
    template = {"image": image, "cpu_count": 4, "memory_mb": 8192, "disk_size_mb": 51200}
    service, manager = make_service(respond, template=template)
    config = DockerDeploymentConfig(image="template-id", template_id="template-id")
    await service.start(config)

    passed = manager.start_from_template.call_args[0][0]
    assert passed.image == f"{target}/{repository}"
    assert passed.template_id == "template-id"
    assert config.image == "template-id"
    assert template["image"] == image
    assert str(requests[0].url) == f"https://{source}/v2/"
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize("case", ["unlisted", "unknown-region", "missing-challenge", "http-error", "dns-error"])
async def test_start_keeps_original_image_when_resolution_is_unavailable(make_service, case):
    requests = []

    def respond(request):
        requests.append(request)
        if case == "dns-error":
            raise httpx.ConnectError("DNS unavailable", request=request)
        if case == "unknown-region":
            return httpx.Response(401, headers=_challenge("ap-southeast-1"))
        if case == "missing-challenge":
            return httpx.Response(401)
        return httpx.Response(500, headers=_challenge())

    service, manager = make_service(respond)
    source = "sg.example.com.other.example" if case == "unlisted" else "sg.example.com"
    image = f"{source}/team/task:version"
    await service.start(DockerDeploymentConfig(image=image))

    assert manager.start_from_template.call_args[0][0].image == image
    assert len(requests) == (0 if case == "unlisted" else 1)


@pytest.mark.parametrize("outcome", ["success", "dns-error", "timeout"])
async def test_concurrent_starts_share_bounded_probe_and_cache(make_service, outcome):
    requests = []

    async def respond(request):
        requests.append(request)
        await asyncio.sleep(0)
        if outcome == "dns-error":
            raise httpx.ConnectError("DNS unavailable", request=request)
        if outcome == "timeout":
            await asyncio.Event().wait()
        return httpx.Response(401, headers=_challenge())

    service, manager = make_service(respond, timeout_seconds=0.02)
    await asyncio.wait_for(
        asyncio.gather(
            *(service.start(DockerDeploymentConfig(image=f"sg.example.com/team/task:{index}")) for index in range(10))
        ),
        timeout=0.5,
    )
    await service.start(DockerDeploymentConfig(image="sg.example.com/team/another:latest"))

    target = "zjk.example.com" if outcome == "success" else "sg.example.com"
    images = {call.args[0].image for call in manager.start_from_template.call_args_list}
    assert images == {f"{target}/team/task:{index}" for index in range(10)} | {f"{target}/team/another:latest"}
    assert len(requests) == 1
