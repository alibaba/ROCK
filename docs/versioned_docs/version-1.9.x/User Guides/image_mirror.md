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

By default, `rock image mirror` pushes images to the **Singapore** registry. Images are then automatically replicated to Shanghai by ACR's built-in cross-region sync.

> **Note:** The ACR replication may experience delays or task queuing under high load. If you need images available in Shanghai immediately, you can mirror directly to Shanghai by specifying `--cluster vpc-nt-a` in remote mode (see [Mirror Directly to Shanghai](#mirror-directly-to-shanghai)).

## Prerequisites

Install the latest version of `rockcli`:

```bash
bash -c "$(curl -fsSL http://xrl.alibaba-inc.com/install_beta.sh)"
```

Verify the installation:

```bash
rock --help
```

## Preparing the Image List

The `rock image mirror` command reads a JSONL file where each line is a JSON object containing a `docker_image` field. This follows the SWE-bench instance format.

**Example file** (`images.jsonl`):

```jsonl
{"instance_id": "example_1", "docker_image": "docker.io/library/python:3.11"}
{"instance_id": "example_2", "docker_image": "ghcr.io/my-org/my-image:v1.0"}
{"instance_id": "example_3", "docker_image": "ubuntu:22.04"}
```

The `docker_image` field must be a full image reference including registry (if not Docker Hub), namespace, name, and tag. If no tag is specified, `latest` is used by default.

## Command Reference

### `rock image mirror`

Mirror images from a source registry to the ROCK target registry.

```bash
rock image mirror -f <file> \
  [--target-registry <target_registry_url>] \
  [--target-username <target_username>] \
  [--target-password <target_password>] \
  [--source-registry <source_registry_url>] \
  [--source-username <source_username>] \
  [--source-password <source_password>] \
  [--mode <local|remote>] \
  [--concurrency <1-50>]
```

#### Required Parameters

| Parameter | Description |
|-----------|-------------|
| `-f, --file` | Path to the JSONL file containing the image list |

#### Optional Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--target-registry` | Built-in (rock-instances Singapore) | Target ACR registry URL. Defaults to the ROCK image registry, usually no need to specify |
| `--target-username` | Built-in | Target registry username. Defaults to the built-in ROCK ACR credential |
| `--target-password` | Built-in | Target registry password. Defaults to the built-in ROCK ACR credential |
| `--source-registry` | *(none)* | Source registry URL. Required only when the source registry needs authentication |
| `--source-username` | *(none)* | Source registry username |
| `--source-password` | *(none)* | Source registry password |
| `--mode` | `local` | Mirror mode: `local` (run on current machine) or `remote` (run on ROCK sandboxes) |
| `--concurrency` | `3` | Number of concurrent mirror tasks (1–50). Only applies to `remote` mode |

> **Note:** The `--target-registry`, `--target-username`, and `--target-password` are built into `rockcli` by default, pointing to the ROCK ACR (rock-instances). In most cases you only need to provide `-f` to start mirroring.

## Usage Examples

### Mirror Public Images (Local Mode)

For images from public registries (Docker Hub, etc.) that don't need authentication, simply provide the image list file:

```bash
rock image mirror -f images.jsonl
```

The built-in target registry credentials are used automatically.

### Mirror Private Images (Local Mode)

When the source images require authentication, provide the source registry credentials:

```bash
rock image mirror -f images.jsonl \
  --source-registry ghcr.io \
  --source-username <your_source_username> \
  --source-password <your_source_password>
```

### Mirror Images in Remote Mode

For large-scale mirroring, use `remote` mode to distribute tasks across multiple ROCK sandboxes. This requires `--auth-token` and `--cluster` to be configured (via global flags or config file):

```bash
rock --auth-token <token> --cluster <cluster_name> \
  image mirror -f images.jsonl \
  --mode remote \
  --concurrency 10
```

### Mirror Directly to Shanghai

If ACR cross-region sync is blocked or too slow, you can bypass it by mirroring directly to the Shanghai registry. Specify `--cluster vpc-nt-a` to run the remote tasks on the Shanghai cluster, and override `--target-registry` to the Shanghai endpoint:

```bash
rock --auth-token <token> --cluster vpc-nt-a \
  image mirror -f images.jsonl \
  --mode remote \
  --concurrency 10 \
  --target-registry rock-instances-registry.cn-hangzhou.cr.aliyuncs.com
```

## How It Works

1. **Parse** — Each line in the JSONL file is read; the `docker_image` field is extracted.
2. **Check** — The tool logs into the target registry and checks if the image already exists. If it does, the image is skipped.
3. **Pull** — The image is pulled from the source registry (logging in first if source credentials are provided).
4. **Tag** — The image is re-tagged to match the target registry URL while preserving the original namespace, name, and tag.
5. **Push** — The re-tagged image is pushed to the target registry.

Each image mirror operation retries up to 3 times on failure.

### Image Name Mapping

The original image name is mapped to the target registry while preserving its structure:

```
Source: ghcr.io/my-org/my-image:v1.0
Target: rock-instances-registry.ap-southeast-1.cr.aliyuncs.com/my-org/my-image:v1.0

Source: docker.io/library/python:3.11
Target: rock-instances-registry.ap-southeast-1.cr.aliyuncs.com/library/python:3.11
```

## Build Results

The mirror results are saved to `data/output/env-build/result.jsonl`. Each line contains the original instance record with two additional fields:

| Field | Description |
|-------|-------------|
| `rock_env_build_result` | `SUCCESS` or `FAILED` |
| `rock_env_build_message` | Success message or error traceback |
