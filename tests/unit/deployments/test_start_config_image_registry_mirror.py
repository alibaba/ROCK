"""Tests for shared image registry mirror normalization."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.config import ImageRegistryMirror
from rock.deployments import start_config
from rock.deployments.config import DockerDeploymentConfig


def _make_rock_config(mirrors, allowlist=None, nacos_provider=None):
    pool_manager = MagicMock()
    pool_manager.get.return_value = MagicMock()
    return SimpleNamespace(
        image_registry_mirrors=mirrors,
        image_mirror_lookup_allowlist=["*"] if allowlist is None else allowlist,
        nacos_provider=nacos_provider,
        http_pool_manager=pool_manager,
    )


@pytest.fixture(autouse=True)
def clear_probe_state():
    start_config.MIRROR_PROBE_CACHE.clear()
    start_config._MIRROR_PROBE_INFLIGHT.clear()
    yield
    start_config.MIRROR_PROBE_CACHE.clear()
    start_config._MIRROR_PROBE_INFLIGHT.clear()


@pytest.fixture
def stub_manifest_probe():
    probes = []
    results = []

    async def probe(rock_config, registry, repo, tag, username=None, password=None):
        probes.append(
            {
                "registry": registry,
                "repo": repo,
                "tag": tag,
                "username": username,
                "password": password,
            }
        )
        return results.pop(0) if results else False

    with patch.object(start_config, "http_probe_manifest", probe):
        yield SimpleNamespace(probes=probes, results=results)


async def test_mirror_hit_rewrites_image_and_credentials(stub_manifest_probe):
    username = "mirror-user"
    password = "mirror-password"
    rock_config = _make_rock_config(
        [
            ImageRegistryMirror(
                registry="mirror.example.com",
                namespace="rock",
                username=username,
                password=password,
            )
        ]
    )
    config = DockerDeploymentConfig(
        image="gcr.io/project/subdir/image:v1",
        registry_username="original-user",
        registry_password="original-password",
    )
    stub_manifest_probe.results.append(True)

    await start_config.apply_image_registry_mirror(rock_config, config)

    assert config.image == "mirror.example.com/project/subdir/image:v1"
    assert config.registry_username == username
    assert config.registry_password == password
    assert stub_manifest_probe.probes == [
        {
            "registry": "mirror.example.com",
            "repo": "project/subdir/image",
            "tag": "v1",
            "username": username,
            "password": password,
        }
    ]


async def test_registry_candidate_miss_falls_back_to_mirror_namespace(stub_manifest_probe):
    rock_config = _make_rock_config([ImageRegistryMirror(registry="mirror.example.com", namespace="rock")])
    config = DockerDeploymentConfig(image="gcr.io/project/python:3.11")
    stub_manifest_probe.results.extend([False, True])

    await start_config.apply_image_registry_mirror(rock_config, config)

    assert config.image == "mirror.example.com/rock/python:3.11"
    assert [probe["repo"] for probe in stub_manifest_probe.probes] == ["project/python", "rock/python"]


async def test_full_miss_keeps_original_image_and_credentials(stub_manifest_probe):
    rock_config = _make_rock_config(
        [
            ImageRegistryMirror(registry="mirror-a.example.com", namespace="rock"),
            ImageRegistryMirror(registry="mirror-b.example.com", namespace="rock"),
        ]
    )
    config = DockerDeploymentConfig(
        image="python:3.11",
        registry_username="original-user",
        registry_password="original-password",
    )
    stub_manifest_probe.results.extend([False, False])

    await start_config.apply_image_registry_mirror(rock_config, config)

    assert config.image == "python:3.11"
    assert config.registry_username == "original-user"
    assert config.registry_password == "original-password"


async def test_probe_error_falls_back_to_next_mirror():
    rock_config = _make_rock_config(
        [
            ImageRegistryMirror(registry="broken.example.com", namespace="rock"),
            ImageRegistryMirror(registry="mirror.example.com", namespace="rock"),
        ]
    )
    config = DockerDeploymentConfig(image="python:3.11")

    async def probe(rock_config, registry, repo, tag, username=None, password=None):
        if registry == "broken.example.com":
            raise RuntimeError("connection failed")
        return True

    with patch.object(start_config, "http_probe_manifest", probe):
        await start_config.apply_image_registry_mirror(rock_config, config)

    assert config.image == "mirror.example.com/rock/python:3.11"


@pytest.mark.parametrize(
    "image",
    ["gcr.io/project/python@sha256:abc123", "python@sha256:abc123"],
)
async def test_digest_reference_skips_lookup(stub_manifest_probe, image):
    rock_config = _make_rock_config([ImageRegistryMirror(registry="mirror.example.com", namespace="rock")])
    config = DockerDeploymentConfig(image=image)

    await start_config.apply_image_registry_mirror(rock_config, config)

    assert config.image == image
    assert stub_manifest_probe.probes == []


@pytest.mark.parametrize(
    ("hit", "expected_image"),
    [
        (True, "mirror.example.com/rock/python:3.11"),
        (False, "python:3.11"),
    ],
)
async def test_probe_result_is_cached(stub_manifest_probe, hit, expected_image):
    rock_config = _make_rock_config([ImageRegistryMirror(registry="mirror.example.com", namespace="rock")])
    stub_manifest_probe.results.append(hit)
    first = DockerDeploymentConfig(image="python:3.11")
    second = DockerDeploymentConfig(image="python:3.11")

    await start_config.apply_image_registry_mirror(rock_config, first)
    await start_config.apply_image_registry_mirror(rock_config, second)

    assert first.image == expected_image
    assert second.image == expected_image
    assert len(stub_manifest_probe.probes) == 1


async def test_expired_cache_entry_reprobes(stub_manifest_probe):
    rock_config = _make_rock_config([ImageRegistryMirror(registry="mirror.example.com", namespace="rock")])
    stub_manifest_probe.results.extend([False, True])

    first = DockerDeploymentConfig(image="python:3.11")
    await start_config.apply_image_registry_mirror(rock_config, first)
    for candidate, (hit, _) in list(start_config.MIRROR_PROBE_CACHE.items()):
        start_config.MIRROR_PROBE_CACHE[candidate] = (hit, 0.0)

    second = DockerDeploymentConfig(image="python:3.11")
    await start_config.apply_image_registry_mirror(rock_config, second)

    assert second.image == "mirror.example.com/rock/python:3.11"
    assert len(stub_manifest_probe.probes) == 2


async def test_concurrent_requests_share_probe():
    rock_config = _make_rock_config([ImageRegistryMirror(registry="mirror.example.com", namespace="rock")])
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    probe_count = 0

    async def probe(rock_config, registry, repo, tag, username=None, password=None):
        nonlocal probe_count
        probe_count += 1
        probe_started.set()
        await release_probe.wait()
        return True

    first = DockerDeploymentConfig(image="python:3.11")
    second = DockerDeploymentConfig(image="python:3.11")
    with patch.object(start_config, "http_probe_manifest", probe):
        first_task = asyncio.create_task(start_config.apply_image_registry_mirror(rock_config, first))
        await probe_started.wait()
        second_task = asyncio.create_task(start_config.apply_image_registry_mirror(rock_config, second))
        await asyncio.sleep(0)
        release_probe.set()
        await asyncio.gather(first_task, second_task)

    assert probe_count == 1
    assert first.image == second.image == "mirror.example.com/rock/python:3.11"


def test_probe_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(start_config, "_MIRROR_PROBE_CACHE_MAX_ENTRIES", 2)

    start_config._probe_cache_set("first", True)
    start_config._probe_cache_set("second", True)
    start_config._probe_cache_set("third", True)

    assert list(start_config.MIRROR_PROBE_CACHE) == ["second", "third"]


async def test_probe_error_is_not_cached():
    rock_config = _make_rock_config([ImageRegistryMirror(registry="mirror.example.com", namespace="rock")])
    probe_count = 0

    async def probe(rock_config, registry, repo, tag, username=None, password=None):
        nonlocal probe_count
        probe_count += 1
        raise RuntimeError("connection failed")

    with patch.object(start_config, "http_probe_manifest", probe):
        await start_config.apply_image_registry_mirror(
            rock_config,
            DockerDeploymentConfig(image="python:3.11"),
        )
        await start_config.apply_image_registry_mirror(
            rock_config,
            DockerDeploymentConfig(image="python:3.11"),
        )

    assert probe_count == 2
    assert start_config.MIRROR_PROBE_CACHE == {}


@pytest.mark.parametrize(
    ("image", "expected_image", "expected_probes"),
    [
        ("swe-bench:case-1", "mirror.example.com/rock/swe-bench:case-1", 1),
        ("python:3.11", "python:3.11", 0),
    ],
)
async def test_prefix_allowlist(stub_manifest_probe, image, expected_image, expected_probes):
    rock_config = _make_rock_config(
        [ImageRegistryMirror(registry="mirror.example.com", namespace="rock")],
        allowlist=["swe-bench:"],
    )
    stub_manifest_probe.results.append(True)
    config = DockerDeploymentConfig(image=image)

    await start_config.apply_image_registry_mirror(rock_config, config)

    assert config.image == expected_image
    assert len(stub_manifest_probe.probes) == expected_probes


async def test_nacos_mirror_config_overrides_yaml(stub_manifest_probe):
    nacos_provider = MagicMock()
    nacos_provider.get_config = AsyncMock(
        return_value={
            "image_registry_mirrors": [
                {"registry": "nacos-mirror.example.com", "namespace": "nacos"},
            ],
            "image_mirror_lookup_allowlist": ["*"],
        }
    )
    rock_config = _make_rock_config(
        [ImageRegistryMirror(registry="yaml-mirror.example.com", namespace="yaml")],
        allowlist=["ignored:"],
        nacos_provider=nacos_provider,
    )
    stub_manifest_probe.results.append(True)
    config = DockerDeploymentConfig(image="python:3.11")

    await start_config.apply_image_registry_mirror(rock_config, config)

    assert config.image == "nacos-mirror.example.com/nacos/python:3.11"
