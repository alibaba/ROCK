from typing import Literal

from pydantic import BaseModel, ConfigDict

from rock.actions import AbstractSandbox


class RockletConfig(BaseModel):
    """Configuration for Rocklet (local sandbox runtime)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["rocklet"] = "rocklet"
    """Runtime type discriminator for serialization/deserialization and CLI parsing. Should not be modified."""

    def get_sandbox_runtime(self) -> AbstractSandbox:
        from rock.rocklet.rocklet import Rocklet

        return Rocklet.from_config(self)


class RemoteSandboxRuntimeConfig(BaseModel):
    host: str = "http://127.0.0.1"
    """Remote sandbox host URL or IP address."""

    port: int | None = None
    """Remote sandbox port number. If None, uses default port."""

    timeout: float = 0.15
    """Connection timeout in seconds for remote sandbox operations."""

    type: Literal["remote"] = "remote"
    """Runtime type discriminator for serialization/deserialization and CLI parsing. Should not be modified."""

    model_config = ConfigDict(extra="forbid")

    def get_sandbox_runtime(self) -> AbstractSandbox:
        from rock.sandbox.remote_sandbox import RemoteSandboxRuntime

        return RemoteSandboxRuntime.from_config(self)


# Union type for all supported sandbox runtime configurations
SandboxRuntimeConfig = RockletConfig | RemoteSandboxRuntimeConfig
