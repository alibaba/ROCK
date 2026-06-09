"""Operator registry: name → AbstractOperator instance.

Two dispatch paths share the registry:
    * Submit path:  ``resolve(RouteContext)`` → router → operator
    * Operate path: ``get(operator_name)`` → operator (name from sandbox meta)
"""

from __future__ import annotations

from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.routing.context import RouteContext
from rock.sandbox.operator.routing.router import Router

logger = init_logger(__name__)


class OperatorRegistry:
    """Holds all loaded operators and dispatches by name or by route ctx.

    Built once at startup. ``register`` is intended to be called only
    during initialization; runtime mutation is not supported.
    """

    def __init__(self, default_name: str) -> None:
        self._default_name: str = default_name
        self._operators: dict[str, AbstractOperator] = {}
        self._router: Router | None = None

    # ------------------------------------------------------------------
    # Registration (startup-only)
    # ------------------------------------------------------------------

    def register(self, name: str, operator: AbstractOperator) -> None:
        if name in self._operators:
            raise ValueError(f"operator {name!r} already registered")
        self._operators[name] = operator
        logger.info("Operator registered: %s (%s)", name, type(operator).__name__)

    def set_router(self, router: Router) -> None:
        self._router = router

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    @property
    def default_name(self) -> str:
        return self._default_name

    @property
    def loaded_names(self) -> set[str]:
        return set(self._operators)

    def get(self, name: str | None) -> AbstractOperator:
        """Resolve operator by name; empty/None → default.

        Raises ``KeyError`` when the name was never loaded — callers
        should let this surface to alert ops that a sandbox's bound
        operator has been removed from config.
        """
        effective = name or self._default_name
        op = self._operators.get(effective)
        if op is None:
            raise KeyError(
                f"operator {effective!r} is not loaded; "
                f"loaded={sorted(self._operators)}"
            )
        return op

    def resolve(self, ctx: RouteContext) -> tuple[str, AbstractOperator]:
        """Submit-time entry: route by ctx, return (name, operator)."""
        if self._router is None:
            # No routing configured → everything goes to default. This is
            # the legacy single-operator path.
            return self._default_name, self.get(self._default_name)
        name, routed_by = self._router.route(ctx)
        logger.info("routing: operator_name=%s routed_by=%s", name, routed_by)
        return name, self.get(name)
