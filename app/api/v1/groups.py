"""Groups: educators manage them, students join and leave them.

Rosters hold students' names, so every response is marked no-store and no
name, code or group text is ever logged.
"""

import logging
from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.api.deps import CurrentUser, get_group_service
from app.db.group_repository import (
    GroupFull,
    GroupNotFound,
    JoinCodeNotFound,
    JoinCodeTaken,
    MemberNotFound,
)
from app.db.session_repository import RepositoryUnavailable
from app.schemas.group import (
    GroupCreateRequest,
    GroupDetailResponse,
    GroupRenameRequest,
    GroupSummaryResponse,
    InvitePreviewResponse,
    JoinedGroupResponse,
    JoinGroupRequest,
    JoinGroupResponse,
)
from app.services.group_service import (
    AccountTypeRequired,
    GroupLimitReached,
    GroupService,
)

logger = logging.getLogger(__name__)


def _never_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(_never_cache)])
limiter = Limiter(key_func=get_remote_address)
Service = Annotated[GroupService, Depends(get_group_service)]
GroupIdPath = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
StudentIdPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9-]{1,128}$")]
CodePath = Annotated[str, Path(min_length=1, max_length=16)]

_SIDE_MESSAGES = {
    "educator": "Only a teacher or parent account can do this.",
    "student": "Only a student account can join a group.",
}


async def _run[T](operation: Awaitable[T], event: str) -> T:
    try:
        return await operation
    except (GroupNotFound, MemberNotFound):
        raise HTTPException(404, "Group not found") from None
    except JoinCodeNotFound:
        raise HTTPException(404, "That code does not match any group.") from None
    except GroupFull:
        raise HTTPException(409, "This group is full.") from None
    except GroupLimitReached:
        raise HTTPException(409, "You have reached the limit of groups.") from None
    except AccountTypeRequired as error:
        raise HTTPException(403, _SIDE_MESSAGES[error.side]) from None
    except (RepositoryUnavailable, JoinCodeTaken) as error:
        logger.error("event=%s error_type=%s", event, type(error).__name__)
        raise HTTPException(
            503, "Groups are temporarily unavailable. Please retry."
        ) from None
    except Exception as error:
        logger.error("event=%s error_type=%s", event, type(error).__name__)
        raise HTTPException(500, "Something went wrong. Please try again.") from None


# ---------------------------------------------------------------- student side
# Declared before /{group_id} for readability; the id pattern keeps them apart.


@router.get("/joined", response_model=list[JoinedGroupResponse])
async def list_joined_groups(service: Service, current_user: CurrentUser):
    return await _run(
        service.list_joined_groups(current_user.user_id), "groups_joined_read_failed"
    )


@router.get("/invite/{code}", response_model=InvitePreviewResponse)
@limiter.limit("20/minute")
async def preview_invite(
    request: Request, code: CodePath, service: Service, current_user: CurrentUser
):
    return await _run(
        service.preview_invite(current_user.user_id, code), "group_invite_read_failed"
    )


@router.post("/join", response_model=JoinGroupResponse)
@limiter.limit("5/minute")
async def join_group(
    request: Request,
    join_request: JoinGroupRequest,
    service: Service,
    current_user: CurrentUser,
):
    return await _run(
        service.join(current_user.user_id, join_request.code), "group_join_failed"
    )


@router.delete("/joined/{group_id}", status_code=204)
async def leave_group(
    group_id: GroupIdPath, service: Service, current_user: CurrentUser
) -> None:
    await _run(service.leave(current_user.user_id, group_id), "group_leave_failed")


# --------------------------------------------------------------- educator side


@router.post("", response_model=GroupDetailResponse, status_code=201)
async def create_group(
    request: GroupCreateRequest, service: Service, current_user: CurrentUser
):
    return await _run(
        service.create_group(current_user.user_id, request), "group_create_failed"
    )


@router.get("", response_model=list[GroupSummaryResponse])
async def list_owned_groups(service: Service, current_user: CurrentUser):
    return await _run(
        service.list_owned_groups(current_user.user_id), "groups_owned_read_failed"
    )


@router.get("/{group_id}", response_model=GroupDetailResponse)
async def get_group(group_id: GroupIdPath, service: Service, current_user: CurrentUser):
    return await _run(
        service.get_group(current_user.user_id, group_id), "group_read_failed"
    )


@router.patch("/{group_id}", response_model=GroupSummaryResponse)
async def rename_group(
    group_id: GroupIdPath,
    request: GroupRenameRequest,
    service: Service,
    current_user: CurrentUser,
):
    return await _run(
        service.rename_group(current_user.user_id, group_id, request.name),
        "group_rename_failed",
    )


@router.delete("/{group_id}", status_code=204)
async def delete_group(
    group_id: GroupIdPath, service: Service, current_user: CurrentUser
) -> None:
    await _run(
        service.delete_group(current_user.user_id, group_id), "group_delete_failed"
    )


@router.post("/{group_id}/join-code", response_model=GroupSummaryResponse)
async def replace_join_code(
    group_id: GroupIdPath, service: Service, current_user: CurrentUser
):
    return await _run(
        service.replace_join_code(current_user.user_id, group_id),
        "group_code_replace_failed",
    )


@router.delete("/{group_id}/members/{student_id}", status_code=204)
async def remove_member(
    group_id: GroupIdPath,
    student_id: StudentIdPath,
    service: Service,
    current_user: CurrentUser,
) -> None:
    await _run(
        service.remove_member(current_user.user_id, group_id, student_id),
        "group_member_remove_failed",
    )
