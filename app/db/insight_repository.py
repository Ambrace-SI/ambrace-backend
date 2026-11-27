"""The saved class insight for one set viva.

    GROUP#<gid> / INSIGHT#<aid>

One item per viva, overwritten on refresh. Students are stored by id, not
name, so a renamed student reads correctly. Deleting the viva or the group
removes it (see AssignmentRepository and GroupRepository).
"""

import asyncio
import datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

from app.db.session_repository import RepositoryUnavailable


class StoredTheme(BaseModel):
    point: str
    student_ids: list[str]
    misconception: str | None = None


class StoredSupport(BaseModel):
    student_id: str
    reason: str


class StoredSuggestion(BaseModel):
    action: str
    addresses: str | None = None


class StoredInsight(BaseModel):
    assignment_id: str
    overview: str
    strengths: list[StoredTheme]
    gaps: list[StoredTheme]
    students_to_support: list[StoredSupport]
    suggestions: list[StoredSuggestion]
    coverage_note: str
    # Which generator (model + prompt version) wrote it, for comparing experiments.
    generator_id: str
    # The results it was built from: new ones since then make it out of date.
    source_session_ids: list[str]
    generated_at: datetime.datetime


def insight_key(group_id: str, assignment_id: str) -> dict[str, Any]:
    return {"pk": {"S": f"GROUP#{group_id}"}, "sk": {"S": f"INSIGHT#{assignment_id}"}}


class InsightRepository:
    def __init__(self, client: Any, table_name: str):
        self.client = client
        self.table_name = table_name

    async def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(getattr(self.client, operation), **kwargs)
        except (ClientError, BotoCoreError):
            raise RepositoryUnavailable("Insight storage is unavailable") from None

    async def get(self, group_id: str, assignment_id: str) -> StoredInsight | None:
        response = await self._call(
            "get_item",
            TableName=self.table_name,
            Key=insight_key(group_id, assignment_id),
            ConsistentRead=True,
        )
        if "Item" not in response:
            return None
        return StoredInsight.model_validate_json(response["Item"]["data"]["S"])

    async def save(self, group_id: str, insight: StoredInsight) -> None:
        await self._call(
            "put_item",
            TableName=self.table_name,
            Item={
                **insight_key(group_id, insight.assignment_id),
                "data": {"S": insight.model_dump_json()},
            },
        )
