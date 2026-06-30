import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from rock.sdk.envhub.docker import DockerFacade
from rock.sdk.envhub.regionless.resolver import ROCK_REGISTRY_ENV, RockRegistryResolver


@pytest.fixture()
def resolver():
    r = RockRegistryResolver()
    yield r
    r.reset_cache()


@pytest.fixture()
def facade(resolver):
    return DockerFacade(resolver=resolver)


class TestResolveImage:
    async def test_delegates_to_resolver(self, facade, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            result = await facade.resolve_image("ghcr.io/org/app:v1")
        assert result == "reg.example.com/org/app:v1"

    async def test_no_env_returns_original(self, facade, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        result = await facade.resolve_image("ghcr.io/org/app:v1")
        assert result == "ghcr.io/org/app:v1"


class TestPullImage:
    async def test_resolve_and_pull(self, facade, monkeypatch):
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

            result = await facade.pull_image("ghcr.io/org/app:v1")

        assert result.returncode == 0
        cmd = mock_exec.call_args[0]
        assert cmd == ("docker", "pull", "reg.example.com/org/app:v1")

    async def test_resolve_failure_pulls_original(self, facade, monkeypatch):
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

            result = await facade.pull_image("ghcr.io/org/app:v1")

        assert result.returncode == 0
        cmd = mock_exec.call_args[0]
        assert cmd == ("docker", "pull", "ghcr.io/org/app:v1")

    async def test_pull_failure_raises(self, facade, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        with patch("rock.sdk.envhub.docker.asyncio.create_subprocess_exec") as mock_exec:
            proc_mock = AsyncMock()
            proc_mock.returncode = 1
            proc_mock.communicate.return_value = (b"", b"Error: pull access denied\n")
            mock_exec.return_value = proc_mock

            with pytest.raises(RuntimeError, match="docker pull failed"):
                await facade.pull_image("ghcr.io/org/app:v1")


class TestResolveDockerfile:
    async def test_delegates_to_resolver(self, facade, tmp_path, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        df = tmp_path / "Dockerfile"
        df.write_text("FROM ghcr.io/org/app:v1\nRUN echo hello\n")
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            changed = await facade.resolve_dockerfile(df)
        assert changed
        assert "FROM reg.example.com/org/app:v1" in df.read_text()


class TestResolveCompose:
    async def test_delegates_to_compose_module(self, facade, tmp_path, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        cf = tmp_path / "docker-compose.yml"
        data = {"version": "3", "services": {"web": {"image": "ghcr.io/org/app:v1"}}}
        cf.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            changed = await facade.resolve_compose(cf)
        assert changed
        result = yaml.safe_load(cf.read_text())
        assert result["services"]["web"]["image"] == "reg.example.com/org/app:v1"


class TestPullCompose:
    async def test_delegates_to_compose_module(self, facade, tmp_path, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        cf = tmp_path / "docker-compose.yml"
        data = {"version": "3", "services": {"web": {"image": "ghcr.io/org/app:v1"}}}
        cf.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
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

            result = await facade.pull_compose(cf)

        assert result.returncode == 0


class TestLogin:
    async def test_delegates_to_docker_util(self, facade):
        with patch("rock.sdk.envhub.docker.DockerUtil.login", return_value="Login Succeeded") as mock_login:
            result = await facade.login("reg.example.com", "user", "pass")
        assert result == "Login Succeeded"
        mock_login.assert_called_once_with("reg.example.com", "user", "pass", 30)

    async def test_custom_timeout(self, facade):
        with patch("rock.sdk.envhub.docker.DockerUtil.login", return_value="ok") as mock_login:
            await facade.login("reg.example.com", "user", "pass", timeout=60)
        mock_login.assert_called_once_with("reg.example.com", "user", "pass", 60)


class TestLogout:
    async def test_delegates_to_docker_util(self, facade):
        with patch("rock.sdk.envhub.docker.DockerUtil.logout", return_value="Logged out") as mock_logout:
            result = await facade.logout("reg.example.com")
        assert result == "Logged out"
        mock_logout.assert_called_once_with("reg.example.com", 30)


class TestBuild:
    async def test_delegates_to_docker_command(self, facade):
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(facade._docker_cmd, "buildx_build", return_value=mock_result) as mock_build:
            result = await facade.build("Dockerfile", "/ctx", "myapp:v1")
        assert result.returncode == 0
        mock_build.assert_called_once_with("Dockerfile", "/ctx", "--tag", "myapp:v1")

    async def test_with_extra_args(self, facade):
        mock_result = MagicMock(returncode=0)
        with patch.object(facade._docker_cmd, "buildx_build", return_value=mock_result) as mock_build:
            await facade.build("Dockerfile", "/ctx", "myapp:v1", "--no-cache", "--platform=linux/amd64")
        mock_build.assert_called_once_with(
            "Dockerfile", "/ctx", "--tag", "myapp:v1", "--no-cache", "--platform=linux/amd64"
        )


class TestPush:
    async def test_delegates_to_docker_command(self, facade):
        mock_result = MagicMock(returncode=0)
        with patch.object(facade._docker_cmd, "push_image", return_value=mock_result) as mock_push:
            result = await facade.push("reg.example.com/ns/app:v1")
        assert result.returncode == 0
        mock_push.assert_called_once_with("reg.example.com/ns/app:v1")


class TestTag:
    async def test_tags_image(self, facade):
        with patch("rock.sdk.envhub.docker.asyncio.create_subprocess_exec") as mock_exec:
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate.return_value = (b"", b"")
            mock_exec.return_value = proc

            result = await facade.tag("myapp:v1", "reg.example.com/ns/myapp:v1")

        assert result.returncode == 0
        assert mock_exec.call_args[0] == ("docker", "tag", "myapp:v1", "reg.example.com/ns/myapp:v1")

    async def test_tag_failure_raises(self, facade):
        with patch("rock.sdk.envhub.docker.asyncio.create_subprocess_exec") as mock_exec:
            proc = AsyncMock()
            proc.returncode = 1
            proc.communicate.return_value = (b"", b"No such image\n")
            mock_exec.return_value = proc

            with pytest.raises(RuntimeError, match="docker tag failed"):
                await facade.tag("nosuch:v1", "reg.example.com/ns/nosuch:v1")


class TestInspect:
    async def test_returns_parsed_json(self, facade):
        inspect_data = [{"Id": "sha256:abc", "RepoTags": ["myapp:v1"]}]
        with patch("rock.sdk.envhub.docker.asyncio.create_subprocess_exec") as mock_exec:
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate.return_value = (json.dumps(inspect_data).encode(), b"")
            mock_exec.return_value = proc

            result = await facade.inspect("myapp:v1")

        assert result["Id"] == "sha256:abc"

    async def test_returns_none_when_not_found(self, facade):
        with patch("rock.sdk.envhub.docker.asyncio.create_subprocess_exec") as mock_exec:
            proc = AsyncMock()
            proc.returncode = 1
            proc.communicate.return_value = (b"", b"No such object\n")
            mock_exec.return_value = proc

            result = await facade.inspect("nosuch:v1")

        assert result is None


class TestIsImageAvailable:
    async def test_delegates_to_docker_util(self, facade):
        with patch("rock.sdk.envhub.docker.DockerUtil.is_image_available", return_value=True) as mock_avail:
            result = await facade.is_image_available("myapp:v1")
        assert result is True
        mock_avail.assert_called_once_with("myapp:v1")

    async def test_returns_false(self, facade):
        with patch("rock.sdk.envhub.docker.DockerUtil.is_image_available", return_value=False):
            result = await facade.is_image_available("nosuch:v1")
        assert result is False


class TestRemoveImage:
    async def test_removes_image(self, facade):
        with patch("rock.sdk.envhub.docker.asyncio.create_subprocess_exec") as mock_exec:
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate.return_value = (b"Untagged: myapp:v1\n", b"")
            mock_exec.return_value = proc

            result = await facade.remove_image("myapp:v1")

        assert result.returncode == 0

    async def test_remove_failure_raises(self, facade):
        with patch("rock.sdk.envhub.docker.asyncio.create_subprocess_exec") as mock_exec:
            proc = AsyncMock()
            proc.returncode = 1
            proc.communicate.return_value = (b"", b"conflict: unable to remove\n")
            mock_exec.return_value = proc

            with pytest.raises(RuntimeError, match="docker rmi failed"):
                await facade.remove_image("myapp:v1")


class TestMirror:
    async def test_login_pull_tag_push_sequence(self, facade, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        calls: list[str] = []

        async def _login(self, reg, user, pwd, *, timeout=30):
            calls.append(f"login:{reg}")
            return "ok"

        async def _pull(self, image, *, timeout_sec=5):
            calls.append(f"pull:{image}")
            from subprocess import CompletedProcess

            return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        async def _tag(self, src, dst):
            calls.append(f"tag:{src}->{dst}")
            from subprocess import CompletedProcess

            return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        async def _push(self, t):
            calls.append(f"push:{t}")
            from subprocess import CompletedProcess

            return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            patch.object(DockerFacade, "login", _login),
            patch.object(DockerFacade, "pull_image", _pull),
            patch.object(DockerFacade, "tag", _tag),
            patch.object(DockerFacade, "push", _push),
        ):
            target = await facade.mirror(
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

    async def test_mirror_with_source_credentials(self, facade, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        logins: list[str] = []

        async def _login(self, reg, user, pwd, *, timeout=30):
            logins.append(reg)
            return "ok"

        async def _noop_pull(self, image, *, timeout_sec=5):
            from subprocess import CompletedProcess

            return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        async def _noop_tag(self, src, dst):
            from subprocess import CompletedProcess

            return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        async def _noop_push(self, t):
            from subprocess import CompletedProcess

            return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with (
            patch.object(DockerFacade, "login", _login),
            patch.object(DockerFacade, "pull_image", _noop_pull),
            patch.object(DockerFacade, "tag", _noop_tag),
            patch.object(DockerFacade, "push", _noop_push),
        ):
            await facade.mirror(
                "ghcr.io/org/app:v1",
                "reg.aliyuncs.com/ns",
                target_username="tu",
                target_password="tp",
                source_registry="ghcr.io",
                source_username="su",
                source_password="sp",
            )

        assert logins == ["reg.aliyuncs.com/ns", "ghcr.io"]


class TestCustomResolver:
    async def test_uses_injected_resolver(self):
        custom = RockRegistryResolver()
        f = DockerFacade(resolver=custom)
        assert f._resolver is custom

    async def test_default_resolver(self):
        f = DockerFacade()
        assert isinstance(f._resolver, RockRegistryResolver)

    async def test_custom_docker_executable(self):
        f = DockerFacade(docker_executable="/usr/local/bin/docker")
        assert f._docker_executable == "/usr/local/bin/docker"
        assert f._docker_cmd.docker_executable == "/usr/local/bin/docker"


class TestRegistriesParameter:
    async def test_registries_param_forwarded_to_resolver(self):
        f = DockerFacade(registries=["reg.example.com/ns"])
        assert f._resolver._registries == ["reg.example.com/ns"]

    async def test_registries_param_used_for_resolve(self, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        f = DockerFacade(registries=["reg.example.com/ns"])
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            result = await f.resolve_image("ghcr.io/org/app:v1")
        assert result == "reg.example.com/org/app:v1"

    async def test_resolver_param_overrides_registries(self):
        custom = RockRegistryResolver(registries=["custom.example.com/ns"])
        f = DockerFacade(resolver=custom, registries=["ignored.example.com/ns"])
        assert f._resolver is custom
        assert f._resolver._registries == ["custom.example.com/ns"]

    async def test_no_registries_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "env-reg.example.com/ns")
        f = DockerFacade()
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            result = await f.resolve_image("ghcr.io/org/app:v1")
        assert result.startswith("env-reg.example.com/")
