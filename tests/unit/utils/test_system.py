import json
import subprocess

from rock.utils import system


def test_get_docker_used_host_ports_keeps_partial_inspect_results(monkeypatch):
    """A container removed during the scan must not hide surviving ports."""
    responses = iter(
        [
            subprocess.CompletedProcess(
                args=["docker", "ps", "-aq"],
                returncode=0,
                stdout="live-container\nstale-container\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["docker", "inspect"],
                returncode=1,
                stdout=json.dumps({"22555/tcp": [{"HostIp": "", "HostPort": "42463"}]}),
                stderr="error: no such object: stale-container",
            ),
        ]
    )

    monkeypatch.setattr(system.subprocess, "run", lambda *args, **kwargs: next(responses))
    system._DOCKER_USED_PORTS.clear()

    assert system._get_docker_used_host_ports() == {42463}
