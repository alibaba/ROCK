import asyncio
import os
from typing import cast

from swebench.harness.constants import SWEbenchInstance
from swebench.harness.test_spec import test_spec


class DockerfileBuilder:
    @staticmethod
    async def build_swe_rebench(instance_record: dict[str, str], build_dir: str) -> str:
        swe_instance = cast(SWEbenchInstance, cast(object, instance_record))
        instance_spec: test_spec.TestSpec = await asyncio.to_thread(test_spec.make_test_spec, swe_instance)
        os.makedirs(build_dir, exist_ok=True)

        with open(f"{build_dir}/Dockerfile.base", "w") as f:
            f.write(instance_spec.base_dockerfile)

        with open(f"{build_dir}/setup_env.sh", "w") as f:
            f.write(instance_spec.setup_env_script)

        with open(f"{build_dir}/setup_repo.sh", "w") as f:
            f.write(instance_spec.install_repo_script)

        lines = instance_spec.instance_dockerfile.splitlines(keepends=True)
        dockerfile = instance_spec.env_dockerfile + "".join(lines[1:])
        with open(f"{build_dir}/Dockerfile", "w") as f:
            f.write(dockerfile)
        return dockerfile

    @staticmethod
    async def build_swe_bench(instance_record: dict[str, str], build_dir: str) -> str:
        return "dockerfile"

    @staticmethod
    async def build_terminal_bench(instance_record: dict[str, str], build_dir: str) -> str:
        os.makedirs(build_dir, exist_ok=True)
        build_dir_resolved = os.path.abspath(build_dir)
        for file_name, content in instance_record["files"].items():
            if os.path.isabs(file_name):
                raise ValueError(f"Invalid file path: {file_name}")
            file_path = os.path.abspath(os.path.join(build_dir_resolved, file_name))
            if os.path.commonpath([build_dir_resolved, file_path]) != build_dir_resolved:
                raise ValueError(f"Invalid file path: {file_name}")
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w") as f:
                f.write(content)
        return instance_record["files"]["Dockerfile"]
