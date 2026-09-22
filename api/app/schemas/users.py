import uuid
from datetime import date, datetime

from pydantic import EmailStr

from app.schemas.common import (
    Locale,
    ORMModel,
    Region,
    Role,
    SubscriptionPlatform,
    SubscriptionStatus,
)


class UserRead(ORMModel):
    id: uuid.UUID
    email: EmailStr | None
    display_name: str
    locale: Locale
    region: Region
    role: Role
    age_gate_passed: bool
    created_at: datetime


class SubscriptionRead(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    platform: SubscriptionPlatform
    product_id: str
    status: SubscriptionStatus
    current_period_end: datetime | None
    updated_at: datetime


class TicketLedgerEntryRead(ORMModel):
    id: int
    user_id: uuid.UUID
    delta: int
    reason: str
    pack_id: uuid.UUID | None
    period_start: date | None
    created_at: datetime
