import asyncio
from pathlib import Path

from miniclaw.bus.queue import MessageBus
from miniclaw.workflows.runtime import LinearWorkflowRuntime, WorkflowRecipe


class FakeAgentRuntime:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def process_direct(
        self,
        content: str,
        session_key: str = "workflow:test",
        channel: str = "system",
        chat_id: str = "workflow",
        model_override: str | None = None,
    ) -> str:
        self.calls.append((content, session_key, channel, chat_id, model_override))
        if not self.responses:
            return ""
        return str(self.responses.pop(0))


class TrackingAgentRuntime:
    def __init__(self, delay_s: float = 0.05):
        self.delay_s = delay_s
        self.current = 0
        self.max_seen = 0
        self.calls = []

    async def process_direct(
        self,
        content: str,
        session_key: str = "workflow:test",
        channel: str = "system",
        chat_id: str = "workflow",
        model_override: str | None = None,
    ) -> str:
        self.calls.append(content)
        self.current += 1
        self.max_seen = max(self.max_seen, self.current)
        await asyncio.sleep(self.delay_s)
        self.current -= 1
        return f"ok:{content}"


async def test_linear_workflow_retries_and_approval_gate(tmp_path: Path) -> None:
    bus = MessageBus()
    runtime = FakeAgentRuntime(["", "step1 ok", "step2 ok"])
    wf = LinearWorkflowRuntime(
        agent_runtime=runtime,
        bus=bus,
        workspace=tmp_path,
        approval_session_key="dashboard:approvals",
    )

    recipe = WorkflowRecipe.from_dict(
        {
            "name": "demo",
            "steps": [
                {
                    "id": "first",
                    "prompt": "first prompt",
                    "retry_max_attempts": 2,
                    "retry_backoff_ms": 1,
                },
                {
                    "id": "second",
                    "prompt": "second prompt",
                    "require_approval": True,
                },
            ],
        }
    )

    async def approve_later():
        await asyncio.sleep(0.05)
        bus.submit_response("dashboard:approvals", "approve")

    approval_task = asyncio.create_task(approve_later())
    result = await wf.run_recipe(recipe)
    await approval_task

    assert result["status"] == "completed"
    assert result["steps"][0]["attempts"] == 2
    assert result["steps"][0]["status"] == "ok"
    assert result["steps"][1]["status"] == "ok"
    assert len(runtime.calls) == 3


async def test_dag_workflow_runs_parallel_branches_and_merges_outputs(tmp_path: Path) -> None:
    runtime = TrackingAgentRuntime(delay_s=0.05)
    wf = LinearWorkflowRuntime(agent_runtime=runtime, workspace=tmp_path)

    recipe = WorkflowRecipe.from_dict(
        {
            "name": "dag-demo",
            "mode": "dag",
            "max_parallel": 4,
            "steps": [
                {"id": "seed", "prompt": "seed"},
                {"id": "left", "prompt": "left uses {seed_output}", "depends_on": ["seed"]},
                {"id": "right", "prompt": "right uses {seed_output}", "depends_on": ["seed"]},
                {
                    "id": "merge",
                    "prompt": "merge {left_output} + {right_output}",
                    "depends_on": ["left", "right"],
                },
            ],
        }
    )

    result = await wf.run_recipe(recipe)

    assert result["status"] == "completed"
    assert result["mode"] == "dag"
    assert [step["id"] for step in result["steps"]] == ["seed", "left", "right", "merge"]
    assert all(step["status"] == "ok" for step in result["steps"])
    assert runtime.max_seen >= 2


async def test_dag_workflow_skips_downstream_on_dependency_failure(tmp_path: Path) -> None:
    runtime = FakeAgentRuntime(["", "independent ok"])
    wf = LinearWorkflowRuntime(agent_runtime=runtime, workspace=tmp_path)

    recipe = WorkflowRecipe.from_dict(
        {
            "name": "dag-failure",
            "mode": "dag",
            "steps": [
                {"id": "seed", "prompt": "seed", "retry_max_attempts": 1, "on_failure": "continue"},
                {"id": "dependent", "prompt": "needs {seed_output}", "depends_on": ["seed"]},
                {"id": "independent", "prompt": "independent"},
            ],
        }
    )

    result = await wf.run_recipe(recipe)
    rows = {step["id"]: step for step in result["steps"]}

    assert result["status"] == "failed"
    assert rows["seed"]["status"] == "failed"
    assert rows["dependent"]["status"] == "skipped"
    assert rows["dependent"]["reason"] == "dependency_failed"
    assert rows["independent"]["status"] == "ok"


def test_dag_recipe_rejects_cycles() -> None:
    try:
        WorkflowRecipe.from_dict(
            {
                "name": "cycle",
                "mode": "dag",
                "steps": [
                    {"id": "a", "prompt": "a", "depends_on": ["b"]},
                    {"id": "b", "prompt": "b", "depends_on": ["a"]},
                ],
            }
        )
    except ValueError as exc:
        assert "cyclic dependencies" in str(exc)
    else:
        raise AssertionError("Expected cycle validation error")
