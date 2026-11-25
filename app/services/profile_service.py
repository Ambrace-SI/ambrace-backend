"""The signed-in user's profile and whether they have finished onboarding."""

from app.db.group_repository import GroupRepository
from app.db.profile_repository import (
    AccountType,
    ProfileRepository,
    UserProfile,
    is_educator,
)
from app.schemas.profile import MeResponse, ProfileResponse, ProfileUpdateRequest


class AccountTypeChangeBlocked(Exception):
    """Switching sides now would break a group: the message says what to do first.

    Teacher and parent are interchangeable (wording only). Moving between
    student and teacher or parent is allowed only when nothing depends on the
    current side: a student must have left their groups, and a teacher or
    parent must have deleted theirs. Otherwise a teacher would sit in a class
    list, or a student would own one.
    """


class ProfileService:
    def __init__(self, repository: ProfileRepository, groups: GroupRepository):
        self.repository = repository
        self.groups = groups

    @staticmethod
    def _response(user_id: str, profile: UserProfile | None) -> MeResponse:
        # A profile can only be saved with every required field present, so
        # having one is exactly what "onboarding complete" means.
        return MeResponse(
            user_id=user_id,
            onboarding_complete=profile is not None,
            profile=ProfileResponse.model_validate(profile) if profile else None,
        )

    async def get_me(self, user_id: str) -> MeResponse:
        return self._response(user_id, await self.repository.get(user_id))

    async def _check_switch(
        self, user_id: str, current: AccountType, wanted: AccountType
    ) -> None:
        if is_educator(current) == is_educator(wanted):
            return
        if is_educator(current):
            if await self.groups.list_owned_group_ids(user_id):
                raise AccountTypeChangeBlocked(
                    "Delete your groups before switching to a student account."
                )
        elif await self.groups.list_memberships(user_id):
            raise AccountTypeChangeBlocked(
                "Leave your groups before switching to a teacher or parent account."
            )

    async def _refresh_groups(self, user_id: str, profile: UserProfile) -> None:
        """Groups keep a copy of names for their lists; bring the copies up to date."""
        if is_educator(profile.account_type):
            for group_id in await self.groups.list_owned_group_ids(user_id):
                await self.groups.update_owner_details(
                    group_id, user_id, profile.name, profile.account_type
                )
            return
        for membership in await self.groups.list_memberships(user_id):
            await self.groups.update_member_details(
                membership.group_id, user_id, profile.name, profile.class_level
            )

    async def save_profile(
        self, user_id: str, request: ProfileUpdateRequest
    ) -> MeResponse:
        existing = await self.repository.get(user_id)
        if existing is not None and existing.account_type != request.account_type:
            await self._check_switch(
                user_id, existing.account_type, request.account_type
            )
        saved = await self.repository.save(user_id, UserProfile(**request.model_dump()))
        if existing is not None:
            await self._refresh_groups(user_id, saved)
        return self._response(user_id, saved)
