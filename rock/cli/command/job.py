import argparse
from pathlib import Path

from rock.cli.command.command import Command
from rock.logger import init_logger
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

logger = init_logger(__name__)


class JobCommand(Command):
    name = "job"

    async def arun(self, args: argparse.Namespace):
        if args.job_command == "run":
            await self._job_run(args)
        else:
            logger.error(f"Unknown job subcommand: {args.job_command}")

    async def _job_run(self, args: argparse.Namespace):
        # 1. Validate script source
        if args.script and args.script_content:
            logger.error("--script and --script-content cannot be used together")
            return
        if not args.script and not args.script_content:
            logger.error("Either --script or --script-content is required")
            return

        if args.script:
            script_path = Path(args.script).resolve()
            if not script_path.exists():
                logger.error(f"Script not found: {script_path}")
                return
            if not script_path.is_file():
                logger.error(f"Not a file: {script_path}")
                return
            script_content = script_path.read_text()
        else:
            script_content = args.script_content

        # 2. Validate local path (optional)
        src_dir = None
        target_path = args.target_path
        if args.local_path:
            local_path = Path(args.local_path).resolve()
            if not local_path.exists():
                logger.error(f"Local path not found: {local_path}")
                return
            src_dir = str(local_path)

        # 3. Build sandbox config
        sandbox_config = SandboxConfig()
        if args.image:
            sandbox_config.image = args.image
        if args.memory:
            sandbox_config.memory = args.memory
        if args.cpus:
            sandbox_config.cpus = args.cpus
        if args.base_url:
            sandbox_config.base_url = args.base_url
        if args.cluster:
            sandbox_config.cluster = args.cluster
        if args.extra_headers:
            sandbox_config.extra_headers.update(args.extra_headers)

        sandbox = Sandbox(sandbox_config)

        try:
            # 4. Start sandbox
            logger.info(f"Starting sandbox with image={sandbox_config.image} ...")
            await sandbox.start()
            logger.info(f"Sandbox started: id={sandbox.sandbox_id}, ip={sandbox.host_ip}")

            # 5. Copy source directory to sandbox (optional)
            if src_dir is not None:
                assert sandbox.fs is not None
                logger.info(f"Uploading {src_dir} -> {target_path}")
                result = await sandbox.fs.upload_dir(source_dir=src_dir, target_dir=target_path)
                if result.exit_code != 0:
                    logger.error(f"Upload failed: {result.failure_reason}")
                    return

            # 6. Execute the script
            assert sandbox.process is not None
            logger.info(f"Executing script: {script_content}")
            result = await sandbox.process.execute_script(
                script_content=script_content,
                wait_timeout=args.timeout,
            )

            # 7. Print output
            if result.output:
                print(result.output)
            logger.info(f"Script exited with code: {result.exit_code}")

        finally:
            logger.info("Stopping sandbox ...")
            await sandbox.stop()

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction):
        job_parser = subparsers.add_parser("job", help="Manage sandbox jobs")
        job_subparsers = job_parser.add_subparsers(dest="job_command")

        # run subcommand
        run_parser = job_subparsers.add_parser("run", help="Run a job in a sandbox")
        run_parser.add_argument("--image", default=None, help="Sandbox image (overrides default)")
        run_parser.add_argument("--memory", default=None, help="Memory allocation (e.g., 8g)")
        run_parser.add_argument("--cpus", default=None, type=float, help="CPU allocation (e.g., 2)")

        run_parser.add_argument("--local-path", default=None, help="Local directory to upload to the sandbox")
        run_parser.add_argument(
            "--target-path", default="/root/job", help="Target directory in sandbox (default: /root/job)"
        )
        run_parser.add_argument("--script", default=None, help="Path to the script to execute in the sandbox")
        run_parser.add_argument("--script-content", default=None, help="Script content to execute directly")

        run_parser.add_argument(
            "--timeout", type=int, default=3600, help="Script execution timeout in seconds (default: 3600)"
        )
