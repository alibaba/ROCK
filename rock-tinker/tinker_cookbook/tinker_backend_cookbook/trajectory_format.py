"""Human-readable trajectory formatting for structured runtime rollouts."""

from __future__ import annotations

import io
import json
from typing import Any

from tinker_cookbook.rl.types import Trajectory, Transition


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _format_observation(transition: Transition) -> str:
    if transition.raw_observation is not None:
        return _json_text(transition.raw_observation.request)
    return f"<structured prompt unavailable; token_count={transition.ob.length}>"


def _format_action(transition: Transition) -> str:
    action = transition.ac
    parts = [action.text if action.text is not None else "<generated text unavailable>"]
    if action.raw_text is not None and action.raw_text != action.text:
        parts.extend(["Raw text:", action.raw_text])
    if action.tool_calls:
        parts.extend(["Tool calls:", _json_text(action.tool_calls)])
    return "\n".join(parts)


def format_structured_trajectory(
    trajectory: Trajectory,
    *,
    only_last_transition: bool = False,
) -> str:
    """Render prompts and generated text without loading a local tokenizer."""
    buf = io.StringIO()
    transitions = list(enumerate(trajectory.transitions))
    if only_last_transition:
        transitions = transitions[-1:]

    print("=" * 60, file=buf)
    for index, transition in transitions:
        finish_reason = transition.ac.finish_reason or transition.ac.stop_reason
        print(f"------ Transition {index} ------", file=buf)
        print("Observation:", file=buf)
        print(_format_observation(transition), file=buf)
        print("Action:", file=buf)
        print(_format_action(transition), file=buf)
        print(
            f"Token counts: prompt_tokens={transition.ob.length} "
            f"output_tokens={len(transition.ac.tokens)}",
            file=buf,
        )
        print(f"Finish reason: {finish_reason}", file=buf)
        print(f"Reward: {transition.reward}", file=buf)
        print(f"Episode done: {transition.episode_done}", file=buf)
        print(f"Metrics: {transition.metrics}", file=buf)
        print("-" * 60, file=buf)
    print("=" * 60, file=buf)
    return buf.getvalue()
