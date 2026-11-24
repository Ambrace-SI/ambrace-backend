"""Groups an educator owns and the students who joined them.

Items, all in the sessions table:

    GROUP#<gid>      / META            the group; member_count is kept atomically
    GROUP#<gid>      / MEMBER#<sid>    a student in the group
    USER#<owner>     / OWNS#<gid>      lets an educator list their groups
    USER#<sid>       / MEMBEROF#<gid>  lets a student list the groups they joined
    JOINCODE#<code>  / JOINCODE        resolves an invite code to its group

Every relationship is written from both sides in one transaction, so the
group's roster and the student's list of groups can never disagree. Group
attributes are stored as top-level attributes rather than one JSON document
because conditions (owner, current join code, member limit) must read them.
"""

import asyncio
import datetime
from collections.abc import Sequence
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

from app.db.profile_repository import EducatorType
from app.db.session_repository import RepositoryUnavailable

# BatchGetItem accepts at most 100 keys per request.
_BATCH_GET_LIMIT = 100
_BATCH_GET_ATTEMPTS = 3
# TransactWriteItems accepts at most 100 actions.
_TRANSACTION_LIMIT = 100


class GroupNotFound(Exception):
    """Missing, or owned by someone else: the two are deliberately the same."""


class MemberNotFound(Exception):
    pass


class JoinCodeNotFound(Exception):
    pass


class JoinCodeTaken(Exception):
    """A freshly generated code collided with an existing one; try another."""


class GroupFull(Exception):
    pass


class Group(BaseModel):
    group_id: str
    name: str
    owner_id: str
    owner_name: str
    # Teacher or parent: only changes how students see the group described.
    owner_type: EducatorType
    class_level: int | None = None
    # Reserved for schools: a group will belong to an organisation whose admins
    # can see it. Always None until organisations exist.
    org_id: str | None = None
    join_code: str
    member_count: int = 0
    created_at: datetime.datetime


class GroupMember(BaseModel):
    student_id: str
    # Copied from the student's profile when they join.
    name: str
    class_level: int | None = None
    joined_at: datetime.datetime


class Membership(BaseModel):
    """One group a student belongs to, as seen from the student's side."""

    group_id: str
    joined_at: datetime.datetime


def _group_key(group_id: str) -> dict[str, Any]:
    return {"pk": {"S": f"GROUP#{group_id}"}, "sk": {"S": "META"}}


def _code_key(code: str) -> dict[str, Any]:
    return {"pk": {"S": f"JOINCODE#{code}"}, "sk": {"S": "JOINCODE"}}


def _member_key(group_id: str, student_id: str) -> dict[str, Any]:
    return {"pk": {"S": f"GROUP#{group_id}"}, "sk": {"S": f"MEMBER#{student_id}"}}


def _membership_key(student_id: str, group_id: str) -> dict[str, Any]:
    return {"pk": {"S": f"USER#{student_id}"}, "sk": {"S": f"MEMBEROF#{group_id}"}}


def _optional_number(value: int | None) -> dict[str, Any]:
    return {"NULL": True} if value is None else {"N": str(value)}


def _read_optional_number(attribute: dict[str, Any]) -> int | None:
    return None if "NULL" in attribute else int(attribute["N"])


def _group_item(group: Group) -> dict[str, Any]:
    return {
        **_group_key(group.group_id),
        "group_id": {"S": group.group_id},
        "name": {"S": group.name},
        "owner_id": {"S": group.owner_id},
        "owner_name": {"S": group.owner_name},
        "owner_type": {"S": group.owner_type},
        "class_level": _optional_number(group.class_level),
        "org_id": {"NULL": True} if group.org_id is None else {"S": group.org_id},
        "join_code": {"S": group.join_code},
        "member_count": {"N": str(group.member_count)},
        "created_at": {"S": group.created_at.isoformat()},
    }


def _decode_group(item: dict[str, Any]) -> Group:
    return Group(
        group_id=item["group_id"]["S"],
        name=item["name"]["S"],
        owner_id=item["owner_id"]["S"],
        owner_name=item["owner_name"]["S"],
        owner_type=item["owner_type"]["S"],
        class_level=_read_optional_number(item["class_level"]),
        org_id=item["org_id"].get("S"),
        join_code=item["join_code"]["S"],
        member_count=int(item["member_count"]["N"]),
        created_at=datetime.datetime.fromisoformat(item["created_at"]["S"]),
    )


def _decode_member(item: dict[str, Any]) -> GroupMember:
    return GroupMember(
        student_id=item["student_id"]["S"],
        name=item["name"]["S"],
        class_level=_read_optional_number(item["class_level"]),
        joined_at=datetime.datetime.fromisoformat(item["joined_at"]["S"]),
    )


class _Cancelled(Exception):
    """A conditional write or transaction was refused; reasons align with its items."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons

    def failed(self, index: int) -> bool:
        return (
            index < len(self.reasons)
            and self.reasons[index] == "ConditionalCheckFailed"
        )


class GroupRepository:
    """Storage for groups. Callers decide who may do what; this only keeps it consistent."""

    def __init__(self, client: Any, table_name: str):
        self.client = client
        self.table_name = table_name

    async def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        """Keep the synchronous SDK off the event loop and hide provider details."""
        try:
            return await asyncio.to_thread(getattr(self.client, operation), **kwargs)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code == "TransactionCanceledException":
                reasons = error.response.get("CancellationReasons", [])
                raise _Cancelled(
                    [reason.get("Code", "") for reason in reasons]
                ) from None
            if code == "ConditionalCheckFailedException":
                raise _Cancelled(["ConditionalCheckFailed"]) from None
            raise RepositoryUnavailable("Group storage is unavailable") from None
        except BotoCoreError:
            raise RepositoryUnavailable("Group storage is unavailable") from None

    async def _transact(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            next(iter(item.values()))["TableName"] = self.table_name
        await self._call("transact_write_items", TransactItems=items)

    async def _query_all(
        self, pk: str, sk_prefix: str | None = None
    ) -> list[dict[str, Any]]:
        """Every item in a partition, or those whose sort key has the prefix."""
        condition = "pk = :pk"
        values: dict[str, Any] = {":pk": {"S": pk}}
        if sk_prefix:
            condition += " AND begins_with(sk, :prefix)"
            values[":prefix"] = {"S": sk_prefix}
        items: list[dict[str, Any]] = []
        start: dict[str, Any] | None = None
        while True:
            response = await self._call(
                "query",
                TableName=self.table_name,
                KeyConditionExpression=condition,
                ExpressionAttributeValues=values,
                ConsistentRead=True,
                **({"ExclusiveStartKey": start} if start else {}),
            )
            items.extend(response.get("Items", []))
            start = response.get("LastEvaluatedKey")
            if not start:
                return items

    # ------------------------------------------------------------------ reads

    async def get_group(self, group_id: str) -> Group | None:
        response = await self._call(
            "get_item",
            TableName=self.table_name,
            Key=_group_key(group_id),
            ConsistentRead=True,
        )
        return _decode_group(response["Item"]) if "Item" in response else None

    async def get_groups(self, group_ids: Sequence[str]) -> list[Group]:
        """Several groups at once, in the order asked; missing groups are skipped."""
        found: dict[str, Group] = {}
        for start in range(0, len(group_ids), _BATCH_GET_LIMIT):
            keys = [
                _group_key(gid) for gid in group_ids[start : start + _BATCH_GET_LIMIT]
            ]
            request: dict[str, Any] = {self.table_name: {"Keys": keys}}
            for _ in range(_BATCH_GET_ATTEMPTS):
                response = await self._call("batch_get_item", RequestItems=request)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    group = _decode_group(item)
                    found[group.group_id] = group
                request = response.get("UnprocessedKeys") or {}
                if not request:
                    break
            if request:
                raise RepositoryUnavailable("Group storage is unavailable")
        return [found[gid] for gid in group_ids if gid in found]

    async def list_owned_group_ids(self, owner_id: str) -> list[str]:
        items = await self._query_all(f"USER#{owner_id}", "OWNS#")
        return [item["sk"]["S"].removeprefix("OWNS#") for item in items]

    async def list_memberships(self, student_id: str) -> list[Membership]:
        items = await self._query_all(f"USER#{student_id}", "MEMBEROF#")
        return [
            Membership(
                group_id=item["sk"]["S"].removeprefix("MEMBEROF#"),
                joined_at=datetime.datetime.fromisoformat(item["joined_at"]["S"]),
            )
            for item in items
        ]

    async def list_members(self, group_id: str) -> list[GroupMember]:
        items = await self._query_all(f"GROUP#{group_id}", "MEMBER#")
        return [_decode_member(item) for item in items]

    async def is_member(self, student_id: str, group_id: str) -> bool:
        response = await self._call(
            "get_item",
            TableName=self.table_name,
            Key=_membership_key(student_id, group_id),
            ConsistentRead=True,
            ProjectionExpression="pk",
        )
        return "Item" in response

    async def find_group_id_by_code(self, code: str) -> str | None:
        response = await self._call(
            "get_item",
            TableName=self.table_name,
            Key=_code_key(code),
            ConsistentRead=True,
        )
        item = response.get("Item")
        return item["group_id"]["S"] if item else None

    # ----------------------------------------------------------------- writes

    async def create_group(self, group: Group) -> None:
        """Write the group, its owner's index entry and its join code together."""
        try:
            await self._transact(
                [
                    {
                        "Put": {
                            "Item": _group_item(group),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "Item": {
                                "pk": {"S": f"USER#{group.owner_id}"},
                                "sk": {"S": f"OWNS#{group.group_id}"},
                                "created_at": {"S": group.created_at.isoformat()},
                            }
                        }
                    },
                    {
                        "Put": {
                            "Item": {
                                **_code_key(group.join_code),
                                "group_id": {"S": group.group_id},
                            },
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except _Cancelled as cancelled:
            if cancelled.failed(2):
                raise JoinCodeTaken from None
            raise RepositoryUnavailable("Group could not be created") from None

    async def rename_group(self, group_id: str, owner_id: str, name: str) -> Group:
        try:
            response = await self._call(
                "update_item",
                TableName=self.table_name,
                Key=_group_key(group_id),
                UpdateExpression="SET #name = :name",
                ConditionExpression="owner_id = :owner",
                ExpressionAttributeNames={"#name": "name"},
                ExpressionAttributeValues={
                    ":name": {"S": name},
                    ":owner": {"S": owner_id},
                },
                ReturnValues="ALL_NEW",
            )
        except _Cancelled:
            raise GroupNotFound from None
        return _decode_group(response["Attributes"])

    async def replace_join_code(
        self, group_id: str, owner_id: str, old_code: str, new_code: str
    ) -> None:
        """Swap the code atomically: the old one stops working as the new one starts."""
        try:
            await self._transact(
                [
                    {
                        "Put": {
                            "Item": {
                                **_code_key(new_code),
                                "group_id": {"S": group_id},
                            },
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {"Delete": {"Key": _code_key(old_code)}},
                    {
                        "Update": {
                            "Key": _group_key(group_id),
                            "UpdateExpression": "SET join_code = :new",
                            "ConditionExpression": "owner_id = :owner AND join_code = :old",
                            "ExpressionAttributeValues": {
                                ":new": {"S": new_code},
                                ":old": {"S": old_code},
                                ":owner": {"S": owner_id},
                            },
                        }
                    },
                ]
            )
        except _Cancelled as cancelled:
            if cancelled.failed(0):
                raise JoinCodeTaken from None
            raise GroupNotFound from None

    async def add_member(
        self, group_id: str, code: str, member: GroupMember, max_members: int
    ) -> bool:
        """Join through `code`. Returns False when the student was already a member.

        The code is checked inside the transaction, so a code the owner has just
        replaced can no longer be used, even by a request already in flight.
        """
        joined_at = {"S": member.joined_at.isoformat()}
        try:
            await self._transact(
                [
                    {
                        "Update": {
                            "Key": _group_key(group_id),
                            "UpdateExpression": "ADD member_count :one",
                            "ConditionExpression": "join_code = :code AND member_count < :max",
                            "ExpressionAttributeValues": {
                                ":one": {"N": "1"},
                                ":code": {"S": code},
                                ":max": {"N": str(max_members)},
                            },
                        }
                    },
                    {
                        "Put": {
                            "Item": {
                                **_member_key(group_id, member.student_id),
                                "student_id": {"S": member.student_id},
                                "name": {"S": member.name},
                                "class_level": _optional_number(member.class_level),
                                "joined_at": joined_at,
                            },
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "Item": {
                                **_membership_key(member.student_id, group_id),
                                "joined_at": joined_at,
                            },
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except _Cancelled as cancelled:
            if cancelled.failed(1) or cancelled.failed(2):
                return False
            group = await self.get_group(group_id)
            if group is None or group.join_code != code:
                raise JoinCodeNotFound from None
            raise GroupFull from None
        return True

    async def remove_member(self, group_id: str, student_id: str) -> None:
        """Used both when a student leaves and when the owner removes them."""
        try:
            await self._transact(
                [
                    {
                        "Delete": {
                            "Key": _member_key(group_id, student_id),
                            "ConditionExpression": "attribute_exists(pk)",
                        }
                    },
                    {"Delete": {"Key": _membership_key(student_id, group_id)}},
                    {
                        "Update": {
                            "Key": _group_key(group_id),
                            "UpdateExpression": "ADD member_count :minus_one",
                            "ConditionExpression": "attribute_exists(pk)",
                            "ExpressionAttributeValues": {":minus_one": {"N": "-1"}},
                        }
                    },
                ]
            )
        except _Cancelled:
            raise MemberNotFound from None

    # ------------------------------------------------- profile changes, deletion

    async def update_owner_details(
        self, group_id: str, owner_id: str, owner_name: str, owner_type: EducatorType
    ) -> None:
        """Keep the owner's name and type shown to students in step with their profile."""
        try:
            await self._call(
                "update_item",
                TableName=self.table_name,
                Key=_group_key(group_id),
                UpdateExpression="SET owner_name = :name, owner_type = :type",
                ConditionExpression="owner_id = :owner",
                ExpressionAttributeValues={
                    ":name": {"S": owner_name},
                    ":type": {"S": owner_type},
                    ":owner": {"S": owner_id},
                },
            )
        except _Cancelled:
            pass  # Deleted meanwhile: nothing left to keep in step.

    async def update_member_details(
        self, group_id: str, student_id: str, name: str, class_level: int | None
    ) -> None:
        """Keep a student's name and class in the owner's list in step with their profile."""
        try:
            await self._call(
                "update_item",
                TableName=self.table_name,
                Key=_member_key(group_id, student_id),
                UpdateExpression="SET #name = :name, class_level = :class",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeNames={"#name": "name"},
                ExpressionAttributeValues={
                    ":name": {"S": name},
                    ":class": _optional_number(class_level),
                },
            )
        except _Cancelled:
            pass  # Removed meanwhile: nothing left to keep in step.

    async def delete_group(self, group: Group) -> None:
        """Delete a group, its code, its owner's entry and every membership.

        The group, owner entry and code go first, in one transaction, so the
        group vanishes and its code stops working at once. Its contents are
        then removed in batches. If that is interrupted, what is left behind
        only points at a group that no longer exists, which every list skips.
        """
        try:
            await self._transact(
                [
                    {
                        "Delete": {
                            "Key": _group_key(group.group_id),
                            "ConditionExpression": "owner_id = :owner",
                            "ExpressionAttributeValues": {
                                ":owner": {"S": group.owner_id}
                            },
                        }
                    },
                    {
                        "Delete": {
                            "Key": {
                                "pk": {"S": f"USER#{group.owner_id}"},
                                "sk": {"S": f"OWNS#{group.group_id}"},
                            }
                        }
                    },
                    {"Delete": {"Key": _code_key(group.join_code)}},
                ]
            )
        except _Cancelled:
            raise GroupNotFound from None

        deletes: list[dict[str, Any]] = []
        for item in await self._query_all(f"GROUP#{group.group_id}"):
            deletes.append({"Delete": {"Key": {"pk": item["pk"], "sk": item["sk"]}}})
            if item["sk"]["S"].startswith("MEMBER#"):
                student_id = item["sk"]["S"].removeprefix("MEMBER#")
                deletes.append(
                    {"Delete": {"Key": _membership_key(student_id, group.group_id)}}
                )
        for start in range(0, len(deletes), _TRANSACTION_LIMIT):
            await self._transact(deletes[start : start + _TRANSACTION_LIMIT])
