from __future__ import annotations

import json

import pytest

from tinker_cookbook.stores import LocalStorage, TrainingRunStore, storage_from_uri


def test_local_artifacts_append_and_step(tmp_path):
    store = TrainingRunStore(storage_from_uri(tmp_path))
    store.write_config({"name": "训练"})
    store.write_code_diff("diff text\n")
    metrics = {"loss": 0.5, "step": -1}
    store.write_metrics(metrics, step=3)
    store.write_metrics({"loss": 0.2})
    assert json.loads((tmp_path / "config.json").read_text()) == {"name": "训练"}
    assert (tmp_path / "code.diff").read_text() == "diff text\n"
    assert [json.loads(line) for line in (tmp_path / "metrics.jsonl").read_text().splitlines()] == [
        {"loss": 0.5, "step": 3},
        {"loss": 0.2},
    ]
    assert metrics["step"] == -1
    store.write_config({"updated": True})
    assert json.loads((tmp_path / "config.json").read_text()) == {"updated": True}


def test_timing_spans(tmp_path):
    store = TrainingRunStore(LocalStorage(tmp_path))
    store.write_timing_spans(0, [])
    assert not (tmp_path / "timing_spans.jsonl").exists()
    store.write_timing_spans(1, [{"name": "sample", "duration": 2}])
    store.write_timing_spans(2, [{"name": "train", "duration": 3}])
    records = [
        json.loads(line) for line in (tmp_path / "timing_spans.jsonl").read_text().splitlines()
    ]
    assert [record["step"] for record in records] == [1, 2]
    assert records[0]["spans"][0]["name"] == "sample"


def test_file_uri_and_nested_paths(tmp_path):
    directory = tmp_path / "with space"
    storage = storage_from_uri(directory.as_uri())
    storage.write_text("nested/file.txt", "first")
    storage.append_text("nested/file.txt", "-second")
    assert (directory / "nested/file.txt").read_text() == "first-second"


@pytest.mark.parametrize(
    "uri",
    [
        "oss://bucket/path",
        "s3://bucket/path",
        "https://example.com/path",
        "file://remote/path",
        "file:///tmp/path?query=1",
        "file:///tmp/path#fragment",
    ],
)
def test_reject_remote_uri(uri):
    with pytest.raises(ValueError):
        storage_from_uri(uri)


@pytest.mark.parametrize("path", ["../escape", "/tmp/escape", "nested/../../escape", "."])
def test_reject_path_escape(tmp_path, path):
    storage = LocalStorage(tmp_path)
    with pytest.raises(ValueError):
        storage.write_text(path, "data")
    with pytest.raises(ValueError):
        storage.append_text(path, "data")


def test_reject_symlink_escape(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    storage = LocalStorage(root)
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        storage.write_text("linked/escape", "data")
    assert not (outside / "escape").exists()


def test_existing_logger_and_trace_use_store(tmp_path, monkeypatch):
    from tinker_cookbook.utils import ml_log, trace

    monkeypatch.setattr(ml_log, "code_state", lambda: "diff")
    logger = ml_log.JsonLogger(tmp_path)
    logger.log_hparams({"batch_size": 2})
    logger.log_hparams({"batch_size": 4})
    logger.log_metrics({"loss": 1}, step=7)
    window = trace.IterationWindow()
    monkeypatch.setattr(window, "get_span_dicts", lambda: [{"name": "sample", "duration": 1}])
    window.save_timing(7, store=logger.store)
    assert json.loads((tmp_path / "config.json").read_text()) == {"batch_size": 2}
    assert json.loads((tmp_path / "metrics.jsonl").read_text())["step"] == 7
    assert json.loads((tmp_path / "timing_spans.jsonl").read_text())["step"] == 7
