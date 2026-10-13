import random
import datasets
import pytest
from tinker.task_catalog import PublicTaskCatalog, list_public_task_ids

def test_public_ids_are_streamed_deduplicated_and_sorted(monkeypatch):
    calls = []
    def load(name, **kwargs):
        calls.append((name, kwargs))
        return iter([{"instance_id": "b"}, {"instance_id": "a"}, {"instance_id": "b"}])
    monkeypatch.setattr(datasets, "load_dataset", load)
    assert list_public_task_ids("public/tasks", "test") == ["a", "b"]
    assert calls == [("public/tasks", {"split": "test", "streaming": True})]

@pytest.mark.parametrize("row", [{}, {"instance_id": ""}, {"instance_id": 42}])
def test_missing_instance_id_is_an_error(monkeypatch, row):
    monkeypatch.setattr(datasets, "load_dataset", lambda *a, **kw: [row])
    with pytest.raises(ValueError, match="instance_id"):
        list_public_task_ids("public/tasks", "test")

@pytest.mark.asyncio
async def test_selection_filters_then_shuffles_without_global_random_mutation(monkeypatch):
    monkeypatch.setattr(datasets, "load_dataset", lambda *a, **kw: [
        {"instance_id": x} for x in ["sympy-c", "other-a", "sympy-a", "sympy-b"]])
    before = random.getstate()
    catalog = PublicTaskCatalog()
    kwargs = dict(dataset="public/tasks", split="test", bench_name="SWE-bench",
                  task_filter="^sympy-", seed=7, limit=2)
    first = await catalog.select_tasks(**kwargs)
    assert first == await catalog.select_tasks(**kwargs)
    assert len(first) == 2 and all(x.task_id.startswith("sympy-") for x in first)
    assert random.getstate() == before

@pytest.mark.asyncio
async def test_invalid_selection_fails_before_network(monkeypatch):
    def no_network(*a, **kw):
        raise AssertionError("Unexpected network access")
    monkeypatch.setattr(datasets, "load_dataset", no_network)
    with pytest.raises(ValueError, match="limit"):
        await PublicTaskCatalog().select_tasks(dataset="public/tasks", split="test", bench_name="SWE-bench", limit=0)
