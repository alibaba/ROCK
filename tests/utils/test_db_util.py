from pathlib import Path

import pytest

from rock.utils import is_absolute_db_path


@pytest.mark.asyncio
async def test_is_absolute_db_path():
    assert is_absolute_db_path("sqlite:///relative/path/db.sqlite") is False
    assert is_absolute_db_path("sqlite:////absolute/path/db.sqlite") is True
    assert is_absolute_db_path(f"sqlite:///{Path.home() / '.rock' / 'rock_envs.db'}") is True
    assert is_absolute_db_path("sqlite:///~/.rock/rock_envs.db") is False
