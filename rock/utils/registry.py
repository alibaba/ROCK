"""Shared Docker Registry v2 manifest probe utilities.

Used by both the admin image-mirror logic and the SDK regionless resolver
to avoid duplicating the HTTP probe, image parsing, and candidate-building
code.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlencode

import httpx

from rock.logger import init_logger

logger = init_logger(__name__)

_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)


def parse_bearer_challenge(header: str) -> dict[str, str]:
    """Parse ``realm``, ``service``, ``scope`` from a Bearer WWW-Authenticate header."""
    return {m.group(1): m.group(2) for m in re.finditer(r'(\w+)="([^"]*)"', header)}


def parse_image_ref(image: str) -> tuple[str, str, str]:
    """Extract ``(registry_host, repo_path, tag_or_digest)`` from an image reference.

    If the first path segment contains ``.`` or ``:`` it is treated as a
    registry host; otherwise the image has no explicit registry.

    Returns ``("", "", "")`` for empty input.

    Examples::

        "gcr.io/foo/bar:v1"   -> ("gcr.io", "foo/bar", "v1")
        "foo/bar:1.2"         -> ("", "foo/bar", "1.2")
        "foo/bar"             -> ("", "foo/bar", "latest")
        "ubuntu"              -> ("", "ubuntu", "latest")
        "reg:5000/foo/bar:v1" -> ("reg:5000", "foo/bar", "v1")
        "foo/bar@sha256:abc"  -> ("", "foo/bar", "sha256:abc")
    """
    if not image:
        return ("", "", "")

    if "@" in image:
        path, _, digest = image.partition("@")
    else:
        path = image
        digest = None

    parts = path.split("/", maxsplit=1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0]):
        registry_host = parts[0]
        repo_and_rest = parts[1]
    else:
        registry_host = ""
        repo_and_rest = path

    if digest is not None:
        return (registry_host, repo_and_rest, digest)

    last_slash = repo_and_rest.rfind("/")
    last_colon = repo_and_rest.rfind(":")
    if last_colon > last_slash:
        repo = repo_and_rest[:last_colon]
        tag = repo_and_rest[last_colon + 1 :]
    else:
        repo = repo_and_rest
        tag = "latest"

    return (registry_host, repo, tag)


def build_mirror_candidates(
    image: str,
    mirror_registry: str,
    mirror_namespace: str,
) -> list[tuple[str, str]]:
    """Build ``(candidate_image, repo)`` pairs for mirror probing.

    Returns up to 2 candidates in priority order:

    1. **Preserve original namespace** — ``{mirror_registry}/{original_ns}/{name}:{tag}``
    2. **Replace with mirror namespace** — ``{mirror_registry}/{mirror_ns}/{name}:{tag}``

    Deduplicates when original namespace equals mirror namespace.
    Skips the original-namespace candidate when the image has no namespace.
    """
    registry_host, repo_path, tag = parse_image_ref(image)

    if "/" in repo_path:
        original_namespace, name_part = repo_path.split("/", 1)
    else:
        original_namespace = None
        name_part = repo_path

    candidates: list[tuple[str, str]] = []
    if original_namespace:
        candidate_image = f"{mirror_registry}/{original_namespace}/{name_part}:{tag}"
        candidate_repo = f"{original_namespace}/{name_part}"
        candidates.append((candidate_image, candidate_repo))
    if original_namespace != mirror_namespace:
        candidate_image = f"{mirror_registry}/{mirror_namespace}/{name_part}:{tag}"
        candidate_repo = f"{mirror_namespace}/{name_part}"
        candidates.append((candidate_image, candidate_repo))
    return candidates


async def _do_bearer_auth(
    client: httpx.AsyncClient,
    resp: httpx.Response,
    url: str,
    headers: dict[str, str],
    auth: tuple[str, str] | None,
) -> httpx.Response:
    """Handle 401 Bearer challenge and retry the manifest request."""
    if resp.status_code != 401 or "www-authenticate" not in resp.headers:
        return resp
    www_auth = resp.headers["www-authenticate"]
    if not www_auth.startswith("Bearer "):
        return resp

    params = parse_bearer_challenge(www_auth)
    realm = params.get("realm", "")
    service = params.get("service", "")
    scope = params.get("scope", "")
    token_url = f"{realm}?{urlencode({'service': service, 'scope': scope})}"
    token_resp = await client.get(token_url, auth=auth)
    if token_resp.status_code == 200:
        data = token_resp.json()
        token = data.get("token") or data.get("access_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
            return await client.get(url, headers=headers)
    return resp


async def probe_manifest(
    registry: str,
    repo: str,
    tag: str,
    *,
    username: str | None = None,
    password: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = 5.0,
) -> bool:
    """Check whether ``repo:tag`` exists on *registry* via the v2 manifest API.

    When *client* is provided (e.g. from :class:`HttpPoolManager`), it is used
    directly and *timeout* is ignored. Otherwise a one-shot
    ``httpx.AsyncClient`` is created with the given *timeout*.
    """
    url = f"https://{registry}/v2/{repo}/manifests/{tag}"
    headers = {"Accept": _MANIFEST_ACCEPT}
    auth = (username, password) if username and password else None

    if client is not None:
        resp = await client.get(url, headers=headers, auth=auth)
        resp = await _do_bearer_auth(client, resp, url, headers, auth)
        return resp.status_code == 200

    async with httpx.AsyncClient(timeout=timeout) as ephemeral:
        resp = await ephemeral.get(url, headers=headers, auth=auth)
        resp = await _do_bearer_auth(ephemeral, resp, url, headers, auth)
        return resp.status_code == 200


class ProbeCache:
    """TTL-based probe result cache (process-local)."""

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[bool, float]] = {}

    def get(self, key: str) -> bool | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        hit, expires_at = entry
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return hit

    def set(self, key: str, hit: bool) -> None:
        self._data[key] = (hit, time.monotonic() + self._ttl)

    def clear(self) -> None:
        self._data.clear()
