"""The /api/v1 REST API: every module router, mounted under one prefix."""

from fastapi import APIRouter

from praxima.modules.agents.api.router import router as agents_router
from praxima.modules.catalog.api.router import router as catalog_router
from praxima.modules.engagement.api.router import router as engagement_router
from praxima.modules.iam.api.router import router as iam_router
from praxima.modules.knowledge.api.router import router as knowledge_router
from praxima.modules.tenancy.api.router import router as tenancy_router


def build_router() -> APIRouter:
    router = APIRouter()
    router.include_router(iam_router)
    router.include_router(tenancy_router)
    router.include_router(catalog_router)
    router.include_router(agents_router)
    router.include_router(knowledge_router)
    router.include_router(engagement_router)
    return router
