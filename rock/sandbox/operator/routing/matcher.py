"""Matcher abstraction + registry.

Adding a new routing dimension is a 2-step extension:
    1. Implement a ``Matcher`` subclass reading fields from ``RouteContext``.
    2. Register it under a yaml key in ``MATCHER_REGISTRY``.

Router and config schema stay untouched.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from rock.sandbox.operator.routing.context import RouteContext


class Matcher(ABC):
    """One predicate over RouteContext.

    Matchers must be stateless after construction — they are reused
    across all submit requests.
    """

    yaml_key: str = ""  # set by subclasses; surfaced in routing logs

    @abstractmethod
    def match(self, ctx: RouteContext) -> bool:
        ...

    @abstractmethod
    def summary(self) -> str:
        """Short, log-friendly description, e.g. ``image_prefix=remote/``."""
        ...


@dataclass(frozen=True)
class ImagePrefixMatcher(Matcher):
    """Match when ``ctx.image`` starts with ``prefix``."""

    yaml_key: str = "image_prefix"
    prefix: str = ""

    def match(self, ctx: RouteContext) -> bool:
        return bool(self.prefix) and ctx.image.startswith(self.prefix)

    def summary(self) -> str:
        return f"image_prefix={self.prefix}"


@dataclass(frozen=True)
class ImagePatternMatcher(Matcher):
    """Match when ``ctx.image`` matches regex ``pattern``.

    Pattern is compiled at construction; invalid patterns fail-fast at
    config load time.
    """

    yaml_key: str = "image_pattern"
    pattern: str = ""

    def match(self, ctx: RouteContext) -> bool:
        if not self.pattern:
            return False
        return re.search(self.pattern, ctx.image) is not None

    def summary(self) -> str:
        return f"image_pattern={self.pattern}"


def _build_image_prefix(value: Any) -> Matcher:
    if not isinstance(value, str):
        raise ValueError(f"image_prefix must be a string, got {type(value).__name__}")
    return ImagePrefixMatcher(prefix=value)


def _build_image_pattern(value: Any) -> Matcher:
    if not isinstance(value, str):
        raise ValueError(f"image_pattern must be a string, got {type(value).__name__}")
    # Compile-check the pattern early so misconfiguration fails at startup.
    try:
        re.compile(value)
    except re.error as e:
        raise ValueError(f"image_pattern is not a valid regex: {value!r} ({e})") from e
    return ImagePatternMatcher(pattern=value)


# yaml-key → builder. Builders take the raw yaml value and return a Matcher.
# To extend: add a new (key, builder) entry; no other code changes needed.
MATCHER_REGISTRY: dict[str, callable] = {
    "image_prefix": _build_image_prefix,
    "image_pattern": _build_image_pattern,
}


def build_matcher(key: str, value: Any) -> Matcher:
    """Build a Matcher from a yaml ``match`` entry.

    Raises ``ValueError`` for unknown keys — startup fail-fast prevents
    typos from silently routing all traffic to default.
    """
    builder = MATCHER_REGISTRY.get(key)
    if builder is None:
        registered = ", ".join(sorted(MATCHER_REGISTRY)) or "(none)"
        raise ValueError(f"unknown match key {key!r}; registered keys: {registered}")
    return builder(value)
