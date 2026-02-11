"""Configuration loading utilities for miniclaw."""

import json
from pathlib import Path
from typing import Any

from miniclaw.config.schema import Config


def get_config_path() -> Path:
    """Get the default configuration file path."""
    return Path.home() / ".miniclaw" / "config.json"


def get_data_dir() -> Path:
    """Get the miniclaw data directory."""
    from miniclaw.utils.helpers import get_data_path
    return get_data_path()


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.
    
    Args:
        config_path: Optional path to config file. Uses default if not provided.
    
    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()
    
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            data = _migrate_config(data)
            return Config.model_validate(convert_keys(data))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")
    
    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.
    
    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # Convert to camelCase format
    data = config.model_dump()
    data = convert_to_camel(data)
    
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    if not isinstance(data, dict):
        return {}

    def _approval_defaults(profile: str) -> dict[str, str]:
        profile = (profile or "coding").strip().lower()
        if profile == "messaging":
            return {
                "exec": "always_deny",
                "browser": "always_deny",
                "webFetch": "always_allow",
                "writeFile": "always_deny",
            }
        if profile == "automation":
            return {
                "exec": "always_allow",
                "browser": "always_deny",
                "webFetch": "always_allow",
                "writeFile": "always_ask",
            }
        if profile == "locked_down":
            return {
                "exec": "always_deny",
                "browser": "always_deny",
                "webFetch": "always_deny",
                "writeFile": "always_deny",
            }
        return {
            "exec": "always_ask",
            "browser": "always_ask",
            "webFetch": "always_allow",
            "writeFile": "always_ask",
        }

    def _ensure_queue_defaults(queue_cfg: dict[str, Any]) -> None:
        if "mode" not in queue_cfg:
            queue_cfg["mode"] = "queue"
        if "collectWindowMs" not in queue_cfg:
            queue_cfg["collectWindowMs"] = 1200
        if "maxBacklog" not in queue_cfg:
            queue_cfg["maxBacklog"] = 8

    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    if not isinstance(tools, dict):
        tools = {}
        data["tools"] = tools
    exec_cfg = tools.get("exec", {})
    if not isinstance(exec_cfg, dict):
        exec_cfg = {}
        tools["exec"] = exec_cfg
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    # Secure-by-default migrations (enforced for existing installs).
    sandbox_cfg = tools.get("sandbox")
    if isinstance(sandbox_cfg, bool):
        sandbox_cfg = {"mode": "all" if sandbox_cfg else "off"}
    if not isinstance(sandbox_cfg, dict):
        sandbox_cfg = {}
    if "mode" not in sandbox_cfg:
        sandbox_cfg["mode"] = "all"
    sandbox_cfg.setdefault("scope", "agent")
    sandbox_cfg.setdefault("workspaceAccess", "rw")
    sandbox_cfg.setdefault("image", "openclaw-sandbox:bookworm-slim")
    sandbox_cfg.setdefault("pruneIdleSeconds", 1800)
    sandbox_cfg.setdefault("pruneMaxAgeSeconds", 21600)
    tools["sandbox"] = sandbox_cfg
    tools["restrictToWorkspace"] = True
    tools.setdefault("approvalProfile", "coding")
    if "approval" not in tools:
        tools["approval"] = _approval_defaults(str(tools.get("approvalProfile", "coding")))
    elif isinstance(tools["approval"], dict):
        defaults = _approval_defaults(str(tools.get("approvalProfile", "coding")))
        for key, value in defaults.items():
            tools["approval"].setdefault(key, value)

    # Queue mode defaults.
    agents = data.get("agents", {})
    if not isinstance(agents, dict):
        agents = {}
        data["agents"] = agents
    defaults = agents.get("defaults", {})
    if isinstance(defaults, dict):
        queue_cfg = defaults.get("queue")
        if not isinstance(queue_cfg, dict):
            queue_cfg = {}
            defaults["queue"] = queue_cfg
        _ensure_queue_defaults(queue_cfg)
        defaults.setdefault("credentialScope", "shared")
    instances = agents.get("instances")
    if isinstance(instances, list):
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            if "queue" in instance and isinstance(instance["queue"], dict):
                _ensure_queue_defaults(instance["queue"])
            instance.setdefault("credentialScope", defaults.get("credentialScope", "shared"))

    channels = data.setdefault("channels", {})
    if isinstance(channels, dict):
        whatsapp = channels.setdefault("whatsapp", {})
        if isinstance(whatsapp, dict):
            whatsapp.setdefault("bridgeUrl", "ws://127.0.0.1:3001")
            whatsapp.setdefault("bridgeHost", "127.0.0.1")
            whatsapp.setdefault("bridgeAuthToken", "")
            whatsapp.setdefault("allowFrom", [])

    # New config surfaces.
    api = data.setdefault("api", {})
    if isinstance(api, dict):
        openai_compat = api.setdefault("openaiCompat", {})
        if isinstance(openai_compat, dict):
            openai_compat.setdefault("enabled", False)
            openai_compat.setdefault("host", "0.0.0.0")
            openai_compat.setdefault("port", 18800)
            openai_compat.setdefault("authToken", "")
            openai_compat.setdefault("maxAudioUploadBytes", 25 * 1024 * 1024)
            rate_limits = openai_compat.setdefault("rateLimits", {})
            if isinstance(rate_limits, dict):
                rate_limits.setdefault("requestsPerMinute", 120)
                rate_limits.setdefault("tokensPerMinute", 120000)

    webhooks = data.setdefault("webhooks", {})
    if isinstance(webhooks, dict):
        webhooks.setdefault("enabled", False)
        webhooks.setdefault("secretRefs", {})
        webhooks.setdefault("allowedEvents", [])
        webhooks.setdefault("replayWindowS", 300)
        webhooks.setdefault("rules", [])

    sessions = data.setdefault("sessions", {})
    if isinstance(sessions, dict):
        sessions.setdefault("idleResetMinutes", 0)
        sessions.setdefault("scheduledResetCron", "")

    retention = data.setdefault("retention", {})
    if isinstance(retention, dict):
        retention.setdefault("defaultDays", 60)

    plugins = data.setdefault("plugins", {})
    if isinstance(plugins, dict):
        plugins.setdefault("allowGit", True)
        plugins.setdefault("allowLocal", True)
        plugins.setdefault("manifestRequired", True)
        plugins.setdefault("signatureMode", "optional")

    workflows = data.setdefault("workflows", {})
    if isinstance(workflows, dict):
        workflows.setdefault("enabled", True)
        workflows.setdefault("path", "workspace/workflows")
        workflows.setdefault("approvalSessionKey", "dashboard:approvals")

    distributed = data.setdefault("distributed", {})
    if isinstance(distributed, dict):
        distributed.setdefault("enabled", False)
        distributed.setdefault("nodeId", "local-node")
        distributed.setdefault("peerAllowlist", [])
        distributed.setdefault("heartbeatTimeoutS", 90)
        distributed.setdefault("maxTasks", 1000)
        mtls = distributed.setdefault("mtls", {})
        if isinstance(mtls, dict):
            mtls.setdefault("enabled", False)
            mtls.setdefault("certRef", "")
            mtls.setdefault("keyRef", "")
            mtls.setdefault("caRef", "")

    alerts = data.setdefault("alerts", {})
    if isinstance(alerts, dict):
        alerts.setdefault("enabled", False)
        alerts.setdefault("channels", {})
        alerts.setdefault("rules", [])

    usage = data.setdefault("usage", {})
    if isinstance(usage, dict):
        usage.setdefault("pricing", {})
        usage.setdefault("aggregationWindows", ["1h", "1d", "30d"])

    providers = data.setdefault("providers", {})
    if isinstance(providers, dict):
        failover = providers.setdefault("failover", {})
        if isinstance(failover, dict):
            failover.setdefault("enabled", True)
            default = failover.setdefault("default", {})
            if isinstance(default, dict):
                default.setdefault("maxAttempts", 2)
                default.setdefault("baseBackoffMs", 350)
                default.setdefault("maxBackoffMs", 5000)
            failover.setdefault("providerOverrides", {})
            failover.setdefault("modelOverrides", {})

    transcription = data.setdefault("transcription", {})
    if isinstance(transcription, dict):
        tts = transcription.setdefault("tts", {})
        if isinstance(tts, dict):
            tts.setdefault("enabled", False)
            tts.setdefault("engine", "kokoro")
            tts.setdefault("outputDir", "~/.miniclaw/tts")
            tts.setdefault("defaultVoice", "af_sky")

    identity = data.setdefault("identity", {})
    if isinstance(identity, dict):
        identity.setdefault("enabled", True)
        identity.setdefault("ownerUserId", "owner")
        identity.setdefault("pairingCodeTtlS", 600)
    return data


def convert_keys(data: Any) -> Any:
    """Convert camelCase keys to snake_case for Pydantic."""
    if isinstance(data, dict):
        return {camel_to_snake(k): convert_keys(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_keys(item) for item in data]
    return data


def convert_to_camel(data: Any) -> Any:
    """Convert snake_case keys to camelCase."""
    if isinstance(data, dict):
        return {snake_to_camel(k): convert_to_camel(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_to_camel(item) for item in data]
    return data


def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    result = []
    for i, char in enumerate(name):
        if char.isupper() and i > 0:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])
