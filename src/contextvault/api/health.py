"""Health check endpoint."""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


@router.get("/health")
async def health() -> HealthResponse:
    """Liveness probe — returns 200 while the app is running."""
    return HealthResponse(status="ok")
