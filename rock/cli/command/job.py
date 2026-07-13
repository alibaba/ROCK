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
        elif args.job_command == "list":
            self._job_list(args)
        elif args.job_command == "show":
            self._job_show(args)
        elif args.job_command == "trials":
            self._job_trials(args)
        elif args.job_command == "trial":
            self._job_trial(args)
        else:
            logger.error(f"Unknown job subcommand: {args.job_command}")

    async def _job_run(self, args: argparse.Namespace):
        # Import lazily to avoid pulling in bench/Harbor modules for bash-only uses
        from rock.sdk.job import Job

        parser = self._run_parser

        # ── 1. Mode validation ────────────────────────────────────────
        has_config = bool(args.job_config)
        has_script = bool(args.script or args.script_content)

        if not has_config and not has_script:
            _fail(
                parser,
                "Missing job definition. Provide either a YAML config or inline script.",
                hint=(
                    "Examples:\n"
                    "  rock job run --job_config job.yaml                 # any job type, auto-detected\n"
                    "  rock job run --script path/to/run.sh               # bash, script file\n"
                    '  rock job run --script-content "echo hi"            # bash, inline snippet'
                ),
            )

        if has_config and has_script:
            _fail(
                parser,
                "--job_config is mutually exclusive with --script / --script-content.",
                hint=(
                    "Pick one mode:\n"
                    "  - YAML mode:  rock job run --job_config job.yaml\n"
                    "  - flags mode: rock job run --script run.sh"
                ),
            )

        if args.script and args.script_content:
            _fail(
                parser,
                "--script and --script-content are mutually exclusive (pick a file path OR an inline snippet).",
            )

        if args.type == "harbor" and not has_config:
            _fail(
                parser,
                "--type harbor requires --job_config <yaml>.",
                hint=(
                    "Harbor jobs cannot be expressed purely via CLI flags.\n"
                    "Example:\n"
                    "  rock job run --job_config harbor.yaml"
                ),
            )

        # ── 2. Build config ───────────────────────────────────────────
        if has_config:
            config = self._config_from_yaml(parser, args)
        else:
            config = self._config_from_flags(args)

        # ── 3. Apply overrides (shared across both modes) ─────────────
        self._apply_overrides(config, args)

        # ── 4. Run ────────────────────────────────────────────────────
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

    def _config_from_yaml(self, parser: argparse.ArgumentParser, args: argparse.Namespace):
        """Load config via JobConfig.from_yaml and enforce --type consistency."""
        from pathlib import Path

        from rock.sdk.job.config import BashJobConfig, JobConfig

        path = args.job_config
        if not Path(path).is_file():
            _fail(parser, f"--job_config path does not exist: {path}")

        try:
            config = JobConfig.from_yaml(path)
        except ValueError as exc:
            # from_yaml raises ValueError with a combined Bash/Harbor error message
            _fail(parser, f"Failed to load --job_config {path!r}:\n{exc}")
        except Exception as exc:  # YAML parse error, IO error, etc.
            _fail(parser, f"Failed to load --job_config {path!r}:\n{exc}")

        if args.type is not None:
            actual_type = "bash" if isinstance(config, BashJobConfig) else "harbor"
            if args.type != actual_type:
                _fail(
                    parser,
                    f"--type {args.type} does not match YAML (detected as {actual_type}).",
                    hint="Remove --type and let the YAML decide, or pass a matching config file.",
                )
        return config

    # ------------------------------------------------------------------
    # Viewer subcommands: list / show / trials / trial
    # ------------------------------------------------------------------

    @staticmethod
    def _build_viewer(args: argparse.Namespace):
        from rock.sdk.job.viewer import JobViewer

        if getattr(args, "use_admin", False):
            return JobViewer.from_admin(
                admin_base_url=args.base_url,
                namespace=args.namespace,
                experiment_id=args.experiment_id,
                auth_token=getattr(args, "auth_token", None),
                extra_headers=getattr(args, "extra_headers", None),
            )
        return JobViewer.from_credentials(
            oss_endpoint=args.oss_endpoint,
            oss_bucket=args.oss_bucket,
            access_key_id=args.oss_access_key_id,
            access_key_secret=args.oss_access_key_secret,
            namespace=args.namespace,
            experiment_id=args.experiment_id,
            oss_region=getattr(args, "oss_region", None),
        )

    def _job_list(self, args: argparse.Namespace):
        viewer = self._build_viewer(args)
        jobs = viewer.list_jobs()
        if not jobs:
            print("No jobs found.")
            return
        for name in jobs:
            print(f"  {name}")
        print(f"\nTotal: {len(jobs)} jobs")

    def _job_show(self, args: argparse.Namespace):
        viewer = self._build_viewer(args)
        meta = viewer.get_job_meta(args.job_name)
        if meta:
            print(f"Job: {meta.job_name}")
            print(f"  type: {meta.job_type}")
            print(f"  status: {meta.status}")
            print(f"  user: {meta.user_id or '-'}")
            print(f"  image: {meta.image or '-'}")
            print(f"  started_at: {meta.started_at or '-'}")
            print(f"  finished_at: {meta.finished_at or '-'}")
            print(f"  exit_code: {meta.exit_code if meta.exit_code is not None else '-'}")
            if meta.labels:
                print(f"  labels: {meta.labels}")
            return
        result = viewer.get_job_result(args.job_name)
        if result is None:
            print(f"Job not found: {args.job_name}")
            return
        print(f"Job: {args.job_name}")
        print(f"  id: {result.get('id', '-')}")
        print(f"  started_at: {result.get('started_at', '-')}")
        print(f"  finished_at: {result.get('finished_at', '-')}")
        print(f"  n_total_trials: {result.get('n_total_trials', '-')}")

    def _job_trials(self, args: argparse.Namespace):
        viewer = self._build_viewer(args)
        trials = viewer.get_trial_results(args.job_name)
        if not trials:
            print(f"No trials found for job: {args.job_name}")
            return
        header = f"{'NAME':<40} {'STATUS':<12} {'SCORE':<8} {'TASK'}"
        print(header)
        print("-" * len(header))
        for name, trial in sorted(trials.items()):
            score = f"{trial.score:.2f}" if trial.score is not None else "-"
            print(f"  {name:<38} {trial.status:<12} {score:<8} {trial.task_name}")
        print(f"\nTotal: {len(trials)} trials")

    def _job_trial(self, args: argparse.Namespace):
        viewer = self._build_viewer(args)
        trial = viewer.get_trial_result(args.job_name, args.trial_name)
        if trial is None:
            print(f"Trial not found: {args.trial_name}")
            return
        print(f"Trial: {args.trial_name}")
        print(f"  task: {trial.task_name}")
        agent = trial.agent_info
        model = agent.model_info
        model_str = f" ({model.name})" if model and model.name else ""
        print(f"  agent: {agent.name} {agent.version}{model_str}")
        print(f"  status: {trial.status}")
        print(f"  score: {trial.score:.2f}")
        print(f"  started_at: {trial.started_at or '-'}")
        print(f"  finished_at: {trial.finished_at or '-'}")
        if trial.exception_info:
            print(f"  exception: {trial.exception_info.exception_type}: {trial.exception_info.exception_message}")

        verifier = viewer.get_verifier_output(args.job_name, args.trial_name)
        has_verifier = any([verifier.stdout, verifier.stderr, verifier.ctrf])
        if has_verifier:
            print("\n  Verifier:")
            if verifier.stdout:
                print(f"    stdout: [{len(verifier.stdout)} chars]")
            if verifier.stderr:
                print(f"    stderr: [{len(verifier.stderr)} chars]")

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

        cfg_kwargs: dict = {}
        if args.timeout is not None:
            cfg_kwargs["timeout"] = args.timeout

        return BashJobConfig(
            script=args.script_content,
            script_path=args.script,
            environment=RockEnvironmentConfig(
                **env_kwargs,
                uploads=uploads,
                env=env,
            ),
            **cfg_kwargs,
        )

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction):
        job_parser = subparsers.add_parser("job", help="Manage sandbox jobs")
        job_subparsers = job_parser.add_subparsers(dest="job_command")

        run_parser = job_subparsers.add_parser(
            "run",
            help="Run a job in a sandbox",
            description=(
                "Run a sandbox job in one of two mutually-exclusive modes:\n"
                "  (1) YAML mode  : --job_config <file>          (type auto-detected)\n"
                "  (2) flags mode : --script / --script-content  (bash only)"
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        run_parser.add_argument(
            "--type",
            choices=["bash", "harbor"],
            default=None,
            help="Explicit job type (flags mode only; YAML mode auto-detects).",
        )
        # bash args
        run_parser.add_argument("--script", default=None, help="Path to script file")
        run_parser.add_argument("--script-content", default=None, help="Inline script content")
        # YAML config (mode A) — flag name is --job_config (distinct from the
        # top-level --config that points at the CLI INI config). Also accept
        # --job-config as the hyphen-form alias.
        run_parser.add_argument(
            "--job_config",
            "--job-config",
            dest="job_config",
            default=None,
            metavar="YAML",
            help="Job YAML config path (any job type; auto-detected).",
        )
        # shared args
        run_parser.add_argument("--image", default=None, help="Sandbox image")
        run_parser.add_argument("--memory", default=None, help="Memory (e.g. 8g)")
        run_parser.add_argument("--cpus", default=None, type=float, help="CPU count")
        run_parser.add_argument(
            "--timeout",
            type=int,
            default=None,
            help="Timeout in seconds (overrides YAML when given).",
        )
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

        # ── Viewer subcommands (shared OSS args via parent) ──────────
        def _add_viewer_args(parser: argparse.ArgumentParser):
            parser.add_argument("--namespace", required=True, help="OSS namespace")
            parser.add_argument("--experiment-id", required=True, help="Experiment ID")
            parser.add_argument("--oss-endpoint", default=None, help="OSS endpoint (AK/SK mode)")
            parser.add_argument("--oss-bucket", default=None, help="OSS bucket (AK/SK mode)")
            parser.add_argument("--oss-access-key-id", default=None, help="OSS access key ID")
            parser.add_argument("--oss-access-key-secret", default=None, help="OSS access key secret")
            parser.add_argument("--oss-region", default=None, help="OSS region")
            parser.add_argument(
                "--use-admin",
                action="store_true",
                default=False,
                help="Use admin STS auth (requires --base-url)",
            )

        list_parser = job_subparsers.add_parser("list", help="List jobs from OSS artifacts")
        _add_viewer_args(list_parser)

        show_parser = job_subparsers.add_parser("show", help="Show job details from OSS")
        show_parser.add_argument("job_name", help="Job name")
        _add_viewer_args(show_parser)

        trials_parser = job_subparsers.add_parser("trials", help="List trials for a job")
        trials_parser.add_argument("job_name", help="Job name")
        _add_viewer_args(trials_parser)

        trial_parser = job_subparsers.add_parser("trial", help="Show trial details")
        trial_parser.add_argument("job_name", help="Job name")
        trial_parser.add_argument("trial_name", help="Trial name")
        _add_viewer_args(trial_parser)
