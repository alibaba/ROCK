import asyncio
import json
import logging
import os
import traceback
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum

from rock.actions import Command, CommandResponse, CreateBashSessionRequest
from rock.sdk.sandbox.client import Sandbox, SandboxGroup
from rock.sdk.sandbox.config import SandboxGroupConfig
from rock.utils import FileUtil, retry_async

logger = logging.getLogger(__name__)


class EnvBuilderStatus(Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class EnvBuilder(ABC):
    DEFAULT_PERSIST_PARENT_PATH = "data/output/env-build"
    DEFAULT_PERSIST_FILE_PATH = f"{DEFAULT_PERSIST_PARENT_PATH}/result.jsonl"

    @abstractmethod
    async def build(self, instance_record: dict[str, str] | None = None, **kwargs):
        """Build environment."""
        pass

    async def build_batch(self, dataset: str, **kwargs):
        """Build environments in batch."""
        await self._pre_persist_status()
        record_index = 0
        line_count = await FileUtil.get_line_count(dataset)
        with open(dataset, encoding="utf-8") as file:
            for line in file:
                if not line:
                    logger.info("line is empty, finished")
                    break
                line_content = line.strip()
                instance_record = json.loads(line_content)

                logger.info(f"start to handle line {record_index}/{line_count}")
                logger.debug(f"record: {instance_record}")
                try:
                    await self.build(instance_record=instance_record, **kwargs)
                    await self._persist_status(
                        record_index, instance_record, EnvBuilderStatus.SUCCESS, message="build finished"
                    )
                except Exception as e:
                    logger.error(f"build for {record_index}/{line_count} failed, {str(e)}", exc_info=True)
                    error_message = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                    await self._persist_status(
                        record_index, instance_record, EnvBuilderStatus.FAILED, message=error_message
                    )
                record_index += 1

    @abstractmethod
    async def verify(self, **kwargs):
        """Verify environment."""
        pass

    async def build_remote(
        self,
        authorization: str,
        cluster: str,
        dataset: str,
        concurrency: int,
        **kwargs,
    ):
        filename = dataset
        tmp_dir = f"tmp/dest-dir-{datetime.now().timestamp()}"

        if not os.path.exists(tmp_dir):
            os.makedirs(tmp_dir)
        await FileUtil.split_file(filename, concurrency, tmp_dir)
        tmp_filename_list = [f"{tmp_dir}/{i}.jsonl" for i in range(concurrency)]

        sandbox_group = SandboxGroup(
            SandboxGroupConfig(
                size=concurrency,
                start_concurrency=10,
                start_retry_times=60,
                image=await self.get_env_build_image(),
                auto_clear_seconds=60 * 10,
                user_id="rock-env-builder",
                experiment_id=f"{dataset}",
                xrl_authorization=authorization,
                cluster=cluster,
            )
        )
        await sandbox_group.start()

        futures = []
        for i in range(concurrency):
            futures.append(
                asyncio.create_task(
                    self._do_build_remote_one_split(i, sandbox_group.sandbox_list[i], tmp_filename_list[i], **kwargs)
                )
            )

        report_future = asyncio.create_task(self._report_status(sandbox_group.sandbox_list))

        # wait for all futures
        await asyncio.gather(*futures, return_exceptions=True)
        report_future.cancel()
        await self._report_once(
            sandbox_group.sandbox_list,
            dest_filename=f"data/output/env-build/result-{datetime.now().timestamp()}.jsonl",
            retry_attempts=3,
        )

    async def get_env_build_image(self) -> str:
        """Get environment build image."""
        raise NotImplementedError

    async def get_build_remote_one_split_command(self, split_filename: str, **kwargs) -> str:
        """Get build remote one split command."""
        raise NotImplementedError

    async def _pre_persist_status(self):
        parent_dir = self.DEFAULT_PERSIST_PARENT_PATH
        file_name = self.DEFAULT_PERSIST_FILE_PATH
        if not os.path.exists(parent_dir):
            os.makedirs(parent_dir)
        if os.path.exists(file_name):
            backup_file_name = f"{file_name}.backup-at-{datetime.now().timestamp()}"
            logger.info(f"backup env build output file {file_name} to {backup_file_name}")
            os.rename(file_name, backup_file_name)

    async def _persist_status(
        self, record_index: int, instance_record: dict[str, str], status: EnvBuilderStatus, message: str = ""
    ):
        file_name = self.DEFAULT_PERSIST_FILE_PATH
        instance_record["rock_env_build_result"] = status.name
        instance_record["rock_env_build_message"] = message
        logger.info(f"persist instance {record_index} status {status} to {file_name}")
        with open(file_name, "a") as file:
            file.write(json.dumps(instance_record) + "\n")

    async def _report_status(self, sandbox_list: list[Sandbox]):
        while True:
            await asyncio.sleep(60)
            try:
                await self._report_once(sandbox_list)
            except Exception as e:
                logger.error(f"report status failed {e}")

    async def _report_once(
        self, sandbox_list: list[Sandbox], dest_filename: str | None = None, retry_attempts: int = 1
    ):
        statistic_dict: dict[int, str] = {}
        for i in range(len(sandbox_list)):
            try:

                @retry_async(max_attempts=retry_attempts)
                async def query_status_for_sandbox(sandbox: Sandbox):
                    return await self._query_status_for_sandbox(sandbox)

                status = await query_status_for_sandbox(sandbox_list[i])
                logger.debug(f"sandbox {i} [{sandbox_list[i].sandbox_id}] status: {status}")
                statistic = await self._parse_status_statistic(sandbox_list[i], status)
                statistic_dict[i] = statistic
                if dest_filename is not None:
                    with open(dest_filename, "a") as file:
                        file.write(status.stdout)
            except Exception as e:
                logger.warning(f"report sandbox {i} [{sandbox_list[i].sandbox_id}] status failed {e}", exc_info=True)

        for i in range(len(statistic_dict)):
            statistic = statistic_dict[i]
            logger.info(f"report statistic {i}: {statistic}")
        if dest_filename is not None:
            logger.info(f"report finished! please check {dest_filename}")

    async def _parse_status_statistic(self, sandbox: Sandbox, status: CommandResponse) -> str:
        if status.exit_code != 0:
            return f"failed to query sandbox {sandbox.sandbox_id}, stderr: {status.stderr}"

        status_count_dict: dict[str, int] = {}
        failed_list: list[str] = []
        content = status.stdout
        lines = content.strip().splitlines()
        for line in lines:
            json_obj = json.loads(line)
            env_build_result = json_obj.get("rock_env_build_result")
            if env_build_result is not None:
                status_count_dict[env_build_result] = status_count_dict.get(env_build_result, 0) + 1
                if EnvBuilderStatus.FAILED.name == env_build_result:
                    failed_list.append(json_obj)

        result = f"sandbox {sandbox.sandbox_id} status: {status_count_dict}"
        if len(failed_list) > 0:
            result = f"{result}, failed list: {failed_list}"
        return result

    async def _query_status_for_sandbox(self, sandbox: Sandbox):
        result = await sandbox.execute(Command(command=["cat", "data/output/env-build/result.jsonl"]))
        return result

    async def _do_build_remote_one_split(self, index: int, sandbox: Sandbox, split_filename: str, **kwargs):
        status = await sandbox.get_status()
        if not status.is_alive:
            raise Exception(f"sandbox {index} [{sandbox.sandbox_id}] is not alive")
        logger.info(f"sandbox {index} [{sandbox.sandbox_id}] is alive")

        session_name = "default"
        await sandbox.create_session(CreateBashSessionRequest(session=session_name))

        await sandbox.arun(cmd="mkdir -p /root/.rock", session=session_name, mode="normal")
        home_dir = os.path.expanduser("~")
        await sandbox.upload_by_path(f"{home_dir}/.rock/config.ini", "/root/.rock/config.ini")
        await sandbox.arun(cmd="mkdir -p data", session=session_name, mode="normal")
        filename = os.path.basename(split_filename)
        target_filename = f"/tmp/{filename}"
        await sandbox.upload_by_path(split_filename, target_filename)

        await sandbox.arun(cmd="service docker start", session=session_name, mode="normal")

        tmp_session_name = "tmp-nohup"
        await sandbox.create_session(CreateBashSessionRequest(session=tmp_session_name, env_enable=True))
        command = await self.get_build_remote_one_split_command(split_filename=target_filename, **kwargs)
        logger.info(f"sandbox {index} [{sandbox.sandbox_id}] start to run command in nohup: {command}")
        result = await sandbox.arun(
            cmd=command, session=tmp_session_name, mode="nohup", wait_timeout=60 * 60 * 12, wait_interval=60
        )
        logger.info(f"sandbox {index} [{sandbox.sandbox_id}] run command in nohup result: {result}")
