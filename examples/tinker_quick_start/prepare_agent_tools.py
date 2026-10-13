"""Create isolated launchers for unchanged public SWE-agent tool implementations.

Only Python shebangs and the editor's dependency-install hook differ. The task's
Python environment and the pinned source tree are never modified.
"""
from pathlib import Path
import shutil
import sys


def prepare_agent_tools(source, destination):
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise ValueError("Tool runtime destination must be new")
    for name in ("registry", "edit_anthropic", "review_on_submit_m"):
        target = destination / name
        shutil.copytree(source / name, target)
        for path in (target / "bin").glob("*"):
            if not path.is_file():
                continue
            data = path.read_bytes()
            if data.startswith(b"#!/usr/bin/env python3\n"):
                path.write_bytes(b"#!/opt/sweagent-venv/bin/python\n" + data.split(b"\n", 1)[1])
    (destination / "edit_anthropic/install.sh").write_text(
        '/opt/sweagent-venv/bin/python -c \'from tree_sitter_languages import get_parser; get_parser("python")\'\n'
    )


if __name__ == "__main__":
    prepare_agent_tools(*sys.argv[1:])
