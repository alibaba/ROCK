"""FC Operator configuration classes."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rock.config import FCConfig


class FCOperatorConfig(BaseModel):
    """Configuration for FC Operator sandbox operations."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["fc"] = "fc"
    session_id: str | None = None
    function_name: str | None = None
    region: str | None = None
    account_id: str | None = None
    access_key_id: str | None = None
    access_key_secret: str | None = Field(default=None, repr=False, exclude=True)
    security_token: str | None = None
    image: str | None = None
    env: dict[str, str] | None = None
    memory: int | None = None
    cpus: float | None = None
    session_ttl: int | None = None
    session_idle_timeout: int | None = None
    function_timeout: float | None = None
    trigger_url: str | None = None
    extended_params: dict[str, str] = Field(default_factory=dict)

    def template_hash(self) -> str:
        import hashlib
        import json

        hash_fields = {
            "image": self.image,
            "memory": self.memory,
            "cpus": self.cpus,
            "env": self.env,
            "session_ttl": self.session_ttl,
            "session_idle_timeout": self.session_idle_timeout,
            "function_timeout": self.function_timeout,
        }
        hash_str = json.dumps(hash_fields, sort_keys=True, default=str)
        return hashlib.sha256(hash_str.encode()).hexdigest()[:16]

    def merge_with_fc_config(self, fc_config: FCConfig) -> "FCOperatorConfig":
        return FCOperatorConfig(
            type=self.type,
            session_id=self.session_id,
            function_name=self.function_name or fc_config.function_name,
            region=self.region or fc_config.region,
            account_id=self.account_id or fc_config.account_id,
            access_key_id=self.access_key_id or fc_config.access_key_id,
            access_key_secret=self.access_key_secret or fc_config.access_key_secret,
            security_token=self.security_token or fc_config.security_token,
            image=self.image,
            env=self.env,
            memory=self.memory or fc_config.default_memory,
            cpus=self.cpus or fc_config.default_cpus,
            session_ttl=self.session_ttl or fc_config.default_session_ttl,
            session_idle_timeout=self.session_idle_timeout or fc_config.default_session_idle_timeout,
            function_timeout=self.function_timeout or fc_config.default_function_timeout,
            trigger_url=self.trigger_url,
            extended_params=self.extended_params,
        )
