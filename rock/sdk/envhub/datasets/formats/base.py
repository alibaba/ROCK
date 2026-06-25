from __future__ import annotations

from abc import ABC, abstractmethod


class FormatParser(ABC):
    @abstractmethod
    def extract(self, raw: dict) -> dict:
        """Extract structured fields from raw payload.

        Must return dict with required key ``instance_id``
        and optional keys: repo, language, difficulty, base_commit, image_uri.
        """
        ...

    @abstractmethod
    def extract_source_files(self, raw: dict) -> list[dict]:
        """Extract external file references from raw payload.

        Returns list of dicts: [{path, source_uri, sha256, size_bytes}].
        """
        ...

    def validate(self, raw: dict) -> list[str]:
        """Validate raw payload, return list of warning messages."""
        return []


_FORMAT_REGISTRY: dict[str, type[FormatParser]] = {}


def register_format(name: str, cls: type[FormatParser]) -> None:
    _FORMAT_REGISTRY[name] = cls


def get_parser(name: str) -> FormatParser:
    cls = _FORMAT_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"No parser registered for format '{name}'. Available: {list(_FORMAT_REGISTRY)}")
    return cls()
