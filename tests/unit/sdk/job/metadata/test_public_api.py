def test_job_package_exports_database_metadata_repository():
    from rock.sdk.job import JobMetadataRepository
    from rock.sdk.job.metadata import JobMetadataRepository as Expected

    assert JobMetadataRepository is Expected


def test_legacy_repository_modules_reexport_database_repository():
    from rock.sdk.job.job_meta import JobMetaRepository
    from rock.sdk.job.metadata import JobMetadataRepository
    from rock.sdk.job.run_meta import RunMetaRepository

    assert JobMetaRepository is JobMetadataRepository
    assert RunMetaRepository is JobMetadataRepository


def test_metadata_package_exports_models_schema_errors_and_repository():
    from rock.sdk.job.metadata import (
        JobGroupMeta,
        JobMeta,
        JobMetadataRepository,
        MetadataBase,
        MetadataNotFoundError,
    )

    assert JobMeta is not None
    assert JobGroupMeta is not None
    assert JobMetadataRepository is not None
    assert MetadataBase is not None
    assert issubclass(MetadataNotFoundError, RuntimeError)


def test_job_viewer_has_no_metadata_api():
    from rock.sdk.job.viewer import JobViewer

    for method in (
        "get_job_meta",
        "write_job_meta",
        "get_run_meta",
        "write_run_meta",
        "list_runs",
        "resolve_run_id_for_resume",
        "find_completed_tasks_in_run",
    ):
        assert not hasattr(JobViewer, method)
