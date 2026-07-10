"""Tests for rock.sdk.job.viewer — JobViewer and data models.

TDD: these tests are written first (RED), then the implementation (GREEN).
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import oss2
import pytest

from rock.sdk.job.viewer import (
    AgentLogs,
    ArtifactsData,
    ArtifactManifestEntry,
    CommandLog,
    FileInfo,
    JobViewer,
    VerifierOutput,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NAMESPACE = "test-ns"
EXPERIMENT_ID = "exp-001"
PREFIX = f"artifacts/{NAMESPACE}/{EXPERIMENT_ID}/"


def _make_oss_object(data: bytes):
    """Simulate oss2 get_object result (a readable stream)."""
    obj = BytesIO(data)
    obj.read = obj.read
    return obj


def _make_oss_iterator_item(key: str, *, is_prefix: bool = False, size: int = 0):
    """Simulate an item returned by oss2.ObjectIterator."""
    item = MagicMock()
    item.key = key
    item.is_prefix = MagicMock(return_value=is_prefix)
    item.size = size
    return item


@pytest.fixture
def mock_bucket():
    return MagicMock()


@pytest.fixture
def viewer(mock_bucket):
    return JobViewer(mock_bucket, namespace=NAMESPACE, experiment_id=EXPERIMENT_ID)


# ---------------------------------------------------------------------------
# Phase 1: Data model tests
# ---------------------------------------------------------------------------


class TestVerifierOutput:
    def test_defaults(self):
        v = VerifierOutput()
        assert v.stdout is None
        assert v.stderr is None
        assert v.ctrf is None

    def test_with_values(self):
        v = VerifierOutput(stdout="ok", stderr="warn", ctrf='{"results":[]}')
        assert v.stdout == "ok"
        assert v.stderr == "warn"
        assert v.ctrf == '{"results":[]}'


class TestAgentLogs:
    def test_defaults(self):
        logs = AgentLogs()
        assert logs.oracle is None
        assert logs.setup is None
        assert logs.commands == []
        assert logs.summary is None

    def test_with_commands(self):
        logs = AgentLogs(
            oracle="oracle text",
            setup="setup text",
            commands=[CommandLog(index=0, content="cmd0"), CommandLog(index=1, content="cmd1")],
            summary="summary text",
        )
        assert len(logs.commands) == 2
        assert logs.commands[0].index == 0
        assert logs.commands[1].content == "cmd1"


class TestFileInfo:
    def test_file(self):
        f = FileInfo(path="patch.diff", name="patch.diff", is_dir=False, size=1024)
        assert not f.is_dir
        assert f.size == 1024

    def test_directory(self):
        f = FileInfo(path="agent", name="agent", is_dir=True)
        assert f.is_dir
        assert f.size is None


class TestArtifactsData:
    def test_empty(self):
        a = ArtifactsData()
        assert a.files == []
        assert a.manifest is None

    def test_with_manifest(self):
        a = ArtifactsData(
            files=[FileInfo(path="f.txt", name="f.txt", is_dir=False, size=10)],
            manifest=[ArtifactManifestEntry(source="/src", destination="dst", type="file")],
        )
        assert len(a.files) == 1
        assert len(a.manifest) == 1
        assert a.manifest[0].source == "/src"


# ---------------------------------------------------------------------------
# Phase 2: Internal OSS operation tests
# ---------------------------------------------------------------------------


class TestJobViewerInternal:
    def test_oss_key_prefix(self, viewer):
        assert viewer._prefix == PREFIX
        assert viewer._oss_key("job1/result.json") == f"{PREFIX}job1/result.json"

    def test_read_text_found(self, viewer, mock_bucket):
        mock_bucket.get_object.return_value = _make_oss_object(b'{"status":"ok"}')
        result = viewer._read_text("job1/result.json")
        assert result == '{"status":"ok"}'
        mock_bucket.get_object.assert_called_once_with(f"{PREFIX}job1/result.json")

    def test_read_text_not_found(self, viewer, mock_bucket):
        mock_bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {"Code": "NoSuchKey"})
        result = viewer._read_text("nonexistent.json")
        assert result is None

    def test_read_bytes_found(self, viewer, mock_bucket):
        mock_bucket.get_object.return_value = _make_oss_object(b"\x89PNG")
        result = viewer._read_bytes("img.png")
        assert result == b"\x89PNG"

    def test_read_bytes_not_found(self, viewer, mock_bucket):
        mock_bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {"Code": "NoSuchKey"})
        result = viewer._read_bytes("nonexistent.png")
        assert result is None

    def test_list_dirs(self, viewer, mock_bucket):
        items = [
            _make_oss_iterator_item(f"{PREFIX}job-a/", is_prefix=True),
            _make_oss_iterator_item(f"{PREFIX}job-b/", is_prefix=True),
        ]
        with patch("oss2.ObjectIterator", return_value=iter(items)):
            dirs = viewer._list_dirs()
        assert sorted(dirs) == ["job-a", "job-b"]

    def test_list_dirs_with_prefix(self, viewer, mock_bucket):
        items = [
            _make_oss_iterator_item(f"{PREFIX}job1/trial-a/", is_prefix=True),
            _make_oss_iterator_item(f"{PREFIX}job1/trial-b/", is_prefix=True),
        ]
        with patch("oss2.ObjectIterator", return_value=iter(items)):
            dirs = viewer._list_dirs("job1")
        assert sorted(dirs) == ["trial-a", "trial-b"]

    def test_exists_file(self, viewer, mock_bucket):
        mock_bucket.object_exists.return_value = True
        assert viewer._exists("job1/result.json") is True

    def test_exists_directory(self, viewer, mock_bucket):
        mock_bucket.object_exists.return_value = False
        items = [_make_oss_iterator_item(f"{PREFIX}job1/trial-a/result.json")]
        with patch("oss2.ObjectIterator", return_value=iter(items)):
            assert viewer._exists("job1") is True

    def test_not_exists(self, viewer, mock_bucket):
        mock_bucket.object_exists.return_value = False
        with patch("oss2.ObjectIterator", return_value=iter([])):
            assert viewer._exists("nonexistent") is False


# ---------------------------------------------------------------------------
# Phase 3: Job operation tests
# ---------------------------------------------------------------------------

SAMPLE_JOB_RESULT = {
    "id": "abc123",
    "started_at": "2024-01-01T10:00:00Z",
    "finished_at": "2024-01-01T12:00:00Z",
    "n_total_trials": 10,
}

SAMPLE_JOB_CONFIG = {
    "job_name": "swe-bench_abc",
    "namespace": "test-ns",
    "experiment_id": "exp-001",
}


class TestJobViewerJobs:
    def test_list_jobs(self, viewer, mock_bucket):
        items = [
            _make_oss_iterator_item(f"{PREFIX}job-a/", is_prefix=True),
            _make_oss_iterator_item(f"{PREFIX}job-b/", is_prefix=True),
        ]
        with patch("oss2.ObjectIterator", return_value=iter(items)):
            jobs = viewer.list_jobs()
        assert "job-a" in jobs
        assert "job-b" in jobs

    def test_list_jobs_excludes_meta(self, viewer, mock_bucket):
        items = [
            _make_oss_iterator_item(f"{PREFIX}_meta/", is_prefix=True),
            _make_oss_iterator_item(f"{PREFIX}job-a/", is_prefix=True),
        ]
        with patch("oss2.ObjectIterator", return_value=iter(items)):
            jobs = viewer.list_jobs()
        assert "_meta" not in jobs
        assert "job-a" in jobs

    def test_get_job_result_found(self, viewer, mock_bucket):
        mock_bucket.get_object.return_value = _make_oss_object(json.dumps(SAMPLE_JOB_RESULT).encode())
        result = viewer.get_job_result("job-a")
        assert result is not None
        assert result["id"] == "abc123"
        assert result["n_total_trials"] == 10

    def test_get_job_result_not_found(self, viewer, mock_bucket):
        mock_bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {"Code": "NoSuchKey"})
        result = viewer.get_job_result("nonexistent")
        assert result is None

    def test_get_job_config(self, viewer, mock_bucket):
        mock_bucket.get_object.return_value = _make_oss_object(json.dumps(SAMPLE_JOB_CONFIG).encode())
        config = viewer.get_job_config("job-a")
        assert config["job_name"] == "swe-bench_abc"

    def test_get_job_summary(self, viewer, mock_bucket):
        mock_bucket.get_object.return_value = _make_oss_object(b"# Summary\nAll passed.")
        summary = viewer.get_job_summary("job-a")
        assert summary == "# Summary\nAll passed."

    def test_get_job_summary_not_found(self, viewer, mock_bucket):
        mock_bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {"Code": "NoSuchKey"})
        assert viewer.get_job_summary("job-a") is None


# ---------------------------------------------------------------------------
# Phase 4: Trial operation tests
# ---------------------------------------------------------------------------

SAMPLE_TRIAL_RESULT = {
    "task_name": "django__django-12345",
    "trial_name": "trial-001",
    "source": "swe-bench",
    "agent_info": {"name": "my-agent", "version": "1.0", "model_info": {"name": "claude-sonnet", "provider": "anthropic"}},
    "verifier_result": {"rewards": {"reward": 1.0}},
    "exception_info": None,
    "started_at": "2024-01-01T10:00:00Z",
    "finished_at": "2024-01-01T10:02:00Z",
}


class TestJobViewerTrials:
    def test_list_trials(self, viewer, mock_bucket):
        dir_items = [
            _make_oss_iterator_item(f"{PREFIX}job-a/trial-001/", is_prefix=True),
            _make_oss_iterator_item(f"{PREFIX}job-a/trial-002/", is_prefix=True),
            _make_oss_iterator_item(f"{PREFIX}job-a/no-result/", is_prefix=True),
        ]
        mock_bucket.object_exists.side_effect = lambda key: "no-result" not in key
        with patch("oss2.ObjectIterator", return_value=iter(dir_items)):
            trials = viewer.list_trials("job-a")
        assert "trial-001" in trials
        assert "trial-002" in trials
        assert "no-result" not in trials

    def test_get_trial_result_parses_harbor_format(self, viewer, mock_bucket):
        mock_bucket.get_object.return_value = _make_oss_object(json.dumps(SAMPLE_TRIAL_RESULT).encode())
        result = viewer.get_trial_result("job-a", "trial-001")
        assert result is not None
        assert result.task_name == "django__django-12345"
        assert result.score == 1.0
        assert result.agent_info.name == "my-agent"
        assert result.agent_info.model_info.name == "claude-sonnet"

    def test_get_trial_result_not_found(self, viewer, mock_bucket):
        mock_bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {"Code": "NoSuchKey"})
        result = viewer.get_trial_result("job-a", "nonexistent")
        assert result is None

    def test_get_trial_results_batch(self, viewer, mock_bucket):
        dir_items = [
            _make_oss_iterator_item(f"{PREFIX}job-a/trial-001/", is_prefix=True),
            _make_oss_iterator_item(f"{PREFIX}job-a/trial-002/", is_prefix=True),
        ]
        mock_bucket.get_object.return_value = _make_oss_object(json.dumps(SAMPLE_TRIAL_RESULT).encode())
        with patch("oss2.ObjectIterator", return_value=iter(dir_items)):
            results = viewer.get_trial_results("job-a")
        assert len(results) >= 1

    def test_get_trial_config(self, viewer, mock_bucket):
        trial_config = {"agent": {"name": "my-agent"}, "environment": {"type": "docker"}}
        mock_bucket.get_object.return_value = _make_oss_object(json.dumps(trial_config).encode())
        config = viewer.get_trial_config("job-a", "trial-001")
        assert config["agent"]["name"] == "my-agent"


# ---------------------------------------------------------------------------
# Phase 5: Artifact and log tests
# ---------------------------------------------------------------------------


class TestJobViewerArtifacts:
    def test_get_trajectory(self, viewer, mock_bucket):
        traj = {"schema_version": "1.0", "steps": [{"step_id": 0}]}
        mock_bucket.get_object.return_value = _make_oss_object(json.dumps(traj).encode())
        result = viewer.get_trajectory("job-a", "trial-001")
        assert result is not None
        assert result["schema_version"] == "1.0"
        assert len(result["steps"]) == 1

    def test_get_trajectory_not_found(self, viewer, mock_bucket):
        mock_bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {"Code": "NoSuchKey"})
        result = viewer.get_trajectory("job-a", "trial-001")
        assert result is None

    def test_get_verifier_output(self, viewer, mock_bucket):
        def fake_get_object(key):
            if "test-stdout.txt" in key:
                return _make_oss_object(b"PASS")
            if "test-stderr.txt" in key:
                return _make_oss_object(b"warn")
            if "ctrf.json" in key:
                return _make_oss_object(b'{"results":[]}')
            raise oss2.exceptions.NoSuchKey(404, {}, b"", {"Code": "NoSuchKey"})

        mock_bucket.get_object.side_effect = fake_get_object
        output = viewer.get_verifier_output("job-a", "trial-001")
        assert isinstance(output, VerifierOutput)
        assert output.stdout == "PASS"
        assert output.stderr == "warn"
        assert output.ctrf == '{"results":[]}'

    def test_get_verifier_output_empty(self, viewer, mock_bucket):
        mock_bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {"Code": "NoSuchKey"})
        output = viewer.get_verifier_output("job-a", "trial-001")
        assert output.stdout is None
        assert output.stderr is None
        assert output.ctrf is None

    def test_get_agent_logs(self, viewer, mock_bucket):
        def fake_get_object(key):
            if "oracle.txt" in key:
                return _make_oss_object(b"oracle content")
            if "setup/stdout.txt" in key:
                return _make_oss_object(b"setup content")
            if "summary.md" in key:
                return _make_oss_object(b"summary content")
            raise oss2.exceptions.NoSuchKey(404, {}, b"", {"Code": "NoSuchKey"})

        mock_bucket.get_object.side_effect = fake_get_object
        with patch("oss2.ObjectIterator", return_value=iter([])):
            logs = viewer.get_agent_logs("job-a", "trial-001")
        assert isinstance(logs, AgentLogs)
        assert logs.oracle == "oracle content"
        assert logs.setup == "setup content"
        assert logs.summary == "summary content"

    def test_get_exception(self, viewer, mock_bucket):
        mock_bucket.get_object.return_value = _make_oss_object(b"TimeoutError: agent timed out")
        result = viewer.get_exception("job-a", "trial-001")
        assert result == "TimeoutError: agent timed out"

    def test_get_exception_not_found(self, viewer, mock_bucket):
        mock_bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {"Code": "NoSuchKey"})
        assert viewer.get_exception("job-a", "trial-001") is None

    def test_get_trial_log(self, viewer, mock_bucket):
        mock_bucket.get_object.return_value = _make_oss_object(b"[INFO] Trial started")
        result = viewer.get_trial_log("job-a", "trial-001")
        assert "Trial started" in result

    def test_get_trial_log_not_found(self, viewer, mock_bucket):
        mock_bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {"Code": "NoSuchKey"})
        assert viewer.get_trial_log("job-a", "trial-001") is None


# ---------------------------------------------------------------------------
# Phase 6: Factory method tests
# ---------------------------------------------------------------------------


class TestJobViewerFactories:
    @patch("oss2.Bucket")
    @patch("oss2.Auth")
    def test_from_credentials(self, mock_auth_cls, mock_bucket_cls):
        viewer = JobViewer.from_credentials(
            oss_endpoint="https://oss.example.com",
            oss_bucket="my-bucket",
            access_key_id="AK",
            access_key_secret="SK",
            namespace="ns",
            experiment_id="exp",
        )
        mock_auth_cls.assert_called_once_with("AK", "SK")
        mock_bucket_cls.assert_called_once()
        assert viewer._prefix == "artifacts/ns/exp/"

    @patch("oss2.Bucket")
    @patch("oss2.Auth")
    def test_from_oss_mirror(self, mock_auth_cls, mock_bucket_cls):
        from rock.sdk.envhub.config import OssMirrorConfig

        mirror = OssMirrorConfig(
            enabled=True,
            oss_bucket="bucket",
            namespace="ns",
            experiment_id="exp",
            oss_access_key_id="AK",
            oss_access_key_secret="SK",
            oss_region="cn-hangzhou",
            oss_endpoint="https://oss.example.com",
        )
        viewer = JobViewer.from_oss_mirror(mirror)
        assert viewer._prefix == "artifacts/ns/exp/"

    def test_from_oss_mirror_missing_fields(self):
        from rock.sdk.envhub.config import OssMirrorConfig

        mirror = OssMirrorConfig(enabled=True)
        with pytest.raises(Exception):
            JobViewer.from_oss_mirror(mirror)

    @patch("httpx.get")
    @patch("oss2.Bucket")
    @patch("oss2.StsAuth")
    def test_from_admin(self, mock_sts_auth_cls, mock_bucket_cls, mock_httpx_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "Success",
            "result": {
                "AccessKeyId": "tmp-ak",
                "AccessKeySecret": "tmp-sk",
                "SecurityToken": "token",
                "Endpoint": "https://oss.example.com",
                "Bucket": "bucket",
                "Region": "cn-hangzhou",
            },
        }
        mock_resp.raise_for_status = MagicMock()
        mock_httpx_get.return_value = mock_resp

        viewer = JobViewer.from_admin(
            admin_base_url="https://admin.example.com",
            namespace="ns",
            experiment_id="exp",
        )
        mock_sts_auth_cls.assert_called_once_with("tmp-ak", "tmp-sk", "token")
        assert viewer._prefix == "artifacts/ns/exp/"

    @patch("httpx.get")
    def test_from_admin_auth_failure(self, mock_httpx_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "Failed", "message": "unauthorized"}
        mock_resp.raise_for_status = MagicMock()
        mock_httpx_get.return_value = mock_resp

        with pytest.raises(RuntimeError, match="unauthorized"):
            JobViewer.from_admin(
                admin_base_url="https://admin.example.com",
                namespace="ns",
                experiment_id="exp",
            )

    @patch("httpx.get")
    @patch("oss2.Bucket")
    @patch("oss2.StsAuth")
    def test_from_admin_url_normalization(self, mock_sts_auth_cls, mock_bucket_cls, mock_httpx_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "Success",
            "result": {
                "AccessKeyId": "ak",
                "AccessKeySecret": "sk",
                "SecurityToken": "t",
                "Endpoint": "https://oss.example.com",
                "Bucket": "b",
            },
        }
        mock_resp.raise_for_status = MagicMock()
        mock_httpx_get.return_value = mock_resp

        JobViewer.from_admin(
            admin_base_url="https://admin.example.com/apis/envs/sandbox/v1",
            namespace="ns",
            experiment_id="exp",
        )
        call_url = mock_httpx_get.call_args[0][0]
        assert call_url == "https://admin.example.com/apis/envs/sandbox/v1/get_token?account=primary"
        assert "/apis/envs/sandbox/v1/apis/" not in call_url


# ---------------------------------------------------------------------------
# Phase bonus: Generic file operations
# ---------------------------------------------------------------------------


class TestJobViewerFileOps:
    def test_read_file(self, viewer, mock_bucket):
        mock_bucket.get_object.return_value = _make_oss_object(b"hello")
        assert viewer.read_file("some/path.txt") == "hello"

    def test_read_file_bytes(self, viewer, mock_bucket):
        mock_bucket.get_object.return_value = _make_oss_object(b"\x00\x01\x02")
        assert viewer.read_file_bytes("bin.dat") == b"\x00\x01\x02"

    def test_file_exists_true(self, viewer, mock_bucket):
        mock_bucket.object_exists.return_value = True
        assert viewer.file_exists("some/file") is True

    def test_file_exists_false(self, viewer, mock_bucket):
        mock_bucket.object_exists.return_value = False
        assert viewer.file_exists("nonexistent") is False


# ---------------------------------------------------------------------------
# Run metadata tests (full-dataset run support)
# ---------------------------------------------------------------------------


class TestJobViewerRunMeta:
    @pytest.fixture
    def viewer(self):
        bucket = MagicMock()
        return JobViewer(bucket, namespace=NAMESPACE, experiment_id=EXPERIMENT_ID), bucket

    def test_get_run_meta_found(self, viewer):
        from rock.sdk.job.meta import RunMeta

        v, bucket = viewer
        meta = RunMeta(
            run_id="20260706T143052-a1b2c3d4",
            dataset="alibaba/aone-bench", split="test",
            total_tasks=100, pending_tasks=100,
            started_at="2026-07-06T14:30:52Z", status="running",
            task_job_map={"task-001": "j1"},
        )
        bucket.get_object.return_value = _make_oss_object(meta.model_dump_json().encode())
        result = v.get_run_meta("20260706T143052-a1b2c3d4")
        assert result is not None
        assert result.run_id == "20260706T143052-a1b2c3d4"
        assert result.status == "running"
        bucket.get_object.assert_called_once_with(f"artifacts/{NAMESPACE}/{EXPERIMENT_ID}/_meta/run_20260706T143052-a1b2c3d4.json")

    def test_get_run_meta_not_found(self, viewer):
        v, bucket = viewer
        bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {"Code": "NoSuchKey"})
        result = v.get_run_meta("nonexistent")
        assert result is None

    def test_write_run_meta(self, viewer):
        from rock.sdk.job.meta import RunMeta

        v, bucket = viewer
        meta = RunMeta(
            run_id="20260706T143052-a1b2c3d4",
            dataset="alibaba/aone-bench", split="test",
            total_tasks=50, pending_tasks=50,
            started_at="2026-07-06T14:30:52Z", status="running",
            task_job_map={"t1": "j1"},
        )
        v.write_run_meta(meta)
        bucket.put_object.assert_called_once()
        key = bucket.put_object.call_args[0][0]
        assert key == f"artifacts/{NAMESPACE}/{EXPERIMENT_ID}/_meta/run_20260706T143052-a1b2c3d4.json"
        body = bucket.put_object.call_args[0][1]
        data = json.loads(body)
        assert data["run_id"] == "20260706T143052-a1b2c3d4"

    def test_list_runs(self, viewer):
        from rock.sdk.job.meta import RunMeta

        v, bucket = viewer
        meta1 = RunMeta(
            run_id="20260706T100000-aaaaaaaa", dataset="d", split="test",
            total_tasks=10, pending_tasks=10, started_at="2026-07-06T10:00:00Z",
            status="completed", task_job_map={},
        )
        meta2 = RunMeta(
            run_id="20260706T120000-bbbbbbbb", dataset="d", split="test",
            total_tasks=10, pending_tasks=5, started_at="2026-07-06T12:00:00Z",
            status="running", task_job_map={},
        )
        prefix = f"artifacts/{NAMESPACE}/{EXPERIMENT_ID}/_meta/"

        mock_obj1 = MagicMock()
        mock_obj1.key = f"{prefix}run_20260706T100000-aaaaaaaa.json"
        mock_obj1.is_prefix.return_value = False
        mock_obj2 = MagicMock()
        mock_obj2.key = f"{prefix}run_20260706T120000-bbbbbbbb.json"
        mock_obj2.is_prefix.return_value = False

        with patch("oss2.ObjectIterator", return_value=iter([mock_obj1, mock_obj2])):
            def get_object_side_effect(key):
                if "aaaaaaaa" in key:
                    return _make_oss_object(meta1.model_dump_json().encode())
                return _make_oss_object(meta2.model_dump_json().encode())

            bucket.get_object.side_effect = get_object_side_effect
            runs = v.list_runs()

        assert len(runs) == 2
        assert runs[0].run_id == "20260706T100000-aaaaaaaa"

    def test_resolve_run_id_for_resume_single_incomplete(self, viewer):
        from rock.sdk.job.meta import RunMeta

        v, bucket = viewer
        meta1 = RunMeta(
            run_id="20260706T100000-aaaaaaaa", dataset="d", split="test",
            total_tasks=10, pending_tasks=10, started_at="t", status="completed", task_job_map={},
        )
        meta2 = RunMeta(
            run_id="20260706T120000-bbbbbbbb", dataset="d", split="test",
            total_tasks=10, pending_tasks=5, started_at="t", status="running", task_job_map={},
        )
        with patch.object(v, "list_runs", return_value=[meta1, meta2]):
            result = v.resolve_run_id_for_resume()
        assert result == "20260706T120000-bbbbbbbb"

    def test_resolve_run_id_for_resume_none_incomplete(self, viewer):
        from rock.sdk.job.meta import RunMeta

        v, bucket = viewer
        meta1 = RunMeta(
            run_id="r1", dataset="d", split="test",
            total_tasks=10, pending_tasks=10, started_at="t", status="completed", task_job_map={},
        )
        with patch.object(v, "list_runs", return_value=[meta1]):
            result = v.resolve_run_id_for_resume()
        assert result is None

    def test_resolve_run_id_for_resume_multiple_incomplete_raises(self, viewer):
        from rock.sdk.job.meta import RunMeta

        v, bucket = viewer
        meta1 = RunMeta(
            run_id="r1", dataset="d", split="test",
            total_tasks=10, pending_tasks=10, started_at="t", status="running", task_job_map={},
        )
        meta2 = RunMeta(
            run_id="r2", dataset="d", split="test",
            total_tasks=10, pending_tasks=5, started_at="t", status="running", task_job_map={},
        )
        with patch.object(v, "list_runs", return_value=[meta1, meta2]):
            with pytest.raises(ValueError, match="multiple"):
                v.resolve_run_id_for_resume()

    def test_find_completed_tasks_in_run(self, viewer):
        from rock.sdk.job.meta import RunMeta

        v, bucket = viewer
        meta = RunMeta(
            run_id="r1", dataset="d", split="test",
            total_tasks=3, pending_tasks=3, started_at="t", status="running",
            task_job_map={"task-001": "j1", "task-002": "j2", "task-003": "j3"},
        )
        with patch.object(v, "get_run_meta", return_value=meta):
            # j1 has completed trial, j2 has failed trial, j3 has no trial
            def mock_get_trial_results(job_name):
                from rock.sdk.bench.models.trial.result import HarborTrialResult

                if job_name == "j1":
                    return {"trial1": HarborTrialResult(task_name="task-001")}
                elif job_name == "j2":
                    from rock.sdk.job.result import ExceptionInfo
                    return {"trial1": HarborTrialResult(
                        task_name="task-002",
                        exception_info=ExceptionInfo(exception_type="Timeout", exception_message="timeout"),
                    )}
                return {}

            with patch.object(v, "get_trial_results", side_effect=mock_get_trial_results):
                completed = v.find_completed_tasks_in_run("r1")

        assert completed == {"task-001"}
        assert "task-002" not in completed
        assert "task-003" not in completed
