from pathlib import Path

from miniclaw.agent.tools.shell import DockerSandboxManager, ExecTool, SandboxRuntimeContext


def test_docker_sandbox_run_args_hardening_and_workspace_modes(tmp_path: Path) -> None:
    manager_rw = DockerSandboxManager(
        image="openclaw-sandbox:bookworm-slim",
        scope="agent",
        workspace_access="rw",
        workspace_root=tmp_path,
        resource_limits={
            "cpu_seconds": 2,
            "memory_mb": 128,
            "file_size_mb": 8,
            "max_processes": 16,
        },
        prune_idle_seconds=60,
        prune_max_age_seconds=300,
    )
    args_rw = manager_rw._build_run_args(
        container_name="sandbox-rw",
        scope_key="agent:default",
        cwd=str(tmp_path),
    )
    joined_rw = " ".join(args_rw)
    assert "--read-only" in joined_rw
    assert "--network none" in joined_rw
    assert "--cap-drop ALL" in joined_rw
    assert "--security-opt no-new-privileges:true" in joined_rw
    assert "--user 65532:65532" in joined_rw
    assert f"-v {tmp_path}:/workspace:rw" in joined_rw

    manager_none = DockerSandboxManager(
        image="openclaw-sandbox:bookworm-slim",
        scope="agent",
        workspace_access="none",
        workspace_root=tmp_path,
        resource_limits={
            "cpu_seconds": 2,
            "memory_mb": 128,
            "file_size_mb": 8,
            "max_processes": 16,
        },
        prune_idle_seconds=60,
        prune_max_age_seconds=300,
    )
    args_none = manager_none._build_run_args(
        container_name="sandbox-none",
        scope_key="agent:default",
        cwd=str(tmp_path),
    )
    joined_none = " ".join(args_none)
    assert "/workspace:rw,nosuid,nodev,noexec" in joined_none
    assert "-v " not in joined_none


def test_docker_sandbox_payload_and_scope_keys(tmp_path: Path) -> None:
    manager = DockerSandboxManager(
        image="openclaw-sandbox:bookworm-slim",
        scope="session",
        workspace_access="ro",
        workspace_root=tmp_path,
        resource_limits={
            "cpu_seconds": 2,
            "memory_mb": 128,
            "file_size_mb": 8,
            "max_processes": 16,
        },
        prune_idle_seconds=60,
        prune_max_age_seconds=300,
    )
    payload = manager._build_limited_payload(command="echo hi", cwd=str(tmp_path))
    assert "ulimit -t 2" in payload
    assert "ulimit -v 131072" in payload
    assert "ulimit -f 16384" in payload
    assert "ulimit -u 16" in payload
    assert "cd /workspace" in payload

    key = manager._scope_key(SandboxRuntimeContext(session_key="telegram:42", agent_id="helper"))
    assert key == "session:helper:telegram:42"


async def test_fail_closed_when_docker_unavailable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("miniclaw.agent.tools.shell.shutil.which", lambda name: None)
    tool = ExecTool(
        sandbox_mode="all",
        working_dir=str(tmp_path),
    )
    result = await tool.execute("echo should_not_run")
    assert "fail-closed" in result.lower()


async def test_docker_scope_shared_reuses_single_container(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    run_count = {"docker_run": 0}

    async def fake_run_cmd(self, args, timeout):
        calls.append(list(args))
        if args[:3] == ["docker", "inspect", "-f"]:
            if run_count["docker_run"] > 0:
                return 0, "true\n", ""
            return 1, "", "No such container"
        if args[:3] == ["docker", "run", "-d"]:
            run_count["docker_run"] += 1
            return 0, "container-id\n", ""
        if args[:3] == ["docker", "exec", "-i"]:
            return 0, "ok\n", ""
        if args[:3] == ["docker", "rm", "-f"]:
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr("miniclaw.agent.tools.shell.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr("miniclaw.agent.tools.shell.DockerSandboxManager._run_cmd", fake_run_cmd)

    tool = ExecTool(
        sandbox_mode="all",
        sandbox_scope="shared",
        working_dir=str(tmp_path),
    )
    tool.set_registry_context(
        channel="telegram",
        chat_id="42",
        session_key="telegram:42",
        user_key="u1",
        run_id="r1",
    )
    first = await tool.execute("echo first")
    second = await tool.execute("echo second")

    assert "ok" in first
    assert "ok" in second
    assert run_count["docker_run"] == 1
    exec_calls = [c for c in calls if c[:3] == ["docker", "exec", "-i"]]
    assert len(exec_calls) == 2
