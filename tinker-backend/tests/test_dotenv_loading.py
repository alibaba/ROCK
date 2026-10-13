from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tinker_backend.config import find_repo_dotenv, load_repo_dotenv


class DotenvLoadingTest(unittest.TestCase):
    def test_find_repo_dotenv_walks_up_to_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            nested = root / "src" / "tinker_backend"
            nested.mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]\nname = \"tinker-backend\"\n", encoding="utf-8")
            dotenv = root / ".env"
            dotenv.write_text("ROCK_KEY=test-key\n", encoding="utf-8")

            self.assertEqual(find_repo_dotenv(nested / "config.py"), dotenv)

    def test_load_repo_dotenv_populates_process_environment_without_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            nested = root / "src" / "tinker_backend"
            nested.mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]\nname = \"tinker-backend\"\n", encoding="utf-8")
            (root / ".env").write_text(
                "ROCK_KEY=dotenv-rock-key\n"
                "OSS_BUCKET=dotenv-bucket\n"
                "OSS_REGION=ap-southeast-1\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"ROCK_KEY": "external-rock-key"}, clear=True):
                loaded = load_repo_dotenv(nested / "config.py")

                self.assertEqual(loaded, root / ".env")
                self.assertEqual(os.environ["ROCK_KEY"], "external-rock-key")
                self.assertEqual(os.environ["OSS_BUCKET"], "dotenv-bucket")
                self.assertEqual(os.environ["OSS_REGION"], "ap-southeast-1")

    def test_load_repo_dotenv_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pyproject.toml").write_text("[project]\nname = \"tinker-backend\"\n", encoding="utf-8")

            self.assertIsNone(load_repo_dotenv(root / "src" / "tinker_backend" / "config.py"))


if __name__ == "__main__":
    unittest.main()
