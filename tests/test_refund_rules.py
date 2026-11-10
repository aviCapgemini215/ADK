import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from google.genai import types
from adk_app import agent as adk_agent
from adk_app.agent import root_agent
from adk_app.agent import (
    _answer_general_question,
    _current_request_text,
    _process_selected_course_refund,
    _route_agent,
    _select_refund_agent,
    _format_result,
    refund_eligibility_agent,
    refund_processing_agent,
    refund_status_agent,
    user_validation_agent,
)
from app.schemas import RefundRequest
from app.services.refunds import (
    _purchase_registrations_statement,
    is_refund_window_open,
)
from pydantic import ValidationError


def test_refund_is_eligible_at_purchase_time_and_exact_seven_day_boundary() -> None:
    purchased_at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    assert is_refund_window_open(purchased_at, purchased_at)
    assert is_refund_window_open(
        purchased_at, purchased_at + timedelta(days=7)
    )


def test_refund_is_ineligible_after_seven_days() -> None:
    purchased_at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    assert not is_refund_window_open(
        purchased_at, purchased_at + timedelta(days=7, microseconds=1)
    )


def test_future_purchase_is_ineligible() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    assert not is_refund_window_open(now + timedelta(seconds=1), now)


def test_active_purchase_query_excludes_refunded_purchases() -> None:
    active_filter = str(
        _purchase_registrations_statement(
            "user-id", active_only=True
        ).whereclause
    )
    all_filter = str(
        _purchase_registrations_statement(
            "user-id", active_only=False
        ).whereclause
    )

    assert "purchases.payment_status = :payment_status_1" in active_filter
    assert "purchases.payment_status" not in all_filter


def test_refund_request_uses_email_without_purchase_id() -> None:
    request = RefundRequest(
        email="Student@Example.com",
        reason="I no longer need this course",
    )

    assert str(request.email) == "Student@example.com"
    assert "purchase_id" not in request.model_dump()


def test_refund_request_rejects_invalid_email() -> None:
    try:
        RefundRequest(email="not-an-email", reason="Please cancel my course")
    except ValidationError:
        return
    raise AssertionError("Invalid email should fail validation")


def test_adk_has_deterministic_specialist_agents() -> None:
    assert len(root_agent.sub_agents) == 4
    assert {agent.action for agent in root_agent.sub_agents} == {
        "validate",
        "eligibility",
        "process",
        "status",
    }


def test_refund_response_includes_refund_id_and_status() -> None:
    response = _format_result(
        "process",
        "student@example.com",
        [
            {
                "course_title": "Python Basics",
                "refund_status": "processed",
                "refund_id": "refund-123",
                "refund_amount": "49.00",
                "reason": "Purchased by mistake",
            }
        ],
    )

    assert "Refund ID: refund-123" in response
    assert "Status: processed" in response


def test_adk_router_only_processes_explicit_cancellation_requests() -> None:
    assert (
        _select_refund_agent(
            "Check eligibility for this email; do not request or process a refund"
        )
        is refund_eligibility_agent
    )
    assert (
        _select_refund_agent("Is my refund eligible?")
        is refund_eligibility_agent
    )
    assert (
        _select_refund_agent("Cancel my registration for student@example.com")
        is refund_processing_agent
    )
    assert (
        _select_refund_agent("Please process the refund for my course")
        is refund_processing_agent
    )
    assert (
        _select_refund_agent("Check the refund status for student@example.com")
        is refund_status_agent
    )
    assert (
        _select_refund_agent("Is student@example.com a registered user?")
        is user_validation_agent
    )
    assert _select_refund_agent("hi") is None
    assert _select_refund_agent("what can you do?") is None
    assert _select_refund_agent("check my registrations") is user_validation_agent


def test_adk_coordinator_answers_general_questions_with_groq(monkeypatch) -> None:
    ctx = SimpleNamespace(
        invocation_id="test-invocation",
        user_content=types.Content(
            role="user",
            parts=[types.Part(text="What can you help me with?")],
        ),
        session=SimpleNamespace(events=[]),
    )

    async def fake_acompletion(**_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="I can answer general questions and help with course refunds."
                    )
                )
            ]
        )

    monkeypatch.setattr(adk_agent.config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(adk_agent, "acompletion", fake_acompletion)

    async def get_response_text() -> str:
        event = await anext(root_agent._run_async_impl(ctx))
        return event.content.parts[0].text

    response = asyncio.run(get_response_text())

    assert response == "I can answer general questions and help with course refunds."
    assert "Registration validation" not in response


def test_general_question_explains_missing_groq_configuration(monkeypatch) -> None:
    monkeypatch.setattr(adk_agent.config, "GROQ_API_KEY", None)

    response = asyncio.run(_answer_general_question("What can you help with?"))

    assert "Set GROQ_API_KEY in your .env file" in response


def test_general_question_redacts_email_before_sending_to_groq(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(adk_agent.config, "GROQ_API_KEY", "test-key")

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Here's help."))]
        )

    monkeypatch.setattr(adk_agent, "acompletion", fake_acompletion)

    response = asyncio.run(
        _answer_general_question("How can I help student@example.com?")
    )

    assert response == "Here's help."
    messages = captured["messages"]
    assert messages[-1]["content"] == "How can I help [email redacted]?"


def test_refund_status_reports_no_request_when_no_refunds_exist() -> None:
    response = _format_result("status", "student@example.com", [])

    assert response.startswith("No refund request found")
    assert "cancel one of your registered courses" in response


def test_adk_router_uses_current_request_not_earlier_eligibility_question() -> None:
    ctx = SimpleNamespace(
        user_content=types.Content(
            role="user",
            parts=[types.Part(text="Cancel my registration now.")],
        ),
        session=SimpleNamespace(
            events=[
                SimpleNamespace(
                    author="user",
                    content=types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                text="Check refund eligibility for me."
                            )
                        ],
                    ),
                )
            ]
        ),
    )

    current_request = _current_request_text(ctx)

    assert _select_refund_agent(current_request) is refund_processing_agent
    assert (
        _select_refund_agent("Check eligibility; do not process or request a refund")
        is refund_eligibility_agent
    )


def test_email_is_validated_before_any_refund_action() -> None:
    email = "student@example.com"
    ctx = SimpleNamespace(
        user_content=types.Content(
            role="user",
            parts=[types.Part(text=f"Cancel CPP for {email}")],
        ),
        session=SimpleNamespace(events=[]),
    )

    target, selected_email = _route_agent(ctx)

    assert target is user_validation_agent
    assert selected_email == email


def test_follow_up_cancel_uses_the_validated_email_and_course_choice() -> None:
    email = "student@example.com"
    ctx = SimpleNamespace(
        user_content=types.Content(
            role="user",
            parts=[types.Part(text="CPP")],
        ),
        session=SimpleNamespace(
            events=[
                SimpleNamespace(
                    author="user",
                    content=types.Content(
                        role="user",
                        parts=[types.Part(text=f"Hello, {email}")],
                    ),
                ),
                SimpleNamespace(
                    author="user_validation_agent",
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                text=(
                                    f"Registration validation for {email}:\n"
                                    "Course: CPP\nWould you like to cancel "
                                    "one of these courses or check your refund status?"
                                )
                            )
                        ],
                    ),
                ),
                SimpleNamespace(
                    author="refund_processing_agent",
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                text="Which course would you like to cancel? Your courses: CPP."
                            )
                        ],
                    ),
                ),
            ]
        ),
    )

    target, selected_email = _route_agent(ctx)

    assert target is refund_processing_agent
    assert selected_email == email


def test_course_refund_rolls_back_read_transaction_before_processing(
    monkeypatch,
) -> None:
    class ReadSession:
        rolled_back = False

        async def rollback(self) -> None:
            self.rolled_back = True

    session = ReadSession()
    expected_results = [{"refund_status": "processed"}]

    async def process_after_rollback(
        actual_session, email, reason, *, course_title
    ):
        assert actual_session is session
        assert actual_session.rolled_back
        assert email == "student@example.com"
        assert reason == "Cancellation requested by user"
        assert course_title == "CPP"
        return expected_results

    monkeypatch.setattr(
        "adk_app.agent.process_refunds_for_email",
        process_after_rollback,
    )

    async def run() -> list[dict[str, object]]:
        return await _process_selected_course_refund(
            session,  # type: ignore[arg-type]
            "student@example.com",
            "Cancellation requested by user",
            "CPP",
        )

    assert asyncio.run(run()) == expected_results
