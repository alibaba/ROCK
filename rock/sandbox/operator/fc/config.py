"""FC Operator configuration classes."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rock.config import FCConfig


class FCOperatorConfig(BaseModel):
    """Configuration for FC Operator sandbox operations.

    FC (Function Compute) is Alibaba Cloud's serverless compute service.
    This configuration is used by FCOperator to manage WebSocket sessions
    for stateful bash operations.

    Note: FC uses Operator-level configuration, not Deployment-level.
    FCOperator manages FCRuntime directly without the Deployment pattern.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["fc"] = "fc"
    """Configuration type discriminator for OperatorFactory."""

    # Session identification
    session_id: str | None = None
    """FC session identifier (also serves as ROCK sandbox_id)."""

    # FC connection settings
    function_name: str | None = None
    """FC function name to connect to."""

    region: str | None = None
    """FC region (e.g., 'cn-hangzhou')."""

    account_id: str | None = None
    """Alibaba Cloud account ID."""

    access_key_id: str | None = None
    """Access key ID for FC API authentication."""

    access_key_secret: str | None = Field(default=None, repr=False, exclude=True)
    """Access key secret for FC API authentication."""

    security_token: str | None = None
    """STS security token (for temporary credentials)."""

    # Resource settings
    memory: int | None = None
    """Memory allocation in MB."""

    cpus: float | None = None
    """CPU allocation (number of cores)."""

    # Timeout settings (in seconds)
    session_ttl: int | None = None
    """Session lifetime in seconds."""

    session_idle_timeout: int | None = None
    """Session idle timeout in seconds."""

    function_timeout: float | None = None
    """Function execution timeout in seconds."""

    # Extension field for custom metadata
    extended_params: dict[str, str] = Field(default_factory=dict)
    """Generic extension field for custom string key-value pairs."""

    def merge_with_fc_config(self, fc_config: FCConfig) -> "FCOperatorConfig":
        """Merge this config with FCConfig defaults from RockConfig.

        Args:
            fc_config: FCConfig containing default credentials and settings.

        Returns:
            FCOperatorConfig with merged values.
        """
        return FCOperatorConfig(
            type=self.type,
            session_id=self.session_id,
            function_name=self.function_name or fc_config.function_name,
            region=self.region or fc_config.region,
            account_id=self.account_id or fc_config.account_id,
            access_key_id=self.access_key_id or fc_config.access_key_id,
            access_key_secret=self.access_key_secret or fc_config.access_key_secret,
            security_token=self.security_token or fc_config.security_token,
            memory=self.memory or fc_config.default_memory,
            cpus=self.cpus or fc_config.default_cpus,
            session_ttl=self.session_ttl or fc_config.default_session_ttl,
            session_idle_timeout=self.session_idle_timeout or fc_config.default_session_idle_timeout,
            function_timeout=self.function_timeout or fc_config.default_function_timeout,
            extended_params=self.extended_params,
        )