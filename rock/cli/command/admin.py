import argparse
import asyncio
import subprocess

import psutil

from rock.cli.command.command import Command as CliCommand
from rock.logger import init_logger

logger = init_logger("rock.cli.admin")


class AdminCommand(CliCommand):
    name = "admin"

    def __init__(self):
        super().__init__()

    async def arun(self, args: argparse.Namespace):
        if not args.admin_action:
            raise ValueError("Admin action is required (start, stop)")

        if args.admin_action == "start":
            await self._admin_start(args)
        elif args.admin_action == "stop":
            await self._admin_stop(args)
        else:
            raise ValueError(f"Unknown admin action '{args.admin_action}'")

    async def _admin_start(self, args: argparse.Namespace):
        """Start admin service"""
        env = getattr(args, "env", None)

        subprocess.Popen(["admin", "--env", env])

    async def _admin_stop(self, args: argparse.Namespace):
        """Stop admin service"""
        try:
            # Find admin processes
            admin_processes = []
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    cmdline = proc.info["cmdline"]
                    if cmdline and len(cmdline) > 0:
                        # Check if it's an admin process
                        if (
                            "admin" in cmdline[0]
                            or (len(cmdline) > 1 and "admin" in cmdline[1])
                            or any("rock.admin.main" in str(cmd) for cmd in cmdline)
                        ):
                            admin_processes.append(proc.info["pid"])
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

            if not admin_processes:
                logger.info("No admin processes found running")
                print("No admin processes found running")
                return

            # Stop the processes gracefully
            stopped_count = 0
            for pid in admin_processes:
                try:
                    proc = psutil.Process(pid)

                    # First try graceful termination
                    proc.terminate()
                    logger.info(f"Sent SIGTERM to admin process with PID: {pid}")

                    # Wait for process to terminate gracefully
                    try:
                        proc.wait(timeout=5)  # Wait up to 5 seconds
                        stopped_count += 1
                        logger.info(f"Admin process {pid} terminated gracefully")
                    except psutil.TimeoutExpired:
                        # If graceful termination fails, force kill
                        logger.warning(f"Process {pid} did not terminate gracefully, forcing kill")
                        try:
                            proc.kill()
                            proc.wait(timeout=2)
                            stopped_count += 1
                            logger.info(f"Admin process {pid} killed forcefully")
                        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                            logger.warning(f"Could not kill process {pid}")

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    logger.warning(f"Could not access process with PID: {pid}")
                except Exception as proc_error:
                    logger.error(f"Error handling process {pid}: {proc_error}")

            if stopped_count > 0:
                print(f"Successfully stopped {stopped_count} admin process(es)")

                # Give a moment for cleanup
                await asyncio.sleep(0.5)
            else:
                print("No admin processes could be stopped")

        except asyncio.CancelledError:
            # Handle the case where the asyncio task is cancelled due to Ray's signal handling
            logger.info("Admin stop operation was cancelled, but processes should be terminated")
            print("Admin stop operation completed (process termination signal sent)")
        except Exception as e:
            logger.error(f"Error stopping admin service: {e}")
            print(f"Error stopping admin service: {e}")

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction):
        admin_parser = subparsers.add_parser("admin", help="Admin operations")
        admin_subparsers = admin_parser.add_subparsers(dest="admin_action", help="Admin actions")

        # admin start
        admin_start_parser = admin_subparsers.add_parser("start", help="Start admin service")
        admin_start_parser.add_argument("--env", default="local", help="admin service env")

        # admin stop
        admin_subparsers.add_parser("stop", help="Stop admin service")
