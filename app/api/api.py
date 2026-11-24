"""
This module defines the main API router for the application.

It centralizes and aggregates all versioned API routers (e.g., /v1)
into a single FastAPI `APIRouter` instance. The resulting `api_router`
is imported and included by `app.main:app`, ensuring a clean separation
between the API layer and the application initialization layer.

By structuring routers this way, the application:
- Maintains clear API versioning.
- Keeps route definitions modular and scalable.
- Supports incremental addition of new API versions (v2, v3, etc.)
  without impacting existing clients.
"""

from fastapi import APIRouter

from app.api.v1 import assignments, groups, me, viva

# ----------------------------------------------------------------------
# Main API Router
# ----------------------------------------------------------------------
# This router acts as the root router for the entire application.
# All sub-routers (v1, v2, etc.) should be included here.
api_router = APIRouter()

# ----------------------------------------------------------------------
# Include Versioned Routers
# ----------------------------------------------------------------------
# Attaches all v1 viva routes with a clear URL prefix.
# Example final routes:
#   POST /v1/viva/start
#   POST /v1/viva/conclude
#   GET  /v1/viva/{session_id}
#
# The `tags` parameter organizes endpoints in API docs (Swagger / ReDoc).
api_router.include_router(
    viva.router,
    prefix="/v1/viva",
    tags=["viva"],
)

# The signed-in user's own profile:
#   GET /v1/me
#   PUT /v1/me
api_router.include_router(
    me.router,
    prefix="/v1/me",
    tags=["me"],
)

# Vivas set for a group. Before the groups router: "/assigned" must not be
# taken for a group id.
api_router.include_router(
    assignments.router,
    prefix="/v1/groups",
    tags=["assignments"],
)

# Groups: educators own them, students join them with a code.
api_router.include_router(
    groups.router,
    prefix="/v1/groups",
    tags=["groups"],
)
