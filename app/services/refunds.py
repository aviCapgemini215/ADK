from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PAYMENT_GATEWAY_MODE
from app.models import Course, Purchase, Refund, User

REFUND_WINDOW = timedelta(days=7)


class RefundError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def is_refund_window_open(purchased_at: datetime, now: datetime) -> bool:
    if purchased_at.tzinfo is None:
        purchased_at = purchased_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return purchased_at <= now <= purchased_at + REFUND_WINDOW


async def get_verified_purchase(
    session: AsyncSession, user_id: UUID, purchase_id: UUID, *, lock: bool = False
) -> tuple[User, Purchase]:
    user = await session.get(User, user_id)
    if user is None:
        raise RefundError(404, "User not found")

    statement = select(Purchase).where(Purchase.id == purchase_id)
    if lock:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    purchase = result.scalar_one_or_none()
    if purchase is None or purchase.user_id != user_id:
        raise RefundError(404, "Purchase not found for this user")
    return user, purchase


async def check_refund_eligibility(
    session: AsyncSession, user_id: UUID, purchase_id: UUID
) -> dict[str, object]:
    _, purchase = await get_verified_purchase(session, user_id, purchase_id)
    if purchase.payment_status != "paid":
        return {"eligible": False, "reason": "Purchase is not in paid status"}

    now = datetime.now(timezone.utc)
    eligible = is_refund_window_open(purchase.purchased_at, now)
    return {
        "eligible": eligible,
        "reason": "Within the 7-day refund window"
        if eligible
        else "The 7-day refund window has expired or the purchase date is invalid",
        "purchased_at": purchase.purchased_at.isoformat(),
    }


async def process_refund(
    session: AsyncSession, user_id: UUID, purchase_id: UUID, reason: str
) -> Refund:
    async with session.begin():
        _, purchase = await get_verified_purchase(
            session, user_id, purchase_id, lock=True
        )

        existing_result = await session.execute(
            select(Refund).where(Refund.purchase_id == purchase_id)
        )
        if existing_result.scalar_one_or_none() is not None:
            raise RefundError(409, "A refund request already exists for this purchase")
        if purchase.payment_status != "paid":
            raise RefundError(409, "Purchase is not eligible because it is not paid")

        now = datetime.now(timezone.utc)
        eligible = is_refund_window_open(purchase.purchased_at, now)
        if not eligible:
            status = "rejected"
            processed_at = now
        elif PAYMENT_GATEWAY_MODE == "demo":
            status = "processed"
            processed_at = now
            purchase.payment_status = "refunded"
        else:
            status = "pending"
            processed_at = None

        refund = Refund(
            purchase_id=purchase.id,
            user_id=user_id,
            reason=reason,
            refund_status=status,
            refund_amount=purchase.amount,
            processed_at=processed_at,
        )
        session.add(refund)
        await session.flush()
        return refund


async def check_refunds_for_email(
    session: AsyncSession, email: str
) -> list[dict[str, object]]:
    user_result = await session.execute(
        select(User).where(func.lower(User.email) == email.strip().lower())
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        raise RefundError(404, "User not found")

    registrations = await session.execute(
        select(Purchase, Course)
        .join(Course, Course.id == Purchase.course_id)
        .where(Purchase.user_id == user.id)
        .order_by(Purchase.purchased_at.desc())
    )
    rows = registrations.all()
    if not rows:
        raise RefundError(404, "No course registrations found for this user")

    refund_result = await session.execute(
        select(Refund).where(Refund.user_id == user.id)
    )
    refunds_by_purchase = {refund.purchase_id: refund for refund in refund_result.scalars()}
    now = datetime.now(timezone.utc)
    outcomes: list[dict[str, object]] = []
    for purchase, course in rows:
        refund = refunds_by_purchase.get(purchase.id)
        if refund is not None:
            status = refund.refund_status
            reason = "A refund request already exists"
        elif purchase.payment_status != "paid":
            status = "not_eligible"
            reason = "Purchase is not in paid status"
        else:
            is_eligible = is_refund_window_open(purchase.purchased_at, now)
            status = "eligible" if is_eligible else "rejected"
            reason = (
                "Within the 7-day refund window"
                if is_eligible
                else "The 7-day refund window has expired or the purchase date is invalid"
            )
        outcomes.append(
            {
                "refund_id": refund.id if refund else None,
                "course_id": course.id,
                "course_title": course.title,
                "refund_status": status,
                "refund_amount": refund.refund_amount if refund else purchase.amount,
                "reason": reason,
                "request_reason": refund.reason if refund else None,
                "existing_request": refund is not None,
            }
        )
    return outcomes


async def process_refunds_for_email(
    session: AsyncSession,
    email: str,
    reason: str,
    *,
    course_title: str | None = None,
) -> list[dict[str, object]]:
    async with session.begin():
        user_result = await session.execute(
            select(User)
            .where(func.lower(User.email) == email.strip().lower())
            .with_for_update()
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            raise RefundError(404, "User not found")

        registrations_statement = (
            select(Purchase, Course)
            .join(Course, Course.id == Purchase.course_id)
            .where(Purchase.user_id == user.id)
            .order_by(Purchase.purchased_at.desc())
            .with_for_update(of=Purchase)
        )
        if course_title is not None:
            registrations_statement = registrations_statement.where(
                func.lower(Course.title) == course_title.strip().lower()
            )
        registrations = await session.execute(registrations_statement)
        rows = registrations.all()
        if not rows:
            if course_title is not None:
                raise RefundError(
                    404,
                    f"Course '{course_title}' is not registered to this user",
                )
            raise RefundError(404, "No course registrations found for this user")

        refunds_result = await session.execute(
            select(Refund).where(Refund.user_id == user.id)
        )
        refunds_by_purchase = {
            refund.purchase_id: refund for refund in refunds_result.scalars()
        }
        now = datetime.now(timezone.utc)
        outcomes: list[dict[str, object]] = []
        for purchase, course in rows:
            existing_refund = refunds_by_purchase.get(purchase.id)
            if existing_refund is not None:
                refund = existing_refund
                existing_request = True
            elif purchase.payment_status != "paid":
                outcomes.append(
                    {
                        "refund_id": None,
                        "course_id": course.id,
                        "course_title": course.title,
                        "refund_status": "not_eligible",
                        "refund_amount": purchase.amount,
                        "reason": "Purchase is not in paid status",
                        "existing_request": False,
                    }
                )
                continue
            else:
                eligible = is_refund_window_open(purchase.purchased_at, now)
                if not eligible:
                    refund_status = "rejected"
                    processed_at = now
                elif PAYMENT_GATEWAY_MODE == "demo":
                    refund_status = "processed"
                    processed_at = now
                    purchase.payment_status = "refunded"
                else:
                    refund_status = "pending"
                    processed_at = None

                refund = Refund(
                    purchase_id=purchase.id,
                    user_id=user.id,
                    reason=reason,
                    refund_status=refund_status,
                    refund_amount=purchase.amount,
                    processed_at=processed_at,
                )
                session.add(refund)
                await session.flush()
                refunds_by_purchase[purchase.id] = refund
                existing_request = False

            outcomes.append(
                {
                    "refund_id": refund.id,
                    "course_id": course.id,
                    "course_title": course.title,
                    "refund_status": refund.refund_status,
                    "refund_amount": refund.refund_amount,
                    "reason": {
                        "processed": "Refund processed successfully.",
                        "pending": "Waiting for payment provider confirmation.",
                        "rejected": (
                            "The 7-day refund window has expired or the "
                            "purchase date is invalid."
                        ),
                    }.get(
                        refund.refund_status,
                        "A refund request already exists.",
                    ),
                    "request_reason": refund.reason,
                    "existing_request": existing_request,
                }
            )
        return outcomes
