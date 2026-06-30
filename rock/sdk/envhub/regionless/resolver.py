"""Rewrite container image references to a ROCK mirror registry when available.

Resolution rule:

- Read ``INSTANCE_ROCK_REGISTRY`` (one or more ``host/namespace`` entries
  separated by ``,`` or ``;``, e.g.
  ``reg-a.aliyuncs.com/mirror-1,reg-b.aliyuncs.com/mirror-2``). If unset/empty,
  return the original image unchanged.
- Take the *last* path segment of the image reference as the image name and
  combine it with the original tag/digest:
  ``swebench/sweb.eval.x86_64.foo:latest`` →
  ``<registry>/sweb.eval.x86_64.foo:latest``.
- For each configured registry in order, probe the candidate via the Docker
  Registry v2 manifest API (``GET /v2/{repo}/manifests/{tag}``), with Bearer
  token authentication support and a short timeout. Return the *first*
  candidate that exists; if none exist (or the probes time out / fail),
  fall back to the original image. Probe results are cached in-process so
  concurrent trials do not hammer the registry.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlencode

import httpx

from rock.logger import init_logger

logger = init_logger(__name__)

ROCK_REGISTRY_ENV = "INSTANCE_ROCK_REGISTRY"
_DEFAULT_PROBE_TIMEOUT_SEC = 5.0
_REGISTRY_SEPARATORS = (",", ";")

_FROM_RE = re.compile(
    r"^(?P<prefix>\s*FROM\s+(?:--\S+\s+)*)(?P<image>\S+)(?P<suffix>.*)$",
    re.IGNORECASE,
)


class RockRegistryResolver:
    """Resolves container image references to ROCK mirror registries.

    Args:
        registries: Explicit list of mirror registries (``host/namespace``).
            When *None* (default), falls back to reading the
            ``INSTANCE_ROCK_REGISTRY`` environment variable at resolve time.
    """

    def __init__(self, registries: list[str] | None = None) -> None:
        self._registries = registries
        self._resolve_cache: dict[str, str] = {}
        self._cache_lock = asyncio.Lock()

    @staticmethod
    def parse_registries(raw: str) -> list[str]:
        """Split the env value into an ordered list of non-empty registry entries."""
        if not raw:
            return []
        tokens = [raw]
        for sep in _REGISTRY_SEPARATORS:
            tokens = [piece for token in tokens for piece in token.split(sep)]
        return [token.strip().rstrip("/") for token in tokens if token.strip()]

    @staticmethod
    def split_tag_or_digest(image: str) -> tuple[str, str]:
        """Split image into (path, tag-or-digest-suffix).

        Examples:
            "foo/bar:1.2" -> ("foo/bar", ":1.2")
            "foo/bar@sha256:abc" -> ("foo/bar", "@sha256:abc")
            "foo/bar" -> ("foo/bar", ":latest")
        """
        if "@" in image:
            path, _, digest = image.partition("@")
            return path, f"@{digest}"
        last_slash = image.rfind("/")
        last_colon = image.rfind(":")
        if last_colon > last_slash:
            return image[:last_colon], f":{image[last_colon + 1 :]}"
        return image, ":latest"

    @staticmethod
    def build_candidate(image: str, registry: str) -> str:
        """Build the candidate image reference under the ROCK registry.

        Strips the original registry and first-level namespace, preserving any
        nested namespaces and the image name plus tag/digest.
        Example: ``ghcr.io/foo/bar/baz:v1`` with registry ``reg/ns`` →
        ``reg/ns/bar/baz:v1``.
        """
        path, suffix = RockRegistryResolver.split_tag_or_digest(image)
        if "/" in path:
            _, path = path.split("/", 1)
        if "/" in path:
            _, path = path.split("/", 1)
        return f"{registry.rstrip('/')}/{path}{suffix}"

    @staticmethod
    def build_candidate_with_original_namespace(image: str, registry: str) -> str:
        """Build candidate preserving the original namespace from the image.

        Replaces only the registry host, keeping the original namespace and
        image name intact.
        Example: ``ghcr.io/swebench/foo:v1`` with registry
        ``reg.aliyuncs.com/fixed-ns`` → ``reg.aliyuncs.com/swebench/foo:v1``.
        """
        path, suffix = RockRegistryResolver.split_tag_or_digest(image)
        # Strip the original registry host from the image path
        if "/" in path:
            first_part, rest = path.split("/", 1)
            if "." in first_part or ":" in first_part:
                path = rest
        # Extract only the host from the mirror registry (before first /)
        mirror_host = registry.split("/", 1)[0] if "/" in registry else registry
        return f"{mirror_host}/{path}{suffix}"

    @staticmethod
    def _parse_bearer_challenge(header: str) -> dict[str, str]:
        """Parse ``realm``, ``service``, ``scope`` from a Bearer WWW-Authenticate header."""
        return {m.group(1): m.group(2) for m in re.finditer(r'(\w+)="([^"]*)"', header)}

    @staticmethod
    def _parse_image_parts(image: str) -> tuple[str, str, str]:
        """Extract (registry_host, repo_path, tag) from a fully-qualified image reference."""
        path, suffix = RockRegistryResolver.split_tag_or_digest(image)
        tag = suffix.lstrip(":")
        first_slash = path.find("/")
        if first_slash == -1:
            return (path, "", tag)
        registry = path[:first_slash]
        repo = path[first_slash + 1 :]
        return (registry, repo, tag)

    async def _http_probe_manifest(self, image: str, timeout_sec: float) -> bool:
        """Check whether *image* exists on its registry via the v2 manifest API."""
        registry, repo, tag = self._parse_image_parts(image)
        if not registry or not repo:
            return False

        url = f"https://{registry}/v2/{repo}/manifests/{tag}"
        headers = {
            "Accept": ", ".join(
                [
                    "application/vnd.docker.distribution.manifest.v2+json",
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.oci.image.index.v1+json",
                ]
            )
        }

        try:
            async with httpx.AsyncClient(timeout=timeout_sec) as client:
                resp = await client.get(url, headers=headers)

                if resp.status_code == 401 and "www-authenticate" in resp.headers:
                    www_auth = resp.headers["www-authenticate"]
                    if www_auth.startswith("Bearer "):
                        params = self._parse_bearer_challenge(www_auth)
                        realm = params.get("realm", "")
                        service = params.get("service", "")
                        scope = params.get("scope", "")
                        token_url = f"{realm}?{urlencode({'service': service, 'scope': scope})}"
                        token_resp = await client.get(token_url)
                        if token_resp.status_code == 200:
                            data = token_resp.json()
                            token = data.get("token") or data.get("access_token")
                            if token:
                                headers["Authorization"] = f"Bearer {token}"
                                resp = await client.get(url, headers=headers)

                return resp.status_code == 200
        except httpx.HTTPError:
            logger.debug("HTTP probe for %s failed (network/protocol)", image, exc_info=True)
            return False
        except (ValueError, KeyError):
            logger.debug("HTTP probe for %s failed (response parsing)", image, exc_info=True)
            return False
        except Exception:
            logger.warning("HTTP probe for %s failed (unexpected)", image, exc_info=True)
            return False

    async def resolve_image(
        self,
        image: str,
        *,
        timeout_sec: float = _DEFAULT_PROBE_TIMEOUT_SEC,
    ) -> str:
        """Return a ROCK-mirrored image reference if available, else ``image``."""
        if not image:
            return image

        if "@" in image:
            return image

        if self._registries is not None:
            registries = self._registries
        else:
            registries = self.parse_registries(os.environ.get(ROCK_REGISTRY_ENV, ""))
        if not registries:
            return image

        cache_key = f"{'|'.join(registries)}||{image}"
        async with self._cache_lock:
            cached = self._resolve_cache.get(cache_key)
        if cached is not None:
            return cached

        resolved = image
        for registry in registries:
            candidates = []
            original_ns = self.build_candidate_with_original_namespace(image, registry)
            fixed_ns = self.build_candidate(image, registry)
            if original_ns != image:
                candidates.append(original_ns)
            if fixed_ns != image and fixed_ns not in candidates:
                candidates.append(fixed_ns)

            found = False
            for candidate in candidates:
                if await self._http_probe_manifest(candidate, timeout_sec=timeout_sec):
                    logger.info("Rewriting image %s -> %s (ROCK mirror)", image, candidate)
                    resolved = candidate
                    found = True
                    break
            if found:
                break

        async with self._cache_lock:
            self._resolve_cache[cache_key] = resolved
        return resolved

    async def resolve_dockerfile(
        self,
        dockerfile: Path,
        *,
        timeout_sec: float = _DEFAULT_PROBE_TIMEOUT_SEC,
    ) -> bool:
        """Rewrite ``FROM`` images in *dockerfile* to ROCK mirrors when available.

        Returns True if any ``FROM`` line was rewritten.
        """
        text = dockerfile.read_text()
        lines = text.splitlines(keepends=True)
        changed = False

        for i, line in enumerate(lines):
            m = _FROM_RE.match(line.rstrip("\n\r"))
            if not m:
                continue
            original = m.group("image")
            resolved = await self.resolve_image(original, timeout_sec=timeout_sec)
            if resolved != original:
                eol = line[len(line.rstrip("\n\r")) :]
                lines[i] = f"{m.group('prefix')}{resolved}{m.group('suffix')}{eol}"
                changed = True

        if changed:
            dockerfile.write_text("".join(lines))
        return changed

    def reset_cache(self) -> None:
        """Clear the in-process resolve cache. Test helper."""
        self._resolve_cache.clear()


RegionlessResolver = RockRegistryResolver

_default_resolver = RockRegistryResolver()

resolve_image = _default_resolver.resolve_image
resolve_dockerfile = _default_resolver.resolve_dockerfile
