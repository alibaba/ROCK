"""Compatibility shim — delegates to tinker_backend.server.app."""

from tinker_backend.server.app import app, create_app, main  # noqa: F401

if __name__ == "__main__":
    main()
