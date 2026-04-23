"""HTTP controllers — one APIRouter per domain area, mounted in main.py."""

from .health import router as health_router
from .policy import router as policy_router
from .request import router as request_router
from .task import router as task_router

__all__ = [
    "health_router",
    "policy_router",
    "request_router",
    "task_router",
]
