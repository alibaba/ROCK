from unittest.mock import AsyncMock, patch

import pytest

from rock.sdk.envhub.regionless.resolver import (
    ROCK_REGISTRY_ENV,
    RockRegistryResolver,
)


@pytest.fixture()
def resolver():
    r = RockRegistryResolver()
    yield r
    r.reset_cache()


class TestSplitTagOrDigest:
    def test_explicit_tag(self):
        assert RockRegistryResolver.split_tag_or_digest("foo/bar:1.2") == ("foo/bar", ":1.2")

    def test_no_tag_defaults_to_latest(self):
        assert RockRegistryResolver.split_tag_or_digest("foo/bar") == ("foo/bar", ":latest")

    def test_digest(self):
        assert RockRegistryResolver.split_tag_or_digest("foo/bar@sha256:abc") == ("foo/bar", "@sha256:abc")

    def test_host_with_port_no_tag(self):
        assert RockRegistryResolver.split_tag_or_digest("registry:5000/foo/bar") == (
            "registry:5000/foo/bar",
            ":latest",
        )

    def test_host_with_port_and_tag(self):
        assert RockRegistryResolver.split_tag_or_digest("registry:5000/foo/bar:v1") == (
            "registry:5000/foo/bar",
            ":v1",
        )


class TestParseRegistries:
    def test_empty(self):
        assert RockRegistryResolver.parse_registries("") == []
        assert RockRegistryResolver.parse_registries("   ") == []

    def test_single(self):
        assert RockRegistryResolver.parse_registries("reg.example.com/ns") == ["reg.example.com/ns"]

    def test_comma_separated(self):
        assert RockRegistryResolver.parse_registries("a.com/x, b.com/y") == ["a.com/x", "b.com/y"]

    def test_semicolon_separated(self):
        assert RockRegistryResolver.parse_registries("a.com/x;b.com/y") == ["a.com/x", "b.com/y"]

    def test_mixed_separators_and_trailing_slashes(self):
        assert RockRegistryResolver.parse_registries("a.com/x/, b.com/y; c.com/z/") == [
            "a.com/x",
            "b.com/y",
            "c.com/z",
        ]

    def test_skips_blank_entries(self):
        assert RockRegistryResolver.parse_registries("a.com/x,,;  ; b.com/y") == ["a.com/x", "b.com/y"]


class TestBuildCandidate:
    def test_strips_first_namespace_only(self):
        assert (
            RockRegistryResolver.build_candidate("swebench/sweb.eval.x86_64.foo:latest", "reg.example.com/mirror")
            == "reg.example.com/mirror/sweb.eval.x86_64.foo:latest"
        )

    def test_explicit_dockerhub_host(self):
        assert (
            RockRegistryResolver.build_candidate("docker.io/library/python:3.12", "reg.example.com/ns")
            == "reg.example.com/ns/python:3.12"
        )

    def test_ghcr_image(self):
        assert (
            RockRegistryResolver.build_candidate("ghcr.io/foo/bar/baz:v1", "reg.example.com/ns")
            == "reg.example.com/ns/bar/baz:v1"
        )

    def test_default_tag_when_missing(self):
        assert RockRegistryResolver.build_candidate("foo/bar", "reg.example.com/ns") == "reg.example.com/ns/bar:latest"

    def test_strips_trailing_slash_on_registry(self):
        assert RockRegistryResolver.build_candidate("foo/bar:1", "reg.example.com/ns/") == "reg.example.com/ns/bar:1"

    def test_deeply_nested_namespaces_preserved(self):
        assert (
            RockRegistryResolver.build_candidate("gcr.io/project/subproj/image:v1", "reg.example.com/ns")
            == "reg.example.com/ns/subproj/image:v1"
        )


class TestResolveImage:
    async def test_no_env_returns_original(self, resolver, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        result = await resolver.resolve_image("foo/bar:1")
        assert result == "foo/bar:1"

    async def test_empty_env_returns_original(self, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "   ")
        result = await resolver.resolve_image("foo/bar:1")
        assert result == "foo/bar:1"

    async def test_empty_image_returns_unchanged(self, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        assert await resolver.resolve_image("") == ""

    async def test_digest_pinned_skips_rewrite_and_probe(self, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        probe = AsyncMock(return_value=True)
        with patch.object(RockRegistryResolver, "_http_probe_manifest", new=probe):
            result = await resolver.resolve_image("swebench/foo@sha256:abc123")
        assert result == "swebench/foo@sha256:abc123"
        probe.assert_not_awaited()

    async def test_hit_returns_candidate(self, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ) as probe:
            result = await resolver.resolve_image("swebench/foo:latest")
        assert result == "reg.example.com/ns/foo:latest"
        probe.assert_awaited_once()

    async def test_miss_returns_original(self, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=False),
        ):
            result = await resolver.resolve_image("swebench/foo:latest")
        assert result == "swebench/foo:latest"

    async def test_cache_skips_second_probe(self, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        probe = AsyncMock(return_value=True)
        with patch.object(RockRegistryResolver, "_http_probe_manifest", new=probe):
            await resolver.resolve_image("swebench/foo:latest")
            await resolver.resolve_image("swebench/foo:latest")
        assert probe.await_count == 1

    async def test_cache_keyed_by_registry(self, resolver, monkeypatch):
        probe = AsyncMock(return_value=True)
        with patch.object(RockRegistryResolver, "_http_probe_manifest", new=probe):
            monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg-a.example.com/ns")
            await resolver.resolve_image("swebench/foo:latest")
            monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg-b.example.com/ns")
            await resolver.resolve_image("swebench/foo:latest")
        assert probe.await_count == 2

    async def test_probe_timeout_falls_back(self, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=False),
        ):
            result = await resolver.resolve_image("swebench/foo:latest", timeout_sec=0.05)
        assert result == "swebench/foo:latest"

    async def test_multi_registry_first_hit_wins(self, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg-a.example.com/ns,reg-b.example.com/ns")
        calls: list[str] = []

        async def _probe(self, candidate, *, timeout_sec):
            calls.append(candidate)
            return candidate.startswith("reg-a.example.com/")

        with patch.object(RockRegistryResolver, "_http_probe_manifest", new=_probe):
            result = await resolver.resolve_image("swebench/foo:latest")
        assert result == "reg-a.example.com/ns/foo:latest"
        assert calls == ["reg-a.example.com/ns/foo:latest"]

    async def test_multi_registry_falls_through_to_second(self, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg-a.example.com/ns,reg-b.example.com/ns")
        calls: list[str] = []

        async def _probe(self, candidate, *, timeout_sec):
            calls.append(candidate)
            return candidate.startswith("reg-b.example.com/")

        with patch.object(RockRegistryResolver, "_http_probe_manifest", new=_probe):
            result = await resolver.resolve_image("swebench/foo:latest")
        assert result == "reg-b.example.com/ns/foo:latest"
        assert calls == [
            "reg-a.example.com/ns/foo:latest",
            "reg-b.example.com/ns/foo:latest",
        ]

    async def test_multi_registry_all_miss_returns_original(self, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg-a.example.com/ns;reg-b.example.com/ns")
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=False),
        ) as probe:
            result = await resolver.resolve_image("swebench/foo:latest")
        assert result == "swebench/foo:latest"
        assert probe.await_count == 2


class TestResolveDockerfile:
    async def test_rewrites_from_line(self, tmp_path, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        df = tmp_path / "Dockerfile"
        df.write_text("FROM old-registry.com/repo/swe-bench:v1\nRUN echo hello\n")
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            changed = await resolver.resolve_dockerfile(df)
        assert changed
        assert "FROM reg.example.com/ns/swe-bench:v1\n" in df.read_text()

    async def test_no_change_on_miss(self, tmp_path, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        df = tmp_path / "Dockerfile"
        original = "FROM old-registry.com/repo/swe-bench:v1\nRUN echo hello\n"
        df.write_text(original)
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=False),
        ):
            changed = await resolver.resolve_dockerfile(df)
        assert not changed
        assert df.read_text() == original

    async def test_no_change_without_env(self, tmp_path, resolver, monkeypatch):
        monkeypatch.delenv(ROCK_REGISTRY_ENV, raising=False)
        df = tmp_path / "Dockerfile"
        original = "FROM python:3.11\nRUN echo hello\n"
        df.write_text(original)
        changed = await resolver.resolve_dockerfile(df)
        assert not changed
        assert df.read_text() == original

    async def test_preserves_as_clause(self, tmp_path, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        df = tmp_path / "Dockerfile"
        df.write_text("FROM old-registry.com/repo/base:v1 AS builder\nRUN make\n")
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            changed = await resolver.resolve_dockerfile(df)
        assert changed
        assert "FROM reg.example.com/ns/base:v1 AS builder\n" in df.read_text()

    async def test_handles_platform_flag(self, tmp_path, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        df = tmp_path / "Dockerfile"
        df.write_text("FROM --platform=linux/amd64 ghcr.io/laude-institute/t-bench/deveval:latest\nRUN echo hello\n")
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=True),
        ):
            changed = await resolver.resolve_dockerfile(df)
        assert changed
        assert "FROM --platform=linux/amd64 reg.example.com/ns/t-bench/deveval:latest\n" in df.read_text()

    async def test_skips_comment_lines(self, tmp_path, resolver, monkeypatch):
        monkeypatch.setenv(ROCK_REGISTRY_ENV, "reg.example.com/ns")
        df = tmp_path / "Dockerfile"
        original = "# FROM old-registry.com/repo/base:v1\nFROM python:3.11\n"
        df.write_text(original)
        with patch.object(
            RockRegistryResolver,
            "_http_probe_manifest",
            new=AsyncMock(return_value=False),
        ):
            changed = await resolver.resolve_dockerfile(df)
        assert not changed
