import pytest

from rock.sdk.envhub.datasets.formats import get_parser
from rock.sdk.envhub.datasets.formats.base import FormatParser


def test_get_parser_swe():
    parser = get_parser("swe")
    assert isinstance(parser, FormatParser)


def test_get_parser_tb2():
    parser = get_parser("tb2")
    assert isinstance(parser, FormatParser)


def test_get_parser_pinchbench():
    parser = get_parser("pinchbench")
    assert isinstance(parser, FormatParser)


def test_get_parser_unknown():
    with pytest.raises(ValueError, match="No parser registered"):
        get_parser("nonexistent")


class TestSweParser:
    def test_extract(self):
        parser = get_parser("swe")
        raw = {"instance_id": "django__django-13121", "repo": "django/django", "base_commit": "abc123"}
        result = parser.extract(raw)
        assert result["instance_id"] == "django__django-13121"
        assert result["repo"] == "django/django"
        assert result["language"] == "python"
        assert result["base_commit"] == "abc123"

    def test_extract_missing_instance_id(self):
        parser = get_parser("swe")
        with pytest.raises(ValueError, match="instance_id"):
            parser.extract({})

    def test_extract_image_uri(self):
        parser = get_parser("swe")
        raw = {"instance_id": "x", "docker_image": "registry.io/img:tag"}
        result = parser.extract(raw)
        assert result["image_uri"] == "registry.io/img:tag"

    def test_extract_source_files(self):
        parser = get_parser("swe")
        raw = {"patch": "diff content", "test_patch": "test diff"}
        files = parser.extract_source_files(raw)
        assert len(files) == 2
        assert files[0]["path"] == "patch.diff"
        assert files[1]["path"] == "test_patch.diff"

    def test_extract_source_files_empty(self):
        parser = get_parser("swe")
        files = parser.extract_source_files({})
        assert files == []

    def test_validate_warnings(self):
        parser = get_parser("swe")
        warnings = parser.validate({})
        assert any("instance_id" in w for w in warnings)
        assert any("repo" in w for w in warnings)
        assert any("base_commit" in w for w in warnings)

    def test_validate_ok(self):
        parser = get_parser("swe")
        raw = {"instance_id": "x", "repo": "a/b", "base_commit": "c", "patch": "d"}
        warnings = parser.validate(raw)
        assert warnings == []


class TestTb2Parser:
    def test_extract(self):
        parser = get_parser("tb2")
        raw = {"instance_id": "task-1", "repo": "r", "language": "go"}
        result = parser.extract(raw)
        assert result["instance_id"] == "task-1"
        assert result["language"] == "go"

    def test_extract_missing_instance_id(self):
        parser = get_parser("tb2")
        with pytest.raises(ValueError, match="instance_id"):
            parser.extract({})

    def test_extract_source_files(self):
        parser = get_parser("tb2")
        raw = {"files": {"setup.sh": "#!/bin/bash", "config.yml": "key: val"}}
        files = parser.extract_source_files(raw)
        assert len(files) == 2
        paths = {f["path"] for f in files}
        assert paths == {"setup.sh", "config.yml"}


class TestPinchBenchParser:
    def test_extract(self):
        parser = get_parser("pinchbench")
        raw = {"instance_id": "pb-1", "repo": "r", "language": "java"}
        result = parser.extract(raw)
        assert result["instance_id"] == "pb-1"

    def test_extract_source_files_with_patch(self):
        parser = get_parser("pinchbench")
        raw = {"patch": "some patch"}
        files = parser.extract_source_files(raw)
        assert len(files) == 1
        assert files[0]["path"] == "patch.diff"

    def test_validate_missing_instance_id(self):
        parser = get_parser("pinchbench")
        warnings = parser.validate({})
        assert any("instance_id" in w for w in warnings)
