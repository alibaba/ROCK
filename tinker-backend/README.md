# Tinker Backend

FastAPI control plane for the Tinker backend prototype. SDKs and runtime adapters call the backend HTTP API; the backend owns runtime state, futures, actions, and environment records.

## Development

This container uses `uv` and Python 3.12. The project is configured to use the Aliyun PyPI mirror through `[tool.uv]` in `pyproject.toml`.

```bash
cd ~/tinker-backend
uv sync
cp .env.example .env
uv run tinker-backend
```

Health checks:

```bash
curl http://127.0.0.1:9000/healthz
curl http://127.0.0.1:9000/readyz
curl http://127.0.0.1:9000/configz
```

Do not commit real AccessKey, database password, auth token, or runtime credentials. Put local secrets only in `.env`.


## Runtime MVP

The backend defaults to a local SQLite database at `./tinker_backend.sqlite3`. PostgreSQL remains available by explicitly setting `DATABASE_URL=postgresql+asyncpg://...`. RocketMQ is intentionally not required for this MVP.

Implemented SDK-facing endpoints:

```text
GET  /api/v1/healthz
POST /api/v1/client/config
POST /api/v1/create_session
POST /api/v1/session_heartbeat
POST /api/v1/runtimes
POST /api/v1/create_runtime
POST /api/v1/retrieve_future
GET  /api/v1/runtimes/{runtime_id}
```

Runtime creation accepts case-insensitive `runtime_type` values and stores the canonical lowercase value: `roll`, `verl`, or `dummy`. The request transports opaque YAML as `config_type="yaml"` plus `config_content`; SDK-side `config_path` should be read into `config_content` before sending.

Runtime creation is a wait-ready future:

- `POST /api/v1/runtimes` / `POST /api/v1/create_runtime` returns a `future_id` immediately.
- If the runtime has a launch script, the backend starts it and leaves the create-runtime future `pending`.
- The runtime must call `POST /api/v1/runtimes/{runtime_id}/heartbeat` with `ready=true` when its lifecycle has reached usable state.
- That ready heartbeat completes the create-runtime future; SDK callers should wait by calling `POST /api/v1/retrieve_future`.
- `GET /api/v1/runtimes/{runtime_id}` is for observability/debugging, not a separate SDK wait-ready protocol.

Long-poll behavior for `retrieve_future` is currently backend-side `600s` timeout with periodic database polling. If no result is ready by then, the backend returns `408`; SDK retry/backoff policy should live in the SDK.

Launch config supports a minimal YAML shape:

```yaml
runtime:
  launch_script: /root/tinker-dummy/run_dummy.py
  workdir: /root/tinker-dummy
  env:
    TINKER_DUMMY_HEARTBEAT_INTERVAL: "30"
backend:
  base_url: http://127.0.0.1:9000
```

`dummy` defaults to `/root/tinker-dummy/run_dummy.py` if no launch script is specified. `roll` and `verl` can use the same launch-script plus heartbeat contract once their launcher scripts are added; without a launch script they remain recorded as `launch_not_implemented`.
