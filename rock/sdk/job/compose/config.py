"""ComposeJobConfig — Docker Compose multi-container job configuration (v2).

Replaces v1 custom ``compose:`` block with a pointer to a standard
``docker-compose.yaml`` file. ROCK does not parse the compose file's
internal structure — orchestration is fully delegated to docker compose.

Type detection signal: ``"compose_file" in yaml_data``
"""

from __future__ import annotations

from datetime import datetime

import yaml
from pydantic import ConfigDict, Field, model_validator

from rock.sdk.job.config import JobConfig


class ComposeJobConfig(JobConfig):
    """Docker Compose multi-container Job configuration (v2: standard compose file).

    Directly inherits JobConfig (not BashJobConfig). There is no top-level
    ``script`` / ``script_path`` — the main container entry-point is defined
    inside the docker-compose.yaml ``main`` service.

    Type detection signal: presence of ``compose_file`` key in YAML data.
    """

    model_config = ConfigDict(extra="forbid")

    job_name: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d__%H-%M-%S"))

    # Required. Identifies ComposeJobConfig. Local path (relative to job_config.yaml).
    compose_file: str

    # When True, any service exit stops the whole group (docker compose up --abort-on-container-exit).
    # Set False to allow sidecar crashes without blocking main.
    abort_on_container_exit: bool = True

    @model_validator(mode="after")
    def _validate_compose_file(self) -> ComposeJobConfig:
        if not self.compose_file:
            raise ValueError("compose_file must not be empty")
        return self

    @classmethod
    def from_yaml(cls, path: str) -> ComposeJobConfig:
        """Load a ComposeJobConfig from a YAML file."""
        with open(path) as f:
            return cls.model_validate(yaml.safe_load(f))
