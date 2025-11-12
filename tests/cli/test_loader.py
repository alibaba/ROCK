from pathlib import Path

import pytest

from rock.cli.loader import CommandLoader
from rock.logger import init_logger

logger = init_logger(__name__)


@pytest.mark.asyncio
async def test_load():
    # 定义项目根目录（相对当前文件）
    PROJECT_ROOT = Path(__file__).resolve().parents[2]  # 根据实际层级调整
    COMMAND_PATH = PROJECT_ROOT / "rock" / "cli" / "command"

    subclasses = await CommandLoader.load([str(COMMAND_PATH)])
    logger.info(f"subclasses is {subclasses}")
    assert len(subclasses) > 0
