"""Unit tests for Image — covers 4-segment image name composition.

Run: uv run pytest tests/unit/sdk/sandbox/test_image.py -v
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from rock.sdk.sandbox.image import Image


@pytest.fixture
def env_dir(tmp_path: Path) -> Path:
    """Minimal valid build context: a Dockerfile + a marker file."""
    d = tmp_path / "env"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM python:3.11\nCOPY hello.txt /opt/hello.txt\n")
    (d / "hello.txt").write_text("hi\n")
    return d


def test_from_dockerfile_rejects_image_name_kwarg(env_dir: Path) -> None:
    """The 4-segment refactor removes image_name= from from_dockerfile."""
    with pytest.raises(TypeError):
        Image.from_dockerfile(env_dir, image_name="reg.io/ns/repo:tag")


def test_resolve_full_name_concatenates_four_segments(env_dir: Path) -> None:
    """Happy path: explicit segments concatenated; trailing slash on registry stripped."""
    image = Image.from_dockerfile(env_dir, registry_url="reg.io/", namespace="myns", repository="myrepo")
    name = image._resolve_full_name()
    tag = image.content_hash()
    assert name == f"reg.io/myns/myrepo:{tag}"


def test_resolve_full_name_raises_when_segments_missing(env_dir: Path) -> None:
    """Missing segments → ValueError listing exactly which ones."""
    with patch("rock.env_vars.ROCK_IMAGE_REGISTRY", None), patch("rock.env_vars.ROCK_IMAGE_NAMESPACE", None):
        image = Image.from_dockerfile(env_dir)  # repository also unset
        with pytest.raises(ValueError) as exc:
            image._resolve_full_name()
        msg = str(exc.value)
        assert "registry_url" in msg and "namespace" in msg and "repository" in msg


def test_resolve_full_name_uses_env_defaults(env_dir: Path) -> None:
    """registry_url / namespace default to env vars when kwargs omitted."""
    with (
        patch("rock.env_vars.ROCK_IMAGE_REGISTRY", "env-reg.io"),
        patch("rock.env_vars.ROCK_IMAGE_NAMESPACE", "env-ns"),
    ):
        image = Image.from_dockerfile(env_dir, repository="myrepo")
        assert image._resolve_full_name().startswith("env-reg.io/env-ns/myrepo:")


def test_tag_is_64_hex_sha256(env_dir: Path) -> None:
    """Tag pinned to full SHA-256 (OCI digest length), no truncation."""
    image = Image.from_dockerfile(env_dir, registry_url="reg.io", namespace="ns", repository="repo")
    tag = image._resolve_full_name().rsplit(":", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{64}", tag)


class _CapturedRepository(Exception):
    def __init__(self, repository):
        super().__init__(repository)
        self.repository = repository


@pytest.mark.asyncio
async def test_sandbox_start_injects_user_id_as_repository(env_dir, monkeypatch):
    from rock.sdk.sandbox.client import Sandbox
    from rock.sdk.sandbox.config import SandboxConfig
    from rock.sdk.sandbox.image import Image

    async def fake_build(self, **kwargs):
        raise _CapturedRepository(self.repository)

    monkeypatch.setattr(Image, "build", fake_build)

    image = Image.from_dockerfile(env_dir, registry_url="reg.io", namespace="ns")
    config = SandboxConfig(image=image, user_id="alice", base_url="http://x")
    sandbox = Sandbox(config)
    with pytest.raises(_CapturedRepository) as excinfo:
        await sandbox.start()
    assert excinfo.value.repository == "alice"


@pytest.mark.asyncio
async def test_sandbox_start_falls_back_to_default_repository(env_dir, monkeypatch):
    from rock.sdk.sandbox.client import Sandbox
    from rock.sdk.sandbox.config import SandboxConfig
    from rock.sdk.sandbox.image import Image

    async def fake_build(self, **kwargs):
        raise _CapturedRepository(self.repository)

    monkeypatch.setattr(Image, "build", fake_build)

    image = Image.from_dockerfile(env_dir, registry_url="reg.io", namespace="ns")
    config = SandboxConfig(image=image, base_url="http://x")  # no user_id
    sandbox = Sandbox(config)
    with pytest.raises(_CapturedRepository) as excinfo:
        await sandbox.start()
    assert excinfo.value.repository == "default"
