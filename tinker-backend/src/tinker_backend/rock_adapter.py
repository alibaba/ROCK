"""Compatibility shim — delegates to tinker_backend.adapters.rock_adapter."""

from tinker_backend.adapters.rock_adapter import main  # noqa: F401

if __name__ == "__main__":
    main()
