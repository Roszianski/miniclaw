"""Identity links and device pairing storage."""

from __future__ import annotations

import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Any


class IdentityStore:
    """Persistent store for identity links and pairing records."""

    def __init__(self, store_path: Path):
        self.store_path = Path(store_path)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.store_path.exists():
            return {"links": [], "pairing_requests": [], "pairings": []}
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except Exception:
            return {"links": [], "pairing_requests": [], "pairings": []}
        if not isinstance(data, dict):
            return {"links": [], "pairing_requests": [], "pairings": []}
        data.setdefault("links", [])
        data.setdefault("pairing_requests", [])
        data.setdefault("pairings", [])
        return data

    def _save(self) -> None:
        self.store_path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _normalize_platform_user_id(value: str) -> str:
        text = str(value or "").strip()
        if "|" in text:
            # Keep a stable, canonical user key for channels that send "id|username".
            return text.split("|", 1)[0].strip() or text
        return text

    def cleanup_expired_requests(self) -> int:
        now_ms = self._now_ms()
        updated = 0
        for req in self._state["pairing_requests"]:
            if not isinstance(req, dict):
                continue
            if req.get("status") != "pending":
                continue
            expires_at = int(req.get("expires_at_ms") or 0)
            if expires_at and expires_at < now_ms:
                req["status"] = "expired"
                req["updated_at_ms"] = now_ms
                updated += 1
        if updated:
            self._save()
        return updated

    def list_links(
        self,
        *,
        canonical_user_id: str | None = None,
        platform: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in self._state["links"]:
            if not isinstance(row, dict):
                continue
            if not include_inactive and not bool(row.get("active", True)):
                continue
            if canonical_user_id and str(row.get("canonical_user_id") or "") != str(canonical_user_id):
                continue
            if platform and str(row.get("platform") or "") != str(platform):
                continue
            out.append(dict(row))
        return out

    def resolve_canonical(self, platform: str, platform_user_id: str) -> str | None:
        normalized_user = self._normalize_platform_user_id(platform_user_id)
        for row in self._state["links"]:
            if not isinstance(row, dict):
                continue
            if not bool(row.get("active", True)):
                continue
            if str(row.get("platform") or "") != str(platform):
                continue
            if str(row.get("platform_user_id") or "") == normalized_user:
                canonical = str(row.get("canonical_user_id") or "").strip()
                return canonical or None
        return None

    def link_identity(
        self,
        *,
        canonical_user_id: str,
        platform: str,
        platform_user_id: str,
        pairing_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now_ms = self._now_ms()
        canonical = str(canonical_user_id).strip()
        platform = str(platform).strip()
        platform_user_id = self._normalize_platform_user_id(platform_user_id)
        if not canonical or not platform or not platform_user_id:
            raise ValueError("canonical_user_id, platform, and platform_user_id are required.")

        existing = None
        for row in self._state["links"]:
            if not isinstance(row, dict):
                continue
            if str(row.get("platform") or "") == platform and str(row.get("platform_user_id") or "") == platform_user_id:
                existing = row
                break

        if existing is None:
            link = {
                "id": f"link_{uuid.uuid4().hex[:14]}",
                "canonical_user_id": canonical,
                "platform": platform,
                "platform_user_id": platform_user_id,
                "pairing_id": str(pairing_id or ""),
                "created_at_ms": now_ms,
                "updated_at_ms": now_ms,
                "active": True,
                "metadata": dict(metadata or {}),
            }
            self._state["links"].append(link)
        else:
            existing["canonical_user_id"] = canonical
            existing["pairing_id"] = str(pairing_id or existing.get("pairing_id") or "")
            existing["active"] = True
            existing["updated_at_ms"] = now_ms
            existing["metadata"] = dict(metadata or existing.get("metadata") or {})
            link = existing

        self._save()
        return dict(link)

    def unlink_identity(self, *, platform: str, platform_user_id: str) -> bool:
        platform = str(platform).strip()
        user_id = self._normalize_platform_user_id(platform_user_id)
        changed = False
        for row in self._state["links"]:
            if not isinstance(row, dict):
                continue
            if str(row.get("platform") or "") == platform and str(row.get("platform_user_id") or "") == user_id:
                row["active"] = False
                row["updated_at_ms"] = self._now_ms()
                changed = True
        if changed:
            self._save()
        return changed

    def create_pairing_request(
        self,
        *,
        platform: str,
        platform_user_id: str,
        device_id: str = "",
        display_name: str = "",
        expires_in_s: int = 600,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.cleanup_expired_requests()
        now_ms = self._now_ms()
        expires_ms = now_ms + max(60, int(expires_in_s)) * 1000
        code = f"{secrets.randbelow(1_000_000):06d}"
        row = {
            "id": f"pairreq_{uuid.uuid4().hex[:14]}",
            "platform": str(platform).strip(),
            "platform_user_id": self._normalize_platform_user_id(platform_user_id),
            "device_id": str(device_id or "").strip(),
            "display_name": str(display_name or "").strip(),
            "code": code,
            "status": "pending",
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
            "expires_at_ms": expires_ms,
            "metadata": dict(metadata or {}),
        }
        self._state["pairing_requests"].append(row)
        self._save()
        return dict(row)

    def list_pairing_requests(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in self._state["pairing_requests"]:
            if not isinstance(row, dict):
                continue
            if not include_inactive and str(row.get("status") or "pending") != "pending":
                continue
            rows.append(dict(row))
        rows.sort(key=lambda r: int(r.get("created_at_ms") or 0), reverse=True)
        return rows

    def approve_pairing(
        self,
        *,
        request_id: str,
        code: str,
        canonical_user_id: str,
        approver: str = "owner",
    ) -> dict[str, Any]:
        self.cleanup_expired_requests()
        req = None
        for row in self._state["pairing_requests"]:
            if isinstance(row, dict) and str(row.get("id") or "") == str(request_id):
                req = row
                break
        if req is None:
            raise ValueError("Pairing request not found.")
        if str(req.get("status") or "") != "pending":
            raise ValueError("Pairing request is not pending.")
        if str(req.get("code") or "") != str(code):
            raise ValueError("Invalid pairing code.")
        if int(req.get("expires_at_ms") or 0) < self._now_ms():
            req["status"] = "expired"
            req["updated_at_ms"] = self._now_ms()
            self._save()
            raise ValueError("Pairing request expired.")

        now_ms = self._now_ms()
        pairing = {
            "id": f"pair_{uuid.uuid4().hex[:14]}",
            "request_id": str(req.get("id") or ""),
            "canonical_user_id": str(canonical_user_id).strip(),
            "platform": str(req.get("platform") or ""),
            "platform_user_id": str(req.get("platform_user_id") or ""),
            "device_id": str(req.get("device_id") or ""),
            "display_name": str(req.get("display_name") or ""),
            "status": "active",
            "approved_at_ms": now_ms,
            "updated_at_ms": now_ms,
            "approved_by": str(approver or "owner"),
            "metadata": dict(req.get("metadata") or {}),
        }
        self._state["pairings"].append(pairing)
        req["status"] = "approved"
        req["updated_at_ms"] = now_ms
        req["approved_at_ms"] = now_ms
        req["approved_by"] = str(approver or "owner")

        link = self.link_identity(
            canonical_user_id=pairing["canonical_user_id"],
            platform=pairing["platform"],
            platform_user_id=pairing["platform_user_id"],
            pairing_id=pairing["id"],
            metadata={"device_id": pairing["device_id"], "display_name": pairing["display_name"]},
        )
        self._save()
        return {"pairing": dict(pairing), "link": link}

    def list_pairings(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in self._state["pairings"]:
            if not isinstance(row, dict):
                continue
            if not include_inactive and str(row.get("status") or "active") != "active":
                continue
            rows.append(dict(row))
        rows.sort(key=lambda r: int(r.get("approved_at_ms") or 0), reverse=True)
        return rows

    def revoke_pairing(
        self,
        *,
        pairing_id: str | None = None,
        platform: str | None = None,
        platform_user_id: str | None = None,
        canonical_user_id: str | None = None,
        revoked_by: str = "owner",
    ) -> int:
        revoked = 0
        now_ms = self._now_ms()
        norm_platform_user = (
            self._normalize_platform_user_id(platform_user_id) if platform_user_id is not None else None
        )
        target_pairing_ids: set[str] = set()

        for pairing in self._state["pairings"]:
            if not isinstance(pairing, dict):
                continue
            if str(pairing.get("status") or "active") != "active":
                continue
            if pairing_id and str(pairing.get("id") or "") != str(pairing_id):
                continue
            if platform and str(pairing.get("platform") or "") != str(platform):
                continue
            if norm_platform_user and str(pairing.get("platform_user_id") or "") != norm_platform_user:
                continue
            if canonical_user_id and str(pairing.get("canonical_user_id") or "") != str(canonical_user_id):
                continue
            pairing["status"] = "revoked"
            pairing["revoked_at_ms"] = now_ms
            pairing["updated_at_ms"] = now_ms
            pairing["revoked_by"] = str(revoked_by or "owner")
            target_pairing_ids.add(str(pairing.get("id") or ""))
            revoked += 1

        if revoked <= 0:
            return 0

        for link in self._state["links"]:
            if not isinstance(link, dict):
                continue
            if str(link.get("pairing_id") or "") in target_pairing_ids:
                link["active"] = False
                link["updated_at_ms"] = now_ms
            elif platform and str(link.get("platform") or "") == str(platform):
                if norm_platform_user and str(link.get("platform_user_id") or "") == norm_platform_user:
                    link["active"] = False
                    link["updated_at_ms"] = now_ms

        self._save()
        return revoked
