"""
This module configures all FastAPI dependency injection bindings
for the application.

It centralizes construction of service instances such as the LLM client
and VivaService, ensuring a clean and testable architecture.

Key Design Principles:
----------------------
1. **Dependency Injection (DI)**
   Services are provided through FastAPI's `Depends()` mechanism,
   which makes route handlers simple, testable, and free of manual wiring.

2. **Singleton LLM Service**
   The LLM provider (GeminiService) is expensive to initialize and
   should not be created per request.
   We use `@lru_cache` to build a process-wide singleton.

3. **Composable Services**
   Higher-level services (e.g., VivaService) receive dependencies
   via constructor injection, aligning with clean architecture
   and promoting loose coupling.
"""

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

# Re-export authentication dependencies for easy import in routes
from app.core.auth import (
    CurrentUser,
)
from app.core.config import settings
from app.db.assignment_repository import AssignmentRepository
from app.db.database import get_repository, init_db
from app.db.group_repository import GroupRepository
from app.db.insight_repository import InsightRepository
from app.db.profile_repository import ProfileRepository
from app.interfaces.insight_generator import ClassInsightGenerator
from app.interfaces.llm_client import LLMClient
from app.services.assignment_service import (
    AssignmentResultRecorder,
    AssignmentService,
)
from app.services.class_insight_service import ClassInsightService
from app.services.gemini_service import GeminiService
from app.services.group_service import GroupService
from app.services.insights.registry import build_generator
from app.services.profile_service import ProfileService
from app.services.viva_service import VivaService


# ----------------------------------------------------------------------
# LLM Service Provider
# ----------------------------------------------------------------------
@lru_cache
def get_llm_service() -> LLMClient:
    """
    Construct and return a cached singleton instance of the LLM service.

    FastAPI evaluates dependency providers once per process by using `lru_cache`,
    ensuring:
    - Only one instance of GeminiService is created.
    - Efficient use of resources (e.g., network clients, auth config).
    - Thread-safe reuse across requests.

    Returns:
        LLMClient: A GeminiService instance implementing the LLMClient interface.
    """
    return GeminiService()


# ----------------------------------------------------------------------
# Viva Service Provider
# ----------------------------------------------------------------------
async def get_viva_service(
    current_user: CurrentUser,
    llm_service: Annotated[LLMClient, Depends(get_llm_service)],
) -> VivaService:
    """
    Wire the lifecycle service after authentication has succeeded.

    CurrentUser is deliberately a dependency even though its value is not used
    here: unauthenticated requests must return 401 before database initialization.
    SDK initialization runs off the event loop; requests reuse the cached client.
    """
    await init_db()
    sessions = get_repository()
    # A viva started from an assignment hands its report to whoever set it.
    results = AssignmentResultRecorder(
        AssignmentRepository(sessions.client, sessions.table_name)
    )
    return VivaService(llm_client=llm_service, repository=sessions, results=results)


# ----------------------------------------------------------------------
# Profile Service Provider
# ----------------------------------------------------------------------
async def get_profile_service(current_user: CurrentUser) -> ProfileService:
    """
    Wire the profile service after authentication has succeeded.

    As with the viva service, CurrentUser is a dependency so that an
    unauthenticated request returns 401 before any database work. Profiles live
    in the sessions table, so the same cached DynamoDB client is reused.
    """
    await init_db()
    sessions = get_repository()
    return ProfileService(
        ProfileRepository(sessions.client, sessions.table_name),
        GroupRepository(sessions.client, sessions.table_name),
    )


# ----------------------------------------------------------------------
# Group Service Provider
# ----------------------------------------------------------------------
async def get_group_service(current_user: CurrentUser) -> GroupService:
    """Groups share the sessions table and its cached client, like profiles."""
    await init_db()
    sessions = get_repository()
    return GroupService(
        GroupRepository(sessions.client, sessions.table_name),
        ProfileRepository(sessions.client, sessions.table_name),
    )


# ----------------------------------------------------------------------
# Assignment Service Provider
# ----------------------------------------------------------------------
async def get_assignment_service(
    current_user: CurrentUser,
    llm_service: Annotated[LLMClient, Depends(get_llm_service)],
) -> AssignmentService:
    """Set vivas need groups, profiles and the viva lifecycle to start one."""
    viva = await get_viva_service(current_user, llm_service)
    sessions = viva.repository
    return AssignmentService(
        GroupRepository(sessions.client, sessions.table_name),
        AssignmentRepository(sessions.client, sessions.table_name),
        ProfileRepository(sessions.client, sessions.table_name),
        viva,
    )


# ----------------------------------------------------------------------
# Class Insight Providers
# ----------------------------------------------------------------------
@lru_cache
def get_insight_generator() -> ClassInsightGenerator:
    """Chosen by CLASS_INSIGHT_PROVIDER; generators hold no per-request state."""
    return build_generator(settings)


async def get_class_insight_service(
    current_user: CurrentUser,
    generator: Annotated[ClassInsightGenerator, Depends(get_insight_generator)],
) -> ClassInsightService:
    await init_db()
    sessions = get_repository()
    return ClassInsightService(
        GroupRepository(sessions.client, sessions.table_name),
        AssignmentRepository(sessions.client, sessions.table_name),
        InsightRepository(sessions.client, sessions.table_name),
        generator,
    )
