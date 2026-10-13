from unittest.mock import patch

import pytest
from tinker_backend.protocol.schemas import DatasetSpec
from tinker_backend.services import task_catalog as catalog


def test_catalog_uses_public_dataset_ids():
    with patch.object(catalog, "list_public_task_ids", return_value=["task-a", "task-b"]) as public:
        assert catalog.list_task_ids("organization/dataset", "test") == ["task-a", "task-b"]
    public.assert_called_once_with("organization/dataset", "test")


def test_catalog_preserves_public_order_and_applies_filter():
    spec = DatasetSpec(dataset="organization/dataset", split="test", bench_name="SWE-bench", task_filter="^task-b")
    with patch.object(catalog, "list_public_task_ids", return_value=["task-a", "task-b", "task-b2"]):
        descriptors = catalog.list_task_descriptors([spec])
    assert [task.task_id for task in descriptors] == ["task-b", "task-b2"]
    assert all(task.dataset == spec.dataset and task.split == "test" for task in descriptors)


def test_catalog_propagates_invalid_public_dataset():
    with patch.object(catalog, "list_public_task_ids", side_effect=ValueError("missing instance_id")):
        with pytest.raises(ValueError, match="instance_id"):
            catalog.list_task_ids("organization/dataset", "test")
