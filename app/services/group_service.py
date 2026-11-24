"""Groups: an educator creates them, students join them with a code.

Who may do what is decided here, from the caller's verified id and profile:

- Only an educator can create a group, and only its owner can read its roster,
  rename it, replace its code or remove a member. Another educator's group is
  reported as not found, so ids cannot be probed.
- Only a student can join. Joining with the code is the student's consent to
  share their results with the group's owner, which is why an educator can
  never add a student directly.
"""

import re
import secrets
import uuid
from typing import Literal

from app.db.group_repository import (
    Group,
    GroupMember,
    GroupNotFound,
    GroupRepository,
    JoinCodeNotFound,
    JoinCodeTaken,
)
from app.db.models import utc_now
from app.db.profile_repository import ProfileRepository, UserProfile, is_educator
from app.schemas.group import (
    GroupCreateRequest,
    GroupDetailResponse,
    GroupMemberResponse,
    GroupSummaryResponse,
    InvitePreviewResponse,
    JoinedGroupResponse,
    JoinGroupResponse,
)

# No 0/O, 1/I/L: codes are read aloud in class and typed from WhatsApp.
JOIN_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
JOIN_CODE_LENGTH = 6
_JOIN_CODE_PATTERN = re.compile(f"^[{JOIN_CODE_ALPHABET}]{{{JOIN_CODE_LENGTH}}}$")
_CODE_ATTEMPTS = 5

MAX_MEMBERS_PER_GROUP = 200
MAX_GROUPS_PER_EDUCATOR = 50


Side = Literal["educator", "student"]


class AccountTypeRequired(PermissionError):
    """The caller is not on the side of the app (educator or student) this needs."""

    def __init__(self, side: Side):
        super().__init__(side)
        self.side = side


class GroupLimitReached(Exception):
    pass


def new_join_code() -> str:
    return "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(JOIN_CODE_LENGTH))


def normalise_join_code(raw: str) -> str | None:
    """Accept what people actually type: lower case, spaces or a dash in the middle."""
    code = re.sub(r"[\s-]", "", raw).upper()
    return code if _JOIN_CODE_PATTERN.match(code) else None


def _summary(group: Group) -> GroupSummaryResponse:
    return GroupSummaryResponse.model_validate(group)


class GroupService:
    def __init__(self, groups: GroupRepository, profiles: ProfileRepository):
        self.groups = groups
        self.profiles = profiles

    async def _require(self, user_id: str, side: Side) -> UserProfile:
        profile = await self.profiles.get(user_id)
        if profile is None:
            raise AccountTypeRequired(side)
        if is_educator(profile.account_type) != (side == "educator"):
            raise AccountTypeRequired(side)
        return profile

    async def _owned_group(self, user_id: str, group_id: str) -> Group:
        group = await self.groups.get_group(group_id)
        if group is None or group.owner_id != user_id:
            raise GroupNotFound
        return group

    # ---------------------------------------------------------------- educator

    async def create_group(
        self, user_id: str, request: GroupCreateRequest
    ) -> GroupDetailResponse:
        educator = await self._require(user_id, "educator")
        if (
            len(await self.groups.list_owned_group_ids(user_id))
            >= MAX_GROUPS_PER_EDUCATOR
        ):
            raise GroupLimitReached
        for _ in range(_CODE_ATTEMPTS):
            group = Group(
                group_id=uuid.uuid4().hex,
                name=request.name,
                owner_id=user_id,
                owner_name=educator.name,
                owner_type=educator.account_type,
                class_level=request.class_level,
                join_code=new_join_code(),
                created_at=utc_now(),
            )
            try:
                await self.groups.create_group(group)
            except JoinCodeTaken:
                continue
            return GroupDetailResponse(**_summary(group).model_dump(), members=[])
        raise JoinCodeTaken

    async def list_owned_groups(self, user_id: str) -> list[GroupSummaryResponse]:
        group_ids = await self.groups.list_owned_group_ids(user_id)
        groups = await self.groups.get_groups(group_ids)
        return [_summary(g) for g in sorted(groups, key=lambda g: g.created_at)]

    async def get_group(self, user_id: str, group_id: str) -> GroupDetailResponse:
        group = await self._owned_group(user_id, group_id)
        members = await self.groups.list_members(group_id)
        return GroupDetailResponse(
            **_summary(group).model_dump(),
            members=[
                GroupMemberResponse.model_validate(m)
                for m in sorted(members, key=lambda m: m.name.casefold())
            ],
        )

    async def rename_group(
        self, user_id: str, group_id: str, name: str
    ) -> GroupSummaryResponse:
        return _summary(await self.groups.rename_group(group_id, user_id, name))

    async def replace_join_code(
        self, user_id: str, group_id: str
    ) -> GroupSummaryResponse:
        group = await self._owned_group(user_id, group_id)
        for _ in range(_CODE_ATTEMPTS):
            code = new_join_code()
            try:
                await self.groups.replace_join_code(
                    group_id, user_id, group.join_code, code
                )
            except JoinCodeTaken:
                continue
            return _summary(group.model_copy(update={"join_code": code}))
        raise JoinCodeTaken

    async def delete_group(self, user_id: str, group_id: str) -> None:
        await self.groups.delete_group(await self._owned_group(user_id, group_id))

    async def remove_member(self, user_id: str, group_id: str, student_id: str) -> None:
        await self._owned_group(user_id, group_id)
        await self.groups.remove_member(group_id, student_id)

    # ----------------------------------------------------------------- student

    async def _group_for_code(self, raw_code: str) -> tuple[str, Group]:
        code = normalise_join_code(raw_code)
        group_id = await self.groups.find_group_id_by_code(code) if code else None
        group = await self.groups.get_group(group_id) if group_id else None
        if code is None or group is None:
            raise JoinCodeNotFound
        return code, group

    async def preview_invite(
        self, user_id: str, raw_code: str
    ) -> InvitePreviewResponse:
        _, group = await self._group_for_code(raw_code)
        return InvitePreviewResponse(
            group_name=group.name,
            owner_name=group.owner_name,
            owner_type=group.owner_type,
            class_level=group.class_level,
            already_member=await self.groups.is_member(user_id, group.group_id),
        )

    async def join(self, user_id: str, raw_code: str) -> JoinGroupResponse:
        student = await self._require(user_id, "student")
        code, group = await self._group_for_code(raw_code)
        member = GroupMember(
            student_id=user_id,
            name=student.name,
            class_level=student.class_level,
            joined_at=utc_now(),
        )
        # Joining twice changes nothing; the answer says so, so the student is
        # told they are already in the group rather than welcomed again.
        added = await self.groups.add_member(
            group.group_id, code, member, MAX_MEMBERS_PER_GROUP
        )
        joined = await self._joined(user_id)
        this_group = next(g for g in joined if g.group_id == group.group_id)
        return JoinGroupResponse(**this_group.model_dump(), already_member=not added)

    async def _joined(self, user_id: str) -> list[JoinedGroupResponse]:
        memberships = await self.groups.list_memberships(user_id)
        joined_at = {m.group_id: m.joined_at for m in memberships}
        groups = await self.groups.get_groups(list(joined_at))
        return sorted(
            (
                JoinedGroupResponse(
                    group_id=g.group_id,
                    name=g.name,
                    owner_name=g.owner_name,
                    owner_type=g.owner_type,
                    class_level=g.class_level,
                    joined_at=joined_at[g.group_id],
                )
                for g in groups
            ),
            key=lambda g: g.joined_at,
        )

    async def list_joined_groups(self, user_id: str) -> list[JoinedGroupResponse]:
        return await self._joined(user_id)

    async def leave(self, user_id: str, group_id: str) -> None:
        await self.groups.remove_member(group_id, user_id)
