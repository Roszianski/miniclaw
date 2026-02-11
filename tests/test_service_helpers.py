from miniclaw.cli.service import (
    SERVICE_LABEL,
    get_service_file_path,
    render_launchd_plist,
    render_systemd_unit,
    write_if_changed,
)


def test_launchd_plist_generation_contains_expected_fields(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    log_dir = tmp_path / "logs"
    content = render_launchd_plist(
        exec_args=["/usr/local/bin/miniclaw", "gateway"],
        workspace=workspace,
        log_dir=log_dir,
        auto_start=True,
    )

    assert SERVICE_LABEL in content
    assert "<key>RunAtLoad</key>" in content
    assert "<true/>" in content
    assert str(log_dir / "gateway.out.log") in content
    assert str(workspace) in content


def test_systemd_unit_generation_contains_expected_fields(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    content = render_systemd_unit(exec_args=["/usr/local/bin/miniclaw", "gateway"], workspace=workspace)

    assert "Description=miniclaw gateway" in content
    assert "ExecStart=/usr/local/bin/miniclaw gateway" in content
    assert f"WorkingDirectory={workspace}" in content
    assert "WantedBy=default.target" in content


def test_service_definition_path_switches_by_platform(tmp_path) -> None:
    mac_path = get_service_file_path(home=tmp_path, system_name="Darwin")
    linux_path = get_service_file_path(home=tmp_path, system_name="Linux")

    assert mac_path == tmp_path / "Library" / "LaunchAgents" / "com.miniclaw.gateway.plist"
    assert linux_path == tmp_path / ".config" / "systemd" / "user" / "miniclaw.service"


def test_write_if_changed_is_idempotent(tmp_path) -> None:
    path = tmp_path / "service.conf"

    assert write_if_changed(path, "one") is True
    assert path.read_text(encoding="utf-8") == "one"
    assert write_if_changed(path, "one") is False
    assert write_if_changed(path, "two") is True
    assert path.read_text(encoding="utf-8") == "two"
