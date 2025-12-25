"""
Solving software engineering(SWE) problem with [Openhands SDK](https://github.com/OpenHands/software-agent-sdk).
Implementation framework reference: `rock/sdk/sandbox/agent/swe_agent.py`

This code is composed with following parts:
    1. Install openhands-sdk into Sandbox
    2. Modify launch entry code which is modified from [run_infer.py](https://github.com/OpenHands/benchmarks/blob/main/benchmarks/swebench/run_infer.py)
        into Sandbox
    3. Upload LLM service configuration into Sandbox
    4. Execute launch entry file
"""
from __future__ import annotations

import json
import os
import time
import shlex
import asyncio
from pathlib import Path
from httpx import ReadTimeout
from contextlib import contextmanager

from rock import env_vars
from rock.logger import init_logger
from rock.actions import (
    Command,
    CreateBashSessionRequest,
    UploadRequest,
    Observation,
    WriteFileRequest
)
from rock.sdk.sandbox.agent.base import Agent
from rock.sdk.sandbox.agent.config import AgentConfig
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.utils import arun_with_retry
from rock.sdk.sandbox.model_service.base import ModelService, ModelServiceConfig

from typing import Any, Literal

logger = init_logger(__name__)

# default prompt template in openhands
DEFAULT_PROMPT = """I have access to a python code repository in the directory {{ instance.repo_path }} . You can explore and modify files using the available tools. Consider the following issue description:

<issue_description>
{{ instance.problem_statement }}
</issue_description>

Can you help me implement the necessary changes to the repository so that the requirements specified in the <issue_description> are met?
I've already taken care of all changes to any of the test files described in the <issue_description>. This means you DON'T have to modify the testing logic or any of the tests in any way!
Also the development Python environment is already set up for you (i.e., all dependencies already installed), so you don't need to install other packages.
Your task is to make the minimal changes to non-test files in the {{ instance.repo_path }} directory to ensure the <issue_description> is satisfied.

Follow these phases to resolve the issue:

Phase 1. READING: read the problem and reword it in clearer terms
   1.1 If there are code or config snippets. Express in words any best practices or conventions in them.
   1.2 Hightlight message errors, method names, variables, file names, stack traces, and technical details.
   1.3 Explain the problem in clear terms.
   1.4 Enumerate the steps to reproduce the problem.
   1.5 Hightlight any best practices to take into account when testing and fixing the issue

Phase 2. RUNNING: install and run the tests on the repository
   2.1 Activate the environment by running  
       ./opt/miniconda3/etc/profile.d/conda.sh ; conda activate testbed
   2.2 Follow the readme
   2.3 Install the environment and anything needed
   2.4 Iterate and figure out how to run the tests

Phase 3. EXPLORATION: find the files that are related to the problem and possible solutions
   3.1 Use `grep` to search for relevant methods, classes, keywords and error messages.
   3.2 Identify all files related to the problem statement.
   3.3 Propose the methods and files to fix the issue and explain why.
   3.4 From the possible file locations, select the most likely location to fix the issue.

Phase 4. TEST CREATION: before implementing any fix, create a script to reproduce and verify the issue.
   4.1 Look at existing test files in the repository to understand the test format/structure.
   4.2 Create a minimal reproduction script that reproduces the located issue.
   4.3 Run the reproduction script to confirm you are reproducing the issue.
   4.4 Adjust the reproduction script as necessary.

Phase 5. FIX ANALYSIS: state clearly the problem and how to fix it
   5.1 State clearly what the problem is.
   5.2 State clearly where the problem is located.
   5.3 State clearly how the test reproduces the issue.
   5.4 State clearly the best practices to take into account in the fix.
   5.5 State clearly how to fix the problem.

Phase 6. FIX IMPLEMENTATION: Edit the source code to implement your chosen solution.
   6.1 Make minimal, focused changes to fix the issue.

Phase 7. VERIFICATION: Test your implementation thoroughly.
   7.1 Run your reproduction script to verify the fix works.
   7.2 Add edge cases to your test script to ensure comprehensive coverage.
   7.3 Run existing tests related to the modified code to ensure you haven't broken anything.

8. FINAL REVIEW: Carefully re-read the problem description and compare your changes with the base commit {{ instance.base_commit }}.
   8.1 Ensure you've fully addressed all requirements.
   8.2 Run any tests in the repository related to:
     8.2.1 The issue you are fixing
     8.2.2 The files you modified
     8.2.3 The functions you changed
   8.3 If any tests fail, revise your implementation until all tests pass

Be thorough in your exploration, testing, and reasoning. It's fine if your thinking process is lengthy - quality and completeness are more important than brevity.
"""

DEFAULT_RUN_SINGLE_CONFIG = {
    # instance
    "instance": {
        "instance_id": "",
        "problem_statement": "",
        "prompt_path": ""
    },
    # model service
    "llm": {
        "model": "",
        "base_url": "",
        "api_key": "",
        "num_retries": 100,
        "retry_multiplier": 8.0,
        "retry_min_wait": 8,
        "retry_max_wait": 64,
        "timeout": None,
        "max_message_chars": 1000000,
        "temperature": 0.3,
        "top_p": 0.75,
        "top_k": 100,
        "custom_llm_provider": None,
        "max_input_tokens": 134144,
        "max_output_tokens": 134144,
        "extra_headers": None,
        "stream": False,
        "caching_prompt": True,
        "log_completions": True,
        "log_completions_folder": "logs/completions",
        "custom_tokenizer": None,
        "native_tool_calling": True,
        "extended_thinking_budget": 200000,
    }
}

DEFAULT_PYTHON_DOWNLOAD_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/20251217/cpython-3.12.12+20251217-x86_64-unknown-linux-gnu-install_only.tar.gz"

MODIFIED_INFER_PATCH = '''diff --git a/benchmarks/swebench/run_infer.py b/benchmarks/swebench/run_infer.py
index ea528b8..a936f37 100644
--- a/benchmarks/swebench/run_infer.py
+++ b/benchmarks/swebench/run_infer.py
@@ -1,39 +1,69 @@
 import os
+import json
+import logging
 from pathlib import Path
 from typing import List
 
 from jinja2 import Environment, FileSystemLoader
 
-from benchmarks.swebench.build_images import (
-    extract_custom_tag,
-    get_official_docker_image,
-)
 from benchmarks.utils.args_parser import get_parser
-from benchmarks.utils.build_utils import build_image
-from benchmarks.utils.constants import EVAL_AGENT_SERVER_IMAGE
 from benchmarks.utils.critics import create_critic
-from benchmarks.utils.dataset import get_dataset
 from benchmarks.utils.evaluation import Evaluation
 from benchmarks.utils.evaluation_utils import (
     construct_eval_output_dir,
     get_default_on_result_writer,
 )
-from benchmarks.utils.image_utils import image_exists
 from benchmarks.utils.models import (
     EvalInstance,
     EvalMetadata,
     EvalOutput,
 )
-from benchmarks.utils.version import SDK_SHORT_SHA
 from openhands.sdk import LLM, Agent, Conversation, get_logger
-from openhands.sdk.workspace import RemoteWorkspace
+from openhands.sdk.workspace import RemoteWorkspace, LocalWorkspace
 from openhands.tools.preset.default import get_default_tools
-from openhands.workspace import APIRemoteWorkspace, DockerWorkspace
-
 
+logging.basicConfig(
+    level=logging.DEBUG,
+    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
+)
 logger = get_logger(__name__)
 
 
+def make_instance(inst_file: str) -> EvalInstance:
+    required_keys = (
+        "instance_id", "image_name", "problem_statement",
+        "repo_name", "base_commit", "remote_user", "project_path",
+        "script_folder", "remote_workspace_folder", "FAIL_TO_PASS"
+    )
+
+    with open(inst_file, "r") as f:
+        curr_instance: dict = json.loads(f.read())
+        for key in required_keys:
+            if key not in curr_instance:
+                logger.warning(f"{key} is missing...")
+
+        instance = EvalInstance(
+            id=curr_instance["instance_id"],
+            data={
+                "repo": curr_instance["repo_name"],
+                "project_path": curr_instance["project_path"].rstrip("/"),  # repo data store path
+                "problem_statement": curr_instance["problem_statement"],
+
+                "base_commit": curr_instance.get("base_commit", ""),
+                "image_name": curr_instance.get("image_name", ""),
+                "FAIL_TO_PASS": curr_instance.get("FAIL_TO_PASS", ""),
+                "remote_user": curr_instance.get("remote_user", ""),
+                "repo_path": f"/workspace/{curr_instance['repo_name'].split('/')[-1]}",  # Agent work place
+                "script_folder": curr_instance.get("script_folder", ""),  # test script directory
+            }
+        )
+
+        if not os.path.exists(instance.data["repo_path"]):
+            os.makedirs(instance.data["repo_path"])
+
+    return instance
+
+
 def get_instruction(
     instance: dict,
     metadata: EvalMetadata,
@@ -78,17 +108,7 @@ class SWEBenchEvaluation(Evaluation):
     def prepare_instances(self) -> List[EvalInstance]:
         logger.info("Setting up SWE-bench evaluation data")
 
-        df = get_dataset(
-            dataset_name=self.metadata.dataset,
-            split=self.metadata.dataset_split,
-            eval_limit=self.metadata.eval_limit,
-            selected_instances_file=self.metadata.selected_instances_file,
-        )
-
-        instances: List[EvalInstance] = []
-        for _, row in df.iterrows():
-            inst_id = str(row["instance_id"])
-            instances.append(EvalInstance(id=inst_id, data=row.to_dict()))
+        instances: List[EvalInstance] = [make_instance(self.metadata.selected_instances_file)]
 
         logger.info("Total instances to process: %d", len(instances))
         return instances
@@ -98,73 +118,8 @@ class SWEBenchEvaluation(Evaluation):
         """
         Use DockerWorkspace by default.
         """
-        official_docker_image = get_official_docker_image(instance.id)
-        build_target = "source-minimal"
-        custom_tag = extract_custom_tag(official_docker_image)
-        # For non-binary targets, append target suffix
-        suffix = f"-{build_target}" if build_target != "binary" else ""
-
         if self.metadata.workspace_type == "docker":
-            agent_server_image = (
-                f"{EVAL_AGENT_SERVER_IMAGE}:{SDK_SHORT_SHA}-{custom_tag}{suffix}"
-            )
-            SKIP_BUILD = os.getenv("SKIP_BUILD", "1").lower() in ("1", "true", "yes")
-            logger.info(f"SKIP_BUILD={SKIP_BUILD}")
-            if not SKIP_BUILD:
-                logger.info(
-                    f"Building workspace from {official_docker_image} "
-                    f"for instance {instance.id}. "
-                    "This may take a while...\n"
-                    "You can run benchmarks/swebench/build_images.py and set "
-                    "SWE_BENCH_SKIP_BUILD=1 to skip building and use pre-built "
-                    "agent-server image."
-                )
-                output = build_image(
-                    base_image=official_docker_image,
-                    target_image=EVAL_AGENT_SERVER_IMAGE,
-                    custom_tag=custom_tag,
-                    target=build_target,
-                    push=False,
-                )
-                logger.info(f"Image build output: {output}")
-                assert output.error is None, f"Image build failed: {output.error}"
-                if agent_server_image not in output.tags:
-                    raise RuntimeError(
-                        f"Built image tags {output.tags} do not include expected tag "
-                        f"{agent_server_image}"
-                    )
-
-            workspace = DockerWorkspace(
-                server_image=agent_server_image,
-                working_dir="/workspace",
-            )
-        elif self.metadata.workspace_type == "remote":
-            runtime_api_key = os.getenv("RUNTIME_API_KEY")
-            sdk_short_sha = os.getenv("SDK_SHORT_SHA", SDK_SHORT_SHA)
-            if not runtime_api_key:
-                raise ValueError(
-                    "RUNTIME_API_KEY environment variable is not set for remote workspace"
-                )
-
-            agent_server_image = (
-                f"{EVAL_AGENT_SERVER_IMAGE}:{sdk_short_sha}-{custom_tag}{suffix}"
-            )
-            if not image_exists(agent_server_image):
-                raise RuntimeError(
-                    f"Agent server image {agent_server_image} does not exist in container registry, "
-                    "make sure to build, push it, and make it public accessible before using remote workspace."
-                )
-            logger.info(
-                f"Using remote workspace with image {agent_server_image} (sdk sha: {sdk_short_sha})"
-            )
-            workspace = APIRemoteWorkspace(
-                runtime_api_url=os.getenv(
-                    "RUNTIME_API_URL", "https://runtime.eval.all-hands.dev"
-                ),
-                runtime_api_key=runtime_api_key,
-                server_image=agent_server_image,
-                target_type="source" if "source" in build_target else "binary",
-            )
+            workspace = LocalWorkspace(working_dir=instance.data["repo_path"])
         else:
             raise ValueError(
                 f"Unsupported workspace_type: {self.metadata.workspace_type}"
@@ -203,14 +158,9 @@ class SWEBenchEvaluation(Evaluation):
             # security_analyzer=LLMSecurityAnalyzer(),
         )
 
-        assert isinstance(workspace, RemoteWorkspace)
-
         def _log_event(ev):  # keep it simple
             logger.debug("Event: %s", ev)
 
-        repo_path = f"/workspace/{instance.data['repo'].split('/')[-1]}/"
-        instance.data["repo_path"] = repo_path
-
         conversation = Conversation(
             agent=agent,
             workspace=workspace,
@@ -218,13 +168,19 @@ class SWEBenchEvaluation(Evaluation):
             max_iteration_per_run=self.metadata.max_iterations,
         )
 
-        logger.info("repo_path: %s", repo_path)
-        cp_testebed_repo = workspace.execute_command(
-            (f"mkdir -p {repo_path} ; cp -r /testbed/. {repo_path}")
-        )
-        assert cp_testebed_repo.exit_code == 0, (
-            f"cp_testebed_repo failed: {cp_testebed_repo.stderr}"
-        )
+        repo_path = instance.data["repo_path"]
+        proj_path = instance.data["project_path"]
+        if proj_path != repo_path:
+            cp_repo = workspace.execute_command(
+                f"mkdir -p {repo_path} ; rm -rf {repo_path}/* ; cp -r {proj_path}/. {repo_path}"
+            )
+            assert cp_repo.exit_code == 0, (
+                f"cp_repo failed: {cp_repo.stderr}"
+            )
+        if not instance.data["base_commit"]:
+            hash_res = workspace.execute_command(f"cd {proj_path} && git rev-parse HEAD")
+            assert hash_res.exit_code == 0
+            instance.data["base_commit"] = hash_res.stdout.strip()
 
         # git reset
         git_reset = workspace.execute_command(f"cd {repo_path} ; git reset --hard")
'''


class OpenhandsConfig(AgentConfig):
    """Configuration dataclass for Openhands initialization and execution.

    This class defines all configurable parameters for setting up and running
    Openhands in a sandboxed environment, including installation commands,
    working directories, and execution timeouts.

    Attributes:
        agent_type: Fixed identifier for this agent type ("openhands")
        default_run_single_config: Default configuration object for a single run
        agent_session: Name of the bash session used for SWE-agent execution
        pre_startup_bash_cmd_list: Commands executed before agent initialization
        post_startup_bash_cmd_list: Commands executed after agent initialization
        agent_workdir: Working directory for agent installation and execution
        python_install_cmd: Command to install Python environment
        openhands_sdk_install_cmd_list: Commands to clone and install Openhands/benchmarks repository
        python_install_timeout: Maximum seconds to wait for Python installation
        agent_install_timeout: Maximum seconds to wait for SWE-agent installation
        model_service_config: Configuration for ModelService (optional)
    """

    agent_type: Literal["openhands"] = "openhands"

    agent_session: str = "openhands-rollout-session"

    agent_prompt: str = DEFAULT_PROMPT

    # Commands to execute before agent initialization (e.g., bashrc setup, hosts config)
    pre_startup_bash_cmd_list: list[str] = env_vars.ROCK_AGENT_PRE_STARTUP_BASH_CMD_LIST

    # Commands to execute after agent initialization
    post_startup_bash_cmd_list: list[str] = []

    # Working directory where SWE-agent will be installed and executed
    agent_workdir: str = "/openhands"

    # Command to download and set up Python environment
    python_install_cmd: str = (
        "[ -f cpython-3.12.12.tar.gz ] && rm cpython-3.12.12.tar.gz; [ -d python ] && rm -rf python; "
        f"wget -q -O cpython-3.12.12.tar.gz {DEFAULT_PYTHON_DOWNLOAD_URL} && tar -xzf cpython-3.12.12.tar.gz"
    )

    # Command to clone Openhands/benchmarks repository and install dependencies
    openhands_sdk_install_cmd_list: list[str] = [
        f"/openhands/python/bin/pip config set {env_vars.ROCK_PIP_INDEX_URL}",
        "/openhands/python/bin/pip install openhands-agent-server==1.6.0 openhands-sdk==1.6.0",
        "/openhands/python/bin/pip openhands-tools==1.6.0 openhands-workspace==1.6.0"
        "git clone https://github.com/OpenHands/benchmarks.git /openhands/benchmarks",
        "git -C /openhands/benchmarks checkout c67349f4ce9bd5e72b394cfb5be91d8f33fe229c",
        "/openhands/python/bin/pip install datasets huggingface-hub jinja2 pandas Pillow toml swebench",
        "/openhands/python/bin/pip install tqdm 'unidiff>=0.7.5,<0.8.0' 'modal>=1.1.4' commit0 pytest-json-report"
    ]

    python_install_timeout: int = 300

    agent_install_timeout: int = 600

    default_run_single_config: dict[str, Any] = DEFAULT_RUN_SINGLE_CONFIG

    session_envs: dict[str, str] = {}

    model_service_config: ModelServiceConfig | None = None


class Openhands(Agent):
    """
    Openhands implementation for automated software engineering tasks.

    This class handles the installation of [Openhands/benchmarks](https://github.com/OpenHands/benchmarks.git) library
    and modifies file [run_infer.py](https://github.com/OpenHands/benchmarks/tree/main/benchmarks/swebench/run_infer.py)
    to be compatible with SWE tasks other than SWE-Bench. It orchestrates rollout generation, patch retrieval,
    and result validation.

    Attributes:
        config: Configuration parameters for agent setup and execution
        agent_session: Name of the bash session used for agent operations
        model_service: ModelService instance (created if configured)
    """

    def __init__(self, sandbox: Sandbox, config: OpenhandsConfig):
        """Initialize Agent with sandbox environment and configuration.

        Args:
            sandbox: Sandbox instance for isolated agent execution
            config: Configuration parameters for agent setup

        Raises:
            AssertionError: If sandbox is not an instance of Sandbox class
        """
        super().__init__(sandbox)
        self._sandbox = sandbox
        self.config = config
        self.agent_session = self.config.agent_session

        self.agent_prompt_path = f"{self.config.agent_workdir}/benchmarks/benchmarks/swebench/prompts/custom.j2"

        # ModelService instance (created during init if configured)
        self.model_service: ModelService | None = None

    async def init(self):
        """Initialize the Openhands/benchmarks environment within the sandbox.

        Performs the following initialization steps in sequence:
        1. Creates a dedicated bash session for agent execution
        2. Executes pre-startup configuration commands
        3. Creates working directory for agent installation
        4. Installs Python environment
        5. Clones and installs SWE-agent
        6. Initializes ModelService if configured (parallel with step 5)

        The initialization process is asynchronous and uses the configured
        timeouts for long-running operations like dependency installation.

        Raises:
            Exception: If any initialization step fails
        """

        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        # Prepare tasks to run in parallel
        tasks = [self._install_agent_repo()]

        # Initialize ModelService if configured
        if self.config.model_service_config:
            tasks.append(self._init_model_service())

        # Run tasks in parallel
        await asyncio.gather(*tasks)

        # Prepare configs and apply patch to RUN ENTER FILE
        await self._hijack_agent_repo()

        elapsed = time.time() - start_time
        logger.info(f"[{sandbox_id}] Openhands init completed (elapsed: {elapsed:.2f}s)")

    async def _install_agent_repo(self):
        """Install Openhands/benchmarks and configure the environment."""

        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Starting Openhands initialization")

        try:
            # Step 1: Create dedicated bash session
            step_start = time.time()
            logger.debug(f"[{sandbox_id}] Creating bash session: {self.agent_session}")
            await self._sandbox.create_session(
                CreateBashSessionRequest(
                    session=self.agent_session,
                    env_enable=True,
                    env=self.config.session_envs,
                )
            )
            elapsed_step = time.time() - step_start
            logger.info(f"[{sandbox_id}] Step 1 completed: Bash session created (elapsed: {elapsed_step:.2f}s)")

            # Step 2: Execute pre-startup commands
            step_start = time.time()
            for cmd in self.config.pre_startup_bash_cmd_list:
                await self._sandbox.arun(
                    cmd=cmd,
                    session=self.agent_session,
                )
            elapsed_step = time.time() - step_start
            logger.info(
                f"[{sandbox_id}] Step 2 completed: Pre-startup commands executed (elapsed: {elapsed_step:.2f}s)"
            )

            # Step 3: Create working directory
            step_start = time.time()
            mkdir_cmd = f"mkdir -p {self.config.agent_workdir}"
            logger.debug(f"[{sandbox_id}] Command: {mkdir_cmd}")
            await self._sandbox.arun(
                cmd=mkdir_cmd,
                session=self.agent_session,
            )
            elapsed_step = time.time() - step_start
            logger.info(f"[{sandbox_id}] Step 3 completed: Working directory created (elapsed: {elapsed_step:.2f}s)")

            # Step 4: Install Python
            step_start = time.time()
            python_install_cmd = f"cd {self.config.agent_workdir} && {self.config.python_install_cmd}"
            full_cmd = f"bash -c {shlex.quote(python_install_cmd)}"
            logger.debug(f"[{sandbox_id}] Command: {full_cmd}")

            await arun_with_retry(
                sandbox=self._sandbox,
                cmd=full_cmd,
                session=self.agent_session,
                mode="nohup",
                wait_timeout=self.config.python_install_timeout,
                error_msg="Python installation failed",
            )
            elapsed_step = time.time() - step_start
            logger.info(f"[{sandbox_id}] Step 4 completed: Python environment installed (elapsed: {elapsed_step:.2f}s)")

            # Step 5: Install Openhands/benchmarks and checkout commit
            step_start = time.time()
            full_cmd = f"bash -c {shlex.quote(' && '.join(self.config.openhands_sdk_install_cmd_list))}"
            logger.debug(f"[{sandbox_id}] Command: {full_cmd}")

            await arun_with_retry(
                sandbox=self._sandbox,
                cmd=full_cmd,
                session=self.agent_session,
                mode="nohup",
                wait_timeout=self.config.swe_agent_install_timeout,
                error_msg="Openhands/benchmarks sdk installation failed",
            )
            elapsed_step = time.time() - step_start
            logger.info(
                f"[{sandbox_id}] Step 5 completed: Openhands/benchmarks repository installed (elapsed: {elapsed_step:.2f}s)"
            )

        except Exception as e:
            elapsed_total = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] Operation failed: SWE-agent installation failed - {str(e)} "
                f"(elapsed: {elapsed_total:.2f}s)",
                exc_info=True,
            )
            raise

    async def _init_model_service(self):
        """Initialize ModelService (install only, not start).

        Creates a ModelService instance and executes the installation steps.
        The service will be started later in run() method if needed.

        Raises:
            Exception: If ModelService initialization fails
        """
        sandbox_id = self._sandbox.sandbox_id

        try:
            logger.info(f"[{sandbox_id}] Initializing ModelService")

            # Create ModelService instance
            self.model_service = ModelService(
                sandbox=self._sandbox,
                config=self.config.model_service_config,
            )

            # Execute install (this prepares the environment but doesn't start the service)
            await self.model_service.install()

            logger.info(f"[{sandbox_id}] ModelService initialized successfully")

        except Exception as e:
            logger.error(f"[{sandbox_id}] ModelService initialization failed: {str(e)}", exc_info=True)
            raise

    async def _hijack_agent_repo(self):
        # Hijack Openhands/benchmarks
        sandbox_id = self._sandbox.sandbox_id

        config = self.config.default_run_single_config
        logger.debug(f"[{sandbox_id}] Config: {config}")

        if self.config.agent_prompt != DEFAULT_PROMPT:
            r = await self._sandbox.write_file(
                WriteFileRequest(
                    content=self.config.agent_prompt,
                    path=self.agent_prompt_path
                )
            )
            assert r.success, f"agent prompt write failed: {r.error}"
            logger.debug("agent prompt write successfully...")

        r = await self._sandbox.write_file(
            WriteFileRequest(content=config["llm"], path=f"{self.config.agent_workdir}/benchmarks/.llm_config.json")
        )
        assert r.success, f"llm configuration write failed: {r.error}"
        logger.debug("llm configuration write successfully...")

        r = await self._sandbox.write_file(
            WriteFileRequest(
                content=MODIFIED_INFER_PATCH,
                path=f"{self.config.agent_workdir}/benchmarks/modify_infer.patch"
            )
        )
        assert r.success, f"patch write failed: {r.error}"
        logger.debug("patch write successfully...")

        r = await self._sandbox.execute(
            Command(
                cmd=f"git apply modify_infer.patch",
                cwd=f"{self.config.agent_workdir}/benchmarks"
            )
        )
        assert r.exit_code == 0
        logger.debug("patch apply successfully...")

    @contextmanager
    def _config_template_context(self, problem_statement: str, project_path: str, instance_id: str):
        """Context manager for temporary config file generation and cleanup.

        Args:
            problem_statement: The problem statement for the task
            project_path: Path to the target project
            instance_id: The instance identifier for the run

        Yields:
            Path to the temporary config file
        """
        import copy
        import tempfile

        # Get the default template config from the config attribute
        template = self.config.default_run_single_config

        # Create a copy to avoid modifying the original
        new_config = copy.deepcopy(template)

        # Set output directory
        new_config["instance_id"] = instance_id
        new_config["problem_statement"] = problem_statement
        new_config["project_path"] = project_path

        # Create a temporary config file using Python's tempfile
        temp_config_file = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=f"_{instance_id}.json",
            delete=False,  # We'll manage the lifecycle through context manager
            encoding="utf-8",
        )

        temp_file_path = temp_config_file.name
        try:
            json.dump(new_config, temp_config_file, indent=4, ensure_ascii=False)
            temp_config_file.close()  # Close the file so it can be read by other processes
            yield temp_file_path
        except Exception as e:
            # In exceptional cases, if file couldn't be processed, try to clean up
            raise e
        finally:
            # Always cleanup the temporary file
            try:
                os.unlink(temp_file_path)
                logger.debug(f"✓ Cleaned up temporary config file: {temp_file_path}")
            except OSError as e:
                logger.warning(f"⚠ Could not clean up temporary config file {temp_file_path}: {e}")

    async def run(
            self,
            problem_statement: str,
            project_path: str,
            instance_id: str,
            agent_run_timeout: int = 1800,
            agent_run_check_interval: int = 30,
    ) -> Observation:
        """Execute Openhands with the specified problem statement and project path.

        This method generates a configuration file from the default template,
        uploads it to the sandbox and executes SWE-agent. If ModelService is configured,
        it will be started and watch_agent will be called to monitor the agent process.

        Args:
            problem_statement: The problem statement for the task
            project_path: Path to the target project
            instance_id: The instance identifier for the run
            agent_run_timeout: Maximum seconds to wait for agent execution completion (default 1800)
            agent_run_check_interval: Seconds between status checks during execution (default 30)

        Returns:
            Observation: Execution result containing exit code, stdout, and stderr

        Raises:
            Exception: If agent execution fails
        """
        sandbox_id = self._sandbox.sandbox_id
        start_time = time.time()

        logger.info(f"[{sandbox_id}] Openhands execution started")

        try:
            # Start ModelService if configured
            if self.model_service:
                logger.info(f"[{sandbox_id}] Starting ModelService")
                await self.model_service.start()

            with self._config_template_context(problem_statement, project_path, instance_id) as generated_config_path:
                instance_config = Path(generated_config_path).name

                step_start = time.time()
                target_path = f"{self.config.agent_workdir}/benchmarks/{instance_config}"
                logger.debug(
                    f"[{sandbox_id}] UploadRequest(source_path={os.path.abspath(generated_config_path)}, "
                    f"target_path={target_path})"
                )

                await self._sandbox.upload(
                    UploadRequest(
                        source_path=os.path.abspath(generated_config_path),
                        target_path=target_path,
                    )
                )
                elapsed_step = time.time() - step_start
                logger.info(
                    f"[{sandbox_id}] Upload completed: Configuration file uploaded (elapsed: {elapsed_step:.2f}s)"
                )

                # Execute Openhands
                step_start = time.time()
                agent_run_cmd = (
                    f"cd {self.config.agent_workdir}/benchmarks && "
                    f"{self.config.agent_workdir}/python/bin/python ./benchmarks/swebench/run_infer.py "
                    f".llm_config.json --dataset eval --split test --note rock_rollout --select ./{instance_config}"
                    f"--max-iterations 300"
                )
                if self.config.agent_prompt != DEFAULT_PROMPT:
                    agent_run_cmd += f" --prompt-path {self.agent_prompt_path}"

                full_cmd = f"bash -c {shlex.quote(agent_run_cmd)}"
                logger.debug(
                    f"[{sandbox_id}] Command: {full_cmd}\n"
                    f"Timeout: {agent_run_timeout}s, Check interval: {agent_run_check_interval}s"
                )

                result = await self._agent_run(
                    cmd=full_cmd,
                    session=self.agent_session,
                    wait_timeout=agent_run_timeout,
                    wait_interval=agent_run_check_interval,
                )
                elapsed_step = time.time() - step_start
                logger.info(f"[{sandbox_id}] Openhands execution completed (elapsed: {elapsed_step:.2f}s)")

                elapsed_total = time.time() - start_time

                if result and result.exit_code == 0:
                    logger.info(
                        f"[{sandbox_id}] Agent Run completed: Rollout execution succeeded (elapsed: {elapsed_total:.2f}s)"
                    )
                else:
                    error_msg = result.failure_reason if result else "No result returned"
                    logger.error(
                        f"[{sandbox_id}] Operation failed: Rollout execution failed - {error_msg} "
                        f"(elapsed: {elapsed_total:.2f}s)"
                    )

                return result

        except Exception as e:
            elapsed_total = time.time() - start_time
            logger.error(
                f"[{sandbox_id}] Operation failed: Rollout execution failed - {str(e)} "
                f"(elapsed: {elapsed_total:.2f}s)",
                exc_info=True,
            )
            raise
        finally:
            # Clean up ModelService if started
            if self.model_service and self.model_service.is_started:
                try:
                    logger.info(f"[{sandbox_id}] Stopping ModelService")
                    await self.model_service.stop()
                except Exception as e:
                    logger.warning(f"[{sandbox_id}] Failed to stop ModelService: {str(e)}")

    async def _agent_run(
            self,
            cmd: str,
            session: str,
            wait_timeout: int,
            wait_interval: int,
    ) -> Observation:
        """Execute agent command in nohup mode with optional ModelService watch.

        Starts the agent process and if ModelService is configured, calls watch_agent
        to monitor the process. The caller is responsible for the anti_call_llm loop
        and Whale API interactions.

        Args:
            cmd: Command to execute
            session: Bash session name
            wait_timeout: Timeout for process completion
            wait_interval: Interval for checking process status

        Returns:
            Observation: Execution result

        Raises:
            Exception: If process execution fails
        """
        sandbox_id = self._sandbox.sandbox_id

        try:
            timestamp = str(time.time_ns())
            tmp_file = f"/tmp/tmp_{timestamp}.out"

            # Start nohup process and get PID
            pid, error_response = await self._sandbox.start_nohup_process(cmd=cmd, tmp_file=tmp_file, session=session)

            if error_response is not None:
                return error_response

            # If failed to extract PID
            if pid is None:
                msg = "Failed to submit command, nohup failed to extract PID"
                return Observation(output=msg, exit_code=1, failure_reason=msg)

            logger.info(f"[{sandbox_id}] Agent process started with PID: {pid}")

            # If ModelService is configured, call watch_agent to monitor the process
            if self.model_service:
                try:
                    logger.info(f"[{sandbox_id}] Starting ModelService watch-agent for pid {pid}")
                    await self.model_service.watch_agent(pid=str(pid))
                    logger.info(f"[{sandbox_id}] ModelService watch-agent started successfully")
                except Exception as e:
                    logger.error(f"[{sandbox_id}] Failed to start watch-agent: {str(e)}", exc_info=True)
                    raise

            # Wait for agent process to complete
            logger.debug(f"[{sandbox_id}] Waiting for agent process completion (pid={pid})")
            success, message = await self._sandbox.wait_for_process_completion(
                pid=pid, session=session, wait_timeout=wait_timeout, wait_interval=wait_interval
            )

            # Handle nohup output and return result
            result = await self._sandbox.handle_nohup_output(
                tmp_file=tmp_file,
                session=session,
                success=success,
                message=message,
                ignore_output=False,
                response_limited_bytes_in_nohup=None,
            )

            return result

        except ReadTimeout:
            error_msg = f"Command execution failed due to timeout: '{cmd}'. This may be caused by an interactive command that requires user input."
            return Observation(output=error_msg, exit_code=1, failure_reason=error_msg)
        except Exception as e:
            error_msg = f"Failed to execute nohup command '{cmd}': {str(e)}"
            return Observation(output=error_msg, exit_code=1, failure_reason=error_msg)
