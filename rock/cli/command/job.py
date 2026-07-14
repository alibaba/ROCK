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
        elif args.job_command == "run-list":
            self._job_run_list(args)
        elif args.job_command == "run-status":
            self._job_run_status(args)
        elif args.job_command == "job-list":
            self._job_artifact_list(args)
        elif args.job_command == "job-show":
            self._job_artifact_show(args)
        elif args.job_command == "trial-list":
            self._job_trial_list(args)
        elif args.job_command == "trial-show":
            self._job_trial_show(args)
        else:
            logger.error(f"Unknown job subcommand: {args.job_command}")

    async def _job_run(self, args: argparse.Namespace):
        from rock.cli.job_run import (
            JsonlProgressReporter,
            NullProgressReporter,
            UnifiedJobRunHandler,
            generate_run_id,
            resolve_task_ids,
            split_dataset_name,
            sync_namespace,
        )
        from rock.sdk.job.executor import JobExecutor
        from rock.sdk.job.job_meta import JobMetaRepository
        from rock.sdk.job.run_meta import RunMetaRepository

        parser = self._run_parser

        # ── 1. Mode validation ────────────────────────────────────────
        has_config = bool(args.job_config)
        has_script = bool(args.script or args.script_content)
        is_resume = bool(args.resume)

        if not is_resume and not has_config and not has_script:
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
        sync_namespace(config, args.namespace)
        if args.experiment_id:
            config.experiment_id = args.experiment_id
            config.environment.experiment_id = args.experiment_id
            if config.environment.oss_mirror:
                config.environment.oss_mirror.experiment_id = args.experiment_id

        try:
            run_meta_repo = None
            job_meta_repo = None
            try:
                run_meta_repo = RunMetaRepository.from_job_config(config)
                job_meta_repo = JobMetaRepository(run_meta_repo._viewer)
            except ValueError:
                if is_resume:
                    _fail(parser, "--resume requires --job-config with oss_mirror or explicit artifact locator.")

            if is_resume:
                forbidden = [
                    name
                    for name in ("task", "tasks", "all", "org", "dataset", "split", "limit")
                    if getattr(args, name, None)
                ]
                if forbidden:
                    _fail(parser, "--resume cannot be combined with task selection or dataset override arguments.")
                if run_meta_repo is None:
                    _fail(parser, "--resume requires an artifact locator.")
                run_id = args.resume
                run_meta = run_meta_repo.get(run_id)
                if run_meta is None:
                    _fail(parser, f"run_id not found: {run_id}")
                completed = run_meta_repo.find_completed_tasks(run_id)
                task_ids = [task_id for task_id in run_meta.task_job_map if task_id not in completed]
                org, dataset = split_dataset_name(run_meta.dataset or "")
                dataset_ref = type("DatasetRefValue", (), {
                    "org": org,
                    "dataset": dataset,
                    "split": run_meta.split,
                    "full_name": run_meta.dataset,
                })()
                mode = run_meta.mode
            else:
                run_id = generate_run_id()
                try:
                    mode, dataset_ref, task_ids = resolve_task_ids(
                        config,
                        task=args.task,
                        tasks=args.tasks,
                        all_tasks=args.all,
                        org=args.org,
                        dataset=args.dataset,
                        split=args.split,
                        limit=args.limit,
                    )
                except ValueError as exc:
                    _fail(parser, str(exc))
                run_meta = None

            result = await UnifiedJobRunHandler(
                mode=mode,
                task_ids=task_ids,
                dataset_ref=dataset_ref,
                run_id=run_id,
                run_meta_repo=run_meta_repo,
                job_meta_repo=job_meta_repo,
                executor=JobExecutor(max_concurrent=args.concurrency),
                progress=JsonlProgressReporter() if args.jsonl else NullProgressReporter(),
                resumed=is_resume,
                base_run_meta=run_meta,
            ).run(config)
            if result.failed > 0:
                raise SystemExit(1)
            logger.info("Run completed: run_id=%s failed=%s", result.run_id, result.failed)
        except Exception as e:
            logger.error(f"Job run failed: {e}", exc_info=True)
            raise SystemExit(1)

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

        if getattr(args, "type", None) is not None:
            actual_type = "bash" if isinstance(config, BashJobConfig) else "harbor"
            if args.type != actual_type:
                _fail(
                    parser,
                    f"--type {args.type} does not match YAML (detected as {actual_type}).",
                    hint="Remove --type and let the YAML decide, or pass a matching config file.",
                )
        return config

    # ------------------------------------------------------------------
    # Viewer subcommands: job-list / job-show / trial-list / trial-show
    # ------------------------------------------------------------------

    @staticmethod
    def _build_viewer(args: argparse.Namespace):
        from rock.sdk.job.viewer import JobViewer
        extra_headers = getattr(args, "extra_headers", None)
        if isinstance(extra_headers, list):
            parsed_headers = {}
            for item in extra_headers:
                key, _, value = item.partition("=")
                if key:
                    parsed_headers[key] = value
            extra_headers = parsed_headers

        if getattr(args, "use_admin", False):
            return JobViewer.from_admin(
                admin_base_url=args.base_url,
                namespace=args.namespace,
                experiment_id=args.experiment_id,
                auth_token=getattr(args, "auth_token", None),
                extra_headers=extra_headers,
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

    def _build_viewer_from_locator(self, args: argparse.Namespace):
        if getattr(args, "job_config", None):
            from rock.sdk.job.run_meta import RunMetaRepository

            config = self._config_from_yaml(self._run_parser or argparse.ArgumentParser(prog="rock job"), args)
            return RunMetaRepository.from_job_config(config)._viewer
        return self._build_viewer(args)

    def _job_run_list(self, args: argparse.Namespace):
        from rock.sdk.job.run_meta import RunMetaRepository

        runs = RunMetaRepository(self._build_viewer_from_locator(args)).list()
        if getattr(args, "output", "table") == "json":
            import json

            print(json.dumps([run.model_dump(mode="json") for run in runs], ensure_ascii=False))
            return
        if not runs:
            print("No runs found.")
            return
        header = f"{'RUN_ID':<28} {'MODE':<8} {'STATUS':<10} {'TOTAL':<7} {'PENDING':<8} {'PASS_RATE':<10} {'AVG_SCORE':<10} DATASET"
        print(header)
        print("-" * len(header))
        for meta in runs:
            pass_rate = f"{meta.summary.pass_rate:.2f}" if meta.summary else "-"
            avg_score = f"{meta.summary.avg_score:.3f}" if meta.summary else "-"
            print(
                f"{meta.run_id:<28} {meta.mode:<8} {meta.status:<10} {meta.total_tasks:<7} "
                f"{meta.pending_tasks:<8} {pass_rate:<10} {avg_score:<10} {meta.dataset or '-'}"
            )

    def _job_run_status(self, args: argparse.Namespace):
        from rock.sdk.job.run_meta import RunMetaRepository

        repo = RunMetaRepository(self._build_viewer_from_locator(args))
        meta = repo.get(args.run_id)
        if meta is None:
            print(f"Run not found: {args.run_id}")
            raise SystemExit(1)
        jobs = repo.get_run_job_statuses(args.run_id) if args.jobs else []
        if getattr(args, "output", "table") == "json":
            import json

            payload = meta.model_dump(mode="json")
            if args.jobs:
                payload["jobs"] = [job.model_dump(mode="json") for job in jobs]
            print(json.dumps(payload, ensure_ascii=False))
            return
        print(f"Run: {meta.run_id}")
        print(f"mode: {meta.mode}")
        print(f"status: {meta.status}")
        print(f"dataset: {meta.dataset or '-'}")
        print(f"split: {meta.split or '-'}")
        print(f"total_tasks: {meta.total_tasks}")
        print(f"pending_tasks: {meta.pending_tasks}")
        if meta.summary:
            print(f"pass_rate: {meta.summary.pass_rate:.2f}")
            print(f"avg_score: {meta.summary.avg_score:.3f}")
        if args.jobs:
            header = f"{'TASK_ID':<20} {'JOB_NAME':<40} {'STATUS':<12} {'SCORE':<8} SANDBOX"
            print(header)
            print("-" * len(header))
            for job in jobs:
                score = f"{job.score:.2f}" if job.score is not None else "-"
                print(f"{job.task_id:<20} {job.job_name:<40} {job.status:<12} {score:<8} {job.sandbox_id or '-'}")

    def _job_artifact_list(self, args: argparse.Namespace):
        viewer = self._build_viewer(args)
        jobs = viewer.list_jobs()
        if not jobs:
            print("No jobs found.")
            return
        for name in jobs:
            print(f"  {name}")
        print(f"\nTotal: {len(jobs)} jobs")

    def _job_artifact_show(self, args: argparse.Namespace):
        viewer = self._build_viewer_from_locator(args)
        job_name = args.job_name_option or args.job_name
        if not job_name and args.run_id and args.task_id:
            from rock.sdk.job.run_meta import RunMetaRepository

            refs = RunMetaRepository(viewer).list_run_jobs(args.run_id)
            for ref in refs:
                if ref.task_id == args.task_id:
                    job_name = ref.job_name
                    break
        if not job_name:
            print("Job not found: provide --job-name or --run-id with --task-id")
            raise SystemExit(1)
        meta = viewer.get_job_meta(job_name)
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
        result = viewer.get_job_result(job_name)
        if result is None:
            print(f"Job not found: {job_name}")
            return
        print(f"Job: {job_name}")
        print(f"  id: {result.get('id', '-')}")
        print(f"  started_at: {result.get('started_at', '-')}")
        print(f"  finished_at: {result.get('finished_at', '-')}")
        print(f"  n_total_trials: {result.get('n_total_trials', '-')}")

    def _job_trial_list(self, args: argparse.Namespace):
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

    def _job_trial_show(self, args: argparse.Namespace):
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
    def _add_viewer_args(parser: argparse.ArgumentParser, *, required: bool = True, include_job_config: bool = False):
        if include_job_config:
            parser.add_argument(
                "--job-config",
                dest="job_config",
                default=None,
                help="YAML config used to locate OSS artifacts",
            )
        parser.add_argument("--namespace", required=required, help="OSS artifact namespace")
        parser.add_argument("--experiment-id", required=required, help="OSS artifact experiment id")
        parser.add_argument("--oss-endpoint", default=None, help="OSS endpoint (AK/SK mode)")
        parser.add_argument("--oss-bucket", default=None, help="OSS bucket (AK/SK mode)")
        parser.add_argument("--oss-access-key-id", default=None, help="OSS access key ID")
        parser.add_argument("--oss-access-key-secret", default=None, help="OSS access key secret")
        parser.add_argument("--oss-region", default=None, help="OSS region")
        parser.add_argument("--base-url", default=None, help="Admin service base URL")
        parser.add_argument("--auth-token", default=None, help="Admin auth token")
        parser.add_argument("--extra-header", action="append", dest="extra_headers", default=None, help="Admin extra header")
        parser.add_argument(
            "--use-admin",
            action="store_true",
            default=False,
            help="Use admin STS auth (requires --base-url)",
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
        run_parser.add_argument("--task", default=None, help="Run one explicit task id")
        run_parser.add_argument("--tasks", default=None, help="Comma-separated task ids")
        run_parser.add_argument("--all", action="store_true", default=False, help="Run all tasks in dataset split")
        run_parser.add_argument("--org", default=None, help="Dataset org override")
        run_parser.add_argument("--dataset", default=None, help="Dataset name override")
        run_parser.add_argument("--split", default=None, help="Dataset split override")
        run_parser.add_argument("--limit", type=int, default=None, help="Limit selected tasks")
        run_parser.add_argument("--concurrency", type=int, default=1, help="Max concurrent jobs")
        run_parser.add_argument("--resume", default=None, metavar="RUN_ID", help="Resume an existing run id")
        run_parser.add_argument("--namespace", default=None, help="OSS namespace override")
        run_parser.add_argument("--experiment-id", default=None, help="Experiment id override")
        run_parser.add_argument("--jsonl", action="store_true", default=False, help="Emit JSONL run events")

        # Stash on the class so _job_run can call parser.error() with the right parser.
        JobCommand._run_parser = run_parser

        runs_parser = job_subparsers.add_parser(
            "run-list",
            help="List historical job runs from run metadata",
            description="List historical job runs from run metadata.",
        )
        runs_parser.add_argument("--output", choices=["table", "json"], default="table")
        JobCommand._add_viewer_args(runs_parser, required=False, include_job_config=True)

        status_parser = job_subparsers.add_parser(
            "run-status",
            help="Show summary and task/job status for one run",
            description="Show summary and task/job status for one run.",
        )
        status_parser.add_argument("--run-id", required=True, help="Run id from rock job run or run-list")
        status_parser.add_argument(
            "--jobs",
            action="store_true",
            default=False,
            help="Include task/job status rows for this run",
        )
        status_parser.add_argument("--output", choices=["table", "json"], default="table")
        JobCommand._add_viewer_args(status_parser, required=False, include_job_config=True)

        # ── Viewer subcommands (shared OSS args via parent) ──────────
        def _add_viewer_args(parser: argparse.ArgumentParser):
            JobCommand._add_viewer_args(parser)

        list_parser = job_subparsers.add_parser(
            "job-list",
            help="List job artifact directories in an experiment",
            description="List job artifact directories in an experiment.",
        )
        _add_viewer_args(list_parser)

        show_parser = job_subparsers.add_parser(
            "job-show",
            help="Show one job artifact by job name or run/task id",
            description="Show one job artifact by job name or run/task id.",
        )
        show_parser.add_argument("job_name", nargs="?", help="Job artifact name")
        show_parser.add_argument("--job-name", dest="job_name_option", default=None, help="Job artifact name")
        show_parser.add_argument("--run-id", default=None, help="Run id from rock job run or run-list")
        show_parser.add_argument("--task-id", default=None, help="Task id inside the run; use with --run-id")
        JobCommand._add_viewer_args(show_parser, required=False, include_job_config=True)

        trials_parser = job_subparsers.add_parser(
            "trial-list",
            help="List trial results under one job artifact",
            description="List trial results under one job artifact.",
        )
        trials_parser.add_argument("job_name", help="Job artifact name")
        _add_viewer_args(trials_parser)

        trial_parser = job_subparsers.add_parser(
            "trial-show",
            help="Show one trial result and verifier details",
            description="Show one trial result and verifier details.",
        )
        trial_parser.add_argument("job_name", help="Job artifact name")
        trial_parser.add_argument("trial_name", help="Trial name under the job artifact")
        _add_viewer_args(trial_parser)
