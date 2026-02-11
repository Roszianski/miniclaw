"""Token-based authentication for the dashboard."""

import secrets
from typing import Callable

from fastapi import Request, HTTPException


def generate_token() -> str:
    """Generate a secure dashboard token."""
    return secrets.token_urlsafe(32)


def require_token(token: str) -> Callable:
    """Dependency that validates the dashboard token."""
    async def verify(request: Request) -> None:
        configured = str(token or "").strip()
        if not configured:
            raise HTTPException(status_code=503, detail="Dashboard token is not configured")

        # Check Authorization header only.
        auth = request.headers.get("Authorization", "")
        parts = auth.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and secrets.compare_digest(parts[1].strip(), configured):
            return
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    return verify
