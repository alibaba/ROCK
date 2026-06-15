---
sidebar_position: 5
---

# Start Sandbox from Dockerfile

ROCK SDK accepts not only a pre-built image tag for `SandboxConfig.image`, but also an `Image` declaration. With `Image.from_dockerfile(path)`, the SDK transparently builds and pushes the image inside a builder sandbox, then starts your sandbox from it — no need to run `docker build` / `docker push` yourself.

## Quick Start

```python
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.sdk.sandbox.image import Image, ImageRegistry

image = Image.from_dockerfile(
    "/path/to/env_dir",                  # dir containing a Dockerfile, OR a Dockerfile file path
    registry=ImageRegistry(
        url="reg.example.com",
        namespace="my-team",
        repository="my-env",
        username="...",
        password="...",
    ),
)

sandbox = Sandbox(SandboxConfig(image=image, memory="2g", cpus=1.0))
await sandbox.start()
```

On `start()` the SDK will:

1. Hash the build context (`SHA-256` over all files in `env_dir`).
2. Check the registry for an existing image with the same hash; if found, skip build + push.
3. Otherwise launch a builder sandbox, run `docker build` and `docker push` inside it.
4. Start your sandbox from the resulting image tag.

Subsequent runs with the same `env_dir` content hit the cache instantly.

## Image Naming

The final image tag is composed of four parts:

```
{registry.url}/{registry.namespace}/{registry.repository}:{content_hash}
```

| Segment | Source |
|---|---|
| `registry.url` | field on `ImageRegistry`, or fetched from admin `image` config |
| `registry.namespace` | field on `ImageRegistry`, or fetched from admin `image` config |
| `registry.repository` | field on `ImageRegistry`, or `SandboxConfig.user_id` (fallback `"default"`) |
| `content_hash` | always a 64-character SHA-256 of the build context; user cannot override |

Using the content hash as the tag means any change to your Dockerfile or build-context files automatically produces a new tag, so cache hits and rebuilds are deterministic.

## API Reference

```python
class ImageRegistry(BaseModel):
    url: str | None = None
    namespace: str | None = None
    repository: str | None = None
    username: str | None = None
    password: str | None = None


Image.from_dockerfile(
    path: str | Path,
    *,
    registry: ImageRegistry | None = None,
    force_build: bool = False,
    build_args: dict[str, str] | None = None,
    builder_config: BuilderConfig | None = None,
)
```

| Parameter | Purpose |
|---|---|
| `path` | Either (a) a local directory containing a `Dockerfile` and any files it `COPY`s, or (b) a path to a single `Dockerfile` file. In file mode the surrounding directory is ignored — only the Dockerfile is the build context, so it must be self-contained (no `COPY` from local files). |
| `registry` | `ImageRegistry` POJO with the push target and credentials. Any unset field is populated from the admin `image` config at `Sandbox.start()`; `registry.repository` falls back to `SandboxConfig.user_id`. Registry credentials (username/password) are obtained automatically via temporary ACR tokens from the admin service. |
| `force_build` | Skip the cache check and always rebuild. |
| `build_args` | Passed through to `docker build --build-arg KEY=VAL`. |
| `builder_config` | `BuilderConfig` (a subclass of `SandboxConfig`) for the builder sandbox itself — gives you control over its image, memory, cpus, timeouts, etc. `BuilderConfig` narrows `image` to `str` (enforced by pydantic) and defaults to admin-configured builder image + builder-appropriate timeouts. When omitted, a `BuilderConfig` is derived from your sandbox's `SandboxConfig`. |

When `builder_config` is omitted, the builder sandbox inherits the inheritable fields (`base_url`, `cluster`, `extra_headers`, etc.) from your `SandboxConfig`; `image` / `startup_timeout` / `auto_clear_seconds` fall back to `BuilderConfig` defaults.

## Configuration

Image registry and builder defaults are managed centrally in the admin YAML config (`rock-conf/rock-*.yml`). SDK clients fetch them automatically from the admin `/acr_config` endpoint at `Sandbox.start()` time — no per-client configuration needed.

```yaml
# rock-dev.yml
image:
  registry:
    url: "reg.example.com"
    namespace: "my-team"
    instance_id: "cri-xxxxxx"        # ACR enterprise instance ID
    region: "cn-hangzhou"
    access_key_id: "..."             # admin-side only, never exposed to SDK
    access_key_secret: "..."
  builder:
    image: "rock-n-roll-registry.cn-hangzhou.cr.aliyuncs.com/rock/rock-env-builder:latest"
    startup_timeout: 600
    auto_clear_seconds: 1800
```

Registry credentials are issued as temporary ACR tokens (15-minute TTL) by the admin service. SDK clients never hold long-lived credentials.

## Custom Builder Image

The build runs inside a short-lived builder sandbox (a container running its own dockerd — i.e. DinD). The default builder image is pre-configured to work in this environment; you only need to read this section if you override it in the admin config.

Inside the builder, `docker build` uses BuildKit by default (Docker 23+). In a container-on-container layout this places two requirements on the builder image:

1. **dockerd's data directory must live on a non-overlay filesystem.** BuildKit mounts overlay under `<data-root>/buildkit/`; if `data-root` itself sits on the sandbox's overlay rootfs, the mount fails with `invalid argument` (overlay-on-overlay).
2. **No stale dockerd pidfiles in the image.** If `/var/run/docker.pid` or `/run/docker/containerd/containerd.pid` are baked into the image, dockerd refuses to start with `process with PID N is still running`.

The default builder image satisfies both by:

- Setting `data-root` to `/data/logs/docker` in `/etc/docker/daemon.json`. ROCK bind-mounts an XFS volume there for every sandbox (originally for log quotas), so dockerd data lands on XFS, not on the overlay rootfs.
- Setting `"features": {"containerd-snapshotter": false}` so BuildKit uses dockerd's classic graph driver instead of the independent containerd-overlayfs snapshotter.
- Removing baked-in pidfiles at image build time.

If you build your own builder image, replicate the same configuration — or use the default and avoid the issue entirely.

## Notes

- The builder sandbox is short-lived: created on demand for the build, destroyed afterwards. Cache is in the registry, not in the builder.
- A second build of the same `Image` returns instantly via `docker manifest inspect` + content-hash label check.
- For pre-built images (no build needed), just pass the tag string directly to `SandboxConfig.image` — `Image` is only used when you want SDK-driven `docker build` + `docker push`.
