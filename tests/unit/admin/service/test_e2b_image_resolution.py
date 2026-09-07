"""Core image-resolution behavior through E2B sandbox start."""

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fakeredis.aioredis import FakeRedis

from rock.admin.service.acr_image_resolver import AcrRegionImageResolver
from rock.admin.service.e2b_service import E2BService
from rock.admin.service.image_resolver import create_image_resolver
from rock.config import ImageResolverConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.utils.providers.redis_provider import RedisProvider


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
        options.setdefault("dadi_tag_suffixes", None)
        redis_provider = options.pop("redis_provider", None)
        pool = Mock()
        pool.get.return_value = client
        resolver = create_image_resolver(
            ImageResolverConfig(
                resolver_class="rock.admin.service.acr_image_resolver.AcrRegionImageResolver",
                options={
                    "source_registries": ["sg.example.com", "sh.example.com"],
                    "region_registry_mapping": {
                        "cn-zhangjiakou": "zjk.example.com",
                        "cn-shanghai": "sh-target.example.com",
                        "cn-beijing": "bj.example.com",
                    },
                    **options,
                },
            ),
            pool,
            redis_provider,
        )
        return E2BService(manager, templates, image_resolver=resolver), manager

    yield build
    for client in clients:
        await client.aclose()


@pytest.fixture
async def dadi_redis():
    provider = RedisProvider(host="", port=0, password="")
    provider.client = FakeRedis(decode_responses=True)
    yield provider
    await provider.close_pool()


@pytest.mark.parametrize(
    ("source", "region", "target", "repository", "probe_registry"),
    [
        ("sg.example.com", "cn-zhangjiakou", "zjk.example.com", "team/task:version", None),
        ("sh.example.com", "cn-shanghai", "sh-target.example.com", "team/task:version", None),
        ("sg.example.com", "cn-beijing", "bj.example.com", "team/task@sha256:" + "a" * 64, None),
        ("sh.example.com", "cn-zhangjiakou", "zjk.example.com", "team/task:version", "sg.example.com"),
    ],
)
async def test_start_maps_template_image_and_preserves_suffix(
    make_service, source, region, target, repository, probe_registry
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(401, headers=_challenge(region))

    image = f"{source}/{repository}"
    template = {"image": image, "cpu_count": 4, "memory_mb": 8192, "disk_size_mb": 51200}
    service, manager = make_service(respond, template=template, probe_registry=probe_registry)
    config = DockerDeploymentConfig(image="template-id", template_id="template-id")
    await service.start(config)

    passed = manager.start_from_template.call_args[0][0]
    assert passed.image == f"{target}/{repository}"
    assert passed.template_id == "template-id"
    assert config.image == "template-id"
    assert template["image"] == image
    assert str(requests[0].url) == f"https://{probe_registry or source}/v2/"
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

    service, manager = make_service(respond, probe_registry="probe.example.com")
    source = "sg.example.com.other.example" if case == "unlisted" else "sg.example.com"
    image = f"{source}/team/task:version"
    await service.start(DockerDeploymentConfig(image=image))

    assert manager.start_from_template.call_args[0][0].image == image
    assert len(requests) == (0 if case == "unlisted" else 1)
    if requests:
        assert str(requests[0].url) == "https://probe.example.com/v2/"


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

    service, manager = make_service(respond, timeout_seconds=0.02, probe_registry="sg.example.com")
    sources = ["sg.example.com", "sh.example.com"] * 5
    await asyncio.wait_for(
        asyncio.gather(
            *(
                service.start(DockerDeploymentConfig(image=f"{source}/team/task:{index}"))
                for index, source in enumerate(sources)
            )
        ),
        timeout=0.5,
    )
    await service.start(DockerDeploymentConfig(image="sg.example.com/team/another:latest"))

    images = {call.args[0].image for call in manager.start_from_template.call_args_list}
    expected = {
        f"{'zjk.example.com' if outcome == 'success' else source}/team/task:{index}"
        for index, source in enumerate(sources)
    }
    target = "zjk.example.com" if outcome == "success" else "sg.example.com"
    assert images == expected | {f"{target}/team/another:latest"}
    assert len(requests) == 1
    assert str(requests[0].url) == "https://sg.example.com/v2/"


@pytest.mark.parametrize("manifest_status", [200, 404, 403])
async def test_start_selects_existing_dadi_tag_after_region_mapping(make_service, manifest_status):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/v2/":
            return httpx.Response(401, headers=_challenge())
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"token": "pull-only-token"})
        if "authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": (
                        'Bearer realm="https://sg.example.com/auth/token", '
                        'service="registry.aliyuncs.com:cn-zhangjiakou:china:cri-test"'
                    )
                },
            )
        return httpx.Response(manifest_status)

    service, manager = make_service(
        respond, probe_registry="sg.example.com", dadi_tag_suffixes=["_containerd_accelerated"]
    )
    config = DockerDeploymentConfig(image="sh.example.com/team/task:version")
    await service.start(config)

    expected = "zjk.example.com/team/task:version"
    if manifest_status == 200:
        expected = "zjk.example.com/team/task:version_containerd_accelerated"
    assert manager.start_from_template.call_args.args[0].image == expected
    assert config.image == "sh.example.com/team/task:version"
    assert {request.url.host for request in requests} == {"sg.example.com"}
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/v2/"),
        ("HEAD", "/v2/team/task/manifests/version_containerd_accelerated"),
        ("GET", "/auth/token"),
        ("HEAD", "/v2/team/task/manifests/version_containerd_accelerated"),
    ]
    assert all("authorization" not in request.headers for request in requests[:3])
    assert requests[2].url.params["scope"] == "repository:team/task:pull"
    assert requests[2].headers["accept"] == "application/json"
    assert requests[3].headers["authorization"] == "Bearer pull-only-token"
    assert "application/vnd.oci.image.manifest.v1+json" in requests[3].headers["accept"]


@pytest.mark.parametrize(
    ("reference", "expected", "probe_count"),
    [
        ("team/task", "team/task:latest_accelerated", 1),
        ("team/task:v_containerd_accelerated", "team/task:v_containerd_accelerated", 0),
        ("team/task:v@sha256:" + "a" * 64, "team/task:v@sha256:" + "a" * 64, 0),
    ],
)
async def test_start_handles_dadi_image_references(make_service, reference, expected, probe_count):
    probes = []

    def respond(request):
        if request.url.path == "/v2/":
            return httpx.Response(401, headers=_challenge())
        probes.append(request)
        assert request.url.path == "/v2/team/task/manifests/latest_accelerated"
        return httpx.Response(200)

    service, manager = make_service(respond, dadi_tag_suffixes=["_accelerated"])
    await service.start(DockerDeploymentConfig(image=f"sg.example.com/{reference}"))
    assert manager.start_from_template.call_args.args[0].image == f"zjk.example.com/{expected}"
    assert len(probes) == probe_count


@pytest.mark.parametrize("outcome", [200, "timeout"])
async def test_dadi_probes_run_concurrently_with_timeout(make_service, outcome):
    active = peak = 0
    all_started = asyncio.Event()

    async def respond(request):
        nonlocal active, peak
        if request.url.path == "/v2/":
            return httpx.Response(401, headers=_challenge())
        active += 1
        peak = max(peak, active)
        try:
            if active == 40:
                all_started.set()
            await all_started.wait()
            if outcome == "timeout":
                await asyncio.Event().wait()
            return httpx.Response(outcome)
        finally:
            active -= 1

    service, manager = make_service(respond, dadi_tag_suffixes=["_accelerated"], timeout_seconds=0.1)
    await asyncio.wait_for(
        asyncio.gather(*(service.start(DockerDeploymentConfig(image="sg.example.com/team/task:v")) for _ in range(40))),
        timeout=0.5,
    )
    expected = "zjk.example.com/team/task:v_accelerated" if outcome == 200 else "zjk.example.com/team/task:v"
    assert {call.args[0].image for call in manager.start_from_template.call_args_list} == {expected}
    assert peak == 40
    assert active == 0


@pytest.mark.parametrize("failure", ["untrusted-realm", "token-denied", "invalid-json"])
async def test_dadi_auth_failure_preserves_region_mapping(make_service, failure):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/v2/":
            return httpx.Response(401, headers=_challenge())
        if request.url.path == "/auth/token":
            if failure == "invalid-json":
                return httpx.Response(200, text="not json")
            return httpx.Response(403)
        realm = (
            "https://untrusted.example.com/auth/token"
            if failure == "untrusted-realm"
            else "https://sg.example.com/auth/token"
        )
        return httpx.Response(401, headers={"www-authenticate": f'Bearer realm="{realm}", service="acr"'})

    service, manager = make_service(respond, dadi_tag_suffixes=["_accelerated"])
    await service.start(DockerDeploymentConfig(image="sg.example.com/team/task:v"))
    assert manager.start_from_template.call_args.args[0].image == "zjk.example.com/team/task:v"
    assert {request.url.host for request in requests} == {"sg.example.com"}
    expected_requests = [("GET", "/v2/"), ("HEAD", "/v2/team/task/manifests/v_accelerated")]
    if failure != "untrusted-realm":
        expected_requests.append(("GET", "/auth/token"))
        assert requests[-1].url.params["scope"] == "repository:team/task:pull"
    assert [(request.method, request.url.path) for request in requests] == expected_requests


@pytest.mark.parametrize(
    ("first_status", "second_status", "expected_tag", "expected_probes"),
    [
        (200, 200, "v_accelerated", ["v_accelerated"]),
        (404, 200, "v_containerd_accelerated", ["v_accelerated", "v_containerd_accelerated"]),
        (404, 404, "v", ["v_accelerated", "v_containerd_accelerated"]),
        (503, 200, "v_containerd_accelerated", ["v_accelerated", "v_containerd_accelerated"]),
        ("timeout", 200, "v_containerd_accelerated", ["v_accelerated", "v_containerd_accelerated"]),
    ],
)
async def test_start_probes_dadi_tags_in_default_priority_order(
    first_status, second_status, expected_tag, expected_probes
):
    probes = []

    async def respond(request):
        if request.url.path == "/v2/":
            return httpx.Response(401, headers=_challenge())
        tag = request.url.path.rsplit("/", 1)[-1]
        probes.append(tag)
        status = first_status if tag == "v_accelerated" else second_status
        if status == "timeout":
            await asyncio.Event().wait()
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolver = AcrRegionImageResolver(
            client,
            source_registries=["sg.example.com"],
            region_registry_mapping={"cn-zhangjiakou": "zjk.example.com"},
            timeout_seconds=0.03,
        )
        manager, templates = AsyncMock(), AsyncMock()
        templates.get_ready_template.return_value = None
        service = E2BService(manager, templates, image_resolver=resolver)
        for attempt in range(1, 3):
            await service.start(DockerDeploymentConfig(image="sg.example.com/team/task:v"))
            assert manager.start_from_template.call_args.args[0].image == f"zjk.example.com/team/task:{expected_tag}"
            assert probes == expected_probes * attempt  # Without Redis, neither hits nor misses are cached.


@pytest.mark.parametrize("redis_ttl", [None, 86400])
async def test_dadi_positive_cache_is_shared_with_configured_ttl(make_service, dadi_redis, redis_ttl):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(401, headers=_challenge()) if request.url.path == "/v2/" else httpx.Response(200)

    options = {"redis_provider": dadi_redis, "dadi_tag_suffixes": ["_accelerated"]}
    if redis_ttl is not None:
        options["dadi_redis_cache_ttl_seconds"] = redis_ttl
    for index in range(2):
        service, manager = make_service(respond, **options)
        await service.start(DockerDeploymentConfig(image="sg.example.com/team/task:v"))
        assert manager.start_from_template.call_args.args[0].image == "zjk.example.com/team/task:v_accelerated"
        if index == 0:
            keys = await dadi_redis.client.keys("e2b:dadi:exists:*")
            assert keys == ["e2b:dadi:exists:v1:sg.example.com:zjk.example.com/team/task:v_accelerated"]
            expected_ttl = 2592000 if redis_ttl is None else 86400
            assert expected_ttl - 5 <= await dadi_redis.get_ttl(keys[0]) <= expected_ttl
            await dadi_redis.client.expire(keys[0], 60)

    assert [request.url.path for request in requests] == [
        "/v2/",
        "/v2/team/task/manifests/v_accelerated",
        "/v2/",
    ]
    keys = await dadi_redis.client.keys("e2b:dadi:exists:*")
    assert len(keys) == 1
    assert await dadi_redis.json_get(keys[0], path=".") is True
    assert 55 <= await dadi_redis.get_ttl(keys[0]) <= 60  # Shared hits do not renew the TTL.
    await dadi_redis.client.delete(keys[0])
    await service.start(DockerDeploymentConfig(image="sg.example.com/team/task:v"))
    assert [request.method for request in requests] == ["GET", "HEAD", "GET", "HEAD"]


@pytest.mark.parametrize("status", [404, 403, 500, "timeout"])
async def test_dadi_non_success_is_not_shared_in_redis(make_service, dadi_redis, status):
    probes = []

    async def respond(request):
        if request.url.path == "/v2/":
            return httpx.Response(401, headers=_challenge())
        probes.append(request)
        if status == "timeout":
            await asyncio.Event().wait()
        return httpx.Response(status)

    service, manager = make_service(
        respond, redis_provider=dadi_redis, dadi_tag_suffixes=["_accelerated"], timeout_seconds=0.05
    )
    for _ in range(2):
        await service.start(DockerDeploymentConfig(image="sg.example.com/team/task:v"))
        assert manager.start_from_template.call_args.args[0].image == "zjk.example.com/team/task:v"
    assert len(probes) == 2
    assert await dadi_redis.client.keys("e2b:dadi:exists:*") == []


@pytest.mark.parametrize("failure", ["read-error", "read-timeout", "write-timeout"])
async def test_dadi_redis_failure_does_not_discard_success(make_service, dadi_redis, monkeypatch, failure):
    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    if failure == "read-error":
        monkeypatch.setattr(dadi_redis, "json_get", AsyncMock(side_effect=ConnectionError("Redis unavailable")))
    else:
        method = "json_get" if failure == "read-timeout" else "json_set_with_ttl"
        monkeypatch.setattr(dadi_redis, method, hang)

    def respond(request):
        return httpx.Response(401, headers=_challenge()) if request.url.path == "/v2/" else httpx.Response(200)

    service, manager = make_service(
        respond, redis_provider=dadi_redis, dadi_tag_suffixes=["_accelerated"], timeout_seconds=0.05
    )
    await asyncio.wait_for(service.start(DockerDeploymentConfig(image="sg.example.com/team/task:v")), timeout=0.5)
    assert manager.start_from_template.call_args.args[0].image == "zjk.example.com/team/task:v_accelerated"


async def test_dadi_redis_cache_is_scoped_to_mapped_registry(make_service, dadi_redis):
    for region, status, expected in [
        ("cn-zhangjiakou", 200, "zjk.example.com/team/task:v_accelerated"),
        ("cn-shanghai", 404, "sh-target.example.com/team/task:v"),
    ]:
        probes = []

        def respond(request):
            if request.url.path == "/v2/":
                return httpx.Response(401, headers=_challenge(region))
            probes.append(request)
            return httpx.Response(status)

        service, manager = make_service(respond, redis_provider=dadi_redis, dadi_tag_suffixes=["_accelerated"])
        await service.start(DockerDeploymentConfig(image="sg.example.com/team/task:v"))
        assert manager.start_from_template.call_args.args[0].image == expected
        assert len(probes) == 1
