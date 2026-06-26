---
sidebar_position: 6
---

# Image Mirror

ROCK uses a unified ACR (Alibaba Cloud Container Registry) — **rock-instances** — to manage sandbox images. When using custom Docker images, you must mirror them to the ROCK image registry before they can be used in sandboxes.

## Registry Regions

The **rock-instances** ACR is deployed in two regions:

| Region | Registry URL | Role |
|--------|-------------|------|
| Singapore (ap-southeast-1) | `rock-instances-registry.ap-southeast-1.cr.aliyuncs.com` | Primary mirror target |
| Shanghai (cn-hangzhou) | `rock-instances-registry.cn-hangzhou.cr.aliyuncs.com` | Synced from Singapore via ACR replication |

By default, `rockcli image mirror` pushes images to the **Singapore** registry. Images are then automatically replicated to Shanghai by ACR's built-in cross-region sync.

> **Note:** The ACR replication may experience delays or task queuing under high load. If you need images available in Shanghai immediately, you can mirror directly to Shanghai by specifying `--cluster vpc-nt-a` in remote mode (see [Mirror Directly to Shanghai](#mirror-directly-to-shanghai)).

## Prerequisites

Install the latest version of `rockcli`:

```bash
bash -c "$(curl -fsSL http://xrl.alibaba-inc.com/install_beta.sh)"
```

Verify the installation:

```bash
rockcli --help
```

## Command Reference

### `rockcli image mirror`

Mirror images from a source registry to the ROCK target registry.

```bash
rockcli image mirror <image> [<image>...] [options]
```

Images can also be provided via `-f <path>` (supports `txt`, `jsonl`, `skopeo` formats).

#### Key Options

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--mode` | `skopeo` | `skopeo` (recommended, skopeo copy in sandboxes), `remote` (docker pull/tag/push in sandboxes), `local` (local Docker) |
| `--concurrency` | *(auto)* | `skopeo`/`remote`: number of sandboxes; `local`: parallel image count |
| `-c, --cluster` | *(none)* | Cluster routing hint. `vpc-sg-*` routes to overseas ACR, other `vpc-*` routes to domestic ACR |
| `--source-username` | *(none)* | Source registry username |
| `--source-password` | *(none)* | Source registry password |
| `--target-registry` | Built-in | Target registry URL. Overrides the region default when explicitly provided |
| `--force, -F` | `false` | Force re-transfer even if target digest matches |
| `--resume` | `false` | Resume from progress file |

> **Note:** The target registry credentials are built into `rockcli` by default. In most cases you only need to provide the source images to start mirroring. Run `rockcli image mirror --help` for the full option list.

## Usage Examples

### Mirror Public Images

For images from public registries (Docker Hub, etc.) that don't need authentication:

```bash
rockcli image mirror rex-registry-vpc.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11
```

Mirror multiple images at once:

```bash
rockcli image mirror rex-registry-vpc.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11 ubuntu:22.04
```

### Mirror Private Images

When the source images require authentication, provide the source registry credentials:

```bash
rockcli image mirror ghcr.io/my-org/my-image:v1.0 \
  --source-username <your_source_username> \
  --source-password <your_source_password>
```

### Mirror with Local Docker

Use `local` mode to mirror via the local Docker daemon instead of sandboxes:

```bash
rockcli image mirror rex-registry-vpc.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11 --mode local
```

### Mirror Directly to Shanghai

If ACR cross-region sync is blocked or too slow, you can bypass it by mirroring directly to the Shanghai registry via `--cluster vpc-nt-a`:

```bash
rockcli image mirror rex-registry-vpc.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11 \
  --cluster vpc-nt-a \
  --target-registry rock-instances-registry.cn-hangzhou.cr.aliyuncs.com
```

## Using the Mirrored Image

After mirroring completes, `rockcli` will output the mirrored image address. Use this address when creating sandboxes:

```
rock-instances-registry.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11
```

## How It Works

1. **Parse** — Source images are parsed from the command line arguments (or from a file if `-f` is used).
2. **Check** — The tool logs into the target registry and checks if the image already exists. If it does, the image is skipped.
3. **Pull** — The image is pulled from the source registry (logging in first if source credentials are provided).
4. **Tag** — The image is re-tagged to match the target registry URL while preserving the original namespace, name, and tag.
5. **Push** — The re-tagged image is pushed to the target registry.

Each image mirror operation retries up to 3 times on failure.

### Image Name Mapping

The original image name is mapped to the target registry while preserving its structure:

```
Source: rex-registry-vpc.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11
Target: rock-instances-registry.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11

Source: ghcr.io/my-org/my-image:v1.0
Target: rock-instances-registry.ap-southeast-1.cr.aliyuncs.com/my-org/my-image:v1.0
```

## Build Results

The mirror results are saved to `data/output/env-build/result.jsonl`. Each line contains the original instance record with two additional fields:

| Field | Description |
|-------|-------------|
| `rock_env_build_result` | `SUCCESS` or `FAILED` |
| `rock_env_build_message` | Success message or error traceback |
