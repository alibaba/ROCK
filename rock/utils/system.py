import asyncio
import datetime
import logging
import os
import re
import socket
import subprocess
import time
import zoneinfo
from pathlib import Path
from threading import Lock

from rock import env_vars
from rock.common.constants import PID_PREFIX

logger = logging.getLogger(__name__)


_REGISTERED_PORTS = set()
_REGISTERED_PORTS_LOCK = Lock()


def run_command_with_output(cmd, wait=False):
    """Execute a command and optionally wait for result

    Args:
        cmd: Command to execute
        wait: Whether to wait for completion

    Returns:
        Tuple of (return_code, stdout, stderr) if wait=True, else None
    """
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if wait:
            stdout, stderr = process.communicate()
            if stdout:
                logger.info(f"stdout:\n{stdout}")
            if stderr:
                logger.error(f"stderr:\n{stderr}")
            logger.info(f"code: {process.returncode}")
            return process.returncode, stdout, stderr
    except Exception as e:
        logger.exception(f"failed to run command: {e}")
        return -1, "", str(e)


async def run_shell_command(command: str) -> tuple[int, str, str]:
    """
    Asynchronously execute a shell command and return its return code, stdout, and stderr.

    Args:
        command: Shell command to execute.

    Returns:
        Tuple containing (return_code, stdout, stderr).
    """
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


def extract_nohup_pid(nohup_output: str) -> int:
    """
    Extract process ID from nohup command output.

    Args:
        nohup_output: Output string from nohup command.

    Returns:
        Process ID as string, or empty string if extraction fails.
    """
    try:
        pid = re.findall(f"{PID_PREFIX}([0-9]+)", nohup_output)
        return int(pid[0])
    except Exception:
        return None


async def find_free_port(max_attempts: int = 10, sleep_between_attempts: float = 0.1) -> int:
    """Find a free port that is not yet registered

    Args:
        max_attempts: Maximum number of attempts to find a port
        sleep_between_attempts: Time to sleep between attempts

    Returns:
        Available port number

    Raises:
        RuntimeError: If unable to find a free port after max_attempts
    """
    for _ in range(max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            with _REGISTERED_PORTS_LOCK:
                s.bind(("", 0))
                port = s.getsockname()[1]
                if port not in _REGISTERED_PORTS:
                    _REGISTERED_PORTS.add(port)
                    logger.debug(f"Found free port {port}")
                    return port
            logger.debug(f"Port {port} already registered, trying again after {sleep_between_attempts}s")
        time.sleep(sleep_between_attempts)
    msg = f"Failed to find a unique free port after {max_attempts} attempts"
    raise RuntimeError(msg)


def release_port(port: int):
    """Release a previously registered port

    Args:
        port: Port number to release
    """
    if port:
        _REGISTERED_PORTS.discard(port)


def get_host_ip():
    """Get host IP from /etc/hostinfo file

    Returns:
        Host IP address or None if failed
    """
    try:
        with open("/etc/hostinfo") as file:
            for line in file:
                host_ip = line.strip()
        logging.info(f"Get host ip: {host_ip}")
        return host_ip
    except Exception as e:
        logging.error(f"Get host ip error: {e}")
        return None


def get_pod_ip():
    try:
        hostname = socket.gethostname()
        pod_ip = socket.gethostbyname(hostname)
        return pod_ip
    except Exception as e:
        logger.error(f"Failed to get pod IP: {e}")
        return None


def get_instance_id():
    """get instance unique identifier(multi degrade)"""
    # 1. use pod ip firstly
    pod_ip = get_pod_ip()
    if pod_ip:
        return pod_ip
    # 2. degrade: use hostname
    try:
        hostname = socket.gethostname()
        if hostname:
            logger.warning(f"Using hostname as instance ID: {hostname}")
            return hostname
    except (OSError, Exception) as e:
        logger.error(f"Failed to get hostname: {e}")

    # 3. final fallback: host_ip + pid
    host_ip = get_host_ip()
    pid = os.getpid()
    fallback_id = f"{host_ip}:{pid}"
    logger.warning(f"Using fallback instance ID: {fallback_id}")
    return fallback_id


def get_uniagent_endpoint(
    host_info_path: str = "/etc/hostinfo",
    default_host: str = "localhost",
    default_port: str = "4318",
) -> tuple[str, str]:
    """
    Read UniAgent's IP address from hostinfo file and return OTLP export endpoint configuration

    Args:
        host_info_path: hostinfo file path
        default_host: default host address
        default_port: default port number

    Returns:
        Tuple[str, str]: (host, port) tuple
    """
    try:
        if not Path(host_info_path).exists():
            logging.warning(f"Host info file {host_info_path} not found, using default endpoint")
            return default_host, default_port
        ip_pattern = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")
        with open(host_info_path) as f:
            for line in f:
                if match := ip_pattern.search(line):
                    host = match.group(1)
                    logging.info(f"Found UniAgent IP: {host}")
                    return host, default_port
        logging.warning(f"No valid IP found in {host_info_path}, using default endpoint")
        return default_host, default_port
    except Exception as e:
        logging.error(f"Error reading UniAgent endpoint: {e}")
        return default_host, default_port


def get_iso8601_timestamp(timestamp: int = None, timezone: str = None):
    """Get current timestamp in ISO8601 format with timezone

    Returns:
        ISO8601 formatted timestamp string (e.g., "2026-01-21T19:38:00+08:00")
    """

    tz = zoneinfo.ZoneInfo(timezone if timezone else env_vars.ROCK_TIME_ZONE)
    if timestamp:
        time = datetime.datetime.fromtimestamp(timestamp, tz)
    else:
        time = datetime.datetime.now(tz)
    return time.isoformat(timespec="seconds")

def is_primary_pod() -> bool:
    """
    Check if the current pod is the primary pod (index 0).
    Reads /etc/hostname file and parses the pod index from the pod name.
    Hostname format example: rock-admin-write-nt-gray-0.rock-admin-write-nt-gray-hs.chatos.svc.cluster.local
    """
    try:
        with open("/etc/hostname") as f:
            hostname = f.read().strip()
        # Extract pod name (content before the first dot)
        pod_name = hostname.split(".")[0]
        # Extract index (number after the last hyphen)
        pod_index = pod_name.rsplit("-", 1)[-1]
        return pod_index == "0"
    except Exception as e:
        # If unable to read or parse, default to True (for local dev environment, etc.)
        logger.warning(f"Failed to determine pod index from hostname: {e}, defaulting to primary")
        return True
