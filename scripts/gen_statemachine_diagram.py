"""Generate a PNG diagram for SandboxStateMachine.

Usage:
    uv run python scripts/gen_statemachine_diagram.py
"""

import re

import pydot
from statemachine.contrib.diagram import DotGraphMachine

from rock.sandbox.sandbox_statemachine import SandboxStateMachine

INTERMEDIATE_STATES = {"pending", "archiving"}

graph = DotGraphMachine(SandboxStateMachine)()
dot_src = graph.to_string()

for state in INTERMEDIATE_STATES:
    dot_src = re.sub(
        rf"({state} \[.*?)fillcolor=white",
        r'\1fillcolor="#FFF3CD", color="#856404"',
        dot_src,
    )

(styled_graph,) = pydot.graph_from_dot_data(dot_src)
styled_graph.write_png("sandbox_statemachine.png")
print("Written to sandbox_statemachine.png")
