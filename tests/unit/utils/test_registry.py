"""Tests for rock.utils.registry — shared registry probe utilities."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from rock.utils.registry import (
    ProbeCache,
    build_mirror_candidates,
    parse_bearer_challenge,
    parse_image_ref,
    probe_manifest,
)


class TestParseBearerChallenge:
    def test_standard_challenge(self):
        header = 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:library/python:pull"'
        result = parse_bearer_challenge(header)
        assert result["realm"] == "https://auth.docker.io/token"
        assert result["service"] == "registry.docker.io"
        assert result["scope"] == "repository:library/python:pull"

    def test_empty_values(self):
        header = 'Bearer realm="",service=""'
        result = parse_bearer_challenge(header)
        assert result["realm"] == ""
        assert result["service"] == ""


class TestParseImageRef:
    def test_full_reference(self):
        assert parse_image_ref("gcr.io/foo/bar:v1") == ("gcr.io", "foo/bar", "v1")

    def test_no_registry(self):
        assert parse_image_ref("foo/bar:1.2") == ("", "foo/bar", "1.2")

    def test_no_tag(self):
        assert parse_image_ref("foo/bar") == ("", "foo/bar", "latest")

    def test_bare_image(self):
        assert parse_image_ref("ubuntu") == ("", "ubuntu", "latest")

    def test_registry_with_port(self):
        assert parse_image_ref("reg:5000/foo/bar:v1") == ("reg:5000", "foo/bar", "v1")

    def test_digest_reference(self):
        assert parse_image_ref("foo/bar@sha256:abc") == ("", "foo/bar", "sha256:abc")

    def test_digest_with_registry(self):
        assert parse_image_ref("gcr.io/foo/bar@sha256:abc") == ("gcr.io", "foo/bar", "sha256:abc")

    def test_empty(self):
        assert parse_image_ref("") == ("", "", "")

    def test_deep_path(self):
        assert parse_image_ref("gcr.io/project/subdir/image:v1") == ("gcr.io", "project/subdir/image", "v1")

    def test_docker_hub_library(self):
        assert parse_image_ref("docker.io/library/python:3.12") == ("docker.io", "library/python", "3.12")


class TestBuildMirrorCandidates:
    def test_with_namespace(self):
        candidates = build_mirror_candidates("gcr.io/foo/python:3.11", "rock-a.example.com", "rock-public")
        assert len(candidates) == 2
        assert candidates[0] == ("rock-a.example.com/foo/python:3.11", "foo/python")
        assert candidates[1] == ("rock-a.example.com/rock-public/python:3.11", "rock-public/python")

    def test_namespace_equals_mirror_namespace(self):
        candidates = build_mirror_candidates("gcr.io/foo/python:3.11", "rock-a.example.com", "foo")
        assert len(candidates) == 1
        assert candidates[0] == ("rock-a.example.com/foo/python:3.11", "foo/python")

    def test_no_namespace(self):
        candidates = build_mirror_candidates("python:3.11", "rock-a.example.com", "rock-public")
        assert len(candidates) == 1
        assert candidates[0] == ("rock-a.example.com/rock-public/python:3.11", "rock-public/python")

    def test_no_tag_defaults_latest(self):
        candidates = build_mirror_candidates("ubuntu", "rock-a.example.com", "rock-public")
        assert len(candidates) == 1
        assert candidates[0] == ("rock-a.example.com/rock-public/ubuntu:latest", "rock-public/ubuntu")

    def test_deep_path(self):
        candidates = build_mirror_candidates("gcr.io/project/subdir/myimage:v1", "rock-a.example.com", "rock-public")
        assert len(candidates) == 2
        assert candidates[0] == ("rock-a.example.com/project/subdir/myimage:v1", "project/subdir/myimage")
        assert candidates[1] == ("rock-a.example.com/rock-public/subdir/myimage:v1", "rock-public/subdir/myimage")


class TestProbeManifest:
    @pytest.fixture
    def mock_response_200(self):
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 200
        resp.headers = {}
        return resp

    @pytest.fixture
    def mock_response_404(self):
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 404
        resp.headers = {}
        return resp

    async def test_hit_with_pooled_client(self, mock_response_200):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=mock_response_200)
        result = await probe_manifest("reg.example.com", "ns/image", "v1", client=client)
        assert result is True
        client.get.assert_called_once()

    async def test_miss_with_pooled_client(self, mock_response_404):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=mock_response_404)
        result = await probe_manifest("reg.example.com", "ns/image", "v1", client=client)
        assert result is False

    async def test_bearer_auth_flow(self):
        token_resp = AsyncMock(spec=httpx.Response)
        token_resp.status_code = 200
        token_resp.json.return_value = {"token": "test-token"}

        first_resp = AsyncMock(spec=httpx.Response)
        first_resp.status_code = 401
        first_resp.headers = {
            "www-authenticate": 'Bearer realm="https://auth.example.com/token",service="registry",scope="repo:pull"'
        }

        second_resp = AsyncMock(spec=httpx.Response)
        second_resp.status_code = 200
        second_resp.headers = {}

        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=[first_resp, token_resp, second_resp])

        result = await probe_manifest("reg.example.com", "ns/image", "v1", client=client)
        assert result is True
        assert client.get.call_count == 3

    async def test_ephemeral_client_when_no_client_provided(self):
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 200
        resp.headers = {}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("rock.utils.registry.httpx.AsyncClient", return_value=mock_client):
            result = await probe_manifest("reg.example.com", "ns/image", "v1", timeout=3.0)
        assert result is True

    async def test_auth_credentials_passed(self):
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 200
        resp.headers = {}
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=resp)

        await probe_manifest("reg.example.com", "ns/image", "v1", username="user", password="pass", client=client)
        _, kwargs = client.get.call_args
        assert kwargs["auth"] == ("user", "pass")


class TestProbeCache:
    def test_get_miss(self):
        cache = ProbeCache()
        assert cache.get("foo") is None

    def test_set_and_get_hit(self):
        cache = ProbeCache()
        cache.set("foo", True)
        assert cache.get("foo") is True

    def test_set_and_get_false(self):
        cache = ProbeCache()
        cache.set("foo", False)
        assert cache.get("foo") is False

    def test_expired_entry(self):
        cache = ProbeCache(ttl_seconds=0)
        cache.set("foo", True)
        assert cache.get("foo") is None

    def test_clear(self):
        cache = ProbeCache()
        cache.set("foo", True)
        cache.set("bar", False)
        cache.clear()
        assert cache.get("foo") is None
        assert cache.get("bar") is None
