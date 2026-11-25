"""The signed-in user's own profile. Profile text is never logged."""

import logging
from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.deps import CurrentUser, get_profile_service
from app.db.session_repository import RepositoryUnavailable
from app.schemas.profile import MeResponse, ProfileUpdateRequest
from app.services.profile_service import AccountTypeChangeBlocked, ProfileService

logger = logging.getLogger(__name__)


def _never_cache(response: Response) -> None:
    """A profile is personal data: no browser or proxy may keep a copy."""
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(_never_cache)])
Service = Annotated[ProfileService, Depends(get_profile_service)]


async def _run(operation: Awaitable[MeResponse], event: str) -> MeResponse:
    """Translate failures without exposing storage details or profile data.

    Only an event name and the error type are logged: an error message could
    contain a student's name or address.
    """
    try:
        return await operation
    except AccountTypeChangeBlocked as blocked:
        # The message is ours and says what to do; it contains no profile data.
        raise HTTPException(409, str(blocked)) from None
    except RepositoryUnavailable:
        logger.error("event=%s error_type=RepositoryUnavailable", event)
        raise HTTPException(
            503, "Profile storage is temporarily unavailable. Please retry."
        ) from None
    except Exception as error:
        logger.error("event=%s error_type=%s", event, type(error).__name__)
        raise HTTPException(500, "Something went wrong. Please try again.") from None


@router.get("", response_model=MeResponse)
async def get_me(service: Service, current_user: CurrentUser):
    """Who is signed in, and whether they still need to complete onboarding."""
    return await _run(service.get_me(current_user.user_id), "profile_read_failed")


@router.put("", response_model=MeResponse)
async def save_profile(
    request: ProfileUpdateRequest, service: Service, current_user: CurrentUser
):
    """Create the profile at onboarding, or replace it when the user edits it."""
    return await _run(
        service.save_profile(current_user.user_id, request), "profile_save_failed"
    )
