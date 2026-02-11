"""Webhook ingestion service with signature + replay protection."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from collections import deque
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request


class WebhookService:
    """Validate and process webhook events."""

    SIGNATURE_HEADERS = (
        "x-miniclaw-signature",
        "x-webhook-signature",
        "x-hub-signature-256",
        "x-signature",
    )
    TIMESTAMP_HEADERS = ("x-webhook-timestamp", "x-timestamp", "x-signature-timestamp")
    EVENT_ID_HEADERS = ("x-event-id", "x-webhook-id", "x-delivery-id")
    EVENT_TYPE_HEADERS = ("x-event-type", "x-github-event", "x-webhook-event")

    def __init__(
        self,
        *,
        config: Any,
        secret_store: Any | None = None,
        agent_runtime: Any | None = None,
        workflow_runtime: Any | None = None,
    ):
        webhook_cfg = config.webhooks
        self.enabled = bool(webhook_cfg.enabled)
        self.secret_refs = dict(webhook_cfg.secret_refs or {})
        self.allowed_events = {str(e).strip() for e in (webhook_cfg.allowed_events or []) if str(e).strip()}
        self.replay_window_s = int(webhook_cfg.replay_window_s or 300)
        self.rules = list(webhook_cfg.rules or [])
        self.secret_store = secret_store
        self.agent_runtime = agent_runtime
        self.workflow_runtime = workflow_runtime

        self._events: dict[str, dict[str, Any]] = {}
        self._dedupe: set[str] = set()
        self._dedupe_order: deque[str] = deque()
        self._max_dedupe = 5000

    async def handle(self, *, source: str, headers: dict[str, str], body: bytes, payload: Any) -> dict[str, Any]:
        if not self.enabled:
            raise HTTPException(status_code=404, detail="Webhooks are disabled.")

        event_type = self._resolve_event_type(headers=headers, payload=payload, source=source)
        if self.allowed_events and event_type not in self.allowed_events:
            raise HTTPException(status_code=403, detail=f"Event '{event_type}' is not allowed.")

        event_id = self._resolve_event_id(headers=headers, payload=payload)
        if event_id in self._dedupe:
            raise HTTPException(status_code=409, detail="Duplicate webhook event.")

        secret = self._resolve_secret(source)
        if not secret:
            raise HTTPException(status_code=401, detail=f"Webhook secret is not configured for source '{source}'.")

        timestamp = self._resolve_timestamp(headers=headers, payload=payload)
        signature = self._resolve_signature(headers)
        if not signature:
            raise HTTPException(status_code=401, detail="Missing webhook signature.")
        if timestamp is None:
            raise HTTPException(status_code=401, detail="Missing webhook timestamp.")
        age_s = abs(time.time() - timestamp)
        if age_s > self.replay_window_s:
            raise HTTPException(status_code=401, detail="Webhook timestamp outside replay window.")
        if not self._verify_signature(secret=secret, timestamp=timestamp, body=body, signature=signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")

        action_result = await self._dispatch_action(
            source=source,
            event=event_type,
            payload=payload if isinstance(payload, dict) else {},
        )

        row = {
            "id": event_id,
            "source": source,
            "event": event_type,
            "received_at": int(time.time()),
            "payload": payload if isinstance(payload, dict) else {},
            "action": action_result,
        }
        self._events[event_id] = row
        self._remember_event_id(event_id)
        return row

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        return self._events.get(event_id)

    def _remember_event_id(self, event_id: str) -> None:
        if event_id in self._dedupe:
            return
        self._dedupe.add(event_id)
        self._dedupe_order.append(event_id)
        while len(self._dedupe_order) > self._max_dedupe:
            old = self._dedupe_order.popleft()
            self._dedupe.discard(old)

    def _resolve_secret(self, source: str) -> str:
        ref = str(self.secret_refs.get(source) or self.secret_refs.get("*") or "").strip()
        if not ref:
            return ""
        if ref.startswith("env:"):
            return str(os.environ.get(ref.split(":", 1)[1], "")).strip()
        if self.secret_store and hasattr(self.secret_store, "get"):
            from_store = self.secret_store.get(ref)
            if from_store:
                return str(from_store).strip()
        return ref

    @staticmethod
    def _resolve_signature(headers: dict[str, str]) -> str:
        lowered = {k.lower(): v for k, v in headers.items()}
        for key in WebhookService.SIGNATURE_HEADERS:
            value = str(lowered.get(key, "")).strip()
            if value:
                return value
        return ""

    @staticmethod
    def _resolve_timestamp(headers: dict[str, str], payload: Any) -> float | None:
        lowered = {k.lower(): v for k, v in headers.items()}
        for key in WebhookService.TIMESTAMP_HEADERS:
            value = str(lowered.get(key, "")).strip()
            if value:
                try:
                    return float(value)
                except ValueError:
                    continue
        if isinstance(payload, dict):
            for key in ("timestamp", "ts"):
                if key in payload:
                    try:
                        return float(payload.get(key))
                    except Exception:
                        return None
        return None

    @staticmethod
    def _resolve_event_id(headers: dict[str, str], payload: Any) -> str:
        lowered = {k.lower(): v for k, v in headers.items()}
        for key in WebhookService.EVENT_ID_HEADERS:
            value = str(lowered.get(key, "")).strip()
            if value:
                return value
        if isinstance(payload, dict):
            for key in ("id", "event_id", "delivery_id"):
                value = str(payload.get(key) or "").strip()
                if value:
                    return value
        return f"evt_{uuid.uuid4().hex[:24]}"

    @staticmethod
    def _resolve_event_type(*, headers: dict[str, str], payload: Any, source: str) -> str:
        lowered = {k.lower(): v for k, v in headers.items()}
        for key in WebhookService.EVENT_TYPE_HEADERS:
            value = str(lowered.get(key, "")).strip()
            if value:
                return value
        if isinstance(payload, dict):
            for key in ("event", "type"):
                value = str(payload.get(key) or "").strip()
                if value:
                    return value
        return source

    @staticmethod
    def _verify_signature(*, secret: str, timestamp: float, body: bytes, signature: str) -> bool:
        clean_sig = signature.strip()
        if "=" in clean_sig:
            _, clean_sig = clean_sig.split("=", 1)
        payload = f"{int(timestamp)}.".encode("utf-8") + body
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, clean_sig.strip())

    async def _dispatch_action(self, *, source: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        rule = self._match_rule(source=source, event=event)
        if rule is None:
            return {"status": "ignored", "reason": "no_matching_rule"}

        mode = str(getattr(rule, "mode", "agent") or "agent").strip().lower()
        target = str(getattr(rule, "target", "") or "").strip()
        template = str(getattr(rule, "message_template", "") or "").strip()
        session_key_tpl = str(getattr(rule, "session_key", "webhook:{source}") or "webhook:{source}")
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        if mode == "workflow":
            if not self.workflow_runtime:
                return {"status": "ignored", "reason": "workflow_runtime_unavailable", "target": target}
            recipe = self.workflow_runtime.load_recipe(target)
            result = await self.workflow_runtime.run_recipe(
                recipe,
                vars={"source": source, "event": event, "payload_json": payload_json, "payload": payload},
                channel="webhook",
                chat_id=source,
            )
            return {"status": "ok", "mode": "workflow", "target": target, "result": result}

        if not self.agent_runtime:
            return {"status": "ignored", "reason": "agent_runtime_unavailable"}

        if not template:
            template = "Webhook event {event} from {source}\n\nPayload:\n{payload_json}"
        rendered = self._render(template, source=source, event=event, payload_json=payload_json, payload=payload)
        session_key = self._render(session_key_tpl, source=source, event=event, payload_json=payload_json, payload=payload)
        response = await self.agent_runtime.process_direct(
            content=rendered,
            session_key=session_key,
            channel="webhook",
            chat_id=source,
        )
        return {
            "status": "ok",
            "mode": "agent",
            "session_key": session_key,
            "response_preview": str(response or "")[:500],
        }

    def _match_rule(self, *, source: str, event: str) -> Any | None:
        for rule in self.rules:
            rule_source = str(getattr(rule, "source", "*") or "*").strip()
            rule_event = str(getattr(rule, "event", "*") or "*").strip()
            if rule_source not in {"*", source}:
                continue
            if rule_event not in {"*", event}:
                continue
            return rule
        return None

    @staticmethod
    def _render(template: str, **values: Any) -> str:
        try:
            return template.format(**values)
        except Exception:
            return template


def create_webhook_router(
    *,
    service: WebhookService,
    dashboard_auth: Callable | None = None,
) -> APIRouter:
    """Create webhook API router."""
    router = APIRouter()
    deps = [Depends(dashboard_auth)] if dashboard_auth else []

    @router.post("/api/webhooks/{source}")
    async def ingest_webhook(source: str, request: Request) -> dict[str, Any]:
        body = await request.body()
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}
        return await service.handle(
            source=source,
            headers=dict(request.headers.items()),
            body=body,
            payload=payload,
        )

    @router.get("/api/webhooks/events/{event_id}", dependencies=deps)
    async def get_webhook_event(event_id: str) -> dict[str, Any]:
        row = service.get_event(event_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Webhook event not found.")
        return row

    return router
