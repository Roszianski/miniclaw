from pathlib import Path

from miniclaw.cli.security import run_security_audit
from miniclaw.config.loader import load_config, save_config
from miniclaw.config.schema import Config


def _status(report, key: str) -> str:
    for item in report.checks:
        if item.key == key:
            return item.status
    raise AssertionError(f"missing check {key}")


def test_security_audit_detects_policy_drift_and_fixes(tmp_path: Path) -> None:
    cfg_path = tmp_path / ".miniclaw" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    config = Config()
    config.tools.approval.exec = "always_allow"
    config.tools.approval.browser = "always_allow"
    config.plugins.manifest_required = False
    config.plugins.signature_mode = "off"
    config.alerts.enabled = False
    config.webhooks.enabled = True
    config.webhooks.replay_window_s = 5000
    save_config(config, cfg_path)

    report = run_security_audit(fix=False, config_path=cfg_path)
    assert _status(report, "tools.approval_policy") == "error"
    assert _status(report, "plugins.manifest_required") == "error"
    assert report.has_errors is True

    fixed = run_security_audit(fix=True, config_path=cfg_path)
    assert fixed.has_errors is False

    updated = load_config(cfg_path)
    assert updated.tools.approval.exec != "always_allow"
    assert updated.tools.approval.browser != "always_allow"
    assert updated.plugins.manifest_required is True
    assert updated.plugins.signature_mode != "off"
    assert updated.webhooks.replay_window_s <= 900
