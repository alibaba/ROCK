"""Sequential rule-based router.

Single semantics: walk rules in declaration order; first whose ``match``
block fully matches wins; otherwise fall back to ``default``.

Multiple match keys within one rule are AND-combined; OR is expressed by
splitting into multiple rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rock.logger import init_logger
from rock.sandbox.operator.routing.context import RouteContext
from rock.sandbox.operator.routing.matcher import Matcher, build_matcher

logger = init_logger(__name__)


@dataclass(frozen=True)
class RoutingRule:
    """One yaml rule entry. Matchers are AND-combined."""

    target: str
    matchers: tuple[Matcher, ...]

    def match(self, ctx: RouteContext) -> bool:
        if not self.matchers:
            # Defensive: an empty match block would always fire and shadow
            # every later rule. Validated to be non-empty at parse time.
            return False
        return all(m.match(ctx) for m in self.matchers)

    def summary(self) -> str:
        return ",".join(m.summary() for m in self.matchers)


@dataclass
class Router:
    default: str
    rules: list[RoutingRule] = field(default_factory=list)

    def route(self, ctx: RouteContext) -> tuple[str, str]:
        """Return (operator_name, routed_by) where routed_by is a log tag."""
        for idx, rule in enumerate(self.rules, start=1):
            if rule.match(ctx):
                return rule.target, f"rule#{idx}({rule.summary()})"
        return self.default, "default"

    @classmethod
    def from_config(
        cls,
        routing_cfg: dict[str, Any] | None,
        fallback_default: str,
        loaded_operators: set[str],
    ) -> "Router":
        """Build router from yaml ``runtime.operator_routing`` block.

        Args:
            routing_cfg: Parsed ``operator_routing`` dict, or None when the
                block is absent — every submit goes to default.
            fallback_default: Used when ``routing_cfg.default`` is omitted.
                Caller passes ``runtime.operator_type`` here to preserve
                the legacy semantics.
            loaded_operators: Names of operators actually loaded into the
                registry. Used to validate ``default`` and rule targets.
        """
        cfg = routing_cfg or {}
        default = cfg.get("default") or fallback_default
        if default not in loaded_operators:
            raise ValueError(
                f"routing default {default!r} is not a loaded operator; "
                f"loaded={sorted(loaded_operators)}"
            )

        raw_rules = cfg.get("rules") or []
        if not isinstance(raw_rules, list):
            raise ValueError("operator_routing.rules must be a list")

        rules: list[RoutingRule] = []
        for i, raw in enumerate(raw_rules, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"rule #{i}: must be a mapping, got {type(raw).__name__}")
            target = raw.get("target")
            if not target or not isinstance(target, str):
                raise ValueError(f"rule #{i}: 'target' is required and must be a string")
            if target not in loaded_operators:
                raise ValueError(
                    f"rule #{i}: target {target!r} is not a loaded operator; "
                    f"loaded={sorted(loaded_operators)}"
                )
            match_block = raw.get("match") or {}
            if not isinstance(match_block, dict) or not match_block:
                raise ValueError(f"rule #{i}: 'match' must be a non-empty mapping")
            matchers = tuple(build_matcher(k, v) for k, v in match_block.items())
            rules.append(RoutingRule(target=target, matchers=matchers))

        logger.info(
            "Router built: default=%s rules=%d (operators=%s)",
            default,
            len(rules),
            sorted(loaded_operators),
        )
        return cls(default=default, rules=rules)
