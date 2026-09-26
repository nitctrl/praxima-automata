"""The /api/v1 REST API: every module router, mounted under one prefix."""

from fastapi import APIRouter

from praxima.modules.iam.api.router import router as iam_router
from praxima.modules.tenancy.api.router import router as tenancy_router


def build_router() -> APIRouter:
    router = APIRouter()
    router.include_router(iam_router)
    router.include_router(tenancy_router)
    return router
