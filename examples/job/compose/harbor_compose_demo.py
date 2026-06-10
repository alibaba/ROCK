"""ComposeJobConfig 端到端 demo —— 用 ComposeJobConfig 跑 harbor 任务。

这是经过 ROCK 真机后端（kata runtime）端到端验证的脚本：
  Job → kata 沙箱 → dockerd → proxy sidecar → health → 主容器(复用外层 dockerd)
       → harbor CLI 运行 → 下载 dataset → 跑 agent rollout

对应的 AP 命令（claude-code agent / aone-bench-java100 / glm-5）：
    ap job create harbor --instance-id codereview-20789198 -p '{...}' --runner rock

与 AP 命令的唯一区别：AP 平台自动注入 OSS 凭证，SDK 直连模式需你显式提供
（harbor 从 OSS 下载 dataset 必需）。把凭证放进环境变量即可。

────────────────────────────────────────────────────────────────────────────
用法：
    # 1) 配置凭证（必填）
    export ROCK_TOKEN='t-42c2a16fa5924e34'
    export MODEL='glm-5'
    export MODEL_BASE_URL='https://routify.alibaba-inc.com/protocol/openai/v1'
    export MODEL_API_KEY='sk-...'
    # OSS 凭证（harbor 下载 dataset 必需 —— AP 命令里没有，需你补）
    export OSS_BUCKET='<your-bucket>'
    export OSS_ENDPOINT='<your-endpoint>'        # e.g. oss-cn-hangzhou-internal.aliyuncs.com
    export OSS_REGION='<your-region>'            # e.g. cn-hangzhou
    export OSS_ACCESS_KEY_ID='<your-ak>'
    export OSS_ACCESS_KEY_SECRET='<your-sk>'

    # 2) 跑
    uv run python examples/job/compose/harbor_compose_demo.py

可选环境变量覆盖（都有默认值，对应 AP 命令的 -p 参数）：
    INSTANCE_ID(codereview-20789198) DATASET(terminal-bench/aone-bench-java100)
    SPLIT(test) HARBOR_AGENT(claude-code) ROCK_BASE_URL ROCK_CLUSTER
    HARBOR_MAIN_IMAGE PROXY_IMAGE JOB_TIMEOUT
────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from rock.sdk.envhub import EnvironmentConfig
from rock.sdk.job import Job
from rock.sdk.job.compose.config import (
    ComposeJobConfig,
    ComposeSpec,
    HealthSpec,
    MainContainerSpec,
    SidecarSpec,
    VolumeMount,
)
from rock.sdk.job.operator import ScatterOperator

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("harbor-compose-demo")

HERE = Path(__file__).resolve().parent

# ── 默认值（对应用户 AP 命令的 -p 参数）──────────────────────────────────────
DEFAULTS = {
    "ROCK_BASE_URL": "http://xrl.alibaba-inc.com",
    "ROCK_CLUSTER": "vpc-sg-a",
    "INSTANCE_ID": "codereview-20789198",
    "DATASET": "terminal-bench/aone-bench-java100",
    "SPLIT": "test",
    "HARBOR_AGENT": "claude-code",
    "HARBOR_MAIN_IMAGE": "rock-registry.ap-southeast-1.cr.aliyuncs.com/harbor/harbor:086a7b5822fc09891b190e18d",
    "PROXY_IMAGE": "agent-platform-staging-registry-vpc.ap-southeast-1.cr.aliyuncs.com/eflops/proxy-hub:bailian-usage-dev",
    "JOB_TIMEOUT": "9000",
}

# 必填凭证（缺一不可）
REQUIRED = [
    "ROCK_TOKEN",
    "MODEL",
    "MODEL_BASE_URL",
    "MODEL_API_KEY",
    "OSS_BUCKET",
    "OSS_ENDPOINT",
    "OSS_REGION",
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
]


def cfg(key: str) -> str:
    """取环境变量，回落到 DEFAULTS。"""
    return os.environ.get(key, DEFAULTS.get(key, ""))


def check_required() -> None:
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        print("缺少必填环境变量：")
        for k in missing:
            print(f"  - {k}")
        print("\n请先 export 这些变量（见本文件顶部 docstring）。")
        sys.exit(1)


def build_config() -> ComposeJobConfig:
    # 容器内 env（harbor main.sh + proxy sidecar 都从这里读）
    container_env = {
        # 模型
        "MODEL": cfg("MODEL"),
        "MODEL_BASE_URL": cfg("MODEL_BASE_URL"),
        "MODEL_API_KEY": cfg("MODEL_API_KEY"),
        # harbor 任务（对应 AP -p 参数）
        "HARBOR_AGENT": cfg("HARBOR_AGENT"),
        "INSTANCE_ID": cfg("INSTANCE_ID"),
        "DATASET": cfg("DATASET"),
        "SPLIT": cfg("SPLIT"),
        "DATASET_TYPE": "local",
        "HARBOR_ENV": "docker",
        "N_ATTEMPTS": "1",
        "N_CONCURRENT": "1",
        "TIMEOUT_MULTIPLIER": "3.0",
        "MAX_RETRIES": "3",
        "MAX_ITERATIONS": "200",
        "AGENT_VERSION": "2.1.87",
        "AGENT_TIMEOUT_MULTIPLIER": "8.0",
        "RETRY_INCLUDE": "NonZeroAgentExitCodeError",
        "FORCE_PROXY": "true",
        "PROVIDER": "anthropic",
        "TEMPERATURE": "1.0",
        "INTERLEAVED_THINKING": "true",
        "THINKING_TYPE": "adaptive",
        "REASONING_EFFORT": "high",
        "CONTEXT_1M": "true",
        "SKIP_CONFIRM": "true",
        "OUTPUT_DIR": "/tmp/output",
        "SHARED_DIR": "/tmp/shared",
        # OSS 凭证（harbor 下载 dataset 必需）
        "OSS_BUCKET": cfg("OSS_BUCKET"),
        "OSS_ENDPOINT": cfg("OSS_ENDPOINT"),
        "OSS_REGION": cfg("OSS_REGION"),
        "OSS_ACCESS_KEY_ID": cfg("OSS_ACCESS_KEY_ID"),
        "OSS_ACCESS_KEY_SECRET": cfg("OSS_ACCESS_KEY_SECRET"),
    }

    return ComposeJobConfig(
        job_name="harbor-compose-demo",
        timeout=int(cfg("JOB_TIMEOUT")),
        # 主容器入口脚本（harbor runner，从 Agent-Hub 复制并适配）
        script_path=str(HERE / "main.sh"),
        environment=EnvironmentConfig(
            # 外层沙箱镜像须自带 docker 工具链（不要用 docker:27-dind，kata 下缺 containerd）
            image=cfg("HARBOR_MAIN_IMAGE"),
            base_url=cfg("ROCK_BASE_URL"),
            cluster=cfg("ROCK_CLUSTER"),
            extra_headers={"XRL-Authorization": f"Bearer {cfg('ROCK_TOKEN')}"},
            use_kata_runtime=True,
            startup_timeout=1200,
            memory="32g",
            cpus=16,
            uploads=[
                (str(HERE / "main.sh"), "/rock/scripts/main.sh"),
                (str(HERE / "sidecars"), "/rock/scripts/sidecars"),
            ],
            env=container_env,
        ),
        compose=ComposeSpec(
            main=MainContainerSpec(
                image=cfg("HARBOR_MAIN_IMAGE"),
                privileged=True,
                env=container_env,
                # 复用外层 dockerd（挂载外层 docker socket），避免主容器内再起第三层 dockerd
                # —— 第三层 dockerd 在 kata 下会失败（"Docker daemon failed to start"）
                volume_mounts=[
                    VolumeMount(
                        name="docker-sock",
                        mount_path="/var/run/docker.sock",
                        host_path="/var/run/docker.sock",
                    )
                ],
            ),
            sidecars=[
                SidecarSpec(
                    name="proxy",
                    image=cfg("PROXY_IMAGE"),
                    script_path="/rock/scripts/sidecars/proxy-sidecar.sh",
                    env=container_env,
                    health=HealthSpec(port=8082, timeout_sec=120),
                ),
            ],
        ),
    )


async def main() -> None:
    config = build_config()
    logger.info("Submitting harbor task via ComposeJobConfig (job_name=%s) ...", config.job_name)
    logger.info("  backend=%s cluster=%s", config.environment.base_url, config.environment.cluster)
    logger.info("  dataset=%s split=%s agent=%s", cfg("DATASET"), cfg("SPLIT"), cfg("HARBOR_AGENT"))

    # size=1：单 trial（避免 ScatterOperator 共享 config 引用的竞态）
    result = await Job(config, operator=ScatterOperator(size=1)).run()

    logger.info("=== RESULT ===")
    logger.info("exit_code=%s status=%s score=%s", result.exit_code, result.status, result.score)
    for t in result.trial_results:
        logger.info("  trial=%s exit=%s score=%s", t.task_name, t.exit_code, t.score)
        if t.exception_info:
            logger.info("    error: %s: %s", t.exception_info.exception_type, t.exception_info.exception_message)


if __name__ == "__main__":
    check_required()
    asyncio.run(main())
