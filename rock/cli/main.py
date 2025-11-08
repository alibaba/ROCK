#!/usr/bin/env python3
"""
ROCK CLI Tool
Supports sandbox build, push, and run commands
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from rock import env_vars
from rock.cli.command.command import Command
from rock.cli.config import ConfigManager
from rock.cli.loader import CommandLoader
from rock.logger import init_logger

logger = init_logger("rock.cli")


def load_config_from_file(args):
    """Load valid configuration, command line arguments take precedence over configuration file"""
    # Load configuration file
    config_path = Path(args.config) if args.config else None

    manager = ConfigManager(config_path)
    cli_config = manager.get_config()

    # If command line arguments are not set, use configuration file
    if not args.base_url:
        args.base_url = cli_config.base_url
    if not args.auth_token and (authorization := cli_config.extra_headers.get("xrl-authorization")):
        args.auth_token = authorization
    if not args.cluster and (cluster := cli_config.extra_headers.get("cluster")):
        args.cluster = cluster

    # Process extra_headers, first get from configuration file
    extra_headers = cli_config.extra_headers.copy()

    # Then process the --extra-header parameter from command line
    if hasattr(args, "extra_headers_list") and args.extra_headers_list:
        for header_str in args.extra_headers_list:
            if "=" in header_str:
                key, value = header_str.split("=", 1)  # Only split the first = sign
                extra_headers[key.strip()] = value.strip()
            else:
                logger.warning(f"Invalid header format: {header_str}. Expected format: 'Key=Value'")

    # Set the final extra_headers
    args.extra_headers = extra_headers


def create_parser(command_classes: list[type]):
    """Create command line argument parser"""
    parser = argparse.ArgumentParser(
        description="ROCK Sandbox CLI Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build a sandbox
  rock sandbox build --image python:3.11 --build-command "pip install -r requirements.txt"

  # Push a sandbox image
  rock sandbox push --image my-app:latest --registry hub.alibaba-inc.com

  # Run a sandbox
  rock sandbox run --image python:3.11 --command "python app.py"

  # Run sandbox interactively
  rock sandbox run --image python:3.11 --interactive

  # Start admin service
  rock admin start
        """,
    )

    # Global parameters
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--config", help="Path to config file (default: ./.rock/config.ini)")
    parser.add_argument("--base-url", help="ROCK server base URL (overrides config file)")
    parser.add_argument("--auth-token", help="ROCK authorization token (overrides config file)")
    parser.add_argument("--cluster", help="ROCK cluster (overrides config file)")

    # extra-header parameter
    parser.add_argument(
        "--extra-header",
        action="append",
        dest="extra_headers_list",
        help="Extra HTTP headers in format 'Key=Value'. Can be used multiple times. Example: --extra-header 'X-Token=abc' --extra-header 'User-Agent=MyApp'",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    for command_class in command_classes:
        asyncio.run(command_class.add_parser_to(subparsers))

    return parser


def find_command(command: str, subclasses: list[type[Command]]) -> type | None:
    """Find command by name"""
    for subclass in subclasses:
        if command == subclass.name:
            return subclass
    return None


def main():
    """Main function"""
    load_paths = env_vars.ROCK_CLI_LOAD_PATHS
    subclasses = asyncio.run(CommandLoader.load(load_paths.split(",")))

    parser = create_parser(subclasses)
    args = parser.parse_args()

    # Check command
    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Load valid configuration (configuration file + command line arguments)
    load_config_from_file(args)

    try:
        command = find_command(args.command, subclasses)
        if not command:
            raise ValueError(f"Error: Unknown command '{args.command}'")
        asyncio.run(command().arun(args))
    except Exception as e:
        # Ensure all logs are flushed to the console
        root_logger = logging.getLogger()
        for handler in root_logger.handlers:
            handler.flush()
        raise e


if __name__ == "__main__":
    main()
