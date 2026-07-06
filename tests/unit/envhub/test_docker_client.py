import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from rock.sdk.envhub.docker import DockerClient
from rock.sdk.envhub.regionless.resolver import ROCK_REGISTRY_ENV, RockRegistryResolver


@pytest.fixture()
def resolver():
    r = RockRegistryResolver()
    yield r
    r.reset_cache()


@pytest.fixture()
def client(resolver):
    return DockerClient(resolver=resolver)


def _write_compose(path, services: dict):
    data = {"version": "3", "services": services}
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return path


# ======================================================================
# Regionless: resolve
# ======================================================================


class TestResolveImage:
    async def test_delegates_to_resolver(self, client, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            result = await client.resolve_image("ghcr.io/org/app:v1")
        assert result == "reg.example.com/org/app:v1"

    async def test_no_env_returns_original(self, client, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        result = await client.resolve_image("ghcr.io/org/app:v1")
        assert result == "ghcr.io/org/app:v1"


class TestResolveDockerfile:
    async def test_delegates_to_resolver(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        df = tmp_path / "Dockerfile"
        df.write_text("FROM ghcr.io/org/app:v1\nRUN echo hello\n")
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            changed = await client.resolve_dockerfile(df)
        assert changed
        assert "FROM reg.example.com/org/app:v1" in df.read_text()


class TestResolveCompose:
    async def test_rewrites_service_image(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {"web": {"image": "ghcr.io/org/app:v1"}},
        )
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            changed = await client.resolve_compose(cf)
        assert changed
        result = yaml.safe_load(cf.read_text())
        assert result["services"]["web"]["image"] == "reg.example.com/org/app:v1"

    async def test_no_change_on_miss(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {"web": {"image": "ghcr.io/org/app:v1"}},
        )
        original = cf.read_text()
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=False),
        ):
            changed = await client.resolve_compose(cf)
        assert not changed
        assert cf.read_text() == original

    async def test_no_env_noop(self, client, tmp_path, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {"web": {"image": "ghcr.io/org/app:v1"}},
        )
        original = cf.read_text()
        changed = await client.resolve_compose(cf)
        assert not changed
        assert cf.read_text() == original

    async def test_dedupes_probe(self, client, tmp_path, monkeypatch):
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
            changed = await client.resolve_compose(cf)
        assert changed
        assert probe.await_count == 1

    async def test_ignores_build_section(self, client, tmp_path, monkeypatch):
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
            changed = await client.resolve_compose(cf)
        assert changed
        data = yaml.safe_load(cf.read_text())
        assert "build" in data["services"]["builder"]
        assert "image" not in data["services"]["builder"]
        assert data["services"]["web"]["image"] == "reg.example.com/org/app:v1"
        assert probe.await_count == 1


# ======================================================================
# Regionless: resolve + pull
# ======================================================================


class TestPullImage:
    async def test_resolve_and_pull(self, client, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        with (
            patch.object(
                RockRegistryResolver,
                "_http_probe_manifest",
                new=AsyncMock(return_value=True),
            ),
            patch("rock.sdk.envhub.docker.asyncio.create_subprocess_exec") as mock_exec,
        ):
            proc_mock = AsyncMock()
            proc_mock.returncode = 0
            proc_mock.communicate.return_value = (b"Pulled\n", b"")
            mock_exec.return_value = proc_mock

            result = await client.pull_image("ghcr.io/org/app:v1")

        assert result.returncode == 0
        cmd = mock_exec.call_args[0]
        assert cmd == ("docker", "pull", "reg.example.com/org/app:v1")

    async def test_resolve_failure_pulls_original(self, client, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        with (
            patch.object(
                RockRegistryResolver,
                "resolve_image",
                new=AsyncMock(side_effect=Exception("probe boom")),
            ),
            patch("rock.sdk.envhub.docker.asyncio.create_subprocess_exec") as mock_exec,
        ):
            proc_mock = AsyncMock()
            proc_mock.returncode = 0
            proc_mock.communicate.return_value = (b"Pulled\n", b"")
            mock_exec.return_value = proc_mock

            result = await client.pull_image("ghcr.io/org/app:v1")

        assert result.returncode == 0
        cmd = mock_exec.call_args[0]
        assert cmd == ("docker", "pull", "ghcr.io/org/app:v1")

    async def test_pull_failure_raises(self, client, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        with patch("rock.sdk.envhub.docker.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.returncode = 1
            proc_mock.communicate.return_value = (b"", b"Error: pull access denied\n")
            mock_exec.return_value = proc_mock

            with pytest.raises(RuntimeError, match="docker pull failed"):
                await client.pull_image("ghcr.io/org/app:v1")


class TestPullCompose:
    async def test_resolve_then_pull(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {"web": {"image": "ghcr.io/org/app:v1"}},
        )
        with (
            patch.object(
                RockRegistryResolver,
                "_http_probe_manifest",
                new=AsyncMock(return_value=True),
            ),
            patch("rock.sdk.envhub.docker.asyncio.create_subprocess_exec") as mock_exec,
        ):
            proc_mock = AsyncMock()
            proc_mock.returncode = 0
            proc_mock.communicate.return_value = (b"Done\n", b"")
            mock_exec.return_value = proc_mock

            result = await client.pull_compose(cf)

        assert result.returncode == 0

    async def test_propagates_pull_failure(self, client, tmp_path, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {"web": {"image": "ghcr.io/org/app:v1"}},
        )
        with patch("rock.sdk.envhub.docker.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.returncode = 1
            proc_mock.communicate.return_value = (b"", b"Error: pull access denied\n")
            mock_exec.return_value = proc_mock

            with pytest.raises(RuntimeError, match="docker compose pull failed"):
                await client.pull_compose(cf)

    async def test_resolve_failure_is_non_blocking(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        cf = _write_compose(
            tmp_path / "docker-compose.yml",
            {"web": {"image": "ghcr.io/org/app:v1"}},
        )
        with (
            patch.object(
                DockerClient,
                "_resolve_compose_file",
                new=AsyncMock(side_effect=Exception("resolve boom")),
            ),
            patch("rock.sdk.envhub.docker.asyncio.create_subprocess_exec") as mock_exec,
        ):
            proc_mock = AsyncMock()
            proc_mock.returncode = 0
            proc_mock.communicate.return_value = (b"Done\n", b"")
            mock_exec.return_value = proc_mock

            result = await client.pull_compose(cf)

        assert result.returncode == 0


# ======================================================================
# Docker: authentication
# ======================================================================


class TestLogin:
    async def test_delegates_to_docker_util(self, client):
        with patch("rock.sdk.envhub.docker.DockerUtil.login", return_value="Login Succeeded") as mock_login:
            result = await client.login("reg.example.com", "user", "pass")
        assert result == "Login Succeeded"
        mock_login.assert_called_once_with("reg.example.com", "user", "pass", 30)

    async def test_custom_timeout(self, client):
        with patch("rock.sdk.envhub.docker.DockerUtil.login", return_value="ok") as mock_login:
            await client.login("reg.example.com", "user", "pass", timeout=60)
        mock_login.assert_called_once_with("reg.example.com", "user", "pass", 60)


class TestLogout:
    async def test_delegates_to_docker_util(self, client):
        with patch("rock.sdk.envhub.docker.DockerUtil.logout", return_value="Logged out") as mock_logout:
            result = await client.logout("reg.example.com")
        assert result == "Logged out"
        mock_logout.assert_called_once_with("reg.example.com", 30)


# ======================================================================
# Docker: build & push
# ======================================================================


class TestBuild:
    async def test_delegates_to_docker_command(self, client):
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(client._docker_cmd, "buildx_build", return_value=mock_result) as mock_build:
            result = await client.build("Dockerfile", "/ctx", "myapp:v1")
        assert result.returncode == 0
        mock_build.assert_called_once_with("Dockerfile", "/ctx", "--tag", "myapp:v1")

    async def test_with_extra_args(self, client):
        mock_result = MagicMock(returncode=0)
        with patch.object(client._docker_cmd, "buildx_build", return_value=mock_result) as mock_build:
            await client.build("Dockerfile", "/ctx", "myapp:v1", "--no-cache", "--platform=linux/amd64")
        mock_build.assert_called_once_with(
            "Dockerfile", "/ctx", "--tag", "myapp:v1", "--no-cache", "--platform=linux/amd64"
        )


class TestPush:
    async def test_delegates_to_docker_command(self, client):
        mock_result = MagicMock(returncode=0)
        with patch.object(client._docker_cmd, "push_image", return_value=mock_result) as mock_push:
            result = await client.push("reg.example.com/ns/app:v1")
        assert result.returncode == 0
        mock_push.assert_called_once_with("reg.example.com/ns/app:v1")


# ======================================================================
# Docker: mirror
# ======================================================================


class TestMirror:
    async def test_login_pull_tag_push_sequence(self, client, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        calls: list[str] = []

        async def _login(self, reg, user, pwd, *, timeout=30):
            calls.append(f"login:{reg}")
            return "ok"

        async def _pull(self, image):
            calls.append(f"pull:{image}")
            return b"Pulled"

        async def _tag(self, src, dst):
            calls.append(f"tag:{src}->{dst}")

        async def _push(self, t):
            calls.append(f"push:{t}")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            patch.object(DockerClient, "login", _login),
            patch.object(DockerClient, "_pull", _pull),
            patch.object(DockerClient, "_tag", _tag),
            patch.object(DockerClient, "push", _push),
        ):
            target = await client.mirror(
                "ghcr.io/org/app:v1",
                "reg.aliyuncs.com/ns",
                target_username="u",
                target_password="p",
            )

        assert target == "reg.aliyuncs.com/ns/org/app:v1"
        assert calls == [
            "login:reg.aliyuncs.com/ns",
            "pull:ghcr.io/org/app:v1",
            "tag:ghcr.io/org/app:v1->reg.aliyuncs.com/ns/org/app:v1",
            "push:reg.aliyuncs.com/ns/org/app:v1",
        ]

    async def test_mirror_with_source_credentials(self, client, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        logins: list[str] = []

        async def _login(self, reg, user, pwd, *, timeout=30):
            logins.append(reg)
            return "ok"

        async def _noop_pull(self, image):
            return b"Pulled"

        async def _noop_tag(self, src, dst):
            pass

        async def _noop_push(self, t):
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            patch.object(DockerClient, "login", _login),
            patch.object(DockerClient, "_pull", _noop_pull),
            patch.object(DockerClient, "_tag", _noop_tag),
            patch.object(DockerClient, "push", _noop_push),
        ):
            await client.mirror(
                "ghcr.io/org/app:v1",
                "reg.aliyuncs.com/ns",
                target_username="tu",
                target_password="tp",
                source_registry="ghcr.io",
                source_username="su",
                source_password="sp",
            )

        assert logins == ["reg.aliyuncs.com/ns", "ghcr.io"]


# ======================================================================
# Construction
# ======================================================================


class TestConstruction:
    async def test_uses_injected_resolver(self):
        custom = RockRegistryResolver()
        c = DockerClient(resolver=custom)
        assert c._resolver is custom

    async def test_default_resolver(self):
        c = DockerClient()
        assert isinstance(c._resolver, RockRegistryResolver)

    async def test_custom_docker_executable(self):
        c = DockerClient(docker_executable="/usr/local/bin/docker")
        assert c._docker_executable == "/usr/local/bin/docker"
        assert c._docker_cmd.docker_executable == "/usr/local/bin/docker"

    async def test_registries_param_forwarded_to_resolver(self):
        c = DockerClient(registries=["reg.example.com/ns"])
        assert c._resolver._registries == ["reg.example.com/ns"]

    async def test_registries_param_used_for_resolve(self, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        c = DockerClient(registries=["reg.example.com/ns"])
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            result = await c.resolve_image("ghcr.io/org/app:v1")
        assert result == "reg.example.com/org/app:v1"

    async def test_resolver_param_overrides_registries(self):
        custom = RockRegistryResolver(registries=["custom.example.com/ns"])
        c = DockerClient(resolver=custom, registries=["ignored.example.com/ns"])
        assert c._resolver is custom
        assert c._resolver._registries == ["custom.example.com/ns"]

    async def test_no_registries_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "env-reg.example.com/ns")
        c = DockerClient()
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            result = await c.resolve_image("ghcr.io/org/app:v1")
        assert result.startswith("env-reg.example.com/")
