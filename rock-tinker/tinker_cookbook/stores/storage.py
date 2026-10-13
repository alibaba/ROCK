"""Local filesystem storage; remote object storage is intentionally unsupported."""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit


class LocalStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, name: str | Path) -> Path:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Storage paths must be relative and cannot contain '..'")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("Storage path must refer to a file inside the root")
        return path

    def write_text(self, name: str | Path, text: str) -> None:
        with self._lock:
            path = self._path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", dir=path.parent, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(text)
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def append_text(self, name: str | Path, text: str) -> None:
        with self._lock:
            path = self._path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(text)


def storage_from_uri(uri: str | Path) -> LocalStorage:
    if isinstance(uri, Path):
        return LocalStorage(uri)
    parsed = urlsplit(uri)
    if parsed.scheme:
        if parsed.scheme != "file":
            raise ValueError("Only local paths and file:// storage are supported")
        if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise ValueError("file:// storage requires a local path without query or fragment")
        return LocalStorage(unquote(parsed.path))
    if parsed.netloc:
        raise ValueError("Remote storage authorities are unsupported")
    return LocalStorage(uri)
