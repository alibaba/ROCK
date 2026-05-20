"""Generate a PNG diagram for SandboxStateMachine.

Usage:
    uv run python scripts/gen_statemachine_diagram.py
"""

from statemachine.contrib.diagram import DotGraphMachine

from rock.sandbox.sandbox_statemachine import SandboxStateMachine

DotGraphMachine(SandboxStateMachine)().write_png("sandbox_statemachine.png")
print("Written to sandbox_statemachine.png")
