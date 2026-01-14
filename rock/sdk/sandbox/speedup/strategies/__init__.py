"""Speedup strategies."""

from rock.sdk.sandbox.speedup.strategies.apt import AptSpeedupStrategy
from rock.sdk.sandbox.speedup.strategies.github import GithubSpeedupStrategy
from rock.sdk.sandbox.speedup.strategies.pip import PipSpeedupStrategy

__all__ = ["AptSpeedupStrategy", "PipSpeedupStrategy", "GithubSpeedupStrategy"]
