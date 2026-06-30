"""Tests for _apply_image_registry_mirror in admin sandbox entrypoint.

Covers: empty list, single hit, second-mirror hit, full miss, credential
propagation (with and without auth), invalid mirror entries, name:tag
extraction from multi-segment image paths, and tolerance of probe errors.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.admin.entrypoints import sandbox_api
from rock.config import ImageRegistryMirror, RockConfig
from rock.deployments.config import DockerDeploymentConfig


def _make_manager(mirrors, allowlist=None):
    """Build a stub sandbox_manager exposing mirrors + allowlist.

    Default allowlist is ``["*"]`` so callers that don't care about the gate
    keep the legacy "check every image" semantics.
    """
    return SimpleNamespace(
        rock_config=SimpleNamespace(
            image_registry_mirrors=mirrors,
            image_mirror_lookup_allowlist=["*"] if allowlist is None else allowlist,
            nacos_provider=None,
        )
    )


@pytest.fixture(autouse=True)
def clear_probe_cache():
    """Mirror probe cache is process-local — wipe between tests to avoid cross-test leakage."""
    sandbox_api._MIRROR_PROBE_CACHE.clear()
    yield
    sandbox_api._MIRROR_PROBE_CACHE.clear()


@pytest.fixture
def restore_sandbox_manager():
    """Save / restore the module-level sandbox_manager singleton around each test."""
    original = getattr(sandbox_api, "sandbox_manager", None)
    yield
    sandbox_api.sandbox_manager = original


@pytest.fixture
def stub_manifest_probe():
    """Replace _http_probe_manifest with a stub that records probes and returns pre-set results."""
    probes: list[dict] = []
    probe_results: list[bool] = []

    async def _mock(registry, repo, tag, username=None, password=None, timeout=5):
        probes.append(
            {
                "image": f"{registry}/{repo}:{tag}",
                "registry": registry,
                "repo": repo,
                "tag": tag,
                "username": username,
                "password": password,
            }
        )
        return probe_results.pop(0) if probe_results else False

    with patch.object(sandbox_api, "_http_probe_manifest", _mock):
        yield SimpleNamespace(probes=probes, probe_results=probe_results)


async def test_empty_mirror_list_is_noop(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager([])
    config = DockerDeploymentConfig(image="python:3.11")

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "python:3.11"
    assert stub_manifest_probe.probes == []


async def test_first_mirror_hit_rewrites_image(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [
            ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public"),
            ImageRegistryMirror(registry="rock-b.example.com", namespace="rock-mirror"),
        ]
    )
    config = DockerDeploymentConfig(image="gcr.io/foo/python:3.11")
    # First probe: registry-only (preserve original namespace "foo") -> hit
    stub_manifest_probe.probe_results.append(True)

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "rock-a.example.com/foo/python:3.11"
    assert config.registry_username is None
    assert config.registry_password is None
    assert len(stub_manifest_probe.probes) == 1
    assert stub_manifest_probe.probes[0]["repo"] == "foo/python"


async def test_registry_only_miss_falls_back_to_namespace_replace(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public")]
    )
    config = DockerDeploymentConfig(image="gcr.io/foo/python:3.11")
    # First probe: registry-only (foo/python) -> miss
    # Second probe: registry+namespace (rock-public/python) -> hit
    stub_manifest_probe.probe_results.extend([False, True])

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "rock-a.example.com/rock-public/python:3.11"
    assert len(stub_manifest_probe.probes) == 2
    assert stub_manifest_probe.probes[0]["repo"] == "foo/python"
    assert stub_manifest_probe.probes[1]["repo"] == "rock-public/python"


async def test_original_namespace_equals_mirror_namespace_deduplicates(restore_sandbox_manager, stub_manifest_probe):
    """When original namespace matches mirror namespace, only one candidate is probed (no redundant request)."""
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="foo")]
    )
    config = DockerDeploymentConfig(image="gcr.io/foo/python:3.11")
    stub_manifest_probe.probe_results.append(True)

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "rock-a.example.com/foo/python:3.11"
    assert len(stub_manifest_probe.probes) == 1
    assert stub_manifest_probe.probes[0]["repo"] == "foo/python"


async def test_second_mirror_hit_after_first_miss(restore_sandbox_manager, stub_manifest_probe):
    """No original namespace (bare image) — only namespace-replace candidates are tried."""
    sandbox_api.sandbox_manager = _make_manager(
        [
            ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public"),
            ImageRegistryMirror(registry="rock-b.example.com", namespace="rock-mirror"),
        ]
    )
    config = DockerDeploymentConfig(image="python:3.11")
    stub_manifest_probe.probe_results.extend([False, True])

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "rock-b.example.com/rock-mirror/python:3.11"
    assert len(stub_manifest_probe.probes) == 2


async def test_full_miss_keeps_original_image(restore_sandbox_manager, stub_manifest_probe):
    """No original namespace — each mirror has 1 candidate, both miss."""
    sandbox_api.sandbox_manager = _make_manager(
        [
            ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public"),
            ImageRegistryMirror(registry="rock-b.example.com", namespace="rock-mirror"),
        ]
    )
    config = DockerDeploymentConfig(image="python:3.11", registry_username="orig", registry_password="orig-pw")
    stub_manifest_probe.probe_results.extend([False, False])

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "python:3.11"
    assert config.registry_username == "orig"
    assert config.registry_password == "orig-pw"


async def test_credentials_propagated_on_hit(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [
            ImageRegistryMirror(
                registry="rock-a.example.com",
                namespace="rock-public",
                username="mirror-user",
                password="mirror-pw",
            ),
        ]
    )
    config = DockerDeploymentConfig(image="python:3.11")
    stub_manifest_probe.probe_results.append(True)

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "rock-a.example.com/rock-public/python:3.11"
    assert config.registry_username == "mirror-user"
    assert config.registry_password == "mirror-pw"
    assert stub_manifest_probe.probes[0]["username"] == "mirror-user"
    assert stub_manifest_probe.probes[0]["registry"] == "rock-a.example.com"


async def test_user_credentials_cleared_when_mirror_has_no_auth(restore_sandbox_manager, stub_manifest_probe):
    """User-supplied registry credentials must not leak to a mirror that needs no auth."""
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public")]
    )
    config = DockerDeploymentConfig(image="my-registry.io/myimage:v1", registry_username="orig-user", registry_password="orig-pw")
    stub_manifest_probe.probe_results.append(True)

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "rock-a.example.com/rock-public/myimage:v1"
    assert config.registry_username is None
    assert config.registry_password is None


async def test_user_credentials_replaced_by_mirror_credentials(restore_sandbox_manager, stub_manifest_probe):
    """When both user and mirror have credentials, the mirror's credentials must win."""
    sandbox_api.sandbox_manager = _make_manager(
        [
            ImageRegistryMirror(
                registry="rock-a.example.com",
                namespace="rock-public",
                username="mirror-user",
                password="mirror-pw",
            ),
        ]
    )
    config = DockerDeploymentConfig(image="my-registry.io/myimage:v1", registry_username="orig-user", registry_password="orig-pw")
    stub_manifest_probe.probe_results.append(True)

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "rock-a.example.com/rock-public/myimage:v1"
    assert config.registry_username == "mirror-user"
    assert config.registry_password == "mirror-pw"


async def test_invalid_mirror_entry_skipped(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [
            ImageRegistryMirror(registry="", namespace="rock-public"),
            ImageRegistryMirror(registry="rock-a.example.com", namespace=""),
            ImageRegistryMirror(registry="rock-b.example.com", namespace="rock-mirror"),
        ]
    )
    config = DockerDeploymentConfig(image="python:3.11")
    stub_manifest_probe.probe_results.append(True)

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "rock-b.example.com/rock-mirror/python:3.11"
    assert len(stub_manifest_probe.probes) == 1


async def test_candidate_strips_registry_and_namespace_preserves_repo(restore_sandbox_manager, stub_manifest_probe):
    """With original namespace 'project', first probe keeps it, second uses mirror namespace."""
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public")]
    )
    config = DockerDeploymentConfig(image="gcr.io/project/subdir/myimage:v1")
    # First probe: registry-only (project/subdir/myimage) -> miss
    # Second probe: registry+namespace (rock-public/subdir/myimage) -> miss
    stub_manifest_probe.probe_results.extend([False, False])

    await sandbox_api._apply_image_registry_mirror(config)

    assert stub_manifest_probe.probes[0]["image"] == "rock-a.example.com/project/subdir/myimage:v1"
    assert stub_manifest_probe.probes[0]["repo"] == "project/subdir/myimage"
    assert stub_manifest_probe.probes[0]["tag"] == "v1"
    assert stub_manifest_probe.probes[1]["image"] == "rock-a.example.com/rock-public/subdir/myimage:v1"
    assert stub_manifest_probe.probes[1]["repo"] == "rock-public/subdir/myimage"


async def test_missing_tag_defaults_to_latest(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public")]
    )
    config = DockerDeploymentConfig(image="ubuntu")
    stub_manifest_probe.probe_results.append(False)

    await sandbox_api._apply_image_registry_mirror(config)

    assert stub_manifest_probe.probes[0]["image"] == "rock-a.example.com/rock-public/ubuntu:latest"


async def test_probe_exception_treated_as_miss(restore_sandbox_manager):
    sandbox_api.sandbox_manager = _make_manager(
        [
            ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public", username="u", password="p"),
            ImageRegistryMirror(registry="rock-b.example.com", namespace="rock-mirror"),
        ]
    )
    config = DockerDeploymentConfig(image="python:3.11")

    call_count = {"n": 0}

    async def _mock(registry, repo, tag, username=None, password=None, timeout=5):
        if registry == "rock-a.example.com":
            raise RuntimeError("connection failed")
        call_count["n"] += 1
        return True

    with patch.object(sandbox_api, "_http_probe_manifest", _mock):
        await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "rock-b.example.com/rock-mirror/python:3.11"
    assert call_count["n"] == 1


async def test_digest_reference_skips_mirror_lookup(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public")]
    )
    config = DockerDeploymentConfig(image="gcr.io/foo/python@sha256:abc123def")

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "gcr.io/foo/python@sha256:abc123def"
    assert stub_manifest_probe.probes == []


async def test_digest_reference_without_registry_also_skips(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public")]
    )
    config = DockerDeploymentConfig(image="python@sha256:abc123def")

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "python@sha256:abc123def"
    assert stub_manifest_probe.probes == []


async def test_cached_hit_skips_probe(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public")]
    )
    stub_manifest_probe.probe_results.append(True)

    first = DockerDeploymentConfig(image="python:3.11")
    await sandbox_api._apply_image_registry_mirror(first)
    second = DockerDeploymentConfig(image="python:3.11")
    await sandbox_api._apply_image_registry_mirror(second)

    assert first.image == "rock-a.example.com/rock-public/python:3.11"
    assert second.image == "rock-a.example.com/rock-public/python:3.11"
    assert len(stub_manifest_probe.probes) == 1


async def test_cached_miss_skips_probe(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [
            ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public"),
            ImageRegistryMirror(registry="rock-b.example.com", namespace="rock-mirror"),
        ]
    )
    stub_manifest_probe.probe_results.extend([False, False])

    first = DockerDeploymentConfig(image="python:3.11")
    await sandbox_api._apply_image_registry_mirror(first)
    second = DockerDeploymentConfig(image="python:3.11")
    await sandbox_api._apply_image_registry_mirror(second)

    assert first.image == "python:3.11"
    assert second.image == "python:3.11"
    assert len(stub_manifest_probe.probes) == 2


async def test_expired_cache_entry_reprobes(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public")]
    )
    stub_manifest_probe.probe_results.extend([False, True])

    first = DockerDeploymentConfig(image="python:3.11")
    await sandbox_api._apply_image_registry_mirror(first)
    for key in list(sandbox_api._MIRROR_PROBE_CACHE):
        hit, _ = sandbox_api._MIRROR_PROBE_CACHE[key]
        sandbox_api._MIRROR_PROBE_CACHE[key] = (hit, 0.0)
    second = DockerDeploymentConfig(image="python:3.11")
    await sandbox_api._apply_image_registry_mirror(second)

    assert second.image == "rock-a.example.com/rock-public/python:3.11"
    assert len(stub_manifest_probe.probes) == 2


async def test_probe_exception_not_cached(restore_sandbox_manager):
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public", username="u", password="p")]
    )
    call_count = {"n": 0}

    async def _mock(registry, repo, tag, username=None, password=None, timeout=5):
        call_count["n"] += 1
        raise RuntimeError("connection failed")

    with patch.object(sandbox_api, "_http_probe_manifest", _mock):
        await sandbox_api._apply_image_registry_mirror(DockerDeploymentConfig(image="python:3.11"))
        await sandbox_api._apply_image_registry_mirror(DockerDeploymentConfig(image="python:3.11"))

    assert call_count["n"] == 2
    assert sandbox_api._MIRROR_PROBE_CACHE == {}


async def test_empty_allowlist_disables_lookup_entirely(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public")],
        allowlist=[],
    )
    stub_manifest_probe.probe_results.append(True)
    config = DockerDeploymentConfig(image="python:3.11")

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "python:3.11"
    assert stub_manifest_probe.probes == []


async def test_wildcard_allowlist_lets_every_image_through(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public")],
        allowlist=["*"],
    )
    # First probe: registry-only (foo/python) -> hit
    stub_manifest_probe.probe_results.append(True)
    config = DockerDeploymentConfig(image="gcr.io/foo/python:3.11")

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "rock-a.example.com/foo/python:3.11"
    assert len(stub_manifest_probe.probes) == 1


async def test_prefix_allowlist_matches_only_listed_images(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public")],
        allowlist=["aaaaaa/bbb/", "swe-bench:"],
    )
    # "aaaaaa/bbb/swe-bench:..." has original_namespace="aaaaaa":
    #   1st probe: registry-only (aaaaaa/bbb/swe-bench) -> hit
    stub_manifest_probe.probe_results.append(True)

    allowed = DockerDeploymentConfig(image="aaaaaa/bbb/swe-bench:astropy__astropy-12907")
    await sandbox_api._apply_image_registry_mirror(allowed)
    assert allowed.image == "rock-a.example.com/aaaaaa/bbb/swe-bench:astropy__astropy-12907"

    # "swe-bench:..." has no namespace -> only namespace-replace candidate
    stub_manifest_probe.probe_results.append(True)
    bare_prefix = DockerDeploymentConfig(image="swe-bench:python__python-1")
    await sandbox_api._apply_image_registry_mirror(bare_prefix)
    assert bare_prefix.image == "rock-a.example.com/rock-public/swe-bench:python__python-1"

    assert len(stub_manifest_probe.probes) == 2


async def test_prefix_allowlist_skips_non_matching_image(restore_sandbox_manager, stub_manifest_probe):
    sandbox_api.sandbox_manager = _make_manager(
        [ImageRegistryMirror(registry="rock-a.example.com", namespace="rock-public")],
        allowlist=["swe-bench:"],
    )
    stub_manifest_probe.probe_results.append(True)
    config = DockerDeploymentConfig(image="python:3.11")

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "python:3.11"
    assert stub_manifest_probe.probes == []


async def test_nacos_mirrors_override_rock_config(restore_sandbox_manager, stub_manifest_probe):
    """When nacos_provider returns mirror config, it takes precedence over rock_config fields."""
    nacos_provider = MagicMock()
    nacos_provider.get_config = AsyncMock(
        return_value={
            "image_registry_mirrors": [
                {"registry": "nacos-mirror.example.com", "namespace": "nacos-ns"},
            ],
            "image_mirror_lookup_allowlist": ["*"],
        }
    )
    sandbox_api.sandbox_manager = SimpleNamespace(
        rock_config=SimpleNamespace(
            image_registry_mirrors=[ImageRegistryMirror(registry="yaml-mirror.example.com", namespace="yaml-ns")],
            image_mirror_lookup_allowlist=["should-be-ignored:"],
            nacos_provider=nacos_provider,
        )
    )
    stub_manifest_probe.probe_results.append(True)
    config = DockerDeploymentConfig(image="python:3.11")

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "nacos-mirror.example.com/nacos-ns/python:3.11"


async def test_nacos_without_mirror_keys_falls_back_to_rock_config(restore_sandbox_manager, stub_manifest_probe):
    """When nacos_provider has no mirror keys, fall back to rock_config fields."""
    nacos_provider = MagicMock()
    nacos_provider.get_config = AsyncMock(return_value={"sandbox_config": {}})
    sandbox_api.sandbox_manager = SimpleNamespace(
        rock_config=SimpleNamespace(
            image_registry_mirrors=[ImageRegistryMirror(registry="yaml-mirror.example.com", namespace="yaml-ns")],
            image_mirror_lookup_allowlist=["*"],
            nacos_provider=nacos_provider,
        )
    )
    stub_manifest_probe.probe_results.append(True)
    config = DockerDeploymentConfig(image="python:3.11")

    await sandbox_api._apply_image_registry_mirror(config)

    assert config.image == "yaml-mirror.example.com/yaml-ns/python:3.11"


def test_image_registry_mirror_field_default_empty():
    assert RockConfig.__dataclass_fields__["image_registry_mirrors"].default_factory() == []


def test_image_mirror_lookup_allowlist_field_default_empty():
    assert RockConfig.__dataclass_fields__["image_mirror_lookup_allowlist"].default_factory() == []
