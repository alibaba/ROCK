"""RemoteOperator: dispatches sandbox lifecycle to an Infra-style external API.

The operator stays inside ROCK's existing AbstractOperator contract, so the
dispatch logic in SandboxManager / SandboxStateMachine does not need to
distinguish remote sandboxes from local ones. All Infra-specific concerns
(payload shape, header auth, response decoding, state mapping) live in this
package.
"""

from rock.sandbox.operator.remote.operator import RemoteOperator

__all__ = ["RemoteOperator"]
