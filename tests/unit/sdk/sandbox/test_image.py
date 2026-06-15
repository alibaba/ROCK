"""Unit tests for Image — covers 4-segment image name composition.

Run: uv run pytest tests/unit/sdk/sandbox/test_image.py -v
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.sdk.sandbox.image import Image, ImageRegistry


@pytest.fixture
def env_dir(tmp_path: Path) -> Path:
    """Minimal valid build context: a Dockerfile + a marker file."""
    d = tmp_path / "env"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM python:3.11\nCOPY hello.txt /opt/hello.txt\n")
    (d / "hello.txt").write_text("hi\n")
    return d


def test_resolve_full_name_concatenates_four_segments(env_dir: Path) -> None:
    """Happy path: explicit segments concatenated; trailing slash on registry stripped."""
    image = Image.from_dockerfile(env_dir, registry=ImageRegistry(url="reg.io/", namespace="myns", repository="myrepo"))
    name = image.full_name
    tag = image.content_hash()
    assert name == f"reg.io/myns/myrepo:{tag}"


def test_resolve_full_name_raises_when_segments_missing(env_dir: Path) -> None:
    """Missing segments → ValueError listing exactly which ones."""
    image = Image.from_dockerfile(env_dir)  # all defaults are None
    with pytest.raises(ValueError) as exc:
        image.full_name
    msg = str(exc.value)
    assert "registry.url" in msg and "registry.namespace" in msg and "registry.repository" in msg


def test_tag_is_64_hex_sha256(env_dir: Path) -> None:
    """Tag pinned to full SHA-256 (OCI digest length), no truncation."""
    image = Image.from_dockerfile(env_dir, registry=ImageRegistry(url="reg.io", namespace="ns", repository="repo"))
    tag = image.full_name.rsplit(":", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{64}", tag)


class _CapturedRepository(Exception):
    def __init__(self, repository):
        super().__init__(repository)
        self.repository = repository


@pytest.mark.asyncio
async def test_sandbox_start_injects_user_id_as_repository(env_dir, monkeypatch):
    def fake_to_build_spec(self):
        raise _CapturedRepository(self.registry.repository)

    monkeypatch.setattr(Image, "to_build_spec", fake_to_build_spec)

    image = Image.from_dockerfile(env_dir, registry=ImageRegistry(url="reg.io", namespace="ns"))
    config = SandboxConfig(image=image, user_id="alice", base_url="http://x")
    sandbox = Sandbox(config)

    with patch.object(sandbox, "_fetch_acr_config", new_callable=AsyncMock, return_value=None):
        with pytest.raises(_CapturedRepository) as excinfo:
            await sandbox.start()
    assert excinfo.value.repository == "alice"


def test_from_dockerfile_accepts_file_path(tmp_path: Path) -> None:
    """When `path` points to a Dockerfile file, only that file is the build
    context — the surrounding directory is not used."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.11\n")
    # Sibling file that must NOT influence the build / content hash.
    (tmp_path / "noise.txt").write_text("ignore me\n")

    image = Image.from_dockerfile(dockerfile, registry=ImageRegistry(url="reg.io", namespace="ns", repository="repo"))
    name = image.full_name
    assert name.startswith("reg.io/ns/repo:")


def test_from_dockerfile_file_path_hash_excludes_siblings(tmp_path: Path) -> None:
    """File-path hash must depend only on the Dockerfile's content, not on
    sibling files that happen to share the parent dir."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.11\n")

    image_no_noise = Image.from_dockerfile(dockerfile, registry=ImageRegistry(url="r", namespace="n", repository="p"))
    hash_a = image_no_noise.content_hash()

    (tmp_path / "noise.txt").write_text("ignore me\n")
    image_with_noise = Image.from_dockerfile(dockerfile, registry=ImageRegistry(url="r", namespace="n", repository="p"))
    hash_b = image_with_noise.content_hash()

    assert hash_a == hash_b, "sibling files must not affect file-mode hash"


def test_from_dockerfile_rejects_nonexistent_path(tmp_path: Path) -> None:
    """Neither a missing file nor a missing directory should validate."""
    with pytest.raises(ValueError):
        Image.from_dockerfile(tmp_path / "does-not-exist")


@pytest.mark.asyncio
async def test_sandbox_start_falls_back_to_default_repository(env_dir, monkeypatch):
    def fake_to_build_spec(self):
        raise _CapturedRepository(self.registry.repository)

    monkeypatch.setattr(Image, "to_build_spec", fake_to_build_spec)

    image = Image.from_dockerfile(env_dir, registry=ImageRegistry(url="reg.io", namespace="ns"))
    config = SandboxConfig(image=image, base_url="http://x")  # no user_id
    sandbox = Sandbox(config)

    with patch.object(sandbox, "_fetch_acr_config", new_callable=AsyncMock, return_value=None):
        with pytest.raises(_CapturedRepository) as excinfo:
            await sandbox.start()
    assert excinfo.value.repository == "default"


@pytest.mark.asyncio
async def test_resolve_image_fills_from_admin_config(env_dir, monkeypatch):
    """_resolve_image() fills registry/builder fields from admin /acr_config."""

    def fake_to_build_spec(self):
        raise _CapturedRepository(self.registry.repository)

    monkeypatch.setattr(Image, "to_build_spec", fake_to_build_spec)

    admin_response = {
        "Registry": "admin-reg.io",
        "Namespace": "admin-ns",
        "Username": "tmp-user",
        "Password": "tmp-pass",
        "BuilderImage": "admin-builder:latest",
    }

    image = Image.from_dockerfile(env_dir)
    config = SandboxConfig(image=image, user_id="bob", base_url="http://x")
    sandbox = Sandbox(config)

    with patch.object(sandbox, "_fetch_acr_config", new_callable=AsyncMock, return_value=admin_response):
        with pytest.raises(_CapturedRepository):
            await sandbox.start()

    assert image.registry.url == "admin-reg.io"
    assert image.registry.namespace == "admin-ns"
    assert image.registry.username == "tmp-user"
    assert image.registry.password == "tmp-pass"
    assert image.builder_config.image == "admin-builder:latest"


@pytest.mark.asyncio
async def test_resolve_image_explicit_overrides_admin(env_dir, monkeypatch):
    """Explicitly set registry fields are NOT overwritten by admin config."""

    def fake_to_build_spec(self):
        raise _CapturedRepository(self.registry.repository)

    monkeypatch.setattr(Image, "to_build_spec", fake_to_build_spec)

    admin_response = {
        "Registry": "admin-reg.io",
        "Namespace": "admin-ns",
        "Username": "tmp-user",
        "Password": "tmp-pass",
        "BuilderImage": "admin-builder:latest",
    }

    image = Image.from_dockerfile(
        env_dir,
        registry=ImageRegistry(url="my-reg.io", namespace="my-ns"),
        builder_config=__import__("rock.sdk.sandbox.image.config", fromlist=["BuilderConfig"]).BuilderConfig(
            image="my-builder:v1"
        ),
    )
    config = SandboxConfig(image=image, user_id="bob", base_url="http://x")
    sandbox = Sandbox(config)

    with patch.object(sandbox, "_fetch_acr_config", new_callable=AsyncMock, return_value=admin_response):
        with pytest.raises(_CapturedRepository):
            await sandbox.start()

    assert image.registry.url == "my-reg.io"
    assert image.registry.namespace == "my-ns"
    assert image.builder_config.image == "my-builder:v1"
    # Credentials always come from admin (temporary ACR token)
    assert image.registry.username == "tmp-user"
