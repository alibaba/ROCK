import json
import os
from typing import Any

from pydantic import BaseModel, Field

from rock import env_vars
from rock.deployments.constants import Status
from rock.logger import init_logger
from rock.utils.system import get_iso8601_timestamp

logger = init_logger(__name__)


class PhaseStatus(BaseModel):
    status: Status = Status.WAITING
    message: str = "waiting"
    started_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status.value, "message": self.message}
        if self.started_at:
            d["started_at"] = self.started_at
        if self.completed_at:
            d["completed_at"] = self.completed_at
        return d


class ServiceStatus(BaseModel):
    phases: dict[str, PhaseStatus] = Field(default_factory=dict)
    port_mapping: dict[int, int] = Field(default_factory=dict)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        if not self.phases:
            self.add_phase("image_pull", PhaseStatus())
            self.add_phase("docker_run", PhaseStatus())

    def add_phase(self, phase_name: str, status: PhaseStatus):
        self.phases[phase_name] = status

    def get_phase(self, phase_name: str) -> PhaseStatus:
        return self.phases[phase_name]

    def update_status(self, phase_name: str, status: Status, message: str):
        phase = self.phases[phase_name]
        now = get_iso8601_timestamp()
        if status == Status.RUNNING and phase.started_at is None:
            phase.started_at = now
        if status in (Status.SUCCESS, Status.FAILED, Status.TIMEOUT):
            phase.completed_at = now
        phase.status = status
        phase.message = message

    def add_port_mapping(self, local_port: int, container_port: int):
        self.port_mapping[local_port] = container_port

    def get_port_mapping(self) -> dict[int, int]:
        return self.port_mapping

    def get_mapped_port(self, local_port: int) -> int:
        return self.port_mapping[local_port]

    def __str__(self) -> str:
        """String representation"""
        status_lines = []
        for name, phase in self.phases.items():
            status_lines.append(f"{name}: {phase.status.value} - {phase.message}")
        return "\n".join(status_lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phases": {name: phase.to_dict() for name, phase in self.phases.items()},
            "port_mapping": self.port_mapping,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServiceStatus":
        """Create ServiceStatus object from dictionary"""
        phases = {}
        for key, phase_data in data.get("phases", {}).items():
            phases[key] = PhaseStatus(
                status=Status(phase_data["status"]),
                message=phase_data.get("message", ""),
                started_at=phase_data.get("started_at"),
                completed_at=phase_data.get("completed_at"),
            )

        port_mapping = {}
        for port_value, mapping in data.get("port_mapping", {}).items():
            port_mapping[int(port_value)] = mapping

        return cls(phases=phases, port_mapping=port_mapping)

    @classmethod
    def from_content(cls, content: str) -> "ServiceStatus":
        """Create ServiceStatus from JSON file."""
        try:
            data = json.loads(content)
            service_status = cls.from_dict(data)
            return service_status
        except Exception as e:
            raise Exception(f"parse service status failed:{str(e)}")


class PersistedServiceStatus(ServiceStatus):
    _json_path: str | None = None
    _status_path_initialized: bool = False
    sandbox_id: str | None = None

    def set_sandbox_id(self, sandbox_id: str):
        self.sandbox_id = sandbox_id
        self._load_and_merge()

    def _load_and_merge(self):
        """Load existing service_status file and merge phases (preserving history)."""
        path = PersistedServiceStatus.gen_service_status_path(self.sandbox_id)
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            existing_phases = data.get("phases", {})
            for name, phase_data in existing_phases.items():
                if name not in self.phases:
                    self.phases[name] = PhaseStatus(
                        status=Status(phase_data["status"]),
                        message=phase_data.get("message", ""),
                        started_at=phase_data.get("started_at"),
                        completed_at=phase_data.get("completed_at"),
                    )
            existing_ports = data.get("port_mapping", {})
            for port_str, mapping in existing_ports.items():
                port = int(port_str)
                if port not in self.port_mapping:
                    self.port_mapping[port] = mapping
        except Exception as e:
            logger.warning(f"load persisted service status failed for {self.sandbox_id}: {e}")

    def _save_to_file(self):
        """Save ServiceStatus to the file specified by _json_path"""
        if self.sandbox_id is None:
            return

        if not self._status_path_initialized:
            self._json_path = PersistedServiceStatus.gen_service_status_path(self.sandbox_id)
            os.makedirs(os.path.dirname(self._json_path), exist_ok=True)
            self._status_path_initialized = True
        try:
            with open(self._json_path, "w") as f:
                json.dump(self.to_dict(), f, indent=2)
        except Exception as e:
            # Error handling to prevent file write failures from affecting the main process
            raise Exception(f"save service status failed: {str(e)}")

    def add_phase(self, phase_name: str, status: PhaseStatus):
        super().add_phase(phase_name, status)
        self._save_to_file()

    def update_status(self, phase_name: str, status: Status, message: str):
        super().update_status(phase_name, status, message)
        self._save_to_file()

    def add_port_mapping(self, local_port: int, container_port: int):
        super().add_port_mapping(local_port, container_port)
        self._save_to_file()

    @classmethod
    def from_content(cls, content: str) -> "ServiceStatus":
        """Create ServiceStatus from JSON file."""
        try:
            data = json.loads(content)
            service_status = cls.from_dict(data)
            return service_status
        except Exception as e:
            raise Exception(f"parse service status failed:{str(e)}")

    @staticmethod
    def gen_service_status_path(sandbox_id: str) -> str:
        return f"{env_vars.ROCK_SERVICE_STATUS_DIR}/{sandbox_id}.json"
