import json
import logging

from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.logger import init_logger

billing_logger = init_logger(name="billing", file_name="billing.log")


def log_billing_info(logger: logging.Logger | None = None, sandbox_info: SandboxInfo | None = None) -> None:
    if not sandbox_info:
        return

    try:
        if not logger:
            logger = billing_logger
        logger.info(json.dumps(sandbox_info, ensure_ascii=False))
    except Exception as e:
        billing_logger.error(f"Failed to log billing info: {e}", exc_info=True)
