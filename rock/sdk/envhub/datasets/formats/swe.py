from __future__ import annotations

from rock.sdk.envhub.datasets.formats.base import FormatParser, register_format


class SweFormatParser(FormatParser):
    REQUIRED_FIELDS = {"instance_id", "repo", "base_commit"}

    def extract(self, raw: dict) -> dict:
        instance_id = raw.get("instance_id")
        if not instance_id:
            raise ValueError("SWE-bench instance missing required field 'instance_id'")
        return {
            "instance_id": instance_id,
            "repo": raw.get("repo", ""),
            "language": "python",
            "difficulty": raw.get("difficulty"),
            "base_commit": raw.get("base_commit"),
            "image_uri": raw.get("docker_image"),
        }

    def extract_source_files(self, raw: dict) -> list[dict]:
        files: list[dict] = []
        if raw.get("patch"):
            files.append({"path": "patch.diff", "source_uri": "", "sha256": None, "size_bytes": len(raw["patch"])})
        if raw.get("test_patch"):
            files.append(
                {"path": "test_patch.diff", "source_uri": "", "sha256": None, "size_bytes": len(raw["test_patch"])}
            )
        return files

    def validate(self, raw: dict) -> list[str]:
        warnings: list[str] = []
        for field in self.REQUIRED_FIELDS:
            if not raw.get(field):
                warnings.append(f"Missing required field '{field}'")
        if not raw.get("patch"):
            warnings.append("Missing 'patch' field — instance may be incomplete")
        return warnings


register_format("swe", SweFormatParser)
