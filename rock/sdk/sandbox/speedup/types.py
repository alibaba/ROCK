"""Speedup types definition."""

from enum import Enum


class SpeedupType(str, Enum):
    """Speedup type enumeration"""

    APT = "apt"
    PIP = "pip"
    GITHUB = "github"
