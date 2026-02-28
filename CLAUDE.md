# CLAUDE.md

## Project Overview

ROCK (Reinforcement Open Construction Kit) is a sandbox environment management framework for agentic AI / reinforcement learning. It provides multi-protocol sandbox lifecycle management with Docker, Ray, and Kubernetes deployment backends.

Package name: `rl-rock`, version 1.3.0, Python 3.10–3.12.

## Quick Reference

```bash
# Setup
make init                          # Create venv, install deps, hooks, preflight checks
uv sync --all-extras --all-groups  # Install all dependencies

# Test
uv run pytest -m "not need_ray and not need_admin and not need_admin_and_network" --reruns 1  # Fast tests
uv run pytest -m "need_ray" --reruns 1                                                        # Ray tests
uv run pytest -m "need_admin" --reruns 1                                                      # Admin tests

# Lint / Format
uv run ruff check --fix .          # Lint with autofix
uv run ruff format .               # Format
```

## Architecture

### Services (Entry Points)

| Command   | Module                    | Role                                              |
|-----------|---------------------------|----------------------------------------------------|
| `rock`    | `rock.cli.main`           | CLI tool (admin start, sandbox build/push/run)     |
| `admin`   | `rock.admin.main`         | FastAPI orchestrator — sandbox lifecycle via API    |
| `rocklet` | `rock.rocklet.server`     | FastAPI proxy — runs inside containers, executes commands |
| `envhub`  | `rock.envhub.server`      | Environment repository CRUD server                 |

### Core Module Map

```
rock/
├── admin/          # Admin service: API routers, Ray service, scheduler, metrics
├── sandbox/        # SandboxManager, Operators (Ray/K8s), SandboxActor
├── deployments/    # AbstractDeployment → Docker/Ray/Local/Remote, configs, validator
├── rocklet/        # Lightweight sandbox runtime server
├── sdk/            # Client SDK: Sandbox client, agent integrations, EnvHub client
├── envhub/         # Environment hub service with SQLModel database
├── actions/        # Request/response models for sandbox and env actions
├── config.py       # Dataclass-based config (RayConfig, K8sConfig, RuntimeConfig, ...)
├── env_vars.py     # Environment variables with lazy defaults via __getattr__
├── cli/            # CLI commands (argparse)
└── utils/          # Docker wrapper, Redis/Nacos providers, HTTP, retry, crypto, etc.
```

### Key Patterns

- **Operator pattern**: `AbstractOperator` → `RayOperator` / `K8sOperator` — decouples scheduling from execution
- **Deployment hierarchy**: `AbstractDeployment` → `DockerDeployment` → `RayDeployment`, plus `LocalDeployment`, `RemoteDeployment`
- **Actor pattern (Ray)**: `SandboxActor` (remote, detached) wraps a `DockerDeployment` instance
- **Config flow**: `SandboxManager` → `DeploymentManager.init_config()` (normalize config) → `Operator.submit()` (orchestrate)
- **Validation**: `SandboxValidator` / `DockerSandboxValidator` → `DockerUtil` (shell out to docker CLI)

## Code Conventions

### Style

- Line length: 120 (`ruff`)
- Lint rules: `E, F, I, W, UP` (pycodestyle, pyflakes, isort, warnings, pyupgrade)
- Ignored: `E501` (line length), `F811` (redefinition), `E741` (ambiguous names)
- Import order: stdlib → third-party → local, managed by `ruff` isort

### Naming

- Classes: `PascalCase` (`SandboxManager`, `RayOperator`)
- Functions/methods: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Private: `_leading_underscore`

### Logging

- Always use `from rock.logger import init_logger; logger = init_logger(__name__)`
- Context vars for distributed tracing: `sandbox_id_ctx_var`, `trace_id_ctx_var`

### Error Handling

- Custom exceptions: `BadRequestRockError` (4xxx), `InternalServerRockError` (5xxx), `CommandRockError` (6xxx)
- Status codes defined in `rock._codes`

### Data Models

- Pydantic v2 for API request/response models and deployment configs
- `dataclass` for internal configs (`RockConfig`, `RayConfig`, etc.)
- SQLModel for database ORM (`envhub`)

### Async

- `asyncio_mode = "auto"` in pytest — all async tests run automatically
- FastAPI async handlers throughout
- Ray async operations via `async_ray_get()`, `async_ray_get_actor()`

## Testing

### Structure

```
tests/
├── unit/           # Fast isolated tests
│   ├── conftest.py # Fixtures: rock_config, redis_provider (FakeRedis), sandbox_manager, ray_*
│   ├── sandbox/    # SandboxManager tests
│   ├── rocklet/    # Rocklet tests
│   └── admin/      # Admin tests
├── integration/    # Tests needing external services (Docker, network)
│   └── conftest.py # SKIP_IF_NO_DOCKER, rocklet/admin remote servers
└── conftest.py     # Global config
```

### Markers

| Marker                     | Purpose                              |
|----------------------------|--------------------------------------|
| `@pytest.mark.need_ray`    | Requires running Ray cluster         |
| `@pytest.mark.need_admin`  | Requires admin service               |
| `@pytest.mark.need_admin_and_network` | Requires admin + network    |
| `@pytest.mark.slow`        | Long-running tests                   |
| `@pytest.mark.integration` | Integration tests                    |

Strict markers enabled (`--strict-markers`). All markers must be registered in `pyproject.toml`.

### CI Pipeline (`.github/workflows/python-ci.yml`)

Runs in 4 phases on `self-hosted` runner:
1. Fast tests (no external deps)
2. Ray-dependent tests
3. Admin-dependent tests
4. Network-dependent tests

## Configuration

### Environment Variables

All defined in `rock/env_vars.py` with lazy evaluation via module `__getattr__`. Key variables:

| Variable                | Default                | Purpose                     |
|-------------------------|------------------------|-----------------------------|
| `ROCK_ADMIN_ENV`        | `dev`                  | Environment: local/dev/test |
| `ROCK_WORKER_ENV_TYPE`  | `local`                | Worker type: local/docker/uv/pip |
| `ROCK_CONFIG`           | (none)                 | Path to YAML config file    |
| `ROCK_BASE_URL`         | `http://localhost:8080` | Admin service URL          |
| `ROCK_LOGGING_LEVEL`    | `INFO`                 | Log level                   |
| `ROCK_TIME_ZONE`        | `Asia/Shanghai`        | Timezone                    |
| `ROCK_RAY_NAMESPACE`    | `xrl-sandbox`          | Ray namespace               |

### YAML Config (`rock-conf/`)

Loaded by `RockConfig.from_env()`. Files: `rock-local.yml`, `rock-dev.yml`, `rock-test.yml`.

Key sections: `ray`, `k8s`, `runtime` (operator_type, standard_spec, max_allowed_spec), `redis`, `proxy_service`, `scheduler`.

## Git Workflow

### Branch & PR Rules

1. **先建 Issue** — 任何代码变更必须先创建 GitHub Issue 描述问题或需求
2. **创建分支** — 从 `master` 拉分支开发
3. **PR 必须关联 Issue** — CI 会通过 `pr-issue-link-check.yml` 检查，未关联的 PR 会被阻断

PR 关联 Issue 的方式（二选一）：
- PR body 中使用关键字：`fixes #123`、`closes #123`、`resolves #123`、`refs #123`
- PR title 中包含 issue 编号：`[FEATURE] Add new feature (#123)`

```bash
# 典型流程
# 1. 在 GitHub 上创建 Issue
# 2. 本地创建分支
git checkout -b feat/my-feature master

# 3. 开发、提交
git add <files>
git commit -m "feat: add my feature"

# 4. 推送并创建 PR（body 中关联 issue）
git push -u origin feat/my-feature
gh pr create --title "feat: add my feature" --body "fixes #123"
```

### Pre-commit Hooks

- `ruff --fix` (lint) + `ruff format` (format)
- Custom hook: prevents mixing internal (`xrl/`, `intetest/`) and external files in one commit

## Dependencies

Core: FastAPI, Pydantic v2, Ray 2.43.0, Redis, Docker CLI, Kubernetes client, APScheduler, OpenTelemetry, httpx.

Package manager: `uv` (Rust-based). Build: setuptools + wheel.
