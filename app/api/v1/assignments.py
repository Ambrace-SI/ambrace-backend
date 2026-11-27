"""Vivas set for a group. Owners set and remove them; members see and start them.

Mounted under /v1/groups before the groups router, so that /assigned is not
read as a group id.
"""

import logging
from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.api.deps import (
    CurrentUser,
    get_assignment_service,
    get_class_insight_service,
)
from app.db.assignment_repository import AssignmentNotFound
from app.db.group_repository import GroupNotFound
from app.db.models import SESSION_ID_PATTERN
from app.db.session_repository import RepositoryUnavailable
from app.schemas.assignment import (
    AssignmentCreateRequest,
    AssignmentResponse,
    AssignmentResultsResponse,
    ClassSummaryResponse,
    StudentAssignmentResponse,
    StudentReportResponse,
)
from app.schemas.class_insight import ClassInsightResponse
from app.schemas.viva import VivaStartResponse
from app.services.assignment_service import (
    AssignmentInvalid,
    AssignmentService,
    ResultNotFound,
)
from app.services.class_insight_service import (
    MIN_RESULTS,
    ClassInsightService,
    InsightUnavailable,
    NotEnoughResults,
)
from app.services.gemini_service import GeminiTokenUnavailable
from app.services.group_service import AccountTypeRequired

logger = logging.getLogger(__name__)


def _never_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(_never_cache)])
limiter = Limiter(key_func=get_remote_address)
Service = Annotated[AssignmentService, Depends(get_assignment_service)]
InsightService = Annotated[ClassInsightService, Depends(get_class_insight_service)]
GroupIdPath = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
AssignmentIdPath = Annotated[str, Path(pattern=SESSION_ID_PATTERN, max_length=53)]
# Cognito subjects are UUIDs; anything else cannot be a student.
StudentIdPath = Annotated[str, Path(pattern=r"^[0-9A-Za-z-]{1,64}$")]


async def _run[T](operation: Awaitable[T], event: str) -> T:
    try:
        return await operation
    except GroupNotFound:
        raise HTTPException(404, "Group not found") from None
    except AssignmentNotFound:
        raise HTTPException(404, "This viva is no longer set.") from None
    except ResultNotFound:
        raise HTTPException(404, "This student hasn't finished this viva.") from None
    except AccountTypeRequired:
        raise HTTPException(403, "Only a student can take a set viva.") from None
    except AssignmentInvalid as invalid:
        # Our own sentence about the request; it contains no personal data.
        raise HTTPException(422, str(invalid)) from None
    except NotEnoughResults:
        raise HTTPException(
            409,
            f"A class insight needs at least {MIN_RESULTS} students to finish this viva.",
        ) from None
    except InsightUnavailable:
        raise HTTPException(
            503,
            "The class insight couldn't be written just now. Please try again.",
            headers={"Retry-After": "5"},
        ) from None
    except GeminiTokenUnavailable:
        logger.error("event=%s error_type=GeminiTokenUnavailable", event)
        raise HTTPException(
            503,
            "The live service is temporarily unavailable. Please try again.",
            headers={"Retry-After": "1"},
        ) from None
    except RepositoryUnavailable:
        logger.error("event=%s error_type=RepositoryUnavailable", event)
        raise HTTPException(
            503, "Vivas are temporarily unavailable. Please retry."
        ) from None
    except Exception as error:
        logger.error("event=%s error_type=%s", event, type(error).__name__)
        raise HTTPException(500, "Something went wrong. Please try again.") from None


@router.get("/assigned", response_model=list[StudentAssignmentResponse])
async def list_assigned(service: Service, current_user: CurrentUser):
    """Every viva set for the signed-in student, in all their groups."""
    return await _run(
        service.list_for_student(current_user.user_id),
        "assignments_student_read_failed",
    )


@router.post(
    "/{group_id}/assignments", response_model=AssignmentResponse, status_code=201
)
async def create_assignment(
    group_id: GroupIdPath,
    request: AssignmentCreateRequest,
    service: Service,
    current_user: CurrentUser,
):
    return await _run(
        service.create(current_user.user_id, group_id, request),
        "assignment_create_failed",
    )


@router.get("/{group_id}/assignments", response_model=list[AssignmentResponse])
async def list_assignments(
    group_id: GroupIdPath, service: Service, current_user: CurrentUser
):
    return await _run(
        service.list_for_group(current_user.user_id, group_id),
        "assignments_read_failed",
    )


@router.get(
    "/{group_id}/assignments/{assignment_id}/results",
    response_model=AssignmentResultsResponse,
)
async def list_results(
    group_id: GroupIdPath,
    assignment_id: AssignmentIdPath,
    service: Service,
    current_user: CurrentUser,
):
    return await _run(
        service.results(current_user.user_id, group_id, assignment_id),
        "assignment_results_read_failed",
    )


@router.get(
    "/{group_id}/assignments/{assignment_id}/results/{student_id}",
    response_model=StudentReportResponse,
)
async def read_report(
    group_id: GroupIdPath,
    assignment_id: AssignmentIdPath,
    student_id: StudentIdPath,
    service: Service,
    current_user: CurrentUser,
):
    """A student's report for the group's owner. Never the transcript."""
    return await _run(
        service.report(current_user.user_id, group_id, assignment_id, student_id),
        "assignment_report_read_failed",
    )


@router.get("/{group_id}/summary", response_model=ClassSummaryResponse)
async def read_summary(
    group_id: GroupIdPath, service: Service, current_user: CurrentUser
):
    return await _run(
        service.summary(current_user.user_id, group_id),
        "class_summary_read_failed",
    )


@router.get(
    "/{group_id}/assignments/{assignment_id}/insight",
    response_model=ClassInsightResponse,
)
async def read_insight(
    group_id: GroupIdPath,
    assignment_id: AssignmentIdPath,
    service: InsightService,
    current_user: CurrentUser,
):
    """The saved class insight, or whether one can be written yet."""
    return await _run(
        service.read(current_user.user_id, group_id, assignment_id),
        "class_insight_read_failed",
    )


@router.post(
    "/{group_id}/assignments/{assignment_id}/insight",
    response_model=ClassInsightResponse,
)
@limiter.limit("5/minute")
async def generate_insight(
    request: Request,
    group_id: GroupIdPath,
    assignment_id: AssignmentIdPath,
    service: InsightService,
    current_user: CurrentUser,
):
    """Write the class insight, or return the saved one if no results changed."""
    return await _run(
        service.generate(current_user.user_id, group_id, assignment_id),
        "class_insight_generate_failed",
    )


@router.delete("/{group_id}/assignments/{assignment_id}", status_code=204)
async def delete_assignment(
    group_id: GroupIdPath,
    assignment_id: AssignmentIdPath,
    service: Service,
    current_user: CurrentUser,
) -> None:
    await _run(
        service.delete(current_user.user_id, group_id, assignment_id),
        "assignment_delete_failed",
    )


@router.post(
    "/{group_id}/assignments/{assignment_id}/start", response_model=VivaStartResponse
)
@limiter.limit("5/minute")
async def start_assignment(
    request: Request,
    group_id: GroupIdPath,
    assignment_id: AssignmentIdPath,
    service: Service,
    current_user: CurrentUser,
):
    """Same answer as /viva/start, so the browser's voice engine is unchanged."""
    return await _run(
        service.start(current_user.user_id, group_id, assignment_id),
        "assignment_start_failed",
    )
