"""Operator routing layer.

Submit-time mechanism that maps a sandbox creation request to one of the
registered operators based on declarative rules. GET-class operations
(get_status / stop / restart / delete) bypass this layer and dispatch by
the ``operator_name`` field stored in sandbox meta.
"""

from rock.sandbox.operator.routing.context import RouteContext
from rock.sandbox.operator.routing.matcher import MATCHER_REGISTRY, Matcher
from rock.sandbox.operator.routing.router import Router, RoutingRule

__all__ = ["MATCHER_REGISTRY", "Matcher", "RouteContext", "Router", "RoutingRule"]
