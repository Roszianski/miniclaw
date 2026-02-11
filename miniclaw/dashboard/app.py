"""FastAPI dashboard backend for miniclaw."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from miniclaw.api.webhooks import WebhookService, create_webhook_router
from miniclaw.dashboard.auth import require_token
from miniclaw.distributed.manager import DistributedNodeManager
from miniclaw.identity import IdentityStore
from miniclaw.plugins.manager import PluginManager, PluginValidationError
from miniclaw.workflows.runtime import LinearWorkflowRuntime

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    config: Any,
    config_path: Path,
    sessions_manager: Any = None,
    cron_service: Any = None,
    heartbeat_service: Any = None,
    skills_loader: Any = None,
    agent_loop: Any = None,
    token: str = "",
    bus: Any = None,
    channels_manager: Any = None,
    memory_store: Any = None,
    secret_store: Any = None,
    process_manager: Any = None,
    identity_store: IdentityStore | None = None,
    distributed_manager: DistributedNodeManager | None = None,
    usage_tracker: Any = None,
    compliance_service: Any = None,
    alert_service: Any = None,
) -> FastAPI:
    """Create the dashboard FastAPI app."""

    app = FastAPI(title="miniclaw dashboard", docs_url=None, redoc_url=None)
    auth = require_token(token)
    plugin_manager = PluginManager(workspace=config.workspace_path, config=config.plugins)
    safe_name_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    def _normalize_safe_name(value: Any, *, kind: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError(f"{kind} name is required")
        if not safe_name_re.fullmatch(name):
            raise ValueError(
                f"invalid {kind} name (allowed: letters, numbers, dot, underscore, hyphen)"
            )
        return name

    def _resolve_child_path(root: Path, child_name: str, *, kind: str) -> Path:
        base = root.expanduser().resolve()
        target = (base / child_name).resolve()
        if target != base and base not in target.parents:
            raise ValueError(f"{kind} path resolves outside target directory")
        return target

    recipe_root = Path(config.workflows.path)
    if not recipe_root.is_absolute():
        recipe_root = (config.workspace_path / recipe_root).resolve()
    workflow_runtime = LinearWorkflowRuntime(
        agent_runtime=agent_loop,
        bus=bus,
        workspace=config.workspace_path,
        recipe_root=recipe_root,
        approval_session_key=config.workflows.approval_session_key,
    )
    webhook_service = WebhookService(
        config=config,
        secret_store=secret_store,
        agent_runtime=agent_loop,
        workflow_runtime=workflow_runtime,
    )
    app.include_router(create_webhook_router(service=webhook_service, dashboard_auth=auth))

    # Serve static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # === Pages ===

    @app.get("/", response_class=HTMLResponse)
    async def index():
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>miniclaw dashboard</h1><p>Static files not found.</p>")

    # === API Routes ===

    @app.get("/api/status", dependencies=[Depends(auth)])
    async def api_status():
        status: dict[str, Any] = {
            "version": "0.2.0",
            "running": True,
            "model": getattr(config.agents.defaults, "model", ""),
            "approvals_enabled": bool(getattr(config.tools, "approval", None)),
            "rate_limit_enabled": bool(getattr(config, "rate_limit", None) and config.rate_limit.enabled),
        }
        if cron_service:
            status["cron"] = cron_service.status()
        if heartbeat_service:
            status["heartbeat"] = heartbeat_service.status()
        if channels_manager:
            status["channels"] = channels_manager.get_status()
        if process_manager:
            try:
                procs = process_manager.list_processes()
                status["processes"] = {
                    "total": len(procs),
                    "running": len([p for p in procs if p.get("running")]),
                }
            except Exception:
                status["processes"] = {"total": 0, "running": 0}
        if identity_store:
            try:
                status["identity"] = {
                    "links": len(identity_store.list_links(include_inactive=False)),
                    "pending_pairings": len(identity_store.list_pairing_requests(include_inactive=False)),
                }
            except Exception:
                status["identity"] = {"links": 0, "pending_pairings": 0}
        if distributed_manager and config.distributed.enabled:
            try:
                nodes = distributed_manager.list_nodes(include_stale=False)
                status["distributed"] = {
                    "enabled": True,
                    "nodes_online": len(nodes),
                    "node_id": config.distributed.node_id,
                }
            except Exception:
                status["distributed"] = {"enabled": True, "nodes_online": 0}
        if agent_loop and hasattr(agent_loop, "list_runs"):
            runs = agent_loop.list_runs(limit=200)
            active = [r for r in runs if r.get("status") in ("queued", "running")]
            status["runs"] = {"active": len(active), "recent": len(runs)}
        if alert_service:
            if hasattr(alert_service, "scan_health"):
                try:
                    alert_service.scan_health()
                except Exception:
                    pass
            status["alerts"] = alert_service.summary() if hasattr(alert_service, "summary") else {"enabled": False}
        if usage_tracker and hasattr(usage_tracker, "summary"):
            try:
                usage = usage_tracker.summary()
                status["usage"] = usage.get("overall", {}).get("totals", {})
            except Exception:
                status["usage"] = {"events": 0, "total_tokens": 0, "cost_usd": 0.0}
        return status

    @app.get("/api/config", dependencies=[Depends(auth)])
    async def api_get_config():
        if config_path.exists():
            return JSONResponse(json.loads(config_path.read_text()))
        return JSONResponse({})

    @app.put("/api/config", dependencies=[Depends(auth)])
    async def api_update_config(body: dict):
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(body, indent=2))
        return {"ok": True}

    @app.post("/api/config/validate", dependencies=[Depends(auth)])
    async def api_validate_config(body: dict):
        try:
            from miniclaw.config.loader import convert_keys
            from miniclaw.config.schema import Config as ConfigModel
            ConfigModel.model_validate(convert_keys(body))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "errors": [str(e)]}

    # === Identity + Pairing API ===

    @app.post("/api/pairing/request", dependencies=[Depends(auth)])
    async def api_pairing_request(body: dict):
        if not identity_store or not config.identity.enabled:
            return JSONResponse({"ok": False, "error": "identity pairing unavailable"}, status_code=400)
        platform = str(body.get("platform") or "").strip()
        platform_user_id = str(body.get("platform_user_id") or body.get("user_id") or "").strip()
        if not platform or not platform_user_id:
            return JSONResponse(
                {"ok": False, "error": "platform and platform_user_id are required"},
                status_code=400,
            )
        try:
            req = identity_store.create_pairing_request(
                platform=platform,
                platform_user_id=platform_user_id,
                device_id=str(body.get("device_id") or ""),
                display_name=str(body.get("display_name") or ""),
                expires_in_s=config.identity.pairing_code_ttl_s,
                metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else None,
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {
            "ok": True,
            "request_id": req["id"],
            "code": req["code"],
            "expires_at_ms": req["expires_at_ms"],
            "platform": req["platform"],
            "platform_user_id": req["platform_user_id"],
        }

    @app.post("/api/pairing/approve", dependencies=[Depends(auth)])
    async def api_pairing_approve(body: dict):
        if not identity_store or not config.identity.enabled:
            return JSONResponse({"ok": False, "error": "identity pairing unavailable"}, status_code=400)
        request_id = str(body.get("request_id") or "").strip()
        code = str(body.get("code") or "").strip()
        canonical_user_id = str(body.get("canonical_user_id") or config.identity.owner_user_id).strip()
        if not request_id or not code:
            return JSONResponse({"ok": False, "error": "request_id and code are required"}, status_code=400)
        try:
            result = identity_store.approve_pairing(
                request_id=request_id,
                code=code,
                canonical_user_id=canonical_user_id,
                approver="dashboard",
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, **result}

    @app.post("/api/pairing/revoke", dependencies=[Depends(auth)])
    async def api_pairing_revoke(body: dict):
        if not identity_store or not config.identity.enabled:
            return JSONResponse({"ok": False, "error": "identity pairing unavailable"}, status_code=400)
        revoked = identity_store.revoke_pairing(
            pairing_id=(str(body.get("pairing_id")) if body.get("pairing_id") else None),
            platform=(str(body.get("platform")) if body.get("platform") else None),
            platform_user_id=(str(body.get("platform_user_id")) if body.get("platform_user_id") else None),
            canonical_user_id=(str(body.get("canonical_user_id")) if body.get("canonical_user_id") else None),
            revoked_by="dashboard",
        )
        return {"ok": True, "revoked": int(revoked)}

    @app.get("/api/identity/links", dependencies=[Depends(auth)])
    async def api_identity_links(
        canonical_user_id: str | None = None,
        platform: str | None = None,
        include_inactive: bool = False,
    ):
        if not identity_store:
            return []
        return identity_store.list_links(
            canonical_user_id=canonical_user_id,
            platform=platform,
            include_inactive=include_inactive,
        )

    @app.get("/api/sessions", dependencies=[Depends(auth)])
    async def api_list_sessions():
        if not sessions_manager:
            return []
        sessions = sessions_manager.list_sessions()
        return [{"key": s.get("key"), "messages": s.get("messages", 0), "updated_at": s.get("updated_at")} for s in sessions]

    @app.get("/api/sessions/{key:path}", dependencies=[Depends(auth)])
    async def api_get_session(key: str):
        if not sessions_manager:
            return {"error": "no sessions manager"}
        session = sessions_manager.get_or_create(key)
        return {
            "key": session.key,
            "summary": getattr(session, "summary", ""),
            "messages": [
                {
                    "role": m.get("role"),
                    "content": m.get("content", "")[:2000],
                    "timestamp": m.get("timestamp", "")
                }
                for m in session.messages
            ],
        }

    @app.get("/api/skills", dependencies=[Depends(auth)])
    async def api_list_skills():
        if not skills_loader:
            return []
        return skills_loader.list_skills(filter_unavailable=False)

    @app.get("/api/skills/{name}", dependencies=[Depends(auth)])
    async def api_get_skill(name: str):
        if not skills_loader:
            return {"error": "skills not available"}
        try:
            safe_name = _normalize_safe_name(name, kind="skill")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        content = skills_loader.load_skill(safe_name) or ""
        meta = skills_loader.get_skill_metadata(safe_name) or {}
        # Attempt to parse additional metadata if present
        skill_meta = {}
        try:
            if hasattr(skills_loader, "_parse_metadata"):
                skill_meta = skills_loader._parse_metadata(meta.get("metadata", ""))
        except Exception:
            skill_meta = {}
        requires = skill_meta.get("requires", {}) if isinstance(skill_meta, dict) else {}
        secret_requirements = {"required": [], "present": [], "missing": []}
        if hasattr(skills_loader, "get_secret_requirement_status"):
            try:
                secret_requirements = skills_loader.get_secret_requirement_status(safe_name)
            except Exception:
                secret_requirements = {"required": [], "present": [], "missing": []}
        return {
            "name": safe_name,
            "metadata": meta,
            "requires": requires,
            "secret_requirements": secret_requirements,
            "content": content,
        }

    @app.get("/api/skills/{name}/secrets", dependencies=[Depends(auth)])
    async def api_get_skill_secrets(name: str):
        if not skills_loader:
            return {"error": "skills not available"}
        try:
            safe_name = _normalize_safe_name(name, kind="skill")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        required: list[str] = []
        if hasattr(skills_loader, "get_required_env_vars"):
            required = list(skills_loader.get_required_env_vars(safe_name))
        values: dict[str, bool] = {}
        for env_name in required:
            present = bool(os.environ.get(env_name))
            if not present and secret_store:
                key = (
                    skills_loader.secret_key_for(safe_name, env_name)
                    if hasattr(skills_loader, "secret_key_for")
                    else f"skill:{safe_name}:env:{env_name}"
                )
                present = bool(secret_store.has(key))
            values[env_name] = present

        present_list = [k for k, v in values.items() if v]
        missing_list = [k for k, v in values.items() if not v]
        return {
            "name": safe_name,
            "required": required,
            "values": values,  # masked status only
            "present": present_list,
            "missing": missing_list,
            "backend": secret_store.backend_name if secret_store and hasattr(secret_store, "backend_name") else "none",
        }

    @app.put("/api/skills/{name}/secrets", dependencies=[Depends(auth)])
    async def api_put_skill_secrets(name: str, body: dict):
        if not skills_loader:
            return {"error": "skills not available"}
        if not secret_store:
            return {"error": "secret store unavailable"}
        try:
            safe_name = _normalize_safe_name(name, kind="skill")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        required: list[str] = []
        if hasattr(skills_loader, "get_required_env_vars"):
            required = list(skills_loader.get_required_env_vars(safe_name))
        allowed = set(required)

        secrets_body = body.get("secrets")
        if not isinstance(secrets_body, dict):
            secrets_body = body.get("values")
        if not isinstance(secrets_body, dict):
            return {"error": "body.secrets (object) is required"}

        for env_name, value in secrets_body.items():
            env_name = str(env_name)
            if allowed and env_name not in allowed:
                continue
            key = (
                skills_loader.secret_key_for(safe_name, env_name)
                if hasattr(skills_loader, "secret_key_for")
                else f"skill:{safe_name}:env:{env_name}"
            )
            if value is None or str(value).strip() == "":
                secret_store.delete(key)
            else:
                secret_store.set(key, str(value))

        return await api_get_skill_secrets(safe_name)

    @app.post("/api/skills/install", dependencies=[Depends(auth)])
    async def api_install_skill(body: dict):
        if not skills_loader:
            return {"error": "skills not available"}
        source = body.get("source") or ""
        if not source:
            return {"error": "source is required"}
        target_root = Path(skills_loader.workspace) / "skills"
        target_root.mkdir(parents=True, exist_ok=True)
        try:
            # Local path
            src_path = Path(source).expanduser()
            if src_path.exists():
                name = _normalize_safe_name(body.get("name") or src_path.name, kind="skill")
                dest = _resolve_child_path(target_root, name, kind="skill")
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src_path, dest)
                if hasattr(skills_loader, "invalidate_cache"):
                    skills_loader.invalidate_cache()
                status = None
                if hasattr(skills_loader, "list_skills"):
                    status = next(
                        (s for s in skills_loader.list_skills(filter_unavailable=False) if s.get("name") == name),
                        None,
                    )
                return {"ok": True, "name": name, "status": status}

            # Git URL
            if source.startswith("http") or source.endswith(".git"):
                with tempfile.TemporaryDirectory() as tmp:
                    subprocess.run(["git", "clone", source, tmp], check=True, capture_output=True)
                    name = _normalize_safe_name(
                        body.get("name") or Path(source).stem.replace(".git", ""),
                        kind="skill",
                    )
                    dest = _resolve_child_path(target_root, name, kind="skill")
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(tmp, dest, ignore=shutil.ignore_patterns(".git"))
                    if hasattr(skills_loader, "invalidate_cache"):
                        skills_loader.invalidate_cache()
                    status = None
                    if hasattr(skills_loader, "list_skills"):
                        status = next(
                            (s for s in skills_loader.list_skills(filter_unavailable=False) if s.get("name") == name),
                            None,
                        )
                    return {"ok": True, "name": name, "status": status}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"error": "invalid source"}, status_code=400)

    @app.get("/api/plugins", dependencies=[Depends(auth)])
    async def api_list_plugins():
        return plugin_manager.list_plugins()

    @app.post("/api/plugins/install", dependencies=[Depends(auth)])
    async def api_install_plugin(body: dict):
        source = str(body.get("source") or "").strip()
        if not source:
            return JSONResponse({"error": "source is required"}, status_code=400)
        name = str(body.get("name") or "").strip() or None
        try:
            result = plugin_manager.install(source, name=name)
        except PluginValidationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr or exc.stdout or str(exc)
            return JSONResponse({"error": detail}, status_code=400)
        return {"ok": True, "plugin": result}

    @app.delete("/api/plugins/{name}", dependencies=[Depends(auth)])
    async def api_remove_plugin(name: str):
        try:
            ok = plugin_manager.remove(name)
        except PluginValidationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"ok": ok}

    @app.get("/api/workflows", dependencies=[Depends(auth)])
    async def api_list_workflows():
        base = recipe_root
        if not base.exists():
            return []
        rows: list[dict[str, str]] = []
        for pattern in ("*.yaml", "*.yml", "*.json"):
            for path in sorted(base.glob(pattern)):
                rows.append({"name": path.stem, "path": str(path)})
        return rows

    @app.post("/api/workflows/run", dependencies=[Depends(auth)])
    async def api_run_workflow(body: dict):
        if not agent_loop:
            return JSONResponse({"error": "agent runtime unavailable"}, status_code=400)
        recipe_ref = str(body.get("recipe") or body.get("name") or body.get("path") or "").strip()
        if not recipe_ref:
            return JSONResponse({"error": "recipe is required"}, status_code=400)
        model_override = str(body.get("model") or "").strip() or None
        vars_payload = body.get("vars")
        vars_dict = vars_payload if isinstance(vars_payload, dict) else {}
        try:
            recipe = workflow_runtime.load_recipe(recipe_ref)
            result = await workflow_runtime.run_recipe(
                recipe,
                vars=vars_dict,
                channel=str(body.get("channel") or "dashboard"),
                chat_id=str(body.get("chat_id") or "workflow"),
                model_override=model_override,
            )
            return {"ok": True, "result": result}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.delete("/api/skills/{name}", dependencies=[Depends(auth)])
    async def api_delete_skill(name: str):
        if not skills_loader:
            return {"error": "skills not available"}
        try:
            safe_name = _normalize_safe_name(name, kind="skill")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        target_root = Path(skills_loader.workspace) / "skills"
        dest = _resolve_child_path(target_root, safe_name, kind="skill")
        if dest.exists():
            shutil.rmtree(dest)
            if hasattr(skills_loader, "invalidate_cache"):
                skills_loader.invalidate_cache()
            return {"ok": True}
        return {"error": "not found"}

    @app.get("/api/cron", dependencies=[Depends(auth)])
    async def api_list_cron():
        if not cron_service:
            return []
        jobs = cron_service.list_jobs(include_disabled=True)
        return [
            {
                "id": j.id,
                "name": j.name,
                "enabled": j.enabled,
                "kind": j.payload.kind,
                "schedule": j.schedule.kind,
                "message": j.payload.message[:100],
                "next_run_at_ms": j.state.next_run_at_ms,
            }
            for j in jobs
        ]

    @app.post("/api/cron", dependencies=[Depends(auth)])
    async def api_add_cron(body: dict):
        if not cron_service:
            return {"error": "cron not available"}
        from miniclaw.cron.types import CronSchedule

        def _parse_int(name: str, default: int) -> tuple[int | None, JSONResponse | None]:
            raw = body.get(name, default)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return None, JSONResponse({"error": f"{name} must be an integer"}, status_code=400)
            return value, None

        message = body.get("message", "")
        kind = body.get("kind", "task")
        isolated = bool(body.get("isolated", False))
        channel = body.get("channel")
        to = body.get("to")
        agent_id = body.get("agent_id")
        model = body.get("model")
        retry_max_attempts, err = _parse_int("retry_max_attempts", 1)
        if err is not None:
            return err
        retry_backoff_ms, err = _parse_int("retry_backoff_ms", 750)
        if err is not None:
            return err
        every_seconds = body.get("every_seconds")
        cron_expr = body.get("cron_expr")
        if not message:
            return {"error": "message is required"}
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=int(every_seconds) * 1000)
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr)
        else:
            return {"error": "every_seconds or cron_expr required"}
        job = cron_service.add_job(
            name=message[:30],
            schedule=schedule,
            message=message,
            deliver=True,
            channel=channel,
            to=to,
            kind=kind,
            isolated=isolated,
            agent_id=agent_id,
            model=model,
            retry_max_attempts=retry_max_attempts or 1,
            retry_backoff_ms=retry_backoff_ms or 750,
        )
        return {"ok": True, "id": job.id}

    @app.delete("/api/cron/{job_id}", dependencies=[Depends(auth)])
    async def api_remove_cron(job_id: str):
        if not cron_service:
            return {"error": "cron not available"}
        removed = cron_service.remove_job(job_id)
        return {"ok": removed}

    @app.post("/api/cron/{job_id}/enable", dependencies=[Depends(auth)])
    async def api_enable_cron(job_id: str, body: dict):
        if not cron_service:
            return {"error": "cron not available"}
        enabled = bool(body.get("enabled", True))
        job = cron_service.enable_job(job_id, enabled=enabled)
        return {"ok": bool(job), "enabled": enabled}

    # === Heartbeat API ===

    @app.get("/api/heartbeat", dependencies=[Depends(auth)])
    async def api_get_heartbeat():
        if not heartbeat_service:
            return {"ok": False, "error": "heartbeat not available"}
        content = ""
        hb_file = heartbeat_service.heartbeat_file
        if hb_file.exists():
            content = hb_file.read_text(encoding="utf-8")
        return {
            "ok": True,
            "status": heartbeat_service.status(),
            "content": content,
        }

    @app.put("/api/heartbeat", dependencies=[Depends(auth)])
    async def api_put_heartbeat(body: dict):
        if not heartbeat_service:
            return {"ok": False, "error": "heartbeat not available"}
        content = body.get("content", "")
        hb_file = heartbeat_service.heartbeat_file
        hb_file.write_text(content, encoding="utf-8")
        return {"ok": True}

    @app.post("/api/heartbeat/trigger", dependencies=[Depends(auth)])
    async def api_trigger_heartbeat():
        if not heartbeat_service:
            return {"ok": False, "error": "heartbeat not available"}
        result = await heartbeat_service.trigger_now()
        return {"ok": True, "result": result or ""}

    @app.get("/api/approvals/pending", dependencies=[Depends(auth)])
    async def api_list_pending_approvals():
        if not bus:
            return []
        return bus.list_pending_approvals()

    @app.get("/api/runs", dependencies=[Depends(auth)])
    async def api_list_runs(limit: int = 100):
        if not agent_loop or not hasattr(agent_loop, "list_runs"):
            return []
        return agent_loop.list_runs(limit=limit)

    @app.get("/api/runs/queue", dependencies=[Depends(auth)])
    async def api_runs_queue():
        if not agent_loop or not hasattr(agent_loop, "get_queue_snapshot"):
            return {"mode": "queue", "collect_window_ms": 0, "max_backlog": 0, "sessions": []}
        return agent_loop.get_queue_snapshot()

    @app.post("/api/runs/{run_id}/cancel", dependencies=[Depends(auth)])
    async def api_cancel_run(run_id: str):
        if not agent_loop or not hasattr(agent_loop, "cancel_run"):
            return {"ok": False, "error": "agent loop unavailable"}
        ok = bool(agent_loop.cancel_run(run_id))
        return {"ok": ok}

    @app.post("/api/runs/{run_id}/steer", dependencies=[Depends(auth)])
    async def api_steer_run(run_id: str, body: dict):
        if not agent_loop or not hasattr(agent_loop, "steer_run"):
            return {"ok": False, "error": "agent loop unavailable"}
        instruction = str(body.get("instruction") or body.get("content") or "").strip()
        if not instruction:
            return {"ok": False, "error": "instruction is required"}
        sender_id = str(body.get("sender_id") or "")
        ok = bool(agent_loop.steer_run(run_id, instruction, source="api", sender_id=sender_id))
        return {"ok": ok}

    @app.post("/api/agents/message", dependencies=[Depends(auth)])
    async def api_agent_message(body: dict):
        if not agent_loop or not hasattr(agent_loop, "send_agent_message"):
            return JSONResponse({"ok": False, "error": "agent router messaging unavailable"}, status_code=400)
        from_agent_id = str(body.get("from_agent_id") or body.get("from") or "default").strip()
        to_agent_id = str(body.get("to_agent_id") or body.get("to") or "").strip()
        content = str(body.get("content") or "").strip()
        if not to_agent_id or not content:
            return JSONResponse(
                {"ok": False, "error": "to_agent_id and content are required"},
                status_code=400,
            )
        meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        ok = bool(
            agent_loop.send_agent_message(
                from_agent_id=from_agent_id,
                to_agent_id=to_agent_id,
                content=content,
                metadata=meta,
            )
        )
        return {"ok": ok}

    @app.get("/api/agents/messages", dependencies=[Depends(auth)])
    async def api_agent_messages(limit: int = 100):
        if not bus or not hasattr(bus, "list_agent_messages"):
            return []
        return bus.list_agent_messages(limit=limit)

    @app.get("/api/processes", dependencies=[Depends(auth)])
    async def api_list_processes():
        if not process_manager:
            return []
        return process_manager.list_processes()

    @app.post("/api/processes/start", dependencies=[Depends(auth)])
    async def api_start_process(body: dict):
        if not process_manager:
            return {"ok": False, "error": "process manager unavailable"}
        command = str(body.get("command") or "").strip()
        cwd = body.get("cwd")
        name = body.get("name")
        if not command:
            return JSONResponse({"ok": False, "error": "command is required"}, status_code=400)
        try:
            started = process_manager.start_process(command=command, cwd=cwd, name=name)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "process": started}

    @app.post("/api/processes/stop", dependencies=[Depends(auth)])
    async def api_stop_process(body: dict):
        if not process_manager:
            return {"ok": False, "error": "process manager unavailable"}
        process_id = str(body.get("id") or body.get("process_id") or "").strip()
        if not process_id:
            return JSONResponse({"ok": False, "error": "id is required"}, status_code=400)
        ok = bool(process_manager.stop_process(process_id))
        return {"ok": ok}

    @app.get("/api/processes/{process_id}/logs", dependencies=[Depends(auth)])
    async def api_process_logs(process_id: str, tail: int = 200):
        if not process_manager:
            return {"ok": False, "error": "process manager unavailable"}
        try:
            logs = process_manager.read_logs(process_id, tail_lines=tail)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        return {"ok": True, "id": process_id, "logs": logs}

    @app.get("/api/alerts", dependencies=[Depends(auth)])
    async def api_alerts(limit: int = 100):
        if not alert_service:
            return {"enabled": False, "summary": {"enabled": False, "total": 0, "by_event": {}}, "events": []}
        if hasattr(alert_service, "scan_health"):
            try:
                alert_service.scan_health()
            except Exception:
                pass
        events = alert_service.list_events(limit=limit) if hasattr(alert_service, "list_events") else []
        summary = alert_service.summary() if hasattr(alert_service, "summary") else {"enabled": False}
        return {"enabled": bool(summary.get("enabled", False)), "summary": summary, "events": events}

    @app.get("/api/usage/summary", dependencies=[Depends(auth)])
    async def api_usage_summary(windows: str | None = None):
        if not usage_tracker or not hasattr(usage_tracker, "summary"):
            return {
                "generated_at_ms": 0,
                "overall": {"window": "all", "totals": {"events": 0, "total_tokens": 0, "cost_usd": 0.0}},
                "windows": {},
            }
        selected = None
        if windows:
            selected = [item.strip() for item in windows.split(",") if item.strip()]
        return usage_tracker.summary(windows=selected)

    @app.post("/api/data/export", dependencies=[Depends(auth)])
    async def api_data_export(body: dict):
        if not compliance_service:
            return JSONResponse({"ok": False, "error": "compliance service unavailable"}, status_code=400)
        include = body.get("include")
        include_list = [str(v) for v in include] if isinstance(include, list) else None
        output_path = str(body.get("output_path") or "").strip() or None
        try:
            result = compliance_service.export_bundle(include=include_list, output_path=output_path)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return result

    @app.post("/api/data/purge", dependencies=[Depends(auth)])
    async def api_data_purge(body: dict):
        if not compliance_service:
            return JSONResponse({"ok": False, "error": "compliance service unavailable"}, status_code=400)
        domains = body.get("domains")
        domains_list = [str(v) for v in domains] if isinstance(domains, list) else None
        try:
            result = compliance_service.purge(
                session_key=str(body.get("session_key") or "").strip() or None,
                user_id=str(body.get("user_id") or "").strip() or None,
                before_date=str(body.get("before_date") or "").strip() or None,
                domains=domains_list,
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if not result.get("ok", False):
            return JSONResponse(result, status_code=400)
        return result

    @app.post("/api/data/sweep", dependencies=[Depends(auth)])
    async def api_data_sweep():
        if not compliance_service:
            return JSONResponse({"ok": False, "error": "compliance service unavailable"}, status_code=400)
        try:
            return compliance_service.sweep()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    # === Distributed API ===

    @app.get("/api/distributed/nodes", dependencies=[Depends(auth)])
    async def api_distributed_nodes(include_stale: bool = False):
        if not distributed_manager or not config.distributed.enabled:
            return {"ok": False, "error": "distributed runtime disabled", "nodes": []}
        return {"ok": True, "nodes": distributed_manager.list_nodes(include_stale=include_stale)}

    @app.post("/api/distributed/nodes/register", dependencies=[Depends(auth)])
    async def api_distributed_register_node(body: dict):
        if not distributed_manager or not config.distributed.enabled:
            return JSONResponse({"ok": False, "error": "distributed runtime disabled"}, status_code=400)
        node_id = str(body.get("node_id") or "").strip()
        if not node_id:
            return JSONResponse({"ok": False, "error": "node_id is required"}, status_code=400)
        capabilities = body.get("capabilities") if isinstance(body.get("capabilities"), list) else []
        metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        try:
            node = distributed_manager.register_node(
                node_id=node_id,
                capabilities=[str(v) for v in capabilities],
                metadata=metadata,
                address=str(body.get("address") or ""),
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "node": node}

    @app.post("/api/distributed/nodes/{node_id}/heartbeat", dependencies=[Depends(auth)])
    async def api_distributed_heartbeat(node_id: str, body: dict):
        if not distributed_manager or not config.distributed.enabled:
            return JSONResponse({"ok": False, "error": "distributed runtime disabled"}, status_code=400)
        capabilities = body.get("capabilities") if isinstance(body.get("capabilities"), list) else None
        metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else None
        try:
            node = distributed_manager.heartbeat(
                node_id=node_id,
                capabilities=[str(v) for v in capabilities] if capabilities is not None else None,
                metadata=metadata,
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "node": node}

    @app.post("/api/distributed/tasks/dispatch", dependencies=[Depends(auth)])
    async def api_distributed_dispatch(body: dict):
        if not distributed_manager or not config.distributed.enabled:
            return JSONResponse({"ok": False, "error": "distributed runtime disabled"}, status_code=400)
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        required = body.get("required_capabilities") if isinstance(body.get("required_capabilities"), list) else []
        preferred = str(body.get("preferred_node_id") or "").strip() or None
        kind = str(body.get("kind") or "generic")
        try:
            task = distributed_manager.dispatch_task(
                payload=payload,
                required_capabilities=[str(v) for v in required],
                preferred_node_id=preferred,
                kind=kind,
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "task": task}

    @app.post("/api/distributed/nodes/{node_id}/tasks/claim", dependencies=[Depends(auth)])
    async def api_distributed_claim(node_id: str):
        if not distributed_manager or not config.distributed.enabled:
            return JSONResponse({"ok": False, "error": "distributed runtime disabled"}, status_code=400)
        task = distributed_manager.claim_task(node_id=node_id)
        return {"ok": True, "task": task}

    @app.post("/api/distributed/tasks/{task_id}/complete", dependencies=[Depends(auth)])
    async def api_distributed_complete(task_id: str, body: dict):
        if not distributed_manager or not config.distributed.enabled:
            return JSONResponse({"ok": False, "error": "distributed runtime disabled"}, status_code=400)
        node_id = str(body.get("node_id") or "").strip()
        if not node_id:
            return JSONResponse({"ok": False, "error": "node_id is required"}, status_code=400)
        result = body.get("result") if isinstance(body.get("result"), dict) else None
        error = str(body.get("error") or "").strip() or None
        try:
            task = distributed_manager.complete_task(
                task_id=task_id,
                node_id=node_id,
                result=result,
                error=error,
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "task": task}

    @app.get("/api/distributed/tasks/{task_id}", dependencies=[Depends(auth)])
    async def api_distributed_get_task(task_id: str):
        if not distributed_manager or not config.distributed.enabled:
            return JSONResponse({"ok": False, "error": "distributed runtime disabled"}, status_code=400)
        task = distributed_manager.get_task(task_id)
        if task is None:
            return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
        return {"ok": True, "task": task}

    @app.post("/api/approvals/respond", dependencies=[Depends(auth)])
    async def api_respond_approval(body: dict):
        if not bus:
            return {"ok": False, "error": "bus not available"}
        session_key = body.get("session_key", "")
        decision = body.get("decision", "")
        approval_id = body.get("id")
        ok = bus.submit_response(session_key, decision, approval_id=approval_id)
        return {"ok": ok}

    @app.get("/api/audit", dependencies=[Depends(auth)])
    async def api_audit(limit: int = 100):
        audit_path = config_path.parent / "audit.log"
        if not audit_path.exists():
            return []
        lines = audit_path.read_text(encoding="utf-8").strip().split("\n")
        entries = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries

    # === Memory API ===

    daily_file_re = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

    @app.get("/api/memory", dependencies=[Depends(auth)])
    async def api_list_memory():
        files = [{"name": "MEMORY.md", "type": "long-term"}]
        if memory_store:
            for p in memory_store.list_memory_files():
                files.append({"name": p.name, "type": "daily"})
        return files

    @app.get("/api/memory/{filename}", dependencies=[Depends(auth)])
    async def api_read_memory(filename: str):
        if filename == "MEMORY.md":
            content = memory_store.read_long_term() if memory_store else ""
            return {"name": filename, "content": content}
        if not daily_file_re.match(filename):
            return JSONResponse({"error": "invalid filename"}, status_code=400)
        if not memory_store:
            return {"name": filename, "content": ""}
        path = memory_store.memory_dir / filename
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        return {"name": filename, "content": content}

    @app.put("/api/memory/{filename}", dependencies=[Depends(auth)])
    async def api_write_memory(filename: str, body: dict):
        if filename == "MEMORY.md":
            if memory_store:
                memory_store.write_long_term(body.get("content", ""))
            return {"ok": True}
        if not daily_file_re.match(filename):
            return JSONResponse({"error": "invalid filename"}, status_code=400)
        if memory_store:
            path = memory_store.memory_dir / filename
            path.write_text(body.get("content", ""), encoding="utf-8")
        return {"ok": True}

    # === Workspace File API ===

    workspace_whitelist = {"SOUL.md", "USER.md", "AGENTS.md", "HEARTBEAT.md"}

    @app.get("/api/workspace", dependencies=[Depends(auth)])
    async def api_list_workspace():
        workspace = config.workspace_path
        files = []
        for filename in sorted(workspace_whitelist):
            path = workspace / filename
            files.append({"name": filename, "exists": path.exists()})
        return files

    @app.get("/api/workspace/{filename}", dependencies=[Depends(auth)])
    async def api_read_workspace(filename: str):
        if filename not in workspace_whitelist:
            return JSONResponse({"error": "not found"}, status_code=404)
        workspace = config.workspace_path
        path = workspace / filename
        if not path.exists():
            return {"name": filename, "content": ""}
        content = path.read_text(encoding="utf-8")
        return {"name": filename, "content": content}

    @app.put("/api/workspace/{filename}", dependencies=[Depends(auth)])
    async def api_write_workspace(filename: str, body: dict):
        if filename not in workspace_whitelist:
            return JSONResponse({"error": "not found"}, status_code=404)
        workspace = config.workspace_path
        workspace.mkdir(parents=True, exist_ok=True)
        path = workspace / filename
        path.write_text(body.get("content", ""), encoding="utf-8")
        return {"ok": True}

    # === WebSocket Chat ===

    @app.websocket("/ws/chat")
    async def ws_chat(websocket: WebSocket):
        # Verify token from query params
        ws_token = websocket.query_params.get("token", "")
        if ws_token != token:
            await websocket.close(code=4001)
            return

        await websocket.accept()
        try:
            while True:
                data = await websocket.receive_text()
                msg = json.loads(data)
                user_text = msg.get("content", "")

                if agent_loop and user_text:
                    response = await agent_loop.process_direct(
                        content=user_text,
                        session_key="dashboard:web",
                        channel="dashboard",
                        chat_id="web",
                    )
                    await websocket.send_json({"role": "assistant", "content": response})
                else:
                    await websocket.send_json({"role": "system", "content": "No agent available"})
        except WebSocketDisconnect:
            pass

    @app.websocket("/ws/approvals")
    async def ws_approvals(websocket: WebSocket):
        ws_token = websocket.query_params.get("token", "")
        if ws_token != token:
            await websocket.close(code=4001)
            return
        await websocket.accept()
        if not bus:
            await websocket.send_json({"type": "error", "message": "bus not available"})
            await websocket.close()
            return
        q = bus.register_approval_listener()
        try:
            while True:
                event = await q.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            bus.unregister_approval_listener(q)

    @app.websocket("/ws/runs")
    async def ws_runs(websocket: WebSocket):
        ws_token = websocket.query_params.get("token", "")
        if ws_token != token:
            await websocket.close(code=4001)
            return
        await websocket.accept()
        if not bus:
            await websocket.send_json({"type": "error", "message": "bus not available"})
            await websocket.close()
            return
        q = bus.register_run_listener()
        try:
            while True:
                event = await q.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            bus.unregister_run_listener(q)

    @app.websocket("/ws/agents")
    async def ws_agents(websocket: WebSocket):
        ws_token = websocket.query_params.get("token", "")
        if ws_token != token:
            await websocket.close(code=4001)
            return
        await websocket.accept()
        if not bus or not hasattr(bus, "register_agent_message_listener"):
            await websocket.send_json({"type": "error", "message": "agent messaging unavailable"})
            await websocket.close()
            return
        q = bus.register_agent_message_listener()
        try:
            while True:
                event = await q.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            bus.unregister_agent_message_listener(q)

    return app
