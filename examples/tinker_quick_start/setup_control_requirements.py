#!/usr/bin/env python3
"""Verify the locked control environment imports without starting services."""
import argparse
import contextlib
import io
import logging
import os
import secrets
import importlib
import importlib.metadata
import json
from pathlib import Path
import sys

MODULES = (
    "rock.admin.main", "rock.rocklet.server", "tinker", "tinker_backend",
    "tinker_cookbook.tinker_backend_cookbook.train_swe_bench",
    "tinker_cookbook.tinker_backend_cookbook.train_swe_bench_kl",
    "tinker_cookbook.tinker_backend_cookbook.train_swe_bench_grpo",
    "examples.tinker_quick_start.harbor_pull_runner",
)

def import_config(output):
    """Generate a private, self-contained import-only config; never print the key."""
    if output.exists():
        if output.stat().st_mode & 0o077:
            raise ValueError("Import-only config must have private permissions")
        return
    payload = {"aes_encrypt_key": secrets.token_hex(16),
               "ray": {}, "nacos": {}, "redis": {}}
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream)
        stream.write("\n")

def verify(output, expected_torch):
    config = output.parent / "import-config.json"
    import_config(config)
    os.environ["ROCK_CONFIG"] = str(config)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    errors = {}
    modules = []
    module_paths = {}
    logging.disable(logging.CRITICAL)
    for name in MODULES:
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                module = importlib.import_module(name)
            module_paths[name] = getattr(module, "__file__", None)
            modules.append(name)
        except Exception as error:
            # Do not print config repr, headers, or exception payloads.
            errors[name] = {"type": type(error).__name__}
            if isinstance(error, ModuleNotFoundError):
                errors[name]["missing_module"] = error.name
    actual_torch = importlib.metadata.version("torch")
    if actual_torch != expected_torch:
        errors["torch"] = {"type": "VersionMismatch"}
    distributions = {}
    for name in ("rl-rock", "torch", "numpy", "ray", "harbor", "litellm", "openai", "pydantic", "transformers", "nacos-sdk-python"):
        try:
            distributions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            errors[name] = {"type": "PackageNotFoundError"}
    report = {"python": sys.version.split()[0], "imports": modules, "module_paths": module_paths,
              "versions": distributions, "errors": errors, "ok": not errors}
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config")
    config.add_argument("--output", required=True, type=Path)
    check = commands.add_parser("verify")
    check.add_argument("--output", required=True, type=Path)
    check.add_argument("--torch-version", required=True)
    args = parser.parse_args()
    if args.command == "config":
        import_config(args.output)
        return 0
    return verify(args.output, args.torch_version)

if __name__ == "__main__":
    raise SystemExit(main())
