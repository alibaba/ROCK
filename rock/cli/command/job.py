import argparse

from rock.cli.command.command import Command
from rock.logger import init_logger

logger = init_logger(__name__)


def _fail(parser: argparse.ArgumentParser, msg: str, *, hint: str | None = None) -> None:
    """Emit a consistent CLI error: message + optional hint + help pointer, then exit 2.

    Uses ``parser.error()`` which prints the parser's usage line to stderr, writes
    the message, and calls ``sys.exit(2)``. Never returns.
    """
    parts = [msg]
    if hint:
        parts.extend(["", hint])
    parts.extend(["", "Run `rock job run --help` for full usage."])
    parser.error("\n".join(parts))


class JobCommand(Command):
    name = "job"

    # Cached reference to the `run` sub-parser; populated by add_parser_to,
    # used by _job_run to call parser.error() consistently.
    _run_parser: argparse.ArgumentParser | None = None

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

        parser = self._run_parser

        has_config = bool(args.config)
        has_script = bool(args.script or args.script_content)

        if not has_config and not has_script:
            _fail(
                parser,
                "Missing job definition. Provide either a YAML config or inline script.",
                hint=(
                    "Examples:\n"
                    "  rock job run --config job.yaml                     # any job type, auto-detected\n"
                    "  rock job run --script path/to/run.sh               # bash, script file\n"
                    '  rock job run --script-content "echo hi"            # bash, inline snippet'
                ),
            )

        if has_config and has_script:
            _fail(
                parser,
                "--config is mutually exclusive with --script / --script-content.",
                hint=(
                    "Pick one mode:\n"
                    "  - YAML mode:  rock job run --config job.yaml\n"
                    "  - flags mode: rock job run --script run.sh"
                ),
            )

        if args.script and args.script_content:
            _fail(
                parser,
                "--script and --script-content are mutually exclusive "
                "(pick a file path OR an inline snippet).",
            )

        if args.type == "harbor" and not has_config:
            _fail(
                parser,
                "--type harbor requires --config <yaml>.",
                hint=(
                    "Harbor jobs cannot be expressed purely via CLI flags.\n"
                    "Example:\n"
                    "  rock job run --config harbor.yaml"
                ),
            )

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
            if getattr(args, "xrl_authorization", None):
                env_kwargs["xrl_authorization"] = args.xrl_authorization

            uploads = []
            if args.local_path:
                uploads.append((args.local_path, args.target_path))

            env = {}
            if getattr(args, "env", None):
                for item in args.env:
                    key, _, value = item.partition("=")
                    env[key] = value

            config = BashJobConfig(
                script=args.script_content,
                script_path=args.script,
                environment=RockEnvironmentConfig(
                    **env_kwargs,
                    uploads=uploads,
                    auto_stop=True,
                    env=env,
                ),
                timeout=args.timeout,
            )

        elif job_type == "harbor":
            if not args.config:
                logger.error("--config is required for harbor type")
                return
            from rock.sdk.bench.models.job.config import HarborJobConfig

            config = HarborJobConfig.from_yaml(args.config)
            if args.image:
                config.environment.image = args.image
            config.environment.auto_stop = True

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

    def _apply_overrides(self, config, args: argparse.Namespace) -> None:
        """Apply CLI overrides that are valid in both YAML and flags modes.

        Mutates ``config`` in place. Works for both BashJobConfig and HarborJobConfig
        because both use ``RockEnvironmentConfig`` for ``environment``.
        """
        env = config.environment
        if args.image:
            env.image = args.image
        if args.memory:
            env.memory = args.memory
        if args.cpus:
            env.cpus = args.cpus
        if getattr(args, "base_url", None):
            env.base_url = args.base_url
        if getattr(args, "cluster", None):
            env.cluster = args.cluster
        if getattr(args, "extra_headers", None):
            env.extra_headers = args.extra_headers
        if getattr(args, "xrl_authorization", None):
            env.xrl_authorization = args.xrl_authorization

        for item in args.env or []:
            key, _, value = item.partition("=")
            env.env[key] = value

        if args.local_path:
            env.uploads = list(env.uploads) + [(args.local_path, args.target_path)]

        if args.timeout is not None:
            config.timeout = args.timeout

        env.auto_stop = True

    def _config_from_yaml(self, parser: argparse.ArgumentParser, args: argparse.Namespace):
        """Load config via JobConfig.from_yaml and enforce --type consistency."""
        from pathlib import Path

        from rock.sdk.job.config import BashJobConfig, JobConfig

        path = args.config
        if not Path(path).is_file():
            _fail(parser, f"--config path does not exist: {path}")

        try:
            config = JobConfig.from_yaml(path)
        except ValueError as exc:
            # from_yaml raises ValueError with a combined Bash/Harbor error message
            _fail(parser, f"Failed to load --config {path!r}:\n{exc}")
        except Exception as exc:  # YAML parse error, IO error, etc.
            _fail(parser, f"Failed to load --config {path!r}:\n{exc}")

        if args.type is not None:
            actual_type = "bash" if isinstance(config, BashJobConfig) else "harbor"
            if args.type != actual_type:
                _fail(
                    parser,
                    f"--type {args.type} does not match YAML (detected as {actual_type}).",
                    hint="Remove --type and let the YAML decide, or pass a matching config file.",
                )
        return config

    def _config_from_flags(self, args: argparse.Namespace):
        """Build a BashJobConfig purely from CLI flags (mode B)."""
        from rock.sdk.bench.models.trial.config import RockEnvironmentConfig
        from rock.sdk.job.config import BashJobConfig

        env: dict[str, str] = {}
        for item in args.env or []:
            key, _, value = item.partition("=")
            env[key] = value

        uploads = [(args.local_path, args.target_path)] if args.local_path else []

        env_kwargs: dict = {}
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
        if getattr(args, "xrl_authorization", None):
            env_kwargs["xrl_authorization"] = args.xrl_authorization

        return BashJobConfig(
            script=args.script_content,
            script_path=args.script,
            environment=RockEnvironmentConfig(
                **env_kwargs,
                uploads=uploads,
                auto_stop=True,
                env=env,
            ),
            timeout=args.timeout,
        )

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction):
        job_parser = subparsers.add_parser("job", help="Manage sandbox jobs")
        job_subparsers = job_parser.add_subparsers(dest="job_command")

        run_parser = job_subparsers.add_parser("run", help="Run a job in a sandbox")
        run_parser.add_argument(
            "--type",
            choices=["bash", "harbor"],
            default=None,
            help="Explicit job type (flags mode only; YAML mode auto-detects).",
        )
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
        run_parser.add_argument("--base-url", default=None, help="Admin service base URL")
        run_parser.add_argument("--cluster", default=None, help="Cluster name (e.g. vpc-sg-sl-a)")
        run_parser.add_argument(
            "--env",
            action="append",
            default=None,
            metavar="KEY=VALUE",
            help="Environment variable, repeatable (e.g. --env FOO=bar --env BAZ=qux)",
        )
        run_parser.add_argument(
            "--xrl-authorization",
            default=None,
            help="XRL authorization token",
        )

        # Stash on the class so _job_run can call parser.error() with the right parser.
        JobCommand._run_parser = run_parser
