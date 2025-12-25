import asyncio
import json
import logging
import os
import shlex

import yaml
from rock.sdk.sandbox.agent.swe_agent import SweAgent, SweAgentConfig
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

# 配置日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class SweConfigBuilder:
    """配置文件生成器类"""

    def __init__(self, template_path: str):
        self.template_path = template_path

    def load_json_data(self, json_file_path: str):
        """从json文件加载数据"""
        with open(json_file_path, encoding="utf-8") as f:
            data = json.load(f)
        return data

    def load_yaml_template(self, yaml_file_path: str):
        """从yaml文件加载模板"""
        with open(yaml_file_path, encoding="utf-8") as f:
            template = yaml.safe_load(f)
        return template

    def create_output_config(self, instance_data: dict, template: dict):
        """基于模板和实例数据创建一个新的配置"""
        import copy

        # 深拷贝模板确保不会修改原始数据
        new_config = copy.deepcopy(template)

        # 提取instance_id作为关键标识符
        instance_id = instance_data["instance_id"]

        # 修改output_dir为要求的格式
        new_config["output_dir"] = f"/tmp_sweagent/{instance_id}"

        # 从image_info字段解析出额外信息
        image_info_str = instance_data.get("image_info", "{}")
        image_info = json.loads(image_info_str)

        # 设置项目路径信息
        project_path = image_info.get("project_path", instance_data.get("project_path"))
        new_config["env"]["repo"]["path"] = project_path

        # 如果json中有base_commit，使用它；否则保持模板中的值
        if "base_commit" in instance_data:
            new_config["env"]["repo"]["base_commit"] = instance_data["base_commit"]

        # 设置问题描述信息
        problem_statement_text = instance_data.get("problem_statement")
        new_config["problem_statement"]["text"] = problem_statement_text
        new_config["problem_statement"]["id"] = instance_id

        return new_config

    def generate_config_files(
        self,
        json_file_path: str = "test_part.json",
        output_dir: str = ".",
        skip_fields: list = None,
    ):
        """
        生成所有实例的配置文件，并返回生成的文件列表
        """
        if skip_fields is None:
            skip_fields = ["FAIL_TO_PASS", "remote_workspace_folder", "repo_name"]

        os.makedirs(output_dir, exist_ok=True)

        # 加载数据
        instances = self.load_json_data(json_file_path)
        template = self.load_yaml_template(self.template_path)

        generated_files = []

        # 遍历每个实例并创建对应的配置文件
        for instance in instances:
            instance_id = instance["instance_id"]

            # 创建新配置
            new_config = self.create_output_config(instance, template)

            # 生成输出文件名
            output_file_name = f"{instance_id}_config.yaml"
            output_file_path = os.path.join(output_dir, output_file_name)

            # 写入新配置到YAML文件
            with open(output_file_path, "w", encoding="utf-8") as f:
                yaml.dump(new_config, f, default_flow_style=False, allow_unicode=True)

            generated_files.append(output_file_path)
            logger.info(f"已生成配置文件: {output_file_path}")

        return generated_files


class SingleInstanceRunner:
    """
    SWE-agent单个实例运行器类
    """

    def __init__(
        self,
        instance_data: dict,
        instance_id: str,
        config_path: str,
        sweagent_dir: str,
    ):
        self.instance_data = instance_data

        self.instance_id = instance_id
        self.config_path = config_path
        self.sweagent_dir = sweagent_dir
        # 获取额外的配置信息，无默认值，没有则报错
        image_info_str = self.instance_data["image_info"]  # 无默认值，不存在则报错
        image_info = json.loads(image_info_str)
        self.project_path = image_info["project_path"]  # 无默认值，不存在则报错
        self.remote_user = image_info["remote_user"]  # 无默认值，不存在则报错
        self.script_folder = image_info["script_folder"]  # 无默认值，不存在则报错
        self.image_name = self.instance_data["image_name"]  # 无默认值，不存在则报错

        # 初始化session和agent文件路径
        self.swe_session = "swe-agent"
        self.target_path = f"{self.sweagent_dir}/{self.instance_id}_config.yaml"

        # 需要下载的文件列表提前列好
        self.agent_file_path = (
            f"{self.sweagent_dir}/{self.instance_id}/{self.instance_id}"
        )
        self.log_file_path = f"{self.agent_file_path}/{self.instance_id}.trace.log"
        self.patch_file_path = f"{self.agent_file_path}/{self.instance_id}.patch"
        self.pred_file_path = f"{self.agent_file_path}/{self.instance_id}.pred"
        self.traj_file_path = f"{self.agent_file_path}/{self.instance_id}.traj"

    def wrap_bash_cmd(
        self, cmd: str, remote_user: str = "root", script_folder: str = "/root"
    ) -> str:
        return f"bash -c {shlex.quote(cmd)}"

    async def run_single_instance(self):
        """
        为单个实例运行swe-agent流程
        """
        logger.info(f"\n{'=' * 60}")
        logger.info(f"开始处理实例: {self.instance_id}")
        logger.info(f"{'=' * 60}")

        try:
            # 创建沙箱
            logger.info(
                f"[{self.instance_id}] Creating sandbox with image: {self.image_name}"
            )
            config = SandboxConfig(
                image=self.image_name, base_url="http://localhost:8080"
            )
            sandbox = Sandbox(config)
            await sandbox.start()

            swe_agent_config = SweAgentConfig(
                agent_type="swe-agent",
                version="unknown",
                swe_agent_workdir=self.sweagent_dir,
                swe_session=self.swe_session,
            )

            sandbox.agent = SweAgent(sandbox, swe_agent_config)

            await sandbox.agent.init()

            await sandbox.agent.run(swe_agent_config_path=self.config_path)

            await self.download_analysis_files(sandbox)

            await self.calculate_reward(sandbox)

        except Exception as e:
            logger.error(f"[{self.instance_id}] Error: {str(e)}")
            return {
                "instance_id": self.instance_id,
                "exit_code": -1,
                "test_result": "ERROR",
                "error": str(e),
            }

        finally:
            # 停止沙箱
            if sandbox:
                try:
                    await sandbox.stop()
                    logger.info(f"[{self.instance_id}] Sandbox stopped")
                except Exception as e:
                    logger.error(
                        f"[{self.instance_id}] Error stopping sandbox: {str(e)}"
                    )

    async def download_analysis_files(self, sandbox: Sandbox):
        """
        下载分析文件
        """
        # 为当前实例创建输出目录
        output_instance_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "tmp_data",
            self.instance_id,
        )
        os.makedirs(output_instance_dir, exist_ok=True)

        # 下载需要的所有文件
        files_to_download = [
            (
                self.log_file_path,
                os.path.join(output_instance_dir, f"{self.instance_id}.trace.log"),
            ),
            (
                self.patch_file_path,
                os.path.join(output_instance_dir, f"{self.instance_id}.patch"),
            ),
            (
                self.pred_file_path,
                os.path.join(output_instance_dir, f"{self.instance_id}.pred"),
            ),
            (
                self.traj_file_path,
                os.path.join(output_instance_dir, f"{self.instance_id}.traj"),
            ),
        ]

        for remote_path, local_path in files_to_download:
            try:
                logger.info(
                    f"[{self.instance_id}] Downloading {remote_path} to {local_path}"
                )
                file_content = await sandbox.read_file_by_line_range(
                    file_path=remote_path, lines_per_request=10000
                )

                # 确保本地目录存在
                local_dir = os.path.dirname(local_path)
                os.makedirs(local_dir, exist_ok=True)

                # 将内容写入本地文件
                with open(local_path, "w", encoding="utf-8") as f:
                    f.write(file_content.content)

                logger.info(
                    f"[{self.instance_id}] Successfully downloaded {remote_path} to {local_path}"
                )
            except Exception as e:
                logger.error(
                    f"[{self.instance_id}] Failed to download {remote_path}: {str(e)}"
                )
                # 即使下载失败也继续处理下一个文件

    async def calculate_reward(self, sandbox: Sandbox):
        """
        计算奖励
        """
        logger.info(f"[{self.instance_id}] Calculating reward...")

        #  应用 patch
        logger.info(f"[{self.instance_id}] Step 7: Applying patch...")
        await sandbox.arun(
            cmd=self.wrap_bash_cmd(
                f"cd {self.project_path} && git apply {self.patch_file_path}",
                remote_user=self.remote_user,
                script_folder=self.script_folder,
            ),
            session=self.swe_session,
        )

        #  运行测试脚本
        logger.info(f"[{self.instance_id}] Step 8: Applying test patch...")
        await sandbox.arun(
            cmd=self.wrap_bash_cmd(
                f"cd {self.script_folder} && bash apply_test_patch.sh",
                remote_user=self.remote_user,
                script_folder=self.script_folder,
            ),
            session=self.swe_session,
            mode="nohup",
            wait_timeout=120,
        )

        #  运行测试
        logger.info(f"[{self.instance_id}] Step 9: Running tests...")
        test_result = await sandbox.arun(
            cmd=self.wrap_bash_cmd(
                f"cd {self.script_folder} && bash run.sh",
                remote_user=self.remote_user,
                script_folder=self.script_folder,
            ),
            session=self.swe_session,
            mode="nohup",
            wait_timeout=600,
        )

        logger.info(f"\n{'=' * 60}")
        logger.info(f"Test Result for {self.instance_id}")
        logger.info(f"{'=' * 60}")
        logger.info(f"Exit code: {test_result.exit_code}")
        logger.info(test_result.output)
        logger.info(f"{'=' * 60}\n")

        # 检查测试是否通过
        if "all test cases run successfully." in test_result.output.lower():
            logger.info(f"[{self.instance_id}] ✅ Tests PASSED!")
        else:
            logger.info(f"[{self.instance_id}] ❌ Tests FAILED")

        logger.info(f"[{self.instance_id}] Completed")


async def run_parallel_instances(
    input_file_path: str,
    config_dir: str,
    max_concurrent=3,
    sweagent_dir="/tmp_sweagent",
):
    # 使用SWEInstanceRunner类
    """
    并行运行所有实例
    """
    logger.info("Loading instances from JSON...")

    with open(input_file_path, encoding="utf-8") as f:
        instances = json.load(f)

    logger.info(f"Found {len(instances)} instances to process")

    # 使用信号量来限制并发数量
    semaphore = asyncio.Semaphore(max_concurrent)

    async def run_with_semaphore(instance_data, instance_id, config_path):
        async with semaphore:
            single_runner = SingleInstanceRunner(
                instance_data,
                instance_id,
                config_path,
                sweagent_dir,
            )
            return await single_runner.run_single_instance()

    tasks = []
    for instance_data in instances:
        instance_id = instance_data["instance_id"]

        # 找到相应的配置文件
        config_path = os.path.join(config_dir, f"{instance_id}_config.yaml")

        if not os.path.exists(config_path):
            logger.warning(
                f"Warning: Config file {config_path} not found for instance {instance_id}"
            )
            continue

        logger.info(
            f"Creating task for instance: {instance_id} with config: {config_path}"
        )
        task = asyncio.create_task(
            run_with_semaphore(instance_data, instance_id, config_path)
        )
        tasks.append(task)

    logger.info(f"\nStarting up to {max_concurrent} concurrent tasks...")
    results = await asyncio.gather(*tasks, return_exceptions=True)

    return results


async def generate_config(
    input_data_file: str, output_dir: str, example_config_file: str
):
    # 根据example.yaml在output_dir生产input_data_file包含的数据文件
    config_builder = SweConfigBuilder(template_path=example_config_file)

    config_builder.generate_config_files(
        json_file_path=input_data_file, output_dir=output_dir
    )


async def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))

    input_data_file = os.path.join(script_dir, "swe_agent_data", "test_data.json")

    output_dir = os.path.join(script_dir, "swe_agent_data", "temp_config_dir")

    default_config_file = os.path.join(
        script_dir, "swe_agent_data", "default_config.yaml"
    )

    max_concurrent = 1

    # 用于写明在Sandbox里的哪个路径里具体创建维护sweagent依赖的python和sweagent等依赖
    sweagent_dir = "/tmp_sweagent"

    # 参考example_config_file.yaml在output_dir生产input_data_file包含的数据文件
    await generate_config(
        input_data_file=input_data_file,
        output_dir=output_dir,
        example_config_file=default_config_file,
    )

    await run_parallel_instances(
        input_file_path=input_data_file,
        config_dir=output_dir,
        max_concurrent=max_concurrent,
        sweagent_dir=sweagent_dir,
    )


if __name__ == "__main__":
    # Ensure admin server is running before executing
    print(
        "IMPORTANT: Make sure the admin server is running before executing this demo!"
    )
    print("Start the admin server with: rock admin start")
    asyncio.run(main())
