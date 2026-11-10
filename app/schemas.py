from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    email: EmailStr


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    email: str
    created_at: datetime


class CourseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)


class CourseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str
    price: Decimal
    status: str
    created_at: datetime


class PurchaseCreate(BaseModel):
    user_id: UUID
    course_id: UUID


class PurchaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    course_id: UUID
    purchased_at: datetime
    amount: Decimal
    payment_status: str


class RefundRequest(BaseModel):
    email: EmailStr
    reason: str = Field(min_length=3, max_length=2000)


class RefundItemRead(BaseModel):
    refund_id: UUID | None
    course_id: UUID
    course_title: str
    refund_status: str
    refund_amount: Decimal
    reason: str
    request_reason: str | None = None
    existing_request: bool = False


class RefundEmailNotification(BaseModel):
    sent: bool
    recipient: EmailStr
    detail: str


class RefundBatchRead(BaseModel):
    email: EmailStr
    results: list[RefundItemRead]
    email_notification: RefundEmailNotification | None = None


class RefundRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    purchase_id: UUID
    user_id: UUID
    reason: str
    refund_status: str
    refund_amount: Decimal
    requested_at: datetime
    processed_at: datetime | None
