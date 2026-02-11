"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings


class WhatsAppConfig(BaseModel):
    """WhatsApp channel configuration."""
    enabled: bool = False
    bridge_url: str = "ws://127.0.0.1:3001"
    bridge_host: str = "127.0.0.1"
    bridge_auth_token: str = ""
    allow_from: list[str] = Field(default_factory=list)  # Allowed phone numbers


class TelegramConfig(BaseModel):
    """Telegram channel configuration."""
    enabled: bool = False
    token: str = ""  # Bot token from @BotFather
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs or usernames
    proxy: str | None = None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"


class ChannelsConfig(BaseModel):
    """Configuration for chat channels."""
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


class ToolApprovalConfig(BaseModel):
    """Per-tool approval policy."""

    exec: str = "always_ask"  # "always_allow" | "always_ask" | "always_deny"
    browser: str = "always_ask"
    web_fetch: str = "always_allow"
    write_file: str = "always_ask"

    @classmethod
    def from_profile(
        cls,
        profile: Literal["coding", "messaging", "automation", "locked_down"] = "coding",
    ) -> "ToolApprovalConfig":
        if profile == "messaging":
            return cls(exec="always_deny", browser="always_deny", web_fetch="always_allow", write_file="always_deny")
        if profile == "automation":
            return cls(exec="always_allow", browser="always_deny", web_fetch="always_allow", write_file="always_ask")
        if profile == "locked_down":
            return cls(exec="always_deny", browser="always_deny", web_fetch="always_deny", write_file="always_deny")
        return cls(exec="always_ask", browser="always_ask", web_fetch="always_allow", write_file="always_ask")


class QueueConfig(BaseModel):
    """Run queue/backpressure configuration."""

    model_config = ConfigDict(populate_by_name=True)

    global_: bool = Field(default=False, alias="global")
    max_concurrency: int = 4
    mode: Literal["queue", "collect", "steer", "followup", "steer_backlog"] = "queue"
    collect_window_ms: int = Field(default=1200, ge=100, le=60_000)
    max_backlog: int = Field(default=8, ge=1, le=200)

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value: str) -> str:
        if isinstance(value, str):
            normalized = value.strip().lower().replace("-", "_")
            if normalized:
                return normalized
        return "queue"


class AgentDefaults(BaseModel):
    """Default agent configuration."""
    workspace: str = "~/.miniclaw/workspace"
    model: str = "anthropic/claude-opus-4-5"
    context_window: int = 32768
    max_tokens: int = 8192
    temperature: float = 0.7
    max_tool_iterations: int = 20
    thinking: str = "off"  # "off" | "low" | "medium" | "high"
    embedding_model: str = ""
    supports_vision: bool = True
    timeout_seconds: int = 180
    stream_events: bool = True
    queue: QueueConfig = Field(default_factory=QueueConfig)
    credential_scope: str = "shared"
    reply_shaping: bool = True
    no_reply_token: str = "NO_REPLY"


class AgentInstanceConfig(BaseModel):
    """Per-agent instance overrides for multi-agent routing."""

    id: str = "default"
    model: str | None = None
    thinking: Literal["off", "low", "medium", "high"] | None = None
    context_window: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = None
    max_tool_iterations: int | None = Field(default=None, ge=1)
    embedding_model: str | None = None
    supports_vision: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)
    stream_events: bool | None = None
    queue: QueueConfig | None = None
    credential_scope: str | None = None
    reply_shaping: bool | None = None
    no_reply_token: str | None = None


class AgentRoutingRule(BaseModel):
    """Deterministic top-down routing rule."""

    model_config = ConfigDict(populate_by_name=True)

    agent: str = "default"
    channel: str | list[str] | None = None
    chat_id: str | list[str] | None = Field(default=None, alias="chatId")
    sender_id: str | list[str] | None = Field(default=None, alias="senderId")
    is_group: bool | None = Field(default=None, alias="isGroup")


class AgentRoutingConfig(BaseModel):
    """Routing configuration for multi-agent dispatch."""

    rules: list[AgentRoutingRule] = Field(default_factory=list)


class AgentsConfig(BaseModel):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)
    instances: list[AgentInstanceConfig] = Field(default_factory=list)
    routing: AgentRoutingConfig = Field(default_factory=AgentRoutingConfig)

    @model_validator(mode="after")
    def validate_instances_and_routing(self) -> "AgentsConfig":
        if len(self.instances) > 3:
            raise ValueError("agents.instances supports at most 3 instances.")

        instance_ids = [inst.id.strip() for inst in self.instances]
        if any(not agent_id for agent_id in instance_ids):
            raise ValueError("agents.instances entries must have a non-empty id.")
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("agents.instances contains duplicate ids.")

        if self.instances and "default" not in instance_ids:
            raise ValueError("agents.instances must include an instance with id 'default'.")

        known_agent_ids = set(instance_ids) if instance_ids else {"default"}
        for rule in self.routing.rules:
            target = rule.agent.strip()
            if not target:
                raise ValueError("agents.routing.rules entries must include a non-empty agent id.")
            if target not in known_agent_ids:
                raise ValueError(f"agents.routing.rules references unknown agent '{target}'.")

        return self


class ProviderConfig(BaseModel):
    """LLM provider configuration."""
    api_key: str = ""
    auth_mode: Literal["oauth", "api_key"] = "api_key"
    oauth_token_ref: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProviderFailoverPolicyConfig(BaseModel):
    """Retry/backoff policy for provider failover."""

    max_attempts: int = Field(default=2, ge=1, le=8)
    base_backoff_ms: int = Field(default=350, ge=0, le=60_000)
    max_backoff_ms: int = Field(default=5000, ge=1, le=120_000)


class ProviderFailoverConfig(BaseModel):
    """Failover settings with per-provider/per-model overrides."""

    enabled: bool = True
    default: ProviderFailoverPolicyConfig = Field(default_factory=ProviderFailoverPolicyConfig)
    provider_overrides: dict[str, ProviderFailoverPolicyConfig] = Field(default_factory=dict)
    model_overrides: dict[str, ProviderFailoverPolicyConfig] = Field(default_factory=dict)


class ProvidersConfig(BaseModel):
    """Configuration for LLM providers."""
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)  # Qwen
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    failover: ProviderFailoverConfig = Field(default_factory=ProviderFailoverConfig)


class GatewayConfig(BaseModel):
    """Gateway/server configuration."""
    host: str = "0.0.0.0"
    port: int = 18790


class OpenAICompatRateLimitsConfig(BaseModel):
    """Rate limits for OpenAI-compatible API."""

    requests_per_minute: int = Field(default=120, ge=1)
    tokens_per_minute: int = Field(default=120_000, ge=1)


class OpenAICompatConfig(BaseModel):
    """OpenAI compatibility API server configuration."""

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 18800
    auth_token: str = ""
    max_audio_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)
    rate_limits: OpenAICompatRateLimitsConfig = Field(default_factory=OpenAICompatRateLimitsConfig)


class APIConfig(BaseModel):
    """API surface configuration."""

    openai_compat: OpenAICompatConfig = Field(default_factory=OpenAICompatConfig)


class WebhooksConfig(BaseModel):
    """Webhook ingestion and verification settings."""

    class ActionRule(BaseModel):
        """Map webhook events to agent/workflow actions."""

        source: str = "*"  # source slug or wildcard
        event: str = "*"  # event type or wildcard
        mode: Literal["agent", "workflow"] = "agent"
        target: str = ""
        message_template: str = (
            "Webhook event {event} from {source}\n\nPayload:\n{payload_json}"
        )
        session_key: str = "webhook:{source}"

    enabled: bool = False
    secret_refs: dict[str, str] = Field(default_factory=dict)
    allowed_events: list[str] = Field(default_factory=list)
    replay_window_s: int = Field(default=300, ge=1, le=86_400)
    rules: list[ActionRule] = Field(default_factory=list)


class SessionsPolicyConfig(BaseModel):
    """Session lifecycle policy."""

    idle_reset_minutes: int = Field(default=0, ge=0)
    scheduled_reset_cron: str = ""


class RetentionConfig(BaseModel):
    """Per-domain retention windows (days)."""

    default_days: int = Field(default=60, ge=1)
    sessions_days: int | None = Field(default=None, ge=1)
    runs_days: int | None = Field(default=None, ge=1)
    audit_days: int | None = Field(default=None, ge=1)
    memory_days: int | None = Field(default=None, ge=1)


class PluginsConfig(BaseModel):
    """Plugin distribution and trust settings."""

    allow_git: bool = True
    allow_local: bool = True
    manifest_required: bool = True
    signature_mode: Literal["off", "optional", "required"] = "optional"


class WorkflowsConfig(BaseModel):
    """Workflow runtime settings."""

    enabled: bool = True
    path: str = "workspace/workflows"
    approval_session_key: str = "dashboard:approvals"


class DistributedMTLSConfig(BaseModel):
    """mTLS configuration for distributed nodes."""

    enabled: bool = False
    cert_ref: str = ""
    key_ref: str = ""
    ca_ref: str = ""


class DistributedConfig(BaseModel):
    """Distributed worker/node configuration."""

    enabled: bool = False
    node_id: str = "local-node"
    peer_allowlist: list[str] = Field(default_factory=list)
    heartbeat_timeout_s: int = Field(default=90, ge=15, le=3600)
    max_tasks: int = Field(default=1000, ge=100, le=100_000)
    mtls: DistributedMTLSConfig = Field(default_factory=DistributedMTLSConfig)


class AlertRuleConfig(BaseModel):
    """Alert dispatch rule."""

    event: str
    channels: list[str] = Field(default_factory=list)


class AlertsConfig(BaseModel):
    """Monitoring and alerting targets/rules."""

    enabled: bool = False
    channels: dict[str, str] = Field(default_factory=dict)
    rules: list[AlertRuleConfig] = Field(default_factory=list)


class UsagePriceConfig(BaseModel):
    """Per-model pricing unit."""

    input_per_1m_tokens_usd: float = 0.0
    output_per_1m_tokens_usd: float = 0.0


class UsageConfig(BaseModel):
    """Usage/cost aggregation settings."""

    pricing: dict[str, UsagePriceConfig] = Field(default_factory=dict)
    aggregation_windows: list[str] = Field(default_factory=lambda: ["1h", "1d", "30d"])


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""
    api_key: str = ""  # Brave Search API key
    max_results: int = 5


class WebToolsConfig(BaseModel):
    """Web tools configuration."""
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecResourceLimitsConfig(BaseModel):
    """Resource limits for shell execution."""
    cpu_seconds: int = Field(default=30, ge=1)
    memory_mb: int = Field(default=512, ge=1)
    file_size_mb: int = Field(default=64, ge=1)
    max_processes: int = Field(default=64, ge=1)


class ExecToolConfig(BaseModel):
    """Shell exec tool configuration."""
    timeout: int = 60
    resource_limits: ExecResourceLimitsConfig = Field(default_factory=ExecResourceLimitsConfig)


class SandboxConfig(BaseModel):
    """Docker sandbox execution configuration."""

    mode: Literal["off", "non_main", "all"] = "all"
    scope: Literal["session", "agent", "shared"] = "agent"
    workspace_access: Literal["none", "ro", "rw"] = "rw"
    image: str = "openclaw-sandbox:bookworm-slim"
    prune_idle_seconds: int = Field(default=1800, ge=30)
    prune_max_age_seconds: int = Field(default=21600, ge=60)

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value: str) -> str:
        if isinstance(value, str):
            normalized = value.strip().lower().replace("-", "_")
            if normalized:
                return normalized
        return "all"


class ToolsConfig(BaseModel):
    """Tools configuration."""
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    restrict_to_workspace: bool = True
    approval_profile: Literal["coding", "messaging", "automation", "locked_down"] = "coding"
    approval: ToolApprovalConfig = Field(default_factory=ToolApprovalConfig)

    @field_validator("sandbox", mode="before")
    @classmethod
    def normalize_sandbox_legacy_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            return {"mode": "all" if value else "off"}
        return value

    @model_validator(mode="after")
    def normalize_approval_profile(self) -> "ToolsConfig":
        # If approval values are untouched/default, refresh from profile.
        approval_set = "approval" in self.model_fields_set
        if not approval_set:
            self.approval = ToolApprovalConfig.from_profile(self.approval_profile)
        return self


class AuditConfig(BaseModel):
    """Audit logging configuration."""
    enabled: bool = False
    level: str = "standard"  # "minimal" | "standard" | "verbose"


class RateLimitConfig(BaseModel):
    """Rate limiting configuration."""
    enabled: bool = False
    messages_per_minute: int = 20
    tool_calls_per_minute: int = 60


class DashboardConfig(BaseModel):
    """Web admin dashboard configuration."""
    enabled: bool = True
    port: int = 18791
    token: str = ""  # Auto-generated if empty


class LocalWhisperConfig(BaseModel):
    """Local whisper.cpp transcription configuration."""

    enabled: bool = False
    cli: str = "whisper-cli"
    model_path: str = "~/.miniclaw/models/whisper-small.en.bin"


class TranscriptionConfig(BaseModel):
    """Voice transcription settings."""

    local_whisper: LocalWhisperConfig = Field(default_factory=LocalWhisperConfig)
    groq_fallback: bool = True

    class TTSConfig(BaseModel):
        """Text-to-speech output settings."""

        enabled: bool = False
        engine: Literal["kokoro", "mock"] = "kokoro"
        output_dir: str = "~/.miniclaw/tts"
        default_voice: str = "af_sky"

    tts: TTSConfig = Field(default_factory=TTSConfig)


class ServiceConfig(BaseModel):
    """User service management configuration."""
    enabled: bool = False
    auto_start: bool = False
    log_retention_days: int = Field(default=60, ge=1)


class IdentityConfig(BaseModel):
    """Identity and pairing settings."""

    enabled: bool = True
    owner_user_id: str = "owner"
    pairing_code_ttl_s: int = Field(default=600, ge=60, le=86_400)


class HooksConfig(BaseModel):
    """Lifecycle hook system configuration."""

    enabled: bool = False
    path: str = "workspace/hooks"
    config_file: str = "hooks.json"
    timeout_seconds: int = 8
    safe_mode: bool = True
    allow_command_prefixes: list[str] = Field(default_factory=list)
    deny_command_patterns: list[str] = Field(
        default_factory=lambda: [
            "rm -rf /",
            "mkfs",
            "shutdown",
            "reboot",
            "poweroff",
            ":(){:|:&};:",
        ]
    )


class Config(BaseSettings):
    """Root configuration for miniclaw."""
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    webhooks: WebhooksConfig = Field(default_factory=WebhooksConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    sessions: SessionsPolicyConfig = Field(default_factory=SessionsPolicyConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    workflows: WorkflowsConfig = Field(default_factory=WorkflowsConfig)
    distributed: DistributedConfig = Field(default_factory=DistributedConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    usage: UsageConfig = Field(default_factory=UsageConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    # Default base URLs for API gateways
    _GATEWAY_DEFAULTS = {"openrouter": "https://openrouter.ai/api/v1", "aihubmix": "https://aihubmix.com/v1"}

    @staticmethod
    def _provider_has_credentials(provider: ProviderConfig) -> bool:
        return bool(
            provider.api_key
            or provider.api_base
            or (provider.auth_mode == "oauth" and provider.oauth_token_ref)
        )

    @staticmethod
    def _provider_name_from_model(model: str) -> str | None:
        model = (model or "").lower()
        keyword_map = [
            ("aihubmix", "aihubmix"),
            ("openrouter", "openrouter"),
            ("deepseek", "deepseek"),
            ("anthropic", "anthropic"),
            ("claude", "anthropic"),
            ("openai", "openai"),
            ("gpt", "openai"),
            ("gemini", "gemini"),
            ("zhipu", "zhipu"),
            ("glm", "zhipu"),
            ("zai", "zhipu"),
            ("dashscope", "dashscope"),
            ("qwen", "dashscope"),
            ("groq", "groq"),
            ("moonshot", "moonshot"),
            ("kimi", "moonshot"),
            ("vllm", "vllm"),
        ]
        for keyword, name in keyword_map:
            if keyword in model:
                return name
        return None

    def get_provider_candidates(self, model: str | None = None) -> list[str]:
        """Return deterministic provider candidate order for runtime fallback."""
        model = (model or self.agents.defaults.model).lower()
        primary = self._provider_name_from_model(model)
        fallback_order = [
            "openrouter",
            "aihubmix",
            "anthropic",
            "openai",
            "deepseek",
            "gemini",
            "zhipu",
            "dashscope",
            "moonshot",
            "vllm",
            "groq",
        ]
        out: list[str] = []
        if primary:
            out.append(primary)
        for name in fallback_order:
            if name not in out:
                out.append(name)
        return out

    def get_provider_with_name(self, model: str | None = None) -> tuple[str, ProviderConfig] | None:
        """Resolve provider name + config for a model, with gateway-first fallback."""
        p = self.providers
        for name in self.get_provider_candidates(model):
            provider = getattr(p, name)
            if self._provider_has_credentials(provider):
                return name, provider
        return None

    def get_provider_name(self, model: str | None = None) -> str | None:
        """Get provider name for the given model."""
        resolved = self.get_provider_with_name(model)
        return resolved[0] if resolved else None

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        """Get matched provider config. Falls back to first configured provider."""
        resolved = self.get_provider_with_name(model)
        return resolved[1] if resolved else None

    def get_api_key(self, model: str | None = None) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model)
        return p.api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        """Get API base URL for the given model. Applies default URLs for known gateways."""
        resolved = self.get_provider_with_name(model)
        if not resolved:
            return None
        provider_name, provider = resolved
        return self.get_api_base_for_provider(provider_name, provider=provider)

    def get_api_base_for_provider(self, provider_name: str, provider: ProviderConfig | None = None) -> str | None:
        """Get API base URL for a specific provider."""
        if provider is None:
            provider = getattr(self.providers, provider_name)
        if provider.api_base:
            return provider.api_base
        if provider_name in self._GATEWAY_DEFAULTS:
            return self._GATEWAY_DEFAULTS[provider_name]
        return None

    class Config:
        env_prefix = "MINICLAW_"
        env_nested_delimiter = "__"
