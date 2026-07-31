# CLI Metadata Decoupling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove local CLI dependencies on persisted Job/Group metadata while retaining parameter-driven single-task resume and OSS artifact inspection.

**Architecture:** Introduce a persistence-neutral `ExistingJobHandle` at the executor boundary. The CLI validates and constructs that handle, while `UnifiedJobRunHandler` performs either a normal local run or one explicit resume without reading or writing metadata repositories.

**Tech Stack:** Python 3.10+, argparse, asyncio, Pydantic, pytest, Ruff

## Global Constraints

- Resume is limited to exactly one explicit `--task`.
- `--resume-sandbox-id` and `--resume-pid` are required together.
- `--resume-session` is optional and defaults to `rock-job-{job_name}`.
- Resume never falls back to creating a new sandbox.
- The CLI does not connect to PostgreSQL or a metadata service.
- OSS artifact commands remain available.

---

### Task 1: Define the persistence-neutral resume boundary

**Files:**
- Modify: `rock/sdk/job/executor.py`
- Modify: `rock/sdk/job/__init__.py`
- Test: `tests/unit/sdk/job/test_executor.py`

**Interfaces:**
- Produces: `ExistingJobHandle(sandbox_id: str, session: str, pid: int)`
- Produces: `JobExecutor.wait_existing_job(planned_job: PlannedJob, handle: ExistingJobHandle) -> TrialResult | list[TrialResult]`

- [ ] **Step 1: Write the failing executor test**

```python
async def test_wait_existing_job_reconnects_from_explicit_handle():
    handle = ExistingJobHandle(sandbox_id="sb-old", session="rock-job-old", pid=42)
    result = await executor.wait_existing_job(planned_job, handle)
    assert result.task_name == "task-1"
    assert sandbox._sandbox_id == "sb-old"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -p pytest_asyncio.plugin tests/unit/sdk/job/test_executor.py -q`

Expected: FAIL because `ExistingJobHandle` does not exist.

- [ ] **Step 3: Implement the minimal executor boundary**

```python
@dataclass(frozen=True)
class ExistingJobHandle:
    sandbox_id: str
    session: str
    pid: int
```

Replace the `JobMeta` parameter and field access in `wait_existing_job` with this type, then export it lazily through `rock.sdk.job`.

- [ ] **Step 4: Run the executor tests**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -p pytest_asyncio.plugin tests/unit/sdk/job/test_executor.py tests/unit/sdk/job/test_integration.py -q`

Expected: PASS.

### Task 2: Make CLI orchestration local-only

**Files:**
- Modify: `rock/cli/job_run.py`
- Test: `tests/unit/cli/test_job_run.py`

**Interfaces:**
- Consumes: `ExistingJobHandle`
- Produces: `UnifiedJobRunHandler(..., resume_handle: ExistingJobHandle | None = None)`

- [ ] **Step 1: Replace metadata-writing tests with failing local-only tests**

```python
async def test_unified_handler_runs_all_tasks_without_metadata_repositories():
    handler = UnifiedJobRunHandler(
        mode="multi",
        task_ids=["t1", "t2"],
        dataset_ref=DatasetRef(None, None, None),
        run_id="run-1",
        executor=executor,
        progress=NullProgressReporter(),
    )
    result = await handler.run(config)
    assert result.total == 2
```

```python
async def test_single_resume_waits_for_explicit_handle_without_starting_new_job():
    handler = UnifiedJobRunHandler(
        mode="single",
        task_ids=["t1"],
        dataset_ref=DatasetRef(None, None, None),
        run_id="run-1",
        executor=executor,
        progress=NullProgressReporter(),
        resume_handle=handle,
    )
    await handler.run(config)
    assert executor.waited_handle == handle
```

- [ ] **Step 2: Run the CLI orchestration tests to verify they fail**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -p pytest_asyncio.plugin tests/unit/cli/test_job_run.py -q`

Expected: FAIL because repository arguments are required and explicit handles are unsupported.

- [ ] **Step 3: Remove persisted metadata orchestration**

Delete repository/model imports and the `_make_run_meta`, `_write_run_meta`, `_get_recoverable_job_meta`, `_mark_previous_attempt_unrecoverable`, and `_write_job_meta` methods. Branch in `run_one`: a resume handle calls `wait_existing_job`; otherwise call `run_job`.

- [ ] **Step 4: Run the CLI orchestration tests**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -p pytest_asyncio.plugin tests/unit/cli/test_job_run.py -q`

Expected: PASS.

### Task 3: Replace CLI metadata commands and parameters

**Files:**
- Modify: `rock/cli/command/job.py`
- Test: `tests/unit/cli/command/test_job.py`

**Interfaces:**
- Consumes: `ExistingJobHandle`
- Produces CLI flags: `--job-name`, `--resume-sandbox-id`, `--resume-pid`, `--resume-session`

- [ ] **Step 1: Write failing parser and validation tests**

```python
def test_run_parser_supports_explicit_single_task_resume():
    ns = parser.parse_args([
        "job", "run", "--job-config", "job.yaml", "--task", "t1",
        "--job-name", "old-job", "--resume-sandbox-id", "sb-1",
        "--resume-pid", "42",
    ])
    assert ns.resume_sandbox_id == "sb-1"
    assert ns.resume_pid == 42
```

Add behavior tests for derived/explicit session, missing required resume fields, forbidden multi-task/concurrency arguments, and removed `run-list`, `run-status`, and legacy `--resume`.

- [ ] **Step 2: Run the command tests to verify they fail**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -p pytest_asyncio.plugin tests/unit/cli/command/test_job.py -q`

Expected: FAIL because the new flags and validation do not exist.

- [ ] **Step 3: Implement parameter-driven resume and remove metadata commands**

Create the handle as:

```python
session = args.resume_session or f"rock-job-{config.job_name}"
resume_handle = ExistingJobHandle(
    sandbox_id=args.resume_sandbox_id,
    session=session,
    pid=args.resume_pid,
)
```

Remove the `run-list` and `run-status` dispatch/parser/methods. Make `job-show` require a job artifact name, read `get_job_result`, and make `--job-config` artifact lookup call `JobViewer.from_oss_mirror(config.environment.oss_mirror)`.

- [ ] **Step 4: Run command tests**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -p pytest_asyncio.plugin tests/unit/cli/command/test_job.py -q`

Expected: PASS.

### Task 4: Regression verification and documentation alignment

**Files:**
- Modify: `docs/dev/cli/README.md` only if it documents removed commands or legacy resume.
- Test: `tests/unit/cli`
- Test: `tests/unit/sdk/job`

**Interfaces:**
- Consumes all prior task interfaces.
- Produces a verified CLI and SDK test baseline.

- [ ] **Step 1: Search and update user-facing legacy references**

Run: `rg -n -- '--resume|run-list|run-status|run_id.*task_id' docs rock/cli tests/unit/cli`

Replace only documentation and tests describing the removed CLI contract.

- [ ] **Step 2: Run focused CLI and SDK tests**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -p pytest_asyncio.plugin --confcutdir=tests/unit/cli tests/unit/cli -q`

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -p pytest_asyncio.plugin tests/unit/sdk/job -q`

Expected: PASS.

- [ ] **Step 3: Run Ruff on changed Python files**

Run: `.venv/bin/ruff check rock/cli/command/job.py rock/cli/job_run.py rock/sdk/job/executor.py rock/sdk/job/__init__.py tests/unit/cli tests/unit/sdk/job/test_executor.py`

Expected: PASS.

- [ ] **Step 4: Review the final diff**

Run: `git diff --check`

Run: `git diff --stat`

Expected: no whitespace errors and only scoped CLI/executor/tests/docs changes.
