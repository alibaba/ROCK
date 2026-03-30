# JobConfig Environment Refactor Design

**Date:** 2026-03-30
**Scope:** `rock/sdk/agent/models/job/config.py`, `rock/sdk/agent/job.py`, related tests and examples
**Breaking change:** Yes — no deprecation shims, direct replacement

---

## Problem

`JobConfig` currently exposes two overlapping environment concepts to users:

1. **Rock extension fields** (`sandbox_config`, `sandbox_env`, `setup_commands`, `file_uploads`, `auto_stop_sandbox`) — control the Rock sandbox lifecycle
2. **Harbor native `environment: EnvironmentConfig`** — Harbor's own env config, serialized into `harbor jobs start -c`

This creates confusion:
- Two separate `env` dicts with different semantics (`sandbox_env` vs `EnvironmentConfig.env`)
- Resource specs in two places (`SandboxConfig.cpus/memory` vs `EnvironmentConfig.override_cpus/memory_mb`)
- Users must understand Rock's internal layering to fill in config correctly

From the user's perspective, they're just running a Rock environment — they shouldn't need to know that Harbor is involved.

---

## Design

### New Model Hierarchy

```
JobConfig
├── environment: EnvironmentConfig        # NEW: unified env concept
│   ├── (Rock fields — main path)
│   └── advanced: AdvancedEnvConfig       # Harbor's EnvironmentConfig, for power users
└── (Harbor native fields — unchanged)
    job_name, jobs_dir, n_attempts, timeout_multiplier,
    agents, verifier, metrics, orchestrator, datasets, tasks, artifacts, ...
```

### `EnvironmentConfig` (new, Rock-centric)

Replaces both the old `SandboxConfig` and the old `EnvironmentConfig`.

```python
class EnvironmentConfig(BaseModel):
    # ── Rock sandbox connection ──
    base_url: str = env_vars.ROCK_BASE_URL
    extra_headers: dict[str, str] = Field(default_factory=dict)
    cluster: str = "zb"
    namespace: str | None = None
    route_key: str | None = None
    registry_username: str | None = None
    registry_password: str | None = None
    user_id: str | None = None
    experiment_id: str | None = None
    use_kata_runtime: bool = False
    sandbox_id: str | None = None

    # ── Rock sandbox runtime ──
    image: str = "python:3.11"
    image_os: str = "linux"
    memory: str = "8g"
    cpus: float = 2
    limit_cpus: float | None = None
    startup_timeout: float = env_vars.ROCK_SANDBOX_STARTUP_TIMEOUT_SECONDS
    auto_clear_seconds: int = 60 * 5

    # ── Unified env vars ──
    # Injected into the sandbox bash session; harbor inherits them naturally
    env: dict[str, str] = Field(default_factory=dict)

    # ── Job setup ──
    setup_commands: list[str] = Field(default_factory=list)
    file_uploads: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Files/dirs to upload before running: [(local_path, sandbox_path), ...]",
    )
    auto_stop: bool = False

    # ── Advanced: direct Harbor EnvironmentConfig override ──
    advanced: AdvancedEnvConfig = Field(default_factory=AdvancedEnvConfig)
```

### `AdvancedEnvConfig` (new, aligns with old `EnvironmentConfig`)

For power users who need to control Harbor's environment layer directly.

```python
class AdvancedEnvConfig(BaseModel):
    type: EnvironmentType | None = None
    import_path: str | None = None
    force_build: bool = False
    delete: bool = True
    override_cpus: int | None = None
    override_memory_mb: int | None = None
    override_storage_mb: int | None = None
    override_gpus: int | None = None
    suppress_override_warnings: bool = False
    mounts_json: list[dict[str, Any]] | None = None
    env: dict[str, str] = Field(default_factory=dict)  # Harbor-layer env only
    kwargs: dict[str, Any] = Field(default_factory=dict)
```

### `JobConfig` changes

Fields removed (breaking):
- `sandbox_config: SandboxConfig | None` → replaced by `environment`
- `sandbox_env: dict[str, str]` → replaced by `environment.env`
- `setup_commands: list[str]` → replaced by `environment.setup_commands`
- `file_uploads: list[tuple]` → replaced by `environment.file_uploads`
- `auto_stop_sandbox: bool` → replaced by `environment.auto_stop`
- `environment: EnvironmentConfig` (old Harbor type) → replaced by `environment.advanced`

Field added:
- `environment: EnvironmentConfig` (new unified type)

`_rock_fields` class variable removed — no longer needed.

---

## Serialization: `to_harbor_yaml()`

The `advanced` block maps directly to Harbor's `environment` YAML section.
All other `EnvironmentConfig` fields are Rock-internal and excluded.

```python
def to_harbor_yaml(self) -> str:
    import yaml
    data = self.model_dump(mode="json", exclude={"environment"}, exclude_none=True)
    advanced = self.environment.advanced.model_dump(mode="json", exclude_none=True)
    if advanced:
        data["environment"] = advanced
    return yaml.dump(data, default_flow_style=False, allow_unicode=True)
```

---

## `Job` class changes

- `self._config.sandbox_config` → `self._config.environment` (passed to `Sandbox(...)`)
- `self._config.sandbox_env` → `self._config.environment.env`
- `self._config.setup_commands` → `self._config.environment.setup_commands`
- `self._config.file_uploads` → `self._config.environment.file_uploads`
- `self._config.auto_stop_sandbox` → `self._config.environment.auto_stop`

`Sandbox(...)` currently takes a `SandboxConfig`. After this refactor it receives the new `EnvironmentConfig` directly, which subsumes all `SandboxConfig` fields.

---

## `SandboxConfig` impact

`SandboxConfig` in `rock/sdk/sandbox/config.py` is used by `Sandbox` client.
Two options:
1. `EnvironmentConfig` inherits from `SandboxConfig` (avoids changing `Sandbox` internals)
2. `Sandbox.__init__` accepts the new `EnvironmentConfig` directly (cleaner but touches more code)

**Decision: option 1** — `EnvironmentConfig` inherits from `SandboxConfig`, adding the Rock job-level fields on top. `Sandbox(config.environment)` works without touching `Sandbox` internals.

---

## User-facing YAML (before / after)

**Before:**
```yaml
sandbox_config:
  base_url: "http://rock-admin:8080"
  image: "my-harbor-image:latest"
  cluster: "zb"
  memory: "32g"
  cpus: 8
  startup_timeout: 1800
  auto_clear_seconds: 7200

sandbox_env:
  OPENAI_API_KEY: "sk-xxx"
  OPENAI_BASE_URL: "https://api.openai.com/v1"

setup_commands:
  - "pip install my-package"

auto_stop_sandbox: false

agents:
  - name: "swe-agent"
    model_name: "custom_openai/my-model"
```

**After:**
```yaml
environment:
  base_url: "http://rock-admin:8080"
  image: "my-harbor-image:latest"
  cluster: "zb"
  memory: "32g"
  cpus: 8
  startup_timeout: 1800
  auto_clear_seconds: 7200
  env:
    OPENAI_API_KEY: "sk-xxx"
    OPENAI_BASE_URL: "https://api.openai.com/v1"
  setup_commands:
    - "pip install my-package"
  auto_stop: false

agents:
  - name: "swe-agent"
    model_name: "custom_openai/my-model"
```

---

## Files to Change

| File | Change |
|------|--------|
| `rock/sdk/agent/models/job/config.py` | Add `AdvancedEnvConfig`, rewrite `EnvironmentConfig`, update `JobConfig`, update `to_harbor_yaml()` |
| `rock/sdk/agent/job.py` | Update all field references |
| `rock/sdk/sandbox/config.py` | `EnvironmentConfig` inherits from `SandboxConfig` (or adjust as needed) |
| `examples/harbor/swe_job_config.yaml.template` | Update to new structure |
| `examples/harbor/tb_job_config.yaml.template` | Update to new structure |
| `tests/unit/sdk/agent/test_job_config_serialization.py` | Update all test fixtures |
| `tests/unit/sdk/agent/test_models.py` | Update model tests |
| `tests/unit/sdk/agent/test_job.py` | Update job tests |
| `docs/dev/agent/README.md` | Update usage examples |

---

## `from_yaml()` override interface

Currently `from_yaml(path, **overrides)` accepts flat top-level field names as kwargs.
After refactor, callers passing Rock fields need to use nested form:

```python
# Before
JobConfig.from_yaml(path, setup_commands=["pip install x"])

# After — pass nested dict for environment fields
JobConfig.from_yaml(path, environment={"setup_commands": ["pip install x"]})
# or equivalently
JobConfig.from_yaml(path, environment=EnvironmentConfig(setup_commands=["pip install x"]))
```

`from_yaml()` implementation must handle merging `environment` overrides with YAML-loaded `environment` dict.

---

## Out of Scope

- Changes to `rock/sdk/sandbox/client.py` internals beyond constructor signature
- Changes to `SandboxGroupConfig`
- Any changes to Harbor itself
