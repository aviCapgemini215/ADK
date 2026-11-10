import re
from collections.abc import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import User
from app.services.email import send_refund_email
from app.services.refunds import (
    RefundError,
    check_refunds_for_email,
    process_refunds_for_email,
)

EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


def _request_text(ctx: InvocationContext) -> str:
    messages = [
        " ".join(
            part.text
            for part in event.content.parts or []
            if part.text
        )
        for event in ctx.session.events
        if event.author == "user" and event.content
    ]
    current_parts = ctx.user_content.parts if ctx.user_content else []
    current = " ".join(part.text for part in current_parts or [] if part.text)
    if current:
        messages.append(current)
    return "\n".join(messages)


def _current_request_text(ctx: InvocationContext) -> str:
    current_parts = ctx.user_content.parts if ctx.user_content else []
    return " ".join(part.text for part in current_parts or [] if part.text)


def _last_assistant_text(ctx: InvocationContext) -> str:
    for event in reversed(ctx.session.events):
        if event.author != "user" and event.content:
            return " ".join(
                part.text
                for part in event.content.parts or []
                if part.text
            )
    return ""


def _has_registration_for_email(ctx: InvocationContext, email: str) -> bool:
    expected = f"registration validation for {email.lower()}:"
    return any(
        event.author != "user"
        and event.content
        and expected in " ".join(
            part.text.lower()
            for part in event.content.parts or []
            if part.text
        )
        for event in ctx.session.events
    )


def _selected_email(ctx: InvocationContext, request: str) -> str | None:
    current = _current_request_text(ctx)
    current_matches = EMAIL_PATTERN.findall(current)
    if current_matches:
        return current_matches[-1]
    history_matches = EMAIL_PATTERN.findall(request)
    return history_matches[-1] if history_matches else None


def _format_result(action: str, email: str, results: list[dict[str, object]]) -> str:
    if action == "status" and not results:
        return (
            f"No refund request found for {email}.\n"
            "Would you like to cancel one of your registered courses?"
        )

    if action == "process":
        heading = f"Refund request results for {email}:"
    elif action == "eligibility":
        heading = f"Refund eligibility for {email}:"
    elif action == "status":
        heading = f"Refund status for {email}:"
    else:
        heading = f"Registration validation for {email}:"

    lines = [heading]
    for result in results:
        lines.extend(
            [
                f"- Course: {result['course_title']}",
                f"  Status: {result['refund_status']}",
                f"  Refund ID: {result['refund_id'] or 'Not created'}",
                f"  Amount: {result['refund_amount']}",
                f"  Reason: {result['reason']}",
            ]
        )
        if result.get("request_reason"):
            lines.append(f"  Request reason: {result['request_reason']}")
    if action == "validate":
        lines.append(
            "Would you like to cancel one of these courses or check your refund status?"
        )
    elif action == "status":
        lines.append("Would you like to cancel one of your registered courses?")
    return "\n".join(lines)


def _matching_course_titles(
    request: str, results: list[dict[str, object]]
) -> list[str]:
    matches: list[str] = []
    for result in results:
        title = str(result["course_title"])
        if re.search(
            rf"(?<!\w){re.escape(title)}(?!\w)",
            request,
            re.IGNORECASE,
        ):
            matches.append(title)
    return list(dict.fromkeys(matches))


def _course_selection_prompt(results: list[dict[str, object]]) -> str:
    titles = list(dict.fromkeys(str(item["course_title"]) for item in results))
    course_list = ", ".join(titles)
    return f"Which course would you like to cancel? Your courses: {course_list}."


async def _process_selected_course_refund(
    session: AsyncSession,
    email: str,
    reason: str,
    course_title: str,
) -> list[dict[str, object]]:
    await session.rollback()
    return await process_refunds_for_email(
        session,
        email,
        reason,
        course_title=course_title,
    )


class EmailRefundSpecialist(BaseAgent):
    action: str

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        request = _request_text(ctx)
        email = _selected_email(ctx, request)
        if email is None:
            response_text = "Please provide the email address used to register."
        else:
            try:
                async with SessionLocal() as session:
                    if self.action == "validate":
                        user_result = await session.execute(
                            select(User).where(
                                func.lower(User.email) == email.strip().lower()
                            )
                        )
                        if user_result.scalar_one_or_none() is None:
                            raise RefundError(404, "User not found")
                        results = await check_refunds_for_email(
                            session, email, active_only=True
                        )
                    elif self.action == "eligibility":
                        results = await check_refunds_for_email(
                            session, email, active_only=True
                        )
                    elif self.action == "status":
                        user_result = await session.execute(
                            select(User).where(
                                func.lower(User.email) == email.strip().lower()
                            )
                        )
                        if user_result.scalar_one_or_none() is None:
                            raise RefundError(404, "User not found")
                        try:
                            results = await check_refunds_for_email(
                                session, email, active_only=False
                            )
                        except RefundError as exc:
                            if exc.detail != (
                                "No course registrations found for this user"
                            ):
                                raise
                            results = []
                        results = [
                            result
                            for result in results
                            if result["existing_request"]
                        ]
                    elif self.action == "process":
                        registrations = await check_refunds_for_email(
                            session, email, active_only=True
                        )
                        current_request = _current_request_text(ctx)
                        matching_titles = _matching_course_titles(
                            current_request, registrations
                        )
                        if len(matching_titles) != 1:
                            if len(matching_titles) > 1:
                                response_text = (
                                    "Please choose one course to cancel: "
                                    + ", ".join(matching_titles)
                                    + "."
                                )
                            else:
                                response_text = _course_selection_prompt(
                                    registrations
                                )
                            yield Event(
                                invocation_id=ctx.invocation_id,
                                author=self.name,
                                content=types.Content(
                                    role="model",
                                    parts=[types.Part(text=response_text)],
                                ),
                            )
                            return
                        reason_match = re.search(
                            r"\breason\s*(?:is|:)\s*(.+?)(?:[.!?\r\n]|$)",
                            request,
                            re.IGNORECASE,
                        )
                        reason = (
                            reason_match.group(1).strip()
                            if reason_match
                            else "Cancellation requested by user"
                        )
                        results = await _process_selected_course_refund(
                            session,
                            email,
                            reason,
                            matching_titles[0],
                        )
                        should_send_email = not all(
                            bool(result["existing_request"])
                            for result in results
                        )
                    else:
                        raise ValueError(f"Unknown refund action: {self.action}")
                response_text = _format_result(self.action, email, results)
                if self.action == "process" and should_send_email:
                    email_result = await send_refund_email(email, results)
                    delivery_status = "sent" if email_result["sent"] else "not sent"
                    response_text += (
                        f"\n\nEmail notification: {delivery_status}."
                    )
                    if not email_result["sent"]:
                        response_text += f" {email_result['detail']}"
            except RefundError as exc:
                if self.action == "validate" and exc.detail == "User not found":
                    response_text = (
                        f"User {email} does not exist. Please check the email address."
                    )
                elif (
                    self.action == "validate"
                    and exc.detail == "No course registrations found for this user"
                ):
                    response_text = (
                        f"User {email} exists, but has no course registrations."
                    )
                elif (
                    self.action in {"validate", "eligibility", "process"}
                    and exc.detail == "No active course purchases found for this user"
                ):
                    response_text = (
                        f"User {email} has no active course purchases."
                    )
                else:
                    response_text = (
                        f"Could not complete the request for {email}: {exc.detail}."
                    )

        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=response_text)],
            ),
        )


user_validation_agent = EmailRefundSpecialist(
    name="user_validation_agent",
    action="validate",
    description="Validate a registered email and show its course registrations.",
)
refund_eligibility_agent = EmailRefundSpecialist(
    name="refund_eligibility_agent",
    action="eligibility",
    description="Check the seven-day refund policy by registered email.",
)
refund_processing_agent = EmailRefundSpecialist(
    name="refund_processing_agent",
    action="process",
    description="Process eligible course refunds and return refund IDs and statuses.",
)
refund_status_agent = EmailRefundSpecialist(
    name="refund_status_agent",
    action="status",
    description="Show refund IDs and current statuses for a registered email.",
)


def _select_refund_agent(request: str) -> EmailRefundSpecialist | None:
    normalized = request.lower()
    if re.search(
        r"\b(status|already\s+requested|check\s+my\s+refund)\b", normalized
    ):
        return refund_status_agent
    if re.search(
        r"\b(cancel\w*|cancell?ation|refund\s+request|"
        r"request\s+(?:a\s+)?refund|"
        r"(?:process|submit|issue|initiate|create|make)\s+"
        r"(?:(?:a|the|my|this)\s+)?refund|"
        r"(?:want|need|get)\s+(?:a\s+)?refund|refund\s+my|"
        r"money\s+back|return\s+my\s+money)\b",
        normalized,
    ) and not re.search(
        r"\b(?:do\s+not|don't|never)\s+(?:request|process|submit|issue|"
        r"initiate|create|make|cancel)\b",
        normalized,
    ):
        return refund_processing_agent
    if re.search(
        r"\b(eligible|eligibility|policy|refund\s+window|within\s+7\s+days)\b",
        normalized,
    ):
        return refund_eligibility_agent
    if re.search(
        r"\b(register|registered|registrations?|enroll(?:ed|ment)?|"
        r"purchases?|courses?\s+(?:did\s+i\s+buy|i\s+(?:bought|purchased)))\b",
        normalized,
    ):
        return user_validation_agent
    return None


def _fallback_response(request: str) -> str:
    return (
        "Please enter your registered email address so I can check whether "
        "your account exists and list your courses."
    )


def _follow_up_prompt() -> str:
    return (
        "Would you like to cancel one of your registered courses or check "
        "your refund status? To cancel, include the course name."
    )


def _route_agent(
    ctx: InvocationContext,
) -> tuple[EmailRefundSpecialist | None, str | None]:
    current_request = _current_request_text(ctx)
    target = _select_refund_agent(current_request)
    request = _request_text(ctx)
    email = _selected_email(ctx, request)
    previous_response = _last_assistant_text(ctx).lower()
    if email is not None and not _has_registration_for_email(ctx, email):
        return user_validation_agent, email
    if (
        target is None
        and email is not None
        and "which course would you like to cancel?" in previous_response
    ):
        return refund_processing_agent, email
    return target, email


class EmailRefundCoordinator(BaseAgent):
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        current_request = _current_request_text(ctx)
        target, email = _route_agent(ctx)
        if target is None:
            response_text = (
                _follow_up_prompt()
                if email is not None
                and _has_registration_for_email(ctx, email)
                else _fallback_response(current_request)
            )
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=response_text)],
                ),
            )
            return
        async for event in target.run_async(ctx):
            yield event


root_agent = EmailRefundCoordinator(
    name="course_refund_workflow",
    description=(
        "Route email-based registration validation, refund eligibility, "
        "cancellation, and status requests to deterministic database specialists."
    ),
    sub_agents=[
        user_validation_agent,
        refund_eligibility_agent,
        refund_processing_agent,
        refund_status_agent,
    ],
)
