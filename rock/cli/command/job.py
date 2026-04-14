import argparse

from rock.cli.command.command import Command
from rock.logger import init_logger

logger = init_logger(__name__)


class JobCommand(Command):
    name = "job"

    async def arun(self, args: argparse.Namespace):
        if args.job_command == "run":
            await self._job_run(args)
        else:
            logger.error(f"Unknown job subcommand: {args.job_command}")

    async def _job_run(self, args: argparse.Namespace):
        # Import lazily to avoid pulling in bench/Harbor modules for bash-only uses
        from rock.sdk.bench.models.trial.config import RockEnvironmentConfig
        from rock.sdk.job import Job
        from rock.sdk.job.config import BashJobConfig

        job_type = args.type or "bash"

        if job_type == "bash":
            if not args.script and not args.script_content:
                logger.error("Either --script or --script-content is required for bash type")
                return
            if args.script and args.script_content:
                logger.error("--script and --script-content cannot be used together")
                return

            env_kwargs = {}
            if args.image:
                env_kwargs["image"] = args.image
            if args.memory:
                env_kwargs["memory"] = args.memory
            if args.cpus:
                env_kwargs["cpus"] = args.cpus
            if getattr(args, "base_url", None):
                env_kwargs["base_url"] = args.base_url
            if getattr(args, "cluster", None):
                env_kwargs["cluster"] = args.cluster
            if getattr(args, "extra_headers", None):
                env_kwargs["extra_headers"] = args.extra_headers

            file_uploads = []
            if args.local_path:
                file_uploads.append((args.local_path, args.target_path))

            config = BashJobConfig(
                script=args.script_content,
                script_path=args.script,
                environment=RockEnvironmentConfig(**env_kwargs),
                file_uploads=file_uploads,
                timeout=args.timeout,
                auto_stop=True,
            )

        elif job_type == "harbor":
            if not args.config:
                logger.error("--config is required for harbor type")
                return
            from rock.sdk.bench.models.job.config import HarborJobConfig

            config = HarborJobConfig.from_yaml(args.config)
            if args.image:
                config.environment.image = args.image
            config.auto_stop = True

        else:
            logger.error(f"Unknown job type: {job_type}")
            return

        try:
            result = await Job(config).run()
            if result.trial_results:
                for tr in result.trial_results:
                    output = getattr(tr, "raw_output", None) or ""
                    if output:
                        print(output)
            logger.info(f"Job completed: status={result.status}")
        except Exception as e:
            logger.error(f"Job failed: {e}")

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction):
        job_parser = subparsers.add_parser("job", help="Manage sandbox jobs")
        job_subparsers = job_parser.add_subparsers(dest="job_command")

        run_parser = job_subparsers.add_parser("run", help="Run a job in a sandbox")
        run_parser.add_argument("--type", choices=["bash", "harbor"], default="bash", help="Job type (default: bash)")
        # bash args
        run_parser.add_argument("--script", default=None, help="Path to script file")
        run_parser.add_argument("--script-content", default=None, help="Inline script content")
        # harbor args
        run_parser.add_argument("--config", default=None, help="Harbor YAML config path")
        # shared args
        run_parser.add_argument("--image", default=None, help="Sandbox image")
        run_parser.add_argument("--memory", default=None, help="Memory (e.g. 8g)")
        run_parser.add_argument("--cpus", default=None, type=float, help="CPU count")
        run_parser.add_argument("--timeout", type=int, default=3600, help="Timeout in seconds")
        run_parser.add_argument("--local-path", default=None, help="Local dir to upload")
        run_parser.add_argument("--target-path", default="/root/job", help="Target dir in sandbox")
