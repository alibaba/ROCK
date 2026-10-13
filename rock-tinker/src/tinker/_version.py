# Derived from https://github.com/thinking-machines-lab/tinker (Apache-2.0).
# Modified in this Tinker fork; see rock-tinker/UPSTREAM.md and NOTICE.
from importlib.metadata import version, PackageNotFoundError

__title__ = "tinker"
try:
    __version__ = version(__title__)
except PackageNotFoundError:
    __version__ = "0.18.1"  # SDK bundled in rl-rock without a separate tinker distribution.
