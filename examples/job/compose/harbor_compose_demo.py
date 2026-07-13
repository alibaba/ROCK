"""ComposeJobConfig 端到端 demo —— 用 ComposeJobConfig (v2) 跑 harbor 任务。

v2 变更：容器编排完全迁移到标准 docker-compose.yaml，job_config 只持有 compose_file 指针。
ROCK 不再解析 compose 内部结构，只负责：
  ① 准备 DinD 外层沙箱；② 引导 dockerd；③ docker compose up；④ 收退出码 + 可选 OSS 上传。

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
from rock.sdk.job.compose.config import ComposeJobConfig
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
    "DATASET": "alibaba/aone-bench-java100",
    "DATASET_NAME": "alibaba/aone-bench-java100",
    "DATASET_VERSION": "latest",
    "DATASET_TYPE": "registry",
    "SPLIT": "test",
    "HARBOR_AGENT": "claude-code",
    "HARBOR_MAIN_IMAGE": "rock-registry.ap-southeast-1.cr.aliyuncs.com/harbor/harbor:33180a83",
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
    # 外层沙箱 env（docker compose 执行时可用 ${VAR} 插值注入内层容器）
    sandbox_env = {
        # 模型
        "MODEL": cfg("MODEL"),
        "MODEL_BASE_URL": cfg("MODEL_BASE_URL"),
        "MODEL_API_KEY": cfg("MODEL_API_KEY"),
        # harbor 任务（对应 AP -p 参数）
        "HARBOR_AGENT": cfg("HARBOR_AGENT"),
        "INSTANCE_ID": cfg("INSTANCE_ID"),
        "DATASET": cfg("DATASET"),
        "DATASET_NAME": cfg("DATASET_NAME"),
        "DATASET_VERSION": cfg("DATASET_VERSION"),
        "SPLIT": cfg("SPLIT"),
        "DATASET_TYPE": cfg("DATASET_TYPE"),
        "N_ATTEMPTS": "1",
        "N_CONCURRENT": "1",
        "TIMEOUT_MULTIPLIER": "3.0",
        "MAX_RETRIES": "3",
        "MAX_ITERATIONS": "200",
        "SKIP_CONFIRM": "true",
        "FORCE_PROXY": "true",
        "PROVIDER": "anthropic",
        "TEMPERATURE": "1.0",
        "INTERLEAVED_THINKING": "true",
        "THINKING_TYPE": "adaptive",
        "REASONING_EFFORT": "high",
        "CONTEXT_1M": "true",
        # 镜像（compose file 里用 ${VAR} 引用，让用户只需改 env 而无需改 compose file）
        "HARBOR_MAIN_IMAGE": cfg("HARBOR_MAIN_IMAGE"),
        "PROXY_IMAGE": cfg("PROXY_IMAGE"),
        # OSS 凭证（harbor 下载 dataset 必需 + compose.main 产物上传）
        "OSS_BUCKET": cfg("OSS_BUCKET"),
        "OSS_ENDPOINT": cfg("OSS_ENDPOINT"),
        "OSS_REGION": cfg("OSS_REGION"),
        "OSS_ACCESS_KEY_ID": cfg("OSS_ACCESS_KEY_ID"),
        "OSS_ACCESS_KEY_SECRET": cfg("OSS_ACCESS_KEY_SECRET"),
    }

    return ComposeJobConfig(
        job_name="harbor-compose-demo",
        timeout=int(cfg("JOB_TIMEOUT")),
        # v2: compose_file 指向本地 docker-compose.yaml（相对路径）
        compose_file=str(HERE / "docker-compose.yaml"),
        abort_on_container_exit=True,
        environment=EnvironmentConfig(
            # 外层沙箱镜像：必须自带 docker 工具链（不要用 docker:27-dind，kata 下缺 containerd）
            # 这里复用 harbor runner 镜像（自带完整 docker 工具链 + harbor CLI + bash）
            image=cfg("HARBOR_MAIN_IMAGE"),
            base_url=cfg("ROCK_BASE_URL"),
            cluster=cfg("ROCK_CLUSTER"),
            extra_headers={"XRL-Authorization": f"Bearer {cfg('ROCK_TOKEN')}"},
            use_kata_runtime=True,
            startup_timeout=1200,
            memory="32g",
            cpus=16,
            # v2 uploads: compose 文件 + 脚本目录一起上传到 /rock/compose/
            uploads=[
                (str(HERE / "docker-compose.yaml"), "/rock/compose/docker-compose.yaml"),
                (str(HERE / "main.sh"), "/rock/compose/main.sh"),
                (str(HERE / "sidecars"), "/rock/compose/sidecars"),
            ],
            env=sandbox_env,
        ),
    )


async def main() -> None:
    config = build_config()
    logger.info("Submitting harbor task via ComposeJobConfig v2 (job_name=%s) ...", config.job_name)
    logger.info("  backend=%s cluster=%s", config.environment.base_url, config.environment.cluster)
    logger.info("  dataset=%s split=%s agent=%s", cfg("DATASET"), cfg("SPLIT"), cfg("HARBOR_AGENT"))
    logger.info("  compose_file=%s", config.compose_file)

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
