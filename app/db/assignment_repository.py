"""Vivas a teacher or parent sets for a group, and the results that come back.

Items, all in the sessions table:

    GROUP#<gid> / ASSIGN#<aid>             the viva set for the group
    GROUP#<gid> / RESULT#<aid>#<sid>       a student's report, for the owner
    USER#<sid>  / DONE#<gid>#<aid>         the same completion, for the student

A result is written from both sides in one transaction, and only while the
assignment still exists and the student is still in the group. The owner's
copy holds the report, never the transcript.
"""

import asyncio
import datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

from app.db.models import VivaFeedback
from app.db.session_repository import RepositoryUnavailable
from app.domain.curriculum import CurriculumSelection

_TRANSACTION_LIMIT = 100


class AssignmentNotFound(Exception):
    pass


class Assignment(BaseModel):
    assignment_id: str
    group_id: str
    # The chapter name, so lists read naturally without loading the selection.
    title: str
    curriculum_selection: CurriculumSelection
    # A calendar day, not a moment: "due Friday" means the whole of Friday.
    due_on: datetime.date | None = None
    created_at: datetime.datetime


class AssignmentResult(BaseModel):
    """A student's finished viva for one assignment, as its owner reads it."""

    assignment_id: str
    student_id: str
    # Their name when they took it, so the result still reads after they leave.
    student_name: str | None = None
    session_id: str
    feedback: VivaFeedback
    completed_at: datetime.datetime


class Completion(BaseModel):
    """The student's own note that they finished an assignment."""

    group_id: str
    assignment_id: str
    session_id: str
    score: int
    completed_at: datetime.datetime


def _assignment_key(group_id: str, assignment_id: str) -> dict[str, Any]:
    return {"pk": {"S": f"GROUP#{group_id}"}, "sk": {"S": f"ASSIGN#{assignment_id}"}}


def _result_key(group_id: str, assignment_id: str, student_id: str) -> dict[str, Any]:
    return {
        "pk": {"S": f"GROUP#{group_id}"},
        "sk": {"S": f"RESULT#{assignment_id}#{student_id}"},
    }


def _completion_key(
    student_id: str, group_id: str, assignment_id: str
) -> dict[str, Any]:
    return {
        "pk": {"S": f"USER#{student_id}"},
        "sk": {"S": f"DONE#{group_id}#{assignment_id}"},
    }


class AssignmentRepository:
    """Storage for assignments and results. Callers decide who may do what."""

    def __init__(self, client: Any, table_name: str):
        self.client = client
        self.table_name = table_name

    async def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(getattr(self.client, operation), **kwargs)
        except (ClientError, BotoCoreError):
            raise RepositoryUnavailable("Assignment storage is unavailable") from None

    async def _query(self, pk: str, prefix: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        start: dict[str, Any] | None = None
        while True:
            response = await self._call(
                "query",
                TableName=self.table_name,
                KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
                ExpressionAttributeValues={
                    ":pk": {"S": pk},
                    ":prefix": {"S": prefix},
                },
                ConsistentRead=True,
                **({"ExclusiveStartKey": start} if start else {}),
            )
            items.extend(response.get("Items", []))
            start = response.get("LastEvaluatedKey")
            if not start:
                return items

    # ---------------------------------------------------------- assignments

    async def create_assignment(self, assignment: Assignment) -> None:
        await self._call(
            "put_item",
            TableName=self.table_name,
            Item={
                **_assignment_key(assignment.group_id, assignment.assignment_id),
                "data": {"S": assignment.model_dump_json()},
            },
            ConditionExpression="attribute_not_exists(pk)",
        )

    async def get_assignment(self, group_id: str, assignment_id: str) -> Assignment:
        response = await self._call(
            "get_item",
            TableName=self.table_name,
            Key=_assignment_key(group_id, assignment_id),
            ConsistentRead=True,
        )
        if "Item" not in response:
            raise AssignmentNotFound
        return Assignment.model_validate_json(response["Item"]["data"]["S"])

    async def list_assignments(self, group_id: str) -> list[Assignment]:
        """Newest first: ids sort by creation time."""
        items = await self._query(f"GROUP#{group_id}", "ASSIGN#")
        assignments = [Assignment.model_validate_json(i["data"]["S"]) for i in items]
        return sorted(assignments, key=lambda a: a.assignment_id, reverse=True)

    async def delete_assignment(self, group_id: str, assignment_id: str) -> None:
        """Remove the assignment, then every result and completion recorded for it."""
        await self._call(
            "delete_item",
            TableName=self.table_name,
            Key=_assignment_key(group_id, assignment_id),
        )
        deletes: list[dict[str, Any]] = []
        for item in await self._query(f"GROUP#{group_id}", f"RESULT#{assignment_id}#"):
            student_id = item["student_id"]["S"]
            deletes.append({"Delete": {"Key": {"pk": item["pk"], "sk": item["sk"]}}})
            deletes.append(
                {
                    "Delete": {
                        "Key": _completion_key(student_id, group_id, assignment_id)
                    }
                }
            )
        for start in range(0, len(deletes), _TRANSACTION_LIMIT):
            batch = deletes[start : start + _TRANSACTION_LIMIT]
            for action in batch:
                action["Delete"]["TableName"] = self.table_name
            await self._call("transact_write_items", TransactItems=batch)

    # -------------------------------------------------------------- results

    async def list_results(
        self, group_id: str, assignment_id: str
    ) -> list[AssignmentResult]:
        items = await self._query(f"GROUP#{group_id}", f"RESULT#{assignment_id}#")
        return [AssignmentResult.model_validate_json(i["data"]["S"]) for i in items]

    async def list_group_results(self, group_id: str) -> list[AssignmentResult]:
        """Every result in the group, for the class summary."""
        items = await self._query(f"GROUP#{group_id}", "RESULT#")
        return [AssignmentResult.model_validate_json(i["data"]["S"]) for i in items]

    async def get_result(
        self, group_id: str, assignment_id: str, student_id: str
    ) -> AssignmentResult | None:
        response = await self._call(
            "get_item",
            TableName=self.table_name,
            Key=_result_key(group_id, assignment_id, student_id),
            ConsistentRead=True,
        )
        if "Item" not in response:
            return None
        return AssignmentResult.model_validate_json(response["Item"]["data"]["S"])

    async def list_completions(self, student_id: str) -> list[Completion]:
        items = await self._query(f"USER#{student_id}", "DONE#")
        return [Completion.model_validate_json(i["data"]["S"]) for i in items]

    async def record_result(self, group_id: str, result: AssignmentResult) -> bool:
        """Record the first finished viva. Returns False when nothing was written.

        Nothing is written if the assignment was deleted, the student has left
        the group, or a result already exists (the first one counts).
        """
        completion = Completion(
            group_id=group_id,
            assignment_id=result.assignment_id,
            session_id=result.session_id,
            score=result.feedback.score,
            completed_at=result.completed_at,
        )
        exists = "attribute_exists(pk)"
        absent = "attribute_not_exists(pk)"
        items = [
            {
                "ConditionCheck": {
                    "Key": _assignment_key(group_id, result.assignment_id),
                    "ConditionExpression": exists,
                }
            },
            {
                "ConditionCheck": {
                    "Key": {
                        "pk": {"S": f"GROUP#{group_id}"},
                        "sk": {"S": f"MEMBER#{result.student_id}"},
                    },
                    "ConditionExpression": exists,
                }
            },
            {
                "Put": {
                    "Item": {
                        **_result_key(
                            group_id, result.assignment_id, result.student_id
                        ),
                        "student_id": {"S": result.student_id},
                        "data": {"S": result.model_dump_json()},
                    },
                    "ConditionExpression": absent,
                }
            },
            {
                "Put": {
                    "Item": {
                        **_completion_key(
                            result.student_id, group_id, result.assignment_id
                        ),
                        "data": {"S": completion.model_dump_json()},
                    },
                    "ConditionExpression": absent,
                }
            },
        ]
        for item in items:
            next(iter(item.values()))["TableName"] = self.table_name
        try:
            await asyncio.to_thread(
                self.client.transact_write_items, TransactItems=items
            )
        except ClientError as error:
            if (
                error.response.get("Error", {}).get("Code")
                == "TransactionCanceledException"
            ):
                return False
            raise RepositoryUnavailable("Assignment storage is unavailable") from None
        except BotoCoreError:
            raise RepositoryUnavailable("Assignment storage is unavailable") from None
        return True
