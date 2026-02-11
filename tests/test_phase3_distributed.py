from pathlib import Path

from fastapi.testclient import TestClient

from miniclaw.config.schema import Config
from miniclaw.dashboard.app import create_app
from miniclaw.distributed.manager import DistributedNodeManager


class _FakeBus:
    def list_pending_approvals(self):
        return []

    def submit_response(self, session_key: str, content: str, approval_id: str | None = None):
        return False


def test_distributed_manager_registration_dispatch_claim_and_complete(tmp_path: Path) -> None:
    manager = DistributedNodeManager(
        store_path=tmp_path / "distributed" / "state.json",
        local_node_id="local-node",
        peer_allowlist=[],
        heartbeat_timeout_s=60,
        max_tasks=1000,
    )
    local = manager.register_node(node_id="local-node", capabilities=["agent", "workflow"])
    remote = manager.register_node(node_id="worker-1", capabilities=["agent", "gpu"])
    assert local["node_id"] == "local-node"
    assert remote["node_id"] == "worker-1"

    manager.heartbeat(node_id="worker-1", capabilities=["agent", "gpu", "tool"])
    task = manager.dispatch_task(
        payload={"instruction": "summarize"},
        required_capabilities=["gpu"],
        preferred_node_id="worker-1",
        kind="inference",
    )
    assert task["assigned_node_id"] == "worker-1"

    claimed = manager.claim_task(node_id="worker-1")
    assert claimed is not None
    assert claimed["task_id"] == task["task_id"]
    assert claimed["status"] == "running"

    completed = manager.complete_task(
        task_id=task["task_id"],
        node_id="worker-1",
        result={"output": "done"},
    )
    assert completed["status"] == "completed"
    fetched = manager.get_task(task["task_id"])
    assert fetched is not None
    assert fetched["result"]["output"] == "done"


def test_distributed_manager_preserves_state_across_multiple_instances(tmp_path: Path) -> None:
    store_path = tmp_path / "distributed" / "state.json"
    manager_a = DistributedNodeManager(store_path=store_path, local_node_id="node-a")
    manager_b = DistributedNodeManager(store_path=store_path, local_node_id="node-b")

    manager_a.register_node(node_id="worker-a", capabilities=["agent"])
    manager_b.register_node(node_id="worker-b", capabilities=["gpu"])

    nodes = manager_a.list_nodes(include_stale=True)
    node_ids = {row["node_id"] for row in nodes}
    assert "worker-a" in node_ids
    assert "worker-b" in node_ids


def test_distributed_dashboard_endpoints(tmp_path: Path) -> None:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    config.distributed.enabled = True
    config.distributed.node_id = "local-node"
    manager = DistributedNodeManager(
        store_path=tmp_path / "distributed" / "state.json",
        local_node_id="local-node",
        peer_allowlist=[],
        heartbeat_timeout_s=config.distributed.heartbeat_timeout_s,
        max_tasks=config.distributed.max_tasks,
    )

    app = create_app(
        config=config,
        config_path=tmp_path / "config.json",
        token="t",
        bus=_FakeBus(),
        distributed_manager=manager,
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer t"}

    registered = client.post(
        "/api/distributed/nodes/register",
        headers=headers,
        json={"node_id": "worker-2", "capabilities": ["agent", "gpu"], "address": "w2"},
    )
    assert registered.status_code == 200
    assert registered.json()["ok"] is True

    dispatch = client.post(
        "/api/distributed/tasks/dispatch",
        headers=headers,
        json={"payload": {"hello": "world"}, "required_capabilities": ["gpu"], "kind": "job"},
    )
    assert dispatch.status_code == 200
    task = dispatch.json()["task"]
    assert task["assigned_node_id"] == "worker-2"

    claim = client.post(f"/api/distributed/nodes/worker-2/tasks/claim", headers=headers)
    assert claim.status_code == 200
    assert claim.json()["task"]["task_id"] == task["task_id"]

    complete = client.post(
        f"/api/distributed/tasks/{task['task_id']}/complete",
        headers=headers,
        json={"node_id": "worker-2", "result": {"ok": True}},
    )
    assert complete.status_code == 200
    assert complete.json()["task"]["status"] == "completed"

    nodes = client.get("/api/distributed/nodes", headers=headers)
    assert nodes.status_code == 200
    assert nodes.json()["ok"] is True
    assert len(nodes.json()["nodes"]) >= 1


def test_distributed_prune_never_drops_active_tasks(tmp_path: Path) -> None:
    manager = DistributedNodeManager(
        store_path=tmp_path / "distributed" / "state.json",
        local_node_id="local-node",
        peer_allowlist=[],
        heartbeat_timeout_s=60,
        max_tasks=100,
    )
    manager.register_node(node_id="worker-1", capabilities=["agent"])

    protected = manager.dispatch_task(
        payload={"job": "protected"},
        required_capabilities=["agent"],
        preferred_node_id="worker-1",
        kind="protected",
    )
    claimed_protected = manager.claim_task(node_id="worker-1")
    assert claimed_protected is not None
    assert claimed_protected["task_id"] == protected["task_id"]
    assert claimed_protected["status"] == "running"

    for idx in range(130):
        manager.dispatch_task(
            payload={"job": idx},
            required_capabilities=["agent"],
            preferred_node_id="worker-1",
            kind="batch",
        )
        claimed = manager.claim_task(node_id="worker-1")
        assert claimed is not None
        manager.complete_task(task_id=claimed["task_id"], node_id="worker-1", result={"ok": idx})

    before_completion = manager.get_task(protected["task_id"])
    assert before_completion is not None
    assert before_completion["status"] == "running"

    completed_protected = manager.complete_task(
        task_id=protected["task_id"],
        node_id="worker-1",
        result={"ok": "protected"},
    )
    assert completed_protected["status"] == "completed"
    assert manager.get_task(protected["task_id"]) is not None
