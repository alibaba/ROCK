from dataclasses import dataclass, field


@dataclass
class DatasetSpec:
    id: str  # "{organization}/{dataset_name}", e.g. "princeton-nlp/SWE-bench_Verified"
    split: str
    task_ids: list[str] = field(default_factory=list)


@dataclass
class UploadResult:
    id: str  # "{organization}/{dataset_name}"
    split: str
    uploaded: int
    skipped: int
    failed: int
