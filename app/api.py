from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import EmailStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Course, Purchase, Refund, User
from app.services.email import send_refund_email
from app.schemas import (
    CourseCreate,
    CourseRead,
    PurchaseCreate,
    PurchaseRead,
    RefundBatchRead,
    RefundRead,
    RefundRequest,
    UserCreate,
    UserRead,
)
from app.services.refunds import (
    RefundError,
    check_refunds_for_email,
    process_refunds_for_email,
)

router = APIRouter()


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate, session: AsyncSession = Depends(get_db)
) -> User:
    user = User(name=payload.name, email=payload.email.lower())
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Email is already registered") from exc
    await session.refresh(user)
    return user


@router.post("/courses", response_model=CourseRead, status_code=status.HTTP_201_CREATED)
async def create_course(
    payload: CourseCreate, session: AsyncSession = Depends(get_db)
) -> Course:
    course = Course(
        title=payload.title,
        description=payload.description,
        price=payload.price,
    )
    session.add(course)
    await session.commit()
    await session.refresh(course)
    return course


@router.get("/courses", response_model=list[CourseRead])
async def list_courses(session: AsyncSession = Depends(get_db)) -> list[Course]:
    result = await session.execute(
        select(Course).where(Course.status == "active").order_by(Course.title)
    )
    return list(result.scalars().all())


@router.post(
    "/purchases", response_model=PurchaseRead, status_code=status.HTTP_201_CREATED
)
async def buy_course(
    payload: PurchaseCreate, session: AsyncSession = Depends(get_db)
) -> Purchase:
    user = await session.get(User, payload.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    course = await session.get(Course, payload.course_id)
    if course is None or course.status != "active":
        raise HTTPException(status_code=404, detail="Active course not found")

    purchase = Purchase(
        user_id=user.id,
        course_id=course.id,
        amount=course.price,
        payment_status="paid",
    )
    session.add(purchase)
    await session.commit()
    await session.refresh(purchase)
    return purchase


@router.post("/refunds", response_model=RefundBatchRead)
async def request_refund(
    payload: RefundRequest, session: AsyncSession = Depends(get_db)
) -> dict[str, object]:
    try:
        results = await process_refunds_for_email(
            session, str(payload.email), payload.reason
        )
        email_notification = await send_refund_email(str(payload.email), results)
        return {
            "email": str(payload.email),
            "results": results,
            "email_notification": email_notification,
        }
    except RefundError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/refunds", response_model=RefundBatchRead)
async def list_refunds_for_email(
    email: EmailStr,
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    try:
        results = await check_refunds_for_email(session, str(email))
        return {"email": str(email), "results": results}
    except RefundError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/refunds/{refund_id}", response_model=RefundRead)
async def get_refund_status(
    refund_id: UUID,
    user_id: UUID = Query(...),
    session: AsyncSession = Depends(get_db),
) -> Refund:
    refund = await session.get(Refund, refund_id)
    if refund is None or refund.user_id != user_id:
        raise HTTPException(status_code=404, detail="Refund not found for this user")
    return refund


@router.get("/refund-eligibility", response_model=RefundBatchRead)
async def refund_eligibility(
    email: EmailStr,
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    try:
        results = await check_refunds_for_email(session, str(email))
        return {"email": str(email), "results": results}
    except RefundError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
