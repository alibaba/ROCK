import argparse
import logging
import sys
from pathlib import Path

from rock.cli.command.command import Command
from rock.sdk.model.client import ModelClient
from rock.sdk.model.service import ModelService

logger = logging.getLogger(__name__)


class ModelServiceCommand(Command):
    """
    Command for managing the model service.

    Provides subcommands to start, stop, and monitor the model service,
    which can run in 'local' mode (using a sandboxed local LLM) or 'proxy'
    mode (forwarding requests to an external endpoint).

    Examples:
        # Start local model service on default port 8080
        rock model-service start --type local

        # Start with custom port
        rock model-service start --type local --port 9000

        # Start with a configuration file
        rock model-service start --type local --config-file config.yaml

        # Start proxy mode with external endpoint
        rock model-service start --type proxy --proxy-url https://api.openai.com/v1

        # Start with custom retry behavior
        rock model-service start --type proxy --retryable-status-codes 429,500,502

        # Stop the running service
        rock model-service stop
    """

    name = "model-service"

    DEFAULT_MODEL_SERVICE_DIR = "data/cli/model"
    DEFAULT_MODEL_SERVICE_PID_FILE = DEFAULT_MODEL_SERVICE_DIR + "/pid.txt"

    async def arun(self, args: argparse.Namespace):
        if not Path(self.DEFAULT_MODEL_SERVICE_DIR).exists():
            Path(self.DEFAULT_MODEL_SERVICE_DIR).mkdir(parents=True, exist_ok=True)

        sub_command = args.model_service_command
        if "start" == sub_command:
            if await self._model_service_exist():
                logger.error("model service already exist, please run 'rock model-service stop' first")
                sys.exit(1)
                return
            logger.info("start model service")
            model_service = ModelService()
            pid = await model_service.start(
                model_service_type=args.type,
                config_file=args.config_file,
                host=args.host,
                port=args.port,
                proxy_url=args.proxy_url,
                retryable_status_codes=args.retryable_status_codes,
                request_timeout=args.request_timeout,
            )
            logger.info(f"model service started, pid: {pid}")
            with open(self.DEFAULT_MODEL_SERVICE_PID_FILE, "w") as f:
                f.write(pid)
            return
        if "watch-agent" == sub_command:
            agent_pid = args.pid
            logger.info(f"start to watch agent process, pid: {agent_pid}")
            model_service = ModelService()
            await model_service.start_watch_agent(agent_pid)
            return
        if "stop" == sub_command:
            if not await self._model_service_exist():
                logger.info("model service not exist, skip")
                return
            logger.info("start to stop model service")
            with open(self.DEFAULT_MODEL_SERVICE_PID_FILE) as f:
                pid = f.read()
                model_service = ModelService()
                await model_service.stop(pid)
            Path(self.DEFAULT_MODEL_SERVICE_PID_FILE).unlink()
            logger.info("model service stopped")
            return
        if "anti-call-llm" == sub_command:
            logger.debug("start to anti call llm")
            model_client = ModelClient()
            next_request = await model_client.anti_call_llm(index=args.index, last_response=args.response)
            # necessary: print next_request to stdout, and do NOT print anything else
            print(next_request)

    async def _model_service_exist(self) -> bool:
        exist = Path(self.DEFAULT_MODEL_SERVICE_PID_FILE).exists()
        if exist:
            with open(self.DEFAULT_MODEL_SERVICE_PID_FILE) as f:
                pid = f.read()
                logger.info(f"model service exist, pid: {pid}.")
        return exist

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction):
        model_service_parser = subparsers.add_parser(
            "model-service",
            description="model-service command",
        )
        model_service_subparsers = model_service_parser.add_subparsers(
            dest="model_service_command",
        )

        # rock model-service start
        start_parser = model_service_subparsers.add_parser(
            "start",
            help="start model service",
        )
        start_parser.add_argument(
            "--type",
            type=str,
            choices=["local", "proxy"],
            default="local",
            help="Type of model service (local/proxy)",
        )
        start_parser.add_argument(
            "--config-file",
            type=str,
            default=None,
            help="Path to the configuration YAML file",
        )
        start_parser.add_argument(
            "--host",
            type=str,
            default=None,
            help="Server host address. Overrides config file.",
        )
        start_parser.add_argument(
            "--port",
            type=int,
            default=None,
            help="Server port. Overrides config file.",
        )
        start_parser.add_argument(
            "--proxy-url",
            type=str,
            default=None,
            help="Direct proxy URL (e.g., https://your-endpoint.com/v1). Takes precedence over config file.",
        )
        start_parser.add_argument(
            "--retryable-status-codes",
            type=str,
            default=None,
            help="Retryable status codes, comma-separated (e.g., '429,500,502'). Overrides config file.",
        )
        start_parser.add_argument(
            "--request-timeout",
            type=int,
            default=None,
            help="Request timeout in seconds. Overrides config file.",
        )

        watch_agent_parser = model_service_subparsers.add_parser(
            "watch-agent",
            help="watch agent status, if stopped, send SESSION_END",
        )
        watch_agent_parser.add_argument(
            "--pid",
            required=True,
            type=int,
            help="pid of agent process to watch",
        )

        # rock model-service stop
        model_service_subparsers.add_parser(
            "stop",
            help="stop model service",
        )

        # rock model-service anti-call-llm --index N [--response RESPONSE]
        anti_call_llm_parser = model_service_subparsers.add_parser(
            "anti-call-llm",
            help="anti call llm, input is response of llm, output is the next request to llm",
        )
        anti_call_llm_parser.add_argument(
            "--index", required=True, type=int, help="index of last llm call, start from 0"
        )
        anti_call_llm_parser.add_argument("--response", required=False, help="response of last llm call")
