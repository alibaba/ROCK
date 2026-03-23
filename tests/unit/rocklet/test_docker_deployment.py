import pytest

from rock import env_vars
from rock.actions import (
    BashAction,
    CloseBashSessionRequest,
    Command,
    CreateBashSessionRequest,
)
from rock.config import RuntimeConfig
from rock.deployments.config import DockerDeploymentConfig, get_deployment
from rock.deployments.docker import DockerDeployment


@pytest.mark.need_docker
async def test_docker_deployment(container_name):
    deployment_config = DockerDeploymentConfig(
        image=env_vars.ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE,
        container_name=container_name,
    )
    d = get_deployment(deployment_config)
    with pytest.raises(RuntimeError):
        await d.is_alive()
    await d.start()
    assert await d.is_alive()
    command = Command(command=["echo", "hello"])
    await d.runtime.execute(command)

    # test bash session with default env
    create_session_request = CreateBashSessionRequest(session_type="bash")
    await d.runtime.create_session(create_session_request)
    action = BashAction(command="echo $PATH")
    path_result_with_env = await d.runtime.run_in_session(action)
    print(path_result_with_env.output)
    action = BashAction(command="echo $HOME")
    home_result_with_env = await d.runtime.run_in_session(action)
    print(home_result_with_env.output)
    close_session_request = CloseBashSessionRequest(session_type="bash")
    await d.runtime.close_session(close_session_request)

    # test bash session without default env
    create_session_request = CreateBashSessionRequest(session_type="bash", env_enable=False)
    await d.runtime.create_session(create_session_request)
    action = BashAction(command="echo $PATH")
    path_result = await d.runtime.run_in_session(action)
    print(path_result.output)
    action = BashAction(command="echo $HOME")
    home_result = await d.runtime.run_in_session(action)
    print(home_result.output)
    close_session_request = CloseBashSessionRequest(session_type="bash")
    await d.runtime.close_session(close_session_request)
    await d.stop()


def test_docker_deployment_config_platform():
    config = DockerDeploymentConfig(docker_args=["--platform", "linux/amd64", "--other-arg"])
    assert config.platform == "linux/amd64"

    config = DockerDeploymentConfig(docker_args=["--platform=linux/amd64", "--other-arg"])
    assert config.platform == "linux/amd64"

    config = DockerDeploymentConfig(docker_args=["--other-arg"])
    assert config.platform is None

    with pytest.raises(ValueError):
        config = DockerDeploymentConfig(platform="linux/amd64", docker_args=["--platform", "linux/amd64"])
    with pytest.raises(ValueError):
        config = DockerDeploymentConfig(platform="linux/amd64", docker_args=["--platform=linux/amd64"])


def test_build_gpu_args_disabled_by_default(monkeypatch):
    deployment = DockerDeployment(runtime_config=RuntimeConfig())

    monkeypatch.delenv("ROCK_ENABLE_GPU_PASSTHROUGH", raising=False)

    assert deployment._build_gpu_args() == []
    assert deployment._build_gpu_env_args() == []


def test_build_gpu_args_fixed_mode_from_runtime():
    deployment = DockerDeployment(
        runtime_config=RuntimeConfig(
            enable_gpu_passthrough=True,
            gpu_allocation_mode="fixed",
            gpu_device_request="device=2",
        )
    )

    assert deployment._build_gpu_args() == ["--gpus", "device=2"]
    assert deployment._build_gpu_env_args() == [
        "-e",
        "NVIDIA_VISIBLE_DEVICES=2",
        "-e",
        "CUDA_VISIBLE_DEVICES=2",
    ]


def test_build_gpu_args_skips_when_docker_args_already_set():
    deployment = DockerDeployment(
        docker_args=["--gpus", "all"],
        runtime_config=RuntimeConfig(enable_gpu_passthrough=True),
    )

    assert deployment._build_gpu_args() == []


def test_build_gpu_args_round_robin(monkeypatch, tmp_path):
    deployment = DockerDeployment(
        runtime_config=RuntimeConfig(
            enable_gpu_passthrough=True,
            gpu_allocation_mode="round_robin",
            gpu_count_per_sandbox=2,
        )
    )

    monkeypatch.setattr(deployment, "_detect_gpu_count", lambda: 4)
    monkeypatch.setenv("ROCK_GPU_COUNTER_PATH", str(tmp_path / "gpu_rr_counter"))

    first = deployment._resolve_round_robin_gpu_spec(2)
    second = deployment._resolve_round_robin_gpu_spec(2)

    assert first == "device=0,1"
    assert second == "device=2,3"
