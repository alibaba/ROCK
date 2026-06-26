from __future__ import annotations

from rock.sdk.envhub.datasets.formats.base import FormatParser, register_format


class Tb2FormatParser(FormatParser):
    REQUIRED_FIELDS = {"instance_id"}

    def extract(self, raw: dict) -> dict:
        instance_id = raw.get("instance_id")
        if not instance_id:
            raise ValueError("TB2 instance missing required field 'instance_id'")
        return {
            "instance_id": instance_id,
            "repo": raw.get("repo"),
            "language": raw.get("language"),
            "difficulty": raw.get("difficulty"),
            "base_commit": raw.get("base_commit"),
            "image_uri": raw.get("docker_image"),
        }

    def extract_source_files(self, raw: dict) -> list[dict]:
        files: list[dict] = []
        raw_files = raw.get("files")
        if isinstance(raw_files, dict):
            for name, content in raw_files.items():
                size = len(content) if isinstance(content, (str, bytes)) else 0
                files.append({"path": name, "source_uri": "", "sha256": None, "size_bytes": size})
        return files

    def validate(self, raw: dict) -> list[str]:
        warnings: list[str] = []
        for field in self.REQUIRED_FIELDS:
            if not raw.get(field):
                warnings.append(f"Missing required field '{field}'")
        return warnings


register_format("tb2", Tb2FormatParser)
