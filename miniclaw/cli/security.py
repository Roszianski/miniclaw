"""Security audit checks and remediation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from miniclaw.config.loader import get_config_path, load_config, save_config
from miniclaw.config.schema import ToolApprovalConfig


@dataclass
class SecurityCheck:
    key: str
    status: str  # ok|warn|error
    message: str
    fixed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "message": self.message,
            "fixed": self.fixed,
        }


@dataclass
class SecurityAuditReport:
    checks: list[SecurityCheck]

    @property
    def has_errors(self) -> bool:
        return any(item.status == "error" for item in self.checks)

    def counts(self) -> dict[str, int]:
        return {
            "ok": sum(1 for item in self.checks if item.status == "ok"),
            "warn": sum(1 for item in self.checks if item.status == "warn"),
            "error": sum(1 for item in self.checks if item.status == "error"),
            "fixed": sum(1 for item in self.checks if item.fixed),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": [item.to_dict() for item in self.checks],
            "summary": self.counts(),
            "has_errors": self.has_errors,
        }


def run_security_audit(
    *,
    fix: bool = False,
    config_path: Path | None = None,
) -> SecurityAuditReport:
    """Audit hardened config posture and optionally repair drift."""
    path = config_path or get_config_path()
    config = load_config(path)
    checks: list[SecurityCheck] = []
    changed = False

    secure_defaults_ok = bool(config.tools.sandbox.mode != "off" and config.tools.restrict_to_workspace)
    if secure_defaults_ok:
        checks.append(SecurityCheck("tools.secure_defaults", "ok", "Sandbox and workspace restrictions are enabled."))
    elif fix:
        config.tools.sandbox.mode = "all"
        config.tools.restrict_to_workspace = True
        changed = True
        checks.append(
            SecurityCheck(
                "tools.secure_defaults",
                "ok",
                "Enabled sandbox and workspace restrictions.",
                fixed=True,
            )
        )
    else:
        checks.append(
            SecurityCheck(
                "tools.secure_defaults",
                "error",
                "tools.sandbox.mode must not be off and tools.restrictToWorkspace must be true.",
            )
        )

    profile = str(config.tools.approval_profile or "coding").strip().lower()
    profile = profile if profile in {"coding", "messaging", "automation", "locked_down"} else "coding"
    expected = ToolApprovalConfig.from_profile(profile)
    current = config.tools.approval
    drift_keys = [
        key
        for key in ("exec", "browser", "web_fetch", "write_file")
        if getattr(current, key) != getattr(expected, key)
    ]
    insecure_flags = [
        name
        for name in ("exec", "browser", "write_file")
        if getattr(current, name) == "always_allow"
    ]
    if not drift_keys and not insecure_flags:
        checks.append(
            SecurityCheck("tools.approval_policy", "ok", f"Tool approval policy matches '{profile}' profile.")
        )
    elif fix:
        config.tools.approval = expected
        changed = True
        checks.append(
            SecurityCheck(
                "tools.approval_policy",
                "ok",
                f"Reset approval policy to '{profile}' profile defaults.",
                fixed=True,
            )
        )
    else:
        checks.append(
            SecurityCheck(
                "tools.approval_policy",
                "error",
                "Tool approval policy drift detected; run security audit --fix to restore profile defaults.",
            )
        )

    if config.plugins.manifest_required:
        checks.append(SecurityCheck("plugins.manifest_required", "ok", "Plugin manifest requirement is enabled."))
    elif fix:
        config.plugins.manifest_required = True
        changed = True
        checks.append(
            SecurityCheck(
                "plugins.manifest_required",
                "ok",
                "Enabled plugin manifest requirement.",
                fixed=True,
            )
        )
    else:
        checks.append(
            SecurityCheck(
                "plugins.manifest_required",
                "error",
                "plugins.manifestRequired should be true.",
            )
        )

    if str(config.plugins.signature_mode or "").strip().lower() != "off":
        checks.append(SecurityCheck("plugins.signature_mode", "ok", "Plugin signature mode is not disabled."))
    elif fix:
        config.plugins.signature_mode = "optional"
        changed = True
        checks.append(
            SecurityCheck("plugins.signature_mode", "ok", "Set plugin signature mode to 'optional'.", fixed=True)
        )
    else:
        checks.append(
            SecurityCheck(
                "plugins.signature_mode",
                "warn",
                "plugins.signatureMode is off; signatures are recommended for plugin integrity.",
            )
        )

    if not config.webhooks.enabled:
        checks.append(SecurityCheck("webhooks.replay_window", "ok", "Webhooks are disabled."))
    else:
        replay_window = int(config.webhooks.replay_window_s or 0)
        if replay_window <= 900:
            checks.append(SecurityCheck("webhooks.replay_window", "ok", f"Replay window is {replay_window}s."))
        elif fix:
            config.webhooks.replay_window_s = 900
            changed = True
            checks.append(
                SecurityCheck(
                    "webhooks.replay_window",
                    "ok",
                    "Reduced webhook replay window to 900 seconds.",
                    fixed=True,
                )
            )
        else:
            checks.append(
                SecurityCheck(
                    "webhooks.replay_window",
                    "warn",
                    f"Replay window is high ({replay_window}s); <=900s is recommended.",
                )
            )

    if not config.distributed.enabled:
        checks.append(SecurityCheck("distributed.mtls", "ok", "Distributed runtime is disabled."))
    elif config.distributed.mtls.enabled:
        checks.append(SecurityCheck("distributed.mtls", "ok", "Distributed mTLS is enabled."))
    else:
        checks.append(
            SecurityCheck(
                "distributed.mtls",
                "warn",
                "Distributed runtime is enabled without mTLS.",
            )
        )

    if config.alerts.enabled:
        checks.append(SecurityCheck("alerts.enabled", "ok", "Monitoring alerts are enabled."))
    elif fix:
        config.alerts.enabled = True
        changed = True
        checks.append(SecurityCheck("alerts.enabled", "ok", "Enabled monitoring alerts.", fixed=True))
    else:
        checks.append(
            SecurityCheck(
                "alerts.enabled",
                "warn",
                "alerts.enabled is false; operational alerts are recommended.",
            )
        )

    if changed:
        save_config(config, path)

    return SecurityAuditReport(checks=checks)
