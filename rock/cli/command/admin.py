import argparse
import subprocess
import signal
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
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = proc.info['cmdline']
                    if cmdline and len(cmdline) > 0:
                        # Check if it's an admin process
                        if ('admin' in cmdline[0] or 
                            (len(cmdline) > 1 and 'admin' in cmdline[1]) or
                            any('rock.admin.main' in str(cmd) for cmd in cmdline)):
                            admin_processes.append(proc.info['pid'])
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

            if not admin_processes:
                logger.info("No admin processes found running")
                print("No admin processes found running")
                return

            # Stop the processes
            stopped_count = 0
            for pid in admin_processes:
                try:
                    proc = psutil.Process(pid)
                    proc.terminate()
                    stopped_count += 1
                    logger.info(f"Terminated admin process with PID: {pid}")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    logger.warning(f"Could not terminate process with PID: {pid}")

            if stopped_count > 0:
                print(f"Successfully stopped {stopped_count} admin process(es)")
            else:
                print("No admin processes could be stopped")

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
        admin_stop_parser = admin_subparsers.add_parser("stop", help="Stop admin service")