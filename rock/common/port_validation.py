"""Port validation utilities for port forwarding."""
from rock.logger import init_logger

logger = init_logger(__name__)

# Port forwarding constants
PORT_FORWARD_MIN_PORT = 1024
PORT_FORWARD_MAX_PORT = 65535
PORT_FORWARD_EXCLUDED_PORTS = {22}  # SSH port


def validate_port_forward_port(port: int) -> tuple[bool, str | None]:
    """Validate if the port is allowed for port forwarding.

    Args:
        port: The port number to validate.

    Returns:
        A tuple of (is_valid, error_message).
        If valid, error_message is None.
    """
    logger.debug(
        f"[Portforward] Validating port: port={port}, "
        f"min={PORT_FORWARD_MIN_PORT}, max={PORT_FORWARD_MAX_PORT}, "
        f"excluded={PORT_FORWARD_EXCLUDED_PORTS}"
    )
    if port < PORT_FORWARD_MIN_PORT:
        error_msg = f"Port {port} is below minimum allowed port {PORT_FORWARD_MIN_PORT}"
        logger.warning(f"[Portforward] Port validation failed: {error_msg}")
        return False, error_msg
    if port > PORT_FORWARD_MAX_PORT:
        error_msg = f"Port {port} is above maximum allowed port {PORT_FORWARD_MAX_PORT}"
        logger.warning(f"[Portforward] Port validation failed: {error_msg}")
        return False, error_msg
    if port in PORT_FORWARD_EXCLUDED_PORTS:
        error_msg = f"Port {port} is not allowed for port forwarding"
        logger.warning(f"[Portforward] Port validation failed: {error_msg}")
        return False, error_msg
    logger.debug(f"[Portforward] Port validation passed: port={port}")
    return True, None
