from dataclasses import dataclass, field


@dataclass
class DatasetSpec:
    organization: str
    name: str
    split: str
    task_ids: list[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.organization}/{self.name}"


@dataclass
class UploadResult:
    organization: str
    name: str
    split: str
    uploaded: int
    skipped: int
    failed: int
