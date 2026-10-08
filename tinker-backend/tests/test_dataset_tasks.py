from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tinker_backend.adapters.rock_adapter import RockAdapter


def _make_adapter(config_path: str) -> RockAdapter:
    return RockAdapter(backend_url="http://localhost:9000", runtime_id="rt_test", config_path=config_path)


def _write_config(tmp_path: Path, payload: dict) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(payload), encoding="utf-8")
    return str(path)


class ListTasksFromCatalogTest(unittest.TestCase):
    def test_adapter_uses_task_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            adapter = _make_adapter(_write_config(Path(td), {"rock": {"bench_name": "SWE-bench"}}))
            expected = ["django__django-14315", "sympy__sympy-19637", "astropy__astropy-1234"]

            with patch("tinker_backend.adapters.rock_adapter.list_task_ids", return_value=expected) as list_ids:
                result = adapter._list_tasks_from_catalog("princeton-nlp/SWE-bench_Verified", "test")

        self.assertEqual(result, expected)
        list_ids.assert_called_once_with(
            "princeton-nlp/SWE-bench_Verified",
            "test",
        )


class ListDatasetTasksTest(unittest.TestCase):
    def _adapter_with_datasets(self, datasets: list[dict]) -> RockAdapter:
        self._tmp = tempfile.TemporaryDirectory()
        return _make_adapter(_write_config(Path(self._tmp.name), {"rock": {"bench_name": "SWE-bench", "datasets": datasets}}))

    def tearDown(self) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def test_with_filter(self) -> None:
        adapter = self._adapter_with_datasets([
            {"dataset": "org/ds", "split": "test", "bench_name": "SWE-bench", "task_filter": "^django__"}
        ])
        all_tasks = ["django__django-14315", "django__django-99999", "sympy__sympy-19637"]
        with patch.object(adapter, "_list_tasks_from_catalog", return_value=all_tasks):
            result = adapter.list_dataset_tasks()

        self.assertEqual([item["task_id"] for item in result], ["django__django-14315", "django__django-99999"])
        self.assertTrue(all(item["bench_name"] == "SWE-bench" for item in result))

    def test_multiple_datasets(self) -> None:
        adapter = self._adapter_with_datasets([
            {"dataset": "org/ds1", "split": "test", "bench_name": "SWE-bench"},
            {"dataset": "org/ds2", "split": "dev", "bench_name": "pinchbench", "task_filter": "^pin_"},
        ])

        def mock_list(dataset: str, split: str) -> list[str]:
            if dataset == "org/ds1":
                return ["task_a", "task_b"]
            return ["pin_1", "pin_2", "other_3"]

        with patch.object(adapter, "_list_tasks_from_catalog", side_effect=mock_list):
            result = adapter.list_dataset_tasks()

        self.assertEqual(len(result), 4)
        self.assertEqual([item["bench_name"] for item in result].count("SWE-bench"), 2)
        self.assertEqual([item["bench_name"] for item in result].count("pinchbench"), 2)


if __name__ == "__main__":
    unittest.main()
