import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
from urllib.parse import urlparse

import httpx

from rock.logger import init_logger
from rock.sandbox.archive.abstract import AbstractImageStorage

logger = init_logger(__name__)


class DockerRegistryV2ImageStorage(AbstractImageStorage):
    """Docker Registry V2 image storage using docker CLI + HTTP API.

    Requires username/password authentication.
    """

    def __init__(self, registry_url: str, username: str, password: str):
        self._registry_url = registry_url
        self._username = username
        self._password = password

    @property
    def registry_url(self) -> str:
        return self._registry_url

    @property
    def client_config(self) -> dict:
        return {
            "registry_url": self._registry_url,
            "username": self._username,
            "password": self._password,
        }

    async def push_from_local(self, local_image_tag: str, remote_image_ref: str) -> None:
        async with self._docker_auth() as env:
            await self._docker_cmd("docker", "tag", local_image_tag, remote_image_ref)
            try:
                await self._docker_cmd("docker", "push", remote_image_ref, env=env)
            finally:
                await self._docker_cmd("docker", "rmi", remote_image_ref, check=False)

    async def pull_to_local(self, remote_image_ref: str) -> None:
        async with self._docker_auth() as env:
            await self._docker_cmd("docker", "pull", remote_image_ref, env=env)

    async def delete(self, image_ref: str) -> bool:
        registry, name, tag = self._parse_ref(image_ref)
        base_url = await self._registry_base_url(registry)
        accept = "application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json"
        async with httpx.AsyncClient() as client:
            auth_headers = await self._resolve_auth_headers(client, base_url, name)
            if not auth_headers:
                logger.warning(f"delete: auth negotiation failed for {image_ref}")
                return False

            resp = await client.get(
                f"{base_url}/v2/{name}/manifests/{tag}",
                headers={**auth_headers, "Accept": accept},
            )
            if resp.status_code == 401:
                logger.warning(f"delete: unauthorized fetching manifest for {image_ref} (status=401)")
                return False
            if resp.status_code != 200:
                logger.info(f"delete: manifest not found for {image_ref} (status={resp.status_code})")
                return False
            digest = resp.headers.get("Docker-Content-Digest")
            if not digest:
                logger.warning(f"delete: no Docker-Content-Digest header for {image_ref}")
                return False

            resp = await client.delete(f"{base_url}/v2/{name}/manifests/{digest}", headers=auth_headers)
            deleted = resp.status_code in (200, 202)
            if deleted:
                logger.info(f"delete: removed manifest {image_ref} (digest={digest})")
            else:
                logger.warning(f"delete: failed to remove {image_ref} (status={resp.status_code})")
            return deleted

    async def _resolve_auth_headers(self, client: httpx.AsyncClient, base_url: str, repo_name: str) -> dict | None:
        """Negotiate auth with the registry and return headers for subsequent requests."""
        challenge = await client.get(f"{base_url}/v2/")
        www_auth = challenge.headers.get("Www-Authenticate", "")

        if www_auth.lower().startswith("basic"):
            logger.debug(f"auth: using Basic auth for {base_url}")
            creds = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
            return {"Authorization": f"Basic {creds}"}

        m = re.search(r'realm="([^"]+)"', www_auth)
        realm = m.group(1) if m else None
        m = re.search(r'service="([^"]+)"', www_auth)
        service = m.group(1) if m else None
        if not realm or not service:
            logger.warning(f"auth: unable to parse Www-Authenticate header: {www_auth}")
            return None

        scope = f"repository:{repo_name}:pull,push,delete"
        token_resp = await client.get(
            realm,
            params={"service": service, "scope": scope},
            auth=(self._username, self._password),
        )
        if token_resp.status_code != 200:
            logger.warning(
                f"auth: token request failed (status={token_resp.status_code}, realm={realm}, scope={scope})"
            )
            return None
        token = token_resp.json().get("token")
        if not token:
            logger.warning(f"auth: token response missing 'token' field (realm={realm})")
            return None
        logger.debug(f"auth: obtained Bearer token for {repo_name} (realm={realm})")
        return {"Authorization": f"Bearer {token}"}

    async def exists(self, image_ref: str) -> bool:
        registry, name, tag = self._parse_ref(image_ref)
        base_url = await self._registry_base_url(registry)
        accept = "application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json"
        async with httpx.AsyncClient() as client:
            auth_headers = await self._resolve_auth_headers(client, base_url, name)
            if not auth_headers:
                logger.warning(f"exists: auth negotiation failed for {image_ref}")
                return False
            resp = await client.head(
                f"{base_url}/v2/{name}/manifests/{tag}",
                headers={**auth_headers, "Accept": accept},
            )
            found = resp.status_code == 200
            logger.info(f"exists: {image_ref} {'found' if found else 'not found'} (status={resp.status_code})")
            return found

    def _docker_auth(self):
        return _DockerAuthContext(self)

    @staticmethod
    async def _registry_base_url(registry: str) -> str:
        """Return the base URL (with scheme) for a registry host:port."""
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(f"https://{registry}/v2/", timeout=3)
                if resp.status_code in (200, 401):
                    return f"https://{registry}"
            except (httpx.ConnectError, httpx.ConnectTimeout):
                pass
        return f"http://{registry}"

    @staticmethod
    def _parse_ref(image_ref: str) -> tuple[str, str, str]:
        """Parse 'registry/repo/name:tag' into (registry, name, tag)."""
        if "://" in image_ref:
            parsed = urlparse(image_ref)
            image_ref = parsed.netloc + parsed.path

        parts = image_ref.split("/", 1)
        if len(parts) < 2:
            raise ValueError(f"Invalid image ref: {image_ref}")
        registry = parts[0]
        name_tag = parts[1]

        if ":" in name_tag.split("/")[-1]:
            last_colon = name_tag.rfind(":")
            name = name_tag[:last_colon]
            tag = name_tag[last_colon + 1 :]
        else:
            name = name_tag
            tag = "latest"

        return registry, name, tag

    @staticmethod
    async def _docker_cmd(*args: str, check: bool = True, env: dict | None = None, stdin_data: bytes | None = None):
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin_data else None,
            env=env,
        )
        try:
            stdout, stderr = await proc.communicate(input=stdin_data)
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        if check and proc.returncode != 0:
            raise RuntimeError(f"{' '.join(args)} failed (rc={proc.returncode}): {stderr.decode()}")


class _DockerAuthContext:
    """Context manager that creates isolated DOCKER_CONFIG with registry credentials."""

    def __init__(self, storage: DockerRegistryV2ImageStorage):
        self._storage = storage
        self._tmpdir = None

    async def __aenter__(self) -> dict:
        self._tmpdir = tempfile.mkdtemp()
        creds = base64.b64encode(f"{self._storage._username}:{self._storage._password}".encode()).decode()
        config = {"auths": {self._storage._registry_url: {"auth": creds}}}
        config_path = os.path.join(self._tmpdir, "config.json")
        with open(config_path, "w") as f:
            json.dump(config, f)
        env = os.environ.copy()
        env["DOCKER_CONFIG"] = self._tmpdir
        return env

    async def __aexit__(self, *args):
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
