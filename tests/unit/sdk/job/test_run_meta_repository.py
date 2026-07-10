from __future__ import annotations

from unittest.mock import MagicMock, patch

from rock.sdk.job.meta import RunMeta


def make_run_meta(run_id: str = "run-1") -> RunMeta:
    return RunMeta(
        run_id=run_id,
        dataset="org/dataset",
        split="test",
        total_tasks=2,
        pending_tasks=2,
        started_at="2026-07-09T00:00:00Z",
        status="running",
        task_job_map={"t1": "j1", "t2": "j2"},
    )


class TestRunMetaRepository:
    def test_delegates_write_get_list_and_completed_tasks_to_viewer(self):
        from rock.sdk.job.run_meta import RunMetaRepository

        viewer = MagicMock()
        meta = make_run_meta()
        viewer.get_run_meta.return_value = meta
        viewer.list_runs.return_value = [meta]
        viewer.find_completed_tasks_in_run.return_value = {"t1"}

        repo = RunMetaRepository(viewer)

        repo.write(meta)
        assert repo.get("run-1") == meta
        assert repo.list() == [meta]
        assert repo.find_completed_tasks("run-1") == {"t1"}

        viewer.write_run_meta.assert_called_once_with(meta)
        viewer.get_run_meta.assert_called_once_with("run-1")
        viewer.list_runs.assert_called_once_with()
        viewer.find_completed_tasks_in_run.assert_called_once_with("run-1")

    def test_resolve_run_id_for_resume_delegates_to_viewer(self):
        from rock.sdk.job.run_meta import RunMetaRepository

        viewer = MagicMock()
        viewer.resolve_run_id_for_resume.return_value = "run-1"

        assert RunMetaRepository(viewer).resolve_run_id_for_resume() == "run-1"

    def test_from_job_config_uses_oss_mirror(self):
        from rock.sdk.bench.models.job.config import HarborJobConfig
        from rock.sdk.envhub.config import OssMirrorConfig
        from rock.sdk.job.run_meta import RunMetaRepository

        config = HarborJobConfig(
            experiment_id="exp-1",
            namespace="ns-1",
            environment={
                "oss_mirror": OssMirrorConfig(
                    enabled=True,
                    oss_bucket="bucket",
                    oss_endpoint="endpoint",
                    oss_access_key_id="ak",
                    oss_access_key_secret="sk",
                )
            },
        )
        viewer = MagicMock()

        with patch("rock.sdk.job.run_meta.JobViewer.from_oss_mirror", return_value=viewer) as factory:
            repo = RunMetaRepository.from_job_config(config)

        assert repo._viewer is viewer
        factory.assert_called_once_with(config.environment.oss_mirror)

    def test_from_job_config_backfills_oss_mirror_identity_from_config(self):
        from rock.sdk.bench.models.job.config import HarborJobConfig
        from rock.sdk.envhub.config import OssMirrorConfig
        from rock.sdk.job.run_meta import RunMetaRepository

        config = HarborJobConfig(
            experiment_id="exp-1",
            namespace="ns-from-sandbox",
            environment={
                "oss_mirror": OssMirrorConfig(
                    enabled=True,
                    oss_bucket="bucket",
                    oss_endpoint="endpoint",
                )
            },
        )
        config.environment.oss_mirror.namespace = None
        config.environment.oss_mirror.experiment_id = None

        with patch("rock.sdk.job.run_meta.JobViewer.from_oss_mirror") as factory:
            RunMetaRepository.from_job_config(config)

        assert config.environment.oss_mirror.namespace == "ns-from-sandbox"
        assert config.environment.oss_mirror.experiment_id == "exp-1"
        factory.assert_called_once_with(config.environment.oss_mirror)
