from rock.sdk.envhub.datasets.models import DatasetSpec, UploadResult


def test_dataset_spec_id():
    spec = DatasetSpec(id="qwen/my-bench", split="train", task_ids=["t1", "t2"])
    assert spec.id == "qwen/my-bench"
    assert len(spec.task_ids) == 2


def test_upload_result_fields():
    result = UploadResult(id="qwen/my-bench", split="train", uploaded=2, skipped=1, failed=0)
    assert result.uploaded == 2
    assert result.skipped == 1
    assert result.failed == 0
