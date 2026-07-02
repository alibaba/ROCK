"""Tests for rock.sdk.job.adapter — TrackingAdapter base class + directory-scan discovery.

Adapters are discovered by scanning the directories listed in
``ROCK_TRACKING_LOAD_PATHS`` for ``TrackingAdapter`` subclasses. Internal /
proprietary adapters are layered in via a symlink into one of those directories
(the same pattern used by the CLI CommandLoader), so no entry_points
registration or ``pip install`` step is required.
"""

from __future__ import annotations

import textwrap

from rock.sdk.job.adapter import TrackingAdapter, resolve_tracking_adapters
from rock.sdk.job.config import JobConfig


class _ConcreteAdapter(TrackingAdapter):
    """Concrete adapter for lifecycle testing (implements all abstract methods)."""

    def __init__(self):
        self.init_called = False
        self.received_config = None
        self.report_calls = []
        self.close_called = False

    def init(self, *, namespace, experiment_id, job_id, config):
        self.init_called = True
        self.received_config = config

    def report(self, metrics):
        self.report_calls.append(metrics)

    def close(self):
        self.close_called = True


def _write_adapter(directory, filename: str, class_name: str) -> None:
    """Write a .py file defining a TrackingAdapter subclass into *directory*."""
    src = textwrap.dedent(
        f"""
        from rock.sdk.job.adapter import TrackingAdapter

        class {class_name}(TrackingAdapter):
            def init(self, *, namespace, experiment_id, job_id, config):
                pass

            def report(self, metrics):
                pass
        """
    )
    (directory / filename).write_text(src)


class TestAdapterLifecycle:
    def test_adapter_lifecycle(self):
        adapter = _ConcreteAdapter()
        config = JobConfig(namespace="ns", experiment_id="exp", job_name="j1")
        adapter.init(namespace="ns", experiment_id="exp", job_id="j1", config=config)
        assert adapter.init_called
        assert adapter.received_config is config

        adapter.report({"score": 0.9, "status": "completed"})
        assert len(adapter.report_calls) == 1
        assert adapter.report_calls[0]["score"] == 0.9

        adapter.close()
        assert adapter.close_called


class TestResolveTrackingAdapters:
    def test_returns_empty_list_when_directory_missing(self, tmp_path, monkeypatch):
        missing = tmp_path / "does_not_exist"
        monkeypatch.setenv("ROCK_TRACKING_LOAD_PATHS", str(missing))
        assert resolve_tracking_adapters() == []

    def test_returns_empty_list_when_directory_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROCK_TRACKING_LOAD_PATHS", str(tmp_path))
        assert resolve_tracking_adapters() == []

    def test_discovers_adapter_in_directory(self, tmp_path, monkeypatch):
        _write_adapter(tmp_path, "foo_adapter.py", "FooAdapter")
        monkeypatch.setenv("ROCK_TRACKING_LOAD_PATHS", str(tmp_path))

        result = resolve_tracking_adapters()

        assert len(result) == 1
        # verifies module-identity: the discovered class really is a
        # TrackingAdapter subclass (same class object across dynamic import)
        assert isinstance(result[0], TrackingAdapter)
        assert type(result[0]).__name__ == "FooAdapter"

    def test_discovers_multiple_adapters_across_files(self, tmp_path, monkeypatch):
        _write_adapter(tmp_path, "a_adapter.py", "AAdapter")
        _write_adapter(tmp_path, "b_adapter.py", "BAdapter")
        monkeypatch.setenv("ROCK_TRACKING_LOAD_PATHS", str(tmp_path))

        result = resolve_tracking_adapters()

        names = sorted(type(a).__name__ for a in result)
        assert names == ["AAdapter", "BAdapter"]

    def test_skips_file_with_import_error_loads_working(self, tmp_path, monkeypatch):
        _write_adapter(tmp_path, "good_adapter.py", "GoodAdapter")
        (tmp_path / "broken_adapter.py").write_text("import a_module_that_does_not_exist\n")
        monkeypatch.setenv("ROCK_TRACKING_LOAD_PATHS", str(tmp_path))

        result = resolve_tracking_adapters()

        assert [type(a).__name__ for a in result] == ["GoodAdapter"]

    def test_ignores_base_class_and_unrelated_classes(self, tmp_path, monkeypatch):
        # A file that imports the base class and defines an unrelated class,
        # but no concrete adapter — nothing should be discovered.
        (tmp_path / "noise.py").write_text(
            textwrap.dedent(
                """
                from rock.sdk.job.adapter import TrackingAdapter

                class NotAnAdapter:
                    pass
                """
            )
        )
        monkeypatch.setenv("ROCK_TRACKING_LOAD_PATHS", str(tmp_path))
        assert resolve_tracking_adapters() == []

    def test_ignores_init_and_non_py_files(self, tmp_path, monkeypatch):
        _write_adapter(tmp_path, "__init__.py", "InitAdapter")
        (tmp_path / "notes.txt").write_text("not python")
        monkeypatch.setenv("ROCK_TRACKING_LOAD_PATHS", str(tmp_path))
        assert resolve_tracking_adapters() == []

    def test_supports_multiple_comma_separated_paths(self, tmp_path, monkeypatch):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        _write_adapter(dir_a, "a_adapter.py", "AAdapter")
        _write_adapter(dir_b, "b_adapter.py", "BAdapter")
        monkeypatch.setenv("ROCK_TRACKING_LOAD_PATHS", f"{dir_a},{dir_b}")

        result = resolve_tracking_adapters()

        names = sorted(type(a).__name__ for a in result)
        assert names == ["AAdapter", "BAdapter"]
