"""Tests for the NonBlankStr Pydantic type."""

import pytest
from pydantic import BaseModel, ValidationError

from rock.common.validation import NonBlankStr


class _Model(BaseModel):
    value: NonBlankStr


def test_accepts_normal_string():
    assert _Model(value="abc").value == "abc"


def test_strips_surrounding_whitespace():
    assert _Model(value="  abc  ").value == "abc"


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_rejects_empty_or_whitespace(bad):
    with pytest.raises(ValidationError):
        _Model(value=bad)


def test_rejects_missing_field():
    with pytest.raises(ValidationError):
        _Model()
