"""
Archive P0 手动 E2E 测试脚本（基于 SDK）

覆盖场景：
  A. stopped → archived（archive 全流程）
  B. archived → restoring → running（restore 全流程）
  C. archived → deleted（直接从 archived 删除）

交互式流程：start → stop → archive → wait ARCHIVED → restart(restore) → wait RUNNING
           → stop → archive → wait ARCHIVED → delete(from archived)

前置条件：
  1. MinIO 容器运行中（端口 9000），已创建 bucket "rock-archive-test"
  2. Docker Registry 容器运行中（端口 5000，开启 delete）
  3. admin 服务运行中（端口 8080），已注入 archive storage

启动命令：
    uv run python test_archive_manual_e2e.py

环境变量（可选）：
    ROCK_BASE_URL      admin 地址，默认 http://localhost:8080
    ROCK_IMAGE         测试用镜像，默认 python:3.11
    ARCHIVE_TIMEOUT    等待 ARCHIVED 超时秒数，默认 300
"""

import asyncio
import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

BASE_URL = os.getenv("ROCK_BASE_URL", "http://localhost:8080")
IMAGE = os.getenv("ROCK_IMAGE", "python:3.11")
ARCHIVE_TIMEOUT = int(os.getenv("ARCHIVE_TIMEOUT", "300"))
INTERACTIVE = os.getenv("INTERACTIVE", "0") == "1"


def banner(step: str, msg: str):
    logger.info(f"\n{'=' * 70}\n  [{step}] {msg}\n{'=' * 70}")


def pause(hint: str = ""):
    if hint:
        logger.info(f"\n  手动检查提示:\n{hint}")
    if INTERACTIVE:
        input("\n  >>> 按 Enter 继续下一步...")
    else:
        logger.info("  (非交互模式，自动继续)")


async def wait_state(sandbox, target_state: str, timeout: int = ARCHIVE_TIMEOUT):
    """轮询 get_status 直到 state 达到 target，或超时。"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            status = await sandbox.get_status(include_all_states=True)
        except Exception as e:
            logger.info(f"    get_status 暂时失败: {e}")
            await asyncio.sleep(5)
            continue
        current = status.state
        logger.info(f"    当前 state: {current}")
        if current == target_state:
            return status
        await asyncio.sleep(5)
    raise TimeoutError(f"等待 state={target_state} 超时（{timeout}s）")


async def wait_state_transition(sandbox, intermediate_state: str, final_state: str, timeout: int = ARCHIVE_TIMEOUT):
    """轮询 get_status，验证经过 intermediate_state 后到达 final_state。"""
    start = time.time()
    saw_intermediate = False
    while time.time() - start < timeout:
        try:
            status = await sandbox.get_status(include_all_states=True)
        except Exception as e:
            logger.info(f"    get_status 暂时失败: {e}")
            await asyncio.sleep(3)
            continue
        current = status.state
        logger.info(f"    当前 state: {current}")
        if current == intermediate_state:
            saw_intermediate = True
        if current == final_state:
            return status, saw_intermediate
        await asyncio.sleep(3)
    raise TimeoutError(f"等待 state={final_state} 超时（{timeout}s）")


async def main():
    from rock.sdk.sandbox.client import Sandbox
    from rock.sdk.sandbox.config import SandboxConfig

    config = SandboxConfig(
        base_url=BASE_URL,
        image=IMAGE,
        auto_clear_seconds=600,
        auto_delete_seconds=600,
        memory="1g",
        cpus=1,
    )
    sandbox = Sandbox(config)
    sandbox_id = None

    try:
        # ═══════════════════════════════════════════════════════════════════
        # SCENARIO A: stopped → archived
        # ═══════════════════════════════════════════════════════════════════

        banner("1/11", "启动 Sandbox")
        await sandbox.start()
        sandbox_id = sandbox.sandbox_id
        logger.info(f"  sandbox_id: {sandbox_id}")
        logger.info(f"  host_ip:    {sandbox.host_ip}")

        pause(f"    docker ps --format '{{{{.Names}}}} {{{{.Status}}}}' | grep {sandbox_id}")

        banner("2/11", "停止 Sandbox")
        await sandbox.stop()
        await asyncio.sleep(3)
        status = await sandbox.get_status(include_all_states=True)
        logger.info(f"  state: {status.state} (应为 stopped)")
        assert status.state == "stopped", f"预期 stopped，实际 {status.state}"

        banner("3/11", "触发 Archive (stopped → archiving → archived)")
        await sandbox.archive()
        logger.info("  archive 已触发，等待 ARCHIVED...")

        banner("4/11", f"等待扫描器推进到 ARCHIVED（超时 {ARCHIVE_TIMEOUT}s）")
        status = await wait_state(sandbox, "archived")
        logger.info("  ✓ stopped → archived 成功!")

        pause(
            f"    # MinIO 应有 tar.gz:\n"
            f"    python3 -c \"import boto3; s3=boto3.client('s3',endpoint_url='http://localhost:9000',aws_access_key_id='rockadmin',aws_secret_access_key='rockadmin123',region_name='us-east-1'); print([o['Key'] for o in s3.list_objects_v2(Bucket='rock-archive-test',Prefix='rock-archives/datalog-{sandbox_id}').get('Contents',[])])\"\n\n"
            f"    # Registry 应有 tag:\n"
            f"    curl -s http://localhost:5000/v2/rock-snapshots/archived-{sandbox_id}/tags/list"
        )

        # ═══════════════════════════════════════════════════════════════════
        # SCENARIO B: archived → restoring → running
        # ═══════════════════════════════════════════════════════════════════

        banner("5/11", "Restart（从 ARCHIVED 恢复 → RESTORING → RUNNING）")
        logger.info("  调用 restart → 内部 restore + docker start（异步）")
        await sandbox.restart()

        banner("6/11", "等待经过 RESTORING 到达 RUNNING")
        status, saw_restoring = await wait_state_transition(sandbox, "restoring", "running", timeout=180)
        if saw_restoring:
            logger.info("  ✓ 观察到 RESTORING 中间状态!")
        else:
            logger.warning("  ⚠ 未捕获到 RESTORING（可能转换太快），但最终到达 RUNNING")
        logger.info("  ✓ archived → restoring → running 成功!")

        pause(
            f"    # 容器应重新运行:\n"
            f"    docker ps --format '{{{{.Names}}}} {{{{.Status}}}}' | grep {sandbox_id}\n\n"
            f"    # API 应返回 running:\n"
            f"    curl -s 'http://localhost:8080/apis/envs/sandbox/v1/get_status?sandbox_id={sandbox_id}&include_all_states=True' | python3 -m json.tool | grep state"
        )

        # ═══════════════════════════════════════════════════════════════════
        # SCENARIO C: archived → deleted (直接从 archived 删除)
        # ═══════════════════════════════════════════════════════════════════

        banner("7/11", "停止 Sandbox（为第二次 archive 做准备）")
        await sandbox.stop()
        await asyncio.sleep(3)
        status = await sandbox.get_status(include_all_states=True)
        assert status.state == "stopped", f"预期 stopped，实际 {status.state}"
        logger.info("  ✓ stopped")

        banner("8/11", "第二次 Archive")
        await sandbox.archive()
        logger.info("  archive 已触发")

        banner("9/11", "等待第二次 ARCHIVED")
        status = await wait_state(sandbox, "archived")
        logger.info("  ✓ 再次到达 ARCHIVED!")

        banner("10/11", "从 ARCHIVED 直接 Delete（验证 archived → deleted）")
        await sandbox.delete()
        logger.info("  delete 已调用")
        await asyncio.sleep(2)

        banner("11/11", "验证最终状态为 DELETED")
        status = await sandbox.get_status(include_all_states=True)
        logger.info(f"  state: {status.state}")
        assert status.state == "deleted", f"预期 deleted，实际 {status.state}"
        logger.info("  ✓ archived → deleted 成功!")

        pause(
            f"    # 容器应已删除:\n"
            f"    docker ps -a | grep {sandbox_id} && echo 'FAIL' || echo 'OK: 已删除'\n\n"
            f"    # MinIO archive 对象应已清理:\n"
            f"    python3 -c \"import boto3; s3=boto3.client('s3',endpoint_url='http://localhost:9000',aws_access_key_id='rockadmin',aws_secret_access_key='rockadmin123',region_name='us-east-1'); objs=s3.list_objects_v2(Bucket='rock-archive-test',Prefix='rock-archives/datalog-{sandbox_id}').get('Contents',[]); print('FAIL: 未清理', [o['Key'] for o in objs]) if objs else print('OK: 已清理')\"\n\n"
            f"    # Registry tag 应已删除:\n"
            f"    curl -s http://localhost:5000/v2/rock-snapshots/archived-{sandbox_id}/tags/list"
        )

        logger.info("\n" + "=" * 70)
        logger.info("  ALL SCENARIOS PASSED:")
        logger.info("    A. stopped → archived              ✓")
        logger.info("    B. archived → restoring → running  ✓")
        logger.info("    C. archived → deleted              ✓")
        logger.info("=" * 70)

    except Exception as e:
        logger.error(f"\n  测试失败: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if sandbox_id:
            logger.info(f"\n  [Cleanup] sandbox_id: {sandbox_id}")
            try:
                await sandbox.stop()
            except Exception:
                pass
            try:
                await sandbox.delete()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
