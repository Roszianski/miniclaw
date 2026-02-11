"""Workflow runtime with linear and DAG execution support."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass
class WorkflowStep:
    """Single workflow step."""

    id: str
    prompt: str
    retry_max_attempts: int = 1
    retry_backoff_ms: int = 750
    require_approval: bool = False
    on_failure: Literal["stop", "continue"] = "stop"
    depends_on: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, index: int) -> "WorkflowStep":
        raw_deps = data.get("depends_on", data.get("dependsOn", []))
        if isinstance(raw_deps, str):
            deps = [raw_deps.strip()] if raw_deps.strip() else []
        elif isinstance(raw_deps, list):
            deps = [str(item).strip() for item in raw_deps if str(item).strip()]
        else:
            deps = []

        return cls(
            id=str(data.get("id") or f"step_{index}"),
            prompt=str(data.get("prompt") or data.get("message") or "").strip(),
            retry_max_attempts=max(1, int(data.get("retry_max_attempts", data.get("retryMaxAttempts", 1)) or 1)),
            retry_backoff_ms=max(0, int(data.get("retry_backoff_ms", data.get("retryBackoffMs", 750)) or 0)),
            require_approval=bool(data.get("require_approval", data.get("requireApproval", False))),
            on_failure=str(data.get("on_failure", data.get("onFailure", "stop")) or "stop").lower(),
            depends_on=deps,
        )


@dataclass
class WorkflowRecipe:
    """Workflow recipe loaded from yaml/json."""

    name: str
    steps: list[WorkflowStep]
    metadata: dict[str, Any] = field(default_factory=dict)
    mode: Literal["linear", "dag"] = "linear"
    max_parallel: int = 4

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, fallback_name: str = "workflow") -> "WorkflowRecipe":
        name = str(data.get("name") or fallback_name)
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            raise ValueError("Workflow recipe requires a list field 'steps'.")

        steps = [WorkflowStep.from_dict(step, index=i + 1) for i, step in enumerate(raw_steps) if isinstance(step, dict)]
        if not steps:
            raise ValueError("Workflow recipe has no valid steps.")

        seen_ids: set[str] = set()
        for step in steps:
            if not step.prompt:
                raise ValueError(f"Workflow step '{step.id}' is missing prompt.")
            if step.id in seen_ids:
                raise ValueError(f"Workflow step ids must be unique. Duplicate: '{step.id}'.")
            seen_ids.add(step.id)
            if step.on_failure not in {"stop", "continue"}:
                step.on_failure = "stop"
            step.depends_on = list(dict.fromkeys(step.depends_on))

        raw_mode = str(data.get("mode") or "").strip().lower()
        mode: Literal["linear", "dag"]
        if raw_mode in {"linear", "dag"}:
            mode = raw_mode
        elif any(step.depends_on for step in steps):
            mode = "dag"
        else:
            mode = "linear"

        max_parallel = max(1, int(data.get("max_parallel", data.get("maxParallel", 4)) or 1))

        recipe = cls(
            name=name,
            steps=steps,
            metadata=dict(data.get("metadata") or {}),
            mode=mode,
            max_parallel=max_parallel,
        )
        recipe._validate_dependencies()
        recipe._ensure_acyclic()
        return recipe

    def _validate_dependencies(self) -> None:
        ids = {step.id for step in self.steps}
        for step in self.steps:
            for dep in step.depends_on:
                if dep == step.id:
                    raise ValueError(f"Workflow step '{step.id}' cannot depend on itself.")
                if dep not in ids:
                    raise ValueError(f"Workflow step '{step.id}' depends on unknown step '{dep}'.")

    def _ensure_acyclic(self) -> None:
        deps = {step.id: set(step.depends_on) for step in self.steps}
        ready = [step_id for step_id, need in deps.items() if not need]
        visited = 0
        while ready:
            node = ready.pop()
            visited += 1
            for step_id, need in deps.items():
                if node in need:
                    need.remove(node)
                    if not need:
                        ready.append(step_id)
        if visited != len(self.steps):
            raise ValueError("Workflow recipe contains cyclic dependencies.")


class LinearWorkflowRuntime:
    """Execute linear and DAG recipes through the current agent runtime."""

    def __init__(
        self,
        *,
        agent_runtime: Any,
        bus: Any | None = None,
        workspace: Path | None = None,
        recipe_root: Path | None = None,
        approval_session_key: str = "dashboard:approvals",
        approval_timeout_s: float = 300.0,
    ):
        self.agent_runtime = agent_runtime
        self.bus = bus
        self.workspace = Path(workspace) if workspace else None
        self.recipe_root = Path(recipe_root) if recipe_root else None
        self.approval_session_key = approval_session_key
        self.approval_timeout_s = max(1.0, float(approval_timeout_s))
        self._approval_lock = asyncio.Lock()

    def load_recipe(self, name_or_path: str | Path) -> WorkflowRecipe:
        path = self._resolve_recipe_path(name_or_path)
        payload = self._load_recipe_payload(path)
        return WorkflowRecipe.from_dict(payload, fallback_name=path.stem)

    async def run_recipe(
        self,
        recipe: WorkflowRecipe,
        *,
        vars: dict[str, Any] | None = None,
        channel: str = "system",
        chat_id: str = "workflow",
        model_override: str | None = None,
    ) -> dict[str, Any]:
        run_id = f"wf_{uuid.uuid4().hex[:12]}"
        scoped_vars = dict(vars or {})
        scoped_vars.setdefault("workflow_name", recipe.name)

        if recipe.mode == "dag":
            return await self._run_dag_recipe(
                recipe=recipe,
                run_id=run_id,
                scoped_vars=scoped_vars,
                channel=channel,
                chat_id=chat_id,
                model_override=model_override,
            )
        return await self._run_linear_recipe(
            recipe=recipe,
            run_id=run_id,
            scoped_vars=scoped_vars,
            channel=channel,
            chat_id=chat_id,
            model_override=model_override,
        )

    async def _run_linear_recipe(
        self,
        *,
        recipe: WorkflowRecipe,
        run_id: str,
        scoped_vars: dict[str, Any],
        channel: str,
        chat_id: str,
        model_override: str | None,
    ) -> dict[str, Any]:
        started = time.time()
        records: list[dict[str, Any]] = []
        final_status = "completed"

        for step in recipe.steps:
            record = await self._execute_step(
                run_id=run_id,
                recipe=recipe,
                step=step,
                scoped_vars=scoped_vars,
                channel=channel,
                chat_id=chat_id,
                model_override=model_override,
            )
            records.append(record)

            if record["status"] == "ok":
                scoped_vars[f"{step.id}_output"] = record.get("output", "")
                continue
            if record["status"] == "blocked":
                final_status = "blocked"
                break
            if record["status"] == "failed":
                final_status = "failed"
                if step.on_failure == "stop":
                    break

        return self._build_result(
            run_id=run_id,
            recipe=recipe,
            status=final_status,
            records=records,
            started=started,
        )

    async def _run_dag_recipe(
        self,
        *,
        recipe: WorkflowRecipe,
        run_id: str,
        scoped_vars: dict[str, Any],
        channel: str,
        chat_id: str,
        model_override: str | None,
    ) -> dict[str, Any]:
        started = time.time()
        step_map = {step.id: step for step in recipe.steps}
        step_order = [step.id for step in recipe.steps]
        order_index = {step_id: idx for idx, step_id in enumerate(step_order)}

        records_by_id: dict[str, dict[str, Any]] = {}
        pending: set[str] = set(step_order)
        final_status = "completed"
        stop_requested = False
        semaphore = asyncio.Semaphore(recipe.max_parallel)

        while pending:
            blocked: list[tuple[WorkflowStep, str]] = []
            ready: list[WorkflowStep] = []
            for step_id in sorted(pending, key=lambda item: order_index[item]):
                step = step_map[step_id]
                if not step.depends_on:
                    ready.append(step)
                    continue
                if any(dep not in records_by_id for dep in step.depends_on):
                    continue
                failed_dep = next(
                    (dep for dep in step.depends_on if records_by_id[dep].get("status") != "ok"),
                    "",
                )
                if failed_dep:
                    blocked.append((step, failed_dep))
                    continue
                ready.append(step)

            for step, dep in blocked:
                pending.discard(step.id)
                records_by_id[step.id] = {
                    "id": step.id,
                    "attempts": 0,
                    "status": "skipped",
                    "reason": "dependency_failed",
                    "dependency": dep,
                    "output": "",
                }
                if final_status == "completed":
                    final_status = "failed"

            if stop_requested:
                for step_id in sorted(pending, key=lambda item: order_index[item]):
                    records_by_id[step_id] = {
                        "id": step_id,
                        "attempts": 0,
                        "status": "skipped",
                        "reason": "workflow_stopped",
                        "output": "",
                    }
                pending.clear()
                break

            if not ready:
                if pending:
                    for step_id in sorted(pending, key=lambda item: order_index[item]):
                        records_by_id[step_id] = {
                            "id": step_id,
                            "attempts": 0,
                            "status": "skipped",
                            "reason": "unresolved_dependencies",
                            "output": "",
                        }
                    pending.clear()
                    if final_status == "completed":
                        final_status = "failed"
                break

            for step in ready:
                pending.discard(step.id)

            async def run_step(step: WorkflowStep) -> tuple[str, dict[str, Any]]:
                async with semaphore:
                    record = await self._execute_step(
                        run_id=run_id,
                        recipe=recipe,
                        step=step,
                        scoped_vars=scoped_vars,
                        channel=channel,
                        chat_id=chat_id,
                        model_override=model_override,
                    )
                    return step.id, record

            results = await asyncio.gather(*(run_step(step) for step in ready))
            for step_id, record in results:
                records_by_id[step_id] = record
                status = str(record.get("status") or "")
                if status == "ok":
                    scoped_vars[f"{step_id}_output"] = record.get("output", "")
                    continue
                if status == "blocked":
                    if step_map[step_id].on_failure == "stop":
                        final_status = "blocked"
                        stop_requested = True
                    elif final_status == "completed":
                        final_status = "failed"
                    continue
                if status == "failed":
                    if final_status == "completed":
                        final_status = "failed"
                    if step_map[step_id].on_failure == "stop":
                        stop_requested = True

        records = [records_by_id[step_id] for step_id in step_order if step_id in records_by_id]
        return self._build_result(
            run_id=run_id,
            recipe=recipe,
            status=final_status,
            records=records,
            started=started,
        )

    async def _execute_step(
        self,
        *,
        run_id: str,
        recipe: WorkflowRecipe,
        step: WorkflowStep,
        scoped_vars: dict[str, Any],
        channel: str,
        chat_id: str,
        model_override: str | None,
    ) -> dict[str, Any]:
        rendered_prompt = self._render_template(step.prompt, scoped_vars)
        if not rendered_prompt.strip():
            return {"id": step.id, "attempts": 0, "status": "skipped", "reason": "empty_prompt", "output": ""}

        if step.require_approval:
            async with self._approval_lock:
                approved = await self._wait_for_approval(
                    run_id=run_id,
                    recipe=recipe,
                    step=step,
                    prompt=rendered_prompt,
                )
            if not approved:
                return {
                    "id": step.id,
                    "attempts": 0,
                    "status": "blocked",
                    "reason": "approval_denied",
                    "output": "",
                }

        step_record: dict[str, Any] = {
            "id": step.id,
            "attempts": 0,
            "status": "failed",
            "output": "",
        }
        for attempt in range(1, step.retry_max_attempts + 1):
            step_record["attempts"] = attempt
            response = await self.agent_runtime.process_direct(
                content=rendered_prompt,
                session_key=f"workflow:{recipe.name}:{step.id}",
                channel=channel,
                chat_id=chat_id,
                model_override=model_override,
            )
            if response and str(response).strip():
                output = str(response).strip()
                step_record["status"] = "ok"
                step_record["output"] = output
                return step_record
            if attempt < step.retry_max_attempts and step.retry_backoff_ms > 0:
                await asyncio.sleep(step.retry_backoff_ms / 1000.0)
        return step_record

    @staticmethod
    def _build_result(
        *,
        run_id: str,
        recipe: WorkflowRecipe,
        status: str,
        records: list[dict[str, Any]],
        started: float,
    ) -> dict[str, Any]:
        duration_ms = int((time.time() - started) * 1000)
        return {
            "id": run_id,
            "name": recipe.name,
            "status": status,
            "mode": recipe.mode,
            "duration_ms": duration_ms,
            "steps": records,
        }

    def _resolve_recipe_path(self, name_or_path: str | Path) -> Path:
        candidate = Path(name_or_path).expanduser()
        if candidate.exists():
            return candidate
        if self.recipe_root is not None:
            for suffix in (".yaml", ".yml", ".json"):
                maybe = self.recipe_root / f"{name_or_path}{suffix}"
                if maybe.exists():
                    return maybe
            maybe = self.recipe_root / str(name_or_path)
            if maybe.exists():
                return maybe

        if self.workspace is None:
            raise FileNotFoundError(f"Workflow recipe not found: {name_or_path}")

        for root in (self.workspace / "workflows",):
            for suffix in (".yaml", ".yml", ".json"):
                maybe = root / f"{name_or_path}{suffix}"
                if maybe.exists():
                    return maybe
            maybe = root / str(name_or_path)
            if maybe.exists():
                return maybe
        raise FileNotFoundError(f"Workflow recipe not found: {name_or_path}")

    @staticmethod
    def _load_recipe_payload(path: Path) -> dict[str, Any]:
        raw = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            data = json.loads(raw)
        else:
            try:
                import yaml  # type: ignore

                data = yaml.safe_load(raw)
            except ImportError as exc:
                raise RuntimeError(
                    "YAML workflow recipes require PyYAML. Install with `pip install pyyaml` "
                    "or use JSON recipes."
                ) from exc
        if not isinstance(data, dict):
            raise ValueError(f"Workflow recipe must be an object: {path}")
        return data

    @staticmethod
    def _render_template(template: str, values: dict[str, Any]) -> str:
        if not template:
            return ""
        try:
            return template.format(**values)
        except Exception:
            return template

    async def _wait_for_approval(
        self,
        *,
        run_id: str,
        recipe: WorkflowRecipe,
        step: WorkflowStep,
        prompt: str,
    ) -> bool:
        if not self.bus or not hasattr(self.bus, "wait_for_response"):
            return True

        approval_id = f"approval_{uuid.uuid4().hex[:10]}"
        event = {
            "type": "workflow_approval",
            "id": approval_id,
            "run_id": run_id,
            "session_key": self.approval_session_key,
            "workflow": recipe.name,
            "step_id": step.id,
            "prompt_preview": prompt[:500],
            "ts": time.time(),
        }
        if hasattr(self.bus, "add_pending_approval"):
            self.bus.add_pending_approval(event)
        if hasattr(self.bus, "publish_approval"):
            await self.bus.publish_approval(event)

        response = await self.bus.wait_for_response(
            self.approval_session_key,
            timeout=self.approval_timeout_s,
            approval_id=approval_id,
        )
        decision = str(response or "").strip().lower()
        approved = decision in {"approve", "approved", "yes", "y", "ok", "continue", "allow"}
        if hasattr(self.bus, "resolve_pending_approval"):
            self.bus.resolve_pending_approval(
                approval_id=approval_id,
                session_key=self.approval_session_key,
            )
        return approved
