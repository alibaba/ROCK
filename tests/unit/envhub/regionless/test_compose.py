from unittest.mock import AsyncMock, patch

import pytest
import yaml

from rock.sdk.envhub.regionless.compose import compose_pull, resolve_compose
from rock.sdk.envhub.regionless.resolver import ROCK_REGISTRY_ENV, RockRegistryResolver


@pytest.fixture()
def resolver():
    r = RockRegistryResolver()
    yield r
    r.reset_cache()


def _write_compose(path, services: dict):
    data = {"version": "3", "services": services}
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return path


class TestResolveCompose:
    async def test_rewrites_service_image(self, tmp_path, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {
                "web": {"image": "ghcr.io/org/app:v1", "ports": ["8080:80"]},
            },
        )
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            changed = await resolve_compose(cf, resolver=resolver)
        assert changed
        data = yaml.safe_load(cf.read_text())
        assert data["services"]["web"]["image"] == "reg.example.com/org/app:v1"

    async def test_no_change_on_miss(self, tmp_path, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {
                "web": {"image": "ghcr.io/org/app:v1"},
            },
        )
        original = cf.read_text()
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=False),
        ):
            changed = await resolve_compose(cf, resolver=resolver)
        assert not changed
        assert cf.read_text() == original

    async def test_no_env_noop(self, tmp_path, resolver, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {
                "web": {"image": "ghcr.io/org/app:v1"},
            },
        )
        original = cf.read_text()
        changed = await resolve_compose(cf, resolver=resolver)
        assert not changed
        assert cf.read_text() == original

    async def test_dedupes_probe(self, tmp_path, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {
                "svc1": {"image": "ghcr.io/org/app:v1"},
                "svc2": {"image": "ghcr.io/org/app:v1"},
            },
        )
        probe = AsyncMock(return_value=True)
        with patch.object(RockRegistryResolver, "_http_probe_manifest", new=probe):
            changed = await resolve_compose(cf, resolver=resolver)
        assert changed
        assert probe.await_count == 1

    async def test_ignores_build_section(self, tmp_path, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {
                "builder": {"build": {"context": ".", "dockerfile": "Dockerfile"}},
                "web": {"image": "ghcr.io/org/app:v1"},
            },
        )
        probe = AsyncMock(return_value=True)
        with patch.object(RockRegistryResolver, "_http_probe_manifest", new=probe):
            changed = await resolve_compose(cf, resolver=resolver)
        assert changed
        data = yaml.safe_load(cf.read_text())
        assert "build" in data["services"]["builder"]
        assert "image" not in data["services"]["builder"]
        assert data["services"]["web"]["image"] == "reg.example.com/org/app:v1"
        assert probe.await_count == 1


class TestComposePull:
    async def test_calls_resolve_then_pull(self, tmp_path, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {
                "web": {"image": "ghcr.io/org/app:v1"},
            },
        )

        with (
            patch.object(
                RockRegistryResolver,
                "_http_probe_manifest",
                new=AsyncMock(return_value=True),
            ),
            patch("rock.sdk.envhub.regionless.compose.asyncio.create_subprocess_exec") as mock_exec,
        ):
            proc_mock = AsyncMock()
            proc_mock.returncode = 0
            proc_mock.communicate.return_value = (b"Done\n", b"")
            mock_exec.return_value = proc_mock

            result = await compose_pull(cf, resolver=resolver)

        assert result.returncode == 0
        cmd = mock_exec.call_args[0]
        assert "docker" in cmd
        assert "pull" in cmd

    async def test_propagates_pull_failure(self, tmp_path, resolver, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {
                "web": {"image": "ghcr.io/org/app:v1"},
            },
        )

        with patch("rock.sdk.envhub.regionless.compose.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.returncode = 1
            proc_mock.communicate.return_value = (b"", b"Error: pull access denied\n")
            mock_exec.return_value = proc_mock

            with pytest.raises(RuntimeError, match="docker compose pull failed"):
                await compose_pull(cf, resolver=resolver)

    async def test_resolve_failure_is_non_blocking(self, tmp_path, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {
                "web": {"image": "ghcr.io/org/app:v1"},
            },
        )

        with (
            patch(
                "rock.sdk.envhub.regionless.compose.resolve_compose",
                new=AsyncMock(side_effect=Exception("resolve boom")),
            ),
            patch("rock.sdk.envhub.regionless.compose.asyncio.create_subprocess_exec") as mock_exec,
        ):
            proc_mock = AsyncMock()
            proc_mock.returncode = 0
            proc_mock.communicate.return_value = (b"Done\n", b"")
            mock_exec.return_value = proc_mock

            result = await compose_pull(cf, resolver=resolver)

        assert result.returncode == 0
