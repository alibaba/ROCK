"""JobViewer — query agent job artifacts and status from OSS.

Reads Harbor job results, trajectories, logs and artifact files that were
persisted to OSS via the OSS mirror mechanism.

OSS key layout:
    artifacts/<namespace>/<experiment_id>/<job_name>/<trial_name>/...
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

import oss2
from pydantic import BaseModel, Field

from rock.sdk.bench.models.trial.result import HarborTrialResult

if TYPE_CHECKING:
    from rock.sdk.envhub.config import OssMirrorConfig

RESERVED_JOB_DIR_NAMES = {"_meta"}

# ---------------------------------------------------------------------------
# Data models (lightweight, co-located to avoid unnecessary modules)
# ---------------------------------------------------------------------------


class VerifierOutput(BaseModel):
    stdout: str | None = None
    stderr: str | None = None
    ctrf: str | None = None


class ArtifactManifestEntry(BaseModel):
    source: str
    destination: str
    type: str


class FileInfo(BaseModel):
    path: str
    name: str
    is_dir: bool
    size: int | None = None


class ArtifactsData(BaseModel):
    files: list[FileInfo] = Field(default_factory=list)
    manifest: list[ArtifactManifestEntry] | None = None


class CommandLog(BaseModel):
    index: int
    content: str


class AgentLogs(BaseModel):
    oracle: str | None = None
    setup: str | None = None
    commands: list[CommandLog] = Field(default_factory=list)
    summary: str | None = None


# ---------------------------------------------------------------------------
# JobViewer
# ---------------------------------------------------------------------------


class JobViewer:
    """Read agent job artifacts and status from OSS. Synchronous API.

    OSS key: ``artifacts/<namespace>/<experiment_id>/<relative_path>``

    Usage::

        viewer = JobViewer.from_credentials(
            oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
            oss_bucket="my-bucket",
            access_key_id="...", access_key_secret="...",
            namespace="my-ns", experiment_id="exp-001",
        )
        for job in viewer.list_jobs():
            print(viewer.get_job_result(job))
    """

    def __init__(self, bucket: oss2.Bucket, namespace: str, experiment_id: str):
        self._bucket = bucket
        self._prefix = f"artifacts/{namespace}/{experiment_id}/"

    # ── Factory methods ──────────────────────────────────────────────

    @classmethod
    def from_credentials(
        cls,
        *,
        oss_endpoint: str,
        oss_bucket: str,
        access_key_id: str,
        access_key_secret: str,
        namespace: str,
        experiment_id: str,
        oss_region: str | None = None,
    ) -> JobViewer:
        auth = oss2.Auth(access_key_id, access_key_secret)
        bucket = oss2.Bucket(auth, oss_endpoint, oss_bucket, region=oss_region)
        return cls(bucket, namespace, experiment_id)

    @classmethod
    def from_oss_mirror(cls, oss_mirror: OssMirrorConfig) -> JobViewer:
        if not oss_mirror.oss_endpoint or not oss_mirror.oss_bucket:
            raise ValueError("OssMirrorConfig must have oss_endpoint and oss_bucket")
        if not oss_mirror.namespace or not oss_mirror.experiment_id:
            raise ValueError("OssMirrorConfig must have namespace and experiment_id")
        return cls.from_credentials(
            oss_endpoint=oss_mirror.oss_endpoint,
            oss_bucket=oss_mirror.oss_bucket,
            access_key_id=oss_mirror.oss_access_key_id or "",
            access_key_secret=oss_mirror.oss_access_key_secret or "",
            namespace=oss_mirror.namespace,
            experiment_id=oss_mirror.experiment_id,
            oss_region=oss_mirror.oss_region,
        )

    @classmethod
    def from_admin(
        cls,
        *,
        admin_base_url: str,
        namespace: str,
        experiment_id: str,
        auth_token: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> JobViewer:
        import httpx

        api_prefix = "/apis/envs/sandbox/v1"
        clean = admin_base_url.rstrip("/")
        if not clean.endswith(api_prefix):
            clean = f"{clean}{api_prefix}"
        url = f"{clean}/get_token?account=primary"

        headers = dict(extra_headers or {})
        if auth_token:
            headers["xrl-authorization"] = auth_token

        resp = httpx.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "Success":
            raise RuntimeError(f"admin /get_token failed: {data.get('message')}")
        sts = data["result"]

        auth = oss2.StsAuth(sts["AccessKeyId"], sts["AccessKeySecret"], sts["SecurityToken"])
        bucket = oss2.Bucket(auth, sts["Endpoint"], sts["Bucket"], region=sts.get("Region"))
        return cls(bucket, namespace, experiment_id)

    # ── Internal OSS operations ──────────────────────────────────────

    def _oss_key(self, path: str) -> str:
        return self._prefix + path

    def _read_text(self, path: str) -> str | None:
        try:
            result = self._bucket.get_object(self._oss_key(path))
            data = result.read()
            return data.decode("utf-8")
        except oss2.exceptions.NoSuchKey:
            return None

    def _read_bytes(self, path: str) -> bytes | None:
        try:
            result = self._bucket.get_object(self._oss_key(path))
            return result.read()
        except oss2.exceptions.NoSuchKey:
            return None

    def _read_json(self, path: str) -> dict[str, Any] | None:
        text = self._read_text(path)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _list_dirs(self, prefix: str = "") -> list[str]:
        oss_prefix = self._oss_key(prefix + "/") if prefix else self._prefix
        result: list[str] = []
        for obj in oss2.ObjectIterator(self._bucket, prefix=oss_prefix, delimiter="/"):
            if obj.is_prefix():
                dir_name = obj.key[len(oss_prefix) :].rstrip("/")
                if dir_name:
                    result.append(dir_name)
        return result

    def _read_texts_batch(self, paths: list[str]) -> dict[str, str | None]:
        if not paths:
            return {}
        results: dict[str, str | None] = {}
        max_workers = min(len(paths), 32)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_path = {executor.submit(self._read_text, p): p for p in paths}
            for future in as_completed(future_to_path):
                path = future_to_path[future]
                try:
                    results[path] = future.result()
                except Exception:
                    results[path] = None
        return results

    def _find_dirs_with_file(self, prefix: str, filename: str) -> list[str]:
        dirs = self._list_dirs(prefix)
        if not dirs:
            return []
        base = f"{prefix}/" if prefix else ""
        max_workers = min(len(dirs), 32)
        results: list[str] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_dir = {
                executor.submit(self._bucket.object_exists, self._oss_key(f"{base}{d}/{filename}")): d for d in dirs
            }
            for future in as_completed(future_to_dir):
                d = future_to_dir[future]
                try:
                    if future.result():
                        results.append(d)
                except Exception:
                    pass
        return sorted(results)

    def _exists(self, path: str) -> bool:
        if self._bucket.object_exists(self._oss_key(path)):
            return True
        for _ in oss2.ObjectIterator(self._bucket, prefix=self._oss_key(path) + "/", max_keys=1):
            return True
        return False

    # ── Job operations ───────────────────────────────────────────────

    def list_jobs(self) -> list[str]:
        return sorted(d for d in self._list_dirs() if d not in RESERVED_JOB_DIR_NAMES)

    def get_job_result(self, job_name: str) -> dict[str, Any] | None:
        return self._read_json(f"{job_name}/result.json")

    def get_job_config(self, job_name: str) -> dict[str, Any] | None:
        return self._read_json(f"{job_name}/config.json")

    def get_job_summary(self, job_name: str) -> str | None:
        return self._read_text(f"{job_name}/summary.md")

    # ── Trial operations ─────────────────────────────────────────────

    def list_trials(self, job_name: str) -> list[str]:
        return self._find_dirs_with_file(job_name, "result.json")

    def get_trial_result(self, job_name: str, trial_name: str) -> HarborTrialResult | None:
        data = self._read_json(f"{job_name}/{trial_name}/result.json")
        if data is None:
            return None
        try:
            return HarborTrialResult.from_harbor_json(data)
        except Exception:
            return None

    def get_trial_results(self, job_name: str) -> dict[str, HarborTrialResult]:
        all_dirs = self._list_dirs(job_name)
        if not all_dirs:
            return {}
        paths = [f"{job_name}/{d}/result.json" for d in all_dirs]
        contents = self._read_texts_batch(paths)
        results: dict[str, HarborTrialResult] = {}
        for d, p in zip(all_dirs, paths):
            text = contents.get(p)
            if text:
                try:
                    data = json.loads(text)
                    results[d] = HarborTrialResult.from_harbor_json(data)
                except Exception:
                    pass
        return results

    def get_trial_config(self, job_name: str, trial_name: str) -> dict[str, Any] | None:
        return self._read_json(f"{job_name}/{trial_name}/config.json")

    # ── Artifacts and logs ───────────────────────────────────────────

    def get_trajectory(self, job_name: str, trial_name: str) -> dict[str, Any] | None:
        return self._read_json(f"{job_name}/{trial_name}/agent/trajectory.json")

    def get_verifier_output(self, job_name: str, trial_name: str) -> VerifierOutput:
        base = f"{job_name}/{trial_name}/verifier"
        return VerifierOutput(
            stdout=self._read_text(f"{base}/test-stdout.txt"),
            stderr=self._read_text(f"{base}/test-stderr.txt"),
            ctrf=self._read_text(f"{base}/ctrf.json"),
        )

    def list_artifacts(self, job_name: str, trial_name: str) -> ArtifactsData:
        base = f"{job_name}/{trial_name}/artifacts"
        manifest = None
        manifest_text = self._read_text(f"{base}/manifest.json")
        if manifest_text:
            try:
                raw = json.loads(manifest_text)
                manifest = [ArtifactManifestEntry(**e) for e in raw] if isinstance(raw, list) else None
            except (json.JSONDecodeError, Exception):
                manifest = None

        oss_prefix = self._oss_key(base + "/")
        files: list[FileInfo] = []
        for obj in oss2.ObjectIterator(self._bucket, prefix=oss_prefix):
            if obj.is_prefix():
                continue
            relative = obj.key[len(oss_prefix) :]
            if not relative or relative == "manifest.json":
                continue
            name = relative.rsplit("/", 1)[-1]
            files.append(FileInfo(path=relative, name=name, is_dir=False, size=obj.size))
        return ArtifactsData(files=files, manifest=manifest)

    def get_agent_logs(self, job_name: str, trial_name: str) -> AgentLogs:
        base = f"{job_name}/{trial_name}"
        agent_base = f"{base}/agent"

        all_dirs = self._list_dirs(agent_base)
        cmd_dirs = sorted(
            [d for d in all_dirs if d.startswith("command-")],
            key=lambda d: int(d.split("-", 1)[1]) if d.split("-", 1)[1].isdigit() else 0,
        )

        commands: list[CommandLog] = []
        for d in cmd_dirs:
            content = self._read_text(f"{agent_base}/{d}/stdout.txt")
            idx = d.split("-", 1)[1]
            commands.append(CommandLog(index=int(idx) if idx.isdigit() else 0, content=content or ""))

        return AgentLogs(
            oracle=self._read_text(f"{agent_base}/oracle.txt"),
            setup=self._read_text(f"{agent_base}/setup/stdout.txt"),
            commands=commands,
            summary=self._read_text(f"{base}/summary.md"),
        )

    def get_job_meta(self, job_name: str):
        """Read rock_meta.json (unified metadata written by ROCK SDK)."""
        from rock.sdk.job.meta import JobMeta

        data = self._read_json(f"{job_name}/meta.json") or self._read_json(f"{job_name}/rock_meta.json")
        if data is None:
            return None
        try:
            return JobMeta.model_validate(data)
        except Exception:
            return None

    def write_job_meta(self, job_meta) -> None:
        key = self._oss_key(f"{job_meta.job_name}/meta.json")
        body = job_meta.model_dump_json(indent=2)
        self._bucket.put_object(key, body.encode("utf-8"))

    def get_exception(self, job_name: str, trial_name: str) -> str | None:
        return self._read_text(f"{job_name}/{trial_name}/exception.txt")

    def get_trial_log(self, job_name: str, trial_name: str) -> str | None:
        return self._read_text(f"{job_name}/{trial_name}/trial.log")

    # ── Generic file operations ──────────────────────────────────────

    def read_file(self, path: str) -> str | None:
        return self._read_text(path)

    def read_file_bytes(self, path: str) -> bytes | None:
        return self._read_bytes(path)

    def file_exists(self, path: str) -> bool:
        return self._bucket.object_exists(self._oss_key(path))

    # ── Run metadata (full-dataset run support) ─────────────────────

    def get_run_meta(self, run_id: str):
        """Read _meta/run_{run_id}.json and return RunMeta or None."""
        from rock.sdk.job.meta import RunMeta

        data = self._read_json(f"_meta/run_{run_id}.json")
        if data is None:
            return None
        try:
            return RunMeta.model_validate(data)
        except Exception:
            return None

    def write_run_meta(self, run_meta) -> None:
        """Write RunMeta to OSS as _meta/run_{run_id}.json."""
        key = self._oss_key(f"_meta/run_{run_meta.run_id}.json")
        body = run_meta.model_dump_json(indent=2)
        self._bucket.put_object(key, body)

    def list_runs(self) -> list:
        """List all runs under this experiment (read _meta/run_*.json files)."""
        from rock.sdk.job.meta import RunMeta

        meta_prefix = self._oss_key("_meta/")
        runs = []
        for obj in oss2.ObjectIterator(self._bucket, prefix=meta_prefix):
            if obj.is_prefix():
                continue
            key = obj.key
            if not key.endswith(".json") or "/run_" not in key:
                continue
            try:
                result = self._bucket.get_object(key)
                data = json.loads(result.read().decode("utf-8"))
                runs.append(RunMeta.model_validate(data))
            except Exception:
                pass
        return sorted(runs, key=lambda r: r.run_id)

    def resolve_run_id_for_resume(self) -> str | None:
        """Find the most recent incomplete run_id for resume.

        Returns None if all runs are completed.
        Raises ValueError if multiple incomplete runs exist.
        """
        runs = self.list_runs()
        incomplete = [r for r in runs if r.status != "completed"]
        if len(incomplete) == 0:
            return None
        if len(incomplete) == 1:
            return incomplete[0].run_id
        run_ids = [r.run_id for r in incomplete]
        raise ValueError(f"Found multiple incomplete runs. Specify --run-id explicitly: {run_ids}")

    def find_completed_tasks_in_run(self, run_id: str) -> set[str]:
        """Find completed tasks within a specific run (does not cross-match other runs).

        Reads the run's task_job_map, checks each job's trial results.
        Only tasks with at least one successful trial (no exception_info) are considered completed.
        """
        run_meta = self.get_run_meta(run_id)
        if run_meta is None:
            return set()

        completed = set()
        for task_id, job_name in run_meta.task_job_map.items():
            trial_results = self.get_trial_results(job_name)
            if any(r.status == "completed" for r in trial_results.values()):
                completed.add(task_id)
        return completed
