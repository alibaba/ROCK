from importlib.metadata import version, PackageNotFoundError

__title__ = "tinker"
try:
    __version__ = version(__title__)
except PackageNotFoundError:
    __version__ = "0.18.1"  # SDK bundled in rl-rock without a separate tinker distribution.
