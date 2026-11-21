import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import VivaSession, utc_now
from app.db.session_repository import SessionConflict
from app.services.viva_service import VivaService


def session(**changes):
    return VivaSession(
        user_id="owner",
        student_name="Student",
        title="Plants",
        topic="Plants",
        class_level="7",
        **changes,
    )


def test_token_failure_never_creates_record():
    repository = SimpleNamespace(create_session=AsyncMock())
    service = VivaService(
        SimpleNamespace(
            create_ephemeral_token=AsyncMock(side_effect=RuntimeError("failed"))
        ),
        repository,
    )
    with pytest.raises(RuntimeError):
        asyncio.run(service.start_new_viva_session(SimpleNamespace(), "owner"))
    repository.create_session.assert_not_awaited()


@pytest.mark.parametrize("state", ["completed", "abandoned"])
def test_abandon_preserves_terminal_results(state):
    saved = session(status=state)
    repository = SimpleNamespace(
        get_session_for_user=AsyncMock(return_value=saved), update_session=AsyncMock()
    )
    result = asyncio.run(
        VivaService(SimpleNamespace(), repository).abandon_viva_session(
            saved.id, "owner"
        )
    )
    assert result == {"status": state}
    repository.update_session.assert_not_awaited()


def test_abandon_marks_active_session_without_feedback():
    saved = session()
    repository = SimpleNamespace(
        get_session_for_user=AsyncMock(return_value=saved), update_session=AsyncMock()
    )
    result = asyncio.run(
        VivaService(SimpleNamespace(), repository).abandon_viva_session(
            saved.id, "owner"
        )
    )
    assert result == {"status": "abandoned"}
    assert saved.feedback is None
    assert saved.ended_at is not None


def test_expiry_targets_only_active_session_and_honors_concurrent_completion():
    saved = session(expires_at=utc_now() - datetime.timedelta(seconds=1))
    winner = saved.model_copy(update={"status": "completed"})
    repository = SimpleNamespace(
        get_session_for_user=AsyncMock(return_value=winner),
        update_session=AsyncMock(side_effect=SessionConflict()),
    )
    result = asyncio.run(
        VivaService(SimpleNamespace(), repository)._reconcile_expired_session(saved)
    )
    assert result.status == "completed"
    assert repository.update_session.await_args.args[0].status == "abandoned"


def test_conclusion_does_not_complete_after_abandon_wins():
    saved = session()
    winner = saved.model_copy(update={"status": "abandoned"})
    repository = SimpleNamespace(
        get_session_for_user=AsyncMock(side_effect=[saved, winner]),
        update_session=AsyncMock(side_effect=SessionConflict()),
    )
    with pytest.raises(SessionConflict, match="no longer active"):
        asyncio.run(
            VivaService(SimpleNamespace(), repository).conclude_viva_session(
                saved.id,
                8,
                "feedback",
                [],
                [],
                "owner",
            )
        )


def test_history_entry_carries_score_and_subject_when_known():
    """A list can tell vivas apart without opening each report."""
    finished = session(
        status="completed",
        feedback={
            "score": 7,
            "summary": "Clear.",
            "strong_points": [],
            "areas_of_improvement": [],
        },
        curriculum_selection={
            "class_level": 7,
            "subject_id": "science",
            "subject_name": "Science",
            "chapters": [{"id": "plants", "name": "Plants"}],
        },
    )
    left_early = session(status="abandoned")
    service = VivaService(
        SimpleNamespace(),
        SimpleNamespace(
            list_sessions_for_user=AsyncMock(
                return_value=([finished, left_early], "next")
            )
        ),
    )

    page = asyncio.run(service.get_user_history("owner", 20, None))

    first, second = page["sessions"]
    assert (first["score"], first["subject_name"]) == (7, "Science")
    assert (second["score"], second["subject_name"]) == (None, None)
    assert "feedback" not in first and "transcript" not in first
    assert page["next_cursor"] == "next"
